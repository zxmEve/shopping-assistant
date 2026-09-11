from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from procure_pilot.config import Settings
from procure_pilot.domain.models import RunState
from procure_pilot.evals.runner import make_engine
from procure_pilot.fixtures.catalog import FixtureCatalogAdapter
from procure_pilot.fixtures.enterprise import FixtureEnterpriseGateway
from procure_pilot.harness.runtime import AgentHarness
from procure_pilot.persistence.execution_repository import SqlAlchemyExecutionRepository
from procure_pilot.persistence.repository import SqlAlchemyRunRepository
from procure_pilot.services.engine import ProcurementEngine
from procure_pilot.workflow import LangGraphProcurementWorkflow, ProcurementWorkflowRuntime

INCOMPLETE_REQUEST = "采购20台开发笔记本，单台预算8000元，两周内到货"
CLARIFICATION = "预算含税，送雅加达，最低32GB和1TB"
COMPLETE_REQUEST = f"{INCOMPLETE_REQUEST}，{CLARIFICATION}"


class InjectedProcessKill(RuntimeError):
    pass


class KillBeforeNodeOnce:
    def __init__(self, target: str) -> None:
        self.target = target
        self.run_id: str | None = None
        self.triggered = False

    def __call__(self, node_name, state) -> None:
        self.run_id = state["run_id"]
        if node_name == self.target and not self.triggered:
            self.triggered = True
            raise InjectedProcessKill(f"worker killed before {node_name}")


def _durable_test_engine(database_url: str) -> ProcurementEngine:
    repository = SqlAlchemyRunRepository(database_url)
    repository.create_schema()
    return ProcurementEngine(
        catalog=FixtureCatalogAdapter(),
        enterprise=FixtureEnterpriseGateway(),
        repository=repository,
        harness=AgentHarness(secret="kill-restart-test"),
        execution_repository=SqlAlchemyExecutionRepository(engine=repository.engine),
    )


def _workflow(engine=None, checkpointer=None):
    return LangGraphProcurementWorkflow(
        engine or make_engine(),
        checkpointer or InMemorySaver(),
        checkpoint_backend="memory",
    )


@pytest.mark.asyncio
async def test_langgraph_interrupts_for_clarification_and_confirmation_then_ends():
    workflow = _workflow()

    started = await workflow.start_run(
        tenant_id="demo_cn",
        user_id="employee_001",
        message=INCOMPLETE_REQUEST,
    )
    first_status = await workflow.status(
        run_id=started.run.run_id,
        tenant_id="demo_cn",
        user_id="employee_001",
    )

    assert started.run.state == RunState.WAITING_USER
    assert first_status.waiting_at == "wait_for_user"
    assert first_status.wait_reason == "MISSING_REQUIREMENT"

    continued = await workflow.continue_run(
        run_id=started.run.run_id,
        tenant_id="demo_cn",
        user_id="employee_001",
        message=CLARIFICATION,
    )
    second_status = await workflow.status(
        run_id=started.run.run_id,
        tenant_id="demo_cn",
        user_id="employee_001",
    )

    assert continued.run.state == RunState.WAITING_CONFIRMATION
    assert second_status.waiting_at == "wait_for_confirmation"
    assert second_status.wait_reason == "WAITING_CONFIRMATION"

    confirmed = await workflow.confirm_run(
        run_id=started.run.run_id,
        tenant_id="demo_cn",
        user_id="employee_001",
        action_token=continued.run.action_token or "",
    )
    final_status = await workflow.status(
        run_id=started.run.run_id,
        tenant_id="demo_cn",
        user_id="employee_001",
    )

    assert confirmed.run.state == RunState.DRAFT_CREATED
    assert confirmed.run.draft is not None
    assert final_status.waiting_at is None
    assert final_status.wait_reason is None


@pytest.mark.asyncio
async def test_langgraph_can_resume_after_workflow_object_is_recreated():
    engine = make_engine()
    checkpointer = InMemorySaver()
    first_process = _workflow(engine, checkpointer)
    started = await first_process.start_run(
        tenant_id="demo_cn",
        user_id="employee_001",
        message=INCOMPLETE_REQUEST,
    )

    recreated_process = _workflow(engine, checkpointer)
    continued = await recreated_process.continue_run(
        run_id=started.run.run_id,
        tenant_id="demo_cn",
        user_id="employee_001",
        message=CLARIFICATION,
    )

    assert continued.run.state == RunState.WAITING_CONFIRMATION


@pytest.mark.asyncio
async def test_langgraph_resume_is_tenant_scoped_and_stage_scoped():
    workflow = _workflow()
    started = await workflow.start_run(
        tenant_id="demo_cn",
        user_id="employee_001",
        message=INCOMPLETE_REQUEST,
    )

    with pytest.raises(PermissionError):
        await workflow.continue_run(
            run_id=started.run.run_id,
            tenant_id="demo_id",
            user_id="employee_101",
            message=CLARIFICATION,
        )

    continued = await workflow.continue_run(
        run_id=started.run.run_id,
        tenant_id="demo_cn",
        user_id="employee_001",
        message=CLARIFICATION,
    )
    with pytest.raises(ValueError, match="cannot resume"):
        await workflow.continue_run(
            run_id=started.run.run_id,
            tenant_id="demo_cn",
            user_id="employee_001",
            message="重复补充",
        )
    assert continued.run.state == RunState.WAITING_CONFIRMATION


