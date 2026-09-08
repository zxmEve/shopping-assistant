"""Add consent-backed long-term memory and audit log.

Revision ID: 20260904_0002
Revises: 20260904_0001
Create Date: 2026-09-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260904_0002"
down_revision: str | None = "20260904_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "long_term_memories",
        sa.Column("memory_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("owner_user_id", sa.String(length=128), nullable=False),
        sa.Column("team_id", sa.String(length=128), nullable=True),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("memory_key", sa.String(length=128), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source_ref", sa.String(length=512), nullable=False),
        sa.Column("consent_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("lock_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("memory_id"),
    )
    op.create_index(
        "ix_long_term_memories_visibility",
        "long_term_memories",
        ["tenant_id", "scope", "owner_user_id", "team_id", "status"],
    )
    op.create_index(
        "ix_long_term_memories_key",
        "long_term_memories",
        ["tenant_id", "memory_key"],
    )
    op.create_table(
        "memory_audit_events",
        sa.Column("audit_id", sa.String(length=64), nullable=False),
        sa.Column("memory_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("actor_user_id", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("before", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["memory_id"], ["long_term_memories.memory_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("audit_id"),
    )
    op.create_index(
        "ix_memory_audit_tenant_memory",
        "memory_audit_events",
        ["tenant_id", "memory_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_memory_audit_tenant_memory", table_name="memory_audit_events")
    op.drop_table("memory_audit_events")
    op.drop_index("ix_long_term_memories_key", table_name="long_term_memories")
    op.drop_index("ix_long_term_memories_visibility", table_name="long_term_memories")
    op.drop_table("long_term_memories")
