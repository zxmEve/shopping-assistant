from __future__ import annotations

import pytest

from procure_pilot.agents.contracts import AgentKind
from procure_pilot.agents.multi_agent import (
    AGENT_TOOL_ALLOWLIST,
    AdapterCall,
    FixtureTwoAgentAdapter,
    TwoAgentPlanner,
)
from procure_pilot.config import Settings
from procure_pilot.domain.models import RunState
from procure_pilot.evals.runner import COMPLETE_REQUEST, make_engine


class RecordingAdapter(FixtureTwoAgentAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[AgentKind] = []

    async def execute_agent(self, envelope, *, context, allowed_tools):
        self.calls.append(envelope.task.agent)
        return await super().execute_agent(envelope, context=context, allowed_tools=allowed_tools)


@pytest.mark.asyncio
async def test_complete_request_runs_requirement_then_sourcing_agents():
    adapter = RecordingAdapter()
    planner = TwoAgentPlanner(adapter, fallback_on_error=False)
    engine = make_engine()
    engine.planner = planner

    recommendation = await engine.start(
        tenant_id="demo_cn", user_id="employee_001", message=COMPLETE_REQUEST
    )
    completed = await engine.confirm(
        run_id=recommendation.run.run_id,
        tenant_id="demo_cn",
        user_id="employee_001",
        action_token=recommendation.run.action_token,
    )

    assert recommendation.run.recommendation.selection_source == "agent_validated"
    assert completed.run.state == RunState.DRAFT_CREATED
    assert planner.last_outcome is not None
    assert planner.last_outcome.executed_agent == AgentKind.SOURCING
    assert adapter.calls == [AgentKind.REQUIREMENT, AgentKind.SOURCING]
    assert completed.run.execution_usage.orchestrator_requests == 0
    assert completed.run.execution_usage.specialist_requests == 3
    assert completed.run.execution_usage.stage_model_requests == 3
    assert completed.run.execution_usage.agent_turns == 3
    assert any(event.actor == "Policy Rule Service" for event in completed.run.trace)
    assert completed.run.execution_usage.reserved_commit_tool_calls == 0


@pytest.mark.asyncio
async def test_incomplete_request_runs_only_requirement_agent():
    adapter = RecordingAdapter()
    planner = TwoAgentPlanner(adapter, fallback_on_error=False)
    engine = make_engine()
    engine.planner = planner

    response = await engine.start(
        tenant_id="demo_cn", user_id="employee_001", message="帮我采购开发电脑"
    )

    assert response.run.state == RunState.WAITING_USER
    assert planner.last_outcome is not None
    assert planner.last_outcome.executed_agent == AgentKind.REQUIREMENT
    assert adapter.calls == [AgentKind.REQUIREMENT]


class StaleAdapter(FixtureTwoAgentAdapter):
    async def execute_agent(self, envelope, *, context, allowed_tools):
        call = await super().execute_agent(envelope, context=context, allowed_tools=allowed_tools)
        stale = call.output.model_copy(update={"state_version": envelope.state_version + 1})
        return AdapterCall(stale, call.usage)


@pytest.mark.asyncio
async def test_stale_agent_result_falls_back_and_cannot_drive_selection():
    planner = TwoAgentPlanner(StaleAdapter())
    engine = make_engine()
    engine.planner = planner

    response = await engine.start(
        tenant_id="demo_cn", user_id="employee_001", message=COMPLETE_REQUEST
    )

    assert response.run.recommendation.selection_source == "deterministic_score"
    assert planner.last_outcome is None
    assert any(event.name == "agent.result_stale" for event in response.run.trace)
    assert any(event.name == "two_agent.fallback" for event in response.run.trace)


class ForbiddenToolAdapter(FixtureTwoAgentAdapter):
    async def execute_agent(self, envelope, *, context, allowed_tools):
        if envelope.task.agent == AgentKind.SOURCING:
            await self._invoke("get_budget", context, {}, ("get_budget",))
        return await super().execute_agent(envelope, context=context, allowed_tools=allowed_tools)


@pytest.mark.asyncio
async def test_runtime_rejects_tools_outside_agent_allowlist():
    planner = TwoAgentPlanner(ForbiddenToolAdapter())
    engine = make_engine()
    engine.planner = planner

    response = await engine.start(
        tenant_id="demo_cn", user_id="employee_001", message=COMPLETE_REQUEST
    )

    assert response.run.recommendation.selection_source == "deterministic_score"
    assert "forbidden tools" in (planner.last_error or "")
    assert AGENT_TOOL_ALLOWLIST == {
        AgentKind.REQUIREMENT: (),
        AgentKind.SOURCING: ("search_catalog",),
    }


@pytest.mark.asyncio
async def test_stage_budget_preflight_stops_before_agent_tools():
    planner = TwoAgentPlanner(FixtureTwoAgentAdapter(), fallback_on_error=False)
    engine = make_engine()
    engine.harness.execution_budget.max_specialist_requests = 0
    engine.planner = planner

    response = await engine.start(
        tenant_id="demo_cn", user_id="employee_001", message=COMPLETE_REQUEST
    )

    assert response.run.state == RunState.COLLECTING
    assert response.run.recommendation is None
    assert not any(event.kind == "TOOL" for event in response.run.trace)
    assert any(
        event.name == "budget.exceeded" and event.detail["resource"] == "specialist_requests"
        for event in response.run.trace
    )


def test_two_agent_feature_flag_builds_fixture_runtime(monkeypatch, tmp_path):
    from procure_pilot.api import app as app_module

    settings = Settings(
        _env_file=None,
        agent_mode="deterministic",
        agent_topology="two_agent",
        run_store_path=tmp_path / "runs.json",
    )
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)
    app_module.get_engine.cache_clear()
    try:
        engine = app_module.get_engine()
        assert isinstance(engine.planner, TwoAgentPlanner)
    finally:
        app_module.get_engine.cache_clear()


def test_two_agent_feature_flag_builds_provider_adapter(monkeypatch, tmp_path):
    from procure_pilot.agents.provider_adapter import OpenAICompatibleTwoAgentAdapter
    from procure_pilot.api import app as app_module

    settings = Settings(
        _env_file=None,
        agent_mode="openai",
        agent_topology="two_agent",
        model_provider="deepseek",
        requirement_model="deepseek-requirement",
        sourcing_model="deepseek-sourcing",
        openai_api_key="test-key-not-used",
        run_store_path=tmp_path / "runs.json",
    )
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)
    app_module.get_engine.cache_clear()
    try:
        engine = app_module.get_engine()
        assert isinstance(engine.planner, TwoAgentPlanner)
        assert isinstance(engine.planner.adapter, OpenAICompatibleTwoAgentAdapter)
        assert engine.planner.adapter.model_assignments == {
            AgentKind.REQUIREMENT: "deepseek-requirement",
            AgentKind.SOURCING: "deepseek-sourcing",
        }
    finally:
        app_module.get_engine.cache_clear()
