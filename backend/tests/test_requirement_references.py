from __future__ import annotations

from datetime import UTC, datetime

import pytest
from agents import AgentOutputSchema
from fastapi.testclient import TestClient
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from procure_pilot.agents.multi_agent import FixtureTwoAgentAdapter, TwoAgentPlanner
from procure_pilot.api.app import create_app
from procure_pilot.config import Settings
from procure_pilot.domain.models import RunState, ToolBudgetPool
from procure_pilot.domain.requirements import RequirementSpec
from procure_pilot.evals.runner import COMPLETE_REQUEST, make_engine
from procure_pilot.gateway.tool_gateway import GatewayExecutionContext
from procure_pilot.persistence.execution_repository import SqlAlchemyExecutionRepository
from procure_pilot.persistence.repository import SqlAlchemyRunRepository
from procure_pilot.services.requirement_resolution import draft_hash
from procure_pilot.workflow import LangGraphProcurementWorkflow, ProcurementWorkflowRuntime

ID = dict(tenant_id="demo_cn", user_id="employee_001")
AUTH = {"Authorization": "Bearer fixture-session-cn-employee-001"}
OTHER = {"Authorization": "Bearer fixture-session-id-employee-101"}
POLICY = "采购20台开发笔记本，单台预算8000元含税，送雅加达，配置按制度来"
HISTORY = "采购10台笔记本，其他参考上次采购"


async def start(text=POLICY, *, fixture=False, backend="langgraph"):
    engine = make_engine()
    if fixture:
        engine.planner = TwoAgentPlanner(FixtureTwoAgentAdapter(), fallback_on_error=False)
    runtime = ProcurementWorkflowRuntime(engine, Settings(workflow_backend=backend))
    response = await runtime.start_run(**ID, message=text)
    return engine, runtime, response


async def reply(runtime, run, text, request="reply", **extra):
    return await runtime.route_message(
        **ID, run_id=run.run_id, message=text, request_id=request, **extra
    )


async def approve(runtime, run, request="approve"):
    draft = run.resolution_draft
    return await runtime.confirm_requirements(
        **ID,
        run_id=run.run_id,
        request_id=request,
        draft_id=draft.draft_id,
        draft_version=draft.version,
        content_hash=draft.content_hash,
    )


