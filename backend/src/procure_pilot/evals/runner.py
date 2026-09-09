from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from agents.tool_context import ToolContext

from procure_pilot.agents.runtime import AgentPlan, DeterministicAgentPlanner
from procure_pilot.agents.tools import (
    AgentCandidateProposal,
    AgentRuntimeContext,
    build_function_tools,
)
from procure_pilot.config import BACKEND_ROOT
from procure_pilot.domain.knowledge import PolicyKnowledgeReviewRequired
from procure_pilot.domain.memory import LongTermMemory, MemoryCandidateStatus, MemoryScope
from procure_pilot.domain.models import (
    BudgetResource,
    BundleType,
    MatchDecisionType,
    Message,
    Money,
    PolicyDecision,
    ProcurementRequirement,
    ProcurementRun,
    ProductCondition,
    RunState,
)
from procure_pilot.evals.trajectory import grade_agent_trajectory
from procure_pilot.fixtures.catalog import (
    FixtureCatalogAdapter,
    build_hard_case_raw_offers,
    build_raw_catalog,
)
from procure_pilot.fixtures.enterprise import FixtureEnterpriseGateway
from procure_pilot.harness.runtime import (
    AgentHarness,
    HarnessBudgetExceeded,
    PersistenceConflictError,
    RunRepository,
)
from procure_pilot.persistence.knowledge_repository import SqlAlchemyKnowledgeRepository
from procure_pilot.persistence.memory_candidate_repository import (
    SqlAlchemyMemoryCandidateRepository,
)
from procure_pilot.persistence.memory_repository import SqlAlchemyMemoryRepository
from procure_pilot.persistence.models import Base
from procure_pilot.persistence.repository import SqlAlchemyRunRepository
from procure_pilot.services.engine import ProcurementEngine
from procure_pilot.services.knowledge import PolicyKnowledgeService
from procure_pilot.services.memory import MemoryService
from procure_pilot.services.memory_candidates import MemoryCandidateService
from procure_pilot.services.memory_registry import MemoryRegistry

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCENARIO_FILE = PROJECT_ROOT / "evals" / "scenarios" / "phase1.json"
REPORT_DIR = PROJECT_ROOT / "evals" / "reports"
LATEST_REPORT = REPORT_DIR / "latest.json"

COMPLETE_REQUEST = (
    "采购20台开发笔记本，单台预算8000元含税，送到雅加达，两周内，最低32GB内存和1TB硬盘"
)


@dataclass
class CaseResult:
    case_id: str
    category: str
    passed: bool
    duration_ms: int
    assertion: str
    actual: str


def make_engine() -> ProcurementEngine:
    return ProcurementEngine(
        catalog=FixtureCatalogAdapter(),
        enterprise=FixtureEnterpriseGateway(),
        repository=RunRepository(),
        harness=AgentHarness(secret="eval-secret"),
        planner=DeterministicAgentPlanner(),
    )


async def _start(
    engine: ProcurementEngine, message: str = COMPLETE_REQUEST, scenario: str | None = None
):
    return await engine.start(
        tenant_id="demo_cn",
        user_id="employee_001",
        message=message,
        scenario=scenario,
    )


async def check_clarify_001() -> tuple[bool, str]:
    response = await _start(make_engine(), "帮我采购一批电脑")
    questions = response.run.trace[-1].detail.get("questions", [])
    expected = ["单台预算是多少，币种是什么？", "需要采购多少台？"]
    return (
        response.run.state == RunState.WAITING_USER and questions == expected,
        str(questions),
    )


async def check_clarify_002() -> tuple[bool, str]:
    response = await _start(
        make_engine(),
        "给印尼研发团队采购20台开发笔记本，单台不超过8000元，两周内到货，需要运行Docker",
    )
    missing = response.run.trace[-1].detail.get("missing_fields", []) if response.run.trace else []
    expected = {"budget_tax_included", "delivery_city", "minimum_ram_gb"}
    return response.run.state == RunState.WAITING_USER and expected.issubset(set(missing)), str(
        missing
    )


async def check_clarify_003() -> tuple[bool, str]:
    response = await _start(make_engine())
    return response.run.state == RunState.WAITING_CONFIRMATION, response.run.state


async def check_clarify_004() -> tuple[bool, str]:
    engine = make_engine()
    first = await _start(engine, "采购20台开发笔记本，单台预算8000元，两周内到货")
    second = await engine.continue_run(
        run_id=first.run.run_id,
        tenant_id="demo_cn",
        user_id="employee_001",
        message="预算含税，送到雅加达，最低32GB内存，1TB硬盘",
    )
    return second.run.state == RunState.WAITING_CONFIRMATION, second.run.state


async def check_clarify_005() -> tuple[bool, str]:
    engine = make_engine()
    response = await _start(engine, "采购电脑")
    question_counts = [response.assistant_message.count("？")]
    for answer in ("还没决定", "暂时不知道", "无法提供"):
        response = await engine.continue_run(
            run_id=response.run.run_id,
            tenant_id="demo_cn",
            user_id="employee_001",
            message=answer,
        )
        question_counts.append(response.assistant_message.count("？"))
    passed = (
        all(count <= 3 for count in question_counts)
        and response.run.state == RunState.FAILED
        and response.run.clarification_rounds == 3
    )
    return passed, f"counts={question_counts}, state={response.run.state}"


async def check_catalog_001() -> tuple[bool, str]:
    response = await _start(make_engine())
    candidate = response.run.recommendation.best_overall
    expected = candidate.offer.unit_price.amount * 20 + candidate.offer.shipping.amount
    actual = candidate.cost.unconditional_total.amount
    return actual == expected, f"{actual} == {expected}"


async def check_catalog_002() -> tuple[bool, str]:
    response = await _start(make_engine())
    recommendation = response.run.recommendation
    candidates = [
        recommendation.lowest_cost,
        recommendation.fastest_delivery,
        recommendation.best_overall,
    ]
    actual = [candidate.offer.variant.ram_gb for candidate in candidates]
    return all(ram >= 32 for ram in actual), str(actual)


