from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from agents import AgentOutputSchema
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from procure_pilot.agents.contracts import AgentKind
from procure_pilot.agents.multi_agent import FixtureTwoAgentAdapter, TwoAgentPlanner
from procure_pilot.agents.provider_adapter import OpenAICompatibleTwoAgentAdapter
from procure_pilot.agents.tools import AgentRuntimeContext
from procure_pilot.config import Settings
from procure_pilot.domain.conversation import DialogueProposal, DialogueSelection
from procure_pilot.domain.models import RunState, ToolBudgetPool
from procure_pilot.evals.runner import COMPLETE_REQUEST, make_engine
from procure_pilot.gateway.tool_gateway import GatewayExecutionContext
from procure_pilot.persistence.execution_repository import SqlAlchemyExecutionRepository
from procure_pilot.persistence.repository import SqlAlchemyRunRepository
from procure_pilot.services.dialogue import classify_turn_rules
from procure_pilot.services.sourcing import EvidenceRequirementPolicy, current_facts
from procure_pilot.workflow import LangGraphProcurementWorkflow, ProcurementWorkflowRuntime

ID = dict(tenant_id="demo_cn", user_id="employee_001")


async def start(message=COMPLETE_REQUEST, scenario=None, *, fixture=False):
    engine = make_engine()
    if fixture:
        engine.planner = TwoAgentPlanner(FixtureTwoAgentAdapter(), fallback_on_error=False)
    runtime = ProcurementWorkflowRuntime(engine, Settings())
    response = await runtime.start_run(**ID, message=message, scenario=scenario)
    return engine, runtime, response


async def reply(runtime, run, text, request_id="turn", **kw):
    return await runtime.route_message(
        **ID, run_id=run.run_id, message=text, request_id=request_id, **kw
    )


@pytest.mark.parametrize(
    ("text", "intent", "target", "pref"),
    [
        ("解释一下推荐理由", "EXPLAIN", None, None),
        ("第二款保修多久", "EXPLAIN", "2", None),
        ("比较三款的区别", "COMPARE", None, None),
        ("更便宜一些", "UPDATE_PREFERENCE", None, "PRICE"),
        ("保修优先", "UPDATE_PREFERENCE", None, "WARRANTY"),
        ("快一点", "UPDATE_PREFERENCE", None, "DELIVERY"),
        ("预算改成7000元", "UPDATE_REQUIREMENT", None, None),
        ("接受方案1", "ACCEPT_NEGOTIATION", "1", None),
        ("选第二款", "SELECT_CANDIDATE", "2", None),
        ("那款", "CLARIFY", None, None),
    ],
)
def test_turn_intent_rules(text, intent, target, pref):
    actual = classify_turn_rules(text)
    assert (actual.intent, actual.target_reference, actual.preference) == (intent, target, pref)


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture", [False, True])
async def test_required_fact_tools_are_covered_and_commit_is_reserved(fixture):
    e, r, response = await start(fixture=fixture)
    run = response.run
    assert run.state == RunState.WAITING_CONFIRMATION
    assert len(run.candidate_pool) <= 6
    for c in run.candidate_pool:
        assert set(current_facts(run, c.offer)) == {"INVENTORY", "DELIVERY", "WARRANTY"}
    assert all(
        all(c.result == "PASS" for c in a.checks)
        for a in run.candidate_assessments
        if a.offer_id in {c.offer.offer_id for c in run.candidate_pool}
    )
    done = await r.confirm_run(**ID, run_id=run.run_id, action_token=run.action_token)
    assert done.run.state == RunState.DRAFT_CREATED
    assert done.run.execution_usage.commit_tool_calls == 6
    assert done.run.execution_usage.reserved_commit_tool_calls == 0
    assert any(ref.startswith("fact_") for ref in done.run.draft.candidate.evidence_refs)


@pytest.mark.asyncio
async def test_delivery_only_queried_when_required_and_new_preference_queries_it():
    e, r, response = await start(COMPLETE_REQUEST.replace("两周内，", ""))
    assert "estimate_delivery" not in EvidenceRequirementPolicy.tools(response.run)
    assert {f.kind for f in response.run.fact_evidence} == {"INVENTORY", "WARRANTY"}
    updated = await reply(r, response.run, "快一点")
    assert updated.run.ranking_preference == "DELIVERY"
    assert any(f.kind == "DELIVERY" for f in updated.run.fact_evidence)
    assert updated.run.requirement_spec == response.run.requirement_spec
    assert updated.run.recommendation.best_overall.offer.delivery_days == min(
        c.offer.delivery_days for c in updated.run.candidate_pool
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario", ["inventory_unknown", "warranty_unavailable", "delivery_unknown"]
)
async def test_unknown_facts_wait_without_feasible_options_or_write(scenario):
    e, r, result = await start(scenario=scenario)
    assert result.run.state == RunState.WAITING_USER
    assert result.run.wait_reason.value == "FACTS_UNAVAILABLE"
    assert not result.run.action_token and not result.run.negotiation_options
    assert any(c.result == "UNKNOWN" for a in result.run.candidate_assessments for c in a.checks)
    assert (await r.status(**ID, run_id=result.run.run_id)).wait_reason.value == "FACTS_UNAVAILABLE"


