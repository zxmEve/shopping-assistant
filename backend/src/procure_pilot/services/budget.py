from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel

from procure_pilot.domain.budget import (
    AppliedBudgetLimit,
    BudgetContext,
    BudgetCounterSnapshot,
    BudgetEventType,
    BudgetLimit,
    BudgetPeriodType,
    BudgetReservation,
    BudgetReservationRequest,
    BudgetReservationStatus,
    BudgetScopeType,
    BudgetSettlement,
    deserialize_applied_limits,
    serialize_applied_limits,
)
from procure_pilot.domain.models import utc_now
from procure_pilot.persistence.budget_repository import BudgetRepositoryPort


class BudgetExceededError(RuntimeError):
    def __init__(
        self,
        *,
        scope_type: BudgetScopeType,
        scope_key: str,
        used: int,
        requested: int,
        limit: int,
        reset_at: datetime,
    ) -> None:
        self.scope_type = scope_type
        self.scope_key = scope_key
        self.used = used
        self.requested = requested
        self.limit = limit
        self.reset_at = reset_at
        super().__init__(
            f"Cost budget exceeded at {scope_type.value}: "
            f"{used} + {requested} > {limit}; resets at {reset_at.isoformat()}"
        )


class BudgetService(Protocol):
    async def reserve(self, request: BudgetReservationRequest) -> BudgetReservation: ...

    async def mark_dispatched(
        self,
        reservation_id: str,
        *,
        provider_invocation_id: str,
        provider_request_id: str | None = None,
    ) -> BudgetReservation: ...

    async def settle(
        self,
        reservation_id: str,
        *,
        actual_cost_micro_usd: int,
        provider_request_id: str | None = None,
        settlement_basis: str = "PROVIDER_USAGE",
    ) -> BudgetSettlement: ...

    async def release(self, reservation_id: str, *, reason: str) -> BudgetReservation: ...

    async def mark_reconciling(
        self, reservation_id: str, *, reason: str
    ) -> BudgetReservation: ...

    async def snapshot(
        self, context: BudgetContext, *, occurred_at: datetime | None = None
    ) -> list[BudgetCounterSnapshot]: ...


class BudgetLimitProvider(Protocol):
    def limits_for(
        self, context: BudgetContext, *, occurred_at: datetime
    ) -> tuple[BudgetLimit, ...]: ...


class StaticBudgetLimitProvider:
    """Resolve platform, tenant, user and provider limits from trusted configuration."""

    def __init__(
        self,
        *,
        platform_daily: int,
        platform_monthly: int,
        tenant_monthly: int,
        user_daily: int,
        tenant_timezone: str = "Asia/Shanghai",
        provider_limits: Mapping[str, Mapping[str, int]] | None = None,
        tenant_monthly_overrides: Mapping[str, int] | None = None,
        user_daily_overrides: Mapping[str, int] | None = None,
    ) -> None:
        self.platform_daily = platform_daily
        self.platform_monthly = platform_monthly
        self.tenant_monthly = tenant_monthly
        self.user_daily = user_daily
        self.tenant_timezone = tenant_timezone
        self.provider_limits = dict(provider_limits or {})
        self.tenant_monthly_overrides = dict(tenant_monthly_overrides or {})
        self.user_daily_overrides = dict(user_daily_overrides or {})

    def limits_for(
        self, context: BudgetContext, *, occurred_at: datetime
    ) -> tuple[BudgetLimit, ...]:
        del occurred_at
        limits = [
            self._limit(BudgetScopeType.PLATFORM, "global", BudgetPeriodType.DAILY, self.platform_daily, "UTC"),
            self._limit(
                BudgetScopeType.PLATFORM,
                "global",
                BudgetPeriodType.MONTHLY,
                self.platform_monthly,
                "UTC",
            ),
            self._limit(
                BudgetScopeType.TENANT,
                context.tenant_id,
                BudgetPeriodType.MONTHLY,
                int(self.tenant_monthly_overrides.get(context.tenant_id, self.tenant_monthly)),
                self.tenant_timezone,
            ),
            self._limit(
                BudgetScopeType.USER,
                f"{context.tenant_id}/{context.user_id}",
                BudgetPeriodType.DAILY,
                int(
                    self.user_daily_overrides.get(
                        f"{context.tenant_id}/{context.user_id}", self.user_daily
                    )
                ),
                self.tenant_timezone,
            ),
        ]
        provider_key = f"{context.provider_kind.value}:{context.provider_id}"
        configured = self.provider_limits.get(provider_key, {})
        for period_name, amount in configured.items():
            period = BudgetPeriodType(period_name.upper())
            limits.append(
                self._limit(
                    BudgetScopeType.PROVIDER,
                    provider_key,
                    period,
                    int(amount),
                    "UTC",
                )
            )
        return tuple(limits)

    @staticmethod
    def _limit(
        scope_type: BudgetScopeType,
        scope_key: str,
        period_type: BudgetPeriodType,
        amount: int,
        timezone: str,
    ) -> BudgetLimit:
        slug = f"{scope_type.value.lower()}-{scope_key}-{period_type.value.lower()}"
        return BudgetLimit(
            policy_id=f"cfg:{slug}",
            scope_type=scope_type,
            scope_key=scope_key,
            period_type=period_type,
            period_timezone=timezone,
            hard_limit=amount,
        )


