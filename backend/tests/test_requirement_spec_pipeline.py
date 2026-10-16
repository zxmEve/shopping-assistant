from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from procure_pilot.agents.contracts import AgentKind, AgentResult
from procure_pilot.agents.multi_agent import AdapterCall, FixtureTwoAgentAdapter, TwoAgentPlanner
from procure_pilot.agents.provider_adapter import ProviderAgentResult
from procure_pilot.agents.tools import AgentCandidateProposal
from procure_pilot.domain.models import RunState
from procure_pilot.domain.requirements import (
    ConstraintStrength,
    RequirementAuthority,
    RequirementConstraint,
    RequirementStatus,
)
from procure_pilot.evals.runner import COMPLETE_REQUEST, make_engine
from procure_pilot.services.requirement_validation import RequirementSpecService
from procure_pilot.workflow import LangGraphProcurementWorkflow


class BoundaryRecordingAdapter(FixtureTwoAgentAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[AgentKind, tuple[str, ...]]] = []

    async def execute_agent(self, envelope, *, context, allowed_tools):
        self.calls.append((envelope.task.agent, allowed_tools))
        return await super().execute_agent(
            envelope, context=context, allowed_tools=allowed_tools
        )


class MirroredRequirementEvidenceAdapter(FixtureTwoAgentAdapter):
    async def execute_agent(self, envelope, *, context, allowed_tools):
        call = await super().execute_agent(
            envelope, context=context, allowed_tools=allowed_tools
        )
        if envelope.task.agent == AgentKind.REQUIREMENT:
            assert call.output.requirement_spec is not None
            call.output.evidence_refs = [
                item.evidence_id for item in call.output.requirement_spec.evidence
            ]
        return call


class InvalidHistoricalEvidenceAdapter(FixtureTwoAgentAdapter):
    async def execute_agent(self, envelope, *, context, allowed_tools):
        call = await super().execute_agent(
            envelope, context=context, allowed_tools=allowed_tools
        )
        if (
            envelope.task.agent == AgentKind.REQUIREMENT
            and context.run.requirement_spec is not None
            and call.output.requirement_spec is not None
        ):
            current_message = context.run.messages[-1]
            historical = call.output.requirement_spec.evidence[0]
            historical.message_id = current_message.message_id
            historical.start = 0
            historical.end = len(historical.quote)
        return call


@pytest.mark.asyncio
async def test_complete_input_runs_requirement_before_sourcing_with_reference_tools_available():
    adapter = BoundaryRecordingAdapter()
    engine = make_engine()
    engine.planner = TwoAgentPlanner(adapter, fallback_on_error=False)

    response = await engine.start(
        tenant_id="demo_cn", user_id="employee_001", message=COMPLETE_REQUEST
    )

    spec = response.run.requirement_spec
    assert spec is not None
    assert spec.authority == RequirementAuthority.VALIDATED
    assert spec.status == RequirementStatus.READY_FOR_SOURCING
    assert spec.base_version == 0 and spec.version == 1
    assert spec.delta.from_version == 0 and spec.delta.to_version == 1
    assert adapter.calls == [
        (AgentKind.REQUIREMENT, ("query_procurement_policy", "query_purchase_history")),
        (AgentKind.SOURCING, ("estimate_delivery", "query_inventory", "query_warranty", "search_catalog")),
    ]
    assert response.run.state == RunState.WAITING_CONFIRMATION
    assert response.run.validated_candidate_set is not None
    assert response.run.sourcing_ranking is not None
    assert response.run.recommendation is not None
    assert response.run.recommendation.candidate_set_hash


@pytest.mark.asyncio
async def test_requirement_evidence_refs_are_not_mistaken_for_catalog_output():
    engine = make_engine()
    engine.planner = TwoAgentPlanner(
        MirroredRequirementEvidenceAdapter(), fallback_on_error=False
    )

    response = await engine.start(
        tenant_id="demo_cn", user_id="employee_001", message=COMPLETE_REQUEST
    )

    assert response.run.state == RunState.WAITING_CONFIRMATION
    assert response.run.requirement_spec is not None


@pytest.mark.asyncio
async def test_langgraph_routes_requirement_validation_into_sourcing_in_one_turn():
    adapter = BoundaryRecordingAdapter()
    engine = make_engine()
    engine.planner = TwoAgentPlanner(adapter, fallback_on_error=False)
    workflow = LangGraphProcurementWorkflow(
        engine, InMemorySaver(), checkpoint_backend="memory"
    )

    response = await workflow.start_run(
        tenant_id="demo_cn", user_id="employee_001", message=COMPLETE_REQUEST
    )
    executions = engine.execution_repository.list_agents(
        response.run.run_id, tenant_id="demo_cn"
    )

    assert response.run.state == RunState.WAITING_CONFIRMATION
    assert adapter.calls == [
        (AgentKind.REQUIREMENT, ("query_procurement_policy", "query_purchase_history")),
        (AgentKind.SOURCING, ("estimate_delivery", "query_inventory", "query_warranty", "search_catalog")),
    ]
    assert [item.node_name for item in executions] == [
        "requirement_agent",
        "sourcing_agent",
        "sourcing_rank",
    ]