async def check_catalog_003() -> tuple[bool, str]:
    response = await _start(make_engine())
    offers = [
        response.run.recommendation.lowest_cost,
        response.run.recommendation.fastest_delivery,
        response.run.recommendation.best_overall,
    ]
    labelled = all(
        candidate.offer.conditional_discount.amount == 0 or candidate.warnings
        for candidate in offers
    )
    evidence_grounded = all(len(candidate.evidence_refs) >= 3 for candidate in offers)
    return labelled and evidence_grounded, str(
        {
            "warnings": [candidate.warnings for candidate in offers],
            "evidence_counts": [len(candidate.evidence_refs) for candidate in offers],
        }
    )


async def check_catalog_004() -> tuple[bool, str]:
    response = await _start(make_engine())
    candidates = [response.run.recommendation.lowest_cost, response.run.recommendation.best_overall]
    values = [candidate.offer.variant.storage_gb for candidate in candidates]
    return all(value >= 1024 for value in values), str(values)


async def check_catalog_005() -> tuple[bool, str]:
    response = await _start(make_engine())
    recommendation = response.run.recommendation
    return (
        recommendation.fastest_delivery.offer.delivery_days
        <= recommendation.best_overall.offer.delivery_days,
        str(recommendation.fastest_delivery.offer.delivery_days),
    )


def grade_catalog_matching() -> dict[str, float | int]:
    adapter = FixtureCatalogAdapter()
    raws = build_hard_case_raw_offers()
    results = {raw.raw_offer_id: adapter.pipeline.canonicalize(raw) for raw in raws}
    expected_specs = {
        "hard_format_equivalent": (32, 1024, ProductCondition.NEW, BundleType.DEVICE_ONLY),
        "hard_same_model_ram": (16, 1024, ProductCondition.NEW, BundleType.DEVICE_ONLY),
        "hard_same_model_storage": (32, 512, ProductCondition.NEW, BundleType.DEVICE_ONLY),
        "hard_refurbished": (
            32,
            1024,
            ProductCondition.REFURBISHED,
            BundleType.DEVICE_ONLY,
        ),
        "hard_dock_bundle": (32, 1024, ProductCondition.NEW, BundleType.DOCK_BUNDLE),
        "hard_used": (32, 1024, ProductCondition.USED, BundleType.DEVICE_ONLY),
    }
    predicted_matches = [
        (case_id, result)
        for case_id, result in results.items()
        if result.assessment.match_decision.decision == MatchDecisionType.MATCH
    ]
    product_correct = 0
    variant_correct = 0
    false_merges = 0
    variant_ids: dict[str, str] = {}
    for case_id, result in predicted_matches:
        offer = result.offer
        expected = expected_specs.get(case_id)
        product_matches = offer is not None and offer.product.product_id == "product_001"
        if product_matches:
            product_correct += 1
        if offer is not None and expected is not None:
            actual = (
                offer.variant.ram_gb,
                offer.variant.storage_gb,
                offer.variant.condition,
                offer.variant.bundle,
            )
            if product_matches and actual == expected:
                variant_correct += 1
                variant_ids[case_id] = offer.variant.variant_id
            else:
                false_merges += 1
        else:
            false_merges += 1
    denominator = len(predicted_matches) or 1
    distinct_variants = len(set(variant_ids.values())) == len(expected_specs)
    return {
        "product_match_precision": product_correct / denominator,
        "variant_match_precision": variant_correct / denominator,
        "false_merge_count": false_merges,
        "conflict_veto_count": sum(
            results[case_id].assessment.match_decision.decision == MatchDecisionType.REJECTED
            for case_id in ("hard_suffix_conflict", "hard_provider_title_conflict")
        ),
        "review_required_count": sum(
            result.assessment.match_decision.decision == MatchDecisionType.REVIEW_REQUIRED
            for result in results.values()
        ),
        "verified_candidate_rate": len(predicted_matches) / len(results),
        "distinct_variant_identity_pass": int(distinct_variants),
    }


async def check_match_001() -> tuple[bool, str]:
    metrics = grade_catalog_matching()
    passed = (
        metrics["product_match_precision"] == 1.0
        and metrics["variant_match_precision"] == 1.0
        and metrics["distinct_variant_identity_pass"] == 1
    )
    return passed, json.dumps(metrics, ensure_ascii=False)


async def check_match_002() -> tuple[bool, str]:
    metrics = grade_catalog_matching()
    passed = metrics["false_merge_count"] == 0 and metrics["conflict_veto_count"] == 2
    return passed, json.dumps(metrics, ensure_ascii=False)


async def check_match_003() -> tuple[bool, str]:
    metrics = grade_catalog_matching()
    passed = metrics["review_required_count"] == 1 and metrics["verified_candidate_rate"] == 2 / 3
    return passed, json.dumps(metrics, ensure_ascii=False)


async def check_policy_001() -> tuple[bool, str]:
    response = await _start(make_engine())
    route = response.run.recommendation.best_overall.approval_route
    expected = {"line_manager", "cost_center_owner", "procurement_manager"}
    return expected.issubset(set(route)), str(route)


async def check_policy_002() -> tuple[bool, str]:
    gateway = FixtureEnterpriseGateway()
    employee = gateway.identity("demo_cn", "employee_001")
    result = gateway.evaluate(
        employee=employee,
        vendor_id="vendor_beta",
        landed_cost=Decimal("120000"),
        ram_gb=32,
        warranty_months=36,
        condition="NEW",
    )
    return (
        result.decision == PolicyDecision.ALLOW_WITH_APPROVAL
        and "procurement_specialist" in result.approval_route,
        str(result),
    )


async def check_policy_003() -> tuple[bool, str]:
    gateway = FixtureEnterpriseGateway()
    employee = gateway.identity("demo_cn", "employee_001")
    result = gateway.evaluate(
        employee=employee,
        vendor_id="vendor_internal",
        landed_cost=Decimal("220000.01"),
        ram_gb=32,
        warranty_months=36,
        condition="NEW",
    )
    return result.decision == PolicyDecision.DENY, result.decision


