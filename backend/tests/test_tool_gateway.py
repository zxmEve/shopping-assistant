from __future__ import annotations

import pytest

from procure_pilot.agents.contracts import AgentKind
from procure_pilot.domain.execution import ToolCall
from procure_pilot.domain.models import Money, ProcurementRun, ToolBudgetPool
from procure_pilot.fixtures.catalog import FixtureCatalogAdapter
from procure_pilot.fixtures.enterprise import FixtureEnterpriseGateway
from procure_pilot.gateway.tool_gateway import (
    GatewayExecutionContext,
    ToolGateway,
    build_default_tool_registry,
)
from procure_pilot.harness.runtime import AgentHarness
from procure_pilot.persistence.budget_repository import InMemoryBudgetRepository
from procure_pilot.persistence.execution_repository import InMemoryExecutionRepository
from procure_pilot.services.budget import InMemoryBudgetService, StaticBudgetLimitProvider


def gateway_stack(*, provider_cost: int = 10):
    harness = AgentHarness(secret="gateway-test")
    execution_repository = InMemoryExecutionRepository()
    budget_repository = InMemoryBudgetRepository()
    limits = StaticBudgetLimitProvider(
        platform_daily=1_000,
        platform_monthly=10_000,
        tenant_monthly=5_000,
        user_daily=500,
    )
    budget_service = InMemoryBudgetService(budget_repository, limit_provider=limits)
    gateway = ToolGateway(
        registry=build_default_tool_registry(
            catalog=FixtureCatalogAdapter(),
            enterprise=FixtureEnterpriseGateway(),
            provider_costs_micro_usd={"internal_catalog": provider_cost},
        ),
        harness=harness,
        execution_repository=execution_repository,
        budget_service=budget_service,
        budget_limit_provider=limits,
    )
    run = ProcurementRun(tenant_id="demo_cn", user_id="employee_001")
    run.requirement.quantity = 1
    run.requirement.unit_budget = Money(amount=10_000)
    run.requirement.budget_tax_included = True
    run.requirement.delivery_city = "Shanghai"
    run.requirement.minimum_ram_gb = 32
    run.requirement.minimum_storage_gb = 1024
    harness.initialize_run(run)
    return gateway, execution_repository, budget_repository, run


@pytest.mark.asyncio
async def test_gateway_executes_only_registered_agent_tools_and_reuses_result():
    gateway, execution_repository, budget_repository, run = gateway_stack()
    context = GatewayExecutionContext(
        run=run,
        roles=("EMPLOYEE",),
        node_name="sourcing_agent",
        budget_pool=ToolBudgetPool.AGENT_READ,
        agent_kind=AgentKind.SOURCING.value,
    )

    first = await gateway.invoke(
        tool_name="search_catalog",
        arguments={"provider": "internal_catalog"},
        context=context,
    )
    second = await gateway.invoke(
        tool_name="search_catalog",
        arguments={"provider": "internal_catalog"},
        context=context,
    )

    assert second == first
    tools = execution_repository.list_tools(run.run_id, tenant_id=run.tenant_id)
    assert len(tools) == 1
    reservation = budget_repository.get_by_idempotency(
        f"budget:{tools[0].idempotency_key}"
    )
    assert reservation is not None
    assert reservation.actual_amount == 10


@pytest.mark.asyncio
async def test_gateway_rejects_unregistered_and_cross_agent_tools():
    gateway, _execution_repository, _budget_repository, run = gateway_stack()
    context = GatewayExecutionContext(
        run=run,
        roles=("EMPLOYEE",),
        node_name="requirement_agent",
        budget_pool=ToolBudgetPool.AGENT_READ,
        agent_kind=AgentKind.REQUIREMENT.value,
    )

    with pytest.raises(PermissionError):
        await gateway.invoke(tool_name="shell", arguments={}, context=context)
    with pytest.raises(PermissionError):
        await gateway.invoke(
            tool_name="search_catalog",
            arguments={"provider": "internal_catalog"},
            context=context,
        )


def test_tool_call_lease_and_fencing_token_prevent_parallel_owner():
    repository = InMemoryExecutionRepository()
    first = ToolCall(
        tenant_id="demo_cn",
        user_id="employee_001",
        run_id="run_lease",
        node_name="node",
        tool_name="search_catalog",
        risk="READ",
        budget_pool="agent_read",
        input_fingerprint="fingerprint",
        idempotency_key="same-call",
    )
    second = first.model_copy(
        update={"tool_call_id": "tc_second", "lease_owner": "worker_second"}
    )

    claimed_first = repository.claim_tool(first)
    claimed_second = repository.claim_tool(second)

    assert claimed_second.lease_owner == claimed_first.lease_owner
    with pytest.raises(PermissionError):
        repository.complete_tool(
            claimed_first.tool_call_id,
            {"ok": True},
            expected_fencing_token=claimed_first.fencing_token + 1,
        )
