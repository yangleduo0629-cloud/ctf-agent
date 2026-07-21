from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine, inspect
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from backend.db import (
    Artifact,
    ArtifactRetentionPolicy,
    CallStatus,
    Challenge,
    ChallengeStatus,
    Checkpoint,
    Competition,
    CompetitionStatus,
    EvalRun,
    EvalRunStatus,
    Evidence,
    EvidenceKind,
    FlagCandidate,
    FlagCandidateStatus,
    ModelCall,
    Repositories,
    SolverRun,
    SolverRunStatus,
    Submission,
    SubmissionStatus,
    ToolCall,
)
from backend.db.base import utc_now

ENTITY_TYPES = (
    Competition,
    Challenge,
    Artifact,
    SolverRun,
    Checkpoint,
    ModelCall,
    ToolCall,
    Evidence,
    FlagCandidate,
    Submission,
    EvalRun,
)


def test_all_entities_have_lifecycle_columns() -> None:
    for entity_type in ENTITY_TYPES:
        columns = {column.key for column in inspect(entity_type).columns}
        assert {"id", "created_at", "updated_at", "version", "deleted_at"} <= columns


def test_complete_domain_graph_round_trip(session: Session) -> None:
    repositories = Repositories(session)
    competition = repositories.competitions.add(
        Competition(
            name="BSides Qualification",
            slug="bsides-qualification",
            status=CompetitionStatus.ACTIVE,
        )
    )
    challenge = repositories.challenges.add(
        Challenge(
            competition_id=competition.id,
            external_id="42",
            slug="cold-coffee",
            name="Cold Coffee",
            category="web",
            points=500,
            status=ChallengeStatus.ACTIVE,
        )
    )
    artifact = repositories.artifacts.add(
        Artifact(
            challenge_id=challenge.id,
            original_name="challenge.zip",
            storage_key="competitions/bsides/challenges/42/challenge.zip",
            content_type="application/zip",
            size_bytes=4096,
            sha256="a" * 64,
        )
    )
    solver_run = repositories.solver_runs.add(
        SolverRun(
            challenge_id=challenge.id,
            run_key="run-1",
            solver_type="codex",
            model_spec="codex/gpt-5.4",
            status=SolverRunStatus.RUNNING,
        )
    )
    checkpoint = repositories.checkpoints.add(
        Checkpoint(
            solver_run_id=solver_run.id,
            sequence=1,
            storage_key="checkpoints/run-1/1.json",
            state={"phase": "recon"},
        )
    )
    model_call = repositories.model_calls.add(
        ModelCall(
            solver_run_id=solver_run.id,
            sequence=1,
            provider="codex",
            model="gpt-5.4",
            status=CallStatus.SUCCEEDED,
            request={"input": "inspect"},
            response={"output": "done"},
            input_tokens=10,
            output_tokens=5,
            cost_usd=Decimal("0.00100000"),
        )
    )
    tool_call = repositories.tool_calls.add(
        ToolCall(
            solver_run_id=solver_run.id,
            model_call_id=model_call.id,
            sequence=1,
            tool_name="read_file",
            status=CallStatus.SUCCEEDED,
            arguments={"path": "challenge.zip"},
            result={"size": 4096},
        )
    )
    evidence = repositories.evidence.add(
        Evidence(
            challenge_id=challenge.id,
            solver_run_id=solver_run.id,
            tool_call_id=tool_call.id,
            artifact_id=artifact.id,
            kind=EvidenceKind.FILE,
            title="Attachment inspected",
            content="Archive contains the fixture.",
        )
    )
    candidate = repositories.flag_candidates.add(
        FlagCandidate(
            challenge_id=challenge.id,
            solver_run_id=solver_run.id,
            evidence_id=evidence.id,
            value="flag{fixture}",
            confidence=Decimal("0.9900"),
            status=FlagCandidateStatus.VALIDATED,
        )
    )
    submission = repositories.submissions.add(
        Submission(
            challenge_id=challenge.id,
            solver_run_id=solver_run.id,
            flag_candidate_id=candidate.id,
            value=candidate.value,
            status=SubmissionStatus.ACCEPTED,
            response="correct",
        )
    )
    eval_run = repositories.eval_runs.add(
        EvalRun(
            competition_id=competition.id,
            name="baseline",
            status=EvalRunStatus.SUCCEEDED,
            configuration={"models": ["codex/gpt-5.4"]},
            summary={"solved": 1},
        )
    )
    session.commit()

    entity_ids = {
        type(entity): entity.id
        for entity in (
            competition,
            challenge,
            artifact,
            solver_run,
            checkpoint,
            model_call,
            tool_call,
            evidence,
            candidate,
            submission,
            eval_run,
        )
    }
    session.expire_all()

    graph = repositories.competitions.get_graph(entity_ids[Competition])
    timeline = repositories.solver_runs.get_timeline(entity_ids[SolverRun])
    event = repositories.tool_calls.get(entity_ids[ToolCall])

    assert graph is not None
    assert graph.challenges[0].artifacts[0].original_name == "challenge.zip"
    assert graph.eval_runs[0].summary == {"solved": 1}
    assert timeline is not None
    assert timeline.checkpoints[0].state == {"phase": "recon"}
    assert timeline.model_calls[0].response == {"output": "done"}
    assert timeline.evidence[0].artifact_id == artifact.id
    assert timeline.flag_candidates[0].value == "flag{fixture}"
    assert timeline.submissions[0].status == SubmissionStatus.ACCEPTED
    assert event is not None
    assert event.result == {"size": 4096}

    previous_updated_at = challenge.updated_at
    challenge.status = ChallengeStatus.SOLVED
    session.commit()
    assert challenge.version == 2
    assert _as_utc(challenge.updated_at) >= _as_utc(previous_updated_at)