def build_reservation_request(
    *,
    context: BudgetContext,
    idempotency_key: str,
    estimated_cost_micro_usd: int,
    pricing_version: str,
    limit_provider: BudgetLimitProvider,
    ttl_seconds: int,
    occurred_at: datetime | None = None,
    metadata_json: dict[str, Any] | None = None,
) -> BudgetReservationRequest:
    instant = occurred_at or utc_now()
    return BudgetReservationRequest(
        context=context,
        idempotency_key=idempotency_key,
        estimated_cost_micro_usd=estimated_cost_micro_usd,
        pricing_version=pricing_version,
        limits=limit_provider.limits_for(context, occurred_at=instant),
        occurred_at=instant,
        expires_at=instant + timedelta(seconds=ttl_seconds),
        metadata_json=metadata_json or {},
    )


def _applied_limits(
    request: BudgetReservationRequest, *, key_builder: Any
) -> tuple[AppliedBudgetLimit, ...]:
    return tuple(
        AppliedBudgetLimit(
            policy_id=item.policy_id,
            policy_version=item.policy_version,
            scope_type=item.scope_type,
            scope_key=item.scope_key,
            period_type=item.period_type,
            period_start=item.period_bounds(request.occurred_at)[0],
            period_end=item.period_bounds(request.occurred_at)[1],
            metric=item.metric,
            hard_limit=item.hard_limit,
            counter_key=key_builder(item.counter_identity(request.occurred_at)),
        )
        for item in request.limits
    )


