from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from procure_pilot.agents.multi_agent import ModelCallUsage
from procure_pilot.domain.requirements import ConstraintStrength
from procure_pilot.evals.requirement_runner import (
    DEFAULT_DATASET,
    compare_reports,
    make_base,
    run_requirement_evaluation,
)
from procure_pilot.evals.requirements import (
    Fact,
    RequirementDataset,
    extract_facts,
    load_dataset,
    score_requirement,
)
from procure_pilot.services.requirement_validation import RequirementSpecService


def one_case(tmp_path, case_id="req-complete-01-1"):
    payload = json.loads(DEFAULT_DATASET.read_text())
    payload["cases"] = [case for case in payload["cases"] if case["case_id"] == case_id]
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(payload, ensure_ascii=False))
    return path


def proposal(case):
    return RequirementSpecService().propose(
        run_id=f"eval_{case.case_id}",
        message_id="msg_current",
        text=case.message,
        base=None,
    )


def test_versioned_labels_are_independent_and_paraphrase_groups_do_not_leak():
    dataset = load_dataset(DEFAULT_DATASET)
    assert len(dataset.cases) == 60
    assert sum(case.split == "development" for case in dataset.cases) == 20
    assert sum(case.split == "holdout" for case in dataset.cases) == 40
    assert len({case.group_id for case in dataset.cases}) == 30
    payload = dataset.model_dump()
    payload["cases"][1]["split"] = "holdout"
    with pytest.raises(ValidationError, match="cannot cross"):
        RequirementDataset.model_validate(payload)


def test_scorer_counts_wrong_value_once_and_does_not_double_count_omission():
    case = load_dataset(DEFAULT_DATASET).cases[0]
    spec = proposal(case)
    spec.quantity.normalized_value = 12
    spec.hard_constraints = [c for c in spec.hard_constraints if c.field != "minimum_storage_gb"]
    spec.soft_preferences.append(
        spec.hard_constraints[0].model_copy(
            update={
                "constraint_id": "extra",
                "field": "brand_preference",
                "operator": "PREFER",
                "value": "invented_brand",
                "strength": ConstraintStrength.SOFT,
            }
        )
    )
    score = score_requirement(case, spec, messages={"msg_current": case.message})
    assert score["wrong_count"] == 1
    assert score["omission_count"] == 1
    assert score["addition_count"] == 1
    assert not score["passed"]


def test_parse_failure_stays_in_denominator():
    case = load_dataset(DEFAULT_DATASET).cases[0]
    score = score_requirement(case, None, messages={})
    assert score["omission_count"] == len(case.expected_facts)
    assert score["correct_count"] == 0
    assert "no_parseable_requirement" in score["issues"]


def test_evidence_quote_does_not_make_wrong_semantics_correct():
    case = load_dataset(DEFAULT_DATASET).cases[0]
    spec = proposal(case)
    spec.budget.tax_included.normalized_value = False
    score = score_requirement(case, spec, messages={"msg_current": case.message})
    assert not score["evidence_errors"]
    assert score["wrong_count"] == 1
    spec.evidence[0].start += 1
    assert score_requirement(case, spec, messages={"msg_current": case.message})["evidence_errors"]


def test_numeric_bool_and_constraint_strength_are_not_interchangeable():
    assert Fact(field="x", value=True).key() != Fact(field="x", value=1).key()
    case = load_dataset(DEFAULT_DATASET).cases[0]
    spec = proposal(case)
    spec.hard_constraints[0].strength = ConstraintStrength.SOFT
    # Re-validate enum coercion after mutation, as provider results are typed.
    spec = type(spec).model_validate(spec.model_dump())
    assert score_requirement(case, spec, messages={"msg_current": case.message})["wrong_count"] == 1


def test_complete_fixture_is_hand_authored_and_keeps_identity():
    base = make_base("run_fixed")
    assert base.version == 3 and base.authority == "VALIDATED"
    assert {fact.field for fact in extract_facts(base)} == {
        "category",
        "quantity",
        "budget.maximum",
        "budget.tax_included",
        "delivery_city",
        "minimum_ram_gb",
        "minimum_storage_gb",
    }


