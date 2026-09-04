import asyncio
from decimal import Decimal

import pytest

from procure_pilot.domain.models import RunState
from procure_pilot.evals.runner import COMPLETE_REQUEST, make_engine
from procure_pilot.fixtures.catalog import build_catalog
from procure_pilot.harness.runtime import AgentHarness


def test_fixture_shape_is_stable():
    offers = build_catalog()
    assert len(offers) == 450
    assert len({offer.product.product_id for offer in offers}) == 40
    assert len({offer.variant.variant_id for offer in offers}) == 120
    assert len({offer.provider for offer in offers}) == 3


@pytest.mark.asyncio
async def test_active_clarification_then_recommendation():
    engine = make_engine()
    first = await engine.start(
        tenant_id="demo_cn",
        user_id="employee_001",
        message="给印尼研发团队采购20台开发笔记本，单台预算8000元，两周内到货",
    )
    assert first.run.state == RunState.WAITING_USER
    assert len(first.run.trace[-1].detail["questions"]) <= 3

    second = await engine.continue_run(
        run_id=first.run.run_id,
        tenant_id="demo_cn",
        user_id="employee_001",
        message="预算含税，送雅加达，最低32GB和1TB",
    )
    assert second.run.state == RunState.WAITING_CONFIRMATION
    assert second.run.recommendation.best_overall.offer.variant.ram_gb >= 32
    assert second.run.recommendation.best_overall.offer.variant.storage_gb >= 1024
    assert second.run.action_token


@pytest.mark.asyncio
async def test_clarification_prioritizes_high_information_fields_and_defaults_to_few_questions():
    engine = make_engine()

    broad = await engine.start(
        tenant_id="demo_cn",
        user_id="employee_001",
        message="帮我采购电脑",
    )
    decision = broad.run.trace[-1].detail
    assert decision["missing_fields"][:3] == ["unit_budget", "quantity", "minimum_ram_gb"]
    assert len(decision["questions"]) == 2
    assert "信息价值排序" in decision["rationale"]

    one_missing = await engine.start(
        tenant_id="demo_cn",
        user_id="employee_001",
        message="采购20台开发笔记本，单台8000元含税，送雅加达，两周内，1TB硬盘",
    )
    assert one_missing.run.state == RunState.WAITING_USER
    assert one_missing.run.trace[-1].detail["missing_fields"] == ["minimum_ram_gb"]
    assert len(one_missing.run.trace[-1].detail["questions"]) == 1


@pytest.mark.asyncio
async def test_authorized_company_standard_creates_visible_assumptions_and_stops_asking():
    engine = make_engine()
    first = await engine.start(
        tenant_id="demo_cn",
        user_id="employee_001",
        message="采购20台开发笔记本，单台8000元含税，送雅加达，两周内，1TB硬盘",
    )

    completed = await engine.continue_run(
        run_id=first.run.run_id,
        tenant_id="demo_cn",
        user_id="employee_001",
        message="最低内存按公司标准决定",
    )

    assert completed.run.state == RunState.WAITING_CONFIRMATION
    assert completed.run.requirement.minimum_ram_gb == 32
    assert any("用户授权按公司标准" in item for item in completed.run.requirement.assumptions)


@pytest.mark.asyncio
async def test_clarification_stops_after_three_rounds_when_required_facts_remain_unknown():
    engine = make_engine()
    response = await engine.start(
        tenant_id="demo_cn",
        user_id="employee_001",
        message="帮我采购电脑",
    )
    for answer in ("还没决定", "暂时不知道", "无法提供"):
        response = await engine.continue_run(
            run_id=response.run.run_id,
            tenant_id="demo_cn",
            user_id="employee_001",
            message=answer,
        )

    assert response.run.state == RunState.FAILED
    assert response.run.clarification_rounds == 3
    assert "达到最大追问轮数" in response.assistant_message
    assert response.run.trace[-1].name == "clarification.stopped"


@pytest.mark.asyncio
async def test_confirm_is_idempotent():
    engine = make_engine()
    response = await engine.start(
        tenant_id="demo_cn",
        user_id="employee_001",
        message=COMPLETE_REQUEST,
    )
    first = await engine.confirm(
        run_id=response.run.run_id,
        tenant_id="demo_cn",
        user_id="employee_001",
        action_token=response.run.action_token,
    )
    second = await engine.confirm(
        run_id=response.run.run_id,
        tenant_id="demo_cn",
        user_id="employee_001",
        action_token=response.run.action_token,
    )
    assert first.run.draft.draft_id == second.run.draft.draft_id
    assert second.run.draft.status == "DRAFT_NOT_SUBMITTED"


