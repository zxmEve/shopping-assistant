from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

from procure_pilot.domain.models import (
    BudgetResource,
    ExecutionBudget,
    ProcurementRun,
    RiskLevel,
    RunState,
    StateTransitionEvent,
    TraceEvent,
    utc_now,
)

T = TypeVar("T")


class RunNotFoundError(KeyError):
    pass


class HarnessBudgetExceeded(RuntimeError):
    def __init__(self, resource: BudgetResource, used: int, limit: int):
        self.resource = resource
        self.used = used
        self.limit = limit
        super().__init__(f"Execution budget exceeded: {resource} ({used}/{limit})")


class ToolTimeoutError(TimeoutError):
    def __init__(self, tool_name: str, timeout_ms: int):
        self.tool_name = tool_name
        self.timeout_ms = timeout_ms
        super().__init__(f"Tool timed out: {tool_name} after {timeout_ms}ms")


class CircuitOpenError(RuntimeError):
    def __init__(self, tool_name: str, provider: str | None):
        self.tool_name = tool_name
        self.provider = provider
        target = f"{tool_name}:{provider}" if provider else tool_name
        super().__init__(f"Circuit is open: {target}")


@dataclass(frozen=True)
class ToolPolicy:
    name: str
    risk: RiskLevel
    allowed_roles: frozenset[str]
    description: str
    timeout_ms: int = 2_000
    max_attempts: int = 3
    backoff_ms: int = 10
    circuit_failure_threshold: int = 3
    circuit_reset_ms: int = 30_000
    retryable_error_codes: frozenset[str] = frozenset(
        {"HTTP_429", "HTTP_500", "HTTP_502", "HTTP_503", "TIMEOUT"}
    )


@dataclass
class CircuitState:
    consecutive_failures: int = 0
    opened_at_ms: float | None = None


DEFAULT_TOOL_POLICIES = (
    ToolPolicy(
        name="search_catalog",
        risk=RiskLevel.READ,
        allowed_roles=frozenset({"EMPLOYEE", "PROCUREMENT_SPECIALIST"}),
        description="Search approved catalog providers.",
    ),
    ToolPolicy(
        name="get_budget",
        risk=RiskLevel.READ,
        allowed_roles=frozenset({"EMPLOYEE", "PROCUREMENT_SPECIALIST"}),
        description="Read the requester's cost-center budget snapshot.",
    ),
    ToolPolicy(
        name="evaluate_policy",
        risk=RiskLevel.READ,
        allowed_roles=frozenset({"EMPLOYEE", "PROCUREMENT_SPECIALIST"}),
        description="Evaluate deterministic procurement and vendor policy.",
    ),
    ToolPolicy(
        name="revalidate_quote",
        risk=RiskLevel.READ,
        allowed_roles=frozenset({"EMPLOYEE", "PROCUREMENT_SPECIALIST"}),
        description="Refresh a selected offer before a business write.",
    ),
    ToolPolicy(
        name="create_requisition_draft",
        risk=RiskLevel.BUSINESS_WRITE,
        allowed_roles=frozenset({"EMPLOYEE", "PROCUREMENT_SPECIALIST"}),
        description="Create an unsubmitted requisition draft after confirmation.",
    ),
)


class ToolRegistry:
    def __init__(
        self, policies: tuple[ToolPolicy, ...] = DEFAULT_TOOL_POLICIES, *, version: str = "v1"
    ) -> None:
        self.version = version
        self._policies = {policy.name: policy for policy in policies}
        if len(self._policies) != len(policies):
            raise ValueError("Tool policies must have unique names")

    def get(self, tool_name: str) -> ToolPolicy:
        try:
            return self._policies[tool_name]
        except KeyError as exc:
            raise PermissionError(f"Tool is not registered: {tool_name}") from exc

    def available(self, roles: tuple[str, ...]) -> list[ToolPolicy]:
        role_set = set(roles)
        return sorted(
            (
                policy
                for policy in self._policies.values()
                if role_set.intersection(policy.allowed_roles)
            ),
            key=lambda policy: policy.name,
        )


