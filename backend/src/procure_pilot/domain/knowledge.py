from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

KNOWLEDGE_EMBEDDING_DIMENSIONS = 256


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise ValueError("Embedding dimensions do not match")
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm)


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
    embedding: list[float] | None = None
    embedding_model: str | None = None
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
    fts_rank: int | None = Field(default=None, gt=0)
    fts_score: float | None = Field(default=None, ge=0)
    vector_rank: int | None = Field(default=None, gt=0)
    rrf_score: float = Field(default=0, ge=0)
    rerank_score: float = Field(default=0, ge=0)
    vector_similarity: float | None = Field(default=None, ge=-1, le=1)
    retrieval_channels: list[str] = Field(default_factory=list)


class KnowledgeSearchResult(BaseModel):
    query: str
    as_of: datetime
    category_id: str
    retrieval_mode: str = "hybrid_rrf"
    embedding_model: str | None = None
    fts_candidate_count: int = Field(default=0, ge=0)
    vector_candidate_count: int = Field(default=0, ge=0)
    degraded_channels: list[str] = Field(default_factory=list)
    citations: list[KnowledgeCitation] = Field(default_factory=list)


class PolicyKnowledgeEvidence(BaseModel):
    policy_version: str
    citations: list[KnowledgeCitation] = Field(min_length=1)


class PolicyKnowledgeReviewRequired(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
