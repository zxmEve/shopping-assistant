from __future__ import annotations

import asyncio
import os
from datetime import timedelta

import pytest

from procure_pilot.domain.budget import BudgetContext, InvocationKind, ProviderKind
from procure_pilot.domain.models import utc_now
from procure_pilot.persistence.budget_repository import InMemoryBudgetRepository
from procure_pilot.services.budget import (
    BudgetExceededError,
    RedisBudgetService,
    StaticBudgetLimitProvider,
    build_reservation_request,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_REDIS_URL"), reason="TEST_REDIS_URL is required for Redis integration"
)


def context() -> BudgetContext:
    return BudgetContext(
        tenant_id="demo_cn",
        user_id="employee_001",
        run_id="run_redis_budget_test",
        invocation_kind=InvocationKind.MODEL,
        provider_kind=ProviderKind.MODEL,
        provider_id="openai",
        model="gpt-test",
    )


@pytest.mark.asyncio
async def test_redis_lua_reserves_all_scopes_atomically_and_settles():
    from redis.asyncio import Redis

    redis = Redis.from_url(os.environ["TEST_REDIS_URL"], decode_responses=False)
    await redis.flushdb()
    repository = InMemoryBudgetRepository()
    limits = StaticBudgetLimitProvider(
        platform_daily=100,
        platform_monthly=1_000,
        tenant_monthly=500,
        user_daily=200,
        provider_limits={"MODEL:openai": {"DAILY": 150}},
    )
    service = RedisBudgetService(redis, repository, limit_provider=limits)

    async def attempt(index: int):
        try:
            return await service.reserve(
                build_reservation_request(
                    context=context(),
                    idempotency_key=f"redis-{index}",
                    estimated_cost_micro_usd=30,
                    pricing_version="test-v1",
                    limit_provider=limits,
                    ttl_seconds=300,
                )
            )
        except BudgetExceededError:
            return None

    reservations = await asyncio.gather(*(attempt(index) for index in range(10)))
    accepted = [item for item in reservations if item is not None]

    assert len(accepted) == 3
    await service.mark_dispatched(
        accepted[0].reservation_id,
        provider_invocation_id="pi_redis",
    )
    await service.settle(accepted[0].reservation_id, actual_cost_micro_usd=20)
    snapshots = await service.snapshot(context())
    platform_daily = next(
        item
        for item in snapshots
        if item.applied_limit.scope_type.value == "PLATFORM"
        and item.applied_limit.period_type.value == "DAILY"
    )
    assert platform_daily.reserved == 60
    assert platform_daily.committed == 20

    await redis.flushdb()
    recovered_service = RedisBudgetService(redis, repository, limit_provider=limits)
    recovered = await recovered_service.snapshot(context())
    recovered_platform_daily = next(
        item
        for item in recovered
        if item.applied_limit.scope_type.value == "PLATFORM"
        and item.applied_limit.period_type.value == "DAILY"
    )
    assert recovered_platform_daily.reserved == 60
    assert recovered_platform_daily.committed == 20
    with pytest.raises(BudgetExceededError):
        await recovered_service.reserve(
            build_reservation_request(
                context=context(),
                idempotency_key="redis-after-rebuild",
                estimated_cost_micro_usd=30,
                pricing_version="test-v1",
                limit_provider=limits,
                ttl_seconds=300,
            )
        )

    await redis.flushdb()
    orphan_repository = InMemoryBudgetRepository()
    orphan_service = RedisBudgetService(redis, orphan_repository, limit_provider=limits)
    await orphan_service.reserve(
        build_reservation_request(
            context=context(),
            idempotency_key="redis-orphan",
            estimated_cost_micro_usd=30,
            pricing_version="test-v1",
            limit_provider=limits,
            ttl_seconds=-1,
        )
    )
    recovery_service = RedisBudgetService(
        redis,
        InMemoryBudgetRepository(),
        limit_provider=limits,
    )
    released = await recovery_service.release_orphaned_reservations(
        before=utc_now() + timedelta(seconds=1)
    )
    assert released == 1
    after_orphan_release = await recovery_service.snapshot(context())
    assert {item.reserved for item in after_orphan_release} == {0}
    await redis.aclose()