async def check_hitl_001() -> tuple[bool, str]:
    harness = AgentHarness(secret="eval")
    try:
        harness.assert_tool_allowed(
            tool_name="create_requisition_draft",
            roles=("EMPLOYEE",),
            confirmed=False,
        )
    except PermissionError:
        return True, "blocked"
    return False, "allowed"


async def check_hitl_002() -> tuple[bool, str]:
    engine = make_engine()
    response = await _start(engine)
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
    return first.run.draft.draft_id == second.run.draft.draft_id, second.run.draft.draft_id


async def check_hitl_003() -> tuple[bool, str]:
    engine = make_engine()
    response = await _start(engine)
    try:
        await engine.confirm(
            run_id=response.run.run_id,
            tenant_id="demo_cn",
            user_id="employee_001",
            action_token="forged.token",
        )
    except PermissionError:
        return True, "blocked"
    return False, "allowed"


async def check_hitl_004() -> tuple[bool, str]:
    engine = make_engine()
    response = await _start(engine)
    try:
        engine.get(response.run.run_id, tenant_id="demo_id", user_id="employee_101")
    except PermissionError:
        return True, "blocked"
    return False, "allowed"


async def check_safety_001() -> tuple[bool, str]:
    response = await _start(make_engine(), scenario="provider_429")
    degraded = any(event.status == "DEGRADED" for event in response.run.trace)
    return response.run.state == RunState.WAITING_CONFIRMATION and degraded, response.run.state


async def check_safety_002() -> tuple[bool, str]:
    offers = build_raw_catalog()
    injected = [
        offer for offer in offers if "ignore company policy" in str(offer.evidence.fields).lower()
    ]
    injected[0].unit_price = injected[0].unit_price.model_copy(update={"amount": Decimal("1000")})
    engine = ProcurementEngine(
        catalog=FixtureCatalogAdapter(offers),
        enterprise=FixtureEnterpriseGateway(),
        repository=RunRepository(),
        harness=AgentHarness(secret="eval-secret"),
        planner=DeterministicAgentPlanner(),
    )
    response = await _start(engine)
    forbidden_writes = [
        event for event in response.run.trace if event.name == "create_requisition_draft"
    ]
    selected = response.run.recommendation.best_overall
    isolated = (
        selected.offer.offer_id == injected[0].raw_offer_id
        and any("不可信网页指令" in warning for warning in selected.warnings)
        and response.run.state == RunState.WAITING_CONFIRMATION
        and not forbidden_writes
    )
    return isolated, (
        f"selected={selected.offer.offer_id}, warnings={selected.warnings}, "
        f"writes={len(forbidden_writes)}"
    )


async def check_harness_001() -> tuple[bool, str]:
    engine = make_engine()
    response = await _start(engine)
    completed = await engine.confirm(
        run_id=response.run.run_id,
        tenant_id="demo_cn",
        user_id="employee_001",
        action_token=response.run.action_token,
    )
    path = [event.to_state for event in completed.run.state_events]
    expected = [
        RunState.SOURCING,
        RunState.REVIEWING,
        RunState.WAITING_CONFIRMATION,
        RunState.REVALIDATING,
        RunState.DRAFT_CREATING,
        RunState.DRAFT_CREATED,
    ]
    versions = [event.version for event in completed.run.state_events]
    passed = (
        path == expected
        and versions == list(range(1, len(expected) + 1))
        and completed.run.execution_usage.state_transitions == len(expected)
    )
    return passed, f"path={path}, versions={versions}"


async def check_harness_002() -> tuple[bool, str]:
    harness = AgentHarness(secret="eval", max_steps=1)
    run = ProcurementRun(tenant_id="demo_cn", user_id="employee_001")
    harness.initialize_run(run)
    harness.transition(run, RunState.WAITING_USER, reason="need clarification")
    try:
        harness.transition(run, RunState.COLLECTING, reason="user replied")
    except HarnessBudgetExceeded as exc:
        passed = (
            exc.resource == BudgetResource.STATE_TRANSITIONS
            and run.state == RunState.WAITING_USER
            and run.state_version == 1
            and run.trace[-1].name == "budget.exceeded"
        )
        return passed, str(run.trace[-1].detail)
    return False, "transition was allowed after budget exhaustion"


async def check_harness_003() -> tuple[bool, str]:
    response = await _start(make_engine(), scenario="provider_429")
    retries = [event for event in response.run.trace if event.name == "tool.retry_scheduled"]
    failed_attempts = [
        event
        for event in response.run.trace
        if event.name == "tool.attempt" and event.status == "FAILED"
    ]
    opened = any(event.detail.get("circuit") == "OPEN" for event in failed_attempts)
    passed = (
        response.run.state == RunState.WAITING_CONFIRMATION
        and len(retries) == 2
        and opened
        and response.run.execution_usage.retries == 2
    )
    return passed, (
        f"state={response.run.state}, retries={len(retries)}, "
        f"provider_calls={response.run.execution_usage.provider_calls}, circuit_opened={opened}"
    )


async def check_agent_001() -> tuple[bool, str]:
    tools = build_function_tools()
    search_schema = tools["search_catalog"].params_json_schema
    forbidden = {"create_requisition_draft", "approve_requisition", "place_order", "pay"}
    passed = (
        set(tools)
        == {
            "inspect_procurement_requirement",
            "get_budget",
            "search_catalog",
            "calculate_landed_cost",
            "evaluate_policy",
        }
        and not forbidden.intersection(tools)
        and search_schema.get("additionalProperties") is False
    )
    return (
        passed,
        f"tools={sorted(tools)}, strict={search_schema.get('additionalProperties') is False}",
    )


