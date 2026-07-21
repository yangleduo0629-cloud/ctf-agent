from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.db.base import utc_now
from backend.db.enums import ChallengeStatus, SolverRunStatus
from backend.db.models import Challenge, Checkpoint, DomainEvent, SolverRun
from backend.orchestration.event_store import EventStore, IdempotencyConflictError
from backend.orchestration.state_machines import (
    CHALLENGE_TRANSITIONS,
    SOLVER_RUN_TRANSITIONS,
    validate_transition,
)


@dataclass(frozen=True)
class StateTransitionResult[StatusT: StrEnum]:
    aggregate_id: uuid.UUID
    status: StatusT
    version: int
    event: DomainEvent
    duplicate: bool


@dataclass(frozen=True)
class CheckpointResult:
    checkpoint_id: uuid.UUID
    solver_run_id: uuid.UUID
    sequence: int
    state: dict[str, Any]
    storage_key: str | None
    checksum: str
    event: DomainEvent
    duplicate: bool


class StateTransitionService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.events = EventStore(session)

    def transition_challenge(
        self,
        challenge_id: uuid.UUID,
        target: ChallengeStatus,
        *,
        idempotency_key: str,
        expected_version: int | None = None,
        reason: str | None = None,
        metadata: dict[str, Any] | None = None,
        correlation_id: uuid.UUID | None = None,
        causation_id: uuid.UUID | None = None,
    ) -> StateTransitionResult[ChallengeStatus]:
        challenge = self.session.scalar(
            select(Challenge)
            .where(Challenge.id == challenge_id, Challenge.deleted_at.is_(None))
            .with_for_update()
        )
        if challenge is None:
            raise LookupError(f"Challenge not found: {challenge_id}")

        duplicate = self._existing_transition(
            idempotency_key,
            aggregate_type="challenge",
            aggregate_id=challenge_id,
            event_type="challenge.state_changed",
            target=target,
        )
        if duplicate is not None:
            return StateTransitionResult(
                challenge_id,
                target,
                duplicate.aggregate_version,
                duplicate,
                True,
            )
        if expected_version is not None and challenge.version != expected_version:
            raise IdempotencyConflictError(
                f"challenge version mismatch: expected {expected_version}, got {challenge.version}"
            )

        source = challenge.status
        validate_transition(source, target, CHALLENGE_TRANSITIONS)
        challenge.status = target
        self.session.flush()
        event = self.events.append(
            aggregate_type="challenge",
            aggregate_id=challenge.id,
            aggregate_version=challenge.version,
            event_type="challenge.state_changed",
            payload={
                "from": source.value,
                "to": target.value,
                "reason": reason,
                "metadata": metadata or {},
            },
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )
        return StateTransitionResult(challenge.id, target, challenge.version, event, False)

    def transition_solver_run(
        self,
        solver_run_id: uuid.UUID,
        target: SolverRunStatus,
        *,
        idempotency_key: str,
        expected_version: int | None = None,
        reason: str | None = None,
        metadata: dict[str, Any] | None = None,
        correlation_id: uuid.UUID | None = None,
        causation_id: uuid.UUID | None = None,
    ) -> StateTransitionResult[SolverRunStatus]:
        solver_run = self.session.scalar(
            select(SolverRun)
            .where(SolverRun.id == solver_run_id, SolverRun.deleted_at.is_(None))
            .with_for_update()
        )
        if solver_run is None:
            raise LookupError(f"SolverRun not found: {solver_run_id}")

        duplicate = self._existing_transition(
            idempotency_key,
            aggregate_type="solver_run",
            aggregate_id=solver_run_id,
            event_type="solver_run.state_changed",
            target=target,
        )
        if duplicate is not None:
            return StateTransitionResult(
                solver_run_id,
                target,
                duplicate.aggregate_version,
                duplicate,
                True,
            )
        if expected_version is not None and solver_run.version != expected_version:
            raise IdempotencyConflictError(
                f"solver run version mismatch: expected {expected_version}, got {solver_run.version}"
            )

        source = solver_run.status
        validate_transition(source, target, SOLVER_RUN_TRANSITIONS)
        solver_run.status = target
        self._set_run_timestamps(solver_run, target)
        self.session.flush()
        event = self.events.append(
            aggregate_type="solver_run",
            aggregate_id=solver_run.id,
            aggregate_version=solver_run.version,
            event_type="solver_run.state_changed",
            payload={
                "from": source.value,
                "to": target.value,
                "reason": reason,
                "metadata": metadata or {},
            },
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            causation_id=causation_id,
        )
        return StateTransitionResult(solver_run.id, target, solver_run.version, event, False)

    def _existing_transition[StatusT: StrEnum](
        self,
        idempotency_key: str,
        *,
        aggregate_type: str,
        aggregate_id: uuid.UUID,
        event_type: str,
        target: StatusT,
    ) -> DomainEvent | None:
        existing = self.events.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            self.events.require_matching(
                existing,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                event_type=event_type,
                payload_values={"to": target.value},
            )
        return existing

    @staticmethod
    def _set_run_timestamps(solver_run: SolverRun, target: SolverRunStatus) -> None:
        now = utc_now()
        if target == SolverRunStatus.RUNNING and solver_run.started_at is None:
            solver_run.started_at = now
        if target in {
            SolverRunStatus.SUCCEEDED,
            SolverRunStatus.FAILED,
            SolverRunStatus.CANCELLED,
        }:
            solver_run.finished_at = now
        elif target == SolverRunStatus.QUEUED:
            solver_run.finished_at = None


