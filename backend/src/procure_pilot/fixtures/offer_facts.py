"""Local read-only supplier API simulator. Replace this adapter for live supplier data."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from procure_pilot.domain.sourcing import FactStatus, OfferFact
from procure_pilot.fixtures.catalog import FixtureCatalogAdapter, ProviderError

FACT_TOOLS = {
    "query_inventory": "INVENTORY",
    "estimate_delivery": "DELIVERY",
    "query_warranty": "WARRANTY",
}


def fact_scope(run, kind):
    scope = {"scenario": run.scenario, "quantity": run.requirement.quantity}
    if kind != "INVENTORY":
        scope["delivery_city"] = run.requirement.delivery_city
    return scope


async def query_offer_fact(catalog: FixtureCatalogAdapter, *, run, provider, offer_id, kind):
    now = datetime.now(UTC)
    status = FactStatus.KNOWN
    value = None
    try:
        current = await catalog.get_offer(provider, offer_id, scenario=run.scenario)
        value = {
            "INVENTORY": {"stock": current.stock},
            "DELIVERY": {"days": current.delivery_days, "city": run.requirement.delivery_city},
            "WARRANTY": {
                "months": current.variant.warranty_months,
                "coverage_city": run.requirement.delivery_city,
            },
        }[kind]
        if run.scenario == f"{kind.lower()}_unknown":
            status, value = FactStatus.UNKNOWN, None
        if run.scenario == f"{kind.lower()}_unavailable":
            status, value = FactStatus.UNAVAILABLE, None
    except ProviderError:
        status = FactStatus.UNAVAILABLE
    ref = f"fixture-api://{provider}/{offer_id}/{kind.lower()}"
    fact_id = f"fact_{uuid4().hex[:16]}"
    return OfferFact(
        fact_id=fact_id,
        provider=provider,
        offer_id=offer_id,
        kind=kind,
        requirement_version=run.requirement.version,
        scope=fact_scope(run, kind),
        status=status,
        value=value,
        evidence_refs=[fact_id] if value is not None else [],
        source_ref=ref,
        fetched_at=now,
        expires_at=now + timedelta(minutes=5),
    )
