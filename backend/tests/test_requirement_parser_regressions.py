from __future__ import annotations

from decimal import Decimal

import pytest

from procure_pilot.domain.models import ProductCondition
from procure_pilot.domain.requirements import ConstraintStrength, RequirementConstraint
from procure_pilot.evals.requirement_runner import BASE_MESSAGE, make_base
from procure_pilot.services.requirement_validation import RequirementSpecService


def propose(text, base=None):
    return RequirementSpecService().propose(
        run_id="run_regression",
        message_id="current",
        text=text,
        base=base,
    )


def validate(spec, text, base=None):
    return RequirementSpecService().validate(
        spec,
        message_id="current",
        text=text,
        base=base,
    )


@pytest.mark.parametrize(
    ("text", "value", "operator"),
    [
        ("最低512GB硬盘", 512, "GTE"),
        ("存储最低512GB", 512, "GTE"),
        ("最低2048GB存储", 2048, "GTE"),
        ("硬盘至少2048GB", 2048, "GTE"),
        ("存储不超过512GB", 512, "LTE"),
        ("最多512GB硬盘", 512, "LTE"),
        ("硬盘改成512GB", 512, "GTE"),
    ],
)
def test_storage_units_keep_markers_and_full_gb_token(text, value, operator):
    spec = validate(propose(text), text)
    assert any(
        c.field == "minimum_storage_gb" and c.value == value and c.operator == operator
        for c in spec.hard_constraints
    )
    assert not any(c.field == "minimum_ram_gb" for c in spec.hard_constraints)


def test_bandwidth_unit_is_not_silently_treated_as_storage():
    spec = propose("接口带宽512GBps，最低16GB内存")
    assert not any(c.field == "minimum_storage_gb" for c in spec.hard_constraints)


def test_storage_conflict_keeps_both_bounds_and_blocks_sourcing():
    text = BASE_MESSAGE.replace("最低1TB硬盘", "最低1024GB存储，存储不超过512GB")
    spec = validate(propose(text), text)
    assert {c.operator for c in spec.hard_constraints if c.field == "minimum_storage_gb"} == {
        "GTE",
        "LTE",
    }
    assert [c.field for c in spec.conflicts] == ["minimum_storage_gb"]
    assert spec.status == "NEEDS_CLARIFICATION"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("采购20台，不对，改成12台笔记本", 12),
        ("原来说20台，最终只采购12台笔记本", 12),
        ("原来20台，现在需要8台笔记本", 8),
        ("采购20台，改成12台，再改成8台笔记本", 8),
        ("采购20台，最终以12台为准", 12),
        ("采购20台笔记本，仓库另有12台旧电脑", 20),
    ],
)
def test_explicit_quantity_correction_is_not_just_last_number(text, expected):
    assert validate(propose(text), text).quantity.normalized_value == expected


def test_model_cannot_keep_old_quantity_even_with_a_real_old_quote():
    text = "采购20台，不对，改成12台笔记本"
    spec = propose(text)
    spec.quantity.normalized_value = 20
    with pytest.raises(ValueError, match="explicit quantity or correction"):
        validate(spec, text)


@pytest.mark.parametrize(
    ("text", "currency"),
    [
        ("单台预算8000美元", "USD"),
        ("预算8000 USD每台", "USD"),
        ("单台预算USD8000", "USD"),
        ("单件预算8000美金", "USD"),
        ("单台预算8000元", "CNY"),
        ("单台预算人民币8000", "CNY"),
        ("单件预算8000欧元", "EUR"),
        ("单台预算8000港元", "HKD"),
        ("单台预算8000日元", "JPY"),
    ],
)
def test_explicit_currency_is_preserved_in_proposal_and_domain_requirement(text, currency):
    spec = validate(propose(text), text)
    assert spec.budget.maximum.normalized_value.amount == Decimal("8000")
    assert spec.budget.maximum.normalized_value.currency == currency
    assert RequirementSpecService.to_procurement_requirement(spec).unit_budget.currency == currency


def test_model_cannot_change_usd_into_cny_using_a_short_numeric_quote():
    text = "单台预算8000美元"
    spec = propose(text)
    spec.budget.maximum.normalized_value.currency = "CNY"
    with pytest.raises(ValueError, match="currency"):
        validate(spec, text)