def test_write_requires_confirmation():
    with pytest.raises(PermissionError):
        AgentHarness(secret="test").assert_tool_allowed(
            tool_name="create_requisition_draft",
            roles=("EMPLOYEE",),
            confirmed=False,
        )


def test_tool_registry_is_default_deny_and_role_scoped():
    harness = AgentHarness(secret="test")
    employee_tools = {tool["name"] for tool in harness.available_tools(("EMPLOYEE",))}

    assert "create_requisition_draft" in employee_tools
    assert "place_order" not in employee_tools
    assert "approve_requisition" not in employee_tools
    with pytest.raises(PermissionError):
        harness.assert_tool_allowed(
            tool_name="place_order",
            roles=("EMPLOYEE",),
            confirmed=True,
        )
    with pytest.raises(PermissionError):
        harness.assert_tool_allowed(
            tool_name="search_catalog",
            roles=("UNRELATED_ROLE",),
            confirmed=False,
        )


@pytest.mark.asyncio
async def test_money_is_decimal_and_exact():
    engine = make_engine()
    response = await engine.start(
        tenant_id="demo_cn",
        user_id="employee_001",
        message=COMPLETE_REQUEST,
    )
    candidate = response.run.recommendation.best_overall
    assert isinstance(candidate.cost.unconditional_total.amount, Decimal)
    assert (
        candidate.cost.unconditional_total.amount
        == candidate.offer.unit_price.amount * 20 + candidate.offer.shipping.amount
    )


@pytest.mark.asyncio
async def test_tenant_isolation():
    engine = make_engine()
    response = await engine.start(
        tenant_id="demo_cn",
        user_id="employee_001",
        message=COMPLETE_REQUEST,
    )
    with pytest.raises(PermissionError):
        engine.get(response.run.run_id, tenant_id="demo_id", user_id="employee_101")


@pytest.mark.asyncio
async def test_quote_change_requires_a_new_confirmation_and_rejects_old_token():
    engine = make_engine()
    response = await engine.start(
        tenant_id="demo_cn",
        user_id="employee_001",
        message=COMPLETE_REQUEST,
        scenario="quote_price_changed",
    )
    original_token = response.run.action_token
    original_price = response.run.recommendation.best_overall.offer.unit_price.amount

    changed = await engine.confirm(
        run_id=response.run.run_id,
        tenant_id="demo_cn",
        user_id="employee_001",
        action_token=original_token,
    )

    assert changed.run.draft is None
    assert changed.run.state == RunState.WAITING_CONFIRMATION
    assert changed.run.action_token != original_token
    assert {change.field for change in changed.run.revalidation_changes} >= {
        "unit_price",
        "content_hash",
    }
    assert (
        changed.run.recommendation.best_overall.offer.unit_price.amount
        == original_price + Decimal("350.00")
    )

    with pytest.raises(PermissionError):
        await engine.confirm(
            run_id=response.run.run_id,
            tenant_id="demo_cn",
            user_id="employee_001",
            action_token=original_token,
        )

    confirmed = await engine.confirm(
        run_id=response.run.run_id,
        tenant_id="demo_cn",
        user_id="employee_001",
        action_token=changed.run.action_token,
    )
    assert confirmed.run.draft is not None
    assert confirmed.run.draft.candidate.offer.unit_price.amount == original_price + Decimal(
        "350.00"
    )


@pytest.mark.asyncio
async def test_action_token_is_bound_to_action_and_requirement_version():
    engine = make_engine()
    response = await engine.start(
        tenant_id="demo_cn",
        user_id="employee_001",
        message=COMPLETE_REQUEST,
    )

    with pytest.raises(PermissionError):
        engine.harness.verify_action_token(
            response.run.action_token,
            response.run,
            action="PLACE_ORDER",
        )

    changed_run = engine.repository.get(response.run.run_id)
    changed_run.requirement.version += 1
    engine.repository.save(changed_run)
    with pytest.raises(PermissionError):
        await engine.confirm(
            run_id=response.run.run_id,
            tenant_id="demo_cn",
            user_id="employee_001",
            action_token=response.run.action_token,
        )


@pytest.mark.asyncio
async def test_concurrent_confirmations_have_one_draft_effect():
    engine = make_engine()
    response = await engine.start(
        tenant_id="demo_cn",
        user_id="employee_001",
        message=COMPLETE_REQUEST,
    )

    results = await asyncio.gather(
        *[
            engine.confirm(
                run_id=response.run.run_id,
                tenant_id="demo_cn",
                user_id="employee_001",
                action_token=response.run.action_token,
            )
            for _ in range(10)
        ]
    )

    assert len({result.run.draft.draft_id for result in results}) == 1
    final_run = engine.get(response.run.run_id, tenant_id="demo_cn", user_id="employee_001")
    assert sum(event.name == "create_requisition_draft" for event in final_run.trace) == 1
