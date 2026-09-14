from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean

from procure_pilot.agents.multi_agent import TwoAgentPlanner
from procure_pilot.agents.provider_adapter import OpenAICompatibleTwoAgentAdapter
from procure_pilot.config import Settings
from procure_pilot.domain.models import RunState
from procure_pilot.evals.runner import COMPLETE_REQUEST
from procure_pilot.evals.trajectory import grade_agent_trajectory
from procure_pilot.fixtures.catalog import FixtureCatalogAdapter
from procure_pilot.fixtures.enterprise import FixtureEnterpriseGateway
from procure_pilot.harness.runtime import AgentHarness, RunRepository
from procure_pilot.services.engine import ProcurementEngine
from procure_pilot.services.model_pricing import estimate_model_cost

PROJECT_ROOT = Path(__file__).resolve().parents[3]
LIVE_REPORT_DIR = PROJECT_ROOT / "evals" / "reports" / "live"
LIVE_S3_SUITE = PROJECT_ROOT / "evals" / "scenarios" / "live_s3.json"


@dataclass(frozen=True)
class LiveSmokeCase:
    case_id: str
    category: str
    message: str
    action: str
    expected_final_state: str
    scenario: str | None = None
    require_approval_route: bool = False


def load_live_smoke_cases(path: Path = LIVE_S3_SUITE) -> list[LiveSmokeCase]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = [LiveSmokeCase(**item) for item in payload]
    if len(cases) != 12 or len({item.case_id for item in cases}) != 12:
        raise ValueError("S3 live smoke suite must contain exactly 12 unique cases")
    return cases


def _percentile(values: list[int], percentile: float) -> int:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round(len(ordered) * percentile) - 1))
    return ordered[index]


def _captured_model_usage(planner: object) -> dict[str, int]:
    """Return provider usage even when the workflow fails before a RunResponse exists."""
    adapter = getattr(planner, "adapter", None)
    last_execution = getattr(adapter, "last_execution", None)
    usage_by_execution = getattr(adapter, "usage_by_execution", {})
    if not last_execution or not isinstance(usage_by_execution, dict):
        return {"model_requests": 0, "input_tokens": 0, "output_tokens": 0}
    run_prefix = f"{last_execution.split(':', 1)[0]}:"
    usages = [
        usage
        for key, usage in usage_by_execution.items()
        if key.startswith(run_prefix)
    ]
    return {
        "model_requests": sum(item.requests for item in usages),
        "input_tokens": sum(item.input_tokens for item in usages),
        "output_tokens": sum(item.output_tokens for item in usages),
        "cost_micro_usd": sum(item.cost_micro_usd for item in usages),
        "structured_output_repairs": sum(item.output_repaired for item in usages),
    }


def _build_live_planner(
    *,
    model: str,
    api_key: str,
    provider: str,
    base_url: str | None,
    topology: str,
    orchestrator_model: str | None,
    specialist_model: str | None,
    verifier_model: str | None,
):
    if topology in {"two_agent", "explicit_multi_agent", "direct_tools"}:
        return TwoAgentPlanner(
            OpenAICompatibleTwoAgentAdapter(
                api_key=api_key,
                provider=provider,
                base_url=base_url,
                requirement_model=orchestrator_model or specialist_model or model,
                sourcing_model=specialist_model or model,
            ),
            fallback_on_error=False,
        )
    raise ValueError(f"Unsupported live evaluation topology: {topology}")


