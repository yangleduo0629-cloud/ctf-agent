from enum import StrEnum


class CompetitionStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    FINISHED = "finished"
    ARCHIVED = "archived"


class ChallengeStatus(StrEnum):
    NEW = "new"
    INGESTED = "ingested"
    CLASSIFIED = "classified"
    READY = "ready"
    SOLVING = "solving"
    CANDIDATE = "candidate"
    VERIFYING = "verifying"
    SOLVED = "solved"
    SUBMITTED = "submitted"
    RETRY = "retry"
    REVIEW = "review"


class ArtifactRetentionPolicy(StrEnum):
    KEEP = "keep"
    DELETE_WITH_PARENT = "delete_with_parent"
    EXPIRES_AT = "expires_at"


class SolverRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CallStatus(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EvidenceKind(StrEnum):
    OBSERVATION = "observation"
    LOG = "log"
    SCREENSHOT = "screenshot"
    REPORT = "report"
    FILE = "file"


class FlagCandidateStatus(StrEnum):
    NEW = "new"
    VALIDATED = "validated"
    REJECTED = "rejected"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"


class SubmissionStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    ERROR = "error"


class EvalRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