@pytest.mark.asyncio
async def test_fact_gateway_denies_unrecalled_offer_and_requirement_agent():
    e, _, result = await start()
    run = result.run
    ctx = GatewayExecutionContext(
        run=run, roles=("EMPLOYEE",), node_name="test", budget_pool=ToolBudgetPool.WORKFLOW_READ
    )
    with pytest.raises(ValueError, match="Offer must come"):
        await e.tool_gateway.invoke(
            tool_name="query_inventory",
            arguments={"provider": "internal_catalog", "offer_id": "made_up"},
            context=ctx,
        )
    ctx.agent_kind = "REQUIREMENT"
    with pytest.raises(PermissionError):
        await e.tool_gateway.invoke(
            tool_name="query_warranty",
            arguments={"provider": "internal_catalog", "offer_id": "made_up"},
            context=ctx,
        )


@pytest.mark.asyncio
async def test_fact_cache_expiry_scope_and_completed_execution_recovery():
    e, _, result = await start()
    run = result.run
    c = run.candidate_pool[0]
    args = {"provider": c.offer.provider, "offer_id": c.offer.offer_id}
    ctx = GatewayExecutionContext(
        run=run, roles=("EMPLOYEE",), node_name="test", budget_pool=ToolBudgetPool.WORKFLOW_READ
    )
    first = await e.tool_gateway.invoke(tool_name="estimate_delivery", arguments=args, context=ctx)
    calls = len(e.execution_repository.list_tools(run.run_id, tenant_id=run.tenant_id))
    assert (
        await e.tool_gateway.invoke(tool_name="estimate_delivery", arguments=args, context=ctx)
    ).fact_id == first.fact_id
    assert len(e.execution_repository.list_tools(run.run_id, tenant_id=run.tenant_id)) == calls
    run.fact_evidence = [f for f in run.fact_evidence if f.fact_id != first.fact_id]
    recovered = await e.tool_gateway.invoke(
        tool_name="estimate_delivery", arguments=args, context=ctx
    )
    assert recovered.fact_id == first.fact_id
    # Journal TTL, not just Run cache, must prevent resurrecting an expired result.
    for f in run.fact_evidence:
        if f.fact_id == first.fact_id:
            f.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    key = next(
        t.idempotency_key
        for t in e.execution_repository.list_tools(run.run_id, tenant_id=run.tenant_id)
        if t.tool_name == "estimate_delivery" and t.arguments_json == args
    )
    row = e.execution_repository.get_tool(key)
    row.result_json["value"]["fetched_at"] = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    row.result_json["value"]["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    e.execution_repository.complete_tool(row.tool_call_id, row.result_json)
    fresh = await e.tool_gateway.invoke(tool_name="estimate_delivery", arguments=args, context=ctx)
    assert fresh.fact_id != first.fact_id
    run.requirement.delivery_city = "Shanghai"
    scoped = await e.tool_gateway.invoke(tool_name="estimate_delivery", arguments=args, context=ctx)
    assert scoped.scope["delivery_city"] == "Shanghai" and scoped.fact_id != fresh.fact_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("old", "new", "field"),
    [
        ("8000", "4000", "budget.maximum"),
        ("两周内", "1天内", "delivery_days"),
        ("32GB", "128GB", "minimum_ram_gb"),
    ],
)
async def test_negotiation_is_feasible_and_acceptance_only_changes_requirement(old, new, field):
    e, r, result = await start(COMPLETE_REQUEST.replace(old, new))
    assert result.run.state == RunState.WAITING_USER
    assert result.run.wait_reason.value == "CONSTRAINT_NEGOTIATION"
    assert result.run.negotiation_options
    option = result.run.negotiation_options[0]
    assert any(a.field == field for a in option.adjustments)
    before = result.run.requirement_spec
    done = await reply(r, result.run, "接受方案1")
    assert done.run.state == RunState.WAITING_CONFIRMATION
    assert done.run.draft is None
    assert done.run.requirement_spec.version == before.version + 1
    assert done.run.negotiation_consents[-1]["option_id"] == option.option_id
    evidence = done.run.requirement_spec.evidence[-1]
    assert evidence.quote == "接受方案1" and evidence.source_ref.endswith(option.option_id)
    replay = await reply(r, done.run, "接受方案1")
    assert len(replay.run.negotiation_consents) == 1
    # An unrelated follow-up must preserve consent-grounded values through Requirement validation.
    updated = await reply(r, done.run, "数量改成10台", request_id="next")
    assert updated.run.state == RunState.WAITING_CONFIRMATION
    if field == "minimum_ram_gb":
        assert updated.run.requirement.minimum_ram_gb == done.run.requirement.minimum_ram_gb


