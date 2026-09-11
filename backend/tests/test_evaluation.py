import json
from pathlib import Path

import pytest

from procure_pilot.evals.runner import run_evaluation


@pytest.mark.asyncio
async def test_phase_one_evaluation_gate():
    report = await run_evaluation()
    assert report["status"] == "passed"
    assert report["passed"] == 51
    assert report["veto_failures"] == []
    assert report["metrics"]["money_exact_match"] == 1.0
    assert report["metrics"]["unauthorized_write_count"] == 0
    assert report["metrics"]["product_match_precision"] == 1.0
    assert report["metrics"]["variant_match_precision"] == 1.0
    assert report["metrics"]["false_merge_count"] == 0
    assert report["metrics"]["conflict_veto_count"] == 2
    assert report["metrics"]["memory_personal_scope_pass_rate"] == 1.0
    assert report["metrics"]["memory_team_scope_pass_rate"] == 1.0
    assert report["metrics"]["memory_lifecycle_audit_pass_rate"] == 1.0
    assert report["metrics"]["memory_candidate_confirmation_pass_rate"] == 1.0
    assert report["metrics"]["memory_candidate_safety_filter_pass_rate"] == 1.0
    assert report["metrics"]["memory_candidate_tenant_idempotency_pass_rate"] == 1.0
    assert report["metrics"]["langgraph_interrupt_resume_pass_rate"] == 1.0
    assert report["metrics"]["langgraph_rehydration_tenant_pass_rate"] == 1.0
    assert report["metrics"]["langgraph_pending_resume_pass_rate"] == 1.0
    assert report["metrics"]["langgraph_kill_restart_write_safety_rate"] == 1.0


def test_every_scenario_has_requirement_or_acceptance_traceability():
    scenario_file = Path(__file__).resolve().parents[1] / "evals" / "scenarios" / "phase1.json"
    scenarios = json.loads(scenario_file.read_text(encoding="utf-8"))

    assert len(scenarios) == 51
    assert len({scenario["case_id"] for scenario in scenarios}) == len(scenarios)
    assert all(
        scenario.get("requirement_ids") or scenario.get("acceptance_refs") for scenario in scenarios
    )