@pytest.mark.asyncio
async def test_offline_runner_writes_unique_reports_without_claiming_model_tokens(tmp_path):
    dataset = one_case(tmp_path)
    first, path1 = await run_requirement_evaluation(
        dataset_path=dataset, output_root=tmp_path / "reports"
    )
    second, path2 = await run_requirement_evaluation(
        dataset_path=dataset, output_root=tmp_path / "reports"
    )
    assert path1 != path2 and path1.exists() and path2.exists()
    assert first["final"]["passed"] == 1
    assert first["usage"]["measured"] is False
    assert first["usage"]["total_tokens_mean_known"] is None
    assert first["release_evidence_eligible"] is False
    assert compare_reports(first, second)["status"] == "no_regression_observed"
    assert path1.with_name("report.html").exists()
    assert path1.with_name("prompt.txt").exists()
    assert first["manifest"]["dataset_sha256"]
    assert first["manifest"]["source_tree_sha256"]


@pytest.mark.asyncio
async def test_proposal_is_preserved_before_validator_supplements_omission(tmp_path):
    case_path = one_case(tmp_path)
    case = load_dataset(case_path).cases[0]

    class OmissionAdapter:
        last_execution = None
        last_model_output = []

        async def execute_agent(self, envelope, **kwargs):
            spec = proposal(case)
            spec.quantity = None
            return SimpleNamespace(
                output=SimpleNamespace(requirement_spec=spec),
                usage=ModelCallUsage(requests=1, input_tokens=100, output_tokens=50),
            )

    report, _ = await run_requirement_evaluation(
        dataset_path=case_path,
        output_root=tmp_path / "reports",
        mode="live",
        adapter=OmissionAdapter(),
        provider="deepseek",
        model="fake-test-model",
    )
    result = report["results"][0]
    assert result["proposal"]["omission_count"] == 1
    assert result["proposal_spec"]["quantity"] is None
    assert result["final_spec"]["quantity"]["normalized_value"] == 20
    assert result["final"]["passed"]
    assert report["usage"]["total_tokens_mean_known"] == 150


@pytest.mark.asyncio
async def test_live_failure_preserves_known_usage_but_does_not_claim_complete_usage(tmp_path):
    dataset = one_case(tmp_path)

    class FailingAdapter:
        last_execution = None
        last_model_output = []
        usage_by_execution = {}

        async def execute_agent(self, envelope, **kwargs):
            self.last_execution = "current"
            self.usage_by_execution["current"] = ModelCallUsage(
                requests=1,
                input_tokens=123,
                output_tokens=45,
            )
            raise RuntimeError("synthetic provider failure")

    report, _ = await run_requirement_evaluation(
        dataset_path=dataset,
        output_root=tmp_path / "reports",
        mode="live",
        adapter=FailingAdapter(),
        provider="deepseek",
        model="fake-test-model",
    )
    assert report["status"] == "failed"
    assert report["usage"]["input_tokens_total_known"] == 123
    assert report["usage"]["incomplete_runs"] == 1
    assert report["final"]["runs"] == 1
    assert report["final"]["case_exact_match_rate"] == 0


@pytest.mark.asyncio
async def test_comparison_rejects_changed_inputs_and_detects_worsening_failed_cases(tmp_path):
    report, _ = await run_requirement_evaluation(
        dataset_path=one_case(tmp_path), output_root=tmp_path / "reports"
    )
    changed = copy.deepcopy(report)
    changed["manifest"]["dataset_sha256"] = "other"
    with pytest.raises(ValueError, match="dataset_sha256"):
        compare_reports(report, changed)
    changed = copy.deepcopy(report)
    changed["results"][0]["input_sha256"] = "other"
    with pytest.raises(ValueError, match="case inputs"):
        compare_reports(report, changed)
    baseline = copy.deepcopy(report)
    baseline["results"][0]["proposal"]["passed"] = False
    candidate = copy.deepcopy(baseline)
    candidate["results"][0]["proposal"]["wrong_count"] += 1
    compared = compare_reports(baseline, candidate)
    assert compared["status"] == "regressed"
    assert compared["degraded_checks"][0]["metric"] == "wrong_count"


