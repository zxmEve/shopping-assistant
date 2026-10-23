from __future__ import annotations

import hashlib

import pytest

from procure_pilot.domain.models import Message, ProcurementRun, RunState
from procure_pilot.domain.requirements import RequirementEvidence
from procure_pilot.evals.requirement_replay import replay_requirements
from procure_pilot.evals.requirement_runner import BACKEND_ROOT, BASE_MESSAGE, make_base
from procure_pilot.evals.runner import make_engine
from procure_pilot.services.engine import AgentStageResult
from procure_pilot.services.requirement_validation import RequirementSpecService

SOURCE = (
    BACKEND_ROOT / "evals/audits/requirement-holdout-v2-live/20261022T135239-22c85f553f/report.json"
)
DATASET = BACKEND_ROOT / "evals/datasets/requirement_holdout_v2.json"


def checked(text, base=None):
    service = RequirementSpecService()
    spec = service.propose(run_id="regression", message_id="msg_current", text=text, base=base)
    return service.validate(spec, message_id="msg_current", text=text, base=base)


@pytest.mark.parametrize(
    "text,quantity",
    [
        ("去年采购40台笔记本，本次只买9台笔记本", 9),
        ("显示器30台已经买完。本次只买9台笔记本", 9),
        ("小王建议买40台，那不是我的要求。我确认只采购11台笔记本", 11),
        ("采购9台笔记本，仓库另有30台旧电脑", 9),
        ("之前提过30台，现在改为9台笔记本", 9),
        ("原来9台笔记本的数量保留", 9),
        ("本次要9台笔记本，另一供应商起订40台", 9),
    ],
)
def test_background_counts_never_override_current_procurement(text, quantity):
    spec = checked(text)
    assert spec.quantity.normalized_value == quantity
    assert spec.category.normalized_value == "laptop"


def test_two_unresolved_current_counts_require_clarification():
    text = "采购9台笔记本，另外的要求是12台笔记本，尚未选定"
    spec = checked(text)
    assert spec.quantity is None
    assert "quantity" in {m.field for m in spec.missing_fields}


@pytest.mark.parametrize(
    "text,field,value,operator",
    [
        ("内存至少24GB", "minimum_ram_gb", 24, "GTE"),
        ("48GB内存起", "minimum_ram_gb", 48, "GTE"),
        ("至少768GB硬盘", "minimum_storage_gb", 768, "GTE"),
        ("1.5TB硬盘起", "minimum_storage_gb", 1536, "GTE"),
        ("硬盘至少2TB", "minimum_storage_gb", 2048, "GTE"),
        ("硬盘至少0.5TB", "minimum_storage_gb", 512, "GTE"),
        ("存储不超过1536GB", "minimum_storage_gb", 1536, "LTE"),
    ],
)
def test_capacities_use_field_scoped_units_in_parser_and_validator(text, field, value, operator):
    spec = checked(text)
    values = [c for c in spec.hard_constraints if c.field == field]
    assert len(values) == 1 and values[0].value == value and values[0].operator == operator


@pytest.mark.parametrize(
    "text,months", [("保修至少两年半", 30), ("质保至少2.5年", 30), ("保修至少18个月", 18)]
)
def test_chinese_and_fractional_warranty_durations_remain_grounded(text, months):
    assert any(
        c.field == "minimum_warranty_months" and c.value == months
        for c in checked(text).hard_constraints
    )


@pytest.mark.parametrize(
    "quote,model_value,expected", [("硬盘至少0.5TB", 500, 512), ("内存至少116GB", 16, 116)]
)
def test_exact_unique_numeric_evidence_is_canonicalized_instead_of_substring_matching(
    quote, model_value, expected
):
    service = RequirementSpecService()
    spec = service.propose(run_id="regression", message_id="msg_current", text=quote, base=None)
    spec.hard_constraints[0].value = model_value
    result = service.validate(spec, message_id="msg_current", text=quote, base=None)
    assert result.hard_constraints[0].value == expected


