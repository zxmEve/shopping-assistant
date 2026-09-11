from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import create_engine

from procure_pilot.domain.budget import (
    BudgetContext,
    BudgetReservationStatus,
    InvocationKind,
    ProviderKind,
)
from procure_pilot.domain.models import utc_now
from procure_pilot.persistence.budget_repository import (
    InMemoryBudgetRepository,
    SqlAlchemyBudgetRepository,
)
from procure_pilot.persistence.models import Base
from procure_pilot.services.budget import (
    BudgetExceededError,
    InMemoryBudgetService,
    RedisBudgetService,
    StaticBudgetLimitProvider,
    build_reservation_request,
)
from procure_pilot.workers.budget_reconciler import BudgetReconciliationWorker


def budget_stack(*, platform_daily: int = 100):
    repository = InMemoryBudgetRepository()
    limits = StaticBudgetLimitProvider(
        platform_daily=platform_daily,
        platform_monthly=1_000,
        tenant_monthly=500,
        user_daily=200,
        provider_limits={"MODEL:openai": {"DAILY": 150}},
    )
    service = InMemoryBudgetService(repository, limit_provider=limits)
    return repository, limits, service


def context(*, user_id: str = "employee_001") -> BudgetContext:
    return BudgetContext(
        tenant_id="demo_cn",
        user_id=user_id,
        run_id="run_budget_test",
        invocation_kind=InvocationKind.MODEL,
        provider_kind=ProviderKind.MODEL,
        provider_id="openai",
        model="gpt-test",
    )


def request(limits, *, key: str, amount: int, user_id: str = "employee_001", ttl: int = 300):
    return build_reservation_request(
        context=context(user_id=user_id),
        idempotency_key=key,
        estimated_cost_micro_usd=amount,
        pricing_version="test-v1",
        limit_provider=limits,
        ttl_seconds=ttl,
    )


@pytest.mark.asyncio
async def test_multilevel_reservation_is_atomic_and_idempotent():
    repository, limits, service = budget_stack(platform_daily=100)
    first = await service.reserve(request(limits, key="first", amount=60))
    duplicate = await service.reserve(request(limits, key="first", amount=60))

    assert duplicate.reservation_id == first.reservation_id
    with pytest.raises(BudgetExceededError) as raised:
        await service.reserve(request(limits, key="second", amount=50))

    assert raised.value.scope_type.value == "PLATFORM"
    snapshots = await service.snapshot(context())
    assert {item.reserved for item in snapshots} == {60}
    assert len(repository.list_ledger(first.reservation_id)) == 5


@pytest.mark.asyncio
async def test_concurrent_reservations_do_not_overspend_last_capacity():
    _repository, limits, service = budget_stack(platform_daily=100)

    async def attempt(index: int):
        try:
            return await service.reserve(request(limits, key=f"call-{index}", amount=30))
        except BudgetExceededError:
            return None

    results = await asyncio.gather(*(attempt(index) for index in range(10)))
    accepted = [item for item in results if item is not None]

    assert len(accepted) == 3
    snapshots = await service.snapshot(context())
    platform_daily = next(
        item
        for item in snapshots
        if item.applied_limit.scope_type.value == "PLATFORM"
        and item.applied_limit.period_type.value == "DAILY"
    )
    assert platform_daily.reserved == 90


@pytest.mark.asyncio
async def test_dispatch_cannot_be_released_and_is_conservatively_reconciled():
    repository, limits, service = budget_stack()
    reservation = await service.reserve(request(limits, key="dispatch", amount=40))
    await service.mark_dispatched(
        reservation.reservation_id,
        provider_invocation_id="pi_test",
        provider_request_id="provider-request-test",
    )

    result = await service.release(reservation.reservation_id, reason="worker crashed")

    assert result.status == BudgetReservationStatus.RECONCILING
    snapshots = await service.snapshot(context())
    assert {item.reserved for item in snapshots} == {40}
    assert any(
        item.event_type.value == "RECONCILE"
        for item in repository.list_ledger(reservation.reservation_id)
    )


