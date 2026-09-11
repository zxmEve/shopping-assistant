from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Literal, TypedDict
from uuid import uuid4

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel

from procure_pilot.agents.contracts import AgentKind
from procure_pilot.config import Settings
from procure_pilot.domain.models import RunResponse, RunState
from procure_pilot.services.engine import (
    AgentStageResult,
    ConfirmationStageResult,
    ProcurementEngine,
)

WorkflowWaitReason = Literal["MISSING_REQUIREMENT", "WAITING_CONFIRMATION"]


class ProcurementGraphState(TypedDict, total=False):
    """Small control-plane state; business facts remain in ProcurementRun."""

    run_id: str
    tenant_id: str
    user_id: str
    message: str | None
    scenario: str | None
    action_token: str | None
    expected_run_version: int
    run_state: str
    assistant_message: str
    suggested_replies: list[str]
    wait_reason: WorkflowWaitReason | None
    last_node: str
    agent_kind: Literal["REQUIREMENT", "SOURCING"]
    agent_stage: dict[str, Any] | None
    confirmation_stage: dict[str, Any] | None


class WorkflowStatus(BaseModel):
    backend: str
    checkpoint_backend: str
    run_id: str
    run_state: RunState
    expected_run_version: int
    waiting_at: str | None
    wait_reason: WorkflowWaitReason | None
    last_node: str | None


