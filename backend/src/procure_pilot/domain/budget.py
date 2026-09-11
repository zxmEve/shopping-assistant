from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator

from procure_pilot.domain.models import utc_now


class BudgetScopeType(StrEnum):
    PLATFORM = "PLATFORM"
    TENANT = "TENANT"
    USER = "USER"
    PROVIDER = "PROVIDER"


class BudgetPeriodType(StrEnum):
    DAILY = "DAILY"
    MONTHLY = "MONTHLY"


class BudgetMetric(StrEnum):
    AI_COST_MICRO_USD = "AI_COST_MICRO_USD"
    PROVIDER_REQUEST_COUNT = "PROVIDER_REQUEST_COUNT"


class BudgetReservationStatus(StrEnum):
    # 已预留，但尚未准备调用外部服务。
    RESERVED = "RESERVED"
    # 数据库已经持久化“即将发送”的意图。
    DISPATCH_INTENT = "DISPATCH_INTENT"
    # 请求已经或很可能已经发送给外部服务。
    DISPATCHED = "DISPATCHED"
    # 已用真实或保守估算的费用完成结算。
    SETTLED = "SETTLED"
    # 确认没有产生外部费用，预留已经退回。
    RELEASED = "RELEASED"
    # 调用结果未知，等待自动对账。
    RECONCILING = "RECONCILING"
    # 实际费用超过了预留金额。
    OVERAGE = "OVERAGE"


class BudgetEventType(StrEnum):
    RESERVE = "RESERVE"
    DISPATCH_INTENT = "DISPATCH_INTENT"
    DISPATCH = "DISPATCH"
    SETTLE = "SETTLE"
    RELEASE = "RELEASE"
    RECONCILE = "RECONCILE"
    ADJUST = "ADJUST"
    OVERAGE = "OVERAGE"


class InvocationKind(StrEnum):
    MODEL = "MODEL"
    TOOL = "TOOL"


class ProviderKind(StrEnum):
    MODEL = "MODEL"
    CATALOG = "CATALOG"
    ENTERPRISE = "ENTERPRISE"
    LOCAL = "LOCAL"
    OTHER = "OTHER"


class BudgetLimit(BaseModel):
    policy_id: str
    policy_version: int = Field(default=1, gt=0)
    scope_type: BudgetScopeType
    scope_key: str = Field(min_length=1, max_length=256)
    period_type: BudgetPeriodType
    period_timezone: str = "UTC"
    metric: BudgetMetric = BudgetMetric.AI_COST_MICRO_USD
    hard_limit: int = Field(ge=0)
    soft_limit: int | None = Field(default=None, ge=0)

    @field_validator("period_timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    def period_bounds(self, occurred_at: datetime) -> tuple[datetime, datetime]:
        instant = occurred_at if occurred_at.tzinfo is not None else occurred_at.replace(tzinfo=UTC)
        local = instant.astimezone(ZoneInfo(self.period_timezone))
        if self.period_type == BudgetPeriodType.DAILY:
            start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
            end_local = start_local + timedelta(days=1)
        else:
            start_local = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            if start_local.month == 12:
                end_local = start_local.replace(year=start_local.year + 1, month=1)
            else:
                end_local = start_local.replace(month=start_local.month + 1)
        return start_local.astimezone(UTC), end_local.astimezone(UTC)

    def counter_identity(self, occurred_at: datetime) -> str:
        period_start, _ = self.period_bounds(occurred_at)
        raw = ":".join(
            (
                self.scope_type.value,
                self.scope_key,
                self.period_type.value,
                self.metric.value,
                period_start.isoformat(),
            )
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class BudgetContext(BaseModel):
    tenant_id: str
    user_id: str
    run_id: str
    agent_execution_id: str | None = None
    tool_call_id: str | None = None
    invocation_kind: InvocationKind
    provider_kind: ProviderKind
    provider_id: str
    model: str | None = None


class BudgetReservationRequest(BaseModel):
    context: BudgetContext
    idempotency_key: str = Field(min_length=1, max_length=256)
    estimated_cost_micro_usd: int = Field(ge=0)
    pricing_version: str
    limits: tuple[BudgetLimit, ...]
    occurred_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime
    metadata_json: dict[str, Any] = Field(default_factory=dict)

    @field_validator("expires_at")
    @classmethod
    def aware_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Budget reservation expiry must include a timezone")
        return value


class AppliedBudgetLimit(BaseModel):
    policy_id: str
    policy_version: int
    scope_type: BudgetScopeType
    scope_key: str
    period_type: BudgetPeriodType
    period_start: datetime
    period_end: datetime
    metric: BudgetMetric
    hard_limit: int
    counter_key: str


class BudgetReservation(BaseModel):
    reservation_id: str = Field(default_factory=lambda: f"br_{uuid4().hex[:20]}")
    request: BudgetReservationRequest
    applied_limits: tuple[AppliedBudgetLimit, ...]
    reserved_amount: int = Field(ge=0)
    actual_amount: int | None = Field(default=None, ge=0)
    status: BudgetReservationStatus = BudgetReservationStatus.RESERVED
    provider_invocation_id: str | None = None
    provider_request_id: str | None = None
    settlement_basis: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class BudgetSettlement(BaseModel):
    reservation_id: str
    status: BudgetReservationStatus
    reserved_amount: int
    actual_amount: int
    overage_amount: int = Field(default=0, ge=0)


class BudgetLedgerEntry(BaseModel):
    entry_id: str = Field(default_factory=lambda: f"ble_{uuid4().hex[:20]}")
    event_id: str
    reservation_id: str
    event_type: BudgetEventType
    scope_type: BudgetScopeType
    scope_key: str
    metric: BudgetMetric
    period_start: datetime
    period_end: datetime
    delta_reserved: int = 0
    delta_committed: int = 0
    currency: str = "USD"
    policy_id: str
    policy_version: int
    occurred_at: datetime = Field(default_factory=utc_now)
    metadata_json: dict[str, Any] = Field(default_factory=dict)


class BudgetCounterSnapshot(BaseModel):
    applied_limit: AppliedBudgetLimit
    reserved: int = Field(ge=0)
    committed: int = Field(ge=0)

    @property
    def available(self) -> int:
        return max(0, self.applied_limit.hard_limit - self.reserved - self.committed)


def serialize_applied_limits(limits: tuple[AppliedBudgetLimit, ...]) -> str:
    return json.dumps(
        [item.model_dump(mode="json") for item in limits],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def deserialize_applied_limits(value: str) -> tuple[AppliedBudgetLimit, ...]:
    return tuple(AppliedBudgetLimit.model_validate(item) for item in json.loads(value))
