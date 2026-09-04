from decimal import Decimal

import pytest

from procure_pilot.domain.models import (
    BundleType,
    CatalogSearchResult,
    MatchDecisionType,
    ProductCondition,
)
from procure_pilot.fixtures.catalog import (
    PROVIDERS,
    FixtureCatalogAdapter,
    ProviderError,
    build_hard_case_raw_offers,
    build_raw_catalog,
)


def test_provider_raw_offers_use_inconsistent_expressions_before_canonicalization():
    raws = build_raw_catalog(include_hard_cases=False)
    first_variant = raws[:4]

    assert {raw.ram for raw in first_variant} >= {"16 GB", "16G", "16384MB"}
    assert {raw.storage for raw in first_variant} >= {"512GB", "512G"}
    assert {raw.condition for raw in first_variant} >= {"全新", "新品", "Brand New"}
    assert len({raw.model for raw in first_variant}) >= 3


def test_equivalent_provider_formats_map_to_the_same_product_and_variant():
    adapter = FixtureCatalogAdapter()
    raws = build_raw_catalog(include_hard_cases=False)
    direct_first_variant = [raw for raw in raws[:4] if "Reseller" not in raw.seller]
    results = [adapter.pipeline.canonicalize(raw) for raw in direct_first_variant]

    assert all(result.offer is not None for result in results)
    assert {result.offer.product.product_id for result in results if result.offer} == {
        "product_001"
    }
    assert {result.offer.variant.variant_id for result in results if result.offer} == {
        "variant_001"
    }
    assert all(result.assessment.match_decision.confidence >= 0.90 for result in results)


def test_hard_cases_separate_variants_and_veto_conflicts():
    adapter = FixtureCatalogAdapter()
    results = {
        raw.raw_offer_id: adapter.pipeline.canonicalize(raw) for raw in build_hard_case_raw_offers()
    }
    match_ids = {
        case_id: result.offer.variant.variant_id
        for case_id, result in results.items()
        if result.offer is not None
    }

    assert len(set(match_ids.values())) == 6
    assert results["hard_refurbished"].offer.variant.condition == ProductCondition.REFURBISHED
    assert results["hard_used"].offer.variant.condition == ProductCondition.USED
    assert results["hard_dock_bundle"].offer.variant.bundle == BundleType.DOCK_BUNDLE
    assert (
        results["hard_suffix_conflict"].assessment.match_decision.decision
        == MatchDecisionType.REJECTED
    )
    assert results["hard_suffix_conflict"].assessment.match_decision.conflict_fields == ["model"]
    assert (
        results["hard_provider_title_conflict"].assessment.match_decision.decision
        == MatchDecisionType.REJECTED
    )
    assert (
        "ram_gb"
        in results["hard_provider_title_conflict"].assessment.match_decision.conflict_fields
    )
    assert (
        results["hard_missing_storage"].assessment.match_decision.decision
        == MatchDecisionType.REVIEW_REQUIRED
    )
    assert results["hard_missing_storage"].offer is None


def test_server_guard_rejects_an_upstream_wrong_variant_proposal():
    adapter = FixtureCatalogAdapter()
    raw = build_hard_case_raw_offers()[0]
    wrong_variant = adapter.variants[0]

    result = adapter.pipeline.canonicalize(
        raw,
        proposed_product=adapter.products[0],
        proposed_variant=wrong_variant,
    )

    assert result.offer is None
    assert result.assessment.match_decision.decision == MatchDecisionType.REJECTED
    assert {"ram_gb", "storage_gb"}.issubset(result.assessment.match_decision.conflict_fields)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", PROVIDERS)
async def test_fixture_adapter_contract_returns_only_verified_comparable_offers(provider: str):
    adapter = FixtureCatalogAdapter()

    result = await adapter.search(
        provider,
        minimum_ram_gb=32,
        minimum_storage_gb=1024,
        quantity=20,
    )

    assert isinstance(result, CatalogSearchResult)
    assert result.provider == provider
    assert result.retrieved_count >= len(result.offers)
    assert all(
        offer.provider == provider
        and offer.match_decision.decision == MatchDecisionType.MATCH
        and offer.match_confidence >= 0.90
        and offer.variant.ram_gb >= 32
        and offer.variant.storage_gb >= 1024
        and offer.evidence.content_hash
        and offer.evidence.expires_at > offer.evidence.fetched_at
        for offer in result.offers
    )
    assert all(not assessment.comparable for assessment in result.assessments)


@pytest.mark.asyncio
async def test_fixture_adapter_exposes_unverified_reasons_but_excludes_them_from_offers():
    adapter = FixtureCatalogAdapter()

    result = await adapter.search(
        "marketplace_alpha",
        minimum_ram_gb=32,
        minimum_storage_gb=1024,
        quantity=20,
    )

    issue_ids = {assessment.raw_offer_id for assessment in result.assessments}
    offer_ids = {offer.offer_id for offer in result.offers}
    assert {
        "hard_suffix_conflict",
        "hard_provider_title_conflict",
        "hard_missing_storage",
    }.issubset(issue_ids)
    assert issue_ids.isdisjoint(offer_ids)
    assert all(assessment.match_decision.reasons for assessment in result.assessments)


@pytest.mark.asyncio
async def test_fixture_adapter_rejects_unknown_provider_and_revalidates_raw_offer():
    adapter = FixtureCatalogAdapter()
    with pytest.raises(ProviderError):
        await adapter.search(
            "unknown",
            minimum_ram_gb=32,
            minimum_storage_gb=1024,
            quantity=20,
        )

    result = await adapter.search(
        "internal_catalog",
        minimum_ram_gb=32,
        minimum_storage_gb=1024,
        quantity=20,
    )
    offer = result.offers[0]
    refreshed = await adapter.get_offer(offer.provider, offer.offer_id)
    assert refreshed.unit_price.amount == offer.unit_price.amount
    assert isinstance(refreshed.unit_price.amount, Decimal)
