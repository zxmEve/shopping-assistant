"""Create tenant-scoped procurement run store.

Revision ID: 20260904_0001
Revises:
Create Date: 2026-09-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260904_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "procurement_runs",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("lock_version", sa.Integer(), nullable=False),
        sa.Column("document", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("draft_id", sa.String(length=64), nullable=True),
        sa.Column("draft_idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("run_id"),
        sa.UniqueConstraint("draft_id", name="uq_procurement_runs_draft_id"),
        sa.UniqueConstraint(
            "draft_idempotency_key",
            name="uq_procurement_runs_draft_idempotency_key",
        ),
        sa.UniqueConstraint("tenant_id", "run_id", name="uq_procurement_runs_tenant_run"),
    )
    op.create_index(
        "ix_procurement_runs_tenant_user",
        "procurement_runs",
        ["tenant_id", "user_id"],
    )
    op.create_table(
        "run_messages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["procurement_runs.run_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "position", name="uq_run_messages_position"),
    )
    op.create_index("ix_run_messages_tenant_run", "run_messages", ["tenant_id", "run_id"])
    op.create_table(
        "run_trace_events",
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["procurement_runs.run_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint("run_id", "position", name="uq_run_trace_events_position"),
    )
    op.create_index("ix_run_trace_events_name", "run_trace_events", ["name"])
    op.create_index("ix_run_trace_events_tenant_run", "run_trace_events", ["tenant_id", "run_id"])


def downgrade() -> None:
    op.drop_index("ix_run_trace_events_tenant_run", table_name="run_trace_events")
    op.drop_index("ix_run_trace_events_name", table_name="run_trace_events")
    op.drop_table("run_trace_events")
    op.drop_index("ix_run_messages_tenant_run", table_name="run_messages")
    op.drop_table("run_messages")
    op.drop_index("ix_procurement_runs_tenant_user", table_name="procurement_runs")
    op.drop_table("procurement_runs")
