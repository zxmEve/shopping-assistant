from __future__ import annotations

import pytest
from agents.tool_context import ToolContext

from procure_pilot.agents.runtime import AgentPlan
from procure_pilot.agents.tools import (
    AgentCandidateProposal,
    AgentRuntimeContext,
    build_function_tools,
)
from procure_pilot.domain.models import Message, Money, ProcurementRequirement, ProcurementRun
from procure_pilot.evals.runner import make_engine
from procure_pilot.fixtures.catalog import FixtureCatalogAdapter
from procure_pilot.fixtures.enterprise import FixtureEnterpriseGateway
from procure_pilot.harness.runtime import AgentHarness, RunRepository
from procure_pilot.services.costing import calculate_landed_cost
from procure_pilot.services.engine import ProcurementEngine


def make_context() -> AgentRuntimeContext:
    run = ProcurementRun(
        tenant_id="demo_cn",
        user_id="employee_001",
        requirement=ProcurementRequirement(
            quantity=20,
            unit_budget=Money(amount="8000"),
            budget_tax_included=True,
            delivery_city="Jakarta",
            delivery_days=14,
            minimum_ram_gb=32,
            minimum_storage_gb=1024,
        ),
    )
    harness = AgentHarness(secret="test")
    harness.initialize_run(run)
    return AgentRuntimeContext(
        run=run,
        catalog=FixtureCatalogAdapter(),
        enterprise=FixtureEnterpriseGateway(),
        harness=harness,
        roles=("EMPLOYEE",),
    )


async def invoke(tool, context: AgentRuntimeContext, arguments: str):
    tool_context = ToolContext(
        context,
        tool_name=tool.name,
        tool_call_id=f"test-{tool.name}",
        tool_arguments=arguments,
    )
    return await tool.on_invoke_tool(tool_context, arguments)


def test_agent_tool_schemas_are_strict_and_do_not_expose_business_writes():
    tools = build_function_tools()

    assert "create_requisition_draft" not in tools
    assert "place_order" not in tools
    assert tools["search_catalog"].strict_json_schema is True
    assert tools["search_catalog"].params_json_schema["additionalProperties"] is False
    assert tools["search_catalog"].params_json_schema["properties"]["provider"]["enum"] == [
        "internal_catalog",
        "marketplace_alpha",
        "marketplace_beta",
    ]


@pytest.mark.asyncio
async def test_agent_tools_use_server_owned_requirement_and_evidence():
    context = make_context()
    tools = build_function_tools()

    budget = await invoke(tools["get_budget"], context, "{}")
    catalog = await invoke(
        tools["search_catalog"],
        context,
        '{"provider":"internal_catalog"}',
    )
    selected = catalog.offers[0]
    arguments = '{"provider":"internal_catalog","offer_id":"' + selected.offer_id + '"}'
    cost = await invoke(tools["calculate_landed_cost"], context, arguments)
    policy = await invoke(tools["evaluate_policy"], context, arguments)

    assert budget.available == "220000"
    assert catalog.verified_offer_count > 0
    assert cost.quantity == 20
    assert cost.breakdown.unconditional_total.amount > 0
    assert policy.evidence_refs
    assert context.run.execution_usage.tool_calls == 4
    assert context.run.execution_usage.agent_read_tool_calls == 4
    assert context.run.execution_usage.workflow_read_tool_calls == 0
    assert context.run.execution_usage.provider_calls == 1
    assert {event.name for event in context.run.trace} >= {
        "get_budget",
        "search_catalog",
        "calculate_landed_cost",
        "evaluate_policy",
    }


@pytest.mark.asyncio
async def test_agent_cannot_invent_an_offer_for_cost_or_policy_tools():
    context = make_context()
    tool = build_function_tools()["calculate_landed_cost"]

    result = await invoke(
        tool,
        context,
        '{"provider":"internal_catalog","offer_id":"invented-by-model"}',
    )

    assert result == ("Tool rejected the offer_id because it was not returned by search_catalog.")
    assert not any(event.name == "calculate_landed_cost" for event in context.run.trace)


@pytest.mark.asyncio
async def test_policy_and_budget_tools_enforce_single_use_beyond_prompt_instructions():
    context = make_context()
    tools = build_function_tools()
    await invoke(tools["get_budget"], context, "{}")
    repeated_budget = await invoke(tools["get_budget"], context, "{}")
    catalog = await invoke(
        tools["search_catalog"],
        context,
        '{"provider":"internal_catalog"}',
    )
    selected = catalog.offers[0]
    arguments = '{"provider":"internal_catalog","offer_id":"' + selected.offer_id + '"}'
    await invoke(tools["evaluate_policy"], context, arguments)
    repeated_policy = await invoke(tools["evaluate_policy"], context, arguments)

    assert "already completed" in repeated_budget
    assert "already completed" in repeated_policy
    assert context.run.execution_usage.tool_calls == 3
    assert sum(event.name == "get_budget" for event in context.run.trace) == 1
    assert sum(event.name == "evaluate_policy" for event in context.run.trace) == 1