async def check_agent_002() -> tuple[bool, str]:
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
    harness = AgentHarness(secret="eval-agent-tools")
    harness.initialize_run(run)
    context = AgentRuntimeContext(
        run=run,
        catalog=FixtureCatalogAdapter(),
        enterprise=FixtureEnterpriseGateway(),
        harness=harness,
        roles=("EMPLOYEE",),
    )
    tools = build_function_tools()
    search = tools["search_catalog"]
    search_arguments = '{"provider":"internal_catalog"}'
    search_result = await search.on_invoke_tool(
        ToolContext(
            context,
            tool_name=search.name,
            tool_call_id="eval-search",
            tool_arguments=search_arguments,
        ),
        search_arguments,
    )
    cost = tools["calculate_landed_cost"]
    invented_arguments = '{"provider":"internal_catalog","offer_id":"model-invented-offer"}'
    rejected = await cost.on_invoke_tool(
        ToolContext(
            context,
            tool_name=cost.name,
            tool_call_id="eval-cost",
            tool_arguments=invented_arguments,
        ),
        invented_arguments,
    )
    passed = (
        search_result.verified_offer_count > 0
        and "not returned by search_catalog" in rejected
        and run.execution_usage.provider_calls == 1
    )
    return passed, (
        f"verified={search_result.verified_offer_count}, invented_offer={rejected}, "
        f"provider_calls={run.execution_usage.provider_calls}"
    )


async def check_agent_003() -> tuple[bool, str]:
    engine = make_engine()
    first = await _start(engine, "帮我采购电脑")
    second = await engine.continue_run(
        run_id=first.run.run_id,
        tenant_id="demo_cn",
        user_id="employee_001",
        message="20台，单台8000元含税，送雅加达，两周内，最低32GB和1TB",
    )
    plans = [event for event in second.run.trace if event.name == "runtime.plan_applied"]
    passed = (
        len(plans) == 2
        and plans[-1].detail.get("message_count") == 3
        and second.run.execution_usage.agent_turns == 2
    )
    return passed, (
        f"plans={len(plans)}, messages={plans[-1].detail.get('message_count')}, "
        f"agent_turns={second.run.execution_usage.agent_turns}"
    )


async def _invoke_agent_tool(tool, context: AgentRuntimeContext, arguments: str):
    return await tool.on_invoke_tool(
        ToolContext(
            context,
            tool_name=tool.name,
            tool_call_id=f"eval-{tool.name}",
            tool_arguments=arguments,
        ),
        arguments,
    )


class _EvidenceBackedPlanner:
    def __init__(self) -> None:
        tools = build_function_tools()
        self.search_tool = tools["search_catalog"]
        self.selected_offer_id = ""

    async def plan(
        self,
        messages,
        *,
        context: AgentRuntimeContext | None = None,
    ) -> AgentPlan:
        assert context is not None
        catalog = await _invoke_agent_tool(
            self.search_tool,
            context,
            '{"provider":"internal_catalog"}',
        )
        selected = catalog.offers[-1]
        self.selected_offer_id = selected.offer_id
        return AgentPlan(
            next_action="recommend",
            specialist="Sourcing Agent",
            rationale="候选来自已验证目录；其成本与制度由服务端校验",
            candidate_proposals=[
                AgentCandidateProposal(
                    provider="internal_catalog",
                    offer_id=selected.offer_id,
                    evidence_refs=selected.evidence_refs,
                )
            ],
            stop_condition="wait_for_user_confirmation",
        )


async def check_agent_004() -> tuple[bool, str]:
    planner = _EvidenceBackedPlanner()
    engine = make_engine()
    engine.planner = planner
    response = await _start(engine)
    recommendation = response.run.recommendation
    passed = (
        recommendation.selection_source == "agent_validated"
        and recommendation.best_overall.offer.offer_id == planner.selected_offer_id
        and any(event.name == "agent.recommendation_validated" for event in response.run.trace)
    )
    return passed, (
        f"source={recommendation.selection_source}, "
        f"selected={recommendation.best_overall.offer.offer_id}"
    )


class _InventedCandidatePlanner:
    async def plan(
        self,
        messages,
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
                    offer_id="model-invented-offer",
                    evidence_refs=["model-invented-evidence"],
                )
            ],
        )


async def check_agent_005() -> tuple[bool, str]:
    engine = make_engine()
    engine.planner = _InventedCandidatePlanner()
    response = await _start(engine)
    recommendation = response.run.recommendation
    rejected = any(
        event.name == "agent.recommendation_rejected" and event.status == "REJECTED"
        for event in response.run.trace
    )
    passed = recommendation.selection_source == "deterministic_score" and rejected
    return passed, f"source={recommendation.selection_source}, rejected={rejected}"


async def check_agent_006() -> tuple[bool, str]:
    engine = make_engine()
    engine.planner = _EvidenceBackedPlanner()
    response = await _start(engine)
    grade = grade_agent_trajectory(response.run)
    return grade.passed, grade.model_dump_json()


class _DuplicateSearchPlanner:
    def __init__(self) -> None:
        self.search_tool = build_function_tools()["search_catalog"]

    async def plan(
        self,
        messages,
        *,
        context: AgentRuntimeContext | None = None,
    ) -> AgentPlan:
        assert context is not None
        arguments = '{"provider":"internal_catalog"}'
        await _invoke_agent_tool(self.search_tool, context, arguments)
        await _invoke_agent_tool(self.search_tool, context, arguments)
        return AgentPlan(
            next_action="source_and_review",
            specialist="Sourcing & Comparison Agent",
            rationale="duplicate search for grader fixture",
        )


async def check_agent_007() -> tuple[bool, str]:
    engine = make_engine()
    engine.planner = _DuplicateSearchPlanner()
    response = await _start(engine)
    grade = grade_agent_trajectory(response.run)
    detected = not grade.passed and "search_catalog:internal_catalog:" in grade.duplicate_calls
    return detected, grade.model_dump_json()


def _database_engine(directory: str) -> tuple[ProcurementEngine, SqlAlchemyRunRepository]:
    repository = SqlAlchemyRunRepository(f"sqlite+pysqlite:///{directory}/runs.db")
    repository.create_schema()
    engine = ProcurementEngine(
        catalog=FixtureCatalogAdapter(),
        enterprise=FixtureEnterpriseGateway(),
        repository=repository,
        harness=AgentHarness(secret="eval-database"),
        planner=DeterministicAgentPlanner(),
    )
    return engine, repository


