"""add challenge ingestion metadata

Revision ID: 20260721_0003
Revises: 20260721_0002
Create Date: 2026-07-21 14:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260721_0003"
down_revision: str | None = "20260721_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("competitions") as batch_op:
        batch_op.add_column(
            sa.Column(
                "flag_format",
                sa.String(length=128),
                server_default="flag{...}",
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "flag_regex",
                sa.String(length=512),
                server_default=r"^flag\{[^}\r\n]+\}$",
                nullable=False,
            )
        )

    with op.batch_alter_table("challenges") as batch_op:
        batch_op.add_column(sa.Column("service_protocol", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("service_host", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("service_port", sa.Integer(), nullable=True))
        batch_op.create_check_constraint(
            op.f("ck_challenges_service_port_range"),
            "service_port IS NULL OR (service_port >= 1 AND service_port <= 65535)",
        )

    with op.batch_alter_table("artifacts") as batch_op:
        batch_op.add_column(sa.Column("source_url", sa.String(length=2048), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("artifacts") as batch_op:
        batch_op.drop_column("source_url")

    with op.batch_alter_table("challenges") as batch_op:
        batch_op.drop_constraint(op.f("ck_challenges_service_port_range"), type_="check")
        batch_op.drop_column("service_port")
        batch_op.drop_column("service_host")
        batch_op.drop_column("service_protocol")

    with op.batch_alter_table("competitions") as batch_op:
        batch_op.drop_column("flag_regex")
        batch_op.drop_column("flag_format")