@pytest.mark.asyncio
async def test_expired_and_unspecified_negotiation_acceptance_are_safe():
    e, r, result = await start(COMPLETE_REQUEST.replace("8000", "4000"))
    unclear = await reply(r, result.run, "接受方案")
    assert not unclear.run.negotiation_consents
    run = e.get(result.run.run_id, **ID)
    for option in run.negotiation_options:
        option.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    e.repository.save(run)
    with pytest.raises(ValueError, match="stale"):
        await reply(r, run, "接受方案1", request_id="expired")
    assert e.get(run.run_id, **ID).requirement == result.run.requirement


@pytest.mark.asyncio
async def test_stock_shortage_and_enterprise_policy_are_not_negotiable():
    e, r, response = await start(COMPLETE_REQUEST.replace("20台", "999台"))
    assert response.run.state == RunState.WAITING_USER
    assert not response.run.negotiation_options
    assert any(
        c.field == "stock" and c.result == "FAIL" and not c.adjustable
        for a in response.run.candidate_assessments
        for c in a.checks
    )
    assert all(
        not c.adjustable
        for a in response.run.candidate_assessments
        for c in a.checks
        if c.authority == "POLICY"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["解释一下推荐理由", "比较三款的区别", "那款"])
async def test_read_only_dialogue_keeps_requirement_candidates_and_token(text):
    e, r, result = await start()
    before = result.run.model_copy(deep=True)
    after = await reply(r, result.run, text)
    assert after.run.requirement_spec == before.requirement_spec
    assert after.run.recommendation == before.recommendation
    assert after.run.action_token == before.action_token
    assert after.run.state_version == before.state_version
    assert len(after.run.turn_decisions) == 1
    assert (await reply(r, after.run, text)).run.messages == after.run.messages


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["更便宜一些", "选第二款"])
async def test_preference_or_selection_invalidates_old_token_without_modifying_requirement(text):
    e, r, result = await start()
    before = result.run.model_copy(deep=True)
    after = await reply(r, result.run, text)
    assert after.run.requirement_spec == before.requirement_spec
    assert after.run.recommendation_version == before.recommendation_version + 1
    with pytest.raises(PermissionError):
        e.harness.verify_action_token(before.action_token, after.run)
    with pytest.raises(ValueError, match="stale"):
        await reply(
            r,
            after.run,
            "选第二款",
            request_id="old_view",
            expected_recommendation_version=before.recommendation_version,
        )
    assert (
        await r.confirm_run(**ID, run_id=after.run.run_id, action_token=after.run.action_token)
    ).run.draft


@pytest.mark.asyncio
async def test_hard_change_invalidates_old_recommendation_and_respects_deadline():
    e, r, result = await start()
    after = await reply(r, result.run, "交期改成1天内")
    assert after.run.requirement.delivery_days == 1
    assert after.run.state == RunState.WAITING_USER
    assert after.run.recommendation is None and after.run.action_token is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("field", "value"), [("stock", 0), ("delivery_days", 30)])
async def test_fresh_confirm_facts_block_stock_and_deadline_regression(field, value):
    e, r, result = await start()
    best = result.run.recommendation.best_overall.offer
    for raw in e.catalog.raw_offers:
        if raw.provider == best.provider and raw.raw_offer_id == best.offer_id:
            setattr(raw, field, value)
    after = await r.confirm_run(
        **ID, run_id=result.run.run_id, action_token=result.run.action_token
    )
    assert after.run.state == RunState.WAITING_CONFIRMATION
    assert after.run.draft is None and after.run.action_token is None
    assert after.run.execution_usage.reserved_commit_tool_calls == 0