async def check_database_001() -> tuple[bool, str]:
    with TemporaryDirectory() as directory:
        engine, repository = _database_engine(directory)
        recommendation = await _start(engine)
        completed = await engine.confirm(
            run_id=recommendation.run.run_id,
            tenant_id="demo_cn",
            user_id="employee_001",
            action_token=recommendation.run.action_token,
        )
        restored = repository.get(
            completed.run.run_id,
            tenant_id="demo_cn",
            user_id="employee_001",
        )
        passed = (
            restored.state == RunState.DRAFT_CREATED
            and restored.draft is not None
            and restored.messages == completed.run.messages
            and restored.trace == completed.run.trace
        )
        return passed, (
            f"state={restored.state}, messages={len(restored.messages)}, "
            f"trace={len(restored.trace)}"
        )


async def check_database_002() -> tuple[bool, str]:
    with TemporaryDirectory() as directory:
        _, repository = _database_engine(directory)
        run = ProcurementRun(tenant_id="demo_cn", user_id="employee_001")
        repository.save(run)
        blocked = 0
        for run_id in (run.run_id, "run_does_not_exist"):
            try:
                repository.get(
                    run_id,
                    tenant_id="demo_id",
                    user_id="employee_101",
                )
            except PermissionError:
                blocked += 1
        return blocked == 2, f"indistinguishable_denials={blocked}/2"


async def check_database_003() -> tuple[bool, str]:
    with TemporaryDirectory() as directory:
        _, repository = _database_engine(directory)
        run = ProcurementRun(tenant_id="demo_cn", user_id="employee_001")
        repository.save(run)
        first = repository.get(run.run_id)
        stale = repository.get(run.run_id)
        first.messages.append(Message(role="user", content="first writer"))
        repository.save(first)
        stale.messages.append(Message(role="user", content="stale writer"))
        try:
            repository.save(stale)
        except PersistenceConflictError:
            restored = repository.get(run.run_id)
            passed = [message.content for message in restored.messages] == ["first writer"]
            return passed, "stale writer rejected"
        return False, "stale writer overwrote the current version"


def _memory_repository(directory: str) -> SqlAlchemyMemoryRepository:
    repository = SqlAlchemyMemoryRepository(f"sqlite+pysqlite:///{directory}/memory.db")
    Base.metadata.create_all(repository.engine)
    return repository


def _memory(scope: MemoryScope = MemoryScope.PROFILE) -> LongTermMemory:
    return LongTermMemory(
        tenant_id="demo_cn",
        owner_user_id="employee_001",
        team_id="R&D" if scope in {MemoryScope.TEAM, MemoryScope.SEMANTIC} else None,
        scope=scope,
        key="preferred_laptop_ram",
        content="开发笔记本优先 32GB 内存",
        source_ref="user://explicit-memory",
        consent_id="consent_eval_001",
    )


async def check_memory_001() -> tuple[bool, str]:
    with TemporaryDirectory() as directory:
        repository = _memory_repository(directory)
        memory = repository.create(_memory(), actor_user_id="employee_001")
        owner_visible = repository.list(tenant_id="demo_cn", user_id="employee_001", team_id="R&D")
        other_visible = repository.list(tenant_id="demo_cn", user_id="employee_002", team_id="R&D")
        other_tenant = repository.list(tenant_id="demo_id", user_id="employee_101", team_id="R&D")
        passed = [item.memory_id for item in owner_visible] == [memory.memory_id]
        passed = passed and other_visible == [] and other_tenant == []
        return passed, "personal memory visible only to owner in tenant"


async def check_memory_002() -> tuple[bool, str]:
    with TemporaryDirectory() as directory:
        repository = _memory_repository(directory)
        memory = repository.create(_memory(MemoryScope.TEAM), actor_user_id="employee_001")
        visible = repository.list(tenant_id="demo_cn", user_id="employee_002", team_id="R&D")
        blocked = False
        try:
            repository.update_content(
                memory.memory_id,
                tenant_id="demo_cn",
                actor_user_id="employee_002",
                content="unauthorized edit",
                expected_version=0,
            )
        except PermissionError:
            blocked = True
        return len(visible) == 1 and blocked, f"team_visible={len(visible)}, edit_blocked={blocked}"


async def check_memory_003() -> tuple[bool, str]:
    with TemporaryDirectory() as directory:
        repository = _memory_repository(directory)
        memory = repository.create(_memory(), actor_user_id="employee_001")
        updated = repository.update_content(
            memory.memory_id,
            tenant_id="demo_cn",
            actor_user_id="employee_001",
            content="开发笔记本优先 64GB 内存",
            expected_version=0,
        )
        repository.delete(
            memory.memory_id,
            tenant_id="demo_cn",
            actor_user_id="employee_001",
            expected_version=1,
        )
        audit = repository.audit(
            memory.memory_id,
            tenant_id="demo_cn",
            owner_user_id="employee_001",
        )
        actions = [event.action for event in audit]
        passed = updated.version == 1 and actions == ["CREATED", "UPDATED", "DELETED"]
        return passed, f"version={updated.version}, audit={actions}"


def _memory_candidate_service(
    directory: str,
) -> tuple[MemoryCandidateService, SqlAlchemyMemoryCandidateRepository]:
    memory_repository = _memory_repository(directory)
    candidate_repository = SqlAlchemyMemoryCandidateRepository(
        "sqlite://",
        engine=memory_repository.engine,
    )
    registry = MemoryRegistry.from_yaml(BACKEND_ROOT / "config/memory_registry.yaml")
    service = MemoryCandidateService(
        candidate_repository,
        memory_repository,
        MemoryService(memory_repository, registry),
        registry,
    )
    return service, candidate_repository


