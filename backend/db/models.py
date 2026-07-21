from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.db.base import Base, RecordMixin, utc_now
from backend.db.enums import (
    ArtifactRetentionPolicy,
    CallStatus,
    ChallengeStatus,
    CompetitionStatus,
    EvalRunStatus,
    EvidenceKind,
    FlagCandidateStatus,
    SolverRunStatus,
    SubmissionStatus,
)


def enum_column(enum_class: type[StrEnum], name: str) -> Enum:
    return Enum(
        enum_class,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda values: [value.value for value in values],
    )


class Competition(RecordMixin, Base):
    __tablename__ = "competitions"
    __table_args__ = (
        UniqueConstraint("platform", "external_id", name="uq_competitions_platform_external_id"),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    platform: Mapped[str] = mapped_column(String(64), nullable=False, default="ctfd")
    external_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[CompetitionStatus] = mapped_column(
        enum_column(CompetitionStatus, "competition_status"),
        nullable=False,
        default=CompetitionStatus.DRAFT,
    )
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    challenges: Mapped[list[Challenge]] = relationship(back_populates="competition")
    eval_runs: Mapped[list[EvalRun]] = relationship(back_populates="competition")


class Challenge(RecordMixin, Base):
    __tablename__ = "challenges"
    __table_args__ = (
        UniqueConstraint("competition_id", "slug", name="uq_challenges_competition_slug"),
        UniqueConstraint(
            "competition_id",
            "external_id",
            name="uq_challenges_competition_external_id",
        ),
        Index("ix_challenges_competition_status", "competition_id", "status"),
    )

    competition_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("competitions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    external_id: Mapped[str | None] = mapped_column(String(128))
    slug: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    points: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    connection_info: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ChallengeStatus] = mapped_column(
        enum_column(ChallengeStatus, "challenge_status"),
        nullable=False,
        default=ChallengeStatus.PENDING,
    )
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    competition: Mapped[Competition] = relationship(back_populates="challenges")
    artifacts: Mapped[list[Artifact]] = relationship(back_populates="challenge")
    solver_runs: Mapped[list[SolverRun]] = relationship(back_populates="challenge")
    evidence: Mapped[list[Evidence]] = relationship(back_populates="challenge")
    flag_candidates: Mapped[list[FlagCandidate]] = relationship(back_populates="challenge")
    submissions: Mapped[list[Submission]] = relationship(back_populates="challenge")


class Artifact(RecordMixin, Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        CheckConstraint("size_bytes >= 0", name="size_nonnegative"),
        CheckConstraint(
            "retention_policy != 'expires_at' OR retain_until IS NOT NULL",
            name="expiry_required",
        ),
        Index("ix_artifacts_challenge_sha256", "challenge_id", "sha256"),
        Index("ix_artifacts_retention", "deleted_at", "retain_until", "purged_at"),
    )

    challenge_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("challenges.id", ondelete="RESTRICT"),
        nullable=False,
    )
    original_name: Mapped[str] = mapped_column(String(512), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False, unique=True)
    content_type: Mapped[str | None] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    retention_policy: Mapped[ArtifactRetentionPolicy] = mapped_column(
        enum_column(ArtifactRetentionPolicy, "artifact_retention_policy"),
        nullable=False,
        default=ArtifactRetentionPolicy.KEEP,
    )
    retain_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    challenge: Mapped[Challenge] = relationship(back_populates="artifacts")
    evidence: Mapped[list[Evidence]] = relationship(back_populates="artifact")


