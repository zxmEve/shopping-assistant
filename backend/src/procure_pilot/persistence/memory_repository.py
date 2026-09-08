from __future__ import annotations

from uuid import uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from procure_pilot.domain.memory import (
    LongTermMemory,
    MemoryAuditEvent,
    MemoryConsent,
    MemoryScope,
    MemoryStatus,
)
from procure_pilot.domain.models import utc_now
from procure_pilot.harness.runtime import PersistenceConflictError
from procure_pilot.persistence.models import (
    LongTermMemoryRecord,
    MemoryAuditRecord,
    MemoryConsentRecord,
)


class SqlAlchemyMemoryRepository:
    """Consent-backed, tenant-scoped long-term memory store."""

    def __init__(self, database_url: str, *, engine: Engine | None = None) -> None:
        from sqlalchemy import create_engine

        self.engine = engine or create_engine(database_url, pool_pre_ping=True)
        self.session_factory = sessionmaker(self.engine, expire_on_commit=False)

    def create(self, memory: LongTermMemory, *, actor_user_id: str) -> LongTermMemory:
        snapshot = self._audit_snapshot(memory)
        with self.session_factory.begin() as session:
            session.add(self._to_record(memory))
            self._audit(session, memory, actor_user_id, "CREATED", None, snapshot)
        return memory

    def create_with_consent(
        self,
        memory: LongTermMemory,
        consent: MemoryConsent,
        *,
        actor_user_id: str,
        merge_strategy: str,
    ) -> LongTermMemory:
        """Atomically persist consent and a new memory version; request_id is idempotent."""
        if merge_strategy not in {"REPLACE", "KEEP_LATEST", "MANUAL"}:
            raise ValueError(f"Unsupported memory merge strategy: {merge_strategy}")
        with self.session_factory.begin() as session:
            previous_consent = session.execute(
                select(MemoryConsentRecord).where(
                    MemoryConsentRecord.tenant_id == consent.tenant_id,
                    MemoryConsentRecord.request_id == consent.request_id,
                )
            ).scalar_one_or_none()
            if previous_consent is not None:
                if (
                    previous_consent.user_id != consent.user_id
                    or previous_consent.memory_key != consent.memory_key
                    or previous_consent.scope != consent.scope.value
                ):
                    raise PermissionError("Consent request_id is already bound to another command")
                if previous_consent.command_fingerprint != consent.command_fingerprint:
                    raise PersistenceConflictError(
                        "Consent request_id was reused with a different payload"
                    )
                existing = session.execute(
                    select(LongTermMemoryRecord).where(
                        LongTermMemoryRecord.tenant_id == consent.tenant_id,
                        LongTermMemoryRecord.consent_id == previous_consent.consent_id,
                    )
                ).scalar_one_or_none()
                if existing is None:
                    raise RuntimeError("Consent exists without its memory record")
                return self._to_domain(existing)

            existing = session.execute(
                select(LongTermMemoryRecord).where(
                    LongTermMemoryRecord.tenant_id == memory.tenant_id,
                    LongTermMemoryRecord.owner_user_id == memory.owner_user_id,
                    LongTermMemoryRecord.team_id == memory.team_id,
                    LongTermMemoryRecord.scope == memory.scope.value,
                    LongTermMemoryRecord.memory_key == memory.key,
                    LongTermMemoryRecord.category_id == memory.category_id,
                    LongTermMemoryRecord.status == MemoryStatus.ACTIVE.value,
                )
            ).scalar_one_or_none()
            if existing is not None and merge_strategy == "MANUAL":
                raise PersistenceConflictError("Memory requires manual conflict resolution")

            session.add(self._consent_to_record(consent))
            if existing is not None:
                previous = self._to_domain(existing)
                existing.status = MemoryStatus.SUPERSEDED.value
                existing.lock_version += 1
                existing.updated_at = memory.created_at
                self._audit(
                    session,
                    previous.model_copy(
                        update={
                            "status": MemoryStatus.SUPERSEDED,
                            "version": existing.lock_version,
                            "updated_at": memory.created_at,
                        }
                    ),
                    actor_user_id,
                    "SUPERSEDED",
                    self._audit_snapshot(previous),
                    {
                        **self._audit_snapshot(previous),
                        "status": MemoryStatus.SUPERSEDED.value,
                        "version": existing.lock_version,
                        "superseded_by": memory.memory_id,
                    },
                )
                memory.metadata = {**memory.metadata, "supersedes_memory_id": previous.memory_id}

            session.add(self._to_record(memory))
            self._audit(
                session,
                memory,
                actor_user_id,
                "CREATED",
                None,
                self._audit_snapshot(memory),
            )
        return memory

    def get(
        self,
        memory_id: str,
        *,
        tenant_id: str,
        user_id: str,
        team_id: str,
    ) -> LongTermMemory:
        with self.session_factory() as session:
            record = session.execute(
                select(LongTermMemoryRecord).where(
                    LongTermMemoryRecord.memory_id == memory_id,
                    self._visible(tenant_id=tenant_id, user_id=user_id, team_id=team_id),
                    LongTermMemoryRecord.status == MemoryStatus.ACTIVE.value,
                    LongTermMemoryRecord.effective_at <= utc_now(),
                    or_(
                        LongTermMemoryRecord.expires_at.is_(None),
                        LongTermMemoryRecord.expires_at > utc_now(),
                    ),
                )
            ).scalar_one_or_none()
        if record is None:
            raise PermissionError("Memory is not accessible in the current tenant context")
        return self._to_domain(record)

    def list(
        self,
        *,
        tenant_id: str,
        user_id: str,
        team_id: str,
        scope: MemoryScope | None = None,
    ) -> list[LongTermMemory]:
        statement = select(LongTermMemoryRecord).where(
            self._visible(tenant_id=tenant_id, user_id=user_id, team_id=team_id),
            LongTermMemoryRecord.status == MemoryStatus.ACTIVE.value,
            LongTermMemoryRecord.effective_at <= utc_now(),
            or_(
                LongTermMemoryRecord.expires_at.is_(None),
                LongTermMemoryRecord.expires_at > utc_now(),
            ),
        )
        if scope is not None:
            statement = statement.where(LongTermMemoryRecord.scope == scope.value)
        statement = statement.order_by(LongTermMemoryRecord.updated_at.desc())
        with self.session_factory() as session:
            records = session.execute(statement).scalars().all()
        return [self._to_domain(record) for record in records]

    def update_content(
        self,
        memory_id: str,
        *,
        tenant_id: str,
        actor_user_id: str,
        content: str,
        expected_version: int,
    ) -> LongTermMemory:
        with self.session_factory.begin() as session:
            record = session.execute(
                select(LongTermMemoryRecord).where(
                    LongTermMemoryRecord.memory_id == memory_id,
                    LongTermMemoryRecord.tenant_id == tenant_id,
                    LongTermMemoryRecord.owner_user_id == actor_user_id,
                    LongTermMemoryRecord.status == MemoryStatus.ACTIVE.value,
                )
            ).scalar_one_or_none()
            if record is None:
                raise PermissionError("Memory is not accessible in the current tenant context")
            before = self._audit_snapshot(self._to_domain(record))
            now = utc_now()
            result = session.execute(
                update(LongTermMemoryRecord)
                .where(
                    LongTermMemoryRecord.memory_id == memory_id,
                    LongTermMemoryRecord.tenant_id == tenant_id,
                    LongTermMemoryRecord.owner_user_id == actor_user_id,
                    LongTermMemoryRecord.lock_version == expected_version,
                    LongTermMemoryRecord.status == MemoryStatus.ACTIVE.value,
                )
                .values(
                    content=content,
                    lock_version=expected_version + 1,
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                raise PersistenceConflictError("Memory was changed by another writer")
            record.content = content
            record.lock_version = expected_version + 1
            record.updated_at = now
            memory = self._to_domain(record)
            self._audit(
                session,
                memory,
                actor_user_id,
                "UPDATED",
                before,
                self._audit_snapshot(memory),
            )
        return memory

    def update_details(
        self,
        memory_id: str,
        *,
        tenant_id: str,
        actor_user_id: str,
        content: str,
        value_json: dict,
        tags: list[str],
        confidence: float,
        effective_at,
        expires_at,
        metadata: dict,
        expected_version: int,
    ) -> LongTermMemory:
        with self.session_factory.begin() as session:
            record = session.execute(
                select(LongTermMemoryRecord).where(
                    LongTermMemoryRecord.memory_id == memory_id,
                    LongTermMemoryRecord.tenant_id == tenant_id,
                    LongTermMemoryRecord.owner_user_id == actor_user_id,
                    LongTermMemoryRecord.status == MemoryStatus.ACTIVE.value,
                )
            ).scalar_one_or_none()
            if record is None:
                raise PermissionError("Memory is not accessible in the current tenant context")
            before = self._audit_snapshot(self._to_domain(record))
            now = utc_now()
            result = session.execute(
                update(LongTermMemoryRecord)
                .where(
                    LongTermMemoryRecord.memory_id == memory_id,
                    LongTermMemoryRecord.lock_version == expected_version,
                    LongTermMemoryRecord.status == MemoryStatus.ACTIVE.value,
                )
                .values(
                    content=content,
                    value_json=value_json,
                    tags=tags,
                    confidence=confidence,
                    effective_at=effective_at,
                    expires_at=expires_at,
                    memory_metadata=metadata,
                    lock_version=expected_version + 1,
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                raise PersistenceConflictError("Memory was changed by another writer")
            record.content = content
            record.value_json = value_json
            record.tags = tags
            record.confidence = confidence
            record.effective_at = effective_at
            record.expires_at = expires_at
            record.memory_metadata = metadata
            record.lock_version = expected_version + 1
            record.updated_at = now
            memory = self._to_domain(record)
            self._audit(
                session,
                memory,
                actor_user_id,
                "UPDATED",
                before,
                self._audit_snapshot(memory),
            )
        return memory

    def delete(
        self,
        memory_id: str,
        *,
        tenant_id: str,
        actor_user_id: str,
        expected_version: int,
    ) -> None:
        with self.session_factory.begin() as session:
            record = session.execute(
                select(LongTermMemoryRecord).where(
                    LongTermMemoryRecord.memory_id == memory_id,
                    LongTermMemoryRecord.tenant_id == tenant_id,
                    LongTermMemoryRecord.owner_user_id == actor_user_id,
                    LongTermMemoryRecord.status == MemoryStatus.ACTIVE.value,
                )
            ).scalar_one_or_none()
            if record is None:
                raise PermissionError("Memory is not accessible in the current tenant context")
            before = self._audit_snapshot(self._to_domain(record))
            now = utc_now()
            result = session.execute(
                update(LongTermMemoryRecord)
                .where(
                    LongTermMemoryRecord.memory_id == memory_id,
                    LongTermMemoryRecord.lock_version == expected_version,
                )
                .values(
                    status=MemoryStatus.DELETED.value,
                    content="__deleted__",
                    value_json={},
                    tags=[],
                    memory_metadata={},
                    lock_version=expected_version + 1,
                    updated_at=now,
                    deleted_at=now,
                )
            )
            if result.rowcount != 1:
                raise PersistenceConflictError("Memory was changed by another writer")
            record.status = MemoryStatus.DELETED.value
            record.content = "__deleted__"
            record.value_json = {}
            record.tags = []
            record.memory_metadata = {}
            record.lock_version = expected_version + 1
            record.updated_at = now
            record.deleted_at = now
            self._audit(session, self._to_domain(record), actor_user_id, "DELETED", before, None)

    def audit(
        self,
        memory_id: str,
        *,
        tenant_id: str,
        owner_user_id: str,
    ) -> list[MemoryAuditEvent]:
        with self.session_factory() as session:
            owner = session.execute(
                select(LongTermMemoryRecord.memory_id).where(
                    LongTermMemoryRecord.memory_id == memory_id,
                    LongTermMemoryRecord.tenant_id == tenant_id,
                    LongTermMemoryRecord.owner_user_id == owner_user_id,
                )
            ).scalar_one_or_none()
            if owner is None:
                raise PermissionError("Memory is not accessible in the current tenant context")
            records = session.execute(
                select(MemoryAuditRecord)
                .where(
                    MemoryAuditRecord.memory_id == memory_id,
                    MemoryAuditRecord.tenant_id == tenant_id,
                )
                .order_by(MemoryAuditRecord.created_at)
            ).scalars()
            return [
                MemoryAuditEvent(
                    audit_id=row.audit_id,
                    memory_id=row.memory_id,
                    tenant_id=row.tenant_id,
                    actor_user_id=row.actor_user_id,
                    action=row.action,
                    before=row.before,
                    after=row.after,
                    created_at=row.created_at,
                )
                for row in records
            ]

    def consent_for_memory(
        self,
        memory_id: str,
        *,
        tenant_id: str,
        owner_user_id: str,
    ) -> MemoryConsent:
        with self.session_factory() as session:
            record = session.execute(
                select(MemoryConsentRecord)
                .join(
                    LongTermMemoryRecord,
                    LongTermMemoryRecord.consent_id == MemoryConsentRecord.consent_id,
                )
                .where(
                    LongTermMemoryRecord.memory_id == memory_id,
                    LongTermMemoryRecord.tenant_id == tenant_id,
                    LongTermMemoryRecord.owner_user_id == owner_user_id,
                    MemoryConsentRecord.tenant_id == tenant_id,
                )
            ).scalar_one_or_none()
        if record is None:
            raise PermissionError("Consent is not accessible in the current tenant context")
        return self._consent_to_domain(record)

    @staticmethod
    def _visible(*, tenant_id: str, user_id: str, team_id: str):
        return and_(
            LongTermMemoryRecord.tenant_id == tenant_id,
            or_(
                and_(
                    LongTermMemoryRecord.scope.in_(
                        [MemoryScope.PROFILE.value, MemoryScope.EPISODIC.value]
                    ),
                    LongTermMemoryRecord.owner_user_id == user_id,
                ),
                and_(
                    LongTermMemoryRecord.scope.in_(
                        [MemoryScope.TEAM.value, MemoryScope.SEMANTIC.value]
                    ),
                    LongTermMemoryRecord.team_id == team_id,
                ),
            ),
        )

    @staticmethod
    def _to_domain(record: LongTermMemoryRecord) -> LongTermMemory:
        return LongTermMemory(
            memory_id=record.memory_id,
            tenant_id=record.tenant_id,
            owner_user_id=record.owner_user_id,
            team_id=record.team_id,
            scope=MemoryScope(record.scope),
            key=record.memory_key,
            key_definition_version=record.key_definition_version,
            category_id=record.category_id,
            content=record.content,
            value_json=record.value_json,
            tags=record.tags,
            source_ref=record.source_ref,
            consent_id=record.consent_id,
            confidence=record.confidence,
            effective_at=record.effective_at,
            expires_at=record.expires_at,
            metadata=record.memory_metadata,
            status=MemoryStatus(record.status),
            version=record.lock_version,
            created_at=record.created_at,
            updated_at=record.updated_at,
            deleted_at=record.deleted_at,
        )

    @staticmethod
    def _audit(
        session: Session,
        memory: LongTermMemory,
        actor_user_id: str,
        action: str,
        before: dict | None,
        after: dict | None,
    ) -> None:
        session.add(
            MemoryAuditRecord(
                audit_id=f"ma_{uuid4().hex[:16]}",
                memory_id=memory.memory_id,
                tenant_id=memory.tenant_id,
                actor_user_id=actor_user_id,
                action=action,
                before=before,
                after=after,
                created_at=utc_now(),
            )
        )

    @staticmethod
    def _audit_snapshot(memory: LongTermMemory) -> dict:
        """Audit lifecycle metadata without duplicating personal memory content."""
        return {
            "memory_id": memory.memory_id,
            "scope": memory.scope.value,
            "key": memory.key,
            "key_definition_version": memory.key_definition_version,
            "category_id": memory.category_id,
            "tags": memory.tags,
            "source_ref": memory.source_ref,
            "consent_id": memory.consent_id,
            "effective_at": memory.effective_at.isoformat(),
            "expires_at": memory.expires_at.isoformat() if memory.expires_at else None,
            "status": memory.status.value,
            "version": memory.version,
        }

    @staticmethod
    def _to_record(memory: LongTermMemory) -> LongTermMemoryRecord:
        return LongTermMemoryRecord(
            memory_id=memory.memory_id,
            tenant_id=memory.tenant_id,
            owner_user_id=memory.owner_user_id,
            team_id=memory.team_id,
            scope=memory.scope.value,
            memory_key=memory.key,
            key_definition_version=memory.key_definition_version,
            category_id=memory.category_id,
            content=memory.content,
            value_json=memory.value_json,
            tags=memory.tags,
            source_ref=memory.source_ref,
            consent_id=memory.consent_id,
            confidence=memory.confidence,
            effective_at=memory.effective_at,
            expires_at=memory.expires_at,
            memory_metadata=memory.metadata,
            status=memory.status.value,
            lock_version=memory.version,
            created_at=memory.created_at,
            updated_at=memory.updated_at,
            deleted_at=memory.deleted_at,
        )

    @staticmethod
    def _consent_to_record(consent: MemoryConsent) -> MemoryConsentRecord:
        return MemoryConsentRecord(
            consent_id=consent.consent_id,
            tenant_id=consent.tenant_id,
            user_id=consent.user_id,
            purpose=consent.purpose,
            displayed_text=consent.displayed_text,
            memory_key=consent.memory_key,
            scope=consent.scope.value,
            policy_version=consent.policy_version,
            accepted_at=consent.accepted_at,
            revoked_at=consent.revoked_at,
            request_id=consent.request_id,
            command_fingerprint=consent.command_fingerprint,
        )

    @staticmethod
    def _consent_to_domain(record: MemoryConsentRecord) -> MemoryConsent:
        return MemoryConsent(
            consent_id=record.consent_id,
            tenant_id=record.tenant_id,
            user_id=record.user_id,
            purpose=record.purpose,
            displayed_text=record.displayed_text,
            memory_key=record.memory_key,
            scope=MemoryScope(record.scope),
            policy_version=record.policy_version,
            accepted_at=record.accepted_at,
            revoked_at=record.revoked_at,
            request_id=record.request_id,
            command_fingerprint=record.command_fingerprint,
        )
