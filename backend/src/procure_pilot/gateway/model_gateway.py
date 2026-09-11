from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeVar
from uuid import uuid4

from procure_pilot.domain.budget import (
    BudgetContext,
    BudgetReservationStatus,
    InvocationKind,
    ProviderKind,
)
from procure_pilot.services.budget import (
    BudgetLimitProvider,
    BudgetService,
    build_reservation_request,
)
from procure_pilot.services.model_pricing import estimate_model_cost

T = TypeVar("T")


class ModelGateway:
    """Budget every physical model request before it reaches the model provider."""

    def __init__(
        self,
        *,
        budget_service: BudgetService,
        budget_limit_provider: BudgetLimitProvider,
        max_reservation_micro_usd: int,
        reservation_ttl_seconds: int = 300,
    ) -> None:
        self.budget_service = budget_service
        self.budget_limit_provider = budget_limit_provider
        self.max_reservation_micro_usd = max_reservation_micro_usd
        self.reservation_ttl_seconds = reservation_ttl_seconds

    async def invoke(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        agent_execution_id: str | None,
        provider: str,
        model: str,
        request_index: int,
        request_fingerprint: str,
        operation: Callable[[], Awaitable[T]],
        usage_reader: Callable[[T], tuple[int, int, str | None]],
    ) -> tuple[T, int]:
        context = BudgetContext(
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            agent_execution_id=agent_execution_id,
            invocation_kind=InvocationKind.MODEL,
            provider_kind=ProviderKind.MODEL,
            provider_id=provider,
            model=model,
        )
        reservation = await self.budget_service.reserve(
            build_reservation_request(
                context=context,
                idempotency_key=(
                    f"model:{run_id}:{agent_execution_id or '-'}:{model}:"
                    f"{request_index}:{request_fingerprint}"
                ),
                estimated_cost_micro_usd=self.max_reservation_micro_usd,
                pricing_version=f"model-reservation:{provider}:{model}:v1",
                limit_provider=self.budget_limit_provider,
                ttl_seconds=self.reservation_ttl_seconds,
                metadata_json={"request_index": request_index},
            )
        )
        if reservation.status != BudgetReservationStatus.RESERVED:
            raise RuntimeError(
                f"Model budget reservation cannot be dispatched from {reservation.status.value}"
            )
        provider_invocation_id = f"pi_{uuid4().hex[:20]}"
        await self.budget_service.mark_dispatched(
            reservation.reservation_id,
            provider_invocation_id=provider_invocation_id,
        )
        try:
            response = await operation()
        except Exception as exc:
            await self.budget_service.mark_reconciling(
                reservation.reservation_id,
                reason=f"{type(exc).__name__}: model response unknown",
            )
            raise
        input_tokens, output_tokens, provider_request_id = usage_reader(response)
        estimate = estimate_model_cost(
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        actual_cost = (
            int(estimate.estimated_usd * 1_000_000)
            if estimate is not None
            else reservation.reserved_amount
        )
        await self.budget_service.settle(
            reservation.reservation_id,
            actual_cost_micro_usd=actual_cost,
            provider_request_id=provider_request_id,
            settlement_basis=("PROVIDER_TOKEN_USAGE" if estimate is not None else "RESERVED_ESTIMATE"),
        )
        return response, actual_cost