def test_ram_cannot_be_grounded_in_a_storage_quote():
    text = "硬盘至少1TB"
    service = RequirementSpecService()
    spec = service.propose(run_id="regression", message_id="msg_current", text=text, base=None)
    spec.hard_constraints[0].field = "minimum_ram_gb"
    result = service.validate(spec, message_id="msg_current", text=text, base=None)
    assert not any(c.field == "minimum_ram_gb" for c in result.hard_constraints)


def test_upper_bound_cannot_silently_become_a_lower_bound():
    text = "内存最多24GB"
    service = RequirementSpecService()
    spec = service.propose(run_id="regression", message_id="msg_current", text=text, base=None)
    spec.hard_constraints[0].operator = "GTE"
    result = service.validate(spec, message_id="msg_current", text=text, base=None)
    assert not any(
        c.field == "minimum_ram_gb" and c.operator == "GTE" for c in result.hard_constraints
    )


@pytest.mark.parametrize(
    "text",
    [
        "刚才的8000元单价上限作废，本次单台预算还没批准",
        "撤销单台预算，其他条件不变",
        "取消原先的预算上限，其他不变",
        "原先的单价8000元作废",
        "本次单台预算待定",
    ],
)
def test_withdrawal_clears_confirmed_budget_and_reopens_clarification(text):
    base = make_base("regression")
    result = checked(text, base)
    assert result.budget.maximum is None
    assert result.quantity == base.quantity
    assert result.budget.tax_included == base.budget.tax_included
    assert result.status == "NEEDS_CLARIFICATION"
    assert "budget.maximum" in {m.field for m in result.missing_fields}
    assert any(op.op == "REMOVE" and op.path == "budget.maximum" for op in result.delta.operations)


@pytest.mark.parametrize(
    "text",
    ["预算不要取消", "预算没有取消", "如果预算取消，先告诉我", "去年预算作废", "有人建议取消预算"],
)
def test_negation_hypothetical_history_and_advice_do_not_revoke_current_authorization(text):
    base = make_base("regression")
    assert checked(text, base).budget.maximum == base.budget.maximum


def test_new_explicit_ceiling_after_withdrawal_is_used():
    assert (
        checked(
            "取消旧预算，单台预算9000元，其他不变", make_base("regression")
        ).budget.maximum.normalized_value.amount
        == 9000
    )


def test_a_city_quote_cannot_authorize_budget_removal_even_in_a_retraction_clause():
    base = make_base("regression")
    text = "取消预算并送上海"
    service = RequirementSpecService()
    spec = service.propose(run_id="regression", message_id="msg_current", text=text, base=base)
    evidence = RequirementEvidence(
        message_id="msg_current", quote="上海", start=text.index("上海"), end=len(text)
    )
    spec.evidence.append(evidence)
    next(op for op in spec.delta.operations if op.path == "budget.maximum").evidence_ids = [
        evidence.evidence_id
    ]
    with pytest.raises(ValueError, match="scope-change evidence"):
        service.validate(spec, message_id="msg_current", text=text, base=base)


def test_duplicate_constraint_ids_are_rejected():
    service = RequirementSpecService()
    text = "最低24GB内存"
    spec = service.propose(run_id="regression", message_id="msg_current", text=text, base=None)
    spec.hard_constraints.append(spec.hard_constraints[0].model_copy(deep=True))
    with pytest.raises(ValueError, match="unique IDs"):
        service.validate(spec, message_id="msg_current", text=text, base=None)