@pytest.mark.asyncio
async def test_requirement_delta_is_versioned_and_all_current_questions_are_asked():
    engine = make_engine()
    engine.planner = TwoAgentPlanner(FixtureTwoAgentAdapter(), fallback_on_error=False)

    first = await engine.start(
        tenant_id="demo_cn",
        user_id="employee_001",
        message="采购20台开发笔记本",
    )
    assert first.run.requirement_spec is not None
    assert first.run.requirement_spec.version == 1
    assert first.run.requirement_spec.next_question is not None
    plan = first.run.requirement_spec.clarification_plan
    assert set(plan.covered_fields) == {item.field for item in first.run.requirement_spec.missing_fields}
    assert len(plan.questions) == 5
    assert first.assistant_message.count("？") == 5

    second = await engine.continue_run(
        run_id=first.run.run_id,
        tenant_id="demo_cn",
        user_id="employee_001",
        message="单台预算8000元含税，送雅加达，两周内，最低32GB内存和1TB存储",
    )
    spec = second.run.requirement_spec
    assert spec is not None
    assert spec.base_version == 1 and spec.version == 2
    assert spec.delta.from_version == 1 and spec.delta.to_version == 2
    assert len(second.run.requirement_spec_history) == 2
    assert second.run.state == RunState.WAITING_CONFIRMATION


@pytest.mark.asyncio
async def test_invalid_model_evidence_falls_back_without_stranding_workflow():
    engine = make_engine()
    engine.planner = TwoAgentPlanner(
        InvalidHistoricalEvidenceAdapter(), fallback_on_error=False
    )

    first = await engine.start(
        tenant_id="demo_cn",
        user_id="employee_001",
        message="采购20台开发笔记本，单台预算7000元，送到上海",
    )
    assert first.run.state == RunState.WAITING_USER

    second = await engine.continue_run(
        run_id=first.run.run_id,
        tenant_id="demo_cn",
        user_id="employee_001",
        message="最低32GB内存和1t硬盘，预算含税，两周内到货",
    )

    assert second.run.state == RunState.WAITING_CONFIRMATION
    assert second.run.requirement_spec is not None
    assert any(
        event.name == "requirement_spec.agent_output_rejected"
        and event.status == "DEGRADED"
        for event in second.run.trace
    )


def test_requirement_agent_contract_classifies_soft_preferences_and_conflicts():
    service = RequirementSpecService()
    spec = service.propose(
        run_id="run_contract",
        message_id="msg_contract",
        text=(
            "采购20台开发笔记本，单台预算8000元含税，送到上海，"
            "至少64GB内存，最多32GB内存，最低1TB存储，最好两周内到货"
        ),
        base=None,
    )

    assert any(item.strength == ConstraintStrength.HARD for item in spec.hard_constraints)
    assert any(item.strength == ConstraintStrength.SOFT for item in spec.soft_preferences)
    assert spec.conflicts and spec.conflicts[0].blocking
    assert spec.next_question is not None
    assert spec.next_question.resolves_conflict_id == spec.conflicts[0].conflict_id


def test_provider_boundary_rejects_extra_product_facts():
    with pytest.raises(ValidationError):
        ProviderAgentResult.model_validate(
            {
                "task_id": "requirement_1",
                "agent": "REQUIREMENT",
                "state_version": 0,
                "status": "SUCCEEDED",
                "product_id": "invented-product",
            }
        )


def test_validator_rejects_promoting_soft_language_to_hard_constraint():
    service = RequirementSpecService()
    proposed = service.propose(
        run_id="run_soft",
        message_id="msg_soft",
        text="采购20台笔记本，单台预算8000元含税，送上海，最好32GB内存，最低1TB存储",
        base=None,
    )
    preference = proposed.soft_preferences.pop()
    proposed.hard_constraints.append(
        preference.model_copy(update={"strength": ConstraintStrength.HARD})
    )

    with pytest.raises(ValueError, match="promoted"):
        service.validate(
            proposed,
            message_id="msg_soft",
            text="采购20台笔记本，单台预算8000元含税，送上海，最好32GB内存，最低1TB存储",
            base=None,
        )