def tool_names(engine, run):
    return [
        t.tool_name
        for t in engine.execution_repository.list_tools(run.run_id, tenant_id=run.tenant_id)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture", [False, True])
@pytest.mark.parametrize("backend", ["langgraph", "legacy"])
async def test_policy_missing_storage_then_confirmation_then_sourcing_and_separate_draft(
    fixture, backend
):
    e, r, response = await start(fixture=fixture, backend=backend)
    assert response.run.wait_reason.value == "MISSING_REQUIREMENT"
    d = response.run.resolution_draft
    assert d.missing_fields == ["minimum_storage_gb"]
    assert response.run.requirement.minimum_ram_gb is None
    assert "search_catalog" not in tool_names(e, response.run)
    assert "query_procurement_policy" in tool_names(e, response.run)
    ready = await reply(r, response.run, "硬盘1TB")
    assert ready.run.wait_reason.value == "REQUIREMENT_CONFIRMATION"
    assert ready.run.requirement.minimum_ram_gb is None
    assert ready.run.resolution_draft.content_hash == draft_hash(ready.run.resolution_draft)
    sourced = await approve(r, ready.run)
    assert sourced.run.state == RunState.WAITING_CONFIRMATION
    assert sourced.run.requirement.minimum_ram_gb == 32
    assert sourced.run.requirement.minimum_warranty_months == 36
    assert sourced.run.draft is None
    assert sourced.run.resolution_draft.status == "CONFIRMED"
    assert any(
        e.kind == "POLICY" and e.start is None and e.end is None
        for e in sourced.run.requirement_spec.evidence
    )
    done = await r.confirm_run(
        **ID, run_id=sourced.run.run_id, action_token=sourced.run.action_token
    )
    assert done.run.state == RunState.DRAFT_CREATED


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture", [False, True])
async def test_complete_direct_input_with_policy_reference_still_needs_requirement_confirmation(
    fixture,
):
    e, r, response = await start(COMPLETE_REQUEST + "，按公司标准", fixture=fixture)
    assert response.run.state == RunState.WAITING_USER
    assert response.run.wait_reason.value == "REQUIREMENT_CONFIRMATION"
    assert not response.run.recommendation and not response.run.action_token
    assert "search_catalog" not in tool_names(e, response.run)
    with pytest.raises(ValueError, match="confirmed"):
        await e._source_and_recommend(response.run, scenario=None)
    ctx = GatewayExecutionContext(
        run=response.run,
        roles=("EMPLOYEE",),
        node_name="test",
        budget_pool=ToolBudgetPool.WORKFLOW_READ,
    )
    with pytest.raises(ValueError, match="confirmed"):
        await e.tool_gateway.invoke(
            tool_name="search_catalog", arguments={"provider": "internal_catalog"}, context=ctx
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture", [False, True])
async def test_history_latest_combines_current_user_fields_with_history_and_checks_policy(fixture):
    e, r, response = await start(HISTORY, fixture=fixture)
    assert response.run.wait_reason.value == "REQUIREMENT_CONFIRMATION"
    assert response.run.resolution_draft.selected_order_id == "HIST-CN-001"
    assert response.run.requirement.unit_budget is None
    values = {v.field: v for v in response.run.resolution_draft.fields}
    assert values["quantity"].value == 10 and values["quantity"].source == "USER"
    assert values["budget.maximum"].source == "HISTORY"
    assert values["delivery_city"].value == "Jakarta"
    assert {"query_purchase_history", "query_procurement_policy"}.issubset(
        tool_names(e, response.run)
    )
    assert "search_catalog" not in tool_names(e, response.run)
    sourced = await approve(r, response.run)
    assert sourced.run.state == RunState.WAITING_CONFIRMATION
    assert sourced.run.requirement.quantity == 10
    # User corrections preserve confirmed external provenance without another reference request.
    updated = await reply(r, sourced.run, "数量改成12台", request="adjust")
    assert updated.run.requirement.quantity == 12
    assert updated.run.requirement.unit_budget.amount == sourced.run.requirement.unit_budget.amount
    assert updated.run.requirement.minimum_ram_gb == 32
    assert (
        updated.run.confirmed_requirement_drafts[0].history_snapshot.source_version
        == "fixture-history-v1"
    )
    assert (
        updated.run.confirmed_requirement_drafts[0].content_hash
        == response.run.resolution_draft.content_hash
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture", [False, True])
async def test_multiple_history_matches_need_selection_then_confirmation_and_upgrade_old_ram(
    fixture,
):
    e, r, response = await start("采购10台笔记本，根据历史订单来", fixture=fixture)
    assert response.run.wait_reason.value == "HISTORY_SELECTION"
    with pytest.raises(ValueError):
        await approve(r, response.run)
    selected = await reply(r, response.run, "参考订单 HIST-CN-002")
    assert selected.run.wait_reason.value == "REQUIREMENT_CONFIRMATION"
    fields = {v.field: v for v in selected.run.resolution_draft.fields}
    assert fields["minimum_ram_gb"].value == 32 and fields["minimum_ram_gb"].source == "POLICY"
    assert (
        fields["minimum_storage_gb"].value == 512
        and fields["minimum_storage_gb"].source == "HISTORY"
    )
    assert "search_catalog" not in tool_names(e, selected.run)
    assert (await approve(r, selected.run)).run.state == RunState.WAITING_CONFIRMATION


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "order_id", ["HIST-ID-001", "HIST-CN-003", "HIST-CN-CANCELLED", "HIST-CN-DRAFT", "HIST-NO-000"]
)
async def test_foreign_private_cancelled_draft_and_unknown_orders_cannot_be_reused(order_id):
    e, r, response = await start(f"采购10台笔记本，参考历史订单 {order_id}")
    assert response.run.wait_reason.value == "REFERENCE_UNAVAILABLE"
    assert response.run.resolution_draft.status == "BLOCKED"
    assert response.run.requirement.unit_budget is None
    with pytest.raises(ValueError):
        await approve(r, response.run)
    assert "search_catalog" not in tool_names(e, response.run)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["公司笔记本采购制度有哪些？", "查询一下历史采购订单"])
async def test_query_only_answers_do_not_start_sourcing_or_apply_external_fields(text):
    e, r, response = await start(text)
    # Category is required before a policy-only lookup; explicit category supports direct information query.
    if response.run.wait_reason.value == "MISSING_REQUIREMENT":
        response = await reply(r, response.run, "笔记本")
    assert response.run.wait_reason.value == "REFERENCE_INFORMATION"
    assert response.run.requirement.minimum_ram_gb is None
    assert response.run.requirement.unit_budget is None
    assert "search_catalog" not in tool_names(e, response.run)


@pytest.mark.asyncio
async def test_withdraw_history_discards_pending_suggestions_but_keeps_user_fields():
    e, r, response = await start(HISTORY)
    updated = await reply(r, response.run, "不要参考历史，内存32GB，硬盘1TB")
    assert updated.run.reference_request is None
    assert updated.run.resolution_draft is None
    assert updated.run.requirement.quantity == 10
    assert updated.run.requirement.unit_budget is None
    assert updated.run.state == RunState.WAITING_USER


@pytest.mark.asyncio
async def test_stale_confirmation_and_history_view_are_rejected_without_consuming_wait():
    e, r, response = await start(HISTORY)
    d = response.run.resolution_draft.model_copy(deep=True)
    changed = await reply(r, response.run, "预算改成7000元")
    assert changed.run.resolution_draft.version > d.version
    with pytest.raises(ValueError, match="stale"):
        await approve(r, response.run)
    assert (await r.status(**ID, run_id=response.run.run_id)).waiting_at == "wait_for_user"
    with pytest.raises(ValueError, match="stale"):
        await reply(
            r,
            changed.run,
            "参考订单 HIST-CN-002",
            request="staleview",
            expected_resolution_draft_id=d.draft_id,
            expected_resolution_version=d.version,
        )
    assert (await approve(r, changed.run, request="new")).run.state == RunState.WAITING_CONFIRMATION


@pytest.mark.asyncio
async def test_policy_change_invalidates_confirmation_and_refreshes_review():
    e, r, response = await start(HISTORY)
    old = response.run.resolution_draft
    e.enterprise.policy["version"] = 4
    e.enterprise.policy["developer_laptop_baseline"]["minimum_ram_gb"] = 64
    with pytest.raises(ValueError, match="sources changed"):
        await approve(r, response.run)
    current = e.get(response.run.run_id, **ID)
    assert current.resolution_draft.version > old.version
    assert current.wait_reason.value == "REQUIREMENT_CONFIRMATION"
    assert {v.field: v.value for v in current.resolution_draft.fields}["minimum_ram_gb"] == 64
    assert (await r.status(**ID, run_id=current.run_id)).waiting_at == "wait_for_user"
    assert not current.requirement_confirmations


@pytest.mark.asyncio
async def test_history_access_change_invalidates_confirmation():
    e, r, response = await start(HISTORY)
    e.purchase_history.orders = [
        o for o in e.purchase_history.orders if o.order_id != "HIST-CN-001"
    ]
    with pytest.raises(ValueError, match="sources changed"):
        await approve(r, response.run)
    current = e.get(response.run.run_id, **ID)
    assert current.resolution_draft.status == "BLOCKED"
    assert current.requirement.unit_budget is None


@pytest.mark.asyncio
async def test_confirm_idempotence_and_different_payload_rejection():
    e, r, response = await start(HISTORY)
    done = await approve(r, response.run)
    again = await approve(r, response.run)
    assert again.run.requirement.version == done.run.requirement.version
    assert len(again.run.requirement_confirmations) == 1
    with pytest.raises(ValueError, match="different input"):
        await r.confirm_requirements(
            **ID,
            run_id=done.run.run_id,
            request_id="approve",
            draft_id="wrong",
            draft_version=1,
            content_hash="a" * 64,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault_node", ["accept_clarification", "accept_clarification:after", "sourcing_agent:after"]
)
async def test_confirmation_retry_recovers_without_duplicate_acceptance(fault_node):
    e, r, response = await start(HISTORY, fixture=True)
    fired = []

    def fault(node, state):
        if node == fault_node and not fired:
            fired.append(node)
            raise RuntimeError("worker stopped")

    r.workflow._fault_injector = fault
    with pytest.raises(RuntimeError, match="worker stopped"):
        await approve(r, response.run)
    done = await approve(r, response.run)
    assert done.run.state == RunState.WAITING_CONFIRMATION
    assert len(done.run.requirement_confirmations) == 1
    consent = done.run.requirement_confirmations[0]
    assert len([m for m in done.run.messages if m.message_id == consent.message_id]) == 1
    assert len([t for t in done.run.trace if t.name == "requirement_draft.confirmed"]) == 1


@pytest.mark.asyncio
async def test_sources_changed_after_interrupt_before_acceptance_rearm_the_review():
    e, r, response = await start(HISTORY)
    fired = []

    def fault(node, state):
        if node == "accept_clarification" and not fired:
            fired.append(node)
            raise RuntimeError("worker stopped")

    r.workflow._fault_injector = fault
    with pytest.raises(RuntimeError):
        await approve(r, response.run)
    e.enterprise.policy["version"] = 4
    with pytest.raises(ValueError, match="sources changed"):
        await approve(r, response.run)
    current = e.get(response.run.run_id, **ID)
    assert current.requirement_confirmations[0].status == "INVALIDATED"
    assert (await r.status(**ID, run_id=current.run_id)).waiting_at == "wait_for_user"
    done = await approve(r, current, request="new_policy")
    assert done.run.state == RunState.WAITING_CONFIRMATION


@pytest.mark.asyncio
async def test_reference_tools_require_user_authorization_and_requirement_role():
    e, _, response = await start(COMPLETE_REQUEST)
    ctx = GatewayExecutionContext(
        run=response.run,
        roles=("EMPLOYEE",),
        node_name="test",
        budget_pool=ToolBudgetPool.WORKFLOW_READ,
    )
    for tool in ["query_purchase_history", "query_procurement_policy"]:
        with pytest.raises(PermissionError):
            await e.tool_gateway.invoke(tool_name=tool, arguments={}, context=ctx)
    e, _, response = await start(HISTORY)
    ctx.run = response.run
    ctx.agent_kind = "SOURCING"
    with pytest.raises(PermissionError):
        await e.tool_gateway.invoke(tool_name="query_purchase_history", arguments={}, context=ctx)


@pytest.mark.asyncio
async def test_no_reference_request_means_no_reference_tool_calls_even_in_fixture_agent():
    e, _, response = await start(COMPLETE_REQUEST, fixture=True)
    assert response.run.state == RunState.WAITING_CONFIRMATION
    assert not {"query_purchase_history", "query_procurement_policy"}.intersection(
        tool_names(e, response.run)
    )
    assert AgentOutputSchema(RequirementSpec).is_strict_json_schema()


@pytest.mark.asyncio
async def test_read_fallback_still_requires_confirmation_if_model_omits_tools():
    class OmitsTools(FixtureTwoAgentAdapter):
        async def execute_agent(self, envelope, *, context, allowed_tools):
            if envelope.task.agent.value == "REQUIREMENT":
                request = context.run.reference_request
                context.run.reference_request = None
                try:
                    return await super().execute_agent(
                        envelope, context=context, allowed_tools=allowed_tools
                    )
                finally:
                    context.run.reference_request = request
            return await super().execute_agent(
                envelope, context=context, allowed_tools=allowed_tools
            )

    e = make_engine()
    e.planner = TwoAgentPlanner(OmitsTools(), fallback_on_error=False)
    r = ProcurementWorkflowRuntime(e, Settings())
    response = await r.start_run(**ID, message=HISTORY)
    assert response.run.wait_reason.value == "REQUIREMENT_CONFIRMATION"
    assert {"query_procurement_policy", "query_purchase_history"}.issubset(
        tool_names(e, response.run)
    )
    assert "search_catalog" not in tool_names(e, response.run)


@pytest.mark.asyncio
async def test_forged_external_reference_or_modified_confirmed_value_is_rejected():
    e, r, response = await start(HISTORY)
    sourced = await approve(r, response.run)
    base = sourced.run.requirement_spec
    proposal = e.requirement_specs.propose(
        run_id=base.run_id, message_id="next", text="继续", base=base
    )
    proposal.budget.maximum.normalized_value.amount = 999
    with pytest.raises(ValueError, match="External requirement value"):
        e.requirement_specs.validate(proposal, message_id="next", text="继续", base=base)
    proposal = e.requirement_specs.propose(
        run_id=base.run_id, message_id="next", text="继续", base=base
    )
    external = next(v for v in proposal.evidence if v.kind == "HISTORY")
    external.quote = "伪造的历史条款"
    with pytest.raises(ValueError, match="external requirement evidence"):
        e.requirement_specs.validate(proposal, message_id="next", text="继续", base=base)


@pytest.mark.asyncio
async def test_requirement_review_survives_sql_and_checkpoint_recreation(tmp_path):
    url = f"sqlite:///{tmp_path / 'runs.sqlite3'}"

    def engine():
        e = make_engine()
        e.repository = SqlAlchemyRunRepository(url)
        e.repository.create_schema()
        e.execution_repository = SqlAlchemyExecutionRepository(engine=e.repository.engine)
        e.tool_gateway.execution_repository = e.execution_repository
        return e

    db = str(tmp_path / "checkpoints.sqlite3")
    async with AsyncSqliteSaver.from_conn_string(db) as saver:
        e = engine()
        r = ProcurementWorkflowRuntime(e, Settings())
        r.workflow = LangGraphProcurementWorkflow(e, saver, checkpoint_backend="sqlite")
        response = await r.start_run(**ID, message=HISTORY)
        snapshot = response.run.resolution_draft.model_dump(mode="json")
    async with AsyncSqliteSaver.from_conn_string(db) as saver:
        e2 = engine()
        r2 = ProcurementWorkflowRuntime(e2, Settings())
        r2.workflow = LangGraphProcurementWorkflow(e2, saver, checkpoint_backend="sqlite")
        restored = e2.get(response.run.run_id, **ID)
        assert restored.resolution_draft.model_dump(mode="json") == snapshot
        done = await approve(r2, restored)
        assert done.run.state == RunState.WAITING_CONFIRMATION


def test_api_confirmation_is_owner_scoped_and_independent_of_purchase_draft(tmp_path):
    e = make_engine()
    with TestClient(
        create_app(settings=Settings(run_store_path=tmp_path / "unused.json"), engine=e)
    ) as client:
        run = client.post("/api/runs", headers=AUTH, json={"message": HISTORY}).json()["run"]
        draft = run["resolution_draft"]
        body = dict(
            request_id="approve",
            draft_id=draft["draft_id"],
            draft_version=draft["version"],
            content_hash=draft["content_hash"],
        )
        assert (
            client.post(
                f"/api/runs/{run['run_id']}/requirement-confirmations", headers=OTHER, json=body
            ).status_code
            == 403
        )
        invalid = {**body, "content_hash": "f" * 64}
        assert (
            client.post(
                f"/api/runs/{run['run_id']}/requirement-confirmations", headers=AUTH, json=invalid
            ).status_code
            == 409
        )
        good = client.post(
            f"/api/runs/{run['run_id']}/requirement-confirmations", headers=AUTH, json=body
        )
        assert good.status_code == 200
        assert good.json()["run"]["state"] == "WAITING_CONFIRMATION"
        assert good.json()["run"]["draft"] is None
        assert (
            client.post(
                f"/api/runs/{run['run_id']}/requirement-confirmations", headers=AUTH, json=body
            ).status_code
            == 200
        )


@pytest.mark.asyncio
async def test_model_cannot_invent_user_quantity_from_history_authorization():
    from procure_pilot.domain.requirements import SourcedRequirementValue

    e, _, response = await start("笔记本，参考上次采购")
    base = response.run.requirement_spec
    proposal = e.requirement_specs.propose(
        run_id=base.run_id, message_id="new", text="参考历史订单", base=base
    )
    from procure_pilot.domain.requirements import RequirementEvidence

    ev = RequirementEvidence(message_id="new", quote="参考历史订单", start=0, end=6)
    proposal.evidence.append(ev)
    proposal.quantity = SourcedRequirementValue(
        value="参考历史订单", normalized_value=999, confidence=1, evidence_ids=[ev.evidence_id]
    )
    with pytest.raises(ValueError, match="numeric value"):
        e.requirement_specs.validate(proposal, message_id="new", text="参考历史订单", base=base)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["确认需求并开始寻源", "就按这个来", "可以"])
