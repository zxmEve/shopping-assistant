from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from agents import AgentOutputSchema
from fastapi.testclient import TestClient
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from procure_pilot.agents.contracts import AgentKind
from procure_pilot.agents.multi_agent import AdapterCall, FixtureTwoAgentAdapter, TwoAgentPlanner
from procure_pilot.agents.provider_adapter import OpenAICompatibleTwoAgentAdapter
from procure_pilot.agents.tools import AgentRuntimeContext
from procure_pilot.api.app import create_app
from procure_pilot.config import Settings
from procure_pilot.domain.conversation import MessageRouteProposal
from procure_pilot.domain.models import RunState
from procure_pilot.evals.runner import COMPLETE_REQUEST, make_engine
from procure_pilot.persistence.execution_repository import SqlAlchemyExecutionRepository
from procure_pilot.persistence.repository import SqlAlchemyRunRepository
from procure_pilot.services.message_routing import classify_message_rules
from procure_pilot.workflow import LangGraphProcurementWorkflow, ProcurementWorkflowRuntime

CN = {"Authorization": "Bearer fixture-session-cn-employee-001"}
ID = {"Authorization": "Bearer fixture-session-id-employee-101"}
IDENTITY = dict(tenant_id="demo_cn", user_id="employee_001")


@pytest.mark.parametrize(
    ("text", "category", "expected"),
    [
        ("预算改成7000元", "laptop", "CONTINUE"),
        ("数量改成10台", "laptop", "CONTINUE"),
        ("内存改成64GB", "laptop", "CONTINUE"),
        ("预算含税，送雅加达", "laptop", "CONTINUE"),
        ("不要重新开始，继续当前的", "laptop", "CONTINUE"),
        ("不是新需求，只是补充内存要求", "laptop", "CONTINUE"),
        ("换一个型号", "laptop", "CONTINUE"),
        ("笔记本", None, "CONTINUE"),
        ("新需求，采购10台笔记本", "laptop", "NEW"),
        ("重新采购10台笔记本", "laptop", "NEW"),
        ("这批先不买了", "laptop", "NEW"),
        ("采购5台显示器", "laptop", "NEW"),
        ("改买手机", "laptop", "NEW"),
        ("换一个", "laptop", "CLARIFY"),
        ("再买10台", "laptop", "CLARIFY"),
        ("换一批", "laptop", "CLARIFY"),
        ("显示器能接吗", "laptop", "CLARIFY"),
    ],
)
def test_offline_router_distinguishes_updates_resets_and_ambiguity(text, category, expected):
    assert classify_message_rules(text, category).route == expected


@pytest.fixture
def client(tmp_path):
    engine = make_engine()
    with TestClient(
        create_app(settings=Settings(run_store_path=tmp_path / "runs.json"), engine=engine)
    ) as client:
        yield client, engine


def start(client, message="采购20台笔记本，单台预算8000元含税，送雅加达"):
    response = client.post("/api/runs", json={"message": message}, headers=CN)
    assert response.status_code == 200
    return response.json()["run"]


def post(client, run_id, message, request_id="request_1", **extra):
    return client.post(
        f"/api/runs/{run_id}/messages",
        json={"message": message, "request_id": request_id, **extra},
        headers=CN,
    )


