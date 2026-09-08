"""Add structured memory fields and durable consent records.

Revision ID: 20260908_0003
Revises: 20260904_0002
Create Date: 2026-09-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260908_0003"
down_revision: str | None = "20260904_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "long_term_memories",
        sa.Column("key_definition_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "long_term_memories",
        sa.Column("category_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "long_term_memories",
        sa.Column(
            "value_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "long_term_memories",
        sa.Column(
            "tags",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "long_term_memories",
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1"),
    )
    op.add_column(
        "long_term_memories",
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "long_term_memories",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "long_term_memories",
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.execute("UPDATE long_term_memories SET effective_at = created_at")
    op.alter_column("long_term_memories", "effective_at", nullable=False)
    op.create_index(
        "ix_long_term_memories_context",
        "long_term_memories",
        [
            "tenant_id",
            "status",
            "category_id",
            "memory_key",
            "effective_at",
            "expires_at",
        ],
    )
    op.create_table(
        "memory_consents",
        sa.Column("consent_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("purpose", sa.String(length=128), nullable=False),
        sa.Column("displayed_text", sa.Text(), nullable=False),
        sa.Column("memory_key", sa.String(length=128), nullable=False),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("command_fingerprint", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("consent_id"),
        sa.UniqueConstraint(
            "tenant_id", "request_id", name="uq_memory_consents_tenant_request"
        ),
    )
    op.create_index(
        "ix_memory_consents_tenant_user",
        "memory_consents",
        ["tenant_id", "user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_memory_consents_tenant_user", table_name="memory_consents")
    op.drop_table("memory_consents")
    op.drop_index("ix_long_term_memories_context", table_name="long_term_memories")
    op.drop_column("long_term_memories", "metadata")
    op.drop_column("long_term_memories", "expires_at")
    op.drop_column("long_term_memories", "effective_at")
    op.drop_column("long_term_memories", "confidence")
    op.drop_column("long_term_memories", "tags")
    op.drop_column("long_term_memories", "value_json")
    op.drop_column("long_term_memories", "category_id")
    op.drop_column("long_term_memories", "key_definition_version")