async def test_natural_confirmation_and_model_reference_bindings_cannot_bypass_review(text):
    e, r, response = await start(HISTORY, fixture=True)
    assert response.run.resolution_draft.status == "READY"
    result = await reply(r, response.run, text, request="natural")
    assert result.run.state == RunState.WAITING_CONFIRMATION
    assert result.run.draft is None
    assert len(result.run.requirement_confirmations) == 1


@pytest.mark.asyncio
async def test_policy_unavailable_and_empty_history_wait_without_sourcing():
    e = make_engine()
    e.enterprise.policy["effective_from"] = datetime(2099, 1, 1, tzinfo=UTC)
    r = ProcurementWorkflowRuntime(e, Settings())
    unavailable = await r.start_run(**ID, message=HISTORY)
    assert unavailable.run.wait_reason.value == "REFERENCE_UNAVAILABLE"
    assert "search_catalog" not in tool_names(e, unavailable.run)
    e2 = make_engine()
    e2.purchase_history.orders = []
    r2 = ProcurementWorkflowRuntime(e2, Settings())
    empty = await r2.start_run(**ID, message=HISTORY)
    assert empty.run.resolution_draft.status == "BLOCKED"
    assert "没有可访问" in empty.assistant_message


@pytest.mark.asyncio
async def test_requirement_tool_queries_are_recovered_from_agent_journal(tmp_path):
    e = make_engine()
    e.planner = TwoAgentPlanner(FixtureTwoAgentAdapter(), fallback_on_error=False)
    calls = []
    original = e.reference_reader.purchase_history

    def counted(run):
        calls.append(run.run_id)
        return original(run)

    e.reference_reader.purchase_history = counted
    r = ProcurementWorkflowRuntime(e, Settings())
    fired = []

    def fault(node, state):
        if node == "requirement_agent:after" and not fired:
            fired.append(node)
            raise RuntimeError("worker stopped after lookup")

    r.workflow._fault_injector = fault
    with pytest.raises(RuntimeError):
        await r.start_run(**ID, message=HISTORY)
    saved = e.repository.list(**ID)[0]
    restored = await r.resume_pending(**ID, run_id=saved.run_id)
    assert restored.run.wait_reason.value == "REQUIREMENT_CONFIRMATION"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_model_field_bindings_choose_external_fields_and_preserve_explicit_destination():
    from procure_pilot.domain.requirements import RequirementReferenceBinding

    class SelectStorage(FixtureTwoAgentAdapter):
        async def execute_agent(self, envelope, *, context, allowed_tools):
            call = await super().execute_agent(
                envelope, context=context, allowed_tools=allowed_tools
            )
            if envelope.task.agent.value == "REQUIREMENT" and context.run.reference_history:
                storage = next(
                    v
                    for v in context.run.reference_history.orders[0].fields
                    if v.field == "minimum_storage_gb"
                )
                call.output.requirement_spec.reference_bindings = [
                    RequirementReferenceBinding(
                        field=storage.field, evidence_id=storage.evidence_id
                    )
                ]
            return call

    e = make_engine()
    e.planner = TwoAgentPlanner(SelectStorage(), fallback_on_error=False)
    r = ProcurementWorkflowRuntime(e, Settings())
    response = await r.start_run(
        **ID, message="采购10台笔记本，预算8000元含税，送上海，其他参考上次采购"
    )
    fields = {v.field: v for v in response.run.resolution_draft.fields}
    assert fields["delivery_city"].value == "Shanghai" and fields["delivery_city"].source == "USER"
    assert fields["minimum_storage_gb"].source == "HISTORY"
    assert fields["minimum_ram_gb"].source == "POLICY"
    assert "purpose" not in fields
    assert response.run.wait_reason.value == "REQUIREMENT_CONFIRMATION"