@pytest.mark.asyncio
async def test_satisfying_fact_change_requires_new_token_and_second_confirmation():
    e, r, result = await start()
    best = result.run.recommendation.best_overall.offer
    for raw in e.catalog.raw_offers:
        if raw.provider == best.provider and raw.raw_offer_id == best.offer_id:
            raw.stock += 1
    changed = await r.confirm_run(
        **ID, run_id=result.run.run_id, action_token=result.run.action_token
    )
    assert changed.run.draft is None and changed.run.revalidation_changes
    assert changed.run.action_token != result.run.action_token
    with pytest.raises(PermissionError):
        e.harness.verify_action_token(result.run.action_token, changed.run)
    done = await r.confirm_run(
        **ID, run_id=result.run.run_id, action_token=changed.run.action_token
    )
    assert done.run.state == RunState.DRAFT_CREATED
    assert done.run.execution_usage.commit_tool_calls <= 12


@pytest.mark.asyncio
async def test_sourcing_restart_reuses_completed_fact_journals(tmp_path, monkeypatch):
    e = make_engine()
    url = f"sqlite:///{tmp_path / 'runs.sqlite3'}"
    e.repository = SqlAlchemyRunRepository(url)
    e.repository.create_schema()
    e.execution_repository = SqlAlchemyExecutionRepository(url)
    e.tool_gateway.execution_repository = e.execution_repository
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "checkpoints.sqlite3")) as saver:
        workflow = LangGraphProcurementWorkflow(e, saver, checkpoint_backend="sqlite")
        original = e.sourcing_evidence.enrich

        async def fault(run, offers, **kwargs):
            await original(run, offers[:1], **kwargs)
            raise RuntimeError("worker lost after facts")

        monkeypatch.setattr(e.sourcing_evidence, "enrich", fault)
        with pytest.raises(RuntimeError, match="worker lost"):
            await workflow.start_run(**ID, message=COMPLETE_REQUEST)
        run = e.repository.list(**ID)[0]
        known = {f.fact_id for f in run.fact_evidence}
        assert len(known) == 3
    e2 = make_engine()
    e2.repository = SqlAlchemyRunRepository(url)
    e2.execution_repository = SqlAlchemyExecutionRepository(url)
    e2.tool_gateway.execution_repository = e2.execution_repository
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "checkpoints.sqlite3")) as saver:
        workflow = LangGraphProcurementWorkflow(e2, saver, checkpoint_backend="sqlite")
        recovered = await workflow.resume_pending(**ID, run_id=run.run_id)
        assert recovered.run.state == RunState.WAITING_CONFIRMATION
        assert known.issubset({f.fact_id for f in recovered.run.fact_evidence})
        assert len(recovered.run.fact_evidence) == 18


@pytest.mark.asyncio
async def test_final_write_checks_fact_expiry_after_checkpoint_delay():
    e, r, result = await start()
    e.prepare_confirmation_node(
        **ID, run_id=result.run.run_id, action_token=result.run.action_token
    )
    quote = await e.revalidate_quote_node(**ID, run_id=result.run.run_id)
    policy = await e.revalidate_policy_node(
        **ID, run_id=result.run.run_id, refreshed_offer=quote.refreshed_offer
    )
    run = e.get(result.run.run_id, **ID)
    for fact in run.fact_evidence:
        fact.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    e.repository.save(run)
    stop = await e.create_draft_node(**ID, run_id=run.run_id, candidate=policy.candidate)
    assert stop.outcome == "WAITING_CONFIRMATION"
    assert e.get(run.run_id, **ID).draft is None


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "deepseek"])
async def test_live_dialogue_uses_role_model_strict_output_and_no_tools(provider):
    e, _, result = await start()
    seen = []

    class Runner:
        @staticmethod
        async def run(agent, _input, **kwargs):
            seen.append(agent)
            output = DialogueProposal(intent="EXPLAIN", confidence=0.99)
            return SimpleNamespace(
                final_output=output,
                raw_responses=[],
                context_wrapper=SimpleNamespace(
                    usage=SimpleNamespace(requests=1, input_tokens=10, output_tokens=10)
                ),
            )

    adapter = OpenAICompatibleTwoAgentAdapter(
        requirement_model="fake",
        sourcing_model="fake",
        api_key="fake",
        provider=provider,
        runner=Runner,
    )
    ctx = AgentRuntimeContext(
        run=result.run,
        catalog=e.catalog,
        enterprise=e.enterprise,
        harness=e.harness,
        roles=("EMPLOYEE",),
    )
    ctx.model_gateway = e.model_gateway
    call = await adapter.dialogue(
        {"phase": "INTENT", "message_id": "one", "message": "why"}, context=ctx
    )
    assert call.output.intent == "EXPLAIN"
    assert seen[0].tools == []
    assert seen[0].model is adapter._models[AgentKind.REQUIREMENT]
    AgentOutputSchema(DialogueProposal)
    AgentOutputSchema(DialogueSelection)


