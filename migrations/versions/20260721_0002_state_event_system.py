"""add state machines and immutable domain events

Revision ID: 20260721_0002
Revises: 20260721_0001
Create Date: 2026-07-21 14:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260721_0002"
down_revision: str | None = "20260721_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_CHALLENGE_STATES = (
    "new",
    "ingested",
    "classified",
    "ready",
    "solving",
    "candidate",
    "verifying",
    "solved",
    "submitted",
    "retry",
    "review",
)
OLD_CHALLENGE_STATES = ("pending", "active", "solved", "failed", "archived")


def _drop_challenge_constraint() -> None:
    with op.batch_alter_table("challenges") as batch_op:
        batch_op.drop_constraint(op.f("ck_challenges_challenge_status"), type_="check")


def _resize_challenge_status(*, existing_length: int, new_length: int) -> None:
    with op.batch_alter_table("challenges") as batch_op:
        batch_op.alter_column(
            "status",
            existing_type=sa.String(length=existing_length),
            type_=sa.String(length=new_length),
            existing_nullable=False,
        )


def _create_immutability_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            """
            CREATE FUNCTION reject_domain_event_mutation() RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'domain_events is append-only';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_domain_events_immutable
            BEFORE UPDATE OR DELETE ON domain_events
            FOR EACH ROW EXECUTE FUNCTION reject_domain_event_mutation()
            """
        )
    elif dialect == "sqlite":
        op.execute(
            """
            CREATE TRIGGER trg_domain_events_no_update
            BEFORE UPDATE ON domain_events
            BEGIN
                SELECT RAISE(ABORT, 'domain_events is append-only');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER trg_domain_events_no_delete
            BEFORE DELETE ON domain_events
            BEGIN
                SELECT RAISE(ABORT, 'domain_events is append-only');
            END
            """
        )


def _drop_immutability_guards() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_domain_events_immutable ON domain_events")
        op.execute("DROP FUNCTION IF EXISTS reject_domain_event_mutation()")
    elif dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_domain_events_no_update")
        op.execute("DROP TRIGGER IF EXISTS trg_domain_events_no_delete")


def upgrade() -> None:
    _drop_challenge_constraint()
    _resize_challenge_status(existing_length=8, new_length=10)
    op.execute(
        """
        UPDATE challenges
        SET status = CASE status
            WHEN 'pending' THEN 'new'
            WHEN 'active' THEN 'solving'
            WHEN 'failed' THEN 'review'
            WHEN 'archived' THEN 'review'
            ELSE status
        END
        """
    )
    with op.batch_alter_table("challenges") as batch_op:
        expression = "status IN ({})".format(
            ", ".join(f"'{value}'" for value in NEW_CHALLENGE_STATES)
        )
        batch_op.create_check_constraint(op.f("ck_challenges_challenge_status"), expression)

    with op.batch_alter_table("checkpoints") as batch_op:
        batch_op.add_column(sa.Column("checksum", sa.String(length=64), nullable=True))

    op.create_table(
        "domain_events",
        sa.Column("sequence", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("aggregate_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("aggregate_version", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=True),
        sa.Column("causation_id", sa.Uuid(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("sequence", name=op.f("pk_domain_events")),
        sa.UniqueConstraint("id", name="uq_domain_events_id"),
        sa.UniqueConstraint("idempotency_key", name="uq_domain_events_idempotency_key"),
    )
    op.create_index(
        "ix_domain_events_aggregate",
        "domain_events",
        ["aggregate_type", "aggregate_id", "sequence"],
        unique=False,
    )
    op.create_index(
        "ix_domain_events_occurred",
        "domain_events",
        ["occurred_at", "sequence"],
        unique=False,
    )
    op.create_table(
        "event_deliveries",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("transport", sa.String(length=64), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["domain_events.id"],
            name=op.f("fk_event_deliveries_event_id_domain_events"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event_deliveries")),
        sa.UniqueConstraint(
            "event_id",
            "transport",
            name="uq_event_deliveries_event_transport",
        ),
    )
    op.create_index(
        "ix_event_deliveries_transport_delivered",
        "event_deliveries",
        ["transport", "delivered_at"],
        unique=False,
    )
    _create_immutability_guards()


def downgrade() -> None:
    _drop_immutability_guards()
    op.drop_index("ix_event_deliveries_transport_delivered", table_name="event_deliveries")
    op.drop_table("event_deliveries")
    op.drop_index("ix_domain_events_occurred", table_name="domain_events")
    op.drop_index("ix_domain_events_aggregate", table_name="domain_events")
    op.drop_table("domain_events")

    with op.batch_alter_table("checkpoints") as batch_op:
        batch_op.drop_column("checksum")

    _drop_challenge_constraint()
    op.execute(
        """
        UPDATE challenges
        SET status = CASE
            WHEN status IN ('new', 'ingested', 'classified', 'ready') THEN 'pending'
            WHEN status IN ('solving', 'candidate', 'verifying') THEN 'active'
            WHEN status IN ('retry', 'review') THEN 'failed'
            WHEN status = 'submitted' THEN 'solved'
            ELSE status
        END
        """
    )
    _resize_challenge_status(existing_length=10, new_length=8)
    with op.batch_alter_table("challenges") as batch_op:
        expression = "status IN ({})".format(
            ", ".join(f"'{value}'" for value in OLD_CHALLENGE_STATES)
        )
        batch_op.create_check_constraint(op.f("ck_challenges_challenge_status"), expression)