@pytest.mark.asyncio
async def test_history_date_constraint_does_not_fall_back_to_another_year():
    _, _, response = await start("采购10台笔记本，参考2025年历史订单")
    assert response.run.reference_request.history_year == 2025
    assert response.run.reference_history.orders == []
    assert response.run.resolution_draft.status == "BLOCKED"


@pytest.mark.asyncio
async def test_explicit_no_storage_preference_with_policy_reference_can_be_confirmed():
    e, r, response = await start(POLICY + "，硬盘无要求")
    assert response.run.resolution_draft.status == "READY"
    sourced = await approve(r, response.run)
    assert sourced.run.state == RunState.WAITING_CONFIRMATION
    assert sourced.run.requirement.minimum_storage_gb is None
    assert sourced.run.requirement.minimum_ram_gb == 32


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "deepseek"])
async def test_provider_requirement_tools_are_read_only_and_routing_stays_tool_free(provider):
    from procure_pilot.agents.contracts import AgentKind
    from procure_pilot.agents.provider_adapter import OpenAICompatibleTwoAgentAdapter

    adapter = OpenAICompatibleTwoAgentAdapter(
        api_key="unused",
        provider=provider,
        requirement_model="unused-req",
        sourcing_model="unused-source",
    )
    requirement = adapter.agents[AgentKind.REQUIREMENT]
    assert {t.name for t in requirement.tools} == {
        "query_procurement_policy",
        "query_purchase_history",
    }
    assert all(callable(t.is_enabled) for t in requirement.tools)
    for tool in requirement.tools:
        assert tool.params_json_schema["additionalProperties"] is False
    assert adapter.agents[AgentKind.SOURCING].model is not requirement.model


