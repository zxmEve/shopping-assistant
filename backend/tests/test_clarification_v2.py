from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import ValidationError

from procure_pilot.agents.multi_agent import FixtureTwoAgentAdapter, TwoAgentPlanner
from procure_pilot.api.app import create_app
from procure_pilot.config import Settings
from procure_pilot.domain.conversation import RunWaitReason
from procure_pilot.domain.models import ProcurementRun, RunState
from procure_pilot.domain.requirements import (
    ClarificationPlan,
    ClarificationQuestion,
    FieldResolution,
    FieldResolutionState,
    RequirementSpec,
    RequirementStatus,
)
from procure_pilot.domain.sourcing import ConstraintCheck, OfferFact
from procure_pilot.evals.runner import make_engine
from procure_pilot.gateway.tool_gateway import DEFAULT_AGENT_TOOL_NAMES, build_default_tool_registry
from procure_pilot.harness.runtime import AgentHarness
from procure_pilot.persistence.execution_repository import SqlAlchemyExecutionRepository
from procure_pilot.persistence.repository import SqlAlchemyRunRepository
from procure_pilot.services.requirement_validation import RequirementSpecService
from procure_pilot.workflow import LangGraphProcurementWorkflow

BASE_TEXT = "采购20台笔记本，单台预算8000元含税，送到雅加达"


def validated(text: str, base=None, message_id="msg_1"):
    service = RequirementSpecService()
    spec = service.propose(run_id="run_test", message_id=message_id, text=text, base=base)
    return service.validate(spec, message_id=message_id, text=text, base=base)


def test_model_questions_are_retained_and_only_missing_or_invalid_parts_are_repaired():
    service = RequirementSpecService()
    text = "采购20台笔记本"
    proposed = service.propose(run_id="run_test", message_id="msg_1", text=text, base=None)
    budget = ClarificationQuestion(
        field="budget.maximum",
        field_ids=["budget.maximum", "budget.tax_included"],
        text="请说明每台预算的上限、币种以及含税口径。",
        impact=0.1,
    )
    city = ClarificationQuestion(field="delivery_city", text="这批设备要送到哪个城市？", impact=0.2)
    proposed.clarification_plan = ClarificationPlan(
        questions=[
            budget,
            city,
            city.model_copy(update={"question_id": "duplicate_city"}),
            ClarificationQuestion(field="quantity", text="需要采购多少件？", impact=1),
            ClarificationQuestion(field="minimum_ram_gb", text="喜欢什么颜色？", impact=1),
        ]
    )
    result = service.validate(proposed, message_id="msg_1", text=text, base=None)
    plan = result.clarification_plan

    assert plan.questions[:2] == [
        budget.model_copy(update={"impact": 1.0}),
        city.model_copy(update={"impact": 0.85}),
    ]
    assert len(plan.questions) == 4
    assert plan.covered_fields == sorted(item.field for item in result.missing_fields)
    assert sum("城市" in item.text for item in plan.questions) == 1
    assert not any("颜色" in item.text or item.field == "quantity" for item in plan.questions)
    assert {item.field for item in plan.questions[2:]} == {"minimum_ram_gb", "minimum_storage_gb"}
    assert plan.repaired_question_count > 0
    assert result.next_question == plan.questions[0]


def test_duplicate_question_ids_are_repaired_without_losing_coverage():
    service = RequirementSpecService()
    spec = service.propose(run_id="run_test", message_id="msg_1", text=BASE_TEXT, base=None)
    assert len(spec.clarification_plan.questions) == 2
    spec.clarification_plan.questions[1].question_id = spec.clarification_plan.questions[
        0
    ].question_id
    result = service.validate(spec, message_id="msg_1", text=BASE_TEXT, base=None)
    questions = result.clarification_plan.questions
    assert len({item.question_id for item in questions}) == 2
    assert result.clarification_plan.covered_fields == ["minimum_ram_gb", "minimum_storage_gb"]


@pytest.mark.parametrize("text", ["帮我采购设备", "采购20台手机，单台预算8000元"])
def test_unknown_or_unsupported_category_only_asks_category(text):
    spec = validated(text)
    assert spec.status == RequirementStatus.NEEDS_CLARIFICATION
    assert spec.clarification_plan.covered_fields == ["category"]
    assert len(spec.clarification_plan.questions) == 1
    assert "内存" not in spec.clarification_plan.questions[0].text