@pytest.mark.asyncio
async def test_offline_prompt_ab_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="does not execute a Prompt"):
        await run_requirement_evaluation(output_root=tmp_path, prompt="short")


@pytest.mark.asyncio
async def test_invalid_evaluation_options_do_not_create_artifacts(tmp_path):
    with pytest.raises(ValueError, match="explicitly configured"):
        await run_requirement_evaluation(mode="live", output_root=tmp_path / "reports")
    with pytest.raises(ValueError, match="Invalid mode"):
        await run_requirement_evaluation(repetitions=0, output_root=tmp_path / "reports")
    assert not (tmp_path / "reports").exists()


@pytest.mark.asyncio
async def test_validator_fallback_does_not_hide_invalid_proposal(tmp_path):
    case_path = one_case(tmp_path)
    case = load_dataset(case_path).cases[0]

    class InvalidEvidenceAdapter:
        last_execution = None
        last_model_output = []

        async def execute_agent(self, envelope, **kwargs):
            spec = proposal(case)
            spec.evidence[0].quote = "fabricated quotation"
            return SimpleNamespace(
                output=SimpleNamespace(requirement_spec=spec),
                usage=ModelCallUsage(requests=1, input_tokens=20, output_tokens=30),
            )

    report, _ = await run_requirement_evaluation(
        dataset_path=case_path,
        output_root=tmp_path / "reports",
        mode="live",
        adapter=InvalidEvidenceAdapter(),
        model="fake-test-model",
    )
    assert report["fallback_count"] == 1
    assert not report["results"][0]["proposal"]["passed"]
    assert report["results"][0]["final"]["passed"]
    assert report["results"][0]["validation_error"]


@pytest.mark.asyncio
async def test_comparison_rejects_changed_evaluator_even_if_version_label_is_same(tmp_path):
    report, _ = await run_requirement_evaluation(
        dataset_path=one_case(tmp_path), output_root=tmp_path / "reports"
    )
    changed = copy.deepcopy(report)
    changed["manifest"]["evaluator_sha256"] = "changed-scorer-implementation"
    with pytest.raises(ValueError, match="evaluator_sha256"):
        compare_reports(report, changed)


@pytest.mark.asyncio
async def test_live_prompt_override_changes_actual_agent_and_saved_instructions(tmp_path):
    case_path = one_case(tmp_path)
    case = load_dataset(case_path).cases[0]

    class AgentDouble:
        instructions = "original instructions"

        def clone(self, *, instructions):
            return SimpleNamespace(instructions=instructions)

    class RecordingAdapter:
        agents = {"REQUIREMENT": AgentDouble()}
        last_execution = None
        last_model_output = []

        async def execute_agent(self, envelope, **kwargs):
            assert self.agents["REQUIREMENT"].instructions.startswith("replacement prompt")
            return SimpleNamespace(
                output=SimpleNamespace(requirement_spec=proposal(case)),
                usage=ModelCallUsage(requests=1, input_tokens=5, output_tokens=8),
            )

    report, path = await run_requirement_evaluation(
        dataset_path=case_path,
        output_root=tmp_path / "reports",
        mode="live",
        adapter=RecordingAdapter(),
        prompt="replacement prompt",
        model="fake-test-model",
    )
    assert path.with_name("prompt.txt").read_text().startswith("replacement prompt")
    assert "schema" in path.with_name("prompt.txt").read_text()
    assert report["final"]["passed"] == 1


def test_unauthorized_validated_authority_is_not_a_valid_model_proposal():
    case = load_dataset(DEFAULT_DATASET).cases[0]
    spec = proposal(case)
    spec.authority = type(spec.authority).VALIDATED
    score = score_requirement(
        case, spec, messages={"msg_current": case.message}, expected_authority="PROPOSED"
    )
    assert "authority_mismatch" in score["issues"]
