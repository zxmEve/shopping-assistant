import asyncio

import pytest

from procure_pilot.domain.models import BudgetResource, ProcurementRun, RiskLevel, RunState
from procure_pilot.evals.runner import COMPLETE_REQUEST, make_engine
from procure_pilot.harness.runtime import (
    AgentHarness,
    CircuitOpenError,
    HarnessBudgetExceeded,
    RunRepository,
    ToolPolicy,
    ToolRegistry,
    ToolTimeoutError,
)


def make_run() -> ProcurementRun:
    return ProcurementRun(tenant_id="demo_cn", user_id="employee_001")


@pytest.mark.asyncio
async def test_happy_path_uses_versioned_legal_state_transitions():
    engine = make_engine()
    recommendation = await engine.start(
        tenant_id="demo_cn",
        user_id="employee_001",
        message=COMPLETE_REQUEST,
    )

    assert [event.to_state for event in recommendation.run.state_events] == [
        RunState.SOURCING,
        RunState.REVIEWING,
        RunState.WAITING_CONFIRMATION,
    ]

    completed = await engine.confirm(
        run_id=recommendation.run.run_id,
        tenant_id="demo_cn",
        user_id="employee_001",
        action_token=recommendation.run.action_token,
    )

    assert [event.to_state for event in completed.run.state_events] == [
        RunState.SOURCING,
        RunState.REVIEWING,
        RunState.WAITING_CONFIRMATION,
        RunState.REVALIDATING,
        RunState.DRAFT_CREATING,
        RunState.DRAFT_CREATED,
    ]
    assert [event.version for event in completed.run.state_events] == list(range(1, 7))
    assert completed.run.state_version == 6
    assert completed.run.execution_usage.state_transitions == 6


def test_illegal_transition_is_rejected_without_mutating_run():
    run = make_run()
    harness = AgentHarness(secret="test")
    harness.initialize_run(run)

    with pytest.raises(ValueError, match="Illegal state transition"):
        harness.transition(run, RunState.DRAFT_CREATED, reason="skip every safety gate")

    assert run.state == RunState.COLLECTING
    assert run.state_version == 0
    assert run.state_events == []
    assert run.execution_usage.state_transitions == 0


def test_transition_budget_stops_before_the_next_state_change():
    run = make_run()
    harness = AgentHarness(secret="test", max_steps=1)
    harness.initialize_run(run)
    harness.transition(run, RunState.WAITING_USER, reason="need clarification")

    with pytest.raises(HarnessBudgetExceeded) as error:
        harness.transition(run, RunState.COLLECTING, reason="user replied")

    assert error.value.resource == BudgetResource.STATE_TRANSITIONS
    assert run.state == RunState.WAITING_USER
    assert run.state_version == 1
    assert run.trace[-1].name == "budget.exceeded"
    assert run.trace[-1].detail == {
        "resource": "state_transitions",
        "used": 1,
        "limit": 1,
    }


def test_repository_round_trip_preserves_state_history_and_budget_usage(tmp_path):
    path = tmp_path / "runs.json"
    harness = AgentHarness(secret="test")
    run = make_run()
    harness.initialize_run(run)
    harness.transition(run, RunState.WAITING_USER, reason="need clarification")
    harness.consume(run, BudgetResource.TOOL_CALLS)
    RunRepository(path).save(run)

    restored = RunRepository(path).get(run.run_id)

    assert restored.state == RunState.WAITING_USER
    assert restored.state_version == 1
    assert restored.state_events[0].reason == "need clarification"
    assert restored.execution_usage.state_transitions == 1
    assert restored.execution_usage.tool_calls == 1


def test_wall_time_budget_is_checked_before_consuming_another_resource():
    run = make_run()
    harness = AgentHarness(secret="test", max_wall_time_ms=5)
    harness.initialize_run(run)
    run.execution_usage.wall_time_ms = 5

    with pytest.raises(HarnessBudgetExceeded) as error:
        harness.consume(run, BudgetResource.TOOL_CALLS)

    assert error.value.resource == BudgetResource.WALL_TIME_MS
    assert run.execution_usage.tool_calls == 0
    assert run.trace[-1].detail["resource"] == "wall_time_ms"


def test_waiting_for_user_does_not_consume_online_wall_time():
    run = make_run()
    harness = AgentHarness(secret="test", max_wall_time_ms=5)
    harness.initialize_run(run)
    run.created_at = run.created_at.replace(year=2020)

    harness.consume(run, BudgetResource.TOOL_CALLS)

    assert run.execution_usage.tool_calls == 1
    assert run.execution_usage.wall_time_ms == 0