async def run_live_evaluation(
    *,
    model: str,
    api_key: str,
    runs: int,
    provider: str = "openai",
    base_url: str | None = None,
    topology: str = "two_agent",
    orchestrator_model: str | None = None,
    specialist_model: str | None = None,
    verifier_model: str | None = None,
    harness_options: dict[str, int] | None = None,
) -> dict:
    planner = _build_live_planner(
        model=model,
        api_key=api_key,
        provider=provider,
        base_url=base_url,
        topology=topology,
        orchestrator_model=orchestrator_model,
        specialist_model=specialist_model,
        verifier_model=verifier_model,
    )
    results: list[dict] = []
    for index in range(runs):
        engine = ProcurementEngine(
            catalog=FixtureCatalogAdapter(),
            enterprise=FixtureEnterpriseGateway(),
            repository=RunRepository(),
            harness=AgentHarness(
                secret=f"live-eval-{index}",
                **(harness_options or {}),
            ),
            planner=planner,
        )
        started = time.perf_counter()
        try:
            response = await engine.start(
                tenant_id="demo_cn",
                user_id="employee_001",
                message=COMPLETE_REQUEST,
            )
            recommendation_state = response.run.state
            if response.run.state == RunState.WAITING_CONFIRMATION and response.run.action_token:
                response = await engine.confirm(
                    run_id=response.run.run_id,
                    tenant_id="demo_cn",
                    user_id="employee_001",
                    action_token=response.run.action_token,
                )
            duration_ms = round((time.perf_counter() - started) * 1000)
            grade = grade_agent_trajectory(response.run)
            recommendation = response.run.recommendation
            results.append(
                {
                    "run": index + 1,
                    "passed": (
                        recommendation_state == RunState.WAITING_CONFIRMATION
                        and response.run.state == RunState.DRAFT_CREATED
                        and response.run.draft is not None
                        and recommendation is not None
                        and recommendation.selection_source == "agent_validated"
                        and grade.passed
                    ),
                    "state": response.run.state,
                    "recommendation_state": recommendation_state,
                    "draft_id": response.run.draft.draft_id if response.run.draft else None,
                    "selection_source": (
                        recommendation.selection_source if recommendation else None
                    ),
                    "duration_ms": duration_ms,
                    "usage": response.run.execution_usage.model_dump(mode="json"),
                    "trajectory": grade.model_dump(mode="json"),
                    "trace": [
                        {
                            "actor": event.actor,
                            "kind": event.kind,
                            "name": event.name,
                            "status": event.status,
                            "detail": event.detail,
                            "duration_ms": event.duration_ms,
                        }
                        for event in response.run.trace
                    ],
                }
            )
        except Exception as exc:
            results.append(
                {
                    "run": index + 1,
                    "passed": False,
                    "duration_ms": round((time.perf_counter() - started) * 1000),
                    "error": f"{type(exc).__name__}: {exc}",
                    "last_model_output": getattr(
                        planner,
                        "last_model_output",
                        getattr(planner.adapter, "last_model_output", [])
                        if isinstance(planner, TwoAgentPlanner)
                        else [],
                    ),
                    "model_outputs_by_execution": (
                        getattr(planner.adapter, "model_outputs_by_execution", {})
                        if isinstance(planner, TwoAgentPlanner)
                        else {}
                    ),
                    "last_error": getattr(planner, "last_error", None),
                    "usage": _captured_model_usage(planner),
                }
            )

    passed = sum(item["passed"] for item in results)
    durations = [item["duration_ms"] for item in results]
    input_tokens = [item.get("usage", {}).get("input_tokens", 0) for item in results]
    output_tokens = [item.get("usage", {}).get("output_tokens", 0) for item in results]
    generated_at = datetime.now(UTC)
    cost_estimate = estimate_model_cost(
        provider=provider,
        model=model,
        input_tokens=sum(input_tokens),
        output_tokens=sum(output_tokens),
        at=generated_at,
    )
    report = {
        "status": "passed" if passed == runs else "failed",
        "generated_at": generated_at.isoformat(),
        "mode": "live-model-with-fixture-tools",
        "topology": topology,
        "provider": provider,
        "model": model,
        "runs": runs,
        "passed": passed,
        "task_success_rate": passed / runs,
        "latency_mean_ms": round(mean(durations)),
        "latency_p95_ms": _percentile(durations, 0.95),
        "input_tokens_mean": round(mean(input_tokens)),
        "output_tokens_mean": round(mean(output_tokens)),
        "cost_observed": False,
        "cost_estimate": cost_estimate.model_dump(mode="json") if cost_estimate else None,
        "cost_note": (
            "Estimated from a versioned cache-miss price table; provider invoice is authoritative."
            if cost_estimate
            else "No versioned price is registered for this provider/model."
        ),
        "results": results,
    }
    LIVE_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (LIVE_REPORT_DIR / "latest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (LIVE_REPORT_DIR / f"latest-{topology}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


async def run_live_smoke_suite(
    *,
    model: str,
    api_key: str,
    runs_per_case: int = 3,
    provider: str = "openai",
    base_url: str | None = None,
    topology: str = "two_agent",
    orchestrator_model: str | None = None,
    specialist_model: str | None = None,
    verifier_model: str | None = None,
    harness_options: dict[str, int] | None = None,
    cases: list[LiveSmokeCase] | None = None,
) -> dict:
    """Run the versioned 12-case S3 matrix; this function performs real paid model calls."""
    if runs_per_case < 1:
        raise ValueError("runs_per_case must be at least 1")
    selected_cases = cases or load_live_smoke_cases()
    results: list[dict] = []
    sequence = 0
    for case in selected_cases:
        for repetition in range(1, runs_per_case + 1):
            sequence += 1
            planner = _build_live_planner(
                model=model,
                api_key=api_key,
                provider=provider,
                base_url=base_url,
                topology=topology,
                orchestrator_model=orchestrator_model,
                specialist_model=specialist_model,
                verifier_model=verifier_model,
            )
            engine = ProcurementEngine(
                catalog=FixtureCatalogAdapter(),
                enterprise=FixtureEnterpriseGateway(),
                repository=RunRepository(),
                harness=AgentHarness(
                    secret=f"live-s3-{case.case_id}-{repetition}",
                    **(harness_options or {}),
                ),
                planner=planner,
            )
            started = time.perf_counter()
            rejection_observed = False
            try:
                response = await engine.start(
                    tenant_id="demo_cn",
                    user_id="employee_001",
                    message=case.message,
                    scenario=case.scenario,
                )
                recommendation_state = response.run.state
                if case.action == "confirm" and response.run.action_token:
                    response = await engine.confirm(
                        run_id=response.run.run_id,
                        tenant_id="demo_cn",
                        user_id="employee_001",
                        action_token=response.run.action_token,
                    )
                elif case.action == "tampered_token" and response.run.action_token:
                    try:
                        await engine.confirm(
                            run_id=response.run.run_id,
                            tenant_id="demo_cn",
                            user_id="employee_001",
                            action_token=f"{response.run.action_token}tampered",
                        )
                    except PermissionError:
                        rejection_observed = True
                run = response.run
                recommendation = run.recommendation
                expected_draft = case.expected_final_state == RunState.DRAFT_CREATED
                state_ok = run.state == case.expected_final_state
                draft_ok = bool(run.draft) == expected_draft
                token_ok = case.action != "tampered_token" or rejection_observed
                approval_ok = not case.require_approval_route or bool(
                    recommendation and recommendation.best_overall.approval_route
                )
                agent_selection_ok = (
                    recommendation is None
                    or topology not in {"two_agent", "explicit_multi_agent", "direct_tools"}
                    or recommendation.selection_source == "agent_validated"
                )
                unauthorized_writes = sum(
                    event.name == "create_requisition_draft"
                    for event in run.trace
                    if case.action != "confirm"
                )
                repaired_outputs = sum(
                    event.name == "agent.model_call_completed"
                    and bool(event.detail.get("output_repaired"))
                    for event in run.trace
                )
                passed = (
                    state_ok
                    and draft_ok
                    and token_ok
                    and approval_ok
                    and agent_selection_ok
                    and unauthorized_writes == 0
                )
                results.append(
                    {
                        "case_id": case.case_id,
                        "category": case.category,
                        "repetition": repetition,
                        "passed": passed,
                        "state": run.state,
                        "expected_state": case.expected_final_state,
                        "recommendation_state": recommendation_state,
                        "selection_source": (
                            recommendation.selection_source if recommendation else None
                        ),
                        "draft_id": run.draft.draft_id if run.draft else None,
                        "invalid_token_rejected": rejection_observed,
                        "unauthorized_writes": unauthorized_writes,
                        "repaired_outputs": repaired_outputs,
                        "duration_ms": round((time.perf_counter() - started) * 1000),
                        "usage": run.execution_usage.model_dump(mode="json"),
                        "last_error": getattr(planner, "last_error", None),
                    }
                )
            except Exception as exc:
                results.append(
                    {
                        "case_id": case.case_id,
                        "category": case.category,
                        "repetition": repetition,
                        "passed": False,
                        "duration_ms": round((time.perf_counter() - started) * 1000),
                        "error": f"{type(exc).__name__}: {exc}",
                        "last_error": getattr(planner, "last_error", None),
                        "model_outputs_by_execution": (
                            getattr(planner.adapter, "model_outputs_by_execution", {})
                            if isinstance(planner, TwoAgentPlanner)
                            else {}
                        ),
                        "usage": _captured_model_usage(planner),
                    }
                )

    generated_at = datetime.now(UTC)
    passed = sum(item["passed"] for item in results)
    durations = [item["duration_ms"] for item in results]
    input_tokens = [item.get("usage", {}).get("input_tokens", 0) for item in results]
    output_tokens = [item.get("usage", {}).get("output_tokens", 0) for item in results]
    cost_estimate = estimate_model_cost(
        provider=provider,
        model=model,
        input_tokens=sum(input_tokens),
        output_tokens=sum(output_tokens),
        at=generated_at,
    )
    report = {
        "status": "passed" if passed == len(results) else "failed",
        "generated_at": generated_at.isoformat(),
        "mode": "s3-live-smoke-with-fixture-tools",
        "provider": provider,
        "model": model,
        "topology": topology,
        "case_count": len(selected_cases),
        "runs_per_case": runs_per_case,
        "runs": len(results),
        "passed": passed,
        "task_success_rate": passed / len(results),
        "veto_failures": sum(item.get("unauthorized_writes", 0) for item in results),
        "structured_output_repairs": sum(item.get("repaired_outputs", 0) for item in results),
        "latency_mean_ms": round(mean(durations)),
        "latency_p95_ms": _percentile(durations, 0.95),
        "input_tokens_mean": round(mean(input_tokens)),
        "output_tokens_mean": round(mean(output_tokens)),
        "cost_observed": False,
        "cost_estimate": cost_estimate.model_dump(mode="json") if cost_estimate else None,
        "results": results,
    }
    LIVE_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (LIVE_REPORT_DIR / f"s3-smoke-{topology}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run live OpenAI Agent evaluation against deterministic fixture tools."
    )
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument(
        "--topology",
        choices=("two_agent", "explicit_multi_agent"),
        default=None,
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="Deprecated: the legacy five-agent/direct-tools comparison was removed.",
    )
    parser.add_argument(
        "--suite",
        choices=("single", "s3-smoke"),
        default="single",
        help="s3-smoke runs the versioned 12-case matrix; --runs is repetitions per case.",
    )
    arguments = parser.parse_args()
    if arguments.runs < 1:
        raise SystemExit("--runs must be at least 1")
    settings = Settings()
    if not settings.openai_api_key:
        raise SystemExit(
            "PROCURE_OPENAI_API_KEY is required. Live evaluation was not run and no live report was written."
        )
    topology = arguments.topology or settings.agent_topology
    if topology not in {"two_agent", "direct_tools", "explicit_multi_agent"}:
        raise SystemExit("Live evaluation topology must be two_agent")
    if arguments.compare:
        raise SystemExit("--compare was removed with the legacy topology; run the two_agent gate")
    topology = "two_agent"
    topologies = (topology,)
    reports = []
    for selected_topology in topologies:
        evaluation = run_live_smoke_suite if arguments.suite == "s3-smoke" else run_live_evaluation
        repetition_argument = (
            {"runs_per_case": arguments.runs}
            if arguments.suite == "s3-smoke"
            else {"runs": arguments.runs}
        )
        reports.append(
            asyncio.run(
                evaluation(
                    model=settings.openai_model,
                    api_key=settings.openai_api_key,
                    **repetition_argument,
                    provider=settings.model_provider,
                    base_url=settings.openai_base_url,
                    topology=selected_topology,
                    orchestrator_model=settings.resolved_agent_models["requirement"],
                    specialist_model=settings.resolved_agent_models["sourcing"],
                    verifier_model=None,
                    harness_options={
                        "max_steps": settings.max_agent_steps,
                        "max_agent_turns": settings.max_agent_turns,
                        "max_orchestrator_requests": settings.max_orchestrator_requests,
                        "max_specialist_requests": settings.max_specialist_requests,
                        "max_stage_model_requests": settings.max_stage_model_requests,
                        "max_tool_calls": settings.max_tool_calls,
                        "max_agent_read_tool_calls": settings.max_agent_read_tool_calls,
                        "max_workflow_read_tool_calls": settings.max_workflow_read_tool_calls,
                        "max_commit_tool_calls": settings.max_commit_tool_calls,
                        "max_provider_calls": settings.max_provider_calls,
                        "max_retries": settings.max_retries,
                        "max_input_tokens": settings.max_input_tokens,
                        "max_output_tokens": settings.max_output_tokens,
                        "max_cost_micro_usd": settings.max_cost_micro_usd,
                        "max_wall_time_ms": settings.max_wall_time_ms,
                        "max_parallel_provider_calls": settings.max_parallel_provider_calls,
                    },
                )
            )
        )
    report = reports[-1]
    if arguments.compare:
        comparison = {
            "generated_at": datetime.now(UTC).isoformat(),
            "provider": settings.model_provider,
            "runs_per_topology": arguments.runs,
            "reports": {
                item["topology"]: {
                    key: item[key]
                    for key in (
                        "status",
                        "task_success_rate",
                        "latency_p95_ms",
                        "input_tokens_mean",
                        "output_tokens_mean",
                        "cost_estimate",
                    )
                }
                for item in reports
            },
        }
        LIVE_REPORT_DIR.mkdir(parents=True, exist_ok=True)
        (LIVE_REPORT_DIR / "comparison-latest.json").write_text(
            json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "status",
                    "mode",
                    "topology",
                    "provider",
                    "model",
                    "runs",
                    "passed",
                    "latency_p95_ms",
                    "input_tokens_mean",
                    "output_tokens_mean",
                    "cost_observed",
                )
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if any(item["status"] != "passed" for item in reports):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
