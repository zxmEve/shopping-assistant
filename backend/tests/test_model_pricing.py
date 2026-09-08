from datetime import UTC, datetime
from decimal import Decimal

from procure_pilot.services.model_pricing import estimate_model_cost


def test_deepseek_peak_cost_uses_versioned_cache_miss_rates():
    estimate = estimate_model_cost(
        provider="deepseek",
        model="deepseek-v4-flash",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        at=datetime(2026, 9, 4, 9, tzinfo=UTC),
    )

    assert estimate is not None
    assert estimate.price_version.endswith("peak")
    assert estimate.estimated_usd == Decimal("1.760000")


def test_deepseek_off_peak_cost_is_half_of_peak():
    estimate = estimate_model_cost(
        provider="deepseek",
        model="deepseek-v4-flash",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        at=datetime(2026, 9, 5, 9, tzinfo=UTC),
    )

    assert estimate is not None
    assert estimate.price_version.endswith("off-peak")
    assert estimate.estimated_usd == Decimal("0.880000")


def test_unknown_model_cost_is_not_reported_as_zero():
    assert (
        estimate_model_cost(
            provider="unknown",
            model="unknown",
            input_tokens=1,
            output_tokens=1,
        )
        is None
    )