def test_continue_keeps_existing_facts_and_new_purchase_never_inherits_them(client):
    client, engine = client
    original = start(client)
    continued = post(client, original["run_id"], "最低32GB内存", request_id="continue_1")
    assert continued.status_code == 200
    payload = continued.json()
    assert payload["message_route"]["route"] == "CONTINUE"
    assert payload["run"]["run_id"] == original["run_id"]
    assert payload["run"]["requirement"]["quantity"] == 20
    assert payload["run"]["requirement"]["unit_budget"]["amount"] == "8000.00"
    before = engine.get(original["run_id"], **IDENTITY).model_copy(deep=True)
    switched = post(client, original["run_id"], "新需求，采购10台笔记本", request_id="new_1")
    assert switched.status_code == 200
    payload = switched.json()
    assert payload["message_route"]["route"] == "NEW"
    assert payload["run"]["run_id"] != original["run_id"]
    assert payload["run"]["requirement"]["quantity"] == 10
    assert payload["run"]["requirement"]["unit_budget"] is None
    assert payload["run"]["requirement"]["minimum_ram_gb"] is None
    assert payload["run"]["requirement"]["delivery_city"] is None
    assert payload["run"]["clarification_rounds"] == 1
    assert payload["run"]["requirement_spec"]["version"] == 1
    after = engine.get(original["run_id"], **IDENTITY)
    assert after.requirement_spec == before.requirement_spec
    assert after.messages == before.messages
    assert after.state_version == before.state_version


@pytest.mark.parametrize("choice", ["CONTINUE", "NEW"])
def test_ambiguous_message_is_non_mutating_until_user_selects_its_target(client, choice):
    client, engine = client
    original = start(client)
    before = engine.get(original["run_id"], **IDENTITY).model_copy(deep=True)
    response = post(client, original["run_id"], "换一个")
    assert response.status_code == 200
    payload = response.json()
    assert payload["message_route"]["route"] == "CLARIFY"
    assert payload["message_route"]["choices"] == ["CONTINUE", "NEW"]
    assert engine.get(original["run_id"], **IDENTITY).messages == before.messages
    assert engine.get(original["run_id"], **IDENTITY).requirement_spec == before.requirement_spec
    selected = post(client, original["run_id"], "换一个", route_choice=choice)
    assert selected.status_code == 200
    assert selected.json()["message_route"]["source"] == "user"
    assert (selected.json()["run"]["run_id"] == original["run_id"]) == (choice == "CONTINUE")
    replay = post(client, original["run_id"], "换一个", route_choice=choice)
    assert replay.status_code == 200
    assert replay.json()["run"]["run_id"] == selected.json()["run"]["run_id"]
    assert len(engine.repository.list()) == (1 if choice == "CONTINUE" else 2)


def test_route_choice_cannot_be_injected_or_reused_after_the_requirement_changed(client):
    client, _ = client
    original = start(client)
    assert post(client, original["run_id"], "换一个", route_choice="NEW").status_code == 409
    assert post(client, original["run_id"], "换一个").json()["message_route"]["route"] == "CLARIFY"
    assert post(client, original["run_id"], "最低32GB内存", request_id="other").status_code == 200
    assert post(client, original["run_id"], "换一个", route_choice="NEW").status_code == 409


def test_new_purchase_request_replay_does_not_duplicate_and_cannot_change_input(client):
    client, engine = client
    original = start(client)
    first = post(client, original["run_id"], "新需求，采购10台笔记本")
    second = post(client, original["run_id"], "新需求，采购10台笔记本")
    assert first.json()["run"]["run_id"] == second.json()["run"]["run_id"]
    assert len(engine.repository.list()) == 2
    assert post(client, original["run_id"], "新需求，采购5台笔记本").status_code == 409
    assert (
        post(
            client, original["run_id"], "新需求，采购10台笔记本", route_choice="CONTINUE"
        ).status_code
        == 409
    )


def test_route_does_not_leak_or_modify_another_tenants_run(client):
    client, engine = client
    original = start(client)
    response = client.post(
        f"/api/runs/{original['run_id']}/messages",
        json={"message": "新需求，采购10台笔记本"},
        headers=ID,
    )
    assert response.status_code == 403
    assert len(engine.repository.list()) == 1
    assert engine.get(original["run_id"], **IDENTITY).message_routes == []