class CheckpointService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.events = EventStore(session)

    def create(
        self,
        solver_run_id: uuid.UUID,
        sequence: int,
        state: dict[str, Any],
        *,
        idempotency_key: str,
        storage_key: str | None = None,
    ) -> CheckpointResult:
        solver_run = self._lock_solver_run(solver_run_id)
        existing_event = self.events.get_by_idempotency_key(idempotency_key)
        if existing_event is not None:
            self.events.require_matching(
                existing_event,
                aggregate_type="solver_run",
                aggregate_id=solver_run_id,
                event_type="solver_run.checkpoint_created",
                payload_values={"sequence": sequence},
            )
            checkpoint = self._checkpoint_from_event(existing_event)
            return self._result(checkpoint, existing_event, duplicate=True)

        checksum = checkpoint_checksum(state)
        checkpoint = self.session.scalar(
            select(Checkpoint).where(
                Checkpoint.solver_run_id == solver_run_id,
                Checkpoint.sequence == sequence,
                Checkpoint.deleted_at.is_(None),
            )
        )
        if checkpoint is not None:
            if checkpoint.checksum != checksum or checkpoint.storage_key != storage_key:
                raise IdempotencyConflictError(
                    f"checkpoint sequence {sequence} already has different content"
                )
            raise IdempotencyConflictError(
                f"checkpoint sequence {sequence} exists under another idempotency key"
            )

        latest_sequence = self.session.scalar(
            select(Checkpoint.sequence)
            .where(
                Checkpoint.solver_run_id == solver_run_id,
                Checkpoint.deleted_at.is_(None),
            )
            .order_by(Checkpoint.sequence.desc())
            .limit(1)
        )
        if latest_sequence is not None and sequence <= latest_sequence:
            raise ValueError(f"checkpoint sequence must be greater than {latest_sequence}")

        checkpoint = Checkpoint(
            solver_run_id=solver_run_id,
            sequence=sequence,
            state=state,
            storage_key=storage_key,
            checksum=checksum,
        )
        self.session.add(checkpoint)
        solver_run.updated_at = utc_now()
        self.session.flush()
        event = self.events.append(
            aggregate_type="solver_run",
            aggregate_id=solver_run_id,
            aggregate_version=solver_run.version,
            event_type="solver_run.checkpoint_created",
            payload={
                "checkpoint_id": str(checkpoint.id),
                "sequence": sequence,
                "checksum": checksum,
                "storage_key": storage_key,
            },
            idempotency_key=idempotency_key,
        )
        return self._result(checkpoint, event, duplicate=False)

    def restore(
        self,
        solver_run_id: uuid.UUID,
        *,
        idempotency_key: str,
        sequence: int | None = None,
    ) -> CheckpointResult:
        solver_run = self._lock_solver_run(solver_run_id)
        existing_event = self.events.get_by_idempotency_key(idempotency_key)
        if existing_event is not None:
            expected_values: dict[str, object] = {}
            if sequence is not None:
                expected_values["sequence"] = sequence
            self.events.require_matching(
                existing_event,
                aggregate_type="solver_run",
                aggregate_id=solver_run_id,
                event_type="solver_run.checkpoint_restored",
                payload_values=expected_values,
            )
            checkpoint = self._checkpoint_from_event(existing_event)
            return self._result(checkpoint, existing_event, duplicate=True)

        statement = select(Checkpoint).where(
            Checkpoint.solver_run_id == solver_run_id,
            Checkpoint.deleted_at.is_(None),
        )
        if sequence is not None:
            statement = statement.where(Checkpoint.sequence == sequence)
        checkpoint = self.session.scalar(statement.order_by(Checkpoint.sequence.desc()).limit(1))
        if checkpoint is None:
            raise LookupError(f"Checkpoint not found for solver run: {solver_run_id}")

        checksum = checkpoint_checksum(checkpoint.state)
        if checkpoint.checksum is not None and checkpoint.checksum != checksum:
            raise ValueError(f"checkpoint checksum mismatch: {checkpoint.id}")
        checkpoint.checksum = checksum
        metrics = dict(solver_run.metrics)
        metrics["resume_checkpoint"] = {
            "id": str(checkpoint.id),
            "sequence": checkpoint.sequence,
            "restored_at": utc_now().isoformat(),
        }
        solver_run.metrics = metrics
        self.session.flush()
        event = self.events.append(
            aggregate_type="solver_run",
            aggregate_id=solver_run_id,
            aggregate_version=solver_run.version,
            event_type="solver_run.checkpoint_restored",
            payload={
                "checkpoint_id": str(checkpoint.id),
                "sequence": checkpoint.sequence,
                "checksum": checksum,
                "storage_key": checkpoint.storage_key,
            },
            idempotency_key=idempotency_key,
        )
        return self._result(checkpoint, event, duplicate=False)

    def _lock_solver_run(self, solver_run_id: uuid.UUID) -> SolverRun:
        solver_run = self.session.scalar(
            select(SolverRun)
            .where(SolverRun.id == solver_run_id, SolverRun.deleted_at.is_(None))
            .with_for_update()
        )
        if solver_run is None:
            raise LookupError(f"SolverRun not found: {solver_run_id}")
        return solver_run

    def _checkpoint_from_event(self, event: DomainEvent) -> Checkpoint:
        checkpoint_id = uuid.UUID(str(event.payload["checkpoint_id"]))
        checkpoint = self.session.get(Checkpoint, checkpoint_id)
        if checkpoint is None:
            raise LookupError(f"Checkpoint not found: {checkpoint_id}")
        return checkpoint

    @staticmethod
    def _result(
        checkpoint: Checkpoint,
        event: DomainEvent,
        *,
        duplicate: bool,
    ) -> CheckpointResult:
        checksum = checkpoint.checksum or checkpoint_checksum(checkpoint.state)
        return CheckpointResult(
            checkpoint.id,
            checkpoint.solver_run_id,
            checkpoint.sequence,
            checkpoint.state,
            checkpoint.storage_key,
            checksum,
            event,
            duplicate,
        )


def checkpoint_checksum(state: dict[str, Any]) -> str:
    canonical = json.dumps(
        state,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()
