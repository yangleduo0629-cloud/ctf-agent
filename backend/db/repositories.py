from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ColumnElement, Select, select
from sqlalchemy.orm import Session, selectinload

from backend.db.base import RecordMixin, utc_now
from backend.db.enums import ArtifactRetentionPolicy
from backend.db.models import (
    Artifact,
    Challenge,
    Checkpoint,
    Competition,
    EvalRun,
    Evidence,
    FlagCandidate,
    ModelCall,
    SolverRun,
    Submission,
    ToolCall,
)


class Repository[ModelT: RecordMixin]:
    def __init__(self, session: Session, model: type[ModelT]) -> None:
        self.session = session
        self.model = model

    def add(self, entity: ModelT) -> ModelT:
        self.session.add(entity)
        self.session.flush()
        return entity

    def get(self, entity_id: uuid.UUID, *, include_deleted: bool = False) -> ModelT | None:
        statement = select(self.model).where(self.model.id == entity_id)
        if not include_deleted:
            statement = statement.where(self.model.deleted_at.is_(None))
        return self.session.scalar(statement)

    def list_all(
        self,
        *,
        include_deleted: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ModelT]:
        statement = select(self.model)
        if not include_deleted:
            statement = statement.where(self.model.deleted_at.is_(None))
        statement = statement.order_by(self.model.created_at).limit(limit).offset(offset)
        return list(self.session.scalars(statement))

    def soft_delete(self, entity: ModelT, *, deleted_at: datetime | None = None) -> ModelT:
        entity.deleted_at = deleted_at or utc_now()
        self.session.flush()
        return entity


class CompetitionRepository(Repository[Competition]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, Competition)

    def get_by_slug(self, slug: str, *, include_deleted: bool = False) -> Competition | None:
        statement = select(Competition).where(Competition.slug == slug)
        if not include_deleted:
            statement = statement.where(Competition.deleted_at.is_(None))
        return self.session.scalar(statement)

    def get_graph(self, competition_id: uuid.UUID) -> Competition | None:
        statement = (
            select(Competition)
            .where(Competition.id == competition_id, Competition.deleted_at.is_(None))
            .options(
                selectinload(Competition.challenges).selectinload(Challenge.artifacts),
                selectinload(Competition.challenges).selectinload(Challenge.solver_runs),
                selectinload(Competition.eval_runs),
            )
        )
        return self.session.scalar(statement)


class ChallengeRepository(Repository[Challenge]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, Challenge)

    def get_by_external_id(
        self,
        competition_id: uuid.UUID,
        external_id: str,
    ) -> Challenge | None:
        return self.session.scalar(
            select(Challenge).where(
                Challenge.competition_id == competition_id,
                Challenge.external_id == external_id,
                Challenge.deleted_at.is_(None),
            )
        )


class ArtifactRepository(Repository[Artifact]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, Artifact)

    def due_for_purge(self, *, as_of: datetime | None = None) -> list[Artifact]:
        cutoff = as_of or utc_now()
        statement = (
            select(Artifact)
            .where(
                Artifact.deleted_at.is_not(None),
                Artifact.purged_at.is_(None),
                Artifact.retention_policy != ArtifactRetentionPolicy.KEEP,
                Artifact.retain_until.is_not(None),
                Artifact.retain_until <= cutoff,
            )
            .order_by(Artifact.retain_until, Artifact.created_at)
        )
        return list(self.session.scalars(statement))

    def mark_purged(self, artifact: Artifact, *, purged_at: datetime | None = None) -> Artifact:
        artifact.purged_at = purged_at or utc_now()
        self.session.flush()
        return artifact


class SolverRunRepository(Repository[SolverRun]):
    def __init__(self, session: Session) -> None:
        super().__init__(session, SolverRun)

    def get_timeline(self, solver_run_id: uuid.UUID) -> SolverRun | None:
        statement = (
            select(SolverRun)
            .where(SolverRun.id == solver_run_id, SolverRun.deleted_at.is_(None))
            .options(
                selectinload(SolverRun.checkpoints),
                selectinload(SolverRun.model_calls),
                selectinload(SolverRun.tool_calls),
                selectinload(SolverRun.evidence),
                selectinload(SolverRun.flag_candidates),
                selectinload(SolverRun.submissions),
            )
        )
        return self.session.scalar(statement)


