from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel


class ModelCostEstimate(BaseModel):
    provider: str
    model: str
    price_version: str
    input_usd_per_million: Decimal
    output_usd_per_million: Decimal
    input_tokens: int
    output_tokens: int
    estimated_usd: Decimal
    basis: str = "conservative cache-miss token estimate; excludes provider credits"


def estimate_model_cost(
    *,
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    at: datetime | None = None,
) -> ModelCostEstimate | None:
    """Return a versioned estimate; unknown prices stay unknown instead of becoming zero."""
    instant = (at or datetime.now(UTC)).astimezone(UTC)
    if provider != "deepseek" or model != "deepseek-v4-flash":
        return None
    weekday = instant.weekday() < 5
    peak_hour = 1 <= instant.hour < 4 or 6 <= instant.hour < 10
    if weekday and peak_hour:
        input_rate = Decimal("0.44")
        output_rate = Decimal("1.32")
        price_version = "deepseek-v4-flash-2026-08-16-peak"
    else:
        input_rate = Decimal("0.22")
        output_rate = Decimal("0.66")
        price_version = "deepseek-v4-flash-2026-08-16-off-peak"
    estimated = (
        Decimal(input_tokens) * input_rate + Decimal(output_tokens) * output_rate
    ) / Decimal(1_000_000)
    return ModelCostEstimate(
        provider=provider,
        model=model,
        price_version=price_version,
        input_usd_per_million=input_rate,
        output_usd_per_million=output_rate,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_usd=estimated.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP),
    )
