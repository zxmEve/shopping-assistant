from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import timedelta

from procure_pilot.api.app import get_engine
from procure_pilot.domain.budget import BudgetReservationStatus
from procure_pilot.domain.models import utc_now
from procure_pilot.persistence.budget_repository import BudgetRepositoryPort
from procure_pilot.services.budget import (
    BudgetService,
    ProviderUsageReconciler,
    RedisBudgetService,
)


class BudgetReconciliationWorker:
    """Repair expired reservations without releasing possibly billed requests."""

    def __init__(
        self,
        *,
        budget_service: BudgetService,
        repository: BudgetRepositoryPort,
        usage_reconcilers: Mapping[str, ProviderUsageReconciler] | None = None,
        unknown_usage_settlement_hours: int = 72,
    ) -> None:
        self.budget_service = budget_service
        self.repository = repository
        self.usage_reconcilers = dict(usage_reconcilers or {})
        self.unknown_usage_settlement_hours = unknown_usage_settlement_hours

    async def run_once(self) -> dict[str, int]:
        now = utc_now()
        stats = {
            "released": 0,
            "orphan_released": 0,
            "settled": 0,
            "reconciling": 0,
            "outbox": 0,
        }
        if isinstance(self.budget_service, RedisBudgetService):
            stats["outbox"] = await self.budget_service.replay_outbox()
            stats["orphan_released"] = (
                await self.budget_service.release_orphaned_reservations(before=now)
            )
        for reservation in self.repository.list_due(now):
            if reservation.status == BudgetReservationStatus.RESERVED:
                await self.budget_service.release(
                    reservation.reservation_id,
                    reason="UNDISPATCHED_RESERVATION_EXPIRED",
                )
                stats["released"] += 1
                continue
            provider_request_id = reservation.provider_request_id
            reconciler = self.usage_reconcilers.get(
                f"{reservation.request.context.provider_kind.value}:"
                f"{reservation.request.context.provider_id}"
            )
            usage = (
                await reconciler.query_usage(provider_request_id)
                if reconciler is not None and provider_request_id
                else None
            )
            if usage is not None and usage.actual_cost_micro_usd is not None:
                await self.budget_service.settle(
                    reservation.reservation_id,
                    actual_cost_micro_usd=usage.actual_cost_micro_usd,
                    provider_request_id=usage.provider_request_id or provider_request_id,
                    settlement_basis="AUTOMATIC_PROVIDER_RECONCILIATION",
                )
                stats["settled"] += 1
                continue
            conservative_deadline = reservation.request.expires_at + timedelta(
                hours=self.unknown_usage_settlement_hours
            )
            if now >= conservative_deadline:
                await self.budget_service.settle(
                    reservation.reservation_id,
                    actual_cost_micro_usd=reservation.reserved_amount,
                    provider_request_id=provider_request_id,
                    settlement_basis="RESERVED_ESTIMATE_AFTER_RECONCILIATION_TIMEOUT",
                )
                stats["settled"] += 1
            elif reservation.status != BudgetReservationStatus.RECONCILING:
                await self.budget_service.mark_reconciling(
                    reservation.reservation_id,
                    reason="DISPATCHED_USAGE_UNKNOWN",
                )
                stats["reconciling"] += 1
        return stats


async def run_forever(interval_seconds: int = 30) -> None:
    engine = get_engine()
    repository = getattr(engine.budget_service, "repository", None)
    if repository is None:
        raise RuntimeError("Configured BudgetService does not expose a durable repository")
    worker = BudgetReconciliationWorker(
        budget_service=engine.budget_service,
        repository=repository,
    )
    while True:
        await worker.run_once()
        await asyncio.sleep(interval_seconds)


def main() -> None:
    asyncio.run(run_forever())