def test_partial_answers_preserve_previous_facts_and_only_ask_remaining_fields():
    first = validated("采购20台笔记本")
    second = validated("单台预算8000元含税，送到雅加达", first, "msg_2")
    assert second.quantity == first.quantity
    assert second.version == first.version + 1
    assert set(second.clarification_plan.covered_fields) == {"minimum_ram_gb", "minimum_storage_gb"}
    assert all(
        "预算" not in item.text and "城市" not in item.text
        for item in second.clarification_plan.questions
    )
    assert first.budget.maximum is None


def test_complete_input_produces_no_questions_and_soft_preference_does_not_change_budget_strength():
    spec = validated(BASE_TEXT + "，最低32GB内存和1TB存储，最好两周内到货")
    assert spec.status == RequirementStatus.READY_FOR_SOURCING
    assert spec.budget.maximum is not None
    assert spec.budget.target is None
    assert not spec.clarification_plan.questions
    assert spec.next_question is None


def test_no_preference_is_distinct_from_missing_and_does_not_invent_numeric_configuration():
    missing = validated(BASE_TEXT)
    spec = validated("内存和硬盘都无要求", missing, "msg_2")
    assert spec.status == RequirementStatus.READY_FOR_SOURCING
    assert not spec.clarification_plan.questions
    assert spec.field_resolutions["minimum_ram_gb"].state == FieldResolutionState.NO_PREFERENCE
    assert spec.field_resolutions["minimum_storage_gb"].state == FieldResolutionState.NO_PREFERENCE
    requirement = RequirementSpecService.to_procurement_requirement(spec)
    assert requirement.minimum_ram_gb is None
    assert requirement.minimum_storage_gb is None
    assert requirement.assumptions == []
    assert not any(
        item.field in {"minimum_ram_gb", "minimum_storage_gb"} for item in spec.hard_constraints
    )


def test_no_preference_removes_prior_constraints_with_auditable_delta_and_can_be_replaced():
    first = validated(BASE_TEXT + "，最低64GB内存和1TB存储")
    second = validated("内存无要求", first, "msg_2")
    assert not any(item.field == "minimum_ram_gb" for item in second.hard_constraints)
    assert any(
        item.op == "REMOVE" and item.path.startswith("hard_constraints/")
        for item in second.delta.operations
    )
    assert second.field_resolutions["minimum_ram_gb"].state == FieldResolutionState.NO_PREFERENCE
    third = validated("内存改成32GB", second, "msg_3")
    assert RequirementSpecService.to_procurement_requirement(third).minimum_ram_gb == 32
    assert third.field_resolutions["minimum_ram_gb"].state == FieldResolutionState.EXPLICIT
    assert first.hard_constraints[0].value == 64


@pytest.mark.parametrize(
    "field", ["quantity", "budget.maximum", "budget.tax_included", "delivery_city"]
)
def test_no_preference_cannot_bypass_business_required_facts(field):
    service = RequirementSpecService()
    text = "采购笔记本，配置无要求"
    proposed = service.propose(run_id="run_test", message_id="msg_1", text=text, base=None)
    ref = proposed.field_resolutions["minimum_ram_gb"].evidence_ids
    proposed.field_resolutions[field] = FieldResolution(state="NO_PREFERENCE", evidence_ids=ref)
    with pytest.raises(ValueError, match="No preference is not allowed"):
        service.validate(proposed, message_id="msg_1", text=text, base=None)


def test_invented_no_preference_and_invented_authorized_default_are_rejected():
    service = RequirementSpecService()
    proposed = service.propose(run_id="run_test", message_id="msg_1", text=BASE_TEXT, base=None)
    ref = proposed.category.evidence_ids
    for state in ("NO_PREFERENCE", "AUTHORIZED_DEFAULT"):
        candidate = proposed.model_copy(deep=True)
        candidate.field_resolutions["minimum_ram_gb"] = FieldResolution(
            state=state, evidence_ids=ref
        )
        with pytest.raises(ValueError):
            service.validate(candidate, message_id="msg_1", text=BASE_TEXT, base=None)