def test_competition_deletion_respects_artifact_retention(session: Session) -> None:
    repositories = Repositories(session)
    now = utc_now()
    competition = repositories.competitions.add(
        Competition(name="Retention fixture", slug="retention-fixture")
    )
    challenge = repositories.challenges.add(
        Challenge(competition_id=competition.id, slug="retention", name="Retention")
    )
    solver_run = repositories.solver_runs.add(
        SolverRun(
            challenge_id=challenge.id,
            run_key="retention-run",
            solver_type="fixture",
            model_spec="fixture/model",
        )
    )
    repositories.checkpoints.add(
        Checkpoint(solver_run_id=solver_run.id, sequence=0, state={"ready": True})
    )
    repositories.submissions.add(
        Submission(
            challenge_id=challenge.id,
            value="flag{manual}",
            status=SubmissionStatus.REJECTED,
        )
    )

    keep = repositories.artifacts.add(_artifact(challenge, "keep", ArtifactRetentionPolicy.KEEP))
    delete_with_parent = repositories.artifacts.add(
        _artifact(challenge, "parent", ArtifactRetentionPolicy.DELETE_WITH_PARENT)
    )
    expired = repositories.artifacts.add(
        _artifact(
            challenge,
            "expired",
            ArtifactRetentionPolicy.EXPIRES_AT,
            retain_until=now - timedelta(days=1),
        )
    )
    future = repositories.artifacts.add(
        _artifact(
            challenge,
            "future",
            ArtifactRetentionPolicy.EXPIRES_AT,
            retain_until=now + timedelta(days=30),
        )
    )
    session.commit()

    report = repositories.soft_delete_competition(competition.id, deleted_at=now)
    session.commit()

    assert repositories.competitions.get(competition.id) is None
    assert repositories.challenges.get(challenge.id) is None
    assert repositories.competitions.get(competition.id, include_deleted=True) is not None
    assert repositories.submissions.list_all() == []
    assert keep.deleted_at is None
    assert delete_with_parent.deleted_at == now
    assert expired.deleted_at == now
    assert future.deleted_at == now
    assert report.retained_artifacts == (future.storage_key, keep.storage_key)
    assert report.purgeable_artifacts == (expired.storage_key, delete_with_parent.storage_key)

    due = repositories.artifacts.due_for_purge(as_of=now)
    assert {artifact.storage_key for artifact in due} == {
        delete_with_parent.storage_key,
        expired.storage_key,
    }
    repositories.artifacts.mark_purged(delete_with_parent, purged_at=now)
    session.commit()
    assert delete_with_parent.purged_at == now
    assert delete_with_parent not in repositories.artifacts.due_for_purge(as_of=now)


def test_version_rejects_stale_updates(engine: Engine) -> None:
    with Session(engine) as seed_session:
        competition = Competition(name="Version fixture", slug="version-fixture")
        seed_session.add(competition)
        seed_session.commit()
        competition_id = competition.id

    with Session(engine) as first_session, Session(engine) as stale_session:
        current = first_session.get(Competition, competition_id)
        stale = stale_session.get(Competition, competition_id)
        assert current is not None
        assert stale is not None

        current.name = "Current update"
        first_session.commit()
        assert current.version == 2

        stale.name = "Stale update"
        with pytest.raises(StaleDataError):
            stale_session.commit()


def _artifact(
    challenge: Challenge,
    suffix: str,
    policy: ArtifactRetentionPolicy,
    *,
    retain_until=None,
) -> Artifact:
    return Artifact(
        challenge_id=challenge.id,
        original_name=f"{suffix}.bin",
        storage_key=f"retention/{suffix}.bin",
        content_type="application/octet-stream",
        size_bytes=1,
        sha256=suffix[0] * 64,
        retention_policy=policy,
        retain_until=retain_until,
    )


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
