import json
from pathlib import Path

import pytest

from procure_pilot.evals.runner import run_evaluation


@pytest.mark.asyncio
async def test_phase_one_evaluation_gate():
    report = await run_evaluation()
    assert report["status"] == "passed"
    assert report["passed"] == 26
    assert report["veto_failures"] == []
    assert report["metrics"]["money_exact_match"] == 1.0
    assert report["metrics"]["unauthorized_write_count"] == 0
    assert report["metrics"]["product_match_precision"] == 1.0
    assert report["metrics"]["variant_match_precision"] == 1.0
    assert report["metrics"]["false_merge_count"] == 0
    assert report["metrics"]["conflict_veto_count"] == 2


def test_every_scenario_has_requirement_or_acceptance_traceability():
    scenario_file = Path(__file__).resolve().parents[1] / "evals" / "scenarios" / "phase1.json"
    scenarios = json.loads(scenario_file.read_text(encoding="utf-8"))

    assert len(scenarios) == 26
    assert len({scenario["case_id"] for scenario in scenarios}) == len(scenarios)
    assert all(
        scenario.get("requirement_ids") or scenario.get("acceptance_refs") for scenario in scenarios
    )