def test_authorized_defaults_have_separate_sources_survive_partial_reply_and_respect_no_preference():
    service = RequirementSpecService()
    first = validated("采购20台笔记本，单台预算8000元，按公司标准")
    defaulted = service.apply_authorized_defaults(
        first, minimum_ram_gb=32, minimum_storage_gb=1024, source_ref="policy://demo/v3"
    )
    for field in ("minimum_ram_gb", "minimum_storage_gb"):
        resolution = defaulted.field_resolutions[field]
        assert resolution.state == FieldResolutionState.AUTHORIZED_DEFAULT
        assert resolution.source_ref
        assert "公司标准" in next(
            item.quote
            for item in defaulted.evidence
            if item.evidence_id == resolution.evidence_ids[0]
        )
    second = validated("预算含税，送到雅加达", defaulted, "msg_2")
    assert second.status == RequirementStatus.READY_FOR_SOURCING
    assert service.to_procurement_requirement(second).minimum_ram_gb == 32
    assert (
        second.field_resolutions["minimum_ram_gb"].state == FieldResolutionState.AUTHORIZED_DEFAULT
    )
    third = validated("内存无要求", second, "msg_3")
    third = service.apply_authorized_defaults(third, minimum_ram_gb=32, minimum_storage_gb=1024)
    assert third.field_resolutions["minimum_ram_gb"].state == FieldResolutionState.NO_PREFERENCE
    assert service.to_procurement_requirement(third).minimum_ram_gb is None


def test_conflict_questions_and_other_missing_fields_are_all_present_and_resolvable():
    first = validated("采购20台笔记本，至少64GB内存，最多32GB内存")
    conflict = first.conflicts[0]
    assert any(
        item.resolves_conflict_id == conflict.conflict_id
        for item in first.clarification_plan.questions
    )
    assert {"budget.maximum", "budget.tax_included", "minimum_storage_gb", "delivery_city"} <= set(
        first.clarification_plan.covered_fields
    )
    second = validated("内存改成32GB", first, "msg_2")
    assert second.conflicts == []
    assert "minimum_ram_gb" not in second.clarification_plan.covered_fields
    assert len(first.conflicts) == 1


def test_legacy_requirement_and_run_snapshot_migrate_with_safe_state_defaults():
    spec = validated("采购20台笔记本")
    legacy = spec.model_dump(mode="json")
    legacy.pop("clarification_plan")
    legacy.pop("field_resolutions")
    legacy["schema_version"] = "1.0"
    restored = RequirementSpec.model_validate(legacy)
    assert restored.next_question == restored.clarification_plan.questions[0]
    run = ProcurementRun(tenant_id="demo_cn", user_id="employee_001")
    harness = AgentHarness(secret="test")
    harness.transition(run, RunState.WAITING_USER, reason="need clarification")
    payload = run.model_dump(mode="json")
    for field in (
        "contract_version",
        "wait_reason",
        "fact_evidence",
        "candidate_assessments",
        "negotiation_options",
        "turn_decisions",
        "processed_message_id",
        "recommendation_version",
        "displayed_candidate_refs",
    ):
        payload.pop(field)
    payload["requirement_spec"] = legacy
    migrated = ProcurementRun.model_validate(payload)
    assert migrated.wait_reason == RunWaitReason.MISSING_REQUIREMENT
    assert migrated.state_version == run.state_version
    assert migrated.fact_evidence == migrated.negotiation_options == migrated.turn_decisions == []
    assert migrated.recommendation_version == 0
    assert DEFAULT_AGENT_TOOL_NAMES == {"REQUIREMENT": ("query_procurement_policy", "query_purchase_history"), "SOURCING": ("estimate_delivery", "query_inventory", "query_warranty", "search_catalog")}
    with pytest.raises(ValueError, match="Illegal state transition"):
        harness.transition(migrated, RunState.REVIEWING, reason="candidate validation is required before review")


