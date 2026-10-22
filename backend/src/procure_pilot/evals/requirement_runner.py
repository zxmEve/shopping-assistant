"""Versioned Requirement boundary experiments; offline by default, live is opt-in."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import html
import importlib.metadata
import json
import math
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from uuid import uuid4

from procure_pilot.agents.contracts import AgentKind, AgentTask, AgentTaskEnvelope
from procure_pilot.agents.multi_agent import AGENT_TOOL_ALLOWLIST
from procure_pilot.agents.provider_adapter import (
    AGENT_INSTRUCTIONS,
    OpenAICompatibleTwoAgentAdapter,
    ProviderRequirementResult,
    _instructions_for_provider,
)
from procure_pilot.agents.tools import AgentRuntimeContext
from procure_pilot.domain.models import Message, ProcurementRun
from procure_pilot.domain.requirements import RequirementSpec
from procure_pilot.evals.requirements import (
    SCORER_VERSION,
    load_dataset,
    score_requirement,
    summarize,
)
from procure_pilot.evals.runner import make_engine
from procure_pilot.services.requirement_validation import (
    RequirementCompletenessPolicy,
    RequirementSpecService,
)

BACKEND_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATASET = BACKEND_ROOT / "evals/datasets/requirement_baseline_v1.json"
DEFAULT_REPORTS = BACKEND_ROOT / "evals/reports/requirements"
BASE_MESSAGE = "采购20台笔记本，单台预算8000元含税，送上海，最低16GB内存，最低1TB硬盘"


def digest(value) -> str:
    data = (
        value
        if isinstance(value, bytes)
        else json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()
    )
    return hashlib.sha256(data).hexdigest()


def make_base(run_id: str) -> RequirementSpec:
    """Hand-authored confirmed snapshot, independent of the parser under test."""
    evidence = []

    def sourced(quote, value):
        evidence_id = f"base_ev_{len(evidence)}"
        start = BASE_MESSAGE.index(quote)
        evidence.append(
            dict(
                evidence_id=evidence_id,
                message_id="msg_base",
                quote=quote,
                start=start,
                end=start + len(quote),
                kind="USER",
            )
        )
        return dict(
            value=quote,
            normalized_value=value,
            confidence=1,
            evidence_ids=[evidence_id],
        )

    category = sourced("笔记本", "laptop")
    quantity = sourced("20台", 20)
    budget = sourced("单台预算8000元", dict(amount="8000", currency="CNY"))
    tax = sourced("含税", True)
    city = sourced("上海", "Shanghai")
    constraints = []
    for field, quote, value in [
        ("minimum_ram_gb", "最低16GB内存", 16),
        ("minimum_storage_gb", "最低1TB硬盘", 1024),
    ]:
        item = sourced(quote, value)
        constraints.append(
            dict(
                constraint_id=f"base_{field}",
                field=field,
                operator="GTE",
                value=value,
                strength="HARD",
                confidence=1,
                evidence_ids=item["evidence_ids"],
            )
        )
    return RequirementSpec.model_validate(
        dict(
            spec_id="reqspec_eval_base",
            run_id=run_id,
            authority="VALIDATED",
            base_version=2,
            version=3,
            source_message_id="msg_base",
            category=category,
            quantity=quantity,
            budget=dict(maximum=budget, tax_included=tax),
            delivery_city=city,
            hard_constraints=constraints,
            evidence=evidence,
            delta=dict(from_version=2, to_version=3, operations=[]),
            status="READY_FOR_SOURCING",
        )
    )


def _manifest(dataset_path, cases, mode, provider, model, prompt, repetitions):
    source_hashes = {
        str(path.relative_to(BACKEND_ROOT)): digest(path.read_bytes())
        for root in ("src", "config", "fixtures")
        for path in sorted((BACKEND_ROOT / root).rglob("*"))
        if path.is_file() and path.suffix in {".py", ".json", ".yaml", ".yml"}
    }
    commit = (
        subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=BACKEND_ROOT,
            text=True,
            capture_output=True,
            check=False,
        ).stdout.strip()
        or "unavailable"
    )
    packages = {}
    for name in ("pydantic", "langgraph", "openai", "openai-agents"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = "unavailable"
    settings = dict(
        provider=provider if mode == "live" else None,
        model=model if mode == "live" else None,
        temperature=0 if provider == "deepseek" and mode == "live" else None,
        max_tokens=4000 if provider == "deepseek" and mode == "live" else None,
        max_model_requests=4,
        max_tool_calls=4,
        parallel_tool_calls=False,
        verbosity="low" if provider == "openai" and mode == "live" else None,
        client_retry_policy="SDK default, pinned by package_versions",
        context_policy="project default; empty memory store",
        tools="registered project tools; no reference authorization",
        validation_fallback="current deterministic proposal+validation",
        output_repair="existing provider adapter behavior",
    )
    return dict(
        git_commit=commit,
        source_tree_sha256=digest(source_hashes),
        source_files=source_hashes,
        package_versions=packages,
        dataset_sha256=digest(dataset_path.read_bytes()),
        selected_case_ids=[case.case_id for case in cases],
        repetitions=repetitions,
        scorer_version=SCORER_VERSION,
        evaluator_sha256=digest(
            {
                name: digest((Path(__file__).parent / name).read_bytes())
                for name in ("requirements.py", "requirement_runner.py")
            }
        ),
        mode=mode,
        settings=settings,
        settings_sha256=digest(settings),
        prompt_sha256=digest(prompt.encode()),
        output_schema_sha256=digest(ProviderRequirementResult.model_json_schema()),
        base_fixture_sha256=digest(make_base("fixed").model_dump(mode="json")),
    )


def _distribution(values):
    ordered = sorted(values)
    return dict(mean_ms=mean(ordered), p95_ms=ordered[math.ceil(len(ordered) * 0.95) - 1])


async def run_requirement_evaluation(
    *,
    dataset_path: Path = DEFAULT_DATASET,
    output_root: Path = DEFAULT_REPORTS,
    mode: str = "offline",
    split: str = "all",
    repetitions: int = 1,
    prompt: str | None = None,
    adapter=None,
    provider: str = "deepseek",
    model: str = "",
    endpoint_hash: str | None = None,
) -> tuple[dict, Path]:
    if mode not in {"offline", "live"} or repetitions < 1:
        raise ValueError("Invalid mode or repetition count")
    if mode == "live" and adapter is None:
        raise ValueError("Live evaluation requires an explicitly configured model adapter")
    if mode == "offline" and prompt is not None:
        raise ValueError("Offline parser does not execute a Prompt; do not use it for Prompt A/B")
    dataset = load_dataset(dataset_path)
    cases = [case for case in dataset.cases if split == "all" or case.split == split]
    if not cases:
        raise ValueError("No selected cases")
    effective_prompt = prompt if prompt is not None else AGENT_INSTRUCTIONS[AgentKind.REQUIREMENT]
    if mode == "live" and prompt is not None:
        if not hasattr(adapter, "agents"):
            raise ValueError(
                "Prompt override requires an adapter exposing its actual Agent instructions"
            )
        adapter.agents[AgentKind.REQUIREMENT] = adapter.agents[AgentKind.REQUIREMENT].clone(
            instructions=_instructions_for_provider(
                prompt, provider=provider, result_model=ProviderRequirementResult
            )
        )
    if mode == "live" and hasattr(adapter, "agents"):
        actual_instructions = adapter.agents[AgentKind.REQUIREMENT].instructions
        if not isinstance(actual_instructions, str):
            raise ValueError("This evaluator requires static, inspectable Agent instructions")
        effective_prompt = actual_instructions
    manifest = _manifest(dataset_path, cases, mode, provider, model, effective_prompt, repetitions)
    manifest["endpoint_sha256"] = endpoint_hash
    experiment_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:10]
    artifact_dir = output_root / experiment_id
    artifact_dir.mkdir(parents=True, exist_ok=False)
    (artifact_dir / "prompt.txt").write_text(effective_prompt, encoding="utf-8")
    service = RequirementSpecService()
    results = []
    for case in cases:
        for repetition in range(1, repetitions + 1):
            run_id = f"eval_{case.case_id}"
            base = make_base(run_id) if case.base_fixture else None
            messages = {"msg_current": case.message}
            if base:
                messages["msg_base"] = BASE_MESSAGE
            proposed = None
            final = None
            failure = None
            validation_error = None
            fallback_used = False
            provider_output = None
            usage = None
            started = time.perf_counter()
            proposal_started = started
            proposal_ms = 0.0
            envelope_json = None
            if mode == "live":
                # Clear per-attempt pointers so a failure cannot borrow prior usage.
                adapter.last_execution = None
                adapter.last_model_output = []
            try:
                if mode == "offline":
                    proposed = service.propose(
                        run_id=run_id,
                        message_id="msg_current",
                        text=case.message,
                        base=base,
                    )
                else:
                    engine = make_engine()
                    run = ProcurementRun(run_id=run_id, tenant_id="demo_cn", user_id="employee_001")
                    engine.harness.initialize_run(run)
                    if base:
                        run.requirement_spec = base.model_copy(deep=True)
                        run.requirement = service.to_procurement_requirement(base)
                        run.messages.append(
                            Message(message_id="msg_base", role="user", content=BASE_MESSAGE)
                        )
                    run.messages.append(
                        Message(message_id="msg_current", role="user", content=case.message)
                    )
                    context = AgentRuntimeContext(
                        run=run,
                        catalog=engine.catalog,
                        enterprise=engine.enterprise,
                        harness=engine.harness,
                        tool_gateway=engine.tool_gateway,
                        model_gateway=engine.model_gateway,
                        agent_kind="REQUIREMENT",
                        roles=engine.enterprise.identity(run.tenant_id, run.user_id).roles,
                    )
                    assembled = engine.context_assembler.assemble(
                        agent=AgentKind.REQUIREMENT,
                        run=run,
                        team_id=engine.enterprise.identity(run.tenant_id, run.user_id).department,
                    )
                    envelope = AgentTaskEnvelope(
                        run_id=run_id,
                        state_version=run.state_version,
                        task=AgentTask(
                            task_id="eval_requirement",
                            agent=AgentKind.REQUIREMENT,
                            objective="Produce one evidence-grounded requirement delta.",
                            max_model_requests=4,
                            max_tool_calls=4,
                        ),
                        requirement=run.requirement.model_dump(mode="json"),
                        requirement_spec=base,
                        context=assembled.payload,
                        source_message_id="msg_current",
                        missing_fields=[item.field for item in base.missing_fields] if base else [],
                        requirement_schema=RequirementCompletenessPolicy.schema_snapshot(),
                    )
                    envelope_json = envelope.model_dump(mode="json")
                    call = await adapter.execute_agent(
                        envelope,
                        context=context,
                        allowed_tools=AGENT_TOOL_ALLOWLIST[AgentKind.REQUIREMENT],
                    )
                    usage = dict(
                        input_tokens=call.usage.input_tokens,
                        output_tokens=call.usage.output_tokens,
                        model_requests=call.usage.requests,
                        cost_micro_usd=call.usage.cost_micro_usd,
                        output_repaired=call.usage.output_repaired,
                    )
                    proposed = call.output.requirement_spec
                    if proposed is None:
                        raise ValueError("Provider omitted RequirementSpec")
                proposal_ms = (time.perf_counter() - proposal_started) * 1000
                # validate mutates the input; preserve the original semantic proposal.
                try:
                    final = service.validate(
                        proposed.model_copy(deep=True),
                        message_id="msg_current",
                        text=case.message,
                        base=base,
                    )
                except ValueError as exc:
                    validation_error = str(exc)
                    fallback_used = True
                    replacement = service.propose(
                        run_id=run_id,
                        message_id="msg_current",
                        text=case.message,
                        base=base,
                    )
                    final = service.validate(
                        replacement,
                        message_id="msg_current",
                        text=case.message,
                        base=base,
                    )
            except Exception as exc:
                failure = type(exc).__name__
            finally:
                if mode == "live":
                    provider_output = getattr(adapter, "last_model_output", [])
                    if usage is None:
                        key = getattr(adapter, "last_execution", None)
                        captured = getattr(adapter, "usage_by_execution", {}).get(key)
                        if captured is not None:
                            usage = dict(
                                input_tokens=captured.input_tokens,
                                output_tokens=captured.output_tokens,
                                model_requests=captured.requests,
                                cost_micro_usd=captured.cost_micro_usd,
                                output_repaired=captured.output_repaired,
                            )
            result = dict(
                case_id=case.case_id,
                group_id=case.group_id,
                split=case.split,
                category=case.category,
                repetition=repetition,
                input_sha256=digest(
                    dict(
                        messages=messages,
                        base=base.model_dump(mode="json") if base else None,
                    )
                ),
                input_messages=messages,
                envelope=envelope_json,
                proposal=score_requirement(
                    case,
                    proposed,
                    messages=messages,
                    base=base,
                    error="execution_failed" if failure else None,
                    expected_authority="PROPOSED",
                ),
                final=score_requirement(
                    case,
                    final,
                    messages=messages,
                    base=base,
                    error="execution_failed" if failure else None,
                    expected_authority="VALIDATED",
                ),
                proposal_spec=proposed.model_dump(mode="json") if proposed else None,
                final_spec=final.model_dump(mode="json") if final else None,
                provider_output=provider_output,
                fallback_used=fallback_used,
                validation_error=validation_error,
                error_type=failure,
                usage=usage,
                usage_complete=(mode == "live" and failure is None and usage is not None),
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                proposal_duration_ms=round(proposal_ms, 3),
            )
            results.append(result)
    known_usage = [item["usage"] for item in results if item["usage"] is not None]
    usage_summary = dict(
        measured=(mode == "live"),
        known_runs=len(known_usage),
        incomplete_runs=sum(not item["usage_complete"] for item in results)
        if mode == "live"
        else 0,
        input_tokens_total_known=sum(item["input_tokens"] for item in known_usage)
        if known_usage
        else None,
        output_tokens_total_known=sum(item["output_tokens"] for item in known_usage)
        if known_usage
        else None,
        estimated_cost_micro_usd_total_known=sum(item["cost_micro_usd"] for item in known_usage)
        if known_usage
        else None,
        model_requests_total_known=sum(item["model_requests"] for item in known_usage)
        if known_usage
        else None,
        total_tokens_mean_known=mean(
            item["input_tokens"] + item["output_tokens"] for item in known_usage
        )
        if known_usage
        else None,
    )
    report = dict(
        report_version="requirement-report-v1",
        experiment_id=experiment_id,
        generated_at=datetime.now(UTC).isoformat(),
        mode=mode,
        scope=dataset.scope,
        label_status=dataset.label_status,
        release_evidence_eligible=dataset.label_status == "business_reviewed" and mode == "live",
        limitations=[
            "Requirement boundary only, not a full workflow or reference/tool quality evaluation.",
            "Proposal is after adapter parsing/metadata normalization, before domain validation; provider wire output is saved separately.",
            "Purpose and delivery preference strength are not scored in v1.",
            "No frozen provider clock; this suite excludes relative-date and reference lookups.",
            "Offline results measure the deterministic parser and validator, not an LLM.",
        ],
        manifest=manifest,
        case_count=len(cases),
        repetitions=repetitions,
        status="passed" if all(item["final"]["passed"] for item in results) else "failed",
        proposal=summarize(results, "proposal"),
        final=summarize(results, "final"),
        by_category={
            category: {
                phase: summarize([item for item in results if item["category"] == category], phase)
                for phase in ("proposal", "final")
            }
            for category in sorted({case.category for case in cases})
        },
        by_split={
            selected_split: {
                phase: summarize(
                    [item for item in results if item["split"] == selected_split], phase
                )
                for phase in ("proposal", "final")
            }
            for selected_split in sorted({case.split for case in cases})
        },
        fallback_count=sum(item["fallback_used"] for item in results),
        latency=_distribution([item["duration_ms"] for item in results]),
        usage=usage_summary,
        results=results,
    )
    path = artifact_dir / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rows = "".join(
        f"<tr><td>{html.escape(item['case_id'])}</td><td>{item['repetition']}</td>"
        f"<td>{item['proposal']['passed']}</td><td>{item['final']['passed']}</td>"
        f"<td><pre>{html.escape(json.dumps(item['final'], ensure_ascii=False, indent=2))}</pre></td></tr>"
        for item in results
    )
    page = (
        "<!doctype html><meta charset='utf-8'><title>Requirement evaluation</title>"
        "<style>body{font:15px system-ui;max-width:1200px;margin:30px auto}td,th{border:1px solid #ccc;padding:8px;vertical-align:top}table{border-collapse:collapse}pre{white-space:pre-wrap}</style>"
        f"<h1>{html.escape(experiment_id)}</h1><p>Mode: {mode}; labels: {dataset.label_status}; "
        "offline is NOT model accuracy. Proposal is before domain validation.</p>"
        f"<pre>{html.escape(json.dumps({key: report[key] for key in ('status', 'proposal', 'final', 'usage', 'fallback_count')}, ensure_ascii=False, indent=2))}</pre>"
        "<table><tr><th>Case</th><th>Repeat</th><th>Proposal</th><th>Final</th><th>Final details</th></tr>"
        + rows
        + "</table>"
    )
    (artifact_dir / "report.html").write_text(page, encoding="utf-8")
    return report, path


def compare_reports(baseline: dict, candidate: dict) -> dict:
    if (
        baseline.get("report_version") != "requirement-report-v1"
        or candidate.get("report_version") != "requirement-report-v1"
    ):
        raise ValueError("Unsupported report version")
    left, right = baseline["manifest"], candidate["manifest"]
    required = (
        "mode",
        "dataset_sha256",
        "selected_case_ids",
        "repetitions",
        "scorer_version",
        "evaluator_sha256",
        "output_schema_sha256",
        "base_fixture_sha256",
        "settings_sha256",
        "endpoint_sha256",
        "package_versions",
    )
    incompatible = [key for key in required if left.get(key) != right.get(key)]
    if incompatible:
        raise ValueError("Incomparable reports: " + ", ".join(incompatible))

    def index(report):
        return {(item["case_id"], item["repetition"]): item for item in report["results"]}

    old, new = index(baseline), index(candidate)
    if old.keys() != new.keys() or any(
        old[key]["input_sha256"] != new[key]["input_sha256"] for key in old
    ):
        raise ValueError("Incomparable case inputs or repetitions")
    regressions = {
        phase: [
            dict(case_id=key[0], repetition=key[1])
            for key in old
            if old[key][phase]["passed"] and not new[key][phase]["passed"]
        ]
        for phase in ("proposal", "final")
    }
    # Gate both semantic counts and operational issues; an already-failing case
    # becoming worse must not be hidden by a case-level pass/fail comparison.
    degraded = []
    for key in old:
        for phase in ("proposal", "final"):
            for metric in ("omission_count", "wrong_count", "addition_count"):
                if new[key][phase][metric] > old[key][phase][metric]:
                    degraded.append(
                        dict(
                            case_id=key[0],
                            repetition=key[1],
                            phase=phase,
                            metric=metric,
                        )
                    )
            if set(new[key][phase]["issues"]) - set(old[key][phase]["issues"]):
                degraded.append(
                    dict(
                        case_id=key[0],
                        repetition=key[1],
                        phase=phase,
                        metric="new_issue",
                    )
                )
    factors = [key for key in ("source_tree_sha256", "prompt_sha256") if left[key] != right[key]]
    usage_comparable = (
        baseline["mode"] == "live"
        and baseline["usage"]["incomplete_runs"] == 0
        and candidate["usage"]["incomplete_runs"] == 0
    )
    return dict(
        status="regressed" if any(regressions.values()) or degraded else "no_regression_observed",
        changed_factors=factors,
        prompt_only=(factors == ["prompt_sha256"]),
        regressions=regressions,
        degraded_checks=degraded,
        note="Paired descriptive comparison; repeated samples are not independent evidence of production quality.",
        token_mean_delta=(
            candidate["usage"]["total_tokens_mean_known"]
            - baseline["usage"]["total_tokens_mean_known"]
            if usage_comparable
            else None
        ),
        quality_delta={
            phase: {
                metric: candidate[phase][metric] - baseline[phase][metric]
                for metric in (
                    "case_exact_match_rate",
                    "omission_rate",
                    "wrong_extraction_rate",
                )
            }
            for phase in ("proposal", "final")
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    run.add_argument("--output-dir", type=Path, default=DEFAULT_REPORTS)
    run.add_argument("--mode", choices=("offline", "live"), default="offline")
    run.add_argument("--split", choices=("all", "development", "holdout"), default="development")
    run.add_argument("--repetitions", type=int, default=1)
    run.add_argument("--prompt-file", type=Path)
    compare = commands.add_parser("compare")
    compare.add_argument("baseline", type=Path)
    compare.add_argument("candidate", type=Path)
    args = parser.parse_args()
    if args.command == "compare":
        try:
            result = compare_reports(
                json.loads(args.baseline.read_text()),
                json.loads(args.candidate.read_text()),
            )
        except ValueError as exc:
            parser.error(str(exc))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(1 if result["status"] == "regressed" else 0)
    if args.repetitions < 1:
        parser.error("--repetitions must be positive")
    if args.mode == "offline" and args.prompt_file:
        parser.error("--prompt-file requires --mode live; offline does not execute prompts")
    prompt = args.prompt_file.read_text(encoding="utf-8") if args.prompt_file else None
    if prompt is not None and not prompt.strip():
        parser.error("Prompt must not be empty")
    adapter = None
    provider, model, endpoint_hash = "deepseek", "", None
    if args.mode == "live":
        from procure_pilot.config import Settings

        settings = Settings()
        if not settings.openai_api_key:
            parser.error("Live mode requires PROCURE_OPENAI_API_KEY; no requests were sent")
        provider = settings.model_provider
        model = settings.resolved_agent_models["requirement"]
        adapter = OpenAICompatibleTwoAgentAdapter(
            api_key=settings.openai_api_key,
            provider=provider,
            base_url=settings.openai_base_url,
            requirement_model=model,
            sourcing_model=model,
        )
        endpoint_hash = digest((settings.openai_base_url or f"default:{provider}").encode())
    report, path = asyncio.run(
        run_requirement_evaluation(
            dataset_path=args.dataset,
            output_root=args.output_dir,
            mode=args.mode,
            split=args.split,
            repetitions=args.repetitions,
            prompt=prompt,
            adapter=adapter,
            provider=provider,
            model=model,
            endpoint_hash=endpoint_hash,
        )
    )
    print(
        json.dumps(
            {
                "report": str(path),
                **{
                    key: report[key]
                    for key in (
                        "mode",
                        "status",
                        "case_count",
                        "proposal",
                        "final",
                        "usage",
                    )
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    raise SystemExit(0 if report["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