def test_validator_repairs_unique_offsets_and_drops_invented_condition():
    service = RequirementSpecService()
    text = "采购20台笔记本，单台预算8000元含税，送上海，最低32GB内存和1TB硬盘"
    proposed = service.propose(
        run_id="run_grounding",
        message_id="msg_grounding",
        text=text,
        base=None,
    )
    evidence = proposed.evidence[0]
    evidence.start += 1
    evidence.end += 1
    proposed.hard_constraints.append(
        RequirementConstraint(
            field="condition",
            operator="EQ",
            value="NEW",
            strength=ConstraintStrength.HARD,
            confidence=0.9,
            evidence_ids=[evidence.evidence_id],
        )
    )

    validated = service.validate(
        proposed,
        message_id="msg_grounding",
        text=text,
        base=None,
    )

    assert text[evidence.start : evidence.end] == evidence.quote
    assert not any(item.field == "condition" for item in validated.hard_constraints)


def test_validator_supplements_only_explicit_required_fields_omitted_by_model():
    service = RequirementSpecService()
    text = "采购20台开发笔记本，单台预算8000元含税，送到雅加达，两周内，最低32GB内存和1TB硬盘"
    proposed = service.propose(
        run_id="run_omission",
        message_id="msg_omission",
        text=text,
        base=None,
    )
    quantity_refs = set(proposed.quantity.evidence_ids) if proposed.quantity else set()
    storage_refs = {
        ref
        for item in proposed.hard_constraints
        if item.field == "minimum_storage_gb"
        for ref in item.evidence_ids
    }
    removed_refs = quantity_refs | storage_refs
    proposed.quantity = None
    proposed.hard_constraints = [
        item
        for item in proposed.hard_constraints
        if item.field != "minimum_storage_gb"
    ]
    proposed.evidence = [
        item for item in proposed.evidence if item.evidence_id not in removed_refs
    ]
    proposed.delta.operations = [
        item
        for item in proposed.delta.operations
        if item.path not in {"quantity", "minimum_storage_gb"}
    ]

    validated = service.validate(
        proposed,
        message_id="msg_omission",
        text=text,
        base=None,
    )

    assert validated.quantity is not None
    assert validated.quantity.normalized_value == 20
    assert any(
        item.field == "minimum_storage_gb" and item.value == 1024
        for item in validated.hard_constraints
    )
    assert validated.status == RequirementStatus.READY_FOR_SOURCING


@pytest.mark.asyncio
async def test_missing_storage_is_clarified_instead_of_crashing_sourcing():
    engine = make_engine()
    engine.planner = TwoAgentPlanner(FixtureTwoAgentAdapter(), fallback_on_error=False)

    response = await engine.start(
        tenant_id="demo_cn",
        user_id="employee_001",
        message="采购20台笔记本，单台预算8000元含税，送到雅加达，两周内，最低32GB内存",
    )

    assert response.run.state == RunState.WAITING_USER
    assert response.run.requirement_spec is not None
    assert [item.field for item in response.run.requirement_spec.missing_fields] == [
        "minimum_storage_gb"
    ]
    assert "存储" in response.assistant_message


class WorstRankingAdapter(FixtureTwoAgentAdapter):
    async def rank_candidates(self, envelope, *, context, allowed_tools):
        candidate_set = envelope.validated_candidates
        assert candidate_set is not None
        selected = min(candidate_set.candidates, key=lambda item: item.utility_score)
        proposal = AgentCandidateProposal(
            provider=selected.provider,
            offer_id=selected.offer_id,
            evidence_refs=selected.evidence_refs,
        )
        return AdapterCall(
            AgentResult(
                task_id=envelope.task.task_id,
                agent=AgentKind.SOURCING,
                state_version=envelope.state_version,
                status="SUCCEEDED",
                output={
                    "ranking_rationale": "故意选择效用最低候选",
                    "candidate_set_hash": candidate_set.candidate_set_hash,
                    "requirement_spec_id": candidate_set.requirement_spec_id,
                    "requirement_spec_hash": candidate_set.requirement_spec_hash,
                    "requirement_version": candidate_set.requirement_version,
                },
                evidence_refs=selected.evidence_refs,
                candidate_proposals=[proposal],
            )
        )


@pytest.mark.asyncio
async def test_regret_guard_rejects_a_grounded_but_materially_worse_ranking():
    engine = make_engine()
    engine.planner = TwoAgentPlanner(WorstRankingAdapter(), fallback_on_error=False)

    response = await engine.start(
        tenant_id="demo_cn", user_id="employee_001", message=COMPLETE_REQUEST
    )

    assert response.run.recommendation is not None
    assert response.run.recommendation.selection_source == "deterministic_score"
    assert any(
        event.name == "agent.recommendation_rejected"
        and event.detail.get("reason") == "utility regret exceeds guardrail"
        for event in response.run.trace
    )
