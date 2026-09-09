from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from procure_pilot.domain.models import utc_now


class MemoryScope(StrEnum):
    PROFILE = "PROFILE"
    TEAM = "TEAM"
    EPISODIC = "EPISODIC"
    SEMANTIC = "SEMANTIC"


class MemoryStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    DELETED = "DELETED"


class MemoryCandidateStatus(StrEnum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class MemoryCandidateConflict(StrEnum):
    NEW = "NEW"
    REPLACES_EXISTING = "REPLACES_EXISTING"


class LongTermMemory(BaseModel):
    memory_id: str = Field(default_factory=lambda: f"mem_{uuid4().hex[:16]}")
    tenant_id: str
    owner_user_id: str
    team_id: str | None = None
    scope: MemoryScope
    key: str = Field(min_length=1, max_length=128)
    key_definition_version: int = Field(default=1, gt=0)
    category_id: str | None = Field(default=None, max_length=128)
    content: str = Field(min_length=1, max_length=8000)
    value_json: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list, max_length=20)
    source_ref: str = Field(min_length=1, max_length=512)
    consent_id: str = Field(min_length=1, max_length=128)
    confidence: float = Field(default=1.0, ge=0, le=1)
    effective_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    status: MemoryStatus = MemoryStatus.ACTIVE
    version: int = 0
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    deleted_at: datetime | None = None

    @model_validator(mode="after")
    def validate_scope(self) -> LongTermMemory:
        if self.scope in {MemoryScope.TEAM, MemoryScope.SEMANTIC} and not self.team_id:
            raise ValueError("Team and semantic memories require a team_id")
        if self.expires_at is not None and self.expires_at <= self.effective_at:
            raise ValueError("Memory expires_at must be after effective_at")
        return self


class MemoryConsent(BaseModel):
    consent_id: str = Field(default_factory=lambda: f"consent_{uuid4().hex[:16]}")
    tenant_id: str
    user_id: str
    purpose: str = "LONG_TERM_PERSONALIZATION"
    displayed_text: str = Field(min_length=1, max_length=2000)
    memory_key: str
    scope: MemoryScope
    policy_version: str
    accepted_at: datetime = Field(default_factory=utc_now)
    revoked_at: datetime | None = None
    request_id: str = Field(min_length=1, max_length=128)
    command_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class MemoryAuditEvent(BaseModel):
    audit_id: str
    memory_id: str
    tenant_id: str
    actor_user_id: str
    action: str
    before: dict | None = None
    after: dict | None = None
    created_at: datetime


class MemoryCandidate(BaseModel):
    """A reviewable proposal. It is not long-term memory until explicitly accepted."""

    candidate_id: str = Field(default_factory=lambda: f"mc_{uuid4().hex[:16]}")
    tenant_id: str
    owner_user_id: str
    origin_run_id: str
    scope: MemoryScope = MemoryScope.PROFILE
    key: str = Field(min_length=1, max_length=128)
    key_definition_version: int = Field(gt=0)
    category_id: str = Field(min_length=1, max_length=128)
    proposed_content: str = Field(min_length=1, max_length=2000)
    proposed_value_json: dict[str, Any]
    tags: list[str] = Field(default_factory=list, max_length=20)
    source_message_refs: list[str] = Field(min_length=1, max_length=20)
    source_ref: str = Field(min_length=1, max_length=512)
    confidence: float = Field(ge=0, le=1)
    proposal_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    dedup_target_memory_id: str | None = None
    conflict_type: MemoryCandidateConflict = MemoryCandidateConflict.NEW
    status: MemoryCandidateStatus = MemoryCandidateStatus.PENDING
    expires_at: datetime
    accepted_memory_id: str | None = None
    resolution_request_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    resolved_at: datetime | None = None