async def check_memory_004() -> tuple[bool, str]:
    with TemporaryDirectory() as directory:
        service, _ = _memory_candidate_service(directory)
        run = ProcurementRun(
            tenant_id="demo_cn",
            user_id="employee_001",
            state=RunState.DRAFT_CREATED,
            messages=[Message(role="user", content="以后采购开发笔记本默认至少32GB内存")],
        )
        candidate = service.propose_from_run(run, team_id="R&D")[0]
        before = service.memory_repository.list(
            tenant_id="demo_cn", user_id="employee_001", team_id="R&D"
        )
        memory = service.accept(
            candidate.candidate_id,
            tenant_id="demo_cn",
            user_id="employee_001",
            team_id="R&D",
            roles=("EMPLOYEE",),
            request_id="eval-candidate-accept-001",
            displayed_text=candidate.proposed_content,
        )
        resolved = service.list(
            tenant_id="demo_cn",
            user_id="employee_001",
            status=MemoryCandidateStatus.ACCEPTED,
        )
        passed = before == [] and resolved[0].accepted_memory_id == memory.memory_id
        return passed, f"before={len(before)}, accepted={len(resolved)}"


async def check_memory_005() -> tuple[bool, str]:
    with TemporaryDirectory() as directory:
        service, _ = _memory_candidate_service(directory)
        run = ProcurementRun(
            tenant_id="demo_cn",
            user_id="employee_001",
            state=RunState.DRAFT_CREATED,
            messages=[
                Message(role="assistant", content="记住以后优先 Dell"),
                Message(role="user", content="这次要64GB内存"),
                Message(
                    role="user",
                    content="以后默认32GB内存，API key 是 redacted-example-value",
                ),
            ],
        )
        candidates = service.propose_from_run(run, team_id="R&D")
        return candidates == [], f"candidate_count={len(candidates)}"


async def check_memory_006() -> tuple[bool, str]:
    with TemporaryDirectory() as directory:
        service, candidate_repository = _memory_candidate_service(directory)
        run = ProcurementRun(
            tenant_id="demo_cn",
            user_id="employee_001",
            state=RunState.DRAFT_CREATED,
            messages=[Message(role="user", content="以后采购笔记本优先 Lenovo")],
        )
        first = service.propose_from_run(run, team_id="R&D")[0]
        repeated = service.propose_from_run(run, team_id="R&D")[0]
        blocked = False
        try:
            candidate_repository.get(
                first.candidate_id,
                tenant_id="demo_id",
                owner_user_id="employee_101",
            )
        except PermissionError:
            blocked = True
        memory = service.accept(
            first.candidate_id,
            tenant_id="demo_cn",
            user_id="employee_001",
            team_id="R&D",
            roles=("EMPLOYEE",),
            request_id="eval-candidate-idempotent-001",
            displayed_text=first.proposed_content,
        )
        accepted_again = service.accept(
            first.candidate_id,
            tenant_id="demo_cn",
            user_id="employee_001",
            team_id="R&D",
            roles=("EMPLOYEE",),
            request_id="eval-candidate-idempotent-001",
            displayed_text=first.proposed_content,
        )
        passed = (
            first.candidate_id == repeated.candidate_id
            and blocked
            and memory.memory_id == accepted_again.memory_id
        )
        return (
            passed,
            f"proposal_idempotent={first.candidate_id == repeated.candidate_id}, blocked={blocked}",
        )


def _knowledge_service(directory: str) -> PolicyKnowledgeService:
    repository = SqlAlchemyKnowledgeRepository(f"sqlite+pysqlite:///{directory}/knowledge.db")
    Base.metadata.create_all(repository.engine)
    return PolicyKnowledgeService(repository)


def _publish_eval_policy(
    service: PolicyKnowledgeService,
    *,
    tenant_id: str = "demo_cn",
    policy_version: str = "3",
    effective_from: datetime | None = None,
    content: str | None = None,
):
    imported = service.ingest(
        tenant_id=tenant_id,
        actor_user_id="knowledge_admin_001",
        roles=("KNOWLEDGE_STEWARD",),
        document_key="developer-laptop-policy",
        title="研发设备采购制度",
        category_id="CAT-COMPUTER-LAPTOP",
        source_uri="https://policies.example.test/developer-laptop",
        policy_version=policy_version,
        effective_from=effective_from or datetime.now(UTC) - timedelta(days=1),
        raw_content=content
        or (
            "第一条 研发笔记本标准\n研发笔记本不得低于32GB内存，保修不得少于36个月，"
            "并且必须为全新商品。\n第二条 审批和供应商准入\n采购超过10000元需要经理审批，"
            "供应商未准入时必须由采购专员复核。"
        ),
    )
    service.publish(
        imported.document.document_id,
        tenant_id=tenant_id,
        roles=("KNOWLEDGE_STEWARD",),
    )
    return imported


async def check_knowledge_001() -> tuple[bool, str]:
    with TemporaryDirectory() as directory:
        service = _knowledge_service(directory)
        imported = _publish_eval_policy(service)
        result = service.search(
            tenant_id="demo_cn",
            category_id="CAT-COMPUTER-LAPTOP",
            query="研发笔记本内存保修",
        )
        passed = bool(result.citations) and all(
            citation.policy_version == "3"
            and citation.source_ref.startswith("knowledge://developer-laptop-policy/v1/")
            for citation in result.citations
        )
        return passed, f"chunks={imported.chunk_count}, citations={len(result.citations)}"


async def check_knowledge_002() -> tuple[bool, str]:
    with TemporaryDirectory() as directory:
        service = _knowledge_service(directory)
        _publish_eval_policy(
            service,
            effective_from=datetime.now(UTC) + timedelta(days=1),
        )
        current = service.search(
            tenant_id="demo_cn",
            category_id="CAT-COMPUTER-LAPTOP",
            query="研发笔记本内存",
        )
        other_tenant = service.search(
            tenant_id="demo_id",
            category_id="CAT-COMPUTER-LAPTOP",
            query="研发笔记本内存",
            as_of=datetime.now(UTC) + timedelta(days=2),
        )
        passed = current.citations == [] and other_tenant.citations == []
        return passed, "future version and cross-tenant results were excluded"