@pytest.mark.asyncio
async def test_langgraph_rejects_checkpoint_when_business_state_version_changed():
    engine = make_engine()
    workflow = _workflow(engine)
    started = await workflow.start_run(
        tenant_id="demo_cn",
        user_id="employee_001",
        message=INCOMPLETE_REQUEST,
    )
    changed = engine.get(
        started.run.run_id,
        tenant_id="demo_cn",
        user_id="employee_001",
    )
    engine.harness.transition(
        changed,
        RunState.COLLECTING,
        reason="simulate another process accepting new input",
    )
    engine.repository.save(changed)

    with pytest.raises(ValueError, match="checkpoint is stale"):
        await workflow.continue_run(
            run_id=started.run.run_id,
            tenant_id="demo_cn",
            user_id="employee_001",
            message=CLARIFICATION,
        )


@pytest.mark.asyncio
async def test_legacy_workflow_backend_remains_available_as_rollback_switch():
    runtime = ProcurementWorkflowRuntime(
        make_engine(),
        Settings(workflow_backend="legacy"),
    )

    started = await runtime.start_run(
        tenant_id="demo_cn",
        user_id="employee_001",
        message=INCOMPLETE_REQUEST,
    )
    status = await runtime.status(
        run_id=started.run.run_id,
        tenant_id="demo_cn",
        user_id="employee_001",
    )

    assert started.run.state == RunState.WAITING_USER
    assert status.backend == "legacy"
    assert status.checkpoint_backend == "none"


@pytest.mark.asyncio
async def test_resume_pending_after_kill_does_not_repeat_completed_agent_node():
    engine = make_engine()
    saver = InMemorySaver()
    kill = KillBeforeNodeOnce("complete_turn:after")
    first_worker = LangGraphProcurementWorkflow(
        engine,
        saver,
        checkpoint_backend="memory",
        fault_injector=kill,
    )

    with pytest.raises(InjectedProcessKill, match="complete_turn:after"):
        await first_worker.start_run(
            tenant_id="demo_cn",
            user_id="employee_001",
            message=COMPLETE_REQUEST,
        )

    assert kill.run_id is not None
    executions_before = engine.execution_repository.list_agents(
        kill.run_id, tenant_id="demo_cn"
    )
    restarted_worker = _workflow(engine, saver)
    recovered = await restarted_worker.resume_pending(
        run_id=kill.run_id,
        tenant_id="demo_cn",
        user_id="employee_001",
    )
    executions_after = engine.execution_repository.list_agents(
        kill.run_id, tenant_id="demo_cn"
    )

    assert recovered.run.state == RunState.WAITING_CONFIRMATION
    assert len(executions_before) == len(executions_after) == 1
    assert executions_after[0].attempt_count == 1


@pytest.mark.asyncio
async def test_resume_pending_after_kill_between_policy_and_draft_creates_one_draft():
    engine = make_engine()
    saver = InMemorySaver()
    initial_worker = _workflow(engine, saver)
    started = await initial_worker.start_run(
        tenant_id="demo_cn",
        user_id="employee_001",
        message=COMPLETE_REQUEST,
    )
    kill = KillBeforeNodeOnce("create_draft:after")
    confirming_worker = LangGraphProcurementWorkflow(
        engine,
        saver,
        checkpoint_backend="memory",
        fault_injector=kill,
    )

    with pytest.raises(InjectedProcessKill, match="create_draft:after"):
        await confirming_worker.confirm_run(
            run_id=started.run.run_id,
            tenant_id="demo_cn",
            user_id="employee_001",
            action_token=started.run.action_token or "",
        )

    restarted_worker = _workflow(engine, saver)
    recovered = await restarted_worker.resume_pending(
        run_id=started.run.run_id,
        tenant_id="demo_cn",
        user_id="employee_001",
    )
    tool_calls = engine.execution_repository.list_tools(
        started.run.run_id, tenant_id="demo_cn"
    )

    assert recovered.run.state == RunState.DRAFT_CREATED
    assert recovered.run.draft is not None
    assert sum(item.tool_name == "create_requisition_draft" for item in tool_calls) == 1


@pytest.mark.asyncio
async def test_kill_restart_reopens_run_execution_and_checkpoint_databases(tmp_path):
    run_database_url = f"sqlite+pysqlite:///{tmp_path / 'runs.db'}"
    checkpoint_path = str(tmp_path / "checkpoints.db")
    kill = KillBeforeNodeOnce("sourcing_agent:after")
    first_engine = _durable_test_engine(run_database_url)

    async with AsyncSqliteSaver.from_conn_string(checkpoint_path) as first_saver:
        first_worker = LangGraphProcurementWorkflow(
            first_engine,
            first_saver,
            checkpoint_backend="sqlite-test",
            fault_injector=kill,
        )
        with pytest.raises(InjectedProcessKill, match="sourcing_agent:after"):
            await first_worker.start_run(
                tenant_id="demo_cn",
                user_id="employee_001",
                message=COMPLETE_REQUEST,
            )

    assert kill.run_id is not None
    restarted_engine = _durable_test_engine(run_database_url)
    async with AsyncSqliteSaver.from_conn_string(checkpoint_path) as restarted_saver:
        restarted_worker = LangGraphProcurementWorkflow(
            restarted_engine,
            restarted_saver,
            checkpoint_backend="sqlite-test",
        )
        recovered = await restarted_worker.resume_pending(
            run_id=kill.run_id,
            tenant_id="demo_cn",
            user_id="employee_001",
        )

    executions = restarted_engine.execution_repository.list_agents(
        kill.run_id, tenant_id="demo_cn"
    )
    assert recovered.run.state == RunState.WAITING_CONFIRMATION
    assert len(executions) == 1
    assert executions[0].attempt_count == 1
