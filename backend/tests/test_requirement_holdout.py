from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from procure_pilot.agents.multi_agent import ModelCallUsage
from procure_pilot.evals.requirement_runner import (
    BACKEND_ROOT,
    DEFAULT_DATASET,
    run_requirement_evaluation,
)
from procure_pilot.evals.requirements import load_dataset
from procure_pilot.services.requirement_validation import RequirementSpecService


def test_new_holdout_is_frozen_and_has_no_reused_messages_or_groups():
    path = BACKEND_ROOT / "evals/datasets/requirement_holdout_v2.json"
    dataset = load_dataset(path)
    old = load_dataset(DEFAULT_DATASET)
    freeze = json.loads(
        (BACKEND_ROOT / "evals/audits/requirement-holdout-v2-freeze.json").read_text()
    )
    assert len(dataset.cases) == 24
    assert len({c.group_id for c in dataset.cases}) == 24
    assert {c.split for c in dataset.cases} == {"holdout"}
    assert not {c.message for c in dataset.cases} & {c.message for c in old.cases}
    assert not {c.group_id for c in dataset.cases} & {c.group_id for c in old.cases}
    assert hashlib.sha256(path.read_bytes()).hexdigest() == freeze["dataset_sha256"]
    assert dataset.label_status == "draft_engineering_labels"


def single_case(tmp_path):
    payload = json.loads(DEFAULT_DATASET.read_text())
    payload["cases"] = payload["cases"][:1]
    path = tmp_path / "case.json"
    path.write_text(json.dumps(payload, ensure_ascii=False))
    return path


@pytest.mark.asyncio
async def test_live_timeout_is_scored_and_persisted_with_elapsed_time(tmp_path):
    class SlowAdapter:
        async def execute_agent(self, *args, **kwargs):
            await asyncio.sleep(60)

    report, path = await run_requirement_evaluation(
        dataset_path=single_case(tmp_path),
        output_root=tmp_path / "reports",
        mode="live",
        adapter=SlowAdapter(),
        model="test-double",
        case_timeout_seconds=0.01,
    )
    result = report["results"][0]
    assert result["error_type"] == "TimeoutError"
    assert result["model_duration_ms"] > 0
    assert result["proposal_duration_ms"] > 0
    assert result["validation_duration_ms"] is None
    assert not result["usage_complete"]
    assert report["final"]["runs"] == 1
    assert report["final"]["passed"] == 0
    assert json.loads(path.with_name("results.jsonl").read_text()) == result
    assert report["manifest"]["settings"]["case_timeout_seconds"] == 0.01


@pytest.mark.asyncio
async def test_paired_quality_and_timing_separate_model_from_backend(tmp_path):
    case_path = single_case(tmp_path)
    case = load_dataset(case_path).cases[0]

    class MissingQuantityAdapter:
        async def execute_agent(self, *args, **kwargs):
            spec = RequirementSpecService().propose(
                run_id="test",
                message_id="msg_current",
                text=case.message,
                base=None,
            )
            spec.quantity = None
            return SimpleNamespace(
                output=SimpleNamespace(requirement_spec=spec),
                usage=ModelCallUsage(requests=1, input_tokens=17, output_tokens=23),
            )

    report, _ = await run_requirement_evaluation(
        dataset_path=case_path,
        output_root=tmp_path / "reports",
        mode="live",
        adapter=MissingQuantityAdapter(),
        model="test-double",
    )
    result = report["results"][0]
    assert result["proposal"]["omission_count"] == 1
    assert result["final"]["omission_count"] == 0
    assert report["validation_impact"]["proposal_fail_final_pass"] == 1
    assert result["model_duration_ms"] >= 0
    assert result["validation_duration_ms"] >= 0
    assert result["duration_ms"] >= result["model_duration_ms"]
    assert report["usage"]["total_tokens_mean_known"] == 40
    assert report["latency_by_phase"]["validation_duration_ms"]["mean_ms"] >= 0


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
async def test_invalid_timeouts_do_not_create_artifacts(tmp_path, timeout):
    with pytest.raises(ValueError, match="Invalid mode"):
        await run_requirement_evaluation(
            output_root=tmp_path / "reports", case_timeout_seconds=timeout
        )
    assert not (tmp_path / "reports").exists()