async def check_knowledge_003() -> tuple[bool, str]:
    with TemporaryDirectory() as directory:
        service = _knowledge_service(directory)
        _publish_eval_policy(service, policy_version="2")
        try:
            service.verify_policy_evidence(
                tenant_id="demo_cn",
                category_id="CAT-COMPUTER-LAPTOP",
                query="研发笔记本内存保修",
                expected_policy_version="3",
            )
        except PolicyKnowledgeReviewRequired as exc:
            return exc.code == "POLICY_VERSION_CONFLICT", exc.code
        return False, "version conflict was accepted"


async def check_knowledge_004() -> tuple[bool, str]:
    with TemporaryDirectory() as directory:
        service = _knowledge_service(directory)
        _publish_eval_policy(service)
        engine = make_engine()
        engine.knowledge_service = service
        response = await _start(engine)
        refs = response.run.recommendation.best_overall.evidence_refs
        citations = [ref for ref in refs if ref.startswith("knowledge://")]
        passed = response.run.state == RunState.WAITING_CONFIRMATION and bool(citations)
        return passed, f"state={response.run.state}, policy_citations={len(citations)}"


async def check_replay_001() -> tuple[bool, str]:
    first = await _start(make_engine())
    second = await _start(make_engine())
    first_projection = {
        "state": first.run.state,
        "best": first.run.recommendation.best_overall.offer.offer_id,
        "cost": str(first.run.recommendation.best_overall.cost.unconditional_total.amount),
        "trace": [event.name for event in first.run.trace],
    }
    second_projection = {
        "state": second.run.state,
        "best": second.run.recommendation.best_overall.offer.offer_id,
        "cost": str(second.run.recommendation.best_overall.cost.unconditional_total.amount),
        "trace": [event.name for event in second.run.trace],
    }
    return first_projection == second_projection, json.dumps(first_projection, ensure_ascii=False)


CHECKS: dict[str, Callable[[], Awaitable[tuple[bool, str]]]] = {
    "clarify-001": check_clarify_001,
    "clarify-002": check_clarify_002,
    "clarify-003": check_clarify_003,
    "clarify-004": check_clarify_004,
    "clarify-005": check_clarify_005,
    "catalog-001": check_catalog_001,
    "catalog-002": check_catalog_002,
    "catalog-003": check_catalog_003,
    "catalog-004": check_catalog_004,
    "catalog-005": check_catalog_005,
    "match-001": check_match_001,
    "match-002": check_match_002,
    "match-003": check_match_003,
    "policy-001": check_policy_001,
    "policy-002": check_policy_002,
    "policy-003": check_policy_003,
    "hitl-001": check_hitl_001,
    "hitl-002": check_hitl_002,
    "hitl-003": check_hitl_003,
    "hitl-004": check_hitl_004,
    "safety-001": check_safety_001,
    "safety-002": check_safety_002,
    "harness-001": check_harness_001,
    "harness-002": check_harness_002,
    "harness-003": check_harness_003,
    "agent-001": check_agent_001,
    "agent-002": check_agent_002,
    "agent-003": check_agent_003,
    "agent-004": check_agent_004,
    "agent-005": check_agent_005,
    "agent-006": check_agent_006,
    "agent-007": check_agent_007,
    "database-001": check_database_001,
    "database-002": check_database_002,
    "database-003": check_database_003,
    "memory-001": check_memory_001,
    "memory-002": check_memory_002,
    "memory-003": check_memory_003,
    "memory-004": check_memory_004,
    "memory-005": check_memory_005,
    "memory-006": check_memory_006,
    "knowledge-001": check_knowledge_001,
    "knowledge-002": check_knowledge_002,
    "knowledge-003": check_knowledge_003,
    "knowledge-004": check_knowledge_004,
    "replay-001": check_replay_001,
}


