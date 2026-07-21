from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from backend.db.enums import ChallengeStatus, SolverRunStatus


class InvalidTransitionError(ValueError):
    pass


CHALLENGE_TRANSITIONS: Mapping[ChallengeStatus, frozenset[ChallengeStatus]] = {
    ChallengeStatus.NEW: frozenset({ChallengeStatus.INGESTED}),
    ChallengeStatus.INGESTED: frozenset({ChallengeStatus.CLASSIFIED}),
    ChallengeStatus.CLASSIFIED: frozenset({ChallengeStatus.READY, ChallengeStatus.REVIEW}),
    ChallengeStatus.READY: frozenset({ChallengeStatus.SOLVING}),
    ChallengeStatus.SOLVING: frozenset(
        {ChallengeStatus.CANDIDATE, ChallengeStatus.RETRY, ChallengeStatus.REVIEW}
    ),
    ChallengeStatus.CANDIDATE: frozenset({ChallengeStatus.VERIFYING}),
    ChallengeStatus.VERIFYING: frozenset(
        {ChallengeStatus.SOLVED, ChallengeStatus.RETRY, ChallengeStatus.REVIEW}
    ),
    ChallengeStatus.RETRY: frozenset({ChallengeStatus.READY, ChallengeStatus.SOLVING}),
    ChallengeStatus.REVIEW: frozenset(
        {ChallengeStatus.READY, ChallengeStatus.VERIFYING, ChallengeStatus.SOLVED}
    ),
    ChallengeStatus.SOLVED: frozenset({ChallengeStatus.SUBMITTED}),
    ChallengeStatus.SUBMITTED: frozenset(),
}

SOLVER_RUN_TRANSITIONS: Mapping[SolverRunStatus, frozenset[SolverRunStatus]] = {
    SolverRunStatus.QUEUED: frozenset({SolverRunStatus.RUNNING, SolverRunStatus.CANCELLED}),
    SolverRunStatus.RUNNING: frozenset(
        {
            SolverRunStatus.QUEUED,
            SolverRunStatus.SUCCEEDED,
            SolverRunStatus.FAILED,
            SolverRunStatus.CANCELLED,
        }
    ),
    SolverRunStatus.FAILED: frozenset({SolverRunStatus.QUEUED, SolverRunStatus.CANCELLED}),
    SolverRunStatus.SUCCEEDED: frozenset(),
    SolverRunStatus.CANCELLED: frozenset(),
}


def validate_transition[StatusT: StrEnum](
    current: StatusT,
    target: StatusT,
    transitions: Mapping[StatusT, frozenset[StatusT]],
) -> None:
    if target not in transitions[current]:
        raise InvalidTransitionError(f"invalid transition: {current.value} -> {target.value}")