@pytest.mark.asyncio
async def test_settlement_moves_reserved_cost_to_committed_cost():
    repository, limits, service = budget_stack()
    reservation = await service.reserve(request(limits, key="settle", amount=40))
    await service.mark_dispatched(
        reservation.reservation_id,
        provider_invocation_id="pi_test",
    )

    settlement = await service.settle(
        reservation.reservation_id,
        actual_cost_micro_usd=25,
    )

    assert settlement.status == BudgetReservationStatus.SETTLED
    snapshots = await service.snapshot(context())
    assert {item.reserved for item in snapshots} == {0}
    assert {item.committed for item in snapshots} == {25}
    assert len(repository.list_ledger(reservation.reservation_id)) == 20


@pytest.mark.asyncio
async def test_worker_releases_only_undispatched_expired_reservations():
    repository, limits, service = budget_stack()
    reservation = await service.reserve(
        request(limits, key="expired", amount=40, ttl=-1)
    )
    worker = BudgetReconciliationWorker(
        budget_service=service,
        repository=repository,
    )

    stats = await worker.run_once()

    assert stats["released"] == 1
    restored = repository.get(reservation.reservation_id)
    assert restored is not None
    assert restored.status == BudgetReservationStatus.RELEASED


def test_daily_period_uses_tenant_timezone():
    _repository, limits, _service = budget_stack()
    instant = utc_now().replace(hour=16, minute=30)
    user_limit = next(
        item
        for item in limits.limits_for(context(), occurred_at=instant)
        if item.scope_type.value == "USER"
    )
    start, end = user_limit.period_bounds(instant)

    assert end - start == timedelta(days=1)
    assert start.astimezone().tzinfo is not None


@pytest.mark.asyncio
async def test_sqlalchemy_budget_repository_persists_immutable_scope_ledger(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'budget.db'}")
    Base.metadata.create_all(engine)
    repository = SqlAlchemyBudgetRepository(engine=engine)
    limits = StaticBudgetLimitProvider(
        platform_daily=100,
        platform_monthly=1_000,
        tenant_monthly=500,
        user_daily=200,
    )
    service = InMemoryBudgetService(repository, limit_provider=limits)

    reservation = await service.reserve(request(limits, key="sql-ledger", amount=40))
    await service.mark_dispatched(
        reservation.reservation_id,
        provider_invocation_id="pi_sql",
    )
    await service.settle(reservation.reservation_id, actual_cost_micro_usd=25)

    restored = repository.get(reservation.reservation_id)
    assert restored is not None
    assert restored.status == BudgetReservationStatus.SETTLED
    assert restored.actual_amount == 25
    ledger = repository.list_ledger(reservation.reservation_id)
    assert len(ledger) == 16
    assert sum(item.delta_committed for item in ledger if item.scope_type.value == "USER") == 25


@pytest.mark.asyncio
async def test_postgres_settlement_survives_redis_write_failure(monkeypatch):
    repository, limits, memory_service = budget_stack()
    reservation = await memory_service.reserve(
        request(limits, key="redis-settlement-failure", amount=40)
    )
    await memory_service.mark_dispatched(
        reservation.reservation_id,
        provider_invocation_id="pi_redis_failure",
    )
    service = RedisBudgetService(object(), repository, limit_provider=limits)

    async def fail_redis(*args, **kwargs):
        del args, kwargs
        raise ConnectionError("redis unavailable")

    async def already_ready():
        return None

    monkeypatch.setattr(service, "ensure_ready", already_ready)
    monkeypatch.setattr(service, "_settle_redis", fail_redis)

    result = await service.settle(
        reservation.reservation_id,
        actual_cost_micro_usd=25,
    )

    assert result.status == BudgetReservationStatus.SETTLED
    assert repository.get(reservation.reservation_id).actual_amount == 25
    assert len(repository.list_pending_outbox(utc_now())) == 1