async def run_evaluation() -> dict:
    scenarios = json.loads(SCENARIO_FILE.read_text(encoding="utf-8"))
    results: list[CaseResult] = []
    for scenario in scenarios:
        started = time.perf_counter()
        try:
            passed, actual = await CHECKS[scenario["case_id"]]()
            assertion = scenario["expected"]
        except Exception as exc:  # evaluator must report every case
            passed, actual, assertion = False, f"{type(exc).__name__}: {exc}", scenario["expected"]
        results.append(
            CaseResult(
                case_id=scenario["case_id"],
                category=scenario["category"],
                passed=passed,
                duration_ms=round((time.perf_counter() - started) * 1000),
                assertion=assertion,
                actual=str(actual),
            )
        )
    passed = sum(result.passed for result in results)
    ordered_durations = sorted(result.duration_ms for result in results)
    p95_index = max(0, min(len(ordered_durations) - 1, round(len(ordered_durations) * 0.95) - 1))
    veto_ids = {
        "catalog-001",
        "catalog-002",
        "match-001",
        "match-002",
        "hitl-001",
        "hitl-002",
        "hitl-003",
        "hitl-004",
        "safety-002",
        "harness-001",
        "harness-002",
        "harness-003",
        "agent-001",
        "agent-002",
        "agent-003",
        "agent-004",
        "agent-005",
        "agent-006",
        "agent-007",
        "database-001",
        "database-002",
        "database-003",
        "memory-001",
        "memory-002",
        "memory-003",
        "memory-004",
        "memory-005",
        "memory-006",
        "knowledge-001",
        "knowledge-002",
        "knowledge-003",
        "knowledge-004",
        "replay-001",
    }
    match_metrics = grade_catalog_matching()
    report = {
        "status": "passed" if passed == len(results) else "failed",
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "deterministic-fixture",
        "total": len(results),
        "passed": passed,
        "task_success_rate": round(passed / len(results), 4),
        "case_p95_ms": ordered_durations[p95_index],
        "veto_failures": [
            result.case_id for result in results if result.case_id in veto_ids and not result.passed
        ],
        "metrics": {
            **match_metrics,
            "money_exact_match": 1.0
            if next(item for item in results if item.case_id == "catalog-001").passed
            else 0.0,
            "ram_hard_constraint_failures": 0
            if next(item for item in results if item.case_id == "catalog-002").passed
            else 1,
            "unauthorized_write_count": 0
            if next(item for item in results if item.case_id == "hitl-001").passed
            else 1,
            "duplicate_draft_count": 0
            if next(item for item in results if item.case_id == "hitl-002").passed
            else 1,
            "prompt_injection_write_count": 0
            if next(item for item in results if item.case_id == "safety-002").passed
            else 1,
            "replay_pass_rate": 1.0
            if next(item for item in results if item.case_id == "replay-001").passed
            else 0.0,
            "clarification_case_pass_rate": sum(
                item.passed for item in results if item.category == "clarification"
            )
            / 5,
            "policy_case_pass_rate": sum(
                item.passed for item in results if item.category == "policy"
            )
            / 3,
            "partial_failure_return_rate": 1.0
            if next(item for item in results if item.case_id == "safety-001").passed
            else 0.0,
            "selected_candidate_reference_presence_rate": 1.0
            if next(item for item in results if item.case_id == "catalog-003").passed
            else 0.0,
            "state_transition_validity_rate": 1.0
            if next(item for item in results if item.case_id == "harness-001").passed
            else 0.0,
            "budget_enforcement_rate": 1.0
            if next(item for item in results if item.case_id == "harness-002").passed
            else 0.0,
            "retry_circuit_policy_pass_rate": 1.0
            if next(item for item in results if item.case_id == "harness-003").passed
            else 0.0,
            "agent_tool_contract_pass_rate": 1.0
            if next(item for item in results if item.case_id == "agent-001").passed
            else 0.0,
            "agent_grounded_tool_pass_rate": 1.0
            if next(item for item in results if item.case_id == "agent-002").passed
            else 0.0,
            "agent_multiturn_context_pass_rate": 1.0
            if next(item for item in results if item.case_id == "agent-003").passed
            else 0.0,
            "agent_validated_recommendation_pass_rate": 1.0
            if next(item for item in results if item.case_id == "agent-004").passed
            else 0.0,
            "agent_candidate_veto_pass_rate": 1.0
            if next(item for item in results if item.case_id == "agent-005").passed
            else 0.0,
            "agent_trajectory_contract_pass_rate": 1.0
            if next(item for item in results if item.case_id == "agent-006").passed
            else 0.0,
            "agent_duplicate_call_detection_rate": 1.0
            if next(item for item in results if item.case_id == "agent-007").passed
            else 0.0,
            "database_round_trip_pass_rate": 1.0
            if next(item for item in results if item.case_id == "database-001").passed
            else 0.0,
            "database_tenant_scope_pass_rate": 1.0
            if next(item for item in results if item.case_id == "database-002").passed
            else 0.0,
            "database_optimistic_lock_pass_rate": 1.0
            if next(item for item in results if item.case_id == "database-003").passed
            else 0.0,
            "memory_personal_scope_pass_rate": 1.0
            if next(item for item in results if item.case_id == "memory-001").passed
            else 0.0,
            "memory_team_scope_pass_rate": 1.0
            if next(item for item in results if item.case_id == "memory-002").passed
            else 0.0,
            "memory_lifecycle_audit_pass_rate": 1.0
            if next(item for item in results if item.case_id == "memory-003").passed
            else 0.0,
            "memory_candidate_confirmation_pass_rate": 1.0
            if next(item for item in results if item.case_id == "memory-004").passed
            else 0.0,
            "memory_candidate_safety_filter_pass_rate": 1.0
            if next(item for item in results if item.case_id == "memory-005").passed
            else 0.0,
            "memory_candidate_tenant_idempotency_pass_rate": 1.0
            if next(item for item in results if item.case_id == "memory-006").passed
            else 0.0,
            "knowledge_citation_pass_rate": 1.0
            if next(item for item in results if item.case_id == "knowledge-001").passed
            else 0.0,
            "knowledge_tenant_effective_filter_pass_rate": 1.0
            if next(item for item in results if item.case_id == "knowledge-002").passed
            else 0.0,
            "knowledge_policy_version_veto_pass_rate": 1.0
            if next(item for item in results if item.case_id == "knowledge-003").passed
            else 0.0,
            "knowledge_engine_grounding_pass_rate": 1.0
            if next(item for item in results if item.case_id == "knowledge-004").passed
            else 0.0,
        },
        "results": [asdict(result) for result in results],
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    LATEST_REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (REPORT_DIR / "latest.html").write_text(_render_html(report), encoding="utf-8")
    return report


def _render_html(report: dict) -> str:
    rows = "".join(
        f"<tr><td>{item['case_id']}</td><td>{item['category']}</td><td>{'PASS' if item['passed'] else 'FAIL'}</td><td>{item['duration_ms']} ms</td><td>{item['actual']}</td></tr>"
        for item in report["results"]
    )
    metric_rows = "".join(
        f"<tr><td>{name}</td><td>{value}</td></tr>" for name, value in report["metrics"].items()
    )
    return f"""<!doctype html><meta charset='utf-8'><title>ProcurePilot Eval</title>
<style>body{{font-family:ui-sans-serif;background:#f3efe5;color:#1c2924;padding:40px}}table{{border-collapse:collapse;width:100%;background:#fffdf7}}th,td{{padding:10px;border:1px solid #d8d0c0;text-align:left}}th{{background:#1c2924;color:#f3efe5}}</style>
<h1>ProcurePilot Phase 1 Evaluation</h1><p>{report["passed"]}/{report["total"]} passed · status: {report["status"]}</p>
<h2>Deterministic fixture metrics</h2><table><thead><tr><th>Metric</th><th>Value</th></tr></thead><tbody>{metric_rows}</tbody></table>
<h2>Scenario results</h2>
<table><thead><tr><th>Case</th><th>Category</th><th>Result</th><th>Duration</th><th>Actual</th></tr></thead><tbody>{rows}</tbody></table>"""


def load_latest_report() -> dict:
    return json.loads(LATEST_REPORT.read_text(encoding="utf-8"))


def main() -> None:
    report = asyncio.run(run_evaluation())
    print(
        json.dumps(
            {key: report[key] for key in ("status", "total", "passed", "metrics")},
            ensure_ascii=False,
            indent=2,
        )
    )
    raise SystemExit(0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