def test_recommendation_update_stays_in_same_run_and_invalidates_old_confirmation(client):
    client, engine = client
    original = start(client, COMPLETE_REQUEST)
    old_token = original["action_token"]
    updated = post(client, original["run_id"], "预算改成8500元")
    assert updated.status_code == 200
    run = updated.json()["run"]
    assert run["run_id"] == original["run_id"]
    assert run["state"] == "WAITING_CONFIRMATION"
    assert run["requirement"]["unit_budget"]["amount"] == "8500.00"
    assert run["requirement"]["quantity"] == 20
    assert run["action_token"] != old_token
    assert (
        client.post(
            f"/api/runs/{run['run_id']}/confirm", json={"action_token": old_token}, headers=CN
        ).status_code
        == 403
    )
    assert engine.get(run["run_id"], **IDENTITY).draft is None


def test_completed_purchase_can_start_new_purchase_without_editing_the_draft(client):
    client, engine = client
    original = start(client, COMPLETE_REQUEST)
    confirmed = client.post(
        f"/api/runs/{original['run_id']}/confirm",
        json={"action_token": original["action_token"]},
        headers=CN,
    )
    assert confirmed.status_code == 200
    draft_id = confirmed.json()["run"]["draft"]["draft_id"]
    new = post(client, original["run_id"], "重新采购10台笔记本")
    assert new.status_code == 200
    assert new.json()["run"]["run_id"] != original["run_id"]
    assert engine.get(original["run_id"], **IDENTITY).draft.draft_id == draft_id


class SemanticRouteAdapter(FixtureTwoAgentAdapter):
    async def classify_message(self, payload, *, context):
        self.payload = payload
        return AdapterCall(
            MessageRouteProposal(route="NEW", confidence=0.95, reason="用户为另一团队开始独立采购")
        )


class UncertainRouteAdapter(FixtureTwoAgentAdapter):
    async def classify_message(self, payload, *, context):
        return AdapterCall(MessageRouteProposal(route="NEW", confidence=0.4, reason="不确定"))


class BrokenRouteAdapter(FixtureTwoAgentAdapter):
    async def classify_message(self, payload, *, context):
        raise RuntimeError("provider unavailable")


@pytest.mark.parametrize(
    ("adapter_type", "message", "route", "source"),
    [
        (SemanticRouteAdapter, "给另一个团队配10台笔记本", "NEW", "model"),
        (SemanticRouteAdapter, "预算改成8500元", "CONTINUE", "model"),
        (UncertainRouteAdapter, "给另一个团队配10台笔记本", "CLARIFY", "model"),
        (BrokenRouteAdapter, "新需求，采购10台笔记本", "NEW", "fallback"),
    ],
)
@pytest.mark.asyncio
async def test_model_routing_uses_current_context_and_failure_or_low_confidence_is_safe(
    adapter_type, message, route, source
):
    engine = make_engine()
    adapter = adapter_type()
    engine.planner = TwoAgentPlanner(adapter, fallback_on_error=False)
    runtime = ProcurementWorkflowRuntime(engine, Settings())
    started = await runtime.start_run(
        **IDENTITY, message="采购20台笔记本，单台预算8000元含税，送雅加达"
    )
    before = started.run.requirement_spec.model_copy(deep=True)
    response = await runtime.route_message(
        run_id=started.run.run_id, **IDENTITY, message=message, request_id="model_route"
    )
    assert response.message_route.route == route
    assert response.message_route.source == source
    executions = engine.execution_repository.list_agents(started.run.run_id, tenant_id="demo_cn")
    assert any(
        item.node_name == "message_router" and item.agent_kind == "REQUIREMENT"
        for item in executions
    )
    if route in {"NEW", "CLARIFY"}:
        assert engine.get(started.run.run_id, **IDENTITY).requirement_spec == before
    if adapter_type == SemanticRouteAdapter:
        assert adapter.payload["category"] == "laptop"
        assert "action_token" not in adapter.payload
        assert adapter.payload["recent_messages"][-1]["role"] == "assistant"


