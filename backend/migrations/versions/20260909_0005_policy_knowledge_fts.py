"""Add tenant-scoped policy knowledge documents and FTS chunks.

Revision ID: 20260909_0005
Revises: 20260908_0004
Create Date: 2026-09-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260909_0005"
down_revision: str | None = "20260908_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "knowledge_documents",
        sa.Column("document_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("document_key", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("category_id", sa.String(length=128), nullable=False),
        sa.Column("source_uri", sa.String(length=1024), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("document_id"),
        sa.UniqueConstraint(
            "tenant_id", "document_key", name="uq_knowledge_documents_tenant_key"
        ),
    )
    op.create_index(
        "ix_knowledge_documents_tenant_category_status",
        "knowledge_documents",
        ["tenant_id", "category_id", "status"],
    )
    op.create_table(
        "knowledge_document_versions",
        sa.Column("version_id", sa.String(length=64), nullable=False),
        sa.Column("document_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"], ["knowledge_documents.document_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("version_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "document_id",
            "version",
            name="uq_knowledge_versions_tenant_document_version",
        ),
    )
    op.create_index(
        "ix_knowledge_versions_effective",
        "knowledge_document_versions",
        ["tenant_id", "status", "effective_from", "effective_to", "policy_version"],
    )
    op.create_table(
        "knowledge_chunks",
        sa.Column("chunk_id", sa.String(length=64), nullable=False),
        sa.Column("version_id", sa.String(length=64), nullable=False),
        sa.Column("document_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("heading", sa.String(length=512), nullable=False),
        sa.Column("clause_ref", sa.String(length=128), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"], ["knowledge_documents.document_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["version_id"], ["knowledge_document_versions.version_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("chunk_id"),
        sa.UniqueConstraint("version_id", "ordinal", name="uq_knowledge_chunks_version_ordinal"),
    )
    op.create_index(
        "ix_knowledge_chunks_tenant_document_version",
        "knowledge_chunks",
        ["tenant_id", "document_id", "version_id"],
    )
    # search_text contains normalized Latin tokens and CJK unigrams/bigrams. The
    # simple dictionary keeps those governed tokens intact and avoids language-specific stemming.
    op.execute(
        "CREATE INDEX ix_knowledge_chunks_search_fts ON knowledge_chunks "
        "USING gin (to_tsvector('simple', search_text))"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_knowledge_chunks_search_fts")
    op.drop_index("ix_knowledge_chunks_tenant_document_version", table_name="knowledge_chunks")
    op.drop_table("knowledge_chunks")
    op.drop_index("ix_knowledge_versions_effective", table_name="knowledge_document_versions")
    op.drop_table("knowledge_document_versions")
    op.drop_index(
        "ix_knowledge_documents_tenant_category_status", table_name="knowledge_documents"
    )
    op.drop_table("knowledge_documents")
