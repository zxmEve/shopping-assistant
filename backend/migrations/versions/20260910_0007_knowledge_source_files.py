"""Preserve policy source files and extraction metadata.

Revision ID: 20260910_0007
Revises: 20260909_0006
Create Date: 2026-09-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260910_0007"
down_revision: str | None = "20260909_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "knowledge_document_versions",
        sa.Column("source_file_name", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "knowledge_document_versions",
        sa.Column("source_media_type", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "knowledge_document_versions",
        sa.Column("source_file_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "knowledge_document_versions",
        sa.Column("source_file_size", sa.Integer(), nullable=True),
    )
    op.add_column(
        "knowledge_document_versions",
        sa.Column("parser_version", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "knowledge_document_versions",
        sa.Column(
            "extraction_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "knowledge_document_versions",
        sa.Column("source_blob", sa.LargeBinary(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("knowledge_document_versions", "source_blob")
    op.drop_column("knowledge_document_versions", "extraction_metadata")
    op.drop_column("knowledge_document_versions", "parser_version")
    op.drop_column("knowledge_document_versions", "source_file_size")
    op.drop_column("knowledge_document_versions", "source_file_hash")
    op.drop_column("knowledge_document_versions", "source_media_type")
    op.drop_column("knowledge_document_versions", "source_file_name")
