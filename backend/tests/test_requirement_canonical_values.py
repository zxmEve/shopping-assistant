from __future__ import annotations

import json

import pytest

from procure_pilot.domain.requirements import RequirementDeltaOperation
from procure_pilot.evals.requirement_replay import replay_requirements
from procure_pilot.evals.requirement_runner import BACKEND_ROOT, make_base
from procure_pilot.services.requirement_validation import RequirementSpecService
from procure_pilot.services.requirement_values import (
    canonical_city,
    canonical_operating_system,
    normalize_requirement_values,
)


@pytest.mark.parametrize(
    "alias,canonical",
    [
        ("上海", "Shanghai"),
        ("上海市", "Shanghai"),
        (" shANGhai ", "Shanghai"),
        ("深圳", "Shenzhen"),
        ("深圳市", "Shenzhen"),
        ("SHENZHEN", "Shenzhen"),
        ("雅加达", "Jakarta"),
        (" JAKARTA ", "Jakarta"),
    ],
)
def test_exact_city_aliases_have_one_canonical_value(alias, canonical):
    assert canonical_city(alias) == canonical


@pytest.mark.parametrize(
    "alias,canonical",
    [
        ("Linux", "linux"),
        ("LINUX", "linux"),
        (" linux ", "linux"),
        ("Windows", "windows"),
        (" WINDOWS ", "windows"),
        ("macOS", "macos"),
    ],
)
def test_supported_system_names_share_one_canonical_case(alias, canonical):
    assert canonical_operating_system(alias) == canonical


@pytest.mark.parametrize("value", ["上海分公司", "Shanghaix", "Shen zhen", "北京", None])
def test_city_normalization_does_not_guess_unknown_or_partial_names(value):
    assert canonical_city(value) == value


@pytest.mark.parametrize("value", ["Windows 11", "Linux Mint", "Mac OS X", "Linuz", None])
def test_system_versions_and_unknown_names_are_not_collapsed(value):
    assert canonical_operating_system(value) == value


@pytest.mark.parametrize("city", ["SHANGHAI", "上海市", "SHENZHEN", "深圳市"])
def test_rule_parser_uses_the_same_registered_city_values(city):
    spec = RequirementSpecService().propose(
        run_id="test", message_id="current", text="采购2台笔记本，交付到" + city, base=None
    )
    assert spec.delivery_city.normalized_value == canonical_city(city)


def proposal(text, base=None):
    return RequirementSpecService().propose(
        run_id="test", message_id="current", text=text, base=base
    )


def test_validation_normalizes_final_values_and_delta_without_rewriting_evidence():
    text = "采购2台笔记本，单台预算8800元含税，送上海，最低24GB内存和768GB硬盘，必须Linux"
    spec = proposal(text)
    spec.delivery_city.normalized_value = "上海"
    system = next(c for c in spec.hard_constraints if c.field == "operating_system")
    system.value = "Linux"
    for op in spec.delta.operations:
        if op.path == "delivery_city":
            op.new_value = "上海"
        if op.path == "operating_system":
            op.new_value = "Linux"
    raw_evidence = [e.model_dump() for e in spec.evidence]
    raw_city = spec.delivery_city.value
    final = RequirementSpecService().validate(spec, message_id="current", text=text, base=None)
    assert final.delivery_city.normalized_value == "Shanghai"
    assert final.delivery_city.value == raw_city
    assert [e.model_dump() for e in final.evidence] == raw_evidence
    assert next(c for c in final.hard_constraints if c.field == "operating_system").value == "linux"
    assert (
        next(op for op in final.delta.operations if op.path == "delivery_city").new_value
        == "Shanghai"
    )
    assert (
        next(op for op in final.delta.operations if op.path == "operating_system").new_value
        == "linux"
    )
    domain = RequirementSpecService.to_procurement_requirement(final)
    assert domain.delivery_city == "Shanghai" and domain.operating_system == "linux"
    first = final.model_dump(mode="json")
    normalize_requirement_values(final)
    assert final.model_dump(mode="json") == first


def test_dotted_constraint_delta_resolves_after_system_normalization():
    text = "必须Windows，送深圳"
    spec = proposal(text)
    constraint = next(c for c in spec.hard_constraints if c.field == "operating_system")
    constraint.value = "Windows"
    spec.delta.operations = [
        RequirementDeltaOperation(
            op="ADD",
            path="hard_constraints.operating_system",
            new_value={
                "field": "operating_system",
                "operator": "EQ",
                "value": "Windows",
                "strength": "HARD",
            },
            evidence_ids=constraint.evidence_ids,
        )
    ]
    final = RequirementSpecService().validate(spec, message_id="current", text=text, base=None)
    op = next(op for op in final.delta.operations if op.path.startswith("hard_constraints/"))
    assert op.path == "hard_constraints/" + constraint.constraint_id
    assert op.new_value["value"] == "windows"


def test_inherited_legacy_city_is_normalized_without_mutating_the_base():
    base = make_base("test")
    base.delivery_city.normalized_value = "上海"
    saved = base.model_dump(mode="json")
    text = "数量改成14台，其他条件不变"
    spec = proposal(text, base)
    final = RequirementSpecService().validate(spec, message_id="current", text=text, base=base)
    assert final.delivery_city.normalized_value == "Shanghai"
    assert base.model_dump(mode="json") == saved
    assert not any(op.path == "delivery_city" for op in final.delta.operations)
    assert RequirementSpecService.to_procurement_requirement(base).delivery_city == "Shanghai"


def test_normalization_does_not_hide_a_real_operating_system_conflict():
    text = "必须Windows，同时必须Linux"
    spec = proposal(text)
    for c in spec.hard_constraints:
        c.value = str(c.value).title()
    final = RequirementSpecService().validate(spec, message_id="current", text=text, base=None)
    assert {c.value for c in final.hard_constraints} == {"windows", "linux"}
    assert {c.field for c in final.conflicts} == {"operating_system"}
    assert final.status == "NEEDS_CLARIFICATION"


def test_case_normalization_does_not_authorize_an_unsupported_system_from_other_evidence():
    text = "必须Linux"
    spec = proposal(text)
    spec.hard_constraints[0].value = "Windows"
    final = RequirementSpecService().validate(spec, message_id="current", text=text, base=None)
    assert not any(c.value == "windows" for c in final.hard_constraints)


def test_all_saved_proposals_pass_unchanged_strict_scoring_after_backend_normalization(tmp_path):
    source = (
        BACKEND_ROOT
        / "evals/audits/requirement-holdout-v2-live/20261022T135239-22c85f553f/report.json"
    )
    dataset = BACKEND_ROOT / "evals/datasets/requirement_holdout_v2.json"
    old = source.read_bytes()
    report, _ = replay_requirements(source_path=source, dataset_path=dataset, output_root=tmp_path)
    assert report["final"]["passed"] == 24
    assert report["final"]["omission_rate"] == report["final"]["wrong_extraction_rate"] == 0
    assert report["final"]["unsupported_addition_count"] == 0
    assert not report["final"]["issue_counts"]
    assert report["new_model_requests"] == report["new_model_tokens"] == 0
    assert source.read_bytes() == old
    saved = json.loads(old)
    assert [r["proposal_spec"] for r in report["results"]] == [
        r["proposal_spec"] for r in saved["results"]
    ]