@pytest.mark.asyncio
async def test_parallel_batch_guards_reject_duplicate_search_and_fourth_cost_call():
    context = make_context()
    tools = build_function_tools()
    catalog = await invoke(
        tools["search_catalog"],
        context,
        '{"provider":"internal_catalog"}',
    )
    repeated_search = await invoke(
        tools["search_catalog"],
        context,
        '{"provider":"internal_catalog"}',
    )
    assert len(catalog.offers) >= 4
    cost_results = []
    for offer in catalog.offers[:4]:
        cost_results.append(
            await invoke(
                tools["calculate_landed_cost"],
                context,
                '{"provider":"internal_catalog","offer_id":"' + offer.offer_id + '"}',
            )
        )

    assert "already requested" in repeated_search
    assert all(result.offer_id for result in cost_results[:3])
    assert "limit already reached" in cost_results[3]
    assert context.run.execution_usage.tool_calls == 4
    assert sum(event.name == "search_catalog" for event in context.run.trace) == 1
    assert sum(event.name == "calculate_landed_cost" for event in context.run.trace) == 3


class RecordingPlanner:
    def __init__(self) -> None:
        self.turns: list[list[str]] = []

    async def plan(
        self,
        messages: list[Message],
        *,
        context: AgentRuntimeContext | None = None,
    ) -> AgentPlan:
        self.turns.append([message.content for message in messages])
        return AgentPlan(
            next_action="continue",
            specialist="Clarification Agent",
            rationale="test",
        )


@pytest.mark.asyncio
async def test_engine_passes_full_multi_turn_history_to_planner():
    planner = RecordingPlanner()
    engine = make_engine()
    engine.planner = planner

    first = await engine.start(
        tenant_id="demo_cn",
        user_id="employee_001",
        message="帮我采购电脑",
    )
    await engine.continue_run(
        run_id=first.run.run_id,
        tenant_id="demo_cn",
        user_id="employee_001",
        message="20台，单台8000元含税，送雅加达，两周内，最低32GB和1TB",
    )

    assert planner.turns[0] == ["帮我采购电脑"]
    assert len(planner.turns[1]) == 3
    assert planner.turns[1][0] == "帮我采购电脑"
    assert "我还需要确认" in planner.turns[1][1]
    assert planner.turns[1][2].startswith("20台")