class RunRepository:
    """Small JSON repository with atomic replacement; replaceable through this port."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._runs: dict[str, ProcurementRun] = {}
        if path and path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            self._runs = {
                key: ProcurementRun.model_validate(value) for key, value in payload.items()
            }

    def save(self, run: ProcurementRun) -> ProcurementRun:
        with self._lock:
            run.updated_at = utc_now()
            self._runs[run.run_id] = run.model_copy(deep=True)
            self._flush()
            return run

    def get(self, run_id: str) -> ProcurementRun:
        with self._lock:
            if run_id not in self._runs:
                raise RunNotFoundError(run_id)
            return self._runs[run_id].model_copy(deep=True)

    def list(self) -> list[ProcurementRun]:
        with self._lock:
            return [item.model_copy(deep=True) for item in self._runs.values()]

    def _flush(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {key: value.model_dump(mode="json") for key, value in self._runs.items()},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary.replace(self.path)


class AgentHarness:
    CREATE_DRAFT_ACTION = "CREATE_REQUISITION_DRAFT"

    def __init__(
        self,
        *,
        secret: str,
        max_steps: int = 20,
        max_agent_turns: int = 12,
        max_tool_calls: int = 12,
        max_provider_calls: int = 12,
        max_retries: int = 4,
        max_input_tokens: int = 30_000,
        max_output_tokens: int = 10_000,
        max_cost_micro_usd: int = 100_000,
        max_wall_time_ms: int = 25_000,
        max_parallel_provider_calls: int = 4,
        tool_registry: ToolRegistry | None = None,
    ) -> None:
        self.secret = secret.encode("utf-8")
        self.max_steps = max_steps
        self.max_tool_calls = max_tool_calls
        self.tool_registry = tool_registry or ToolRegistry()
        self.execution_budget = ExecutionBudget(
            max_state_transitions=max_steps,
            max_agent_turns=max_agent_turns,
            max_tool_calls=max_tool_calls,
            max_provider_calls=max_provider_calls,
            max_retries=max_retries,
            max_input_tokens=max_input_tokens,
            max_output_tokens=max_output_tokens,
            max_cost_micro_usd=max_cost_micro_usd,
            max_wall_time_ms=max_wall_time_ms,
            max_parallel_provider_calls=max_parallel_provider_calls,
        )
        self._provider_semaphore = asyncio.Semaphore(max_parallel_provider_calls)
        self._circuits: dict[tuple[str, str | None], CircuitState] = {}

    def initialize_run(self, run: ProcurementRun) -> None:
        """Freeze the configured limits into the run so later inspection is self-contained."""
        run.execution_budget = self.execution_budget.model_copy(deep=True)
        self.assert_within_wall_time(run)

    def consume(
        self,
        run: ProcurementRun,
        resource: BudgetResource,
        amount: int = 1,
    ) -> None:
        if amount < 0:
            raise ValueError("Budget consumption cannot be negative")
        self.assert_budget_available(run, resource, amount)
        usage_field = resource.value
        used = int(getattr(run.execution_usage, usage_field))
        setattr(run.execution_usage, usage_field, used + amount)

    def assert_budget_available(
        self,
        run: ProcurementRun,
        resource: BudgetResource,
        amount: int = 1,
    ) -> None:
        if amount < 0:
            raise ValueError("Budget capacity cannot be negative")
        self.assert_within_wall_time(run)
        usage_field = resource.value
        limit_field = f"max_{usage_field}"
        used = int(getattr(run.execution_usage, usage_field))
        limit = int(getattr(run.execution_budget, limit_field))
        if used + amount > limit:
            self._record_budget_stop(run, resource, used, limit)
            raise HarnessBudgetExceeded(resource, used, limit)

    def record_model_usage(
        self,
        run: ProcurementRun,
        *,
        input_tokens: int,
        output_tokens: int,
        cost_micro_usd: int,
    ) -> None:
        """Apply measured model usage; callers must not pass estimates as observations."""
        self.consume(run, BudgetResource.INPUT_TOKENS, input_tokens)
        self.consume(run, BudgetResource.OUTPUT_TOKENS, output_tokens)
        self.consume(run, BudgetResource.COST_MICRO_USD, cost_micro_usd)

    def assert_within_wall_time(self, run: ProcurementRun) -> None:
        elapsed = run.execution_usage.wall_time_ms
        limit = run.execution_budget.max_wall_time_ms
        if elapsed >= limit:
            self._record_budget_stop(
                run,
                BudgetResource.WALL_TIME_MS,
                elapsed,
                limit,
            )
            raise HarnessBudgetExceeded(BudgetResource.WALL_TIME_MS, elapsed, limit)

    async def execute_agent_turn(
        self,
        run: ProcurementRun,
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        self.consume(run, BudgetResource.AGENT_TURNS)
        return await self._execute_with_wall_time(run, operation)

    def transition(
        self,
        run: ProcurementRun,
        to_state: RunState,
        *,
        reason: str,
        actor: str = "Procurement Orchestrator",
    ) -> StateTransitionEvent:
        allowed = ALLOWED_STATE_TRANSITIONS.get(run.state, frozenset())
        if to_state not in allowed:
            raise ValueError(f"Illegal state transition: {run.state} -> {to_state}")
        self.consume(run, BudgetResource.STATE_TRANSITIONS)
        previous = run.state
        run.state_version += 1
        run.state = to_state
        event = StateTransitionEvent(
            version=run.state_version,
            from_state=previous,
            to_state=to_state,
            reason=reason,
            actor=actor,
        )
        run.state_events.append(event)
        self.trace(
            run,
            actor=actor,
            kind="STATE",
            name="state.transition",
            detail={
                "from": previous,
                "to": to_state,
                "reason": reason,
                "state_version": run.state_version,
            },
        )
        return event

    async def execute_tool(
        self,
        run: ProcurementRun,
        *,
        tool_name: str,
        roles: tuple[str, ...],
        operation: Callable[[], Awaitable[T]],
        confirmed: bool = False,
        provider: str | None = None,
    ) -> T:
        policy = self.begin_tool_call(
            run,
            tool_name=tool_name,
            roles=roles,
            confirmed=confirmed,
        )
        circuit_key = (tool_name, provider)
        circuit = self._circuits.setdefault(circuit_key, CircuitState())
        self._assert_circuit_available(run, policy, circuit_key, circuit)

        for attempt in range(1, policy.max_attempts + 1):
            if provider is not None:
                self.consume(run, BudgetResource.PROVIDER_CALLS)
            started = time.perf_counter()
            remaining_ms = self._remaining_wall_time_ms(run)
            effective_timeout_ms = min(policy.timeout_ms, remaining_ms)
            try:
                if provider is None:
                    result = await asyncio.wait_for(
                        operation(), timeout=effective_timeout_ms / 1000
                    )
                else:
                    async with self._provider_semaphore:
                        result = await asyncio.wait_for(
                            operation(), timeout=effective_timeout_ms / 1000
                        )
            except Exception as exc:
                duration_ms = int((time.perf_counter() - started) * 1000)
                self._add_wall_time(run, duration_ms)
                if isinstance(exc, TimeoutError) and effective_timeout_ms == remaining_ms:
                    run.execution_usage.wall_time_ms = run.execution_budget.max_wall_time_ms
                    self._record_budget_stop(
                        run,
                        BudgetResource.WALL_TIME_MS,
                        run.execution_usage.wall_time_ms,
                        run.execution_budget.max_wall_time_ms,
                    )
                    raise HarnessBudgetExceeded(
                        BudgetResource.WALL_TIME_MS,
                        run.execution_usage.wall_time_ms,
                        run.execution_budget.max_wall_time_ms,
                    ) from exc
                code = (
                    "TIMEOUT"
                    if isinstance(exc, TimeoutError)
                    else str(getattr(exc, "code", type(exc).__name__))
                )
                circuit.consecutive_failures += 1
                if circuit.consecutive_failures >= policy.circuit_failure_threshold:
                    circuit.opened_at_ms = time.monotonic() * 1000
                self.trace(
                    run,
                    actor="Agent Harness",
                    kind="HARNESS",
                    name="tool.attempt",
                    detail={
                        "tool": tool_name,
                        "provider": provider,
                        "attempt": attempt,
                        "error_code": code,
                        "circuit": "OPEN" if circuit.opened_at_ms is not None else "CLOSED",
                    },
                    status="FAILED",
                    duration_ms=duration_ms,
                )
                retryable = code in policy.retryable_error_codes
                if attempt >= policy.max_attempts or not retryable:
                    if isinstance(exc, TimeoutError):
                        raise ToolTimeoutError(tool_name, policy.timeout_ms) from exc
                    raise
                self.consume(run, BudgetResource.RETRIES)
                self.trace(
                    run,
                    actor="Agent Harness",
                    kind="HARNESS",
                    name="tool.retry_scheduled",
                    detail={
                        "tool": tool_name,
                        "provider": provider,
                        "next_attempt": attempt + 1,
                        "backoff_ms": policy.backoff_ms,
                    },
                    status="RETRYING",
                )
                if policy.backoff_ms:
                    remaining_ms = self._remaining_wall_time_ms(run)
                    if policy.backoff_ms >= remaining_ms:
                        self._add_wall_time(run, remaining_ms)
                        self._record_budget_stop(
                            run,
                            BudgetResource.WALL_TIME_MS,
                            run.execution_usage.wall_time_ms,
                            run.execution_budget.max_wall_time_ms,
                        )
                        raise HarnessBudgetExceeded(
                            BudgetResource.WALL_TIME_MS,
                            run.execution_usage.wall_time_ms,
                            run.execution_budget.max_wall_time_ms,
                        ) from exc
                    await asyncio.sleep(policy.backoff_ms / 1000)
                    self._add_wall_time(run, policy.backoff_ms)
            else:
                duration_ms = int((time.perf_counter() - started) * 1000)
                self._add_wall_time(run, duration_ms)
                circuit.consecutive_failures = 0
                circuit.opened_at_ms = None
                self.trace(
                    run,
                    actor="Agent Harness",
                    kind="HARNESS",
                    name="tool.attempt",
                    detail={
                        "tool": tool_name,
                        "provider": provider,
                        "attempt": attempt,
                    },
                    duration_ms=duration_ms,
                )
                return result
        raise AssertionError("Tool attempt loop exhausted unexpectedly")

    def begin_tool_call(
        self,
        run: ProcurementRun,
        *,
        tool_name: str,
        roles: tuple[str, ...],
        confirmed: bool,
    ) -> ToolPolicy:
        """Authorize and reserve one logical tool call before its side effect starts."""
        policy = self.assert_tool_allowed(
            tool_name=tool_name,
            roles=roles,
            confirmed=confirmed,
        )
        self.consume(run, BudgetResource.TOOL_CALLS)
        return policy

    def trace(
        self,
        run: ProcurementRun,
        *,
        actor: str,
        kind: str,
        name: str,
        detail: dict[str, Any] | None = None,
        status: str = "OK",
        duration_ms: int | None = None,
    ) -> None:
        run.trace.append(
            TraceEvent(
                actor=actor,
                kind=kind,
                name=name,
                detail=detail or {},
                status=status,
                duration_ms=duration_ms,
            )
        )

    def assert_tool_allowed(
        self,
        *,
        tool_name: str,
        roles: tuple[str, ...],
        confirmed: bool,
    ) -> ToolPolicy:
        policy = self.tool_registry.get(tool_name)
        if not set(roles).intersection(policy.allowed_roles):
            raise PermissionError(f"Current role is not allowed to use tool: {tool_name}")
        if policy.risk == RiskLevel.BUSINESS_WRITE and not confirmed:
            raise PermissionError("BUSINESS_WRITE requires explicit confirmation")
        return policy

    def available_tools(self, roles: tuple[str, ...]) -> list[dict[str, Any]]:
        return [
            {
                "name": policy.name,
                "risk": policy.risk,
                "description": policy.description,
                "registry_version": self.tool_registry.version,
                "timeout_ms": policy.timeout_ms,
                "max_attempts": policy.max_attempts,
                "circuit_failure_threshold": policy.circuit_failure_threshold,
            }
            for policy in self.tool_registry.available(roles)
        ]

    def issue_action_token(
        self,
        run: ProcurementRun,
        *,
        action: str = CREATE_DRAFT_ACTION,
        expires_in: timedelta = timedelta(minutes=15),
    ) -> str:
        if run.quote_snapshot is None:
            raise ValueError("Cannot issue an action token without a quote snapshot")
        payload = {
            "run_id": run.run_id,
            "tenant_id": run.tenant_id,
            "user_id": run.user_id,
            "requirement_version": run.requirement.version,
            "action": action,
            "quote_fingerprint": run.quote_snapshot.fingerprint,
            "expires_at": int((datetime.now(UTC) + expires_in).timestamp()),
        }
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        signature = hmac.new(self.secret, raw, hashlib.sha256).digest()
        return f"{base64.urlsafe_b64encode(raw).decode().rstrip('=')}.{base64.urlsafe_b64encode(signature).decode().rstrip('=')}"

    def verify_action_token(
        self,
        token: str,
        run: ProcurementRun,
        *,
        action: str = CREATE_DRAFT_ACTION,
    ) -> dict[str, Any]:
        if run.quote_snapshot is None:
            raise PermissionError("Run does not have a confirmed quote snapshot")
        try:
            payload_part, signature_part = token.split(".", 1)
            raw = base64.urlsafe_b64decode(payload_part + "=" * (-len(payload_part) % 4))
            signature = base64.urlsafe_b64decode(signature_part + "=" * (-len(signature_part) % 4))
        except (ValueError, TypeError) as exc:
            raise PermissionError("Invalid action token") from exc
        expected = hmac.new(self.secret, raw, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise PermissionError("Invalid action token signature")
        payload = json.loads(raw)
        expected_fields = {
            "run_id": run.run_id,
            "tenant_id": run.tenant_id,
            "user_id": run.user_id,
            "requirement_version": run.requirement.version,
            "action": action,
            "quote_fingerprint": run.quote_snapshot.fingerprint,
        }
        if any(payload.get(key) != value for key, value in expected_fields.items()):
            raise PermissionError("Action token does not match the current run")
        if int(payload["expires_at"]) < int(datetime.now(UTC).timestamp()):
            raise PermissionError("Action token expired")
        return payload

    def _assert_circuit_available(
        self,
        run: ProcurementRun,
        policy: ToolPolicy,
        key: tuple[str, str | None],
        circuit: CircuitState,
    ) -> None:
        if circuit.opened_at_ms is None:
            return
        elapsed = time.monotonic() * 1000 - circuit.opened_at_ms
        if elapsed >= policy.circuit_reset_ms:
            circuit.consecutive_failures = 0
            circuit.opened_at_ms = None
            return
        self.trace(
            run,
            actor="Agent Harness",
            kind="HARNESS",
            name="tool.circuit_open",
            detail={"tool": key[0], "provider": key[1], "reset_after_ms": policy.circuit_reset_ms},
            status="BLOCKED",
        )
        raise CircuitOpenError(*key)

    def _record_budget_stop(
        self,
        run: ProcurementRun,
        resource: BudgetResource,
        used: int,
        limit: int,
    ) -> None:
        name = resource.value
        if (
            run.trace
            and run.trace[-1].name == "budget.exceeded"
            and run.trace[-1].detail.get("resource") == name
        ):
            return
        self.trace(
            run,
            actor="Agent Harness",
            kind="HARNESS",
            name="budget.exceeded",
            detail={
                "resource": name,
                "used": used,
                "limit": limit,
            },
            status="STOPPED",
        )

    def _remaining_wall_time_ms(self, run: ProcurementRun) -> int:
        self.assert_within_wall_time(run)
        return run.execution_budget.max_wall_time_ms - run.execution_usage.wall_time_ms

    def _add_wall_time(self, run: ProcurementRun, amount_ms: int) -> None:
        run.execution_usage.wall_time_ms = min(
            run.execution_budget.max_wall_time_ms,
            run.execution_usage.wall_time_ms + max(0, amount_ms),
        )

    async def _execute_with_wall_time(
        self,
        run: ProcurementRun,
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        remaining_ms = self._remaining_wall_time_ms(run)
        started = time.perf_counter()
        try:
            result = await asyncio.wait_for(operation(), timeout=remaining_ms / 1000)
        except TimeoutError as exc:
            self._add_wall_time(run, int((time.perf_counter() - started) * 1000))
            run.execution_usage.wall_time_ms = run.execution_budget.max_wall_time_ms
            self._record_budget_stop(
                run,
                BudgetResource.WALL_TIME_MS,
                run.execution_usage.wall_time_ms,
                run.execution_budget.max_wall_time_ms,
            )
            raise HarnessBudgetExceeded(
                BudgetResource.WALL_TIME_MS,
                run.execution_usage.wall_time_ms,
                run.execution_budget.max_wall_time_ms,
            ) from exc
        self._add_wall_time(run, int((time.perf_counter() - started) * 1000))
        return result


ALLOWED_STATE_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.COLLECTING: frozenset({RunState.WAITING_USER, RunState.SOURCING, RunState.FAILED}),
    RunState.WAITING_USER: frozenset({RunState.COLLECTING, RunState.FAILED}),
    RunState.SOURCING: frozenset({RunState.REVIEWING, RunState.FAILED}),
    RunState.REVIEWING: frozenset({RunState.WAITING_CONFIRMATION, RunState.FAILED}),
    RunState.WAITING_CONFIRMATION: frozenset({RunState.REVALIDATING, RunState.FAILED}),
    RunState.REVALIDATING: frozenset(
        {RunState.WAITING_CONFIRMATION, RunState.DRAFT_CREATING, RunState.FAILED}
    ),
    RunState.DRAFT_CREATING: frozenset({RunState.DRAFT_CREATED, RunState.FAILED}),
    RunState.DRAFT_CREATED: frozenset(),
    RunState.FAILED: frozenset(),
}