@pytest.mark.asyncio
async def test_continue_retry_recovers_after_message_acceptance_without_duplicate_messages():
    engine = make_engine()
    runtime = ProcurementWorkflowRuntime(engine, Settings())
    started = await runtime.start_run(**IDENTITY, message="采购20台笔记本")
    failed = False

    def fail_after_accept(node, state):
        nonlocal failed
        if node == "accept_clarification:after" and not failed:
            failed = True
            raise RuntimeError("worker stopped")

    runtime.workflow._fault_injector = fail_after_accept
    kwargs = dict(
        run_id=started.run.run_id,
        **IDENTITY,
        message="预算8000元含税，送雅加达，配置无要求",
        request_id="recover",
    )
    with pytest.raises(RuntimeError, match="worker stopped"):
        await runtime.route_message(**kwargs)
    result = await runtime.route_message(**kwargs)
    assert result.run.state == RunState.WAITING_CONFIRMATION
    assert len([item for item in result.run.messages if item.role == "user"]) == 2
    assert result.run.message_routes[0].status == "COMPLETED"


@pytest.mark.asyncio
async def test_new_purchase_retry_recovers_child_run_instead_of_creating_another():
    engine = make_engine()
    runtime = ProcurementWorkflowRuntime(engine, Settings())
    started = await runtime.start_run(**IDENTITY, message="采购20台笔记本")
    failed = False

    def fail_after_initialize(node, state):
        nonlocal failed
        if (
            node == "initialize_start:after"
            and state["run_id"] != started.run.run_id
            and not failed
        ):
            failed = True
            raise RuntimeError("worker stopped")

    runtime.workflow._fault_injector = fail_after_initialize
    kwargs = dict(
        run_id=started.run.run_id,
        **IDENTITY,
        message="新需求，采购10台笔记本",
        request_id="recover_child",
    )
    with pytest.raises(RuntimeError, match="worker stopped"):
        await runtime.route_message(**kwargs)
    result = await runtime.route_message(**kwargs)
    assert result.message_route.route == "NEW"
    assert result.run.state == RunState.WAITING_USER
    assert result.run.requirement.quantity == 10
    assert len(engine.repository.list()) == 2


@pytest.mark.asyncio
async def test_ambiguous_route_survives_database_and_checkpoint_recreation(tmp_path):
    url = f"sqlite+pysqlite:///{tmp_path / 'runs.db'}"

    def engine_factory():
        engine = make_engine()
        engine.repository = SqlAlchemyRunRepository(url)
        engine.repository.create_schema()
        engine.execution_repository = SqlAlchemyExecutionRepository(engine=engine.repository.engine)
        return engine

    checkpoint = str(tmp_path / "checkpoints.db")
    async with AsyncSqliteSaver.from_conn_string(checkpoint) as saver:
        engine = engine_factory()
        runtime = ProcurementWorkflowRuntime(engine, Settings())
        runtime.workflow = LangGraphProcurementWorkflow(engine, saver, checkpoint_backend="sqlite")
        started = await runtime.start_run(**IDENTITY, message="采购20台笔记本")
        kwargs = dict(run_id=started.run.run_id, **IDENTITY, message="换一个", request_id="pending")
        assert (await runtime.route_message(**kwargs)).message_route.route == "CLARIFY"
    async with AsyncSqliteSaver.from_conn_string(checkpoint) as saver:
        restored_engine = engine_factory()
        runtime = ProcurementWorkflowRuntime(restored_engine, Settings())
        runtime.workflow = LangGraphProcurementWorkflow(
            restored_engine, saver, checkpoint_backend="sqlite"
        )
        response = await runtime.route_message(**kwargs, route_choice="NEW")
        assert response.run.run_id != started.run.run_id
        assert response.run.requirement.quantity is None
        assert len(restored_engine.repository.list()) == 2
        assert (
            await runtime.route_message(**kwargs, route_choice="NEW")
        ).run.run_id == response.run.run_id