class CountingCatalog(FixtureCatalogAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.search_calls: list[str] = []

    async def search(self, provider: str, **kwargs):
        self.search_calls.append(provider)
        return await super().search(provider, **kwargs)


class ReadOnlyToolPlanner:
    def __init__(self) -> None:
        self.search_tool = build_function_tools()["search_catalog"]

    async def plan(
        self,
        messages: list[Message],
        *,
        context: AgentRuntimeContext | None = None,
    ) -> AgentPlan:
        assert context is not None
        await invoke(
            self.search_tool,
            context,
            '{"provider":"internal_catalog"}',
        )
        return AgentPlan(
            next_action="source_and_review",
            specialist="Sourcing & Comparison Agent",
            rationale="catalog evidence collected",
        )


class FailedProviderToolPlanner:
    def __init__(self) -> None:
        self.search_tool = build_function_tools()["search_catalog"]

    async def plan(
        self,
        messages: list[Message],
        *,
        context: AgentRuntimeContext | None = None,
    ) -> AgentPlan:
        assert context is not None
        await invoke(
            self.search_tool,
            context,
            '{"provider":"marketplace_beta"}',
        )
        return AgentPlan(
            next_action="source_and_review",
            specialist="Sourcing Agent",
            rationale="continue with the available providers",
        )


@pytest.mark.asyncio
async def test_engine_reuses_catalog_result_already_collected_by_agent_tool():
    catalog = CountingCatalog()
    engine = ProcurementEngine(
        catalog=catalog,
        enterprise=FixtureEnterpriseGateway(),
        repository=RunRepository(),
        harness=AgentHarness(secret="test"),
        planner=ReadOnlyToolPlanner(),
    )

    response = await engine.start(
        tenant_id="demo_cn",
        user_id="employee_001",
        message="采购20台开发笔记本，单台8000元含税，送雅加达，两周内，最低32GB和1TB",
    )

    assert response.run.state.value == "WAITING_CONFIRMATION"
    assert catalog.search_calls.count("internal_catalog") == 1
    assert sorted(catalog.search_calls) == [
        "internal_catalog",
        "marketplace_alpha",
        "marketplace_beta",
    ]
    assert any(
        event.name == "search_catalog"
        and event.detail.get("provider") == "internal_catalog"
        and event.detail.get("reused_agent_tool_result") is True
        for event in response.run.trace
    )


@pytest.mark.asyncio
async def test_engine_does_not_redispatch_provider_failed_during_agent_retrieval():
    catalog = CountingCatalog()
    engine = ProcurementEngine(
        catalog=catalog,
        enterprise=FixtureEnterpriseGateway(),
        repository=RunRepository(),
        harness=AgentHarness(secret="test"),
        planner=FailedProviderToolPlanner(),
    )

    response = await engine.start(
        tenant_id="demo_cn",
        user_id="employee_001",
        message="采购20台开发笔记本，单台8000元含税，送雅加达，两周内，最低32GB和1TB",
        scenario="provider_429",
    )

    assert response.run.state.value == "WAITING_CONFIRMATION"
    assert catalog.search_calls.count("marketplace_beta") == 3
    assert sorted(set(catalog.search_calls)) == [
        "internal_catalog",
        "marketplace_alpha",
        "marketplace_beta",
    ]
    failed_beta_calls = [
        item
        for item in engine.execution_repository.list_tools(
            response.run.run_id, tenant_id="demo_cn"
        )
        if item.provider == "marketplace_beta"
    ]
    assert len(failed_beta_calls) == 1
    assert failed_beta_calls[0].attempt_count == 1


class EvidenceBackedDecisionPlanner:
    def __init__(self) -> None:
        tools = build_function_tools()
        self.search_tool = tools["search_catalog"]
        self.selected_offer_id = ""

    async def plan(
        self,
        messages: list[Message],
        *,
        context: AgentRuntimeContext | None = None,
    ) -> AgentPlan:
        assert context is not None
        catalog = await invoke(
            self.search_tool,
            context,
            '{"provider":"internal_catalog"}',
        )
        selected = catalog.offers[-1]
        self.selected_offer_id = selected.offer_id
        return AgentPlan(
            next_action="recommend",
            specialist="Sourcing Agent",
            rationale="该候选来自已验证目录，服务端会复核成本、预算和制度",
            candidate_proposals=[
                AgentCandidateProposal(
                    provider="internal_catalog",
                    offer_id=selected.offer_id,
                    evidence_refs=selected.evidence_refs,
                )
            ],
            stop_condition="wait_for_user_confirmation",
        )


@pytest.mark.asyncio
async def test_evidence_backed_agent_candidate_can_drive_validated_recommendation():
    planner = EvidenceBackedDecisionPlanner()
    engine = ProcurementEngine(
        catalog=CountingCatalog(),
        enterprise=FixtureEnterpriseGateway(),
        repository=RunRepository(),
        harness=AgentHarness(secret="test"),
        planner=planner,
    )

    response = await engine.start(
        tenant_id="demo_cn",
        user_id="employee_001",
        message="采购20台开发笔记本，单台8000元含税，送雅加达，两周内，最低32GB和1TB",
    )

    assert response.run.recommendation.selection_source == "agent_validated"
    assert response.run.recommendation.best_overall.offer.offer_id == planner.selected_offer_id
    assert response.run.recommendation.selection_rationale
    assert any(event.name == "agent.recommendation_validated" for event in response.run.trace)


class LiveShapeDecisionPlanner:
    """Exercise the nine read calls observed in the current DeepSeek trajectory."""

    def __init__(self) -> None:
        self.tools = build_function_tools()

    async def plan(
        self,
        messages: list[Message],
        *,
        context: AgentRuntimeContext | None = None,
    ) -> AgentPlan:
        assert context is not None
        await invoke(self.tools["inspect_procurement_requirement"], context, "{}")
        await invoke(self.tools["get_budget"], context, "{}")
        catalogs = []
        for provider in ("internal_catalog", "marketplace_alpha", "marketplace_beta"):
            catalogs.append(
                await invoke(
                    self.tools["search_catalog"],
                    context,
                    '{"provider":"' + provider + '"}',
                )
            )
        for catalog in catalogs:
            selected = catalog.offers[0]
            await invoke(
                self.tools["calculate_landed_cost"],
                context,
                '{"provider":"' + selected.provider + '","offer_id":"' + selected.offer_id + '"}',
            )
        selected = catalogs[0].offers[-1]
        arguments = '{"provider":"internal_catalog","offer_id":"' + selected.offer_id + '"}'
        policy = await invoke(self.tools["evaluate_policy"], context, arguments)
        return AgentPlan(
            next_action="recommend",
            specialist="Procurement Orchestrator",
            rationale="live-shape evidence chain",
            candidate_proposals=[
                AgentCandidateProposal(
                    provider="internal_catalog",
                    offer_id=selected.offer_id,
                    evidence_refs=list(
                        dict.fromkeys([*selected.evidence_refs, *policy.evidence_refs])
                    ),
                )
            ],
            stop_condition="wait_for_user_confirmation",
        )


@pytest.mark.asyncio
async def test_live_shape_agent_reads_leave_commit_capacity_for_draft_creation():
    engine = ProcurementEngine(
        catalog=FixtureCatalogAdapter(),
        enterprise=FixtureEnterpriseGateway(),
        repository=RunRepository(),
        harness=AgentHarness(secret="live-shape"),
        planner=LiveShapeDecisionPlanner(),
    )
    recommendation = await engine.start(
        tenant_id="demo_cn",
        user_id="employee_001",
        message="采购20台开发笔记本，单台8000元含税，送雅加达，两周内，最低32GB和1TB",
    )

    completed = await engine.confirm(
        run_id=recommendation.run.run_id,
        tenant_id="demo_cn",
        user_id="employee_001",
        action_token=recommendation.run.action_token,
    )

    assert completed.run.state.value == "DRAFT_CREATED"
    assert completed.run.draft is not None
    assert completed.run.execution_usage.agent_read_tool_calls == 9
    assert completed.run.execution_usage.workflow_read_tool_calls == 1
    assert completed.run.execution_usage.commit_tool_calls == 3
    assert completed.run.execution_usage.tool_calls == 13
    assert completed.run.execution_usage.reserved_commit_tool_calls == 0


class InventedDecisionPlanner:
    async def plan(
        self,
        messages: list[Message],
        *,
        context: AgentRuntimeContext | None = None,
    ) -> AgentPlan:
        return AgentPlan(
            next_action="recommend",
            specialist="Procurement Orchestrator",
            rationale="invented",
            candidate_proposals=[
                AgentCandidateProposal(
                    provider="internal_catalog",
                    offer_id="invented-offer",
                    evidence_refs=["invented-evidence"],
                )
            ],
        )


@pytest.mark.asyncio
async def test_invented_agent_candidate_is_rejected_and_deterministic_fallback_is_used():
    engine = make_engine()
    engine.planner = InventedDecisionPlanner()

    response = await engine.start(
        tenant_id="demo_cn",
        user_id="employee_001",
        message="采购20台开发笔记本，单台8000元含税，送雅加达，两周内，最低32GB和1TB",
    )

    assert response.run.recommendation.selection_source == "deterministic_score"
    assert any(
        event.name == "agent.recommendation_rejected" and event.status == "REJECTED"
        for event in response.run.trace
    )


class BypassedToolPlanner:
    async def plan(
        self,
        messages: list[Message],
        *,
        context: AgentRuntimeContext | None = None,
    ) -> AgentPlan:
        assert context is not None
        requirement = context.run.requirement
        catalog = await context.catalog.search(
            "internal_catalog",
            minimum_ram_gb=requirement.minimum_ram_gb,
            minimum_storage_gb=requirement.minimum_storage_gb,
            quantity=requirement.quantity,
        )
        offer = catalog.offers[0]
        employee = context.enterprise.identity(context.run.tenant_id, context.run.user_id)
        policy = context.enterprise.evaluate(
            employee=employee,
            vendor_id=offer.vendor_id,
            landed_cost=calculate_landed_cost(offer, requirement).unconditional_total.amount,
            ram_gb=offer.variant.ram_gb,
            warranty_months=offer.variant.warranty_months,
            condition=offer.variant.condition,
        )
        return AgentPlan(
            next_action="recommend",
            specialist="Procurement Orchestrator",
            rationale="bypassed registered tools",
            candidate_proposals=[
                AgentCandidateProposal(
                    provider="internal_catalog",
                    offer_id=offer.offer_id,
                    evidence_refs=list(
                        dict.fromkeys(
                            [
                                *offer.match_decision.evidence_refs,
                                offer.evidence.evidence_id,
                                *policy.evidence_refs,
                            ]
                        )
                    ),
                )
            ],
        )


@pytest.mark.asyncio
async def test_candidate_is_rejected_when_required_agent_tools_were_not_called():
    engine = make_engine()
    engine.planner = BypassedToolPlanner()

    response = await engine.start(
        tenant_id="demo_cn",
        user_id="employee_001",
        message="采购20台开发笔记本，单台8000元含税，送雅加达，两周内，最低32GB和1TB",
    )

    assert response.run.recommendation.selection_source == "deterministic_score"
    assert any(
        event.name == "agent.recommendation_rejected" and event.status == "REJECTED"
        for event in response.run.trace
    )