def test_fact_contract_cannot_turn_unavailable_response_into_zero_stock_or_remove_policy_gate():
    now = datetime.now(UTC)
    payload = dict(
        fact_id="fact_1",
        provider="internal_catalog",
        offer_id="offer_1",
        kind="INVENTORY",
        requirement_version=1,
        scope={"quantity": 20},
        source_ref="fixture://inventory/1",
        fetched_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    unavailable = OfferFact(**payload, status="UNAVAILABLE")
    assert unavailable.value is None
    with pytest.raises(ValidationError, match="cannot assert a value"):
        OfferFact(**payload, status="UNAVAILABLE", value={"stock": 0})
    with pytest.raises(ValidationError, match="require a value and evidence"):
        OfferFact(**payload, status="KNOWN", value={"stock": 20})
    with pytest.raises(ValidationError, match="expiry"):
        OfferFact(**{**payload, "expires_at": now}, status="UNKNOWN")
    with pytest.raises(ValidationError, match="not negotiable"):
        ConstraintCheck(
            constraint_id="policy_ram",
            field="minimum_ram_gb",
            authority="POLICY",
            result="FAIL",
            expected=32,
            actual=16,
            adjustable=True,
        )


@pytest.mark.parametrize("two_agent", [False, True])
@pytest.mark.asyncio
async def test_no_preference_round_trip_can_source_and_confirm_without_bypassing_policy(two_agent):
    engine = make_engine()
    if two_agent:
        engine.planner = TwoAgentPlanner(FixtureTwoAgentAdapter(), fallback_on_error=False)
    workflow = LangGraphProcurementWorkflow(engine, InMemorySaver(), checkpoint_backend="memory")
    started = await workflow.start_run(
        tenant_id="demo_cn", user_id="employee_001", message=BASE_TEXT
    )
    assert started.run.state == RunState.WAITING_USER
    assert started.run.wait_reason == RunWaitReason.MISSING_REQUIREMENT
    assert set(started.run.requirement_spec.clarification_plan.covered_fields) == {
        "minimum_ram_gb",
        "minimum_storage_gb",
    }
    result = await workflow.continue_run(
        run_id=started.run.run_id,
        tenant_id="demo_cn",
        user_id="employee_001",
        message="内存和硬盘无要求",
    )
    assert result.run.state == RunState.WAITING_CONFIRMATION
    assert result.run.requirement.minimum_ram_gb is None
    assert result.run.requirement.minimum_storage_gb is None
    assert result.run.recommendation.best_overall.offer.variant.ram_gb >= 32
    assert result.run.recommendation.best_overall.offer.variant.warranty_months >= 36
    assert result.run.fact_evidence
    assert {f.kind for f in result.run.fact_evidence} == {"INVENTORY", "WARRANTY"}
    registry = build_default_tool_registry(catalog=engine.catalog, enterprise=engine.enterprise)
    assert {item.name for item in registry.for_agent("SOURCING")} == {"search_catalog", "query_inventory", "estimate_delivery", "query_warranty"}
    completed = await workflow.confirm_run(
        run_id=result.run.run_id,
        tenant_id="demo_cn",
        user_id="employee_001",
        action_token=result.run.action_token,
    )
    assert completed.run.state == RunState.DRAFT_CREATED
    assert completed.run.wait_reason is None


@pytest.mark.asyncio
async def test_question_plan_and_no_preference_survive_database_checkpoint_recreation(tmp_path):
    database_url = f"sqlite+pysqlite:///{tmp_path / 'runs.db'}"
    repository = SqlAlchemyRunRepository(database_url)
    repository.create_schema()
    engine = make_engine()
    engine.repository = repository
    engine.execution_repository = SqlAlchemyExecutionRepository(engine=repository.engine)
    engine.planner = TwoAgentPlanner(FixtureTwoAgentAdapter(), fallback_on_error=False)
    checkpoint = str(tmp_path / "checkpoints.db")
    async with AsyncSqliteSaver.from_conn_string(checkpoint) as saver:
        workflow = LangGraphProcurementWorkflow(engine, saver, checkpoint_backend="sqlite")
        started = await workflow.start_run(
            tenant_id="demo_cn", user_id="employee_001", message="采购20台笔记本"
        )
        run_id = started.run.run_id
        first_question_ids = [
            item.question_id for item in started.run.requirement_spec.clarification_plan.questions
        ]
    replacement = make_engine()
    replacement.repository = SqlAlchemyRunRepository(database_url)
    replacement.execution_repository = SqlAlchemyExecutionRepository(
        engine=replacement.repository.engine
    )
    replacement.planner = TwoAgentPlanner(FixtureTwoAgentAdapter(), fallback_on_error=False)
    restored = replacement.repository.get(run_id, tenant_id="demo_cn", user_id="employee_001")
    assert [
        item.question_id for item in restored.requirement_spec.clarification_plan.questions
    ] == first_question_ids
    async with AsyncSqliteSaver.from_conn_string(checkpoint) as saver:
        workflow = LangGraphProcurementWorkflow(replacement, saver, checkpoint_backend="sqlite")
        second = await workflow.continue_run(
            run_id=run_id,
            tenant_id="demo_cn",
            user_id="employee_001",
            message="单台预算8000元含税，送雅加达，配置无要求",
        )
        assert second.run.state == RunState.WAITING_CONFIRMATION
        assert second.run.requirement_spec.version == 2
        assert len(second.run.requirement_spec_history) == 2
        assert (
            second.run.requirement_spec.field_resolutions["minimum_ram_gb"].state
            == FieldResolutionState.NO_PREFERENCE
        )
    replacement.repository.engine.dispose()
    repository.engine.dispose()


def test_api_exposes_question_plan_and_handles_post_recommendation_explanation(tmp_path):
    engine = make_engine()
    engine.planner = TwoAgentPlanner(FixtureTwoAgentAdapter(), fallback_on_error=False)
    headers = {"Authorization": "Bearer fixture-session-cn-employee-001"}
    with TestClient(
        create_app(settings=Settings(run_store_path=tmp_path / "runs.json"), engine=engine)
    ) as client:
        response = client.post("/api/runs", json={"message": "采购20台笔记本"}, headers=headers)
        assert response.status_code == 200
        run = response.json()["run"]
        assert len(run["requirement_spec"]["clarification_plan"]["questions"]) == 5
        assert run["wait_reason"] == "MISSING_REQUIREMENT"
        second = client.post(
            f"/api/runs/{run['run_id']}/messages",
            json={"message": "单台预算8000元含税，送雅加达，内存和硬盘无要求"},
            headers=headers,
        )
        assert second.status_code == 200
        assert second.json()["run"]["state"] == "WAITING_CONFIRMATION"
        blocked = client.post(
            f"/api/runs/{run['run_id']}/messages",
            json={"message": "解释一下推荐理由"},
            headers=headers,
        )
        assert blocked.status_code == 200
        assert blocked.json()["message_route"]["route"] == "CONTINUE"
        assert (
            client.get(f"/api/runs/{run['run_id']}", headers=headers).json()["state"]
            == "WAITING_CONFIRMATION"
        )


def test_requirement_output_supports_native_strict_schema_and_dotted_resolution_round_trip():
    from agents import AgentOutputSchema

    from procure_pilot.agents.provider_adapter import ProviderRequirementResult

    assert AgentOutputSchema(ProviderRequirementResult).is_strict_json_schema()
    spec = validated(BASE_TEXT)
    payload = spec.model_dump(mode="json")
    assert "budget.maximum" in payload["field_resolutions"]
    assert "budget_maximum" not in payload["field_resolutions"]
    restored = RequirementSpec.model_validate(payload)
    assert restored.field_resolutions["budget.maximum"] == spec.field_resolutions["budget.maximum"]


def test_a_change_to_one_field_and_a_preference_for_another_do_not_drop_old_hard_floor():
    first = validated(BASE_TEXT + "，最低32GB内存和1TB存储")
    second = validated("内存改成64GB，硬盘最好512GB", first, "msg_2")
    requirement = RequirementSpecService.to_procurement_requirement(second)
    assert requirement.minimum_ram_gb == 64
    assert requirement.minimum_storage_gb == 1024
    assert any(
        item.field == "minimum_storage_gb" and item.value == 512 for item in second.soft_preferences
    )


def test_model_adding_only_soft_preference_cannot_erase_previous_hard_floor():
    service = RequirementSpecService()
    first = validated(BASE_TEXT + "，最低32GB内存和1TB存储")
    text = "内存最好64GB"
    proposed = service.propose(run_id="run_test", message_id="msg_2", text=text, base=first)
    proposed.hard_constraints = [
        item for item in proposed.hard_constraints if item.field != "minimum_ram_gb"
    ]
    second = service.validate(proposed, message_id="msg_2", text=text, base=first)
    assert service.to_procurement_requirement(second).minimum_ram_gb == 32
    assert any(
        item.value == 64 for item in second.soft_preferences if item.field == "minimum_ram_gb"
    )


def test_compatible_lower_bounds_preserve_the_strongest_explicit_requirement():
    spec = validated(BASE_TEXT + "，至少64GB内存，至少32GB内存，最低1TB存储")
    assert not spec.conflicts
    assert RequirementSpecService.to_procurement_requirement(spec).minimum_ram_gb == 64
