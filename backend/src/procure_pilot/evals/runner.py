from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from procure_pilot.agents.runtime import DeterministicAgentPlanner
from procure_pilot.domain.models import (
    BudgetResource,
    BundleType,
    MatchDecisionType,
    PolicyDecision,
    ProcurementRun,
    ProductCondition,
    RunState,
)
from procure_pilot.fixtures.catalog import (
    FixtureCatalogAdapter,
    build_hard_case_raw_offers,
    build_raw_catalog,
)
from procure_pilot.fixtures.enterprise import FixtureEnterpriseGateway
from procure_pilot.harness.runtime import AgentHarness, HarnessBudgetExceeded, RunRepository
from procure_pilot.services.engine import ProcurementEngine

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