def test_engine_fallback_does_not_restore_a_withdrawn_budget_after_invalid_model_output():
    engine = make_engine()
    run = ProcurementRun(
        run_id="regression", tenant_id="demo_cn", user_id="employee_001", state=RunState.COLLECTING
    )
    engine.harness.initialize_run(run)
    run.requirement_spec = make_base(run.run_id)
    run.requirement = engine.requirement_specs.to_procurement_requirement(run.requirement_spec)
    text = "刚才的8000元单价上限作废，本次单台预算还没批准"
    run.messages = [
        Message(message_id="msg_base", role="user", content=BASE_MESSAGE),
        Message(message_id="msg_current", role="user", content=text),
    ]
    invalid = engine.requirement_specs.propose(
        run_id=run.run_id, message_id="msg_current", text=text, base=run.requirement_spec
    )
    invalid.version += 3  # Force the actual engine's rejection/fallback branch.
    result = engine._complete_requirement_stage(
        run,
        AgentStageResult(execution_id="test", agent_kind="REQUIREMENT", requirement_spec=invalid),
    )
    assert result.run.requirement_spec.budget.maximum is None
    assert result.run.requirement.unit_budget is None
    assert result.run.state == RunState.WAITING_USER
    assert any(t.name == "requirement_spec.agent_output_rejected" for t in result.run.trace)


def test_saved_live_proposals_replay_without_new_model_calls_or_lost_conditions(tmp_path):
    before = SOURCE.read_bytes()
    report, path = replay_requirements(
        source_path=SOURCE, dataset_path=DATASET, output_root=tmp_path
    )
    assert path.exists() and SOURCE.read_bytes() == before
    assert report["source_report_sha256"] == hashlib.sha256(before).hexdigest()
    assert report["new_model_requests"] == report["new_model_tokens"] == 0
    assert report["worsened_case_ids"] == []
    assert report["comparison"]["after"]["omissions"] == 0
    assert report["comparison"]["after"]["additions"] == 0
    rows = {r["case_id"]: r for r in report["results"]}
    for case_id, quantity in [("newholdout-04", 9), ("newholdout-05", 11)]:
        assert rows[case_id]["final_spec"]["quantity"]["normalized_value"] == quantity
        assert rows[case_id]["final_spec"]["category"]["normalized_value"] == "laptop"
    assert rows["newholdout-22"]["final_spec"]["budget"]["maximum"] is None
    assert rows["newholdout-22"]["final_spec"]["status"] == "NEEDS_CLARIFICATION"
    for case_id, field, value in [
        ("newholdout-02", "minimum_storage_gb", 1536),
        ("newholdout-08", "minimum_warranty_months", 30),
        ("newholdout-19", "minimum_ram_gb", 48),
        ("newholdout-23", "minimum_storage_gb", 2048),
    ]:
        constraints = [
            c for c in rows[case_id]["final_spec"]["hard_constraints"] if c["field"] == field
        ]
        assert len(constraints) == 1 and constraints[0]["value"] == value


def test_replay_rejects_changed_gold_labels(tmp_path):
    path = tmp_path / "changed.json"
    path.write_bytes(DATASET.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="byte-identical"):
        replay_requirements(source_path=SOURCE, dataset_path=path, output_root=tmp_path / "reports")


@pytest.mark.parametrize(
    "text,operator",
    [
        ("单台预算上限8000元含税 内存至少24GB", "GTE"),
        ("预算至少7000元 内存最多24GB", "LTE"),
    ],
)
def test_capacity_bound_does_not_borrow_the_budget_qualifier(text, operator):
    constraints = [c for c in checked(text).hard_constraints if c.field == "minimum_ram_gb"]
    assert len(constraints) == 1 and constraints[0].operator == operator


def test_model_cannot_promote_a_background_only_count_to_current_quantity():
    text = "去年采购40台笔记本，这次采购数量待定"
    service = RequirementSpecService()
    spec = service.propose(run_id="regression", message_id="msg_current", text=text, base=None)
    start = text.index("40台")
    proof = RequirementEvidence(message_id="msg_current", quote="40台", start=start, end=start + 3)
    spec.evidence.append(proof)
    spec.quantity = make_base("regression").quantity.model_copy(
        update={
            "value": "40台",
            "normalized_value": 40,
            "evidence_ids": [proof.evidence_id],
        }
    )
    with pytest.raises(ValueError, match="Background quantity"):
        service.validate(spec, message_id="msg_current", text=text, base=None)
    assert checked(text).quantity is None
