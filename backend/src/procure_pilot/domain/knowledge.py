from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class KnowledgeDocumentStatus(StrEnum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    RETIRED = "RETIRED"


class KnowledgeVersionStatus(StrEnum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"


class KnowledgeDocument(BaseModel):
    document_id: str
    tenant_id: str
    document_key: str
    title: str
    category_id: str
    source_uri: str
    status: KnowledgeDocumentStatus = KnowledgeDocumentStatus.DRAFT
    created_by: str
    created_at: datetime
    updated_at: datetime


class KnowledgeDocumentVersion(BaseModel):
    version_id: str
    document_id: str
    tenant_id: str
    version: int = Field(gt=0)
    policy_version: str
    effective_from: datetime
    effective_to: datetime | None = None
    raw_content: str
    content_hash: str
    status: KnowledgeVersionStatus = KnowledgeVersionStatus.DRAFT
    published_at: datetime | None = None
    created_by: str
    created_at: datetime


class KnowledgeChunk(BaseModel):
    chunk_id: str
    version_id: str
    document_id: str
    tenant_id: str
    ordinal: int = Field(ge=0)
    heading: str
    clause_ref: str | None = None
    content: str
    content_hash: str
    search_text: str
    created_at: datetime


class KnowledgeCitation(BaseModel):
    chunk_id: str
    document_id: str
    document_key: str
    document_title: str
    document_version: int
    policy_version: str
    heading: str
    clause_ref: str | None = None
    excerpt: str
    source_uri: str
    source_ref: str
    score: float = Field(ge=0)


class KnowledgeSearchResult(BaseModel):
    query: str
    as_of: datetime
    category_id: str
    citations: list[KnowledgeCitation] = Field(default_factory=list)


class PolicyKnowledgeEvidence(BaseModel):
    policy_version: str
    citations: list[KnowledgeCitation] = Field(min_length=1)


class PolicyKnowledgeReviewRequired(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