class RoutingRunner:
    async def run(self, agent, input_text, *, context, max_turns):
        self.agent = agent
        self.payload = json.loads(input_text)
        self.max_turns = max_turns
        return SimpleNamespace(
            final_output=json.dumps({"route": "NEW", "confidence": 0.95, "reason": "独立的新采购"}),
            context_wrapper=SimpleNamespace(
                usage=SimpleNamespace(requests=1, input_tokens=30, output_tokens=20)
            ),
        )


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
@pytest.mark.asyncio
async def test_provider_route_phase_has_no_tools_and_uses_requirement_model(provider):
    runner = RoutingRunner()
    adapter = OpenAICompatibleTwoAgentAdapter(
        api_key="not-used",
        provider=provider,
        requirement_model="requirement-test",
        sourcing_model="sourcing-test",
        runner=runner,
    )
    engine = make_engine()
    run = (await engine.start(**IDENTITY, message="采购20台笔记本")).run
    context = AgentRuntimeContext(
        run=run,
        catalog=engine.catalog,
        enterprise=engine.enterprise,
        harness=engine.harness,
        roles=("EMPLOYEE",),
    )
    result = await adapter.classify_message(
        {"request_id": "provider_route", "message": "新采购"}, context=context
    )
    assert result.output.route == "NEW"
    assert runner.payload["phase"] == "ROUTE_MESSAGE"
    assert runner.agent.tools == []
    assert runner.agent.model is adapter._models[AgentKind.REQUIREMENT]
    assert runner.max_turns == 1
    assert len(adapter.agents) == 2
    if provider == "openai":
        assert AgentOutputSchema(runner.agent.output_type).is_strict_json_schema()


@pytest.mark.parametrize("choice", ["CONTINUE", "NEW"])
@pytest.mark.asyncio
async def test_confirmed_ambiguous_route_can_retry_after_dispatch_failure(choice):
    engine = make_engine()
    runtime = ProcurementWorkflowRuntime(engine, Settings())
    started = await runtime.start_run(**IDENTITY, message="采购20台笔记本")
    kwargs = dict(
        run_id=started.run.run_id, **IDENTITY, message="换一个", request_id="choose_retry"
    )
    assert (await runtime.route_message(**kwargs)).message_route.route == "CLARIFY"
    failed = False

    def fail_dispatch(node, state):
        nonlocal failed
        target = "accept_clarification:after" if choice == "CONTINUE" else "initialize_start:after"
        if node == target and not failed:
            failed = True
            raise RuntimeError("worker stopped")

    runtime.workflow._fault_injector = fail_dispatch
    with pytest.raises(RuntimeError, match="worker stopped"):
        await runtime.route_message(**kwargs, route_choice=choice)
    response = await runtime.route_message(**kwargs, route_choice=choice)
    assert response.message_route.route == choice
    assert response.run.state == RunState.WAITING_USER
    assert len(engine.repository.list()) == (1 if choice == "CONTINUE" else 2)


@pytest.mark.asyncio
async def test_category_answer_stays_in_current_run_even_if_model_suggests_reset():
    engine = make_engine()
    engine.planner = TwoAgentPlanner(SemanticRouteAdapter(), fallback_on_error=False)
    runtime = ProcurementWorkflowRuntime(engine, Settings())
    started = await runtime.start_run(
        **IDENTITY, message="采购20台设备，单台预算8000元含税，送雅加达"
    )
    assert started.run.requirement_spec.clarification_plan.covered_fields == ["category"]
    response = await runtime.route_message(
        run_id=started.run.run_id, **IDENTITY, message="笔记本", request_id="category_answer"
    )
    assert response.message_route.route == "CONTINUE"
    assert response.run.run_id == started.run.run_id
    assert response.run.requirement.quantity == 20
    assert response.run.requirement.unit_budget.amount == 8000
    assert len(engine.repository.list()) == 1