class InMemoryBudgetService:
    """Atomic process-local implementation used by tests and the JSON demo backend."""

    def __init__(
        self,
        repository: BudgetRepositoryPort,
        *,
        limit_provider: BudgetLimitProvider | None = None,
    ) -> None:
        self.repository = repository
        self.limit_provider = limit_provider
        self._counters: dict[str, dict[str, int]] = {}
        self._lock = asyncio.Lock()

    async def reserve(self, request: BudgetReservationRequest) -> BudgetReservation:
        async with self._lock:
            current = self.repository.get_by_idempotency(request.idempotency_key)
            if current is not None:
                return current
            applied = _applied_limits(request, key_builder=lambda value: f"memory:{value}")
            amount = request.estimated_cost_micro_usd
            for item in applied:
                counter = self._counters.setdefault(
                    item.counter_key, {"reserved": 0, "committed": 0}
                )
                used = counter["reserved"] + counter["committed"]
                if used + amount > item.hard_limit:
                    raise BudgetExceededError(
                        scope_type=item.scope_type,
                        scope_key=item.scope_key,
                        used=used,
                        requested=amount,
                        limit=item.hard_limit,
                        reset_at=item.period_end,
                    )
            for item in applied:
                self._counters[item.counter_key]["reserved"] += amount
            reservation = BudgetReservation(
                request=request,
                applied_limits=applied,
                reserved_amount=amount,
            )
            try:
                return self.repository.record_reserved(reservation)
            except Exception:
                for item in applied:
                    self._counters[item.counter_key]["reserved"] -= amount
                raise

    async def mark_dispatched(
        self,
        reservation_id: str,
        *,
        provider_invocation_id: str,
        provider_request_id: str | None = None,
    ) -> BudgetReservation:
        async with self._lock:
            self.repository.transition(
                reservation_id,
                status=BudgetReservationStatus.DISPATCH_INTENT,
                event_type=BudgetEventType.DISPATCH_INTENT,
                provider_invocation_id=provider_invocation_id,
                provider_request_id=provider_request_id,
            )
            return self.repository.transition(
                reservation_id,
                status=BudgetReservationStatus.DISPATCHED,
                event_type=BudgetEventType.DISPATCH,
                provider_invocation_id=provider_invocation_id,
                provider_request_id=provider_request_id,
            )

    async def settle(
        self,
        reservation_id: str,
        *,
        actual_cost_micro_usd: int,
        provider_request_id: str | None = None,
        settlement_basis: str = "PROVIDER_USAGE",
    ) -> BudgetSettlement:
        if actual_cost_micro_usd < 0:
            raise ValueError("Actual cost cannot be negative")
        async with self._lock:
            reservation = self.repository.get(reservation_id)
            if reservation is None:
                raise KeyError(reservation_id)
            if reservation.status in {
                BudgetReservationStatus.SETTLED,
                BudgetReservationStatus.OVERAGE,
            }:
                return BudgetSettlement(
                    reservation_id=reservation_id,
                    status=reservation.status,
                    reserved_amount=reservation.reserved_amount,
                    actual_amount=reservation.actual_amount or 0,
                    overage_amount=max(
                        0, (reservation.actual_amount or 0) - reservation.reserved_amount
                    ),
                )
            overage = max(0, actual_cost_micro_usd - reservation.reserved_amount)
            status = (
                BudgetReservationStatus.OVERAGE
                if overage
                else BudgetReservationStatus.SETTLED
            )
            for item in reservation.applied_limits:
                counter = self._counters[item.counter_key]
                counter["reserved"] -= reservation.reserved_amount
                counter["committed"] += actual_cost_micro_usd
            self.repository.transition(
                reservation_id,
                status=status,
                event_type=(BudgetEventType.OVERAGE if overage else BudgetEventType.SETTLE),
                actual_amount=actual_cost_micro_usd,
                provider_request_id=provider_request_id,
                settlement_basis=settlement_basis,
                delta_reserved=-reservation.reserved_amount,
                delta_committed=actual_cost_micro_usd,
            )
            return BudgetSettlement(
                reservation_id=reservation_id,
                status=status,
                reserved_amount=reservation.reserved_amount,
                actual_amount=actual_cost_micro_usd,
                overage_amount=overage,
            )

    async def release(self, reservation_id: str, *, reason: str) -> BudgetReservation:
        async with self._lock:
            reservation = self.repository.get(reservation_id)
            if reservation is None:
                raise KeyError(reservation_id)
            if reservation.status == BudgetReservationStatus.RELEASED:
                return reservation
            if reservation.status not in {BudgetReservationStatus.RESERVED}:
                return self.repository.transition(
                    reservation_id,
                    status=BudgetReservationStatus.RECONCILING,
                    event_type=BudgetEventType.RECONCILE,
                    settlement_basis=reason,
                )
            for item in reservation.applied_limits:
                self._counters[item.counter_key]["reserved"] -= reservation.reserved_amount
            return self.repository.transition(
                reservation_id,
                status=BudgetReservationStatus.RELEASED,
                event_type=BudgetEventType.RELEASE,
                settlement_basis=reason,
                delta_reserved=-reservation.reserved_amount,
            )

    async def mark_reconciling(
        self, reservation_id: str, *, reason: str
    ) -> BudgetReservation:
        async with self._lock:
            return self.repository.transition(
                reservation_id,
                status=BudgetReservationStatus.RECONCILING,
                event_type=BudgetEventType.RECONCILE,
                settlement_basis=reason,
            )

    async def snapshot(
        self, context: BudgetContext, *, occurred_at: datetime | None = None
    ) -> list[BudgetCounterSnapshot]:
        if self.limit_provider is None:
            raise RuntimeError("Budget snapshot requires a BudgetLimitProvider")
        instant = occurred_at or utc_now()
        request = BudgetReservationRequest(
            context=context,
            idempotency_key="snapshot",
            estimated_cost_micro_usd=0,
            pricing_version="snapshot",
            limits=self.limit_provider.limits_for(context, occurred_at=instant),
            occurred_at=instant,
            expires_at=instant + timedelta(seconds=1),
        )
        applied = _applied_limits(request, key_builder=lambda value: f"memory:{value}")
        async with self._lock:
            return [
                BudgetCounterSnapshot(
                    applied_limit=item,
                    reserved=max(0, self._counters.get(item.counter_key, {}).get("reserved", 0)),
                    committed=max(
                        0, self._counters.get(item.counter_key, {}).get("committed", 0)
                    ),
                )
                for item in applied
            ]