@pytest.mark.asyncio
async def test_retry_succeeds_and_usage_is_persisted_on_the_run():
    policy = ToolPolicy(
        name="search_catalog",
        risk=RiskLevel.READ,
        allowed_roles=frozenset({"EMPLOYEE"}),
        description="test",
        max_attempts=3,
        backoff_ms=0,
    )
    harness = AgentHarness(
        secret="test",
        tool_registry=ToolRegistry((policy,)),
    )
    run = make_run()
    harness.initialize_run(run)
    attempts = 0

    class RetryableError(RuntimeError):
        code = "HTTP_429"

    async def operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RetryableError("rate limited")
        return "ok"

    result = await harness.execute_tool(
        run,
        tool_name="search_catalog",
        roles=("EMPLOYEE",),
        provider="provider-a",
        operation=operation,
    )

    assert result == "ok"
    assert run.execution_usage.tool_calls == 1
    assert run.execution_usage.provider_calls == 2
    assert run.execution_usage.retries == 1
    assert sum(event.name == "tool.retry_scheduled" for event in run.trace) == 1


@pytest.mark.asyncio
async def test_timeout_opens_circuit_and_next_call_is_blocked():
    policy = ToolPolicy(
        name="search_catalog",
        risk=RiskLevel.READ,
        allowed_roles=frozenset({"EMPLOYEE"}),
        description="test",
        timeout_ms=5,
        max_attempts=2,
        backoff_ms=0,
        circuit_failure_threshold=2,
    )
    harness = AgentHarness(
        secret="test",
        tool_registry=ToolRegistry((policy,)),
    )
    run = make_run()
    harness.initialize_run(run)

    async def slow_operation() -> str:
        await asyncio.sleep(0.05)
        return "late"

    with pytest.raises(ToolTimeoutError):
        await harness.execute_tool(
            run,
            tool_name="search_catalog",
            roles=("EMPLOYEE",),
            provider="provider-a",
            operation=slow_operation,
        )

    executed = False

    async def must_not_execute() -> str:
        nonlocal executed
        executed = True
        return "unexpected"

    with pytest.raises(CircuitOpenError):
        await harness.execute_tool(
            run,
            tool_name="search_catalog",
            roles=("EMPLOYEE",),
            provider="provider-a",
            operation=must_not_execute,
        )

    assert executed is False
    assert run.execution_usage.provider_calls == 2
    assert run.execution_usage.retries == 1
    assert run.trace[-1].name == "tool.circuit_open"


@pytest.mark.asyncio
async def test_provider_bulkhead_caps_parallel_operations():
    policy = ToolPolicy(
        name="search_catalog",
        risk=RiskLevel.READ,
        allowed_roles=frozenset({"EMPLOYEE"}),
        description="test",
        max_attempts=1,
    )
    harness = AgentHarness(
        secret="test",
        max_tool_calls=8,
        max_provider_calls=8,
        max_parallel_provider_calls=2,
        tool_registry=ToolRegistry((policy,)),
    )
    run = make_run()
    harness.initialize_run(run)
    active = 0
    peak = 0

    async def operation() -> str:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return "ok"

    await asyncio.gather(
        *[
            harness.execute_tool(
                run,
                tool_name="search_catalog",
                roles=("EMPLOYEE",),
                provider=f"provider-{index}",
                operation=operation,
            )
            for index in range(4)
        ]
    )

    assert peak == 2


@pytest.mark.asyncio
async def test_confirmation_stays_retryable_when_all_final_state_transitions_do_not_fit():
    engine = make_engine()
    response = await engine.start(
        tenant_id="demo_cn",
        user_id="employee_001",
        message=COMPLETE_REQUEST,
    )
    stored = engine.repository.get(response.run.run_id)
    stored.execution_budget.max_state_transitions = 5
    engine.repository.save(stored)

    stopped = await engine.confirm(
        run_id=response.run.run_id,
        tenant_id="demo_cn",
        user_id="employee_001",
        action_token=response.run.action_token,
    )

    assert stopped.run.state == RunState.WAITING_CONFIRMATION
    assert stopped.run.draft is None
    assert stopped.run.execution_usage.commit_tool_calls == 0
    assert stopped.run.execution_usage.reserved_commit_tool_calls == 0
    assert all(event.to_state != RunState.REVALIDATING for event in stopped.run.state_events)
    assert stopped.run.trace[-1].name == "budget.exceeded"


@pytest.mark.asyncio
async def test_confirmation_stays_retryable_when_commit_reservation_does_not_fit():
    engine = make_engine()
    response = await engine.start(
        tenant_id="demo_cn",
        user_id="employee_001",
        message=COMPLETE_REQUEST,
    )
    stored = engine.repository.get(response.run.run_id)
    stored.execution_budget.max_commit_tool_calls = 2
    engine.repository.save(stored)

    stopped = await engine.confirm(
        run_id=response.run.run_id,
        tenant_id="demo_cn",
        user_id="employee_001",
        action_token=response.run.action_token,
    )

    assert stopped.run.state == RunState.WAITING_CONFIRMATION
    assert stopped.run.draft is None
    assert stopped.run.execution_usage.commit_tool_calls == 0
    assert stopped.run.execution_usage.reserved_commit_tool_calls == 0
    assert stopped.run.trace[-1].detail["resource"] == "commit_tool_calls"
