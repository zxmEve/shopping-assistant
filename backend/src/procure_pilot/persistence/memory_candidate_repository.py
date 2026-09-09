from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from procure_pilot.domain.memory import (
    MemoryCandidate,
    MemoryCandidateConflict,
    MemoryCandidateStatus,
    MemoryScope,
)
from procure_pilot.domain.models import utc_now
from procure_pilot.persistence.models import MemoryCandidateRecord


class SqlAlchemyMemoryCandidateRepository:
    """Tenant- and owner-scoped persistence for reviewable memory proposals."""

    def __init__(self, database_url: str, *, engine: Engine | None = None, clock=utc_now) -> None:
        from sqlalchemy import create_engine

        self.engine = engine or create_engine(database_url, pool_pre_ping=True)
        self.session_factory = sessionmaker(self.engine, expire_on_commit=False)
        self.clock = clock

    def create_pending(self, candidate: MemoryCandidate) -> MemoryCandidate:
        with self.session_factory.begin() as session:
            existing = session.execute(
                select(MemoryCandidateRecord).where(
                    MemoryCandidateRecord.tenant_id == candidate.tenant_id,
                    MemoryCandidateRecord.owner_user_id == candidate.owner_user_id,
                    MemoryCandidateRecord.origin_run_id == candidate.origin_run_id,
                    MemoryCandidateRecord.proposal_fingerprint == candidate.proposal_fingerprint,
                )
            ).scalar_one_or_none()
            if existing is not None:
                return self._to_domain(existing)
            session.add(self._to_record(candidate))
        return candidate

    def list(
        self,
        *,
        tenant_id: str,
        owner_user_id: str,
        status: MemoryCandidateStatus | None = None,
        origin_run_id: str | None = None,
    ) -> list[MemoryCandidate]:
        now = self.clock()
        with self.session_factory.begin() as session:
            self._expire_pending(session, tenant_id=tenant_id, owner_user_id=owner_user_id, now=now)
            statement = select(MemoryCandidateRecord).where(
                MemoryCandidateRecord.tenant_id == tenant_id,
                MemoryCandidateRecord.owner_user_id == owner_user_id,
            )
            if status is not None:
                statement = statement.where(MemoryCandidateRecord.status == status.value)
            if origin_run_id is not None:
                statement = statement.where(MemoryCandidateRecord.origin_run_id == origin_run_id)
            records = (
                session.execute(statement.order_by(MemoryCandidateRecord.created_at.desc()))
                .scalars()
                .all()
            )
            return [self._to_domain(record) for record in records]

    def get(
        self,
        candidate_id: str,
        *,
        tenant_id: str,
        owner_user_id: str,
    ) -> MemoryCandidate:
        now = self.clock()
        with self.session_factory.begin() as session:
            self._expire_pending(session, tenant_id=tenant_id, owner_user_id=owner_user_id, now=now)
            record = session.execute(
                select(MemoryCandidateRecord).where(
                    MemoryCandidateRecord.candidate_id == candidate_id,
                    MemoryCandidateRecord.tenant_id == tenant_id,
                    MemoryCandidateRecord.owner_user_id == owner_user_id,
                )
            ).scalar_one_or_none()
            if record is None:
                raise PermissionError(
                    "Memory candidate is not accessible in the current tenant context"
                )
            return self._to_domain(record)

    def resolve(
        self,
        candidate_id: str,
        *,
        tenant_id: str,
        owner_user_id: str,
        status: MemoryCandidateStatus,
        request_id: str,
        accepted_memory_id: str | None = None,
    ) -> MemoryCandidate:
        if status not in {MemoryCandidateStatus.ACCEPTED, MemoryCandidateStatus.REJECTED}:
            raise ValueError("Candidate can only be accepted or rejected")
        now = self.clock()
        with self.session_factory.begin() as session:
            self._expire_pending(session, tenant_id=tenant_id, owner_user_id=owner_user_id, now=now)
            record = session.execute(
                select(MemoryCandidateRecord).where(
                    MemoryCandidateRecord.candidate_id == candidate_id,
                    MemoryCandidateRecord.tenant_id == tenant_id,
                    MemoryCandidateRecord.owner_user_id == owner_user_id,
                )
            ).scalar_one_or_none()
            if record is None:
                raise PermissionError(
                    "Memory candidate is not accessible in the current tenant context"
                )
            if record.status == status.value and record.resolution_request_id == request_id:
                return self._to_domain(record)
            if record.status != MemoryCandidateStatus.PENDING.value:
                raise ValueError(f"Memory candidate is already {record.status.lower()}")
            record.status = status.value
            record.resolution_request_id = request_id
            record.accepted_memory_id = accepted_memory_id
            record.resolved_at = now
            return self._to_domain(record)

    @staticmethod
    def _expire_pending(session, *, tenant_id: str, owner_user_id: str, now) -> None:
        session.execute(
            update(MemoryCandidateRecord)
            .where(
                MemoryCandidateRecord.tenant_id == tenant_id,
                MemoryCandidateRecord.owner_user_id == owner_user_id,
                MemoryCandidateRecord.status == MemoryCandidateStatus.PENDING.value,
                MemoryCandidateRecord.expires_at <= now,
            )
            .values(status=MemoryCandidateStatus.EXPIRED.value, resolved_at=now)
        )

    @staticmethod
    def _to_record(candidate: MemoryCandidate) -> MemoryCandidateRecord:
        return MemoryCandidateRecord(
            candidate_id=candidate.candidate_id,
            tenant_id=candidate.tenant_id,
            owner_user_id=candidate.owner_user_id,
            origin_run_id=candidate.origin_run_id,
            scope=candidate.scope.value,
            memory_key=candidate.key,
            key_definition_version=candidate.key_definition_version,
            category_id=candidate.category_id,
            proposed_content=candidate.proposed_content,
            proposed_value_json=candidate.proposed_value_json,
            tags=candidate.tags,
            source_message_refs=candidate.source_message_refs,
            source_ref=candidate.source_ref,
            confidence=candidate.confidence,
            proposal_fingerprint=candidate.proposal_fingerprint,
            dedup_target_memory_id=candidate.dedup_target_memory_id,
            conflict_type=candidate.conflict_type.value,
            status=candidate.status.value,
            expires_at=candidate.expires_at,
            accepted_memory_id=candidate.accepted_memory_id,
            resolution_request_id=candidate.resolution_request_id,
            created_at=candidate.created_at,
            resolved_at=candidate.resolved_at,
        )

    @staticmethod
    def _to_domain(record: MemoryCandidateRecord) -> MemoryCandidate:
        return MemoryCandidate(
            candidate_id=record.candidate_id,
            tenant_id=record.tenant_id,
            owner_user_id=record.owner_user_id,
            origin_run_id=record.origin_run_id,
            scope=MemoryScope(record.scope),
            key=record.memory_key,
            key_definition_version=record.key_definition_version,
            category_id=record.category_id,
            proposed_content=record.proposed_content,
            proposed_value_json=record.proposed_value_json,
            tags=record.tags,
            source_message_refs=record.source_message_refs,
            source_ref=record.source_ref,
            confidence=record.confidence,
            proposal_fingerprint=record.proposal_fingerprint,
            dedup_target_memory_id=record.dedup_target_memory_id,
            conflict_type=MemoryCandidateConflict(record.conflict_type),
            status=MemoryCandidateStatus(record.status),
            expires_at=record.expires_at,
            accepted_memory_id=record.accepted_memory_id,
            resolution_request_id=record.resolution_request_id,
            created_at=record.created_at,
            resolved_at=record.resolved_at,
        )
