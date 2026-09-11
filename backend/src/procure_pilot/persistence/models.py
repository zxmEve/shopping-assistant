from __future__ import annotations

from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from procure_pilot.domain.knowledge import KNOWLEDGE_EMBEDDING_DIMENSIONS

JSON_DOCUMENT = JSON().with_variant(JSONB(), "postgresql")
KNOWLEDGE_VECTOR = VECTOR(KNOWLEDGE_EMBEDDING_DIMENSIONS).with_variant(JSON(), "sqlite")


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


class AgentExecutionRecord(Base):
    __tablename__ = "agent_executions"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_agent_executions_idempotency_key"),
        Index("ix_agent_executions_tenant_run", "tenant_id", "run_id"),
        Index("ix_agent_executions_status", "status", "updated_at"),
    )

    execution_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("procurement_runs.run_id", ondelete="CASCADE"), nullable=False
    )
    node_name: Mapped[str] = mapped_column(String(128), nullable=False)
    agent_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    state_version: Mapped[int] = mapped_column(Integer, nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ToolCallRecord(Base):
    __tablename__ = "tool_calls"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_tool_calls_idempotency_key"),
        Index("ix_tool_calls_tenant_run", "tenant_id", "run_id"),
        Index("ix_tool_calls_status", "status", "updated_at"),
    )

    tool_call_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("procurement_runs.run_id", ondelete="CASCADE"), nullable=False
    )
    node_name: Mapped[str] = mapped_column(String(128), nullable=False)
    agent_execution_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent_executions.execution_id", ondelete="SET NULL")
    )
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(128))
    risk: Mapped[str] = mapped_column(String(32), nullable=False)
    budget_pool: Mapped[str] = mapped_column(String(32), nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    arguments_json: Mapped[dict[str, Any]] = mapped_column(
        JSON_DOCUMENT, nullable=False, default=dict
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fencing_token: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON_DOCUMENT)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BudgetPolicyRecord(Base):
    __tablename__ = "budget_policies"
    __table_args__ = (
        UniqueConstraint(
            "scope_type",
            "scope_key",
            "provider_kind",
            "provider_id",
            "metric",
            "period_type",
            "version",
            name="uq_budget_policies_scope_version",
        ),
        Index("ix_budget_policies_lookup", "scope_type", "scope_key", "enabled"),
    )

    policy_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope_type: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(256), nullable=False)
    provider_kind: Mapped[str | None] = mapped_column(String(32))
    provider_id: Mapped[str | None] = mapped_column(String(256))
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    period_type: Mapped[str] = mapped_column(String(32), nullable=False)
    period_timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    hard_limit: Mapped[int] = mapped_column(BigInteger, nullable=False)
    soft_limit: Mapped[int | None] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BudgetReservationRecord(Base):
    __tablename__ = "budget_reservations"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_budget_reservations_idempotency"),
        Index("ix_budget_reservations_status_expiry", "status", "expires_at"),
        Index("ix_budget_reservations_tenant_user", "tenant_id", "user_id"),
    )

    reservation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_call_id: Mapped[str | None] = mapped_column(String(64))
    agent_execution_id: Mapped[str | None] = mapped_column(String(64))
    provider_invocation_id: Mapped[str | None] = mapped_column(String(64))
    invocation_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_id: Mapped[str] = mapped_column(String(256), nullable=False)
    model: Mapped[str | None] = mapped_column(String(256))
    pricing_version: Mapped[str] = mapped_column(String(128), nullable=False)
    reserved_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actual_amount: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_request_id: Mapped[str | None] = mapped_column(String(256))
    settlement_basis: Mapped[str | None] = mapped_column(String(64))
    applied_limits_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON_DOCUMENT, nullable=False
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSON_DOCUMENT, nullable=False, default=dict
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BudgetLedgerEntryRecord(Base):
    __tablename__ = "budget_ledger_entries"
    __table_args__ = (
        UniqueConstraint(
            "event_id",
            "scope_type",
            "scope_key",
            "period_start",
            "metric",
            name="uq_budget_ledger_event_scope",
        ),
        Index("ix_budget_ledger_scope_period", "scope_type", "scope_key", "period_start"),
        Index("ix_budget_ledger_reservation", "reservation_id", "occurred_at"),
    )

    entry_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    reservation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_key: Mapped[str] = mapped_column(String(256), nullable=False)
    metric: Mapped[str] = mapped_column(String(64), nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    delta_reserved: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    delta_committed: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    policy_id: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSON_DOCUMENT, nullable=False, default=dict
    )


class BudgetOutboxRecord(Base):
    __tablename__ = "budget_outbox"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_budget_outbox_event"),
        Index("ix_budget_outbox_pending", "processed_at", "available_at"),
    )

    outbox_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    reservation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON_DOCUMENT, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


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
    source_file_name: Mapped[str | None] = mapped_column(String(512))
    source_media_type: Mapped[str | None] = mapped_column(String(128))
    source_file_hash: Mapped[str | None] = mapped_column(String(64))
    source_file_size: Mapped[int | None] = mapped_column(Integer)
    parser_version: Mapped[str | None] = mapped_column(String(64))
    extraction_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSON_DOCUMENT, nullable=False, default=dict
    )
    source_blob: Mapped[bytes | None] = mapped_column(LargeBinary)
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
        Index(
            "ix_knowledge_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
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
    embedding: Mapped[list[float] | None] = mapped_column(KNOWLEDGE_VECTOR)
    embedding_model: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
