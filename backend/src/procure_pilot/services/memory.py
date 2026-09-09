from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

from procure_pilot.domain.memory import LongTermMemory, MemoryConsent, MemoryScope
from procure_pilot.domain.models import utc_now
from procure_pilot.persistence.memory_repository import SqlAlchemyMemoryRepository
from procure_pilot.services.memory_registry import MemoryRegistry

TEAM_MEMORY_WRITERS = frozenset({"TEAM_ADMIN", "PROCUREMENT_SPECIALIST", "MEMORY_STEWARD"})


class MemoryService:
    """Validates vocabulary, authorization and consent before memory persistence."""

    consent_policy_version = "memory-consent-v1"

    def __init__(
        self,
        repository: SqlAlchemyMemoryRepository,
        registry: MemoryRegistry,
        *,
        clock=utc_now,
    ) -> None:
        self.repository = repository
        self.registry = registry
        self.clock = clock

    def create(
        self,
        *,
        tenant_id: str,
        user_id: str,
        team_id: str,
        roles: tuple[str, ...],
        scope: MemoryScope,
        key: str,
        category_id: str,
        content: str,
        value_json: dict[str, Any],
        tags: list[str],
        effective_at: datetime | None,
        expires_at: datetime | None,
        request_id: str,
        displayed_text: str,
        source_ref: str | None = None,
        confidence: float = 1.0,
        metadata: dict[str, Any] | None = None,
    ) -> LongTermMemory:
        self._authorize_write(scope, roles)
        definition, canonical_tags = self.registry.validate_memory(
            key=key,
            definition_version=None,
            category_id=category_id,
            scope=scope,
            value_json=value_json,
            tags=tags,
        )
        now = self.clock()
        effective = effective_at or now
        expiry = expires_at
        if expiry is None and definition.default_ttl_days is not None:
            expiry = effective + timedelta(days=definition.default_ttl_days)
        consent = MemoryConsent(
            tenant_id=tenant_id,
            user_id=user_id,
            displayed_text=displayed_text,
            memory_key=definition.key,
            scope=scope,
            policy_version=self.consent_policy_version,
            request_id=request_id,
            accepted_at=now,
            command_fingerprint=self._command_fingerprint(
                {
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "team_id": team_id if scope == MemoryScope.TEAM else None,
                    "scope": scope,
                    "key": definition.key,
                    "key_definition_version": definition.definition_version,
                    "category_id": category_id,
                    "content": content,
                    "value_json": value_json,
                    "tags": canonical_tags,
                    "effective_at": effective_at,
                    "expires_at": expires_at,
                    "displayed_text": displayed_text,
                    "source_ref": source_ref,
                    "confidence": confidence,
                    "metadata": metadata or {},
                }
            ),
        )
        memory = LongTermMemory(
            tenant_id=tenant_id,
            owner_user_id=user_id,
            team_id=team_id if scope == MemoryScope.TEAM else None,
            scope=scope,
            key=definition.key,
            key_definition_version=definition.definition_version,
            category_id=category_id,
            content=content,
            value_json=value_json,
            tags=canonical_tags,
            source_ref=source_ref or f"user://{user_id}/explicit-memory/{request_id}",
            consent_id=consent.consent_id,
            confidence=confidence,
            effective_at=effective,
            expires_at=expiry,
            metadata={
                "memory_registry_version": self.registry.version,
                **(metadata or {}),
            },
            created_at=now,
            updated_at=now,
        )
        return self.repository.create_with_consent(
            memory,
            consent,
            actor_user_id=user_id,
            merge_strategy=definition.merge_strategy.value,
        )

    def update(
        self,
        memory_id: str,
        *,
        tenant_id: str,
        user_id: str,
        team_id: str,
        roles: tuple[str, ...],
        content: str,
        value_json: dict[str, Any],
        tags: list[str],
        effective_at: datetime | None,
        expires_at: datetime | None,
        expected_version: int,
    ) -> LongTermMemory:
        current = self.repository.get(
            memory_id,
            tenant_id=tenant_id,
            user_id=user_id,
            team_id=team_id,
        )
        self._authorize_write(current.scope, roles)
        _, canonical_tags = self.registry.validate_memory(
            key=current.key,
            definition_version=current.key_definition_version,
            category_id=current.category_id,
            scope=current.scope,
            value_json=value_json,
            tags=tags,
        )
        effective = effective_at or current.effective_at
        candidate = current.model_copy(
            update={
                "content": content,
                "value_json": value_json,
                "tags": canonical_tags,
                "effective_at": effective,
                "expires_at": expires_at,
            }
        )
        LongTermMemory.model_validate(candidate.model_dump())
        return self.repository.update_details(
            memory_id,
            tenant_id=tenant_id,
            actor_user_id=user_id,
            content=content,
            value_json=value_json,
            tags=canonical_tags,
            confidence=current.confidence,
            effective_at=effective,
            expires_at=expires_at,
            metadata=current.metadata,
            expected_version=expected_version,
        )

    def delete(
        self,
        memory_id: str,
        *,
        tenant_id: str,
        user_id: str,
        team_id: str,
        roles: tuple[str, ...],
        expected_version: int,
    ) -> None:
        current = self.repository.get(
            memory_id,
            tenant_id=tenant_id,
            user_id=user_id,
            team_id=team_id,
        )
        self._authorize_write(current.scope, roles)
        self.repository.delete(
            memory_id,
            tenant_id=tenant_id,
            actor_user_id=user_id,
            expected_version=expected_version,
        )

    @staticmethod
    def _authorize_write(scope: MemoryScope, roles: tuple[str, ...]) -> None:
        if scope == MemoryScope.SEMANTIC:
            raise PermissionError("Knowledge records must use the governed RAG ingestion path")
        if scope == MemoryScope.TEAM and not TEAM_MEMORY_WRITERS.intersection(roles):
            raise PermissionError("TEAM memory requires an authorized team memory role")

    @staticmethod
    def _command_fingerprint(payload: dict[str, Any]) -> str:
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
