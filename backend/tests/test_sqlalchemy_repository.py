from __future__ import annotations

from copy import deepcopy

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import func, select

from procure_pilot.domain.models import Message, ProcurementRun, RunState
from procure_pilot.evals.runner import COMPLETE_REQUEST
from procure_pilot.fixtures.catalog import FixtureCatalogAdapter
from procure_pilot.fixtures.enterprise import FixtureEnterpriseGateway
from procure_pilot.harness.runtime import (
    AgentHarness,
    PersistenceConflictError,
)
from procure_pilot.persistence.execution_repository import SqlAlchemyExecutionRepository
from procure_pilot.persistence.models import (
    AgentExecutionRecord,
    RunMessageRecord,
    RunTraceEventRecord,
    ToolCallRecord,
)
from procure_pilot.persistence.repository import SqlAlchemyRunRepository
from procure_pilot.services.engine import ProcurementEngine
from procure_pilot.workflow import LangGraphProcurementWorkflow


def make_repository(tmp_path) -> SqlAlchemyRunRepository:
    repository = SqlAlchemyRunRepository(f"sqlite+pysqlite:///{tmp_path / 'runs.db'}")
    repository.create_schema()
    return repository


def make_database_engine(repository: SqlAlchemyRunRepository) -> ProcurementEngine:
    return ProcurementEngine(
        catalog=FixtureCatalogAdapter(),
        enterprise=FixtureEnterpriseGateway(),
        repository=repository,
        harness=AgentHarness(secret="database-test"),
    )


@pytest.mark.asyncio
async def test_database_repository_persists_run_messages_trace_and_draft(tmp_path):
    repository = make_repository(tmp_path)
    engine = make_database_engine(repository)

    recommendation = await engine.start(
        tenant_id="demo_cn",
        user_id="employee_001",
        message=COMPLETE_REQUEST,
    )
    completed = await engine.confirm(
        run_id=recommendation.run.run_id,
        tenant_id="demo_cn",
        user_id="employee_001",
        action_token=recommendation.run.action_token,
    )
    restored = repository.get(
        completed.run.run_id,
        tenant_id="demo_cn",
        user_id="employee_001",
    )

    assert restored.state == RunState.DRAFT_CREATED
    assert restored.draft is not None
    assert restored.draft.status == "DRAFT_NOT_SUBMITTED"
    assert restored.messages == completed.run.messages
    assert restored.trace == completed.run.trace
    with repository.session_factory() as session:
        message_count = session.scalar(
            select(func.count())
            .select_from(RunMessageRecord)
            .where(RunMessageRecord.run_id == restored.run_id)
        )
        trace_count = session.scalar(
            select(func.count())
            .select_from(RunTraceEventRecord)
            .where(RunTraceEventRecord.run_id == restored.run_id)
        )
    assert message_count == len(restored.messages)
    assert trace_count == len(restored.trace)


def test_database_repository_enforces_tenant_scope_without_existence_leak(tmp_path):
    repository = make_repository(tmp_path)
    run = ProcurementRun(tenant_id="demo_cn", user_id="employee_001")
    repository.save(run)

    with pytest.raises(PermissionError, match="current tenant context"):
        repository.get(
            run.run_id,
            tenant_id="demo_id",
            user_id="employee_101",
        )
    with pytest.raises(PermissionError, match="current tenant context"):
        repository.get(
            "run_does_not_exist",
            tenant_id="demo_id",
            user_id="employee_101",
        )


def test_database_repository_rejects_stale_concurrent_writer(tmp_path):
    repository = make_repository(tmp_path)
    run = ProcurementRun(tenant_id="demo_cn", user_id="employee_001")
    repository.save(run)
    first_writer = repository.get(run.run_id)
    stale_writer = repository.get(run.run_id)

    first_writer.messages.append(Message(role="user", content="first"))
    repository.save(first_writer)
    stale_writer.messages.append(Message(role="user", content="stale"))

    with pytest.raises(PersistenceConflictError, match="another writer"):
        repository.save(stale_writer)
    restored = repository.get(run.run_id)
    assert [message.content for message in restored.messages] == ["first"]


@pytest.mark.asyncio
async def test_database_unique_constraint_rejects_duplicate_draft_identity(tmp_path):
    repository = make_repository(tmp_path)
    engine = make_database_engine(repository)
    recommendation = await engine.start(
        tenant_id="demo_cn",
        user_id="employee_001",
        message=COMPLETE_REQUEST,
    )
    completed = await engine.confirm(
        run_id=recommendation.run.run_id,
        tenant_id="demo_cn",
        user_id="employee_001",
        action_token=recommendation.run.action_token,
    )
    duplicate = deepcopy(completed.run)
    duplicate.run_id = "run_duplicate_draft"
    duplicate._persistence_version = None

    with pytest.raises(PersistenceConflictError, match="uniqueness constraint"):
        repository.save(duplicate)


@pytest.mark.asyncio
async def test_database_persists_agent_executions_and_tool_calls(tmp_path):
    repository = make_repository(tmp_path)
    execution_repository = SqlAlchemyExecutionRepository(engine=repository.engine)
    engine = ProcurementEngine(
        catalog=FixtureCatalogAdapter(),
        enterprise=FixtureEnterpriseGateway(),
        repository=repository,
        harness=AgentHarness(secret="execution-journal-test"),
        execution_repository=execution_repository,
    )
    workflow = LangGraphProcurementWorkflow(
        engine,
        InMemorySaver(),
        checkpoint_backend="memory",
    )

    recommendation = await workflow.start_run(
        tenant_id="demo_cn",
        user_id="employee_001",
        message=COMPLETE_REQUEST,
    )
    completed = await workflow.confirm_run(
        run_id=recommendation.run.run_id,
        tenant_id="demo_cn",
        user_id="employee_001",
        action_token=recommendation.run.action_token or "",
    )

    agents = execution_repository.list_agents(completed.run.run_id, tenant_id="demo_cn")
    tools = execution_repository.list_tools(completed.run.run_id, tenant_id="demo_cn")
    assert completed.run.state == RunState.DRAFT_CREATED
    assert len(agents) == 1
    assert {item.tool_name for item in tools} >= {
        "search_catalog",
        "revalidate_quote",
        "evaluate_policy",
        "create_requisition_draft",
    }
    with repository.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(AgentExecutionRecord)) == 1
        assert session.scalar(select(func.count()).select_from(ToolCallRecord)) == len(tools)
