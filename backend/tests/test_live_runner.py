from __future__ import annotations

import pytest

from procure_pilot.evals import live_runner
from procure_pilot.evals.runner import _EvidenceBackedPlanner


@pytest.mark.asyncio
async def test_live_runner_requires_confirmation_and_draft_creation(monkeypatch, tmp_path):
    monkeypatch.setattr(
        live_runner,
        "_build_live_planner",
        lambda **kwargs: _EvidenceBackedPlanner(),
    )
    monkeypatch.setattr(live_runner, "LIVE_REPORT_DIR", tmp_path)

    report = await live_runner.run_live_evaluation(
        model="fake-model",
        api_key="fake-key",
        runs=1,
    )

    result = report["results"][0]
    assert report["status"] == "passed"
    assert report["topology"] == "two_agent"
    assert result["recommendation_state"] == "WAITING_CONFIRMATION"
    assert result["state"] == "DRAFT_CREATED"
    assert result["draft_id"].startswith("draft_")
    assert result["usage"]["commit_tool_calls"] == 3
    assert result["usage"]["reserved_commit_tool_calls"] == 0


def test_s3_live_suite_is_versioned_as_twelve_balanced_cases():
    cases = live_runner.load_live_smoke_cases()

    assert len(cases) == 12
    assert {case.category for case in cases} == {
        "complete_requirement",
        "clarification",
        "tool_fault",
        "safety",
        "policy",
        "state_recovery",
    }
    assert all(
        sum(case.category == category for case in cases) == 2
        for category in {case.category for case in cases}
    )


@pytest.mark.asyncio
async def test_s3_live_suite_runner_records_case_identity(monkeypatch, tmp_path):
    monkeypatch.setattr(
        live_runner,
        "_build_live_planner",
        lambda **kwargs: _EvidenceBackedPlanner(),
    )
    monkeypatch.setattr(live_runner, "LIVE_REPORT_DIR", tmp_path)
    case = live_runner.LiveSmokeCase(
        case_id="smoke-one",
        category="complete_requirement",
        message=live_runner.COMPLETE_REQUEST,
        action="confirm",
        expected_final_state="DRAFT_CREATED",
    )

    report = await live_runner.run_live_smoke_suite(
        model="fake-model",
        api_key="fake-key",
        runs_per_case=1,
        topology="two_agent",
        cases=[case],
    )

    assert report["status"] == "passed"
    assert report["case_count"] == 1
    assert report["results"][0]["case_id"] == "smoke-one"
    assert (tmp_path / "s3-smoke-two_agent.json").exists()
