from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JSON_DOCUMENT = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


class ProcurementRunRecord(Base):
    __tablename__ = "procurement_runs"
    __table_args__ = (
        Index("ix_procurement_runs_tenant_user", "tenant_id", "user_id"),
        UniqueConstraint("tenant_id", "run_id", name="uq_procurement_runs_tenant_run"),
        UniqueConstraint("draft_id", name="uq_procurement_runs_draft_id"),
        UniqueConstraint(
            "draft_idempotency_key",
            name="uq_procurement_runs_draft_idempotency_key",
        ),
    )

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    document: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    draft_id: Mapped[str | None] = mapped_column(String(64))
    draft_idempotency_key: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RunMessageRecord(Base):
    __tablename__ = "run_messages"
    __table_args__ = (
        UniqueConstraint("run_id", "position", name="uq_run_messages_position"),
        Index("ix_run_messages_tenant_run", "tenant_id", "run_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("procurement_runs.run_id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RunTraceEventRecord(Base):
    __tablename__ = "run_trace_events"
    __table_args__ = (
        UniqueConstraint("run_id", "position", name="uq_run_trace_events_position"),
        Index("ix_run_trace_events_tenant_run", "tenant_id", "run_id"),
        Index("ix_run_trace_events_name", "name"),
    )

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("procurement_runs.run_id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)


class LongTermMemoryRecord(Base):
    __tablename__ = "long_term_memories"
    __table_args__ = (
        Index(
            "ix_long_term_memories_visibility",
            "tenant_id",
            "scope",
            "owner_user_id",
            "team_id",
            "status",
        ),
        Index("ix_long_term_memories_key", "tenant_id", "memory_key"),
        Index(
            "ix_long_term_memories_context",
            "tenant_id",
            "status",
            "category_id",
            "memory_key",
            "effective_at",
            "expires_at",
        ),
    )

    memory_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    team_id: Mapped[str | None] = mapped_column(String(128))
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    memory_key: Mapped[str] = mapped_column(String(128), nullable=False)
    key_definition_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    category_id: Mapped[str | None] = mapped_column(String(128))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    value_json: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False, default=dict)
    tags: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, nullable=False, default=list)
    source_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    consent_id: Mapped[str] = mapped_column(String(128), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    memory_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON_DOCUMENT, nullable=False, default=dict
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MemoryConsentRecord(Base):
    __tablename__ = "memory_consents"
    __table_args__ = (
        UniqueConstraint("tenant_id", "request_id", name="uq_memory_consents_tenant_request"),
        Index("ix_memory_consents_tenant_user", "tenant_id", "user_id"),
    )

    consent_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    purpose: Mapped[str] = mapped_column(String(128), nullable=False)
    displayed_text: Mapped[str] = mapped_column(Text, nullable=False)
    memory_key: Mapped[str] = mapped_column(String(128), nullable=False)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    command_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)


class MemoryAuditRecord(Base):
    __tablename__ = "memory_audit_events"
    __table_args__ = (Index("ix_memory_audit_tenant_memory", "tenant_id", "memory_id"),)

    audit_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    memory_id: Mapped[str] = mapped_column(
        ForeignKey("long_term_memories.memory_id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MemoryCandidateRecord(Base):
    __tablename__ = "memory_candidates"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "owner_user_id",
            "origin_run_id",
            "proposal_fingerprint",
            name="uq_memory_candidates_run_proposal",
        ),
        Index(
            "ix_memory_candidates_owner_status",
            "tenant_id",
            "owner_user_id",
            "status",
            "expires_at",
        ),
        Index("ix_memory_candidates_origin_run", "tenant_id", "origin_run_id"),
    )

    candidate_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    origin_run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    memory_key: Mapped[str] = mapped_column(String(128), nullable=False)
    key_definition_version: Mapped[int] = mapped_column(Integer, nullable=False)
    category_id: Mapped[str] = mapped_column(String(128), nullable=False)
    proposed_content: Mapped[str] = mapped_column(Text, nullable=False)
    proposed_value_json: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    tags: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, nullable=False, default=list)
    source_message_refs: Mapped[list[str]] = mapped_column(JSON_DOCUMENT, nullable=False)
    source_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    proposal_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    dedup_target_memory_id: Mapped[str | None] = mapped_column(String(64))
    conflict_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_memory_id: Mapped[str | None] = mapped_column(String(64))
    resolution_request_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KnowledgeDocumentRecord(Base):
    __tablename__ = "knowledge_documents"
    __table_args__ = (
        UniqueConstraint("tenant_id", "document_key", name="uq_knowledge_documents_tenant_key"),
        Index(
            "ix_knowledge_documents_tenant_category_status",
            "tenant_id",
            "category_id",
            "status",
        ),
    )

    document_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    document_key: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    category_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_uri: Mapped[str] = mapped_column(String(1024), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class KnowledgeDocumentVersionRecord(Base):
    __tablename__ = "knowledge_document_versions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "document_id",
            "version",
            name="uq_knowledge_versions_tenant_document_version",
        ),
        Index(
            "ix_knowledge_versions_effective",
            "tenant_id",
            "status",
            "effective_from",
            "effective_to",
            "policy_version",
        ),
    )

    version_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_documents.document_id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class KnowledgeChunkRecord(Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        UniqueConstraint("version_id", "ordinal", name="uq_knowledge_chunks_version_ordinal"),
        Index(
            "ix_knowledge_chunks_tenant_document_version",
            "tenant_id",
            "document_id",
            "version_id",
        ),
        Index(
            "ix_knowledge_chunks_search_fts",
            text("to_tsvector('simple', search_text)"),
            postgresql_using="gin",
        ).ddl_if(dialect="postgresql"),
    )

    chunk_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_document_versions.version_id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_documents.document_id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    heading: Mapped[str] = mapped_column(String(512), nullable=False)
    clause_ref: Mapped[str | None] = mapped_column(String(128))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    search_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