@pytest.mark.asyncio
async def test_invalid_confirm_does_not_consume_checkpoint_and_duplicate_confirm_is_idempotent():
    e, r, result = await start()
    with pytest.raises(PermissionError):
        await r.confirm_run(**ID, run_id=result.run.run_id, action_token="invalid")
    assert (await r.status(**ID, run_id=result.run.run_id)).waiting_at == "wait_for_confirmation"
    done = await r.confirm_run(**ID, run_id=result.run.run_id, action_token=result.run.action_token)
    again = await r.confirm_run(
        **ID, run_id=result.run.run_id, action_token=result.run.action_token
    )
    assert again.run.draft.draft_id == done.run.draft.draft_id


@pytest.mark.asyncio
async def test_model_intent_and_negotiation_order_are_used_but_cannot_authorize_writes():
    from procure_pilot.agents.multi_agent import AdapterCall

    class SemanticAdapter(FixtureTwoAgentAdapter):
        async def dialogue(self, payload, *, context):
            if payload["phase"] == "INTENT":
                return AdapterCall(
                    DialogueProposal(
                        intent="UPDATE_PREFERENCE", confidence=0.99, preference="WARRANTY"
                    )
                )
            if payload["phase"] == "NEGOTIATE":
                return AdapterCall(
                    DialogueSelection(
                        option_ids=[payload["options"][-1]["option_id"]],
                        rationale="已验证方案中优先最后一项",
                    )
                )
            return AdapterCall(DialogueSelection(rationale="证据已校验"))

    e, r, result = await start(fixture=True)
    e.planner.adapter = SemanticAdapter()
    changed = await reply(r, result.run, "继续当前采购，稳定性优先")
    assert changed.run.ranking_preference == "WARRANTY"
    assert changed.run.requirement_spec == result.run.requirement_spec
    assert changed.run.draft is None
    assert any(
        x.node_name == "dialogue_intent"
        for x in e.execution_repository.list_agents(result.run.run_id, tenant_id=ID["tenant_id"])
    )
    e2 = make_engine()
    e2.planner = TwoAgentPlanner(SemanticAdapter(), fallback_on_error=False)
    r2 = ProcurementWorkflowRuntime(e2, Settings())
    negotiated = await r2.start_run(**ID, message=COMPLETE_REQUEST.replace("8000", "4000"))
    assert len(negotiated.run.negotiation_options) == 1
    assert negotiated.run.draft is None


@pytest.mark.asyncio
async def test_soft_delivery_is_not_promoted_to_a_hard_deadline():
    _, _, response = await start(COMPLETE_REQUEST.replace("两周内", "最好1天内"))
    assert response.run.requirement.delivery_days is None
    assert response.run.state == RunState.WAITING_CONFIRMATION
    assert any(f.kind == "DELIVERY" for f in response.run.fact_evidence)


@pytest.mark.asyncio
async def test_new_fact_tools_retry_through_harness_and_journal_failure_status(monkeypatch):
    import procure_pilot.gateway.tool_gateway as module
    from procure_pilot.fixtures.catalog import ProviderError

    original = module.query_offer_fact
    calls = []

    async def transient(*args, **kw):
        calls.append(kw["kind"])
        if len(calls) == 1:
            raise ProviderError(kw["provider"], "HTTP_429", "temporary fact API rate limit")
        return await original(*args, **kw)

    monkeypatch.setattr(module, "query_offer_fact", transient)
    e, _, response = await start()
    assert response.run.state == RunState.WAITING_CONFIRMATION
    assert response.run.execution_usage.retries == 1
    assert len(calls) == 19


@pytest.mark.asyncio
async def test_confirmation_retry_resumes_pending_write_without_duplicate_fact_calls():
    e, r, response = await start()
    fired = []

    def fault(node, state):
        if node == "create_draft" and not fired:
            fired.append(node)
            raise RuntimeError("worker stopped after policy")

    r.workflow._fault_injector = fault
    with pytest.raises(RuntimeError, match="worker stopped"):
        await r.confirm_run(
            **ID, run_id=response.run.run_id, action_token=response.run.action_token
        )
    saved = e.get(response.run.run_id, **ID)
    facts = {f.fact_id for f in saved.fact_evidence}
    assert saved.state == RunState.REVALIDATING
    done = await r.confirm_run(
        **ID, run_id=response.run.run_id, action_token=response.run.action_token
    )
    assert done.run.state == RunState.DRAFT_CREATED
    assert facts == {f.fact_id for f in done.run.fact_evidence}