class LangGraphProcurementWorkflow:
    """Durable request lifecycle around the existing safe procurement engine."""

    backend = "langgraph"

    def __init__(
        self,
        engine: ProcurementEngine,
        checkpointer: BaseCheckpointSaver,
        *,
        checkpoint_backend: str,
        fault_injector: Callable[[str, ProcurementGraphState], None] | None = None,
    ) -> None:
        self.engine = engine
        self.checkpoint_backend = checkpoint_backend
        self._locks: dict[str, asyncio.Lock] = {}
        self._fault_injector = fault_injector
        builder = StateGraph(ProcurementGraphState)
        builder.add_node("authorize_start", self._authorize_start)
        builder.add_node("initialize_start", self._initialize_start)
        builder.add_node("route_agent", self._route_agent)
        builder.add_node("requirement_agent", self._requirement_agent)
        builder.add_node("sourcing_agent", self._sourcing_agent)
        builder.add_node("complete_turn", self._complete_turn)
        builder.add_node("route_after_run", self._route_after_run)
        builder.add_node("wait_for_user", self._wait_for_user)
        builder.add_node("accept_clarification", self._accept_clarification)
        builder.add_node("wait_for_confirmation", self._wait_for_confirmation)
        builder.add_node("prepare_confirmation", self._prepare_confirmation)
        builder.add_node("revalidate_quote", self._revalidate_quote)
        builder.add_node("revalidate_policy", self._revalidate_policy)
        builder.add_node("create_draft", self._create_draft)
        builder.add_edge(START, "authorize_start")
        builder.add_edge("authorize_start", "initialize_start")
        builder.add_edge("initialize_start", "route_agent")
        builder.add_conditional_edges(
            "route_agent",
            self._next_agent,
            {
                "requirement_agent": "requirement_agent",
                "sourcing_agent": "sourcing_agent",
            },
        )
        builder.add_edge("requirement_agent", "complete_turn")
        builder.add_edge("sourcing_agent", "complete_turn")
        builder.add_edge("complete_turn", "route_after_run")
        builder.add_conditional_edges(
            "route_after_run",
            self._next_after_run,
            {
                "wait_for_user": "wait_for_user",
                "wait_for_confirmation": "wait_for_confirmation",
                "end": END,
            },
        )
        builder.add_edge("wait_for_user", "accept_clarification")
        builder.add_edge("accept_clarification", "route_agent")
        builder.add_edge("wait_for_confirmation", "prepare_confirmation")
        builder.add_conditional_edges(
            "prepare_confirmation",
            self._next_confirmation_stage,
            {"revalidate_quote": "revalidate_quote", "route": "route_after_run"},
        )
        builder.add_conditional_edges(
            "revalidate_quote",
            self._next_quote_stage,
            {"revalidate_policy": "revalidate_policy", "route": "route_after_run"},
        )
        builder.add_conditional_edges(
            "revalidate_policy",
            self._next_policy_stage,
            {"create_draft": "create_draft", "route": "route_after_run"},
        )
        builder.add_edge("create_draft", "route_after_run")
        self.graph = builder.compile(checkpointer=checkpointer)

    async def start_run(
        self,
        *,
        tenant_id: str,
        user_id: str,
        message: str,
        scenario: str | None = None,
    ) -> RunResponse:
        run_id = f"run_{uuid4().hex[:12]}"
        config = self._config(tenant_id, run_id)
        output = await self.graph.ainvoke(
            ProcurementGraphState(
                run_id=run_id,
                tenant_id=tenant_id,
                user_id=user_id,
                message=message,
                scenario=scenario,
                action_token=None,
                expected_run_version=0,
                wait_reason=None,
                last_node="input",
                agent_stage=None,
                confirmation_stage=None,
            ),
            config,
        )
        return self._response(output)

    async def continue_run(
        self,
        *,
        run_id: str,
        tenant_id: str,
        user_id: str,
        message: str,
        scenario: str | None = None,
    ) -> RunResponse:
        async with self._lock(run_id):
            config = self._config(tenant_id, run_id)
            await self._assert_resumable(
                config,
                run_id=run_id,
                tenant_id=tenant_id,
                user_id=user_id,
                expected_node="wait_for_user",
            )
            output = await self.graph.ainvoke(
                Command(
                    resume={
                        "event_type": "USER_MESSAGE",
                        "message": message,
                        "scenario": scenario,
                    }
                ),
                config,
            )
            return self._response(output)

    async def resume_pending(
        self,
        *,
        run_id: str,
        tenant_id: str,
        user_id: str,
    ) -> RunResponse:
        """Continue a non-interrupt node after the previous worker disappeared."""
        async with self._lock(run_id):
            config = self._config(tenant_id, run_id)
            run, snapshot = await self._assert_accessible_checkpoint(
                config,
                run_id=run_id,
                tenant_id=tenant_id,
                user_id=user_id,
            )
            pending = snapshot.next[0] if len(snapshot.next) == 1 else None
            recoverable = {
                "initialize_start",
                "route_agent",
                "requirement_agent",
                "sourcing_agent",
                "complete_turn",
                "route_after_run",
                "accept_clarification",
                "prepare_confirmation",
                "revalidate_quote",
                "revalidate_policy",
                "create_draft",
            }
            if pending not in recoverable:
                raise ValueError(f"Workflow has no recoverable pending node: {snapshot.next}")
            expected_version = int(snapshot.values.get("expected_run_version", -1))
            if run.state_version < expected_version:
                raise ValueError("Business Run version is older than its LangGraph checkpoint")
            output = await self.graph.ainvoke(None, config)
            return self._response(output)

    async def confirm_run(
        self,
        *,
        run_id: str,
        tenant_id: str,
        user_id: str,
        action_token: str,
    ) -> RunResponse:
        async with self._lock(run_id):
            config = self._config(tenant_id, run_id)
            await self._assert_resumable(
                config,
                run_id=run_id,
                tenant_id=tenant_id,
                user_id=user_id,
                expected_node="wait_for_confirmation",
            )
            output = await self.graph.ainvoke(
                Command(
                    resume={
                        "event_type": "CONFIRM",
                        "action_token": action_token,
                    }
                ),
                config,
            )
            return self._response(output)

    async def status(
        self,
        *,
        run_id: str,
        tenant_id: str,
        user_id: str,
    ) -> WorkflowStatus:
        run = self.engine.get(run_id, tenant_id=tenant_id, user_id=user_id)
        snapshot = await self.graph.aget_state(self._config(tenant_id, run_id))
        values = snapshot.values
        if not values or values.get("user_id") != user_id:
            raise ValueError("Run has no LangGraph checkpoint in this workflow")
        return WorkflowStatus(
            backend=self.backend,
            checkpoint_backend=self.checkpoint_backend,
            run_id=run_id,
            run_state=run.state,
            expected_run_version=int(values.get("expected_run_version", 0)),
            waiting_at=snapshot.next[0] if snapshot.next else None,
            wait_reason=values.get("wait_reason"),
            last_node=values.get("last_node"),
        )

    async def _authorize_start(self, state: ProcurementGraphState) -> dict[str, Any]:
        self.engine.enterprise.identity(state["tenant_id"], state["user_id"])
        return {"last_node": "authorize_start"}

    async def _initialize_start(self, state: ProcurementGraphState) -> dict[str, Any]:
        self._inject_fault("initialize_start", state)
        run = self.engine.initialize_workflow_run(
            run_id=state["run_id"],
            tenant_id=state["tenant_id"],
            user_id=state["user_id"],
            message=state["message"] or "",
            scenario=state.get("scenario"),
        )
        update = self._run_update(run, node="initialize_start")
        self._inject_fault("initialize_start:after", state)
        return update

    def _route_agent(self, state: ProcurementGraphState) -> dict[str, Any]:
        run = self.engine.get(
            state["run_id"], tenant_id=state["tenant_id"], user_id=state["user_id"]
        )
        return {
            "agent_kind": self.engine.required_agent_kind(run).value,
            "expected_run_version": run.state_version,
            "last_node": "route_agent",
        }

    @staticmethod
    def _next_agent(state: ProcurementGraphState) -> str:
        return (
            "requirement_agent"
            if state["agent_kind"] == AgentKind.REQUIREMENT.value
            else "sourcing_agent"
        )

    async def _requirement_agent(self, state: ProcurementGraphState) -> dict[str, Any]:
        return await self._execute_agent(state, AgentKind.REQUIREMENT, "requirement_agent")

    async def _sourcing_agent(self, state: ProcurementGraphState) -> dict[str, Any]:
        return await self._execute_agent(state, AgentKind.SOURCING, "sourcing_agent")

    async def _execute_agent(
        self,
        state: ProcurementGraphState,
        kind: AgentKind,
        node_name: str,
    ) -> dict[str, Any]:
        self._inject_fault(node_name, state)
        result = await self.engine.execute_agent_node(
            run_id=state["run_id"],
            tenant_id=state["tenant_id"],
            user_id=state["user_id"],
            expected_kind=kind,
            node_name=node_name,
        )
        run = self.engine.get(
            state["run_id"], tenant_id=state["tenant_id"], user_id=state["user_id"]
        )
        update = {
            **self._run_update(run, node=node_name),
            "agent_stage": result.model_dump(mode="json"),
        }
        self._inject_fault(f"{node_name}:after", state)
        return update

    async def _complete_turn(self, state: ProcurementGraphState) -> dict[str, Any]:
        self._inject_fault("complete_turn", state)
        if state.get("agent_stage") is None:
            raise ValueError("Agent checkpoint is missing before turn completion")
        response = await self.engine.complete_workflow_turn(
            run_id=state["run_id"],
            tenant_id=state["tenant_id"],
            user_id=state["user_id"],
            agent_stage=AgentStageResult.model_validate(state["agent_stage"]),
        )
        update = self._response_update(response, node="complete_turn")
        self._inject_fault("complete_turn:after", state)
        return update

    @staticmethod
    def _route_after_run(state: ProcurementGraphState) -> dict[str, Any]:
        run_state = RunState(state["run_state"])
        reason: WorkflowWaitReason | None = None
        if run_state == RunState.WAITING_USER:
            reason = "MISSING_REQUIREMENT"
        elif run_state == RunState.WAITING_CONFIRMATION:
            reason = "WAITING_CONFIRMATION"
        return {"wait_reason": reason, "last_node": "route_after_run"}

    @staticmethod
    def _next_after_run(state: ProcurementGraphState) -> str:
        if state.get("wait_reason") == "MISSING_REQUIREMENT":
            return "wait_for_user"
        if state.get("wait_reason") == "WAITING_CONFIRMATION":
            return "wait_for_confirmation"
        return "end"

    @staticmethod
    def _wait_for_user(state: ProcurementGraphState) -> dict[str, Any]:
        resumed = interrupt(
            {
                "type": "MISSING_REQUIREMENT",
                "run_id": state["run_id"],
                "expected_run_version": state["expected_run_version"],
            }
        )
        if not isinstance(resumed, dict) or resumed.get("event_type") != "USER_MESSAGE":
            raise ValueError("Clarification checkpoint requires a USER_MESSAGE event")
        message = resumed.get("message")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("Clarification message cannot be empty")
        scenario = resumed.get("scenario")
        if scenario is not None and not isinstance(scenario, str):
            raise ValueError("Scenario must be a string")
        return {
            "message": message,
            "scenario": scenario,
            "last_node": "wait_for_user",
        }

    async def _accept_clarification(self, state: ProcurementGraphState) -> dict[str, Any]:
        self._inject_fault("accept_clarification", state)
        run = self.engine.accept_workflow_message(
            run_id=state["run_id"],
            tenant_id=state["tenant_id"],
            user_id=state["user_id"],
            message=state["message"] or "",
            scenario=state.get("scenario"),
        )
        update = {**self._run_update(run, node="accept_clarification"), "agent_stage": None}
        self._inject_fault("accept_clarification:after", state)
        return update

    @staticmethod
    def _wait_for_confirmation(state: ProcurementGraphState) -> dict[str, Any]:
        resumed = interrupt(
            {
                "type": "WAITING_CONFIRMATION",
                "run_id": state["run_id"],
                "expected_run_version": state["expected_run_version"],
            }
        )
        if not isinstance(resumed, dict) or resumed.get("event_type") != "CONFIRM":
            raise ValueError("Confirmation checkpoint requires a CONFIRM event")
        action_token = resumed.get("action_token")
        if not isinstance(action_token, str) or not action_token:
            raise ValueError("Confirmation requires an action token")
        return {"action_token": action_token, "last_node": "wait_for_confirmation"}

    async def _prepare_confirmation(self, state: ProcurementGraphState) -> dict[str, Any]:
        self._inject_fault("prepare_confirmation", state)
        result = self.engine.prepare_confirmation_node(
            run_id=state["run_id"],
            tenant_id=state["tenant_id"],
            user_id=state["user_id"],
            action_token=state["action_token"] or "",
        )
        update = self._confirmation_update(state, result, "prepare_confirmation")
        self._inject_fault("prepare_confirmation:after", state)
        return update

    @staticmethod
    def _next_confirmation_stage(state: ProcurementGraphState) -> str:
        result = ConfirmationStageResult.model_validate(state["confirmation_stage"])
        return "revalidate_quote" if result.outcome == "READY_FOR_QUOTE" else "route"

    async def _revalidate_quote(self, state: ProcurementGraphState) -> dict[str, Any]:
        self._inject_fault("revalidate_quote", state)
        result = await self.engine.revalidate_quote_node(
            run_id=state["run_id"],
            tenant_id=state["tenant_id"],
            user_id=state["user_id"],
        )
        update = self._confirmation_update(state, result, "revalidate_quote")
        self._inject_fault("revalidate_quote:after", state)
        return update

    @staticmethod
    def _next_quote_stage(state: ProcurementGraphState) -> str:
        result = ConfirmationStageResult.model_validate(state["confirmation_stage"])
        return "revalidate_policy" if result.outcome == "READY_FOR_POLICY" else "route"

    async def _revalidate_policy(self, state: ProcurementGraphState) -> dict[str, Any]:
        self._inject_fault("revalidate_policy", state)
        previous = ConfirmationStageResult.model_validate(state["confirmation_stage"])
        if previous.refreshed_offer is None:
            raise ValueError("Quote checkpoint is missing before policy revalidation")
        result = await self.engine.revalidate_policy_node(
            run_id=state["run_id"],
            tenant_id=state["tenant_id"],
            user_id=state["user_id"],
            refreshed_offer=previous.refreshed_offer,
        )
        update = self._confirmation_update(state, result, "revalidate_policy")
        self._inject_fault("revalidate_policy:after", state)
        return update

    @staticmethod
    def _next_policy_stage(state: ProcurementGraphState) -> str:
        result = ConfirmationStageResult.model_validate(state["confirmation_stage"])
        return "create_draft" if result.outcome == "READY_FOR_WRITE" else "route"

    async def _create_draft(self, state: ProcurementGraphState) -> dict[str, Any]:
        self._inject_fault("create_draft", state)
        previous = ConfirmationStageResult.model_validate(state["confirmation_stage"])
        if previous.candidate is None:
            raise ValueError("Policy checkpoint is missing before draft creation")
        result = await self.engine.create_draft_node(
            run_id=state["run_id"],
            tenant_id=state["tenant_id"],
            user_id=state["user_id"],
            candidate=previous.candidate,
        )
        update = self._confirmation_update(state, result, "create_draft")
        self._inject_fault("create_draft:after", state)
        return update

    def _confirmation_update(
        self,
        state: ProcurementGraphState,
        result: ConfirmationStageResult,
        node_name: str,
    ) -> dict[str, Any]:
        run = self.engine.get(
            state["run_id"], tenant_id=state["tenant_id"], user_id=state["user_id"]
        )
        update = self._run_update(run, node=node_name)
        update["confirmation_stage"] = result.model_dump(mode="json")
        if result.assistant_message:
            update["assistant_message"] = result.assistant_message
        return update

    async def _assert_resumable(
        self,
        config: dict[str, Any],
        *,
        run_id: str,
        tenant_id: str,
        user_id: str,
        expected_node: str,
    ) -> None:
        run, snapshot = await self._assert_accessible_checkpoint(
            config,
            run_id=run_id,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        values = snapshot.values
        if snapshot.next != (expected_node,):
            raise ValueError(f"Workflow cannot resume from {snapshot.next or ('END',)}")
        if int(values.get("expected_run_version", -1)) != run.state_version:
            raise ValueError("LangGraph checkpoint is stale for the current Run version")

    async def _assert_accessible_checkpoint(
        self,
        config: dict[str, Any],
        *,
        run_id: str,
        tenant_id: str,
        user_id: str,
    ):
        run = self.engine.get(run_id, tenant_id=tenant_id, user_id=user_id)
        snapshot = await self.graph.aget_state(config)
        values = snapshot.values
        if (
            not values
            or values.get("run_id") != run_id
            or values.get("tenant_id") != tenant_id
            or values.get("user_id") != user_id
        ):
            raise ValueError("Run has no accessible LangGraph checkpoint")
        return run, snapshot

    def _response(self, output: dict[str, Any]) -> RunResponse:
        run = self.engine.get(
            output["run_id"],
            tenant_id=output["tenant_id"],
            user_id=output["user_id"],
        )
        return RunResponse(
            run=run,
            assistant_message=output["assistant_message"],
            suggested_replies=output.get("suggested_replies", []),
        )

    @staticmethod
    def _response_update(response: RunResponse, *, node: str) -> dict[str, Any]:
        return {
            "message": None,
            "action_token": None,
            "expected_run_version": response.run.state_version,
            "run_state": response.run.state.value,
            "assistant_message": response.assistant_message,
            "suggested_replies": response.suggested_replies,
            "wait_reason": None,
            "last_node": node,
        }

    @staticmethod
    def _run_update(run, *, node: str) -> dict[str, Any]:
        return {
            "message": None,
            "action_token": None,
            "expected_run_version": run.state_version,
            "run_state": run.state.value,
            "wait_reason": None,
            "last_node": node,
        }

    def _inject_fault(self, node_name: str, state: ProcurementGraphState) -> None:
        if self._fault_injector is not None:
            self._fault_injector(node_name, state)

    def _lock(self, run_id: str) -> asyncio.Lock:
        return self._locks.setdefault(run_id, asyncio.Lock())

    @staticmethod
    def _config(tenant_id: str, run_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": f"{tenant_id}:{run_id}"}}


class LegacyProcurementWorkflow:
    backend = "legacy"
    checkpoint_backend = "none"

    def __init__(self, engine: ProcurementEngine) -> None:
        self.engine = engine

    async def start_run(self, **kwargs) -> RunResponse:
        return await self.engine.start(**kwargs)

    async def continue_run(self, **kwargs) -> RunResponse:
        return await self.engine.continue_run(**kwargs)

    async def confirm_run(self, **kwargs) -> RunResponse:
        return await self.engine.confirm(**kwargs)

    async def resume_pending(self, **kwargs) -> RunResponse:
        raise ValueError("Legacy workflow has no durable pending node")

    async def status(self, **kwargs) -> WorkflowStatus:
        run = self.engine.get(
            kwargs["run_id"],
            tenant_id=kwargs["tenant_id"],
            user_id=kwargs["user_id"],
        )
        return WorkflowStatus(
            backend=self.backend,
            checkpoint_backend=self.checkpoint_backend,
            run_id=run.run_id,
            run_state=run.state,
            expected_run_version=run.state_version,
            waiting_at=None,
            wait_reason=None,
            last_node=None,
        )


class ProcurementWorkflowRuntime:
    """Owns the checkpointer lifecycle and exposes one workflow interface to FastAPI."""

    def __init__(self, engine: ProcurementEngine, settings: Settings) -> None:
        self.engine = engine
        self.settings = settings
        self._open_lock = asyncio.Lock()
        self._postgres_context: AbstractAsyncContextManager | None = None
        self.workflow: LangGraphProcurementWorkflow | LegacyProcurementWorkflow | None = None
        if settings.workflow_backend == "legacy":
            self.checkpoint_backend = "none"
            self.workflow = LegacyProcurementWorkflow(engine)
            return
        if settings.workflow_backend != "langgraph":
            raise RuntimeError("Unsupported PROCURE_WORKFLOW_BACKEND. Use langgraph or legacy.")
        requested = settings.workflow_checkpoint_backend
        self.checkpoint_backend = (
            "postgres" if requested == "auto" and settings.repository_backend == "postgres" else requested
        )
        if self.checkpoint_backend == "auto":
            self.checkpoint_backend = "memory"
        if self.checkpoint_backend == "memory":
            self.workflow = LangGraphProcurementWorkflow(
                engine,
                InMemorySaver(),
                checkpoint_backend="memory",
            )
        elif self.checkpoint_backend != "postgres":
            raise RuntimeError(
                "Unsupported PROCURE_WORKFLOW_CHECKPOINT_BACKEND. Use auto, memory or postgres."
            )

    async def open(self) -> None:
        if self.workflow is not None:
            return
        async with self._open_lock:
            if self.workflow is not None:
                return
            if not self.settings.database_url:
                raise RuntimeError("PostgreSQL workflow checkpoints require PROCURE_DATABASE_URL")
            connection_url = self.settings.database_url.replace(
                "postgresql+psycopg://", "postgresql://", 1
            )
            context = AsyncPostgresSaver.from_conn_string(connection_url)
            checkpointer = await context.__aenter__()
            try:
                await checkpointer.setup()
            except Exception:
                await context.__aexit__(None, None, None)
                raise
            self._postgres_context = context
            self.workflow = LangGraphProcurementWorkflow(
                self.engine,
                checkpointer,
                checkpoint_backend="postgres",
            )

    async def close(self) -> None:
        if self._postgres_context is not None:
            await self._postgres_context.__aexit__(None, None, None)
            self._postgres_context = None
            self.workflow = None

    async def start_run(self, **kwargs) -> RunResponse:
        await self.open()
        assert self.workflow is not None
        return await self.workflow.start_run(**kwargs)

    async def continue_run(self, **kwargs) -> RunResponse:
        await self.open()
        assert self.workflow is not None
        return await self.workflow.continue_run(**kwargs)

    async def confirm_run(self, **kwargs) -> RunResponse:
        await self.open()
        assert self.workflow is not None
        return await self.workflow.confirm_run(**kwargs)

    async def resume_pending(self, **kwargs) -> RunResponse:
        await self.open()
        assert self.workflow is not None
        return await self.workflow.resume_pending(**kwargs)

    async def status(self, **kwargs) -> WorkflowStatus:
        await self.open()
        assert self.workflow is not None
        return await self.workflow.status(**kwargs)

    @property
    def description(self) -> dict[str, str]:
        return {
            "backend": self.settings.workflow_backend,
            "checkpoint_backend": self.checkpoint_backend,
            "manager": "deterministic LangGraph StateGraph",
        }