class SolverRun(RecordMixin, Base):
    __tablename__ = "solver_runs"
    __table_args__ = (
        UniqueConstraint("challenge_id", "run_key", name="uq_solver_runs_challenge_run_key"),
        CheckConstraint("attempt > 0", name="attempt_positive"),
        Index("ix_solver_runs_challenge_status", "challenge_id", "status"),
    )

    challenge_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("challenges.id", ondelete="RESTRICT"),
        nullable=False,
    )
    run_key: Mapped[str] = mapped_column(String(128), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    solver_type: Mapped[str] = mapped_column(String(64), nullable=False)
    model_spec: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[SolverRunStatus] = mapped_column(
        enum_column(SolverRunStatus, "solver_run_status"),
        nullable=False,
        default=SolverRunStatus.QUEUED,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result_summary: Mapped[str | None] = mapped_column(Text)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    challenge: Mapped[Challenge] = relationship(back_populates="solver_runs")
    checkpoints: Mapped[list[Checkpoint]] = relationship(back_populates="solver_run")
    model_calls: Mapped[list[ModelCall]] = relationship(back_populates="solver_run")
    tool_calls: Mapped[list[ToolCall]] = relationship(back_populates="solver_run")
    evidence: Mapped[list[Evidence]] = relationship(back_populates="solver_run")
    flag_candidates: Mapped[list[FlagCandidate]] = relationship(back_populates="solver_run")
    submissions: Mapped[list[Submission]] = relationship(back_populates="solver_run")


class Checkpoint(RecordMixin, Base):
    __tablename__ = "checkpoints"
    __table_args__ = (
        UniqueConstraint("solver_run_id", "sequence", name="uq_checkpoints_run_sequence"),
        CheckConstraint("sequence >= 0", name="sequence_nonnegative"),
    )

    solver_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("solver_runs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_key: Mapped[str | None] = mapped_column(String(1024))
    state: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    solver_run: Mapped[SolverRun] = relationship(back_populates="checkpoints")


class ModelCall(RecordMixin, Base):
    __tablename__ = "model_calls"
    __table_args__ = (
        UniqueConstraint("solver_run_id", "sequence", name="uq_model_calls_run_sequence"),
        CheckConstraint("sequence >= 0", name="sequence_nonnegative"),
        CheckConstraint("input_tokens >= 0", name="input_tokens_nonnegative"),
        CheckConstraint("output_tokens >= 0", name="output_tokens_nonnegative"),
        CheckConstraint("cache_read_tokens >= 0", name="cache_tokens_nonnegative"),
    )

    solver_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("solver_runs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[CallStatus] = mapped_column(
        enum_column(CallStatus, "model_call_status"),
        nullable=False,
        default=CallStatus.STARTED,
    )
    request: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    response: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)

    solver_run: Mapped[SolverRun] = relationship(back_populates="model_calls")
    tool_calls: Mapped[list[ToolCall]] = relationship(back_populates="model_call")


class ToolCall(RecordMixin, Base):
    __tablename__ = "tool_calls"
    __table_args__ = (
        UniqueConstraint("solver_run_id", "sequence", name="uq_tool_calls_run_sequence"),
        CheckConstraint("sequence >= 0", name="sequence_nonnegative"),
    )

    solver_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("solver_runs.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    model_call_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("model_calls.id", ondelete="SET NULL"),
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[CallStatus] = mapped_column(
        enum_column(CallStatus, "tool_call_status"),
        nullable=False,
        default=CallStatus.STARTED,
    )
    arguments: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)

    solver_run: Mapped[SolverRun] = relationship(back_populates="tool_calls")
    model_call: Mapped[ModelCall | None] = relationship(back_populates="tool_calls")
    evidence: Mapped[list[Evidence]] = relationship(back_populates="tool_call")


class Evidence(RecordMixin, Base):
    __tablename__ = "evidence"
    __table_args__ = (Index("ix_evidence_challenge_kind", "challenge_id", "kind"),)

    challenge_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("challenges.id", ondelete="RESTRICT"),
        nullable=False,
    )
    solver_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("solver_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    tool_call_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tool_calls.id", ondelete="SET NULL"),
    )
    artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("artifacts.id", ondelete="SET NULL"),
    )
    kind: Mapped[EvidenceKind] = mapped_column(
        enum_column(EvidenceKind, "evidence_kind"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    challenge: Mapped[Challenge] = relationship(back_populates="evidence")
    solver_run: Mapped[SolverRun] = relationship(back_populates="evidence")
    tool_call: Mapped[ToolCall | None] = relationship(back_populates="evidence")
    artifact: Mapped[Artifact | None] = relationship(back_populates="evidence")
    flag_candidates: Mapped[list[FlagCandidate]] = relationship(back_populates="evidence")


class FlagCandidate(RecordMixin, Base):
    __tablename__ = "flag_candidates"
    __table_args__ = (
        UniqueConstraint("challenge_id", "value", name="uq_flag_candidates_challenge_value"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
    )

    challenge_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("challenges.id", ondelete="RESTRICT"),
        nullable=False,
    )
    solver_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("solver_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("evidence.id", ondelete="SET NULL"),
    )
    value: Mapped[str] = mapped_column(String(1024), nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False, default=0)
    status: Mapped[FlagCandidateStatus] = mapped_column(
        enum_column(FlagCandidateStatus, "flag_candidate_status"),
        nullable=False,
        default=FlagCandidateStatus.NEW,
    )
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    challenge: Mapped[Challenge] = relationship(back_populates="flag_candidates")
    solver_run: Mapped[SolverRun] = relationship(back_populates="flag_candidates")
    evidence: Mapped[Evidence | None] = relationship(back_populates="flag_candidates")
    submissions: Mapped[list[Submission]] = relationship(back_populates="flag_candidate")


class Submission(RecordMixin, Base):
    __tablename__ = "submissions"
    __table_args__ = (Index("ix_submissions_challenge_submitted", "challenge_id", "submitted_at"),)

    challenge_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("challenges.id", ondelete="RESTRICT"),
        nullable=False,
    )
    solver_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("solver_runs.id", ondelete="SET NULL"),
    )
    flag_candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("flag_candidates.id", ondelete="SET NULL"),
    )
    value: Mapped[str] = mapped_column(String(1024), nullable=False)
    status: Mapped[SubmissionStatus] = mapped_column(
        enum_column(SubmissionStatus, "submission_status"),
        nullable=False,
        default=SubmissionStatus.PENDING,
    )
    response: Mapped[str | None] = mapped_column(Text)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    challenge: Mapped[Challenge] = relationship(back_populates="submissions")
    solver_run: Mapped[SolverRun | None] = relationship(back_populates="submissions")
    flag_candidate: Mapped[FlagCandidate | None] = relationship(back_populates="submissions")


class EvalRun(RecordMixin, Base):
    __tablename__ = "eval_runs"
    __table_args__ = (Index("ix_eval_runs_competition_status", "competition_id", "status"),)

    competition_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("competitions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[EvalRunStatus] = mapped_column(
        enum_column(EvalRunStatus, "eval_run_status"),
        nullable=False,
        default=EvalRunStatus.QUEUED,
    )
    configuration: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    competition: Mapped[Competition] = relationship(back_populates="eval_runs")
