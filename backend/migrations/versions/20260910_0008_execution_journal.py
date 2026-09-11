"""Add durable Agent execution and tool call journals.

Revision ID: 20260910_0008
Revises: 20260910_0007
Create Date: 2026-09-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260910_0008"
down_revision: str | None = "20260910_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_executions",
        sa.Column("execution_id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column(
            "run_id",
            sa.String(length=64),
            sa.ForeignKey("procurement_runs.run_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("node_name", sa.String(length=128), nullable=False),
        sa.Column("agent_kind", sa.String(length=64), nullable=False),
        sa.Column("task_id", sa.String(length=128), nullable=False),
        sa.Column("state_version", sa.Integer(), nullable=False),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("result_json", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("error", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_agent_executions_idempotency_key"),
    )
    op.create_index(
        "ix_agent_executions_tenant_run", "agent_executions", ["tenant_id", "run_id"]
    )
    op.create_index(
        "ix_agent_executions_status", "agent_executions", ["status", "updated_at"]
    )
    op.create_table(
        "tool_calls",
        sa.Column("tool_call_id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column(
            "run_id",
            sa.String(length=64),
            sa.ForeignKey("procurement_runs.run_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("node_name", sa.String(length=128), nullable=False),
        sa.Column(
            "agent_execution_id",
            sa.String(length=64),
            sa.ForeignKey("agent_executions.execution_id", ondelete="SET NULL"),
        ),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("provider", sa.String(length=128)),
        sa.Column("risk", sa.String(length=32), nullable=False),
        sa.Column("budget_pool", sa.String(length=32), nullable=False),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column(
            "arguments_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("result_json", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("error", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_tool_calls_idempotency_key"),
    )
    op.create_index("ix_tool_calls_tenant_run", "tool_calls", ["tenant_id", "run_id"])
    op.create_index("ix_tool_calls_status", "tool_calls", ["status", "updated_at"])


def downgrade() -> None:
    op.drop_index("ix_tool_calls_status", table_name="tool_calls")
    op.drop_index("ix_tool_calls_tenant_run", table_name="tool_calls")
    op.drop_table("tool_calls")
    op.drop_index("ix_agent_executions_status", table_name="agent_executions")
    op.drop_index("ix_agent_executions_tenant_run", table_name="agent_executions")
    op.drop_table("agent_executions")
