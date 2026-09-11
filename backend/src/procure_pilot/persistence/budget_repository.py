from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy import Engine, create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from procure_pilot.domain.budget import (
    AppliedBudgetLimit,
    BudgetEventType,
    BudgetLedgerEntry,
    BudgetLimit,
    BudgetReservation,
    BudgetReservationRequest,
    BudgetReservationStatus,
)
from procure_pilot.domain.models import utc_now
from procure_pilot.persistence.models import (
    BudgetLedgerEntryRecord,
    BudgetOutboxRecord,
    BudgetReservationRecord,
)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class BudgetRepositoryPort(Protocol):
    def get(self, reservation_id: str) -> BudgetReservation | None: ...

    def get_by_idempotency(self, idempotency_key: str) -> BudgetReservation | None: ...

    def record_reserved(self, reservation: BudgetReservation) -> BudgetReservation: ...

    def transition(
        self,
        reservation_id: str,
        *,
        status: BudgetReservationStatus,
        event_type: BudgetEventType,
        actual_amount: int | None = None,
        provider_invocation_id: str | None = None,
        provider_request_id: str | None = None,
        settlement_basis: str | None = None,
        delta_reserved: int = 0,
        delta_committed: int = 0,
        outbox_payload: dict[str, Any] | None = None,
        event_id: str | None = None,
    ) -> BudgetReservation: ...

    def list_due(self, before: datetime) -> list[BudgetReservation]: ...

    def list_for_rebuild(self) -> list[BudgetReservation]: ...

    def list_ledger(self, reservation_id: str) -> list[BudgetLedgerEntry]: ...

    def list_pending_outbox(self, before: datetime, *, limit: int = 100) -> list[dict[str, Any]]: ...

    def complete_outbox(self, event_id: str) -> None: ...

    def fail_outbox(self, event_id: str, error: str, *, available_at: datetime) -> None: ...


def _ledger_entries(
    reservation: BudgetReservation,
    *,
    event_id: str,
    event_type: BudgetEventType,
    delta_reserved: int,
    delta_committed: int,
) -> list[BudgetLedgerEntry]:
    return [
        BudgetLedgerEntry(
            event_id=event_id,
            reservation_id=reservation.reservation_id,
            event_type=event_type,
            scope_type=item.scope_type,
            scope_key=item.scope_key,
            metric=item.metric,
            period_start=item.period_start,
            period_end=item.period_end,
            delta_reserved=delta_reserved,
            delta_committed=delta_committed,
            policy_id=item.policy_id,
            policy_version=item.policy_version,
            metadata_json={
                "provider_kind": reservation.request.context.provider_kind.value,
                "provider_id": reservation.request.context.provider_id,
            },
        )
        for item in reservation.applied_limits
    ]


