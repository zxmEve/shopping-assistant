"""Add reviewable memory candidates.

Revision ID: 20260908_0004
Revises: 20260908_0003
Create Date: 2026-09-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260908_0004"
down_revision: str | None = "20260908_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memory_candidates",
        sa.Column("candidate_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("owner_user_id", sa.String(length=128), nullable=False),
        sa.Column("origin_run_id", sa.String(length=64), nullable=False),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("memory_key", sa.String(length=128), nullable=False),
        sa.Column("key_definition_version", sa.Integer(), nullable=False),
        sa.Column("category_id", sa.String(length=128), nullable=False),
        sa.Column("proposed_content", sa.Text(), nullable=False),
        sa.Column(
            "proposed_value_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "tags",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "source_message_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("source_ref", sa.String(length=512), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("proposal_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("dedup_target_memory_id", sa.String(length=64), nullable=True),
        sa.Column("conflict_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_memory_id", sa.String(length=64), nullable=True),
        sa.Column("resolution_request_id", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("candidate_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "owner_user_id",
            "origin_run_id",
            "proposal_fingerprint",
            name="uq_memory_candidates_run_proposal",
        ),
    )
    op.create_index(
        "ix_memory_candidates_owner_status",
        "memory_candidates",
        ["tenant_id", "owner_user_id", "status", "expires_at"],
    )
    op.create_index(
        "ix_memory_candidates_origin_run",
        "memory_candidates",
        ["tenant_id", "origin_run_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_memory_candidates_origin_run", table_name="memory_candidates")
    op.drop_index("ix_memory_candidates_owner_status", table_name="memory_candidates")
    op.drop_table("memory_candidates")