class RedisBudgetService:
    """Redis online gate with PostgreSQL-compatible durable ledger projection."""

    RESERVE_SCRIPT = r"""
local count = tonumber(ARGV[1])
local amount = tonumber(ARGV[2])
local reservation_id = ARGV[3]
local expires_at_ms = tonumber(ARGV[4])
local applied_json = ARGV[5]
local request_json = ARGV[6]
local idempotency_ttl = tonumber(ARGV[7])
if redis.call('EXISTS', KEYS[count + 5]) == 0 then return {'NOT_READY'} end
local existing = redis.call('GET', KEYS[count + 2])
if existing then
  return {'EXISTS', existing}
end
for i = 1, count do
  local base = 7 + ((i - 1) * 3)
  local hard_limit = tonumber(ARGV[base + 1])
  local reserved = tonumber(redis.call('HGET', KEYS[i], 'reserved') or '0')
  local committed = tonumber(redis.call('HGET', KEYS[i], 'committed') or '0')
  if reserved + committed + amount > hard_limit then
    return {'DENIED', tostring(i), tostring(reserved + committed), tostring(hard_limit)}
  end
end
for i = 1, count do
  local base = 7 + ((i - 1) * 3)
  local hard_limit = tonumber(ARGV[base + 1])
  local policy_version = ARGV[base + 2]
  local expire_at = tonumber(ARGV[base + 3])
  redis.call('HSET', KEYS[i], 'limit', hard_limit, 'policy_version', policy_version)
  redis.call('HINCRBY', KEYS[i], 'reserved', amount)
  redis.call('EXPIREAT', KEYS[i], expire_at)
end
redis.call('HSET', KEYS[count + 1],
  'reservation_id', reservation_id,
  'status', 'RESERVED',
  'reserved_amount', amount,
  'actual_amount', '',
  'applied_limits', applied_json,
  'request', request_json)
redis.call('PEXPIREAT', KEYS[count + 1], expires_at_ms + 2592000000)
redis.call('SET', KEYS[count + 2], reservation_id, 'EX', idempotency_ttl)
redis.call('ZADD', KEYS[count + 3], expires_at_ms, reservation_id)
redis.call('XADD', KEYS[count + 4], '*', 'event', 'RESERVE', 'reservation_id', reservation_id)
return {'OK', reservation_id}
"""

    DISPATCH_SCRIPT = r"""
if redis.call('EXISTS', KEYS[3]) == 0 then return {'NOT_READY'} end
local status = redis.call('HGET', KEYS[1], 'status')
if not status then return {'MISSING'} end
if status == 'SETTLED' or status == 'OVERAGE' or status == 'RELEASED' then
  return {'TERMINAL', status}
end
redis.call('HSET', KEYS[1], 'status', 'DISPATCHED',
  'provider_invocation_id', ARGV[1], 'provider_request_id', ARGV[2])
redis.call('XADD', KEYS[2], '*', 'event', 'DISPATCH', 'reservation_id', ARGV[3])
return {'OK'}
"""

    SETTLE_SCRIPT = r"""
local count = tonumber(ARGV[1])
local reserved_amount = tonumber(ARGV[2])
local actual_amount = tonumber(ARGV[3])
local reservation_id = ARGV[4]
if redis.call('EXISTS', KEYS[count + 4]) == 0 then return {'NOT_READY'} end
local status = redis.call('HGET', KEYS[count + 1], 'status')
if not status then return {'MISSING'} end
if status == 'SETTLED' or status == 'OVERAGE' then return {'ALREADY', status} end
if status == 'RELEASED' then return {'INVALID', status} end
local final_status = 'SETTLED'
if actual_amount > reserved_amount then final_status = 'OVERAGE' end
for i = 1, count do
  redis.call('HINCRBY', KEYS[i], 'reserved', -reserved_amount)
  redis.call('HINCRBY', KEYS[i], 'committed', actual_amount)
end
redis.call('HSET', KEYS[count + 1], 'status', final_status,
  'actual_amount', actual_amount, 'provider_request_id', ARGV[5])
redis.call('ZREM', KEYS[count + 2], reservation_id)
redis.call('XADD', KEYS[count + 3], '*', 'event', final_status,
  'reservation_id', reservation_id, 'actual_amount', actual_amount)
return {'OK', final_status}
"""

    RELEASE_SCRIPT = r"""
local count = tonumber(ARGV[1])
local reserved_amount = tonumber(ARGV[2])
local reservation_id = ARGV[3]
if redis.call('EXISTS', KEYS[count + 4]) == 0 then return {'NOT_READY'} end
local status = redis.call('HGET', KEYS[count + 1], 'status')
if not status then return {'MISSING'} end
if status == 'RELEASED' then return {'ALREADY'} end
if status ~= 'RESERVED' then return {'UNSAFE', status} end
for i = 1, count do
  redis.call('HINCRBY', KEYS[i], 'reserved', -reserved_amount)
end
redis.call('HSET', KEYS[count + 1], 'status', 'RELEASED', 'release_reason', ARGV[4])
redis.call('ZREM', KEYS[count + 2], reservation_id)
redis.call('XADD', KEYS[count + 3], '*', 'event', 'RELEASE', 'reservation_id', reservation_id)
return {'OK'}
"""

    def __init__(
        self,
        redis: Any,
        repository: BudgetRepositoryPort,
        *,
        prefix: str = "budget:{budget}:v1",
        idempotency_ttl_seconds: int = 30 * 24 * 60 * 60,
        counter_retention_seconds: int = 30 * 24 * 60 * 60,
        limit_provider: BudgetLimitProvider | None = None,
    ) -> None:
        self.redis = redis
        self.repository = repository
        self.prefix = prefix.rstrip(":")
        self.idempotency_ttl_seconds = idempotency_ttl_seconds
        self.counter_retention_seconds = counter_retention_seconds
        self.limit_provider = limit_provider
        self._bootstrap_lock = asyncio.Lock()

    def _counter_key(self, identity: str) -> str:
        return f"{self.prefix}:counter:{identity}"

    def _reservation_key(self, reservation_id: str) -> str:
        return f"{self.prefix}:reservation:{reservation_id}"

    def _idempotency_key(self, value: str) -> str:
        digest = __import__("hashlib").sha256(value.encode("utf-8")).hexdigest()
        return f"{self.prefix}:idempotency:{digest}"

    @property
    def _expiry_key(self) -> str:
        return f"{self.prefix}:reservation_expiry"

    @property
    def _stream_key(self) -> str:
        return f"{self.prefix}:events"

    @property
    def _ready_key(self) -> str:
        return f"{self.prefix}:ready"

    @property
    def _rebuild_lock_key(self) -> str:
        return f"{self.prefix}:rebuild_lock"

    async def ensure_ready(self) -> None:
        """Fail closed until Redis counters can be rebuilt from the durable database."""
        if await self.redis.exists(self._ready_key):
            return
        async with self._bootstrap_lock:
            if await self.redis.exists(self._ready_key):
                return
            owner = uuid4().hex
            acquired = await self.redis.set(
                self._rebuild_lock_key,
                owner,
                nx=True,
                ex=60,
            )
            if not acquired:
                for _ in range(100):
                    await asyncio.sleep(0.05)
                    if await self.redis.exists(self._ready_key):
                        return
                raise RuntimeError("Redis budget counters are still being rebuilt")
            try:
                await self._rebuild_from_repository()
                await self.redis.set(self._ready_key, utc_now().isoformat())
            finally:
                await self.redis.eval(
                    "if redis.call('GET', KEYS[1]) == ARGV[1] then "
                    "return redis.call('DEL', KEYS[1]) else return 0 end",
                    1,
                    self._rebuild_lock_key,
                    owner,
                )

    async def _rebuild_from_repository(self) -> None:
        now = utc_now()
        stale_keys: list[Any] = [self._expiry_key]
        for pattern in (
            f"{self.prefix}:counter:*",
            f"{self.prefix}:reservation:*",
            f"{self.prefix}:idempotency:*",
        ):
            stale_keys.extend([item async for item in self.redis.scan_iter(match=pattern)])
        if stale_keys:
            await self.redis.delete(*stale_keys)

        counters: dict[str, dict[str, Any]] = {}
        active_statuses = {
            BudgetReservationStatus.RESERVED,
            BudgetReservationStatus.DISPATCH_INTENT,
            BudgetReservationStatus.DISPATCHED,
            BudgetReservationStatus.RECONCILING,
        }
        reservations = self.repository.list_for_rebuild()
        pipeline = self.redis.pipeline(transaction=True)
        for reservation in reservations:
            is_active = reservation.status in active_statuses
            for item in reservation.applied_limits:
                retained_until = item.period_end + timedelta(
                    seconds=self.counter_retention_seconds
                )
                if not is_active and retained_until <= now:
                    continue
                counter = counters.setdefault(
                    item.counter_key,
                    {
                        "reserved": 0,
                        "committed": 0,
                        "limit": item.hard_limit,
                        "policy_version": item.policy_version,
                        "expires_at": retained_until,
                    },
                )
                counter["limit"] = min(counter["limit"], item.hard_limit)
                counter["policy_version"] = max(
                    counter["policy_version"], item.policy_version
                )
                counter["expires_at"] = max(
                    counter["expires_at"],
                    retained_until if retained_until > now else now + timedelta(days=30),
                )
                if is_active:
                    counter["reserved"] += reservation.reserved_amount
                elif reservation.status in {
                    BudgetReservationStatus.SETTLED,
                    BudgetReservationStatus.OVERAGE,
                }:
                    counter["committed"] += reservation.actual_amount or 0

            record_until = reservation.request.expires_at + timedelta(
                seconds=self.idempotency_ttl_seconds
            )
            if not is_active and record_until <= now:
                continue
            if record_until <= now:
                record_until = now + timedelta(seconds=self.idempotency_ttl_seconds)
            pipeline.hset(
                self._reservation_key(reservation.reservation_id),
                mapping={
                    "reservation_id": reservation.reservation_id,
                    "status": reservation.status.value,
                    "reserved_amount": reservation.reserved_amount,
                    "actual_amount": reservation.actual_amount or "",
                    "applied_limits": serialize_applied_limits(reservation.applied_limits),
                    "request": reservation.request.model_dump_json(),
                    "provider_invocation_id": reservation.provider_invocation_id or "",
                    "provider_request_id": reservation.provider_request_id or "",
                },
            )
            pipeline.expireat(
                self._reservation_key(reservation.reservation_id), int(record_until.timestamp())
            )
            pipeline.set(
                self._idempotency_key(reservation.request.idempotency_key),
                reservation.reservation_id,
                ex=self.idempotency_ttl_seconds,
            )
            if is_active:
                pipeline.zadd(
                    self._expiry_key,
                    {reservation.reservation_id: reservation.request.expires_at.timestamp()},
                )
        for key, counter in counters.items():
            pipeline.hset(
                key,
                mapping={
                    "reserved": counter["reserved"],
                    "committed": counter["committed"],
                    "limit": counter["limit"],
                    "policy_version": counter["policy_version"],
                },
            )
            pipeline.expireat(key, int(counter["expires_at"].timestamp()))
        await pipeline.execute()

    async def reserve(self, request: BudgetReservationRequest) -> BudgetReservation:
        await self.ensure_ready()
        existing = self.repository.get_by_idempotency(request.idempotency_key)
        if existing is not None:
            return existing
        applied = _applied_limits(request, key_builder=self._counter_key)
        reservation = BudgetReservation(
            request=request,
            applied_limits=applied,
            reserved_amount=request.estimated_cost_micro_usd,
        )
        keys = [item.counter_key for item in applied]
        keys.extend(
            [
                self._reservation_key(reservation.reservation_id),
                self._idempotency_key(request.idempotency_key),
                self._expiry_key,
                self._stream_key,
                self._ready_key,
            ]
        )
        args: list[Any] = [
            len(applied),
            reservation.reserved_amount,
            reservation.reservation_id,
            int(request.expires_at.timestamp() * 1000),
            serialize_applied_limits(applied),
            request.model_dump_json(),
            self.idempotency_ttl_seconds,
        ]
        for item in applied:
            args.extend(
                [
                    item.hard_limit,
                    item.policy_version,
                    int(item.period_end.timestamp()) + self.counter_retention_seconds,
                ]
            )
        result = await self.redis.eval(self.RESERVE_SCRIPT, len(keys), *keys, *args)
        decoded = self._decode(result)
        if decoded[0] == "EXISTS":
            for _ in range(20):
                current = self.repository.get(decoded[1])
                if current is not None:
                    return current
                await asyncio.sleep(0.01)
            raise RuntimeError("Redis reservation exists without a durable ledger record")
        if decoded[0] == "DENIED":
            index = int(decoded[1]) - 1
            item = applied[index]
            raise BudgetExceededError(
                scope_type=item.scope_type,
                scope_key=item.scope_key,
                used=int(decoded[2]),
                requested=reservation.reserved_amount,
                limit=int(decoded[3]),
                reset_at=item.period_end,
            )
        try:
            return self.repository.record_reserved(reservation)
        except Exception:
            await self._release_redis(reservation, reason="DURABLE_LEDGER_WRITE_FAILED")
            raise

    async def mark_dispatched(
        self,
        reservation_id: str,
        *,
        provider_invocation_id: str,
        provider_request_id: str | None = None,
    ) -> BudgetReservation:
        await self.ensure_ready()
        self.repository.transition(
            reservation_id,
            status=BudgetReservationStatus.DISPATCH_INTENT,
            event_type=BudgetEventType.DISPATCH_INTENT,
            provider_invocation_id=provider_invocation_id,
            provider_request_id=provider_request_id,
        )
        result = await self.redis.eval(
            self.DISPATCH_SCRIPT,
            3,
            self._reservation_key(reservation_id),
            self._stream_key,
            self._ready_key,
            provider_invocation_id,
            provider_request_id or "",
            reservation_id,
        )
        decoded = self._decode(result)
        if decoded[0] not in {"OK", "TERMINAL"}:
            raise RuntimeError(f"Cannot mark Redis reservation as dispatched: {decoded}")
        return self.repository.transition(
            reservation_id,
            status=BudgetReservationStatus.DISPATCHED,
            event_type=BudgetEventType.DISPATCH,
            provider_invocation_id=provider_invocation_id,
            provider_request_id=provider_request_id,
        )

    async def settle(
        self,
        reservation_id: str,
        *,
        actual_cost_micro_usd: int,
        provider_request_id: str | None = None,
        settlement_basis: str = "PROVIDER_USAGE",
    ) -> BudgetSettlement:
        await self.ensure_ready()
        if actual_cost_micro_usd < 0:
            raise ValueError("Actual cost cannot be negative")
        reservation = self.repository.get(reservation_id)
        if reservation is None:
            raise KeyError(reservation_id)
        if reservation.status in {
            BudgetReservationStatus.SETTLED,
            BudgetReservationStatus.OVERAGE,
        }:
            actual = reservation.actual_amount or 0
            return BudgetSettlement(
                reservation_id=reservation_id,
                status=reservation.status,
                reserved_amount=reservation.reserved_amount,
                actual_amount=actual,
                overage_amount=max(0, actual - reservation.reserved_amount),
            )
        overage = max(0, actual_cost_micro_usd - reservation.reserved_amount)
        status = BudgetReservationStatus.OVERAGE if overage else BudgetReservationStatus.SETTLED
        event_type = BudgetEventType.OVERAGE if overage else BudgetEventType.SETTLE
        event_id = f"be_{uuid4().hex[:20]}"
        payload = {
            "operation": "SETTLE",
            "reservation_id": reservation_id,
            "actual_cost_micro_usd": actual_cost_micro_usd,
            "provider_request_id": provider_request_id,
        }
        if overage:
            await self._settle_redis(
                reservation,
                actual_cost_micro_usd=actual_cost_micro_usd,
                provider_request_id=provider_request_id,
            )
        self.repository.transition(
            reservation_id,
            status=status,
            event_type=event_type,
            actual_amount=actual_cost_micro_usd,
            provider_request_id=provider_request_id,
            settlement_basis=settlement_basis,
            delta_reserved=-reservation.reserved_amount,
            delta_committed=actual_cost_micro_usd,
            outbox_payload=payload,
            event_id=event_id,
        )
        if not overage:
            try:
                await self._settle_redis(
                    reservation,
                    actual_cost_micro_usd=actual_cost_micro_usd,
                    provider_request_id=provider_request_id,
                )
            except Exception:
                # PostgreSQL is already settled and its outbox row remains pending.
                # Keeping the Redis reservation is conservative; the worker will replay it.
                pass
            else:
                self.repository.complete_outbox(event_id)
        else:
            self.repository.complete_outbox(event_id)
        return BudgetSettlement(
            reservation_id=reservation_id,
            status=status,
            reserved_amount=reservation.reserved_amount,
            actual_amount=actual_cost_micro_usd,
            overage_amount=overage,
        )

    async def _settle_redis(
        self,
        reservation: BudgetReservation,
        *,
        actual_cost_micro_usd: int,
        provider_request_id: str | None,
    ) -> None:
        keys = [item.counter_key for item in reservation.applied_limits]
        keys.extend(
            [
                self._reservation_key(reservation.reservation_id),
                self._expiry_key,
                self._stream_key,
                self._ready_key,
            ]
        )
        result = await self.redis.eval(
            self.SETTLE_SCRIPT,
            len(keys),
            *keys,
            len(reservation.applied_limits),
            reservation.reserved_amount,
            actual_cost_micro_usd,
            reservation.reservation_id,
            provider_request_id or "",
        )
        decoded = self._decode(result)
        if decoded[0] not in {"OK", "ALREADY"}:
            raise RuntimeError(f"Redis budget settlement failed: {decoded}")

    async def release(self, reservation_id: str, *, reason: str) -> BudgetReservation:
        await self.ensure_ready()
        reservation = self.repository.get(reservation_id)
        if reservation is None:
            raise KeyError(reservation_id)
        if reservation.status == BudgetReservationStatus.RELEASED:
            return reservation
        decoded = await self._release_redis(reservation, reason=reason)
        if decoded[0] == "UNSAFE":
            return await self.mark_reconciling(reservation_id, reason=reason)
        if decoded[0] not in {"OK", "ALREADY"}:
            raise RuntimeError(f"Redis budget release failed: {decoded}")
        return self.repository.transition(
            reservation_id,
            status=BudgetReservationStatus.RELEASED,
            event_type=BudgetEventType.RELEASE,
            settlement_basis=reason,
            delta_reserved=-reservation.reserved_amount,
        )

    async def _release_redis(
        self, reservation: BudgetReservation, *, reason: str
    ) -> list[str]:
        keys = [item.counter_key for item in reservation.applied_limits]
        keys.extend(
            [
                self._reservation_key(reservation.reservation_id),
                self._expiry_key,
                self._stream_key,
                self._ready_key,
            ]
        )
        result = await self.redis.eval(
            self.RELEASE_SCRIPT,
            len(keys),
            *keys,
            len(reservation.applied_limits),
            reservation.reserved_amount,
            reservation.reservation_id,
            reason,
        )
        return self._decode(result)

    async def mark_reconciling(
        self, reservation_id: str, *, reason: str
    ) -> BudgetReservation:
        await self.ensure_ready()
        await self.redis.hset(
            self._reservation_key(reservation_id),
            mapping={"status": BudgetReservationStatus.RECONCILING.value, "reason": reason},
        )
        return self.repository.transition(
            reservation_id,
            status=BudgetReservationStatus.RECONCILING,
            event_type=BudgetEventType.RECONCILE,
            settlement_basis=reason,
        )

    async def snapshot(
        self, context: BudgetContext, *, occurred_at: datetime | None = None
    ) -> list[BudgetCounterSnapshot]:
        await self.ensure_ready()
        if self.limit_provider is None:
            raise RuntimeError("Budget snapshot requires a BudgetLimitProvider")
        instant = occurred_at or utc_now()
        request = BudgetReservationRequest(
            context=context,
            idempotency_key="snapshot",
            estimated_cost_micro_usd=0,
            pricing_version="snapshot",
            limits=self.limit_provider.limits_for(context, occurred_at=instant),
            occurred_at=instant,
            expires_at=instant + timedelta(seconds=1),
        )
        applied = _applied_limits(request, key_builder=self._counter_key)
        pipeline = self.redis.pipeline(transaction=False)
        for item in applied:
            pipeline.hmget(item.counter_key, "reserved", "committed")
        values = await pipeline.execute()
        snapshots = []
        for item, (reserved, committed) in zip(applied, values, strict=True):
            snapshots.append(
                BudgetCounterSnapshot(
                    applied_limit=item,
                    reserved=int(reserved or 0),
                    committed=int(committed or 0),
                )
            )
        return snapshots

    async def replay_outbox(self, *, limit: int = 100) -> int:
        await self.ensure_ready()
        rows = self.repository.list_pending_outbox(utc_now(), limit=limit)
        completed = 0
        for row in rows:
            event_id = str(row["event_id"])
            try:
                payload = row["payload_json"]
                reservation = self.repository.get(str(row["reservation_id"]))
                if reservation is None:
                    raise KeyError(row["reservation_id"])
                if payload["operation"] == "SETTLE":
                    await self._settle_redis(
                        reservation,
                        actual_cost_micro_usd=int(payload["actual_cost_micro_usd"]),
                        provider_request_id=payload.get("provider_request_id"),
                    )
                self.repository.complete_outbox(event_id)
                completed += 1
            except Exception as exc:
                self.repository.fail_outbox(
                    event_id,
                    f"{type(exc).__name__}: {exc}",
                    available_at=utc_now() + timedelta(seconds=30),
                )
        return completed

    async def release_orphaned_reservations(self, *, before: datetime) -> int:
        """Release expired Redis-only reservations left by a crash before the database write."""
        await self.ensure_ready()
        reservation_ids = await self.redis.zrangebyscore(
            self._expiry_key,
            "-inf",
            int(before.timestamp() * 1000),
        )
        released = 0
        for raw_id in reservation_ids:
            reservation_id = self._decode(raw_id)[0]
            if self.repository.get(reservation_id) is not None:
                continue
            values = await self.redis.hgetall(self._reservation_key(reservation_id))
            decoded_values = {
                self._decode(key)[0]: self._decode(value)[0]
                for key, value in values.items()
            }
            if decoded_values.get("status") != BudgetReservationStatus.RESERVED.value:
                continue
            request_json = decoded_values.get("request")
            applied_json = decoded_values.get("applied_limits")
            if not request_json or not applied_json:
                continue
            request = BudgetReservationRequest.model_validate_json(request_json)
            reservation = BudgetReservation(
                reservation_id=reservation_id,
                request=request,
                applied_limits=deserialize_applied_limits(applied_json),
                reserved_amount=int(decoded_values["reserved_amount"]),
            )
            result = await self._release_redis(
                reservation,
                reason="ORPHANED_REDIS_RESERVATION_EXPIRED",
            )
            if result[0] in {"OK", "ALREADY"}:
                released += 1
        return released

    @staticmethod
    def _decode(result: Any) -> list[str]:
        values = result if isinstance(result, (list, tuple)) else [result]
        return [item.decode("utf-8") if isinstance(item, bytes) else str(item) for item in values]


class ProviderUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    actual_cost_micro_usd: int | None = None
    provider_request_id: str | None = None


class ProviderUsageReconciler(Protocol):
    async def query_usage(self, provider_request_id: str) -> ProviderUsage | None: ...
