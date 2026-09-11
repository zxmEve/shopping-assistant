from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol
from uuid import uuid4

from agents.tool_context import ToolContext
from pydantic import BaseModel, Field

from procure_pilot.agents.contracts import (
    AgentKind,
    AgentResult,
    AgentTask,
    AgentTaskEnvelope,
    TwoAgentOutcome,
)
from procure_pilot.agents.runtime import AgentPlan, DeterministicAgentPlanner
from procure_pilot.agents.tools import (
    AgentCandidateProposal,
    AgentRuntimeContext,
    build_function_tools,
)
from procure_pilot.domain.models import BudgetResource, Message
from procure_pilot.gateway.tool_gateway import (
    DEFAULT_AGENT_TOOL_NAMES,
    GatewayToolRegistry,
)
from procure_pilot.harness.runtime import HarnessBudgetExceeded


class ModelCallUsage(BaseModel):
    requests: int = Field(default=1, gt=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_micro_usd: int = Field(default=0, ge=0)
    output_repaired: bool = False


@dataclass(frozen=True)
class AdapterCall[T]:
    output: T
    usage: ModelCallUsage = field(default_factory=ModelCallUsage)


class TwoAgentAdapter(Protocol):
    async def execute_agent(
        self,
        envelope: AgentTaskEnvelope,
        *,
        context: AgentRuntimeContext,
        allowed_tools: tuple[str, ...],
    ) -> AdapterCall[AgentResult]: ...


class TwoAgentRuntimeError(RuntimeError):
    pass


class StaleAgentResultError(TwoAgentRuntimeError):
    pass


# Models only get the minimum read capability needed for judgement. Cost, budget,
# policy, state transitions and writes are deliberately absent.
AGENT_TOOL_ALLOWLIST: dict[AgentKind, tuple[str, ...]] = {
    AgentKind(kind): names for kind, names in DEFAULT_AGENT_TOOL_NAMES.items()
}

AGENT_TRACE_ALLOWLIST: dict[AgentKind, frozenset[str]] = {
    AgentKind.REQUIREMENT: frozenset({"inspect_requirement"}),
    AgentKind.SOURCING: frozenset({"search_catalog"}),
}

REQUIRED_FIELDS = (
    "quantity",
    "unit_budget",
    "budget_tax_included",
    "delivery_city",
    "minimum_ram_gb",
    "minimum_storage_gb",
)


class AgentFactory:
    def available_agents(self) -> list[AgentKind]:
        return list(AGENT_TOOL_ALLOWLIST)

    def allowed_tools(
        self, agent: AgentKind, registry: GatewayToolRegistry | None = None
    ) -> tuple[str, ...]:
        if registry is not None:
            return tuple(item.name for item in registry.for_agent(agent.value))
        try:
            return AGENT_TOOL_ALLOWLIST[agent]
        except KeyError as exc:
            raise TwoAgentRuntimeError(f"No capability registered for {agent}") from exc

    def allowed_trace_tools(
        self, agent: AgentKind, registry: GatewayToolRegistry | None = None
    ) -> frozenset[str]:
        if registry is not None:
            return frozenset(item.policy_name for item in registry.for_agent(agent.value))
        try:
            return AGENT_TRACE_ALLOWLIST[agent]
        except KeyError as exc:
            raise TwoAgentRuntimeError(f"No trace policy registered for {agent}") from exc


class TwoAgentPlanner:
    """Deterministic workflow router around two narrowly-scoped model agents."""

    pattern = "deterministic-runtime-with-two-agents"

    def __init__(
        self,
        adapter: TwoAgentAdapter,
        *,
        agent_factory: AgentFactory | None = None,
        fallback_on_error: bool = True,
    ) -> None:
        self.adapter = adapter
        self.agent_factory = agent_factory or AgentFactory()
        self.fallback_on_error = fallback_on_error
        self.fallback = DeterministicAgentPlanner()
        self.last_outcome: TwoAgentOutcome | None = None
        self.last_error: str | None = None
        self._has_reserved_first_global_request = False

    async def plan(
        self,
        messages: list[Message],
        *,
        context: AgentRuntimeContext | None = None,
    ) -> AgentPlan:
        if context is None:
            raise ValueError("Two-agent mode requires an AgentRuntimeContext")
        self.last_outcome = None
        self.last_error = None
        self._has_reserved_first_global_request = False
        try:
            return await self._run(messages, context)
        except HarnessBudgetExceeded:
            raise
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            context.harness.trace(
                context.run,
                actor="Application Runtime",
                kind="HARNESS",
                name="two_agent.fallback",
                detail={"error": self.last_error, "fallback": "deterministic"},
                status="DEGRADED",
            )
            if not self.fallback_on_error:
                raise
            return await self.fallback.plan(messages, context=context)

    async def _run(self, messages: list[Message], context: AgentRuntimeContext) -> AgentPlan:
        missing = [
            name for name in REQUIRED_FIELDS if getattr(context.run.requirement, name) is None
        ]
        kind = AgentKind.REQUIREMENT if missing else AgentKind.SOURCING
        task = AgentTask(
            task_id=f"{'requirement' if missing else 'source'}_{context.run.state_version}",
            agent=kind,
            objective=(
                "Identify the smallest set of missing procurement facts."
                if missing
                else "Search approved providers and propose real catalog offer IDs."
            ),
            max_model_requests=2 if missing else 4,
            max_tool_calls=1 if missing else 3,
        )
        self._preflight(context)
        assembled = None
        if context.context_assembler is not None:
            employee = context.enterprise.identity(context.run.tenant_id, context.run.user_id)
            assembled = context.context_assembler.assemble(
                agent=kind,
                run=context.run,
                team_id=employee.department,
            )
            context.harness.trace(
                context.run,
                actor="ContextAssembler",
                kind="SERVICE",
                name="context.assembly_completed",
                detail={
                    "agent": kind,
                    "policy_version": assembled.payload.policy_version,
                    "estimated_tokens": assembled.payload.estimated_tokens,
                    "recent_message_count": len(assembled.payload.recent_messages),
                    "dropped_message_count": assembled.dropped_message_count,
                    "selected": [
                        {
                            "memory_id": item.memory_id,
                            "scope": item.scope,
                            "key": item.key,
                            "key_definition_version": item.key_definition_version,
                            "category_id": item.category_id,
                            "tags": item.tags,
                            "version": item.version,
                            "confidence": item.confidence,
                            "relevance_score": item.relevance_score,
                        }
                        for item in assembled.payload.memories
                    ],
                    "excluded": [item.model_dump(mode="json") for item in assembled.exclusions],
                },
            )
        result = await self._execute_task(
            AgentTaskEnvelope(
                run_id=context.run.run_id,
                state_version=context.run.state_version,
                task=task,
                requirement=context.run.requirement.model_dump(mode="json"),
                missing_fields=missing,
                context_summary=(messages[-1].content if messages and assembled is None else ""),
                context=assembled.payload if assembled is not None else None,
            ),
            context,
        )
        if kind == AgentKind.REQUIREMENT:
            proposals: list[AgentCandidateProposal] = []
            stop_condition = "wait_for_user"
            plan = AgentPlan(
                next_action="clarify",
                specialist="Requirement Agent",
                rationale="需求仍缺少会影响选型、价格或交付的必要信息。",
                required_evidence=["requirement"],
                candidate_proposals=[],
                stop_condition=stop_condition,
            )
        else:
            proposals = result.candidate_proposals
            stop_condition = "wait_for_user_confirmation"
            plan = AgentPlan(
                next_action="validate_and_recommend",
                specialist="Sourcing Agent",
                rationale="寻源 Agent 给出候选；成本、预算、政策和证据由服务端重新校验。",
                required_evidence=["catalog"],
                candidate_proposals=proposals,
                stop_condition=stop_condition,
            )
        self.last_outcome = TwoAgentOutcome(
            state_version=context.run.state_version,
            executed_agent=kind,
            result=result,
            candidate_proposals=proposals,
            stop_condition=stop_condition,
            rationale=plan.rationale,
        )
        context.harness.trace(
            context.run,
            actor="Application Runtime",
            kind="AGENT",
            name="two_agent.completed",
            detail={
                "state_version": context.run.state_version,
                "executed_agent": kind,
                "candidate_count": len(proposals),
                "stop_condition": stop_condition,
            },
        )
        return plan

    @staticmethod
    def _preflight(context: AgentRuntimeContext) -> None:
        # execute_agent_turn already reserved the first global AGENT_TURNS unit.
        context.harness.assert_budget_available(context.run, BudgetResource.SPECIALIST_REQUESTS, 1)
        context.harness.assert_budget_available(context.run, BudgetResource.STAGE_MODEL_REQUESTS, 1)

    async def _execute_task(
        self, envelope: AgentTaskEnvelope, context: AgentRuntimeContext
    ) -> AgentResult:
        task = envelope.task
        execution_id = f"ax_{uuid4().hex[:12]}"
        gateway_registry = context.require_gateway().registry
        allowed_tools = self.agent_factory.allowed_tools(task.agent, gateway_registry)
        context.agent_kind = task.agent.value
        trace_start = len(context.run.trace)
        context.harness.trace(
            context.run,
            actor="Application Runtime",
            kind="AGENT",
            name="agent.execution_started",
            detail={
                "execution_id": execution_id,
                "task_id": task.task_id,
                "agent": task.agent,
                "state_version": envelope.state_version,
                "allowed_tools": allowed_tools,
            },
        )
        with context.harness.agent_execution(execution_id):
            call = await self._invoke_adapter(
                context,
                task.agent,
                lambda: self.adapter.execute_agent(
                    envelope, context=context, allowed_tools=allowed_tools
                ),
                max_requests=task.max_model_requests,
                operation_name=f"agent:{task.task_id}",
            )
        result = AgentResult.model_validate(call.output)
        if result.task_id != task.task_id or result.agent != task.agent:
            raise TwoAgentRuntimeError("Agent result does not match its task envelope")
        if result.state_version != context.run.state_version:
            context.harness.trace(
                context.run,
                actor="Application Runtime",
                kind="HARNESS",
                name="agent.result_stale",
                detail={"expected": context.run.state_version, "received": result.state_version},
                status="REJECTED",
            )
            raise StaleAgentResultError(f"Stale result for task {task.task_id}")
        if result.status == "FAILED":
            raise TwoAgentRuntimeError(f"Agent task failed: {task.task_id}")
        tool_events = [
            event
            for event in context.run.trace[trace_start:]
            if event.kind == "TOOL" and event.detail.get("agent_execution_id") == execution_id
        ]
        forbidden = {event.name for event in tool_events} - self.agent_factory.allowed_trace_tools(
            task.agent, gateway_registry
        )
        if forbidden:
            raise TwoAgentRuntimeError(
                f"Agent {task.agent} used forbidden tools: {sorted(forbidden)}"
            )
        if len(tool_events) > task.max_tool_calls:
            raise TwoAgentRuntimeError(f"Agent tool budget exceeded: {task.task_id}")
        context.harness.trace(
            context.run,
            actor="Application Runtime",
            kind="AGENT",
            name="agent.execution_completed",
            detail={
                "execution_id": execution_id,
                "task_id": task.task_id,
                "agent": task.agent,
                "tool_calls": len(tool_events),
                "evidence_refs": result.evidence_refs,
            },
            status=result.status,
        )
        return result

    async def _invoke_adapter[T](
        self,
        context: AgentRuntimeContext,
        agent: AgentKind,
        operation: Callable[[], Awaitable[AdapterCall[T]]],
        *,
        max_requests: int,
        operation_name: str,
    ) -> AdapterCall[T]:
        self._reserve_model_requests(context, 1)
        try:
            call = await operation()
        except Exception as exc:
            context.harness.trace(
                context.run,
                actor="Application Runtime",
                kind="AGENT",
                name="agent.model_call_failed",
                detail={"agent": agent, "operation": operation_name, "error": str(exc)},
                status="FAILED",
            )
            raise
        if call.usage.requests > max_requests:
            raise TwoAgentRuntimeError(
                f"{agent} used {call.usage.requests} model requests, limit is {max_requests}"
            )
        if call.usage.requests > 1:
            self._reserve_model_requests(context, call.usage.requests - 1)
        context.harness.record_model_usage(
            context.run,
            input_tokens=call.usage.input_tokens,
            output_tokens=call.usage.output_tokens,
            cost_micro_usd=call.usage.cost_micro_usd,
        )
        context.harness.trace(
            context.run,
            actor="Application Runtime",
            kind="AGENT",
            name="agent.model_call_completed",
            detail={
                "agent": agent,
                "operation": operation_name,
                "requests": call.usage.requests,
                "input_tokens": call.usage.input_tokens,
                "output_tokens": call.usage.output_tokens,
                "output_repaired": call.usage.output_repaired,
            },
            status="RECOVERED" if call.usage.output_repaired else "SUCCEEDED",
        )
        return call

    def _reserve_model_requests(self, context: AgentRuntimeContext, amount: int) -> None:
        resources = [BudgetResource.STAGE_MODEL_REQUESTS, BudgetResource.SPECIALIST_REQUESTS]
        if self._has_reserved_first_global_request:
            resources.append(BudgetResource.AGENT_TURNS)
        for resource in resources:
            context.harness.assert_budget_available(context.run, resource, amount)
        for resource in resources:
            context.harness.consume(context.run, resource, amount)
        self._has_reserved_first_global_request = True


class FixtureTwoAgentAdapter:
    """Network-free adapter for deterministic tests and local demonstrations."""

    def __init__(self) -> None:
        self.tools = build_function_tools()

    async def execute_agent(
        self,
        envelope: AgentTaskEnvelope,
        *,
        context: AgentRuntimeContext,
        allowed_tools: tuple[str, ...],
    ) -> AdapterCall[AgentResult]:
        if envelope.task.agent == AgentKind.REQUIREMENT:
            inspected = await self._invoke(
                "inspect_procurement_requirement", context, {}, allowed_tools
            )
            return AdapterCall(
                AgentResult(
                    task_id=envelope.task.task_id,
                    agent=envelope.task.agent,
                    state_version=envelope.state_version,
                    status="SUCCEEDED",
                    output={"missing_fields": inspected.missing_fields},
                )
            )
        if envelope.task.agent == AgentKind.SOURCING:
            offers = []
            for provider in ("internal_catalog", "marketplace_alpha", "marketplace_beta"):
                catalog = await self._invoke(
                    "search_catalog", context, {"provider": provider}, allowed_tools
                )
                offers.extend(catalog.offers)
            if not offers:
                raise TwoAgentRuntimeError("Sourcing Agent found no verified offers")
            selected = min(offers, key=lambda item: (Decimal(item.unit_price), item.delivery_days))
            proposal = AgentCandidateProposal(
                provider=selected.provider,
                offer_id=selected.offer_id,
                evidence_refs=selected.evidence_refs,
            )
            return AdapterCall(
                AgentResult(
                    task_id=envelope.task.task_id,
                    agent=envelope.task.agent,
                    state_version=envelope.state_version,
                    status="SUCCEEDED",
                    output={"compared_providers": 3},
                    evidence_refs=selected.evidence_refs,
                    candidate_proposals=[proposal],
                )
            )
        raise TwoAgentRuntimeError(f"Unsupported fixture Agent: {envelope.task.agent}")

    async def _invoke(
        self,
        name: str,
        context: AgentRuntimeContext,
        arguments: dict,
        allowed_tools: tuple[str, ...],
    ):
        if name not in allowed_tools:
            raise PermissionError(f"Tool {name} is not allowed for this Agent")
        tool = self.tools[name]
        payload = json.dumps(arguments)
        return await tool.on_invoke_tool(
            ToolContext(
                context,
                tool_name=tool.name,
                tool_call_id=f"agent-{uuid4().hex[:10]}",
                tool_arguments=payload,
            ),
            payload,
        )
