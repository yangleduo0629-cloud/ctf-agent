from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.db.enums import ChallengeStatus, SolverRunStatus
from backend.db.models import Challenge, Checkpoint, Competition, DomainEvent, SolverRun
from backend.orchestration.event_store import EventStore, IdempotencyConflictError
from backend.orchestration.services import CheckpointService, StateTransitionService
from backend.orchestration.state_machines import InvalidTransitionError


def test_challenge_state_machine_full_path_and_idempotency(session: Session) -> None:
    challenge, _solver_run = _domain_fixture(session)
    service = StateTransitionService(session)
    path = (
        ChallengeStatus.INGESTED,
        ChallengeStatus.CLASSIFIED,
        ChallengeStatus.READY,
        ChallengeStatus.SOLVING,
        ChallengeStatus.CANDIDATE,
        ChallengeStatus.VERIFYING,
        ChallengeStatus.SOLVED,
        ChallengeStatus.SUBMITTED,
    )

    first_result = None
    for target in path:
        result = service.transition_challenge(
            challenge.id,
            target,
            idempotency_key=f"challenge:{challenge.id}:{target.value}",
        )
        first_result = first_result or result
        assert result.status == target
        assert result.duplicate is False

    assert challenge.status == ChallengeStatus.SUBMITTED
    assert session.scalar(select(func.count()).select_from(DomainEvent)) == len(path)

    assert first_result is not None
    duplicate = service.transition_challenge(
        challenge.id,
        ChallengeStatus.INGESTED,
        idempotency_key=f"challenge:{challenge.id}:ingested",
    )
    assert duplicate.duplicate is True
    assert duplicate.event.id == first_result.event.id
    assert session.scalar(select(func.count()).select_from(DomainEvent)) == len(path)


def test_challenge_state_machine_rejects_invalid_and_conflicting_updates(
    session: Session,
) -> None:
    challenge, _solver_run = _domain_fixture(session)
    service = StateTransitionService(session)

    with pytest.raises(InvalidTransitionError, match="new -> solving"):
        service.transition_challenge(
            challenge.id,
            ChallengeStatus.SOLVING,
            idempotency_key="invalid-transition",
        )

    service.transition_challenge(
        challenge.id,
        ChallengeStatus.INGESTED,
        idempotency_key="shared-key",
    )
    with pytest.raises(IdempotencyConflictError, match="another operation"):
        service.transition_challenge(
            challenge.id,
            ChallengeStatus.CLASSIFIED,
            idempotency_key="shared-key",
        )


def test_solver_run_state_machine_supports_restart_retry(session: Session) -> None:
    _challenge, solver_run = _domain_fixture(session)
    service = StateTransitionService(session)

    service.transition_solver_run(
        solver_run.id,
        SolverRunStatus.RUNNING,
        idempotency_key="run-start",
    )
    service.transition_solver_run(
        solver_run.id,
        SolverRunStatus.QUEUED,
        idempotency_key="run-requeue-after-lease-loss",
    )
    service.transition_solver_run(
        solver_run.id,
        SolverRunStatus.RUNNING,
        idempotency_key="run-resume",
    )
    service.transition_solver_run(
        solver_run.id,
        SolverRunStatus.SUCCEEDED,
        idempotency_key="run-finish",
    )

    assert solver_run.status == SolverRunStatus.SUCCEEDED
    assert solver_run.started_at is not None
    assert solver_run.finished_at is not None


def test_domain_events_reject_update_and_delete(session: Session) -> None:
    challenge, _solver_run = _domain_fixture(session)
    result = StateTransitionService(session).transition_challenge(
        challenge.id,
        ChallengeStatus.INGESTED,
        idempotency_key="immutable-event",
    )
    event_id = result.event.id
    session.commit()

    result.event.payload = {"tampered": True}
    with pytest.raises(TypeError, match="append-only"):
        session.flush()
    session.rollback()

    event = session.scalar(select(DomainEvent).where(DomainEvent.id == event_id))
    assert event is not None
    session.delete(event)
    with pytest.raises(TypeError, match="append-only"):
        session.flush()


def test_checkpoint_create_restore_and_checksum_validation(session: Session) -> None:
    _challenge, solver_run = _domain_fixture(session)
    checkpoints = CheckpointService(session)
    state = {"phase": "solving", "messages": ["one", "two"], "attempt": 1}

    created = checkpoints.create(
        solver_run.id,
        1,
        state,
        idempotency_key="checkpoint-create-1",
        storage_key="checkpoints/run/1.json",
    )
    duplicate = checkpoints.create(
        solver_run.id,
        1,
        state,
        idempotency_key="checkpoint-create-1",
        storage_key="checkpoints/run/1.json",
    )
    restored = checkpoints.restore(
        solver_run.id,
        idempotency_key="checkpoint-restore-1",
    )
    restored_duplicate = checkpoints.restore(
        solver_run.id,
        idempotency_key="checkpoint-restore-1",
    )

    assert duplicate.duplicate is True
    assert restored.state == state
    assert restored.checksum == created.checksum
    assert restored_duplicate.duplicate is True
    assert solver_run.metrics["resume_checkpoint"]["sequence"] == 1

    checkpoint = session.get(Checkpoint, created.checkpoint_id)
    assert checkpoint is not None
    checkpoint.state = {"tampered": True}
    with pytest.raises(ValueError, match="checksum mismatch"):
        checkpoints.restore(
            solver_run.id,
            idempotency_key="checkpoint-restore-tampered",
        )


def test_event_store_lists_global_sequence(session: Session) -> None:
    challenge, solver_run = _domain_fixture(session)
    service = StateTransitionService(session)
    service.transition_challenge(
        challenge.id,
        ChallengeStatus.INGESTED,
        idempotency_key="event-order-1",
    )
    service.transition_solver_run(
        solver_run.id,
        SolverRunStatus.RUNNING,
        idempotency_key="event-order-2",
    )

    events = EventStore(session).list_after(0)
    assert [event.sequence for event in events] == sorted(event.sequence for event in events)
    assert [event.event_type for event in events] == [
        "challenge.state_changed",
        "solver_run.state_changed",
    ]


def _domain_fixture(session: Session) -> tuple[Challenge, SolverRun]:
    marker = uuid.uuid4().hex
    competition = Competition(name=f"State fixture {marker}", slug=f"state-{marker}")
    session.add(competition)
    session.flush()
    challenge = Challenge(
        competition_id=competition.id,
        slug="state-machine",
        name="State Machine",
        category="test",
    )
    session.add(challenge)
    session.flush()
    solver_run = SolverRun(
        challenge_id=challenge.id,
        run_key="run-1",
        solver_type="test",
        model_spec="test/model",
    )
    session.add(solver_run)
    session.flush()
    return challenge, solver_run