@dataclass(frozen=True)
class DeletionReport:
    deleted_records: int
    retained_artifacts: tuple[str, ...]
    purgeable_artifacts: tuple[str, ...]


class Repositories:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.competitions = CompetitionRepository(session)
        self.challenges = ChallengeRepository(session)
        self.artifacts = ArtifactRepository(session)
        self.solver_runs = SolverRunRepository(session)
        self.checkpoints = Repository(session, Checkpoint)
        self.model_calls = Repository(session, ModelCall)
        self.tool_calls = Repository(session, ToolCall)
        self.evidence = Repository(session, Evidence)
        self.flag_candidates = Repository(session, FlagCandidate)
        self.submissions = Repository(session, Submission)
        self.eval_runs = Repository(session, EvalRun)

    def soft_delete_competition(
        self,
        competition_id: uuid.UUID,
        *,
        deleted_at: datetime | None = None,
    ) -> DeletionReport:
        timestamp = deleted_at or utc_now()
        competition = self.competitions.get(competition_id)
        if competition is None:
            raise LookupError(f"Competition not found: {competition_id}")

        challenges = list(
            self.session.scalars(
                select(Challenge).where(
                    Challenge.competition_id == competition_id,
                    Challenge.deleted_at.is_(None),
                )
            )
        )
        challenge_ids = [challenge.id for challenge in challenges]
        runs = self._active(SolverRun, SolverRun.challenge_id.in_(challenge_ids))
        run_ids = [run.id for run in runs]

        dependent_records: list[RecordMixin] = [competition, *challenges, *runs]
        if run_ids:
            dependent_records.extend(
                self._active(Checkpoint, Checkpoint.solver_run_id.in_(run_ids))
            )
            dependent_records.extend(self._active(ModelCall, ModelCall.solver_run_id.in_(run_ids)))
            dependent_records.extend(self._active(ToolCall, ToolCall.solver_run_id.in_(run_ids)))
        if challenge_ids:
            dependent_records.extend(
                self._active(Evidence, Evidence.challenge_id.in_(challenge_ids))
            )
            dependent_records.extend(
                self._active(FlagCandidate, FlagCandidate.challenge_id.in_(challenge_ids))
            )
            dependent_records.extend(
                self._active(Submission, Submission.challenge_id.in_(challenge_ids))
            )
        dependent_records.extend(self._active(EvalRun, EvalRun.competition_id == competition_id))

        for record in dependent_records:
            record.deleted_at = timestamp

        retained: list[str] = []
        purgeable: list[str] = []
        artifacts = self._active(Artifact, Artifact.challenge_id.in_(challenge_ids))
        for artifact in artifacts:
            if artifact.retention_policy == ArtifactRetentionPolicy.KEEP:
                retained.append(artifact.storage_key)
                continue
            artifact.deleted_at = timestamp
            if artifact.retention_policy == ArtifactRetentionPolicy.DELETE_WITH_PARENT:
                artifact.retain_until = timestamp
                purgeable.append(artifact.storage_key)
            elif artifact.retain_until and artifact.retain_until <= timestamp:
                purgeable.append(artifact.storage_key)
            else:
                retained.append(artifact.storage_key)

        self.session.flush()
        return DeletionReport(
            deleted_records=len(dependent_records),
            retained_artifacts=tuple(sorted(retained)),
            purgeable_artifacts=tuple(sorted(purgeable)),
        )

    def _active[EntityT: RecordMixin](
        self,
        model: type[EntityT],
        predicate: ColumnElement[bool],
    ) -> list[EntityT]:
        statement: Select[tuple[EntityT]] = select(model).where(
            predicate,
            model.deleted_at.is_(None),
        )
        return list(self.session.scalars(statement))