@pytest.mark.asyncio
async def test_current_knowledge_service_version_conflict_blocks_reference_draft():
    from procure_pilot.domain.knowledge import PolicyKnowledgeReviewRequired

    class ConflictingKnowledge:
        def verify_policy_evidence(self, **kwargs):
            raise PolicyKnowledgeReviewRequired("POLICY_VERSION_CONFLICT", "版本冲突")

    e = make_engine()
    e.knowledge_service = ConflictingKnowledge()
    r = ProcurementWorkflowRuntime(e, Settings())
    response = await r.start_run(**ID, message=HISTORY)
    assert response.run.reference_policy.status == "UNAVAILABLE"
    assert response.run.resolution_draft.status == "BLOCKED"
    assert "POLICY_VERSION_CONFLICT" in response.assistant_message
    assert "search_catalog" not in tool_names(e, response.run)


@pytest.mark.asyncio
async def test_policy_floor_overrides_no_extra_ram_preference_without_breaking_future_edits():
    _, r, response = await start(POLICY)
    ready = await reply(r, response.run, "内存无要求，硬盘1TB")
    sourced = await approve(r, ready.run)
    assert sourced.run.requirement.minimum_ram_gb == 32
    assert (
        sourced.run.requirement_spec.field_resolutions["minimum_ram_gb"].state.value
        == "CONFIRMED_REFERENCE"
    )
    changed = await reply(r, sourced.run, "数量改成10台", request="next")
    assert changed.run.state == RunState.WAITING_CONFIRMATION
    assert changed.run.requirement.minimum_ram_gb == 32