@pytest.mark.parametrize("text", ["整批预算160000元", "含税总预算160000元", "预算总额160000元"])
def test_total_budget_is_not_a_unit_ceiling_or_implicitly_divided(text):
    spec = validate(propose("采购20台笔记本，" + text), "采购20台笔记本，" + text)
    assert spec.budget.maximum is None
    assert any(
        marker in e.quote for e in spec.evidence for marker in ("整批", "总预算", "预算总额")
    )
    assert "budget.maximum" in {m.field for m in spec.missing_fields}


@pytest.mark.parametrize(
    "text",
    [
        "整批预算160000元，单台预算8000元",
        "单台预算8000元，整批预算160000元",
        "整批预算160000元，折合单台8000元",
    ],
)
def test_explicit_unit_ceiling_can_coexist_with_total_budget(text):
    spec = validate(propose(text), text)
    assert spec.budget.maximum.normalized_value.amount == Decimal("8000")


def test_changing_to_total_scope_invalidates_previously_confirmed_unit_ceiling():
    base = make_base("run_regression")
    text = "本次总预算160000元，其他条件不变"
    spec = validate(propose(text, base), text, base)
    assert spec.budget.maximum is None
    assert spec.quantity.normalized_value == 20
    assert any(op.path == "budget.maximum" and op.op == "REMOVE" for op in spec.delta.operations)


def test_model_cannot_label_total_budget_as_a_per_item_budget():
    text = "采购20台笔记本，整批预算160000元"
    spec = propose(text)
    copied = make_base("run_regression").budget.maximum.model_copy(deep=True)
    copied.normalized_value.amount = Decimal("160000")
    spec.budget.maximum = copied
    with pytest.raises(ValueError, match="Total budget"):
        validate(spec, text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("不要翻新机，只接受全新机", "NEW"),
        ("必须全新，不要二手或翻新", "NEW"),
        ("不要翻新只接受全新", "NEW"),
        ("必须翻新机", "REFURBISHED"),
        ("接受二手机", "USED"),
    ],
)
def test_affirmative_condition_is_preserved_to_domain(text, expected):
    spec = validate(propose(text), text)
    conditions = [c.value for c in spec.hard_constraints if c.field == "condition"]
    assert conditions == [expected]
    assert RequirementSpecService.to_procurement_requirement(spec).condition == ProductCondition(
        expected
    )


@pytest.mark.parametrize("text", ["不要翻新机", "不要二手或翻新", "不要求全新", "无需全新"])
def test_negation_does_not_create_an_affirmative_condition(text):
    assert not any(c.field == "condition" for c in propose(text).hard_constraints)


def test_backend_supplements_explicit_condition_omitted_by_model():
    text = BASE_MESSAGE + "，必须全新"
    spec = propose(text)
    spec.hard_constraints = [c for c in spec.hard_constraints if c.field != "condition"]
    spec.delta.operations = [op for op in spec.delta.operations if op.path != "condition"]
    result = validate(spec, text)
    assert any(c.field == "condition" and c.value == "NEW" for c in result.hard_constraints)


def test_model_cannot_ground_refurbished_in_a_negative_quote():
    text = "不要翻新机，只接受全新机"
    spec = propose(text)
    spec.hard_constraints = [
        RequirementConstraint(
            field="condition",
            value="REFURBISHED",
            operator="EQ",
            strength=ConstraintStrength.HARD,
            confidence=1,
            evidence_ids=[spec.evidence[0].evidence_id],
        )
    ]
    # Even the complete quote contains the letters for refurbished; polarity matters.
    spec.evidence[0].quote = text
    spec.evidence[0].start = 0
    spec.evidence[0].end = len(text)
    with pytest.raises(ValueError, match="Condition proposal"):
        validate(spec, text)


def test_budget_removal_with_arbitrary_evidence_is_still_rejected():
    base = make_base("run_regression")
    text = "本次总预算160000元，送上海"
    spec = propose(text, base)
    city_ref = spec.delivery_city.evidence_ids[0]
    removal = next(op for op in spec.delta.operations if op.path == "budget.maximum")
    removal.evidence_ids = [city_ref]
    # A real quote for another field must not authorize changing budget scope.
    with pytest.raises(ValueError, match="scope-change evidence"):
        validate(spec, text, base)


@pytest.mark.parametrize(
    ("text", "condition"),
    [("must be new", "NEW"), ("refurbished", "REFURBISHED"), ("used", "USED")],
)
def test_existing_english_condition_evidence_remains_supported(text, condition):
    spec = validate(propose(text), text)
    assert any(c.field == "condition" and c.value == condition for c in spec.hard_constraints)


def test_english_negative_condition_does_not_ground_an_equality():
    assert not any(
        c.field == "condition" for c in propose("do not accept refurbished").hard_constraints
    )