class InMemoryBudgetRepository:
    def __init__(self) -> None:
        self._reservations: dict[str, BudgetReservation] = {}
        self._idempotency: dict[str, str] = {}
        self._ledger: dict[tuple[str, str, str, str, str], BudgetLedgerEntry] = {}
        self._outbox: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def get(self, reservation_id: str) -> BudgetReservation | None:
        with self._lock:
            value = self._reservations.get(reservation_id)
            return value.model_copy(deep=True) if value is not None else None

    def get_by_idempotency(self, idempotency_key: str) -> BudgetReservation | None:
        with self._lock:
            reservation_id = self._idempotency.get(idempotency_key)
            return self.get(reservation_id) if reservation_id is not None else None

    def record_reserved(self, reservation: BudgetReservation) -> BudgetReservation:
        with self._lock:
            current = self.get_by_idempotency(reservation.request.idempotency_key)
            if current is not None:
                return current
            stored = reservation.model_copy(deep=True)
            self._reservations[stored.reservation_id] = stored
            self._idempotency[stored.request.idempotency_key] = stored.reservation_id
            self._append_entries(
                _ledger_entries(
                    stored,
                    event_id=f"be_{uuid4().hex[:20]}",
                    event_type=BudgetEventType.RESERVE,
                    delta_reserved=stored.reserved_amount,
                    delta_committed=0,
                )
            )
            return stored.model_copy(deep=True)

    def transition(
        self,
        reservation_id: str,
        *,
        status: BudgetReservationStatus,
        event_type: BudgetEventType,
        actual_amount: int | None = None,
        provider_invocation_id: str | None = None,
        provider_request_id: str | None = None,
        settlement_basis: str | None = None,
        delta_reserved: int = 0,
        delta_committed: int = 0,
        outbox_payload: dict[str, Any] | None = None,
        event_id: str | None = None,
    ) -> BudgetReservation:
        with self._lock:
            reservation = self._reservations[reservation_id].model_copy(deep=True)
            reservation.status = status
            reservation.actual_amount = actual_amount
            reservation.provider_invocation_id = (
                provider_invocation_id or reservation.provider_invocation_id
            )
            reservation.provider_request_id = provider_request_id or reservation.provider_request_id
            reservation.settlement_basis = settlement_basis or reservation.settlement_basis
            reservation.updated_at = utc_now()
            self._reservations[reservation_id] = reservation.model_copy(deep=True)
            resolved_event_id = event_id or f"be_{uuid4().hex[:20]}"
            self._append_entries(
                _ledger_entries(
                    reservation,
                    event_id=resolved_event_id,
                    event_type=event_type,
                    delta_reserved=delta_reserved,
                    delta_committed=delta_committed,
                )
            )
            if outbox_payload is not None:
                self._outbox.setdefault(
                    resolved_event_id,
                    {
                        "event_id": resolved_event_id,
                        "event_type": event_type.value,
                        "reservation_id": reservation_id,
                        "payload_json": outbox_payload,
                        "attempt_count": 0,
                        "available_at": utc_now(),
                        "processed_at": None,
                        "last_error": None,
                    },
                )
            return reservation.model_copy(deep=True)

    def _append_entries(self, entries: list[BudgetLedgerEntry]) -> None:
        for entry in entries:
            key = (
                entry.event_id,
                entry.scope_type.value,
                entry.scope_key,
                entry.period_start.isoformat(),
                entry.metric.value,
            )
            self._ledger.setdefault(key, entry.model_copy(deep=True))

    def list_due(self, before: datetime) -> list[BudgetReservation]:
        with self._lock:
            return [
                item.model_copy(deep=True)
                for item in self._reservations.values()
                if item.request.expires_at <= before
                and item.status
                in {
                    BudgetReservationStatus.RESERVED,
                    BudgetReservationStatus.DISPATCH_INTENT,
                    BudgetReservationStatus.DISPATCHED,
                    BudgetReservationStatus.RECONCILING,
                }
            ]

    def list_for_rebuild(self) -> list[BudgetReservation]:
        with self._lock:
            return [item.model_copy(deep=True) for item in self._reservations.values()]

    def list_ledger(self, reservation_id: str) -> list[BudgetLedgerEntry]:
        with self._lock:
            return sorted(
                (
                    item.model_copy(deep=True)
                    for item in self._ledger.values()
                    if item.reservation_id == reservation_id
                ),
                key=lambda item: item.occurred_at,
            )

    def list_pending_outbox(self, before: datetime, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            return [
                dict(item)
                for item in self._outbox.values()
                if item["processed_at"] is None and item["available_at"] <= before
            ][:limit]

    def complete_outbox(self, event_id: str) -> None:
        with self._lock:
            self._outbox[event_id]["processed_at"] = utc_now()

    def fail_outbox(self, event_id: str, error: str, *, available_at: datetime) -> None:
        with self._lock:
            item = self._outbox[event_id]
            item["attempt_count"] += 1
            item["last_error"] = error
            item["available_at"] = available_at


class SqlAlchemyBudgetRepository:
    def __init__(self, database_url: str | None = None, *, engine: Engine | None = None) -> None:
        if engine is None and database_url is None:
            raise ValueError("database_url or engine is required")
        self.engine = engine or create_engine(database_url, pool_pre_ping=True)
        self.session_factory = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def _session(self) -> Iterator[Session]:
        with self.session_factory() as session, session.begin():
            yield session

    def get(self, reservation_id: str) -> BudgetReservation | None:
        with self.session_factory() as session:
            row = session.get(BudgetReservationRecord, reservation_id)
            return self._reservation(row) if row is not None else None

    def get_by_idempotency(self, idempotency_key: str) -> BudgetReservation | None:
        with self.session_factory() as session:
            row = session.scalar(
                select(BudgetReservationRecord).where(
                    BudgetReservationRecord.idempotency_key == idempotency_key
                )
            )
            return self._reservation(row) if row is not None else None

    def record_reserved(self, reservation: BudgetReservation) -> BudgetReservation:
        try:
            with self._session() as session:
                session.add(self._reservation_record(reservation))
                self._add_ledger(
                    session,
                    reservation,
                    event_id=f"be_{uuid4().hex[:20]}",
                    event_type=BudgetEventType.RESERVE,
                    delta_reserved=reservation.reserved_amount,
                    delta_committed=0,
                )
            return reservation
        except IntegrityError:
            current = self.get_by_idempotency(reservation.request.idempotency_key)
            if current is None:
                raise
            return current

    def transition(
        self,
        reservation_id: str,
        *,
        status: BudgetReservationStatus,
        event_type: BudgetEventType,
        actual_amount: int | None = None,
        provider_invocation_id: str | None = None,
        provider_request_id: str | None = None,
        settlement_basis: str | None = None,
        delta_reserved: int = 0,
        delta_committed: int = 0,
        outbox_payload: dict[str, Any] | None = None,
        event_id: str | None = None,
    ) -> BudgetReservation:
        resolved_event_id = event_id or f"be_{uuid4().hex[:20]}"
        with self._session() as session:
            row = session.get(BudgetReservationRecord, reservation_id)
            if row is None:
                raise KeyError(reservation_id)
            row.status = status.value
            if actual_amount is not None:
                row.actual_amount = actual_amount
            if provider_invocation_id is not None:
                row.provider_invocation_id = provider_invocation_id
            if provider_request_id is not None:
                row.provider_request_id = provider_request_id
            if settlement_basis is not None:
                row.settlement_basis = settlement_basis
            row.updated_at = utc_now()
            reservation = self._reservation(row)
            self._add_ledger(
                session,
                reservation,
                event_id=resolved_event_id,
                event_type=event_type,
                delta_reserved=delta_reserved,
                delta_committed=delta_committed,
            )
            if outbox_payload is not None:
                session.add(
                    BudgetOutboxRecord(
                        outbox_id=f"bo_{uuid4().hex[:20]}",
                        event_id=resolved_event_id,
                        event_type=event_type.value,
                        reservation_id=reservation_id,
                        payload_json=outbox_payload,
                        attempt_count=0,
                        available_at=utc_now(),
                        created_at=utc_now(),
                    )
                )
        restored = self.get(reservation_id)
        assert restored is not None
        return restored

    def list_due(self, before: datetime) -> list[BudgetReservation]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(BudgetReservationRecord).where(
                    BudgetReservationRecord.expires_at <= before,
                    BudgetReservationRecord.status.in_(
                        [
                            BudgetReservationStatus.RESERVED.value,
                            BudgetReservationStatus.DISPATCH_INTENT.value,
                            BudgetReservationStatus.DISPATCHED.value,
                            BudgetReservationStatus.RECONCILING.value,
                        ]
                    ),
                )
            ).all()
            return [self._reservation(row) for row in rows]

    def list_for_rebuild(self) -> list[BudgetReservation]:
        with self.session_factory() as session:
            rows = session.scalars(select(BudgetReservationRecord)).all()
            return [self._reservation(row) for row in rows]

    def list_ledger(self, reservation_id: str) -> list[BudgetLedgerEntry]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(BudgetLedgerEntryRecord)
                .where(BudgetLedgerEntryRecord.reservation_id == reservation_id)
                .order_by(BudgetLedgerEntryRecord.occurred_at)
            ).all()
            return [
                BudgetLedgerEntry.model_validate(
                    {column.name: getattr(row, column.name) for column in row.__table__.columns}
                )
                for row in rows
            ]

    def list_pending_outbox(self, before: datetime, *, limit: int = 100) -> list[dict[str, Any]]:
        with self.session_factory() as session:
            rows = session.scalars(
                select(BudgetOutboxRecord)
                .where(
                    BudgetOutboxRecord.processed_at.is_(None),
                    BudgetOutboxRecord.available_at <= before,
                )
                .order_by(BudgetOutboxRecord.available_at)
                .limit(limit)
            ).all()
            return [
                {column.name: getattr(row, column.name) for column in row.__table__.columns}
                for row in rows
            ]

    def complete_outbox(self, event_id: str) -> None:
        with self._session() as session:
            row = session.scalar(
                select(BudgetOutboxRecord).where(BudgetOutboxRecord.event_id == event_id)
            )
            if row is not None:
                row.processed_at = utc_now()

    def fail_outbox(self, event_id: str, error: str, *, available_at: datetime) -> None:
        with self._session() as session:
            row = session.scalar(
                select(BudgetOutboxRecord).where(BudgetOutboxRecord.event_id == event_id)
            )
            if row is None:
                return
            row.attempt_count += 1
            row.last_error = error
            row.available_at = available_at

    @staticmethod
    def _add_ledger(
        session: Session,
        reservation: BudgetReservation,
        *,
        event_id: str,
        event_type: BudgetEventType,
        delta_reserved: int,
        delta_committed: int,
    ) -> None:
        for entry in _ledger_entries(
            reservation,
            event_id=event_id,
            event_type=event_type,
            delta_reserved=delta_reserved,
            delta_committed=delta_committed,
        ):
            session.add(BudgetLedgerEntryRecord(**entry.model_dump()))

    @staticmethod
    def _reservation_record(reservation: BudgetReservation) -> BudgetReservationRecord:
        request = reservation.request
        context = request.context
        return BudgetReservationRecord(
            reservation_id=reservation.reservation_id,
            idempotency_key=request.idempotency_key,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            run_id=context.run_id,
            tool_call_id=context.tool_call_id,
            agent_execution_id=context.agent_execution_id,
            provider_invocation_id=reservation.provider_invocation_id,
            invocation_kind=context.invocation_kind.value,
            provider_kind=context.provider_kind.value,
            provider_id=context.provider_id,
            model=context.model,
            pricing_version=request.pricing_version,
            reserved_amount=reservation.reserved_amount,
            actual_amount=reservation.actual_amount,
            status=reservation.status.value,
            provider_request_id=reservation.provider_request_id,
            settlement_basis=reservation.settlement_basis,
            applied_limits_json=[item.model_dump(mode="json") for item in reservation.applied_limits],
            metadata_json=request.metadata_json,
            occurred_at=request.occurred_at,
            expires_at=request.expires_at,
            created_at=reservation.created_at,
            updated_at=reservation.updated_at,
        )

    @staticmethod
    def _reservation(row: BudgetReservationRecord) -> BudgetReservation:
        applied = tuple(
            AppliedBudgetLimit.model_validate(item) for item in row.applied_limits_json
        )
        limits = tuple(
            BudgetLimit(
                policy_id=item.policy_id,
                policy_version=item.policy_version,
                scope_type=item.scope_type,
                scope_key=item.scope_key,
                period_type=item.period_type,
                metric=item.metric,
                hard_limit=item.hard_limit,
            )
            for item in applied
        )
        from procure_pilot.domain.budget import BudgetContext, InvocationKind, ProviderKind

        request = BudgetReservationRequest(
            context=BudgetContext(
                tenant_id=row.tenant_id,
                user_id=row.user_id,
                run_id=row.run_id,
                agent_execution_id=row.agent_execution_id,
                tool_call_id=row.tool_call_id,
                invocation_kind=InvocationKind(row.invocation_kind),
                provider_kind=ProviderKind(row.provider_kind),
                provider_id=row.provider_id,
                model=row.model,
            ),
            idempotency_key=row.idempotency_key,
            estimated_cost_micro_usd=row.reserved_amount,
            pricing_version=row.pricing_version,
            limits=limits,
            occurred_at=_as_utc(row.occurred_at),
            expires_at=_as_utc(row.expires_at),
            metadata_json=row.metadata_json,
        )
        return BudgetReservation(
            reservation_id=row.reservation_id,
            request=request,
            applied_limits=applied,
            reserved_amount=row.reserved_amount,
            actual_amount=row.actual_amount,
            status=BudgetReservationStatus(row.status),
            provider_invocation_id=row.provider_invocation_id,
            provider_request_id=row.provider_request_id,
            settlement_basis=row.settlement_basis,
            created_at=_as_utc(row.created_at),
            updated_at=_as_utc(row.updated_at),
        )
