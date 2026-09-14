from __future__ import annotations

import asyncio
import hashlib
import json
import re
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

from procure_pilot.agents.contracts import AgentKind
from procure_pilot.agents.runtime import AgentPlanner, DeterministicAgentPlanner
from procure_pilot.agents.tools import AgentCandidateProposal, AgentRuntimeContext
from procure_pilot.domain.execution import AgentExecution, ExecutionStatus
from procure_pilot.domain.knowledge import PolicyKnowledgeReviewRequired
from procure_pilot.domain.models import (
    BudgetResource,
    Candidate,
    CatalogSearchResult,
    ClarificationDecision,
    CostBreakdown,
    ExecutionUsage,
    MatchDecisionType,
    Message,
    Money,
    Offer,
    PolicyDecision,
    ProcurementDraft,
    ProcurementRequirement,
    ProcurementRun,
    QuoteSnapshot,
    Recommendation,
    RevalidationChange,
    RunResponse,
    RunState,
    SourcingRankingDecision,
    ToolBudgetPool,
    TraceEvent,
    ValidatedCandidateSet,
    ValidatedCandidateView,
)
from procure_pilot.domain.requirements import RequirementSpec, RequirementStatus
from procure_pilot.fixtures.catalog import PROVIDERS, FixtureCatalogAdapter, ProviderError
from procure_pilot.fixtures.enterprise import FixtureEnterpriseGateway
from procure_pilot.gateway.model_gateway import ModelGateway
from procure_pilot.gateway.tool_gateway import (
    GatewayExecutionContext,
    ToolGateway,
    build_default_tool_registry,
)
from procure_pilot.harness.runtime import (
    AgentHarness,
    CircuitOpenError,
    HarnessBudgetExceeded,
    RunNotFoundError,
    RunRepositoryPort,
    ToolTimeoutError,
)
from procure_pilot.persistence.budget_repository import InMemoryBudgetRepository
from procure_pilot.persistence.execution_repository import (
    ExecutionRepositoryPort,
    InMemoryExecutionRepository,
)
from procure_pilot.services.budget import (
    BudgetLimitProvider,
    BudgetService,
    InMemoryBudgetService,
    StaticBudgetLimitProvider,
)
from procure_pilot.services.context import (
    DEFAULT_CONTEXT_POLICY,
    ContextAssembler,
    ContextPolicy,
    MemoryReader,
)
from procure_pilot.services.costing import calculate_landed_cost
from procure_pilot.services.knowledge import PolicyKnowledgeService
from procure_pilot.services.memory_registry import MemoryRegistry
from procure_pilot.services.requirement_validation import RequirementSpecService

QUESTION_LABELS = {
    "quantity": "需要采购多少台？",
    "unit_budget": "单台预算是多少，币种是什么？",
    "budget_tax_included": "单台预算是含税还是未税？",
    "delivery_city": "具体交付城市是哪里？",
    "minimum_ram_gb": "最低内存要求是多少？开发机建议明确为 32GB 或 64GB。",
    "minimum_storage_gb": "最低存储容量要求是多少？",
}

QUESTION_INFORMATION_VALUE = {
    "unit_budget": 1.00,
    "quantity": 0.95,
    "minimum_ram_gb": 0.90,
    "minimum_storage_gb": 0.88,
    "delivery_city": 0.85,
    "budget_tax_included": 0.80,
}

MAX_CLARIFICATION_ROUNDS = 3
class AgentStageResult(BaseModel):
    execution_id: str
    agent_kind: Literal["REQUIREMENT", "SOURCING"]
    proposed_candidates: list[AgentCandidateProposal] = Field(default_factory=list)
    decision_rationale: str = ""
    catalog_results: dict[str, CatalogSearchResult] = Field(default_factory=dict)
    catalog_failures: dict[str, str] = Field(default_factory=dict)
    execution_usage: ExecutionUsage | None = None
    trace_events: list[TraceEvent] = Field(default_factory=list)
    requirement_spec: RequirementSpec | None = None


class ConfirmationStageResult(BaseModel):
    outcome: Literal[
        "READY_FOR_QUOTE",
        "READY_FOR_POLICY",
        "READY_FOR_WRITE",
        "WAITING_CONFIRMATION",
        "DRAFT_CREATED",
        "STOPPED",
    ]
    assistant_message: str = ""
    refreshed_offer: Offer | None = None
    candidate: Candidate | None = None


class ProcurementEngine:
    def __init__(
        self,
        *,
        catalog: FixtureCatalogAdapter,
        enterprise: FixtureEnterpriseGateway,
        repository: RunRepositoryPort,
        harness: AgentHarness,
        planner: AgentPlanner | None = None,
        memory_repository: MemoryReader | None = None,
        context_policy: ContextPolicy = DEFAULT_CONTEXT_POLICY,
        memory_registry: MemoryRegistry | None = None,
        knowledge_service: PolicyKnowledgeService | None = None,
        execution_repository: ExecutionRepositoryPort | None = None,
        budget_service: BudgetService | None = None,
        budget_limit_provider: BudgetLimitProvider | None = None,
        tool_gateway: ToolGateway | None = None,
        provider_costs_micro_usd: dict[str, int] | None = None,
        model_max_reservation_micro_usd: int = 100_000,
        budget_reservation_ttl_seconds: int = 300,
        max_agent_utility_regret: float = 0.05,
    ) -> None:
        self.catalog = catalog
        self.enterprise = enterprise
        self.repository = repository
        self.harness = harness
        self.planner = planner or DeterministicAgentPlanner()
        self.memory_repository = memory_repository
        self.context_policy = context_policy
        self.memory_registry = memory_registry
        self.knowledge_service = knowledge_service
        self.execution_repository = execution_repository or InMemoryExecutionRepository()
        self.budget_limit_provider = budget_limit_provider or StaticBudgetLimitProvider(
            platform_daily=10_000_000_000,
            platform_monthly=100_000_000_000,
            tenant_monthly=50_000_000_000,
            user_daily=5_000_000_000,
        )
        self.budget_service = budget_service or InMemoryBudgetService(
            InMemoryBudgetRepository(), limit_provider=self.budget_limit_provider
        )
        self.tool_gateway = tool_gateway or ToolGateway(
            registry=build_default_tool_registry(
                catalog=catalog,
                enterprise=enterprise,
                provider_costs_micro_usd=provider_costs_micro_usd,
            ),
            harness=harness,
            execution_repository=self.execution_repository,
            budget_service=self.budget_service,
            budget_limit_provider=self.budget_limit_provider,
            reservation_ttl_seconds=budget_reservation_ttl_seconds,
        )
        self.model_gateway = ModelGateway(
            budget_service=self.budget_service,
            budget_limit_provider=self.budget_limit_provider,
            max_reservation_micro_usd=model_max_reservation_micro_usd,
            reservation_ttl_seconds=budget_reservation_ttl_seconds,
        )
        self.context_assembler = ContextAssembler(
            memory_repository,
            policy=context_policy,
            memory_registry=memory_registry,
        )
        self._run_locks: dict[str, asyncio.Lock] = {}
        self.requirement_specs = RequirementSpecService()
        if not 0 <= max_agent_utility_regret <= 1:
            raise ValueError("max_agent_utility_regret must be between 0 and 1")
        self.max_agent_utility_regret = max_agent_utility_regret

    def initialize_workflow_run(
        self,
        *,
        run_id: str,
        tenant_id: str,
        user_id: str,
        message: str,
        scenario: str | None = None,
    ) -> ProcurementRun:
        """Persist user input before an expensive Agent node starts."""
        self.enterprise.identity(tenant_id, user_id)
        try:
            existing = self.repository.get(run_id)
        except RunNotFoundError:
            existing = None
        if existing is not None:
            self._assert_owner(existing, tenant_id, user_id)
            first_user_message = next(
                (item.content for item in existing.messages if item.role == "user"), None
            )
            if first_user_message != message:
                raise ValueError("Existing Run input does not match the workflow checkpoint")
            return existing
        run = ProcurementRun(
            run_id=run_id,
            tenant_id=tenant_id,
            user_id=user_id,
            scenario=scenario,
        )
        self.harness.initialize_run(run)
        self.harness.trace(run, actor="Application Runtime", kind="STATE", name="run.started")
        run.messages.append(Message(role="user", content=message))
        self._merge_requirement(run.requirement, message)
        if "按公司标准" in message or "按团队标准" in message:
            self._apply_authorized_defaults(run.requirement)
        self.repository.save(run)
        return run

    def accept_workflow_message(
        self,
        *,
        run_id: str,
        tenant_id: str,
        user_id: str,
        message: str,
        scenario: str | None = None,
    ) -> ProcurementRun:
        """Persist one clarification before an Agent node; duplicate state is rejected."""
        run = self.repository.get(run_id, tenant_id=tenant_id, user_id=user_id)
        self._assert_owner(run, tenant_id, user_id)
        if (
            run.state == RunState.COLLECTING
            and run.messages
            and run.messages[-1].role == "user"
            and run.messages[-1].content == message
        ):
            return run
        if run.state not in {RunState.WAITING_USER, RunState.COLLECTING}:
            raise ValueError(f"Run cannot accept clarification in state {run.state}")
        if run.state == RunState.WAITING_USER:
            self.harness.transition(run, RunState.COLLECTING, reason="user supplied clarification")
        run.messages.append(Message(role="user", content=message))
        run.requirement.version += 1
        if scenario is not None:
            run.scenario = scenario
        self._merge_requirement(run.requirement, message)
        if "按公司标准" in message or "按团队标准" in message:
            self._apply_authorized_defaults(run.requirement)
        self.repository.save(run)
        return run

    def required_agent_kind(self, run: ProcurementRun) -> AgentKind:
        if getattr(self.planner, "requires_requirement_stage", False):
            latest_user = next(
                (item for item in reversed(run.messages) if item.role == "user"), None
            )
            if latest_user is None or run.requirement_spec is None:
                return AgentKind.REQUIREMENT
            if run.requirement_spec.source_message_id != latest_user.message_id:
                return AgentKind.REQUIREMENT
            if run.requirement_spec.status == RequirementStatus.READY_FOR_SOURCING:
                return AgentKind.SOURCING
            return AgentKind.REQUIREMENT
        clarification = self._clarification(run.requirement, run.clarification_rounds)
        return AgentKind.SOURCING if clarification.ready_for_sourcing else AgentKind.REQUIREMENT

    async def execute_agent_node(
        self,
        *,
        run_id: str,
        tenant_id: str,
        user_id: str,
        expected_kind: AgentKind,
        node_name: str,
    ) -> AgentStageResult:
        run = self.repository.get(run_id, tenant_id=tenant_id, user_id=user_id)
        self._assert_owner(run, tenant_id, user_id)
        if run.state != RunState.COLLECTING:
            raise ValueError(f"Agent node cannot execute in state {run.state}")
        actual_kind = self.required_agent_kind(run)
        if actual_kind != expected_kind:
            raise ValueError(f"Agent node mismatch: expected {expected_kind}, got {actual_kind}")
        payload = {
            "run_id": run.run_id,
            "state_version": run.state_version,
            "requirement": run.requirement.model_dump(mode="json"),
            "messages": [item.model_dump(mode="json") for item in run.messages],
            "agent": expected_kind.value,
        }
        input_fingerprint = self._fingerprint(payload)
        idempotency_key = f"agent:{run.run_id}:{expected_kind.value}:{input_fingerprint}"
        existing = self.execution_repository.get_agent(idempotency_key)
        if existing is not None and existing.status == ExecutionStatus.SUCCEEDED:
            if existing.result_json is None:
                raise ValueError("Completed Agent execution is missing its result")
            result = AgentStageResult.model_validate(existing.result_json)
            if result.execution_usage is not None:
                run.execution_usage = result.execution_usage.model_copy(deep=True)
            known_event_ids = {event.event_id for event in run.trace}
            run.trace.extend(
                event.model_copy(deep=True)
                for event in result.trace_events
                if event.event_id not in known_event_ids
            )
            self.harness.trace(
                run,
                actor="Application Runtime",
                kind="AGENT",
                name="agent.execution_reused",
                detail={"execution_id": existing.execution_id, "node": node_name},
                status="RECOVERED",
            )
            self.repository.save(run)
            return result
        claimed = self.execution_repository.claim_agent(
            AgentExecution(
                tenant_id=run.tenant_id,
                user_id=run.user_id,
                run_id=run.run_id,
                node_name=node_name,
                agent_kind=expected_kind.value,
                task_id=f"{expected_kind.value.lower()}_{run.state_version}",
                state_version=run.state_version,
                input_fingerprint=input_fingerprint,
                idempotency_key=idempotency_key,
            )
        )
        try:
            trace_start = len(run.trace)
            context = await self._plan(run, expected_kind=expected_kind)
            result = AgentStageResult(
                execution_id=claimed.execution_id,
                agent_kind=expected_kind.value,
                proposed_candidates=context.proposed_candidates,
                decision_rationale=context.decision_rationale,
                catalog_results=context.catalog_results,
                catalog_failures=context.catalog_failures,
                execution_usage=run.execution_usage.model_copy(deep=True),
                trace_events=[event.model_copy(deep=True) for event in run.trace[trace_start:]],
                requirement_spec=context.requirement_spec,
            )
            self.execution_repository.complete_agent(
                claimed.execution_id,
                result.model_dump(mode="json"),
            )
            self.repository.save(run)
            return result
        except Exception as exc:
            self.execution_repository.fail_agent(
                claimed.execution_id,
                f"{type(exc).__name__}: {exc}",
            )
            raise

    async def complete_workflow_turn(
        self,
        *,
        run_id: str,
        tenant_id: str,
        user_id: str,
        agent_stage: AgentStageResult,
    ) -> RunResponse:
        run = self.repository.get(run_id, tenant_id=tenant_id, user_id=user_id)
        if run.state != RunState.COLLECTING:
            assistant_message = next(
                (item.content for item in reversed(run.messages) if item.role == "assistant"),
                run.error or "任务已恢复到持久化业务状态。",
            )
            return RunResponse(run=run, assistant_message=assistant_message)
        context = AgentRuntimeContext(
            run=run,
            catalog=self.catalog,
            enterprise=self.enterprise,
            harness=self.harness,
            roles=self.enterprise.identity(run.tenant_id, run.user_id).roles,
            tool_gateway=self.tool_gateway,
            model_gateway=self.model_gateway,
            context_assembler=self.context_assembler,
            proposed_candidates=agent_stage.proposed_candidates,
            decision_rationale=agent_stage.decision_rationale,
            catalog_failures=agent_stage.catalog_failures,
        )
        for result in agent_stage.catalog_results.values():
            context.remember_catalog_result(result)
        try:
            if (
                agent_stage.agent_kind == AgentKind.REQUIREMENT.value
                and agent_stage.requirement_spec is not None
            ):
                response = self._complete_requirement_stage(run, agent_stage)
                self.repository.save(run)
                return response
            response = await self._advance(
                run,
                scenario=run.scenario,
                agent_context=context,
            )
        except HarnessBudgetExceeded as exc:
            response = self._budget_stopped(run, exc)
        self.repository.save(run)
        return response

    def _complete_requirement_stage(
        self, run: ProcurementRun, agent_stage: AgentStageResult
    ) -> RunResponse:
        proposed = agent_stage.requirement_spec
        if proposed is None:
            raise ValueError("Requirement Agent did not return RequirementSpec")
        if proposed.run_id != run.run_id:
            raise ValueError("RequirementSpec belongs to a different Run")
        latest = next((item for item in reversed(run.messages) if item.role == "user"), None)
        if latest is None:
            raise ValueError("RequirementSpec has no source user message")
        if (
            run.requirement_spec
            and run.requirement_spec.spec_id == proposed.spec_id
            and run.requirement_spec.version == proposed.version
        ):
            validated = run.requirement_spec
        else:
            validated = self.requirement_specs.validate(
                proposed,
                message_id=latest.message_id,
                text=latest.content,
                base=run.requirement_spec,
            )
            baseline = self.enterprise.policy["developer_laptop_baseline"]
            validated = self.requirement_specs.apply_authorized_defaults(
                validated,
                minimum_ram_gb=int(baseline["minimum_ram_gb"]),
                minimum_storage_gb=1024,
            )
            run.requirement_spec = validated
            run.requirement_spec_history.append(validated.model_copy(deep=True))
            run.requirement = self.requirement_specs.to_procurement_requirement(validated)
        self.harness.trace(
            run,
            actor="Requirement Validation Service",
            kind="SERVICE",
            name="requirement_spec.validated",
            detail={
                "spec_id": validated.spec_id,
                "base_version": validated.base_version,
                "version": validated.version,
                "status": validated.status,
                "missing_fields": [item.field for item in validated.missing_fields],
                "conflicts": [item.model_dump(mode="json") for item in validated.conflicts],
                "delta_operations": len(validated.delta.operations),
            },
        )
        if validated.status == RequirementStatus.READY_FOR_SOURCING:
            return RunResponse(run=run, assistant_message="需求已校验，正在进入寻源。")
        if run.clarification_rounds >= MAX_CLARIFICATION_ROUNDS:
            self.harness.transition(run, RunState.FAILED, reason="clarification round limit reached")
            run.error = "达到最大追问轮数，需求仍存在缺失或冲突。"
            run.messages.append(Message(role="assistant", content=run.error))
            return RunResponse(run=run, assistant_message=run.error)
        run.clarification_rounds += 1
        self.harness.transition(run, RunState.WAITING_USER, reason="RequirementSpec needs clarification")
        question = validated.next_question.text if validated.next_question else "请补充必要采购条件。"
        message = f"为了避免错配，我还需要确认：{question}"
        run.messages.append(Message(role="assistant", content=message))
        return RunResponse(run=run, assistant_message=message)

    def prepare_confirmation_node(
        self,
        *,
        run_id: str,
        tenant_id: str,
        user_id: str,
        action_token: str,
    ) -> ConfirmationStageResult:
        run = self.repository.get(run_id, tenant_id=tenant_id, user_id=user_id)
        self._assert_owner(run, tenant_id, user_id)
        if run.draft:
            return ConfirmationStageResult(
                outcome="DRAFT_CREATED",
                assistant_message=f"采购草稿 {run.draft.draft_id} 已存在，未重复创建。",
            )
        if run.state == RunState.REVALIDATING:
            return ConfirmationStageResult(outcome="READY_FOR_QUOTE")
        if run.state != RunState.WAITING_CONFIRMATION or not run.recommendation:
            raise ValueError("Run is not waiting for confirmation")
        try:
            self.harness.verify_action_token(action_token, run)
            self.harness.assert_budget_available(run, BudgetResource.STATE_TRANSITIONS, 3)
            self.harness.reserve_commit_tool_calls(run, 3)
            self.harness.transition(
                run,
                RunState.REVALIDATING,
                reason="user confirmed the quoted action",
            )
        except HarnessBudgetExceeded as exc:
            self._restore_waiting_confirmation(
                run,
                reason="confirmation budget was unavailable or exhausted safely",
            )
            response = self._budget_stopped(run, exc)
            self.repository.save(run)
            return ConfirmationStageResult(
                outcome="STOPPED", assistant_message=response.assistant_message
            )
        self.repository.save(run)
        return ConfirmationStageResult(outcome="READY_FOR_QUOTE")

    async def revalidate_quote_node(
        self,
        *,
        run_id: str,
        tenant_id: str,
        user_id: str,
    ) -> ConfirmationStageResult:
        run = self.repository.get(run_id, tenant_id=tenant_id, user_id=user_id)
        if run.state != RunState.REVALIDATING or not run.recommendation:
            raise ValueError("Run is not ready for quote revalidation")
        employee = self.enterprise.identity(run.tenant_id, run.user_id)
        candidate = run.recommendation.best_overall
        arguments = {
            "provider": candidate.offer.provider,
            "offer_id": candidate.offer.offer_id,
            "quote_fingerprint": run.quote_snapshot.fingerprint if run.quote_snapshot else None,
        }
        fingerprint = self._fingerprint(arguments)
        key = f"tool:{run.run_id}:revalidate_quote:{fingerprint}"
        try:
            refreshed_offer = await self.tool_gateway.invoke(
                tool_name="revalidate_quote",
                arguments={
                    "provider": candidate.offer.provider,
                    "offer_id": candidate.offer.offer_id,
                },
                context=GatewayExecutionContext(
                    run=run,
                    roles=employee.roles,
                    node_name="revalidate_quote",
                    budget_pool=ToolBudgetPool.COMMIT,
                ),
                idempotency_key=key,
            )
        except (HarnessBudgetExceeded, ProviderError, ToolTimeoutError, CircuitOpenError) as exc:
            self._restore_waiting_confirmation(
                run,
                reason="quote revalidation tool failed safely",
            )
            message = "报价复核暂时失败，未创建草稿。请稍后重新确认。"
            if isinstance(exc, HarnessBudgetExceeded):
                message = self._budget_stopped(run, exc).assistant_message
            else:
                self.harness.trace(
                    run,
                    actor="Agent Harness",
                    kind="HARNESS",
                    name="revalidation.degraded",
                    detail={"error_code": getattr(exc, "code", type(exc).__name__)},
                    status="DEGRADED",
                )
                run.messages.append(Message(role="assistant", content=message))
            self.repository.save(run)
            return ConfirmationStageResult(
                outcome="WAITING_CONFIRMATION", assistant_message=message
            )
        refreshed_offer = Offer.model_validate(refreshed_offer)
        self.repository.save(run)
        return ConfirmationStageResult(
            outcome="READY_FOR_POLICY", refreshed_offer=refreshed_offer
        )

    async def revalidate_policy_node(
        self,
        *,
        run_id: str,
        tenant_id: str,
        user_id: str,
        refreshed_offer: Offer,
    ) -> ConfirmationStageResult:
        run = self.repository.get(run_id, tenant_id=tenant_id, user_id=user_id)
        if run.state != RunState.REVALIDATING or not run.recommendation:
            raise ValueError("Run is not ready for policy revalidation")
        employee = self.enterprise.identity(run.tenant_id, run.user_id)
        refreshed_cost = self._calculate_cost(refreshed_offer, run.requirement)
        arguments = {
            "offer": refreshed_offer.model_dump(mode="json"),
            "landed_cost": str(refreshed_cost.unconditional_total.amount),
            "policy_version": str(self.enterprise.policy["version"]),
        }
        fingerprint = self._fingerprint(arguments)
        key = f"tool:{run.run_id}:revalidate_policy:{fingerprint}"
        try:
            policy_payload = await self.tool_gateway.invoke(
                tool_name="evaluate_policy",
                arguments={
                    "provider": refreshed_offer.provider,
                    "offer_id": refreshed_offer.offer_id,
                },
                context=GatewayExecutionContext(
                    run=run,
                    roles=employee.roles,
                    node_name="revalidate_policy",
                    budget_pool=ToolBudgetPool.COMMIT,
                    runtime_data={
                        "offers": {
                            (refreshed_offer.provider, refreshed_offer.offer_id): refreshed_offer
                        }
                    },
                ),
                idempotency_key=key,
            )
            policy_payload = {
                **policy_payload,
                "policy_knowledge_refs": self._policy_knowledge_refs(run),
            }
        except (HarnessBudgetExceeded, PolicyKnowledgeReviewRequired) as exc:
            self._restore_waiting_confirmation(
                run,
                reason="policy revalidation could not complete safely",
            )
            message = (
                self._budget_stopped(run, exc).assistant_message
                if isinstance(exc, HarnessBudgetExceeded)
                else "制度条款缺失或版本不一致，未创建草稿，需要人工复核制度知识。"
            )
            if not isinstance(exc, HarnessBudgetExceeded):
                run.messages.append(Message(role="assistant", content=message))
            self.repository.save(run)
            return ConfirmationStageResult(
                outcome="WAITING_CONFIRMATION", assistant_message=message
            )
        decision = PolicyDecision(policy_payload["decision"])
        if decision == PolicyDecision.DENY:
            self._restore_waiting_confirmation(
                run,
                reason="policy revalidation denied the write safely",
            )
            self.repository.save(run)
            raise PermissionError("Policy revalidation denied draft creation")
        if run.requirement.unit_budget and (
            refreshed_offer.unit_price.amount > run.requirement.unit_budget.amount
        ):
            self._restore_waiting_confirmation(
                run,
                reason="revalidated quote exceeded the confirmed budget safely",
            )
            self.repository.save(run)
            raise PermissionError("Revalidated offer exceeds the confirmed unit budget")
        if run.quote_snapshot is None:
            self._restore_waiting_confirmation(
                run,
                reason="missing quote snapshot prevented the write safely",
            )
            self.repository.save(run)
            raise ValueError("Run is missing the quote snapshot required for confirmation")
        refreshed_snapshot = self._quote_snapshot(refreshed_offer)
        if refreshed_snapshot.fingerprint != run.quote_snapshot.fingerprint:
            changes = self._quote_changes(run.quote_snapshot, refreshed_snapshot)
            run.revalidation_changes = changes
            candidate = self._confirmed_candidate(
                run.recommendation.best_overall,
                refreshed_offer,
                refreshed_cost,
                policy_payload,
            )
            run.recommendation.best_overall = candidate
            run.quote_snapshot = refreshed_snapshot
            run.action_token = self.harness.issue_action_token(run)
            self.harness.transition(
                run,
                RunState.WAITING_CONFIRMATION,
                reason="quote facts changed and require a new confirmation",
            )
            self.harness.release_commit_tool_calls(run)
            detail = "；".join(
                f"{change.field}: {change.previous} → {change.current}" for change in changes
            )
            message = f"报价关键事实已变化，未创建草稿。请核对后重新确认：{detail}"
            run.messages.append(Message(role="assistant", content=message))
            self.repository.save(run)
            return ConfirmationStageResult(
                outcome="WAITING_CONFIRMATION", assistant_message=message
            )
        candidate = self._confirmed_candidate(
            run.recommendation.best_overall,
            refreshed_offer,
            refreshed_cost,
            policy_payload,
        )
        run.revalidation_changes = []
        self.repository.save(run)
        return ConfirmationStageResult(outcome="READY_FOR_WRITE", candidate=candidate)

    async def create_draft_node(
        self,
        *,
        run_id: str,
        tenant_id: str,
        user_id: str,
        candidate: Candidate,
    ) -> ConfirmationStageResult:
        run = self.repository.get(run_id, tenant_id=tenant_id, user_id=user_id)
        if run.draft:
            for tool_call in self.execution_repository.list_tools(
                run.run_id, tenant_id=run.tenant_id
            ):
                if (
                    tool_call.tool_name == "create_requisition_draft"
                    and tool_call.status != ExecutionStatus.SUCCEEDED
                    and tool_call.arguments_json.get("business_idempotency_key")
                    == run.draft.idempotency_key
                ):
                    self.execution_repository.complete_tool(
                        tool_call.tool_call_id,
                        {"draft": run.draft.model_dump(mode="json")},
                    )
            return ConfirmationStageResult(
                outcome="DRAFT_CREATED",
                assistant_message=f"采购草稿 {run.draft.draft_id} 已存在，未重复创建。",
                candidate=run.draft.candidate,
            )
        if run.state != RunState.REVALIDATING:
            raise ValueError("Run is not ready for draft creation")
        employee = self.enterprise.identity(run.tenant_id, run.user_id)
        idempotency_key = hashlib.sha256(
            f"{run.run_id}:{candidate.offer.offer_id}:{run.requirement.version}".encode()
        ).hexdigest()[:24]
        arguments = {
            "business_idempotency_key": idempotency_key,
            "offer_id": candidate.offer.offer_id,
            "requirement_version": run.requirement.version,
        }
        journal_key = f"tool:{run.run_id}:create_draft:{idempotency_key}"
        self.harness.transition(run, RunState.DRAFT_CREATING, reason="revalidation passed")
        try:
            draft = await self.tool_gateway.invoke(
                tool_name="create_requisition_draft",
                arguments=arguments,
                context=GatewayExecutionContext(
                    run=run,
                    roles=employee.roles,
                    node_name="create_draft",
                    budget_pool=ToolBudgetPool.COMMIT,
                    confirmed=True,
                    runtime_data={"candidate": candidate},
                ),
                idempotency_key=journal_key,
            )
            run.draft = ProcurementDraft.model_validate(draft)
            self.harness.recovery_transition(
                run,
                RunState.DRAFT_CREATED,
                reason="requisition draft created",
            )
        except Exception:
            if run.state == RunState.DRAFT_CREATING:
                self.harness.recovery_transition(
                    run,
                    RunState.WAITING_CONFIRMATION,
                    reason="draft Gateway failed before a durable business write",
                )
                self.harness.release_commit_tool_calls(run)
                self.repository.save(run)
            raise
        self.harness.trace(
            run,
            actor="Application Runtime",
            kind="TOOL",
            name="create_requisition_draft",
            detail={"risk": "BUSINESS_WRITE", "draft_id": run.draft.draft_id},
        )
        run.error = None
        self.repository.save(run)
        message = (
            f"已创建采购草稿 {run.draft.draft_id}。状态为 DRAFT_NOT_SUBMITTED，"
            "尚未提交审批或下单。"
        )
        return ConfirmationStageResult(
            outcome="DRAFT_CREATED", assistant_message=message, candidate=run.draft.candidate
        )

    @staticmethod
    def _confirmed_candidate(
        original: Candidate,
        offer: Offer,
        cost: CostBreakdown,
        policy_payload: dict[str, Any],
    ) -> Candidate:
        candidate = original.model_copy(deep=True)
        candidate.offer = offer
        candidate.cost = cost
        candidate.policy_decision = PolicyDecision(policy_payload["decision"])
        candidate.approval_route = policy_payload["approval_route"]
        candidate.policy_reasons = policy_payload["reasons"]
        candidate.evidence_refs = list(
            dict.fromkeys(
                [
                    *offer.match_decision.evidence_refs,
                    offer.evidence.evidence_id,
                    *policy_payload["evidence_refs"],
                    *policy_payload["policy_knowledge_refs"],
                ]
            )
        )
        return candidate

    @staticmethod
    def _fingerprint(payload: dict[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()

    def configure_memory_context(
        self,
        memory_repository: MemoryReader | None,
        *,
        context_policy: ContextPolicy | None = None,
        memory_registry: MemoryRegistry | None = None,
    ) -> None:
        """Bind the same memory repository used by the API to Agent context assembly."""
        self.memory_repository = memory_repository
        if context_policy is not None:
            self.context_policy = context_policy
        if memory_registry is not None:
            self.memory_registry = memory_registry
        self.context_assembler = ContextAssembler(
            memory_repository,
            policy=self.context_policy,
            memory_registry=self.memory_registry,
        )

    async def start(
        self,
        *,
        run_id: str | None = None,
        tenant_id: str,
        user_id: str,
        message: str,
        scenario: str | None = None,
    ) -> RunResponse:
        self.enterprise.identity(tenant_id, user_id)
        run = ProcurementRun(
            **({"run_id": run_id} if run_id is not None else {}),
            tenant_id=tenant_id,
            user_id=user_id,
            scenario=scenario,
        )
        self.harness.initialize_run(run)
        try:
            self.harness.trace(run, actor="Application Runtime", kind="STATE", name="run.started")
            run.messages.append(Message(role="user", content=message))
            self._merge_requirement(run.requirement, message)
            if "按公司标准" in message or "按团队标准" in message:
                self._apply_authorized_defaults(run.requirement)
            if getattr(self.planner, "requires_requirement_stage", False):
                requirement_context = await self._plan(run, expected_kind=AgentKind.REQUIREMENT)
                stage = AgentStageResult(
                    execution_id="legacy-requirement",
                    agent_kind=AgentKind.REQUIREMENT.value,
                    requirement_spec=requirement_context.requirement_spec,
                )
                if requirement_context.requirement_spec is None:
                    response = await self._advance(
                        run, scenario=run.scenario, agent_context=requirement_context
                    )
                else:
                    response = self._complete_requirement_stage(run, stage)
                    if run.state == RunState.COLLECTING:
                        agent_context = await self._plan(run, expected_kind=AgentKind.SOURCING)
                        response = await self._advance(
                            run, scenario=run.scenario, agent_context=agent_context
                        )
            else:
                agent_context = await self._plan(run)
                response = await self._advance(
                    run,
                    scenario=run.scenario,
                    agent_context=agent_context,
                )
        except HarnessBudgetExceeded as exc:
            response = self._budget_stopped(run, exc)
        self.repository.save(run)
        return response

    async def continue_run(
        self,
        *,
        run_id: str,
        tenant_id: str,
        user_id: str,
        message: str,
        scenario: str | None = None,
    ) -> RunResponse:
        run = self.repository.get(run_id, tenant_id=tenant_id, user_id=user_id)
        self._assert_owner(run, tenant_id, user_id)
        if run.state not in {RunState.WAITING_USER, RunState.COLLECTING}:
            raise ValueError(f"Run cannot accept clarification in state {run.state}")
        try:
            self.harness.transition(
                run,
                RunState.COLLECTING,
                reason="user supplied clarification",
            )
            run.messages.append(Message(role="user", content=message))
            run.requirement.version += 1
            if scenario is not None:
                run.scenario = scenario
            self._merge_requirement(run.requirement, message)
            if "按公司标准" in message or "按团队标准" in message:
                self._apply_authorized_defaults(run.requirement)
            if getattr(self.planner, "requires_requirement_stage", False):
                requirement_context = await self._plan(run, expected_kind=AgentKind.REQUIREMENT)
                stage = AgentStageResult(
                    execution_id="legacy-requirement",
                    agent_kind=AgentKind.REQUIREMENT.value,
                    requirement_spec=requirement_context.requirement_spec,
                )
                if requirement_context.requirement_spec is None:
                    response = await self._advance(
                        run, scenario=run.scenario, agent_context=requirement_context
                    )
                else:
                    response = self._complete_requirement_stage(run, stage)
                    if run.state == RunState.COLLECTING:
                        agent_context = await self._plan(run, expected_kind=AgentKind.SOURCING)
                        response = await self._advance(
                            run, scenario=run.scenario, agent_context=agent_context
                        )
            else:
                agent_context = await self._plan(run)
                response = await self._advance(
                    run,
                    scenario=run.scenario,
                    agent_context=agent_context,
                )
        except HarnessBudgetExceeded as exc:
            response = self._budget_stopped(run, exc)
        self.repository.save(run)
        return response

    async def confirm(
        self,
        *,
        run_id: str,
        tenant_id: str,
        user_id: str,
        action_token: str,
    ) -> RunResponse:
        lock = self._run_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            return await self._confirm_locked(
                run_id=run_id,
                tenant_id=tenant_id,
                user_id=user_id,
                action_token=action_token,
            )

    async def _confirm_locked(
        self,
        *,
        run_id: str,
        tenant_id: str,
        user_id: str,
        action_token: str,
    ) -> RunResponse:
        prepared = self.prepare_confirmation_node(
            run_id=run_id,
            tenant_id=tenant_id,
            user_id=user_id,
            action_token=action_token,
        )
        if prepared.outcome == "STOPPED":
            run = self.repository.get(run_id, tenant_id=tenant_id, user_id=user_id)
            return RunResponse(run=run, assistant_message=prepared.assistant_message)
        if prepared.outcome == "DRAFT_CREATED":
            run = self.repository.get(run_id, tenant_id=tenant_id, user_id=user_id)
            return RunResponse(run=run, assistant_message=prepared.assistant_message)
        quote = await self.revalidate_quote_node(
            run_id=run_id,
            tenant_id=tenant_id,
            user_id=user_id,
        )
        if quote.outcome != "READY_FOR_POLICY" or quote.refreshed_offer is None:
            run = self.repository.get(run_id, tenant_id=tenant_id, user_id=user_id)
            return RunResponse(run=run, assistant_message=quote.assistant_message)
        policy = await self.revalidate_policy_node(
            run_id=run_id,
            tenant_id=tenant_id,
            user_id=user_id,
            refreshed_offer=quote.refreshed_offer,
        )
        if policy.outcome != "READY_FOR_WRITE" or policy.candidate is None:
            run = self.repository.get(run_id, tenant_id=tenant_id, user_id=user_id)
            return RunResponse(run=run, assistant_message=policy.assistant_message)
        created = await self.create_draft_node(
            run_id=run_id,
            tenant_id=tenant_id,
            user_id=user_id,
            candidate=policy.candidate,
        )
        run = self.repository.get(run_id, tenant_id=tenant_id, user_id=user_id)
        return RunResponse(run=run, assistant_message=created.assistant_message)

    def get(self, run_id: str, *, tenant_id: str, user_id: str) -> ProcurementRun:
        run = self.repository.get(run_id, tenant_id=tenant_id, user_id=user_id)
        self._assert_owner(run, tenant_id, user_id)
        return run

    async def _advance(
        self,
        run: ProcurementRun,
        *,
        scenario: str | None,
        agent_context: AgentRuntimeContext | None = None,
    ) -> RunResponse:
        clarification = self._clarification(run.requirement, run.clarification_rounds)
        if not clarification.ready_for_sourcing:
            if run.clarification_rounds >= MAX_CLARIFICATION_ROUNDS:
                self.harness.trace(
                    run,
                    actor="Requirement Validation Service",
                    kind="AGENT",
                    name="clarification.decision",
                    detail=clarification.model_dump(mode="json"),
                )
                self.harness.transition(
                    run,
                    RunState.FAILED,
                    reason="clarification round limit reached",
                )
                run.error = "达到最大追问轮数，仍缺少不能安全假设的字段：" + "、".join(
                    clarification.missing_fields
                )
                run.messages.append(Message(role="assistant", content=run.error))
                self.harness.trace(
                    run,
                    actor="Application Runtime",
                    kind="STATE",
                    name="clarification.stopped",
                    detail={
                        "rounds": run.clarification_rounds,
                        "missing_fields": clarification.missing_fields,
                    },
                    status="STOPPED",
                )
                return RunResponse(run=run, assistant_message=run.error)
            run.clarification_rounds += 1
            self.harness.transition(
                run,
                RunState.WAITING_USER,
                reason="required procurement facts are missing",
            )
            self.harness.trace(
                run,
                actor="Requirement Validation Service",
                kind="AGENT",
                name="clarification.decision",
                detail=clarification.model_dump(mode="json"),
            )
            question_text = "为了避免错配和预算口径错误，我还需要确认：" + " ".join(
                clarification.questions
            )
            run.messages.append(Message(role="assistant", content=question_text))
            return RunResponse(
                run=run,
                assistant_message=question_text,
                suggested_replies=["预算含税，送到雅加达，最低 32GB 内存"],
            )
        self.harness.trace(
            run,
            actor="Requirement Validation Service",
            kind="AGENT",
            name="clarification.decision",
            detail=clarification.model_dump(mode="json"),
        )
        return await self._source_and_recommend(
            run,
            scenario=scenario,
            agent_context=agent_context,
        )

    async def _source_and_recommend(
        self,
        run: ProcurementRun,
        *,
        scenario: str | None,
        agent_context: AgentRuntimeContext | None = None,
    ) -> RunResponse:
        self.harness.transition(
            run,
            RunState.SOURCING,
            reason="minimum requirement is complete",
        )
        requirement = run.requirement
        safety_missing = [
            name
            for name in ("quantity", "minimum_ram_gb", "minimum_storage_gb")
            if getattr(requirement, name) is None
        ]
        if safety_missing:
            self.harness.transition(run, RunState.FAILED, reason="sourcing precondition failed")
            run.error = "寻源前置校验失败，仍缺少：" + "、".join(safety_missing)
            return RunResponse(run=run, assistant_message=run.error)
        employee = self.enterprise.identity(run.tenant_id, run.user_id)
        try:
            policy_knowledge_refs = self._policy_knowledge_refs(run)
        except PolicyKnowledgeReviewRequired as exc:
            self.harness.trace(
                run,
                actor="Policy Knowledge Service",
                kind="KNOWLEDGE",
                name="knowledge.policy_review_required",
                detail={"code": exc.code},
                status="REQUIRES_REVIEW",
            )
            self.harness.transition(
                run,
                RunState.FAILED,
                reason="policy knowledge requires human review",
            )
            run.error = "制度条款缺失或版本不一致，需要人工复核制度知识"
            return RunResponse(run=run, assistant_message=run.error)
        cached_results = agent_context.catalog_results if agent_context else {}
        cached_failures = agent_context.catalog_failures if agent_context else {}
        tasks = {
            provider: asyncio.create_task(
                self._search_catalog_with_journal(
                    run,
                    roles=employee.roles,
                    provider=provider,
                    scenario=scenario,
                )
            )
            for provider in PROVIDERS
            if provider not in cached_results and provider not in cached_failures
        }
        offers: list[Offer] = []
        normalization_issues = []
        failed_providers: list[str] = []
        executed_results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        provider_results = {
            **cached_results,
            **{
                provider: ProviderError(
                    provider,
                    code,
                    "Provider attempt already failed during Agent retrieval",
                )
                for provider, code in cached_failures.items()
            },
            **dict(zip(tasks, executed_results, strict=True)),
        }
        for provider in PROVIDERS:
            provider_result = provider_results[provider]
            if not isinstance(provider_result, BaseException):
                offers.extend(provider_result.offers)
                normalization_issues.extend(provider_result.assessments)
                match_rejected = sum(
                    assessment.match_decision.decision == MatchDecisionType.REJECTED
                    for assessment in provider_result.assessments
                )
                review_required = sum(
                    assessment.match_decision.decision == MatchDecisionType.REVIEW_REQUIRED
                    for assessment in provider_result.assessments
                )
                self.harness.trace(
                    run,
                    actor="Catalog Service",
                    kind="TOOL",
                    name="search_catalog",
                    detail={
                        "provider": provider,
                        "retrieved": provider_result.retrieved_count,
                        "verified_offers": len(provider_result.offers),
                        "conflict_vetoes": match_rejected,
                        "review_required": review_required,
                        "risk": "READ",
                        "reused_agent_tool_result": provider in cached_results,
                    },
                )
            elif isinstance(
                provider_result,
                (ProviderError, ToolTimeoutError, CircuitOpenError),
            ):
                failed_providers.append(provider)
                self.harness.trace(
                    run,
                    actor="Catalog Service",
                    kind="TOOL",
                    name="search_catalog",
                    detail={
                        "provider": provider,
                        "code": getattr(provider_result, "code", type(provider_result).__name__),
                        "risk": "READ",
                    },
                    status="DEGRADED",
                )
            elif isinstance(provider_result, HarnessBudgetExceeded):
                raise provider_result
            else:
                raise provider_result
        if not offers:
            self.harness.transition(
                run,
                RunState.FAILED,
                reason="all catalog providers failed",
            )
            run.error = "所有商品来源均不可用"
            return RunResponse(run=run, assistant_message=run.error)

        candidates: list[Candidate] = []
        rejected = 0
        offer_map = {(offer.provider, offer.offer_id): offer for offer in offers}
        policy_results = await self.tool_gateway.invoke(
            tool_name="evaluate_policy_batch",
            arguments={
                "offer_keys": [f"{offer.provider}/{offer.offer_id}" for offer in offers]
            },
            context=GatewayExecutionContext(
                run=run,
                roles=employee.roles,
                node_name="complete_turn",
                budget_pool=ToolBudgetPool.WORKFLOW_READ,
                runtime_data={"offers": offer_map},
            ),
        )
        for offer in offers:
            cost = self._calculate_cost(offer, requirement)
            if requirement.unit_budget and offer.unit_price.amount > requirement.unit_budget.amount:
                rejected += 1
                continue
            if offer.variant.condition != requirement.condition:
                rejected += 1
                continue
            policy_payload = policy_results[f"{offer.provider}/{offer.offer_id}"]
            policy_decision = PolicyDecision(policy_payload["decision"])
            if policy_decision == PolicyDecision.DENY:
                rejected += 1
                continue
            warnings: list[str] = []
            if offer.discount_condition:
                warnings.append(f"条件优惠未计入可信总价：{offer.discount_condition}")
            description = str(offer.evidence.fields.get("description", ""))
            if "ignore company policy" in description.lower():
                warnings.append("检测到不可信网页指令，已作为商品数据隔离")
            score = self._score(offer, cost, policy_decision)
            candidates.append(
                Candidate(
                    offer=offer,
                    cost=cost,
                    requirement_fit=1.0,
                    policy_decision=policy_decision,
                    approval_route=policy_payload["approval_route"],
                    policy_reasons=policy_payload["reasons"],
                    evidence_refs=list(
                        dict.fromkeys(
                            [
                                *offer.match_decision.evidence_refs,
                                offer.evidence.evidence_id,
                            *policy_payload["evidence_refs"],
                                *policy_knowledge_refs,
                            ]
                        )
                    ),
                    warnings=warnings,
                    score=score,
                )
            )
        if not candidates:
            self.harness.transition(
                run,
                RunState.FAILED,
                reason="no candidate passed budget, specification and policy gates",
            )
            run.error = "没有同时满足预算、规格和制度的候选"
            return RunResponse(run=run, assistant_message=run.error)

        self.harness.transition(
            run,
            RunState.REVIEWING,
            reason="catalog evidence collected",
        )
        lowest = min(candidates, key=lambda item: item.cost.unconditional_total.amount)
        fastest = min(candidates, key=lambda item: (item.offer.delivery_days, -item.score))
        best = max(candidates, key=lambda item: item.score)
        deterministic_best = best
        selection_source = "deterministic_score"
        selection_rationale = "按固定成本、交付、供应商状态和保修权重选择"
        selected_regret = 0.0
        candidate_projection = [
            ValidatedCandidateView(
                provider=item.offer.provider,
                offer_id=item.offer.offer_id,
                brand=item.offer.product.brand,
                model=item.offer.product.model,
                landed_total=item.cost.unconditional_total,
                delivery_days=item.offer.delivery_days,
                policy_decision=item.policy_decision,
                approval_route=item.approval_route,
                evidence_refs=item.evidence_refs,
                utility_score=item.score,
            )
            for item in candidates
        ]
        candidate_set_hash = self._fingerprint(
            {"candidates": [item.model_dump(mode="json") for item in candidate_projection]}
        )
        validated_set = ValidatedCandidateSet(
            run_id=run.run_id,
            candidate_set_hash=candidate_set_hash,
            requirement_spec_id=(run.requirement_spec.spec_id if run.requirement_spec else None),
            requirement_spec_hash=(
                self._fingerprint(run.requirement_spec.model_dump(mode="json"))
                if run.requirement_spec
                else None
            ),
            requirement_version=requirement.version,
            policy_version=str(self.enterprise.policy["version"]),
            candidates=candidate_projection,
        )
        run.validated_candidate_set = validated_set
        self.harness.trace(
            run,
            actor="Candidate Validation Service",
            kind="SERVICE",
            name="validated_candidate_set.created",
            detail={
                "requirement_spec_id": validated_set.requirement_spec_id,
                "requirement_spec_hash": validated_set.requirement_spec_hash,
                "requirement_version": validated_set.requirement_version,
                "policy_version": validated_set.policy_version,
                "candidate_count": len(validated_set.candidates),
                "candidate_set_hash": validated_set.candidate_set_hash,
            },
        )
        ranking_proposals = list(agent_context.proposed_candidates) if agent_context else []
        if (
            agent_context
            and run.requirement_spec is not None
            and not getattr(self.planner, "last_error", None)
            and hasattr(self.planner, "rank_candidates")
        ):
            try:
                ranking = await self._rank_validated_candidates(
                    run, validated_set=validated_set, agent_context=agent_context
                )
                if ranking is not None:
                    run.sourcing_ranking = ranking
                    ranking_proposals = [
                        AgentCandidateProposal(
                            provider=ranking.provider,
                            offer_id=ranking.offer_id,
                            evidence_refs=ranking.evidence_refs,
                        )
                    ]
                    agent_context.decision_rationale = ranking.rationale
            except HarnessBudgetExceeded:
                raise
            except Exception as exc:
                ranking_proposals = []
                self.harness.trace(
                    run,
                    actor="Application Runtime",
                    kind="AGENT",
                    name="sourcing.rank_degraded",
                    detail={"error": f"{type(exc).__name__}: {exc}"},
                    status="DEGRADED",
                )
        if agent_context and ranking_proposals:
            candidate_by_key = {
                (candidate.offer.provider, candidate.offer.offer_id): candidate
                for candidate in candidates
            }
            for proposal in ranking_proposals:
                proposal_key = (proposal.provider, proposal.offer_id)
                proposed = candidate_by_key.get(proposal_key)
                proposal_evidence = set(proposal.evidence_refs)
                validated_evidence = set(proposed.evidence_refs) if proposed else set()
                regret = deterministic_best.score - proposed.score if proposed else 1.0
                ranking_is_bound = bool(
                    run.sourcing_ranking
                    and run.sourcing_ranking.candidate_set_hash
                    == validated_set.candidate_set_hash
                    and run.sourcing_ranking.requirement_version
                    == validated_set.requirement_version
                    and run.sourcing_ranking.requirement_spec_hash
                    == validated_set.requirement_spec_hash
                    and (run.sourcing_ranking.provider, run.sourcing_ranking.offer_id)
                    == proposal_key
                )
                evidence_is_valid = bool(
                    proposed
                    and (ranking_is_bound or proposal_key in agent_context.offers)
                    and proposed.offer.evidence.evidence_id in proposal_evidence
                    and proposal_evidence.issubset(validated_evidence)
                )
                if (
                    proposed
                    and evidence_is_valid
                    and regret <= self.max_agent_utility_regret
                ):
                    best = proposed
                    selected_regret = max(0.0, regret)
                    selection_source = "agent_validated"
                    selection_rationale = agent_context.decision_rationale
                    self.harness.trace(
                        run,
                        actor="Evidence Validation Service",
                        kind="SERVICE",
                        name="agent.recommendation_validated",
                        detail={
                            "provider": proposal.provider,
                            "offer_id": proposal.offer_id,
                            "evidence_refs": proposal.evidence_refs,
                            "utility_regret": round(regret, 6),
                            "maximum_regret": self.max_agent_utility_regret,
                        },
                    )
                    self.harness.trace(
                        run,
                        actor="Ranking Validation Service",
                        kind="SERVICE",
                        name="sourcing.ranking_accepted",
                        detail={
                            "candidate_set_hash": validated_set.candidate_set_hash,
                            "selected": f"{proposal.provider}/{proposal.offer_id}",
                            "baseline": (
                                f"{deterministic_best.offer.provider}/"
                                f"{deterministic_best.offer.offer_id}"
                            ),
                            "utility_regret": round(regret, 6),
                        },
                    )
                    break
                self.harness.trace(
                    run,
                    actor="Evidence Validation Service",
                    kind="SERVICE",
                    name="agent.recommendation_rejected",
                    detail={
                        "provider": proposal.provider,
                        "offer_id": proposal.offer_id,
                        "reason": (
                            "utility regret exceeds guardrail"
                            if proposed and evidence_is_valid
                            else "candidate or required evidence did not pass server validation"
                        ),
                        "utility_regret": round(regret, 6),
                    },
                    status="REJECTED",
                )
                self.harness.trace(
                    run,
                    actor="Ranking Validation Service",
                    kind="SERVICE",
                    name="sourcing.ranking_rejected",
                    detail={
                        "candidate_set_hash": validated_set.candidate_set_hash,
                        "selected": f"{proposal.provider}/{proposal.offer_id}",
                        "utility_regret": round(regret, 6),
                    },
                    status="REJECTED",
                )
        degraded = (
            f"；{', '.join(failed_providers)} 暂不可用，结果为部分成功" if failed_providers else ""
        )
        summary = (
            f"已对 {len(offers)} 个已验证同款报价进行比较，{len(candidates)} 个通过业务硬约束；"
            f"{len(normalization_issues)} 个原始报价因冲突或信息不足未进入比价。"
            f"综合最优为 {best.offer.product.brand} {best.offer.product.model}，"
            f"{requirement.quantity} 台可信到手总价为 "
            f"{best.cost.unconditional_total.amount} {best.cost.unconditional_total.currency}{degraded}。"
        )
        run.recommendation = Recommendation(
            lowest_cost=lowest,
            fastest_delivery=fastest,
            best_overall=best,
            compared_offer_count=len(offers),
            rejected_offer_count=rejected + len(normalization_issues),
            match_rejected_count=sum(
                assessment.match_decision.decision == MatchDecisionType.REJECTED
                for assessment in normalization_issues
            ),
            review_required_count=sum(
                assessment.match_decision.decision == MatchDecisionType.REVIEW_REQUIRED
                for assessment in normalization_issues
            ),
            normalization_issues=normalization_issues,
            selection_source=selection_source,
            selection_rationale=selection_rationale,
            requirement_spec_id=(run.requirement_spec.spec_id if run.requirement_spec else None),
            requirement_spec_hash=validated_set.requirement_spec_hash,
            candidate_set_hash=validated_set.candidate_set_hash,
            ranking_policy_version="ranking-policy-v1",
            utility_regret=round(selected_regret, 6),
            summary=summary,
        )
        self.harness.trace(
            run,
            actor="Policy Rule Service",
            kind="SERVICE",
            name="policy.reviewed",
            detail={
                "candidates": len(candidates),
                "match_rejected": run.recommendation.match_rejected_count,
                "review_required": run.recommendation.review_required_count,
                "decision": best.policy_decision,
                "approval_route": best.approval_route,
            },
        )
        self.harness.trace(
            run,
            actor="Recommendation Service",
            kind="SERVICE",
            name="recommendation.composed",
            detail={
                "best_offer": best.offer.offer_id,
                "evidence_refs": best.evidence_refs,
                "selection_source": selection_source,
            },
        )
        self.harness.transition(
            run,
            RunState.WAITING_CONFIRMATION,
            reason="recommendation is ready for explicit confirmation",
        )
        run.quote_snapshot = self._quote_snapshot(best.offer)
        run.revalidation_changes = []
        run.action_token = self.harness.issue_action_token(run)
        run.messages.append(Message(role="assistant", content=summary))
        return RunResponse(run=run, assistant_message=summary)

    async def _rank_validated_candidates(
        self,
        run: ProcurementRun,
        *,
        validated_set: ValidatedCandidateSet,
        agent_context: AgentRuntimeContext,
    ) -> SourcingRankingDecision | None:
        idempotency_key = (
            f"agent:{run.run_id}:SOURCING_RANK:{validated_set.candidate_set_hash}"
        )
        existing = self.execution_repository.get_agent(idempotency_key)
        if existing is not None and existing.status == ExecutionStatus.SUCCEEDED:
            payload = existing.result_json or {}
            ranking_payload = payload.get("ranking")
            return (
                SourcingRankingDecision.model_validate(ranking_payload)
                if ranking_payload is not None
                else None
            )
        claimed = self.execution_repository.claim_agent(
            AgentExecution(
                tenant_id=run.tenant_id,
                user_id=run.user_id,
                run_id=run.run_id,
                node_name="sourcing_rank",
                agent_kind=AgentKind.SOURCING.value,
                task_id=f"rank_{run.state_version}",
                state_version=run.state_version,
                input_fingerprint=validated_set.candidate_set_hash,
                idempotency_key=idempotency_key,
            )
        )
        self.harness.trace(
            run,
            actor="Sourcing Agent",
            kind="AGENT",
            name="sourcing.ranking_started",
            detail={
                "execution_id": claimed.execution_id,
                "candidate_set_hash": validated_set.candidate_set_hash,
                "requirement_version": validated_set.requirement_version,
            },
        )
        try:
            ranking = await self.planner.rank_candidates(
                validated_set, context=agent_context
            )
            self.execution_repository.complete_agent(
                claimed.execution_id,
                {
                    "ranking": ranking.model_dump(mode="json")
                    if ranking is not None
                    else None
                },
            )
            return ranking
        except Exception as exc:
            self.execution_repository.fail_agent(
                claimed.execution_id, f"{type(exc).__name__}: {exc}"
            )
            raise

    async def _search_catalog_with_journal(
        self,
        run: ProcurementRun,
        *,
        roles: tuple[str, ...],
        provider: str,
        scenario: str | None,
    ) -> CatalogSearchResult:
        if scenario != run.scenario:
            raise ValueError("Catalog scenario must come from the server-owned Run")
        result = await self.tool_gateway.invoke(
            tool_name="search_catalog",
            arguments={"provider": provider},
            context=GatewayExecutionContext(
                run=run,
                roles=roles,
                node_name="complete_turn",
                budget_pool=ToolBudgetPool.WORKFLOW_READ,
            ),
        )
        return CatalogSearchResult.model_validate(result)

    async def _plan(
        self, run: ProcurementRun, expected_kind: AgentKind | None = None
    ) -> AgentRuntimeContext:
        employee = self.enterprise.identity(run.tenant_id, run.user_id)
        context = AgentRuntimeContext(
            run=run,
            catalog=self.catalog,
            enterprise=self.enterprise,
            harness=self.harness,
            roles=employee.roles,
            tool_gateway=self.tool_gateway,
            model_gateway=self.model_gateway,
            context_assembler=self.context_assembler,
            requirement_spec=run.requirement_spec,
        )
        if expected_kind is not None:
            context.agent_kind = expected_kind.value
        if expected_kind is not None and hasattr(self.planner, "plan_for"):
            async def operation():
                return await self.planner.plan_for(
                    expected_kind, run.messages, context=context
                )
        else:
            async def operation():
                return await self.planner.plan(run.messages, context=context)
        plan = await self.harness.execute_agent_turn(
            run,
            operation,
        )
        context.proposed_candidates = plan.candidate_proposals
        context.decision_rationale = plan.rationale
        context.requirement_spec = plan.requirement_spec
        self.harness.trace(
            run,
            actor="Application Runtime",
            kind="SERVICE",
            name="runtime.plan_applied",
            detail={
                **plan.model_dump(mode="json"),
                "message_count": len(run.messages),
            },
        )
        return context

    def _policy_knowledge_refs(self, run: ProcurementRun) -> list[str]:
        if self.knowledge_service is None:
            return []
        expected_version = str(self.enterprise.policy["version"])
        evidence = self.knowledge_service.verify_policy_evidence(
            tenant_id=run.tenant_id,
            category_id="CAT-COMPUTER-LAPTOP",
            query="研发 笔记本 内存 保修 全新 预算 审批 供应商 准入 发票",
            expected_policy_version=expected_version,
        )
        if evidence is None:
            return []
        refs = [citation.source_ref for citation in evidence.citations]
        self.harness.trace(
            run,
            actor="Policy Knowledge Service",
            kind="KNOWLEDGE",
            name="knowledge.policy_evidence_resolved",
            detail={
                "policy_version": evidence.policy_version,
                "citation_refs": refs,
            },
        )
        return refs

    def _budget_stopped(self, run: ProcurementRun, exc: HarnessBudgetExceeded) -> RunResponse:
        message = (
            f"执行已停止：{exc.resource} 预算已用尽（{exc.used}/{exc.limit}）。"
            "已保留当前状态和追踪记录。"
        )
        run.error = str(exc)
        run.messages.append(Message(role="assistant", content=message))
        return RunResponse(run=run, assistant_message=message)

    def _restore_waiting_confirmation(self, run: ProcurementRun, *, reason: str) -> None:
        """Release an unused commit reservation and preserve a retryable safe state."""
        self.harness.release_commit_tool_calls(run)
        if run.state == RunState.REVALIDATING:
            self.harness.recovery_transition(
                run,
                RunState.WAITING_CONFIRMATION,
                reason=reason,
            )

    @staticmethod
    def _merge_requirement(requirement: ProcurementRequirement, text: str) -> None:
        normalized = text.lower().replace("，", ",")
        quantity_match = re.search(r"(\d+)\s*台", normalized)
        if quantity_match:
            requirement.quantity = int(quantity_match.group(1))
        budget_match = re.search(r"(?:单台[^\d]{0,8}|预算[^\d]{0,8})(\d{3,6})", normalized)
        if budget_match:
            requirement.unit_budget = Money(amount=Decimal(budget_match.group(1)), currency="CNY")
        if "含税" in normalized:
            requirement.budget_tax_included = True
        elif "未税" in normalized or "不含税" in normalized:
            requirement.budget_tax_included = False
        city_mapping = {
            "雅加达": "Jakarta",
            "jakarta": "Jakarta",
            "上海": "Shanghai",
            "深圳": "Shenzhen",
        }
        for marker, city in city_mapping.items():
            if marker in normalized:
                requirement.delivery_city = city
                break
        ram_match = re.search(r"(?:最低|至少)?\s*(16|32|64|128)\s*(?:g|gb)", normalized)
        if ram_match:
            requirement.minimum_ram_gb = int(ram_match.group(1))
        storage_match = re.search(r"(?:最低|至少)?\s*(512|1024|2048)\s*(?:g|gb)", normalized)
        if storage_match:
            requirement.minimum_storage_gb = int(storage_match.group(1))
        if "1t" in normalized or "1tb" in normalized:
            requirement.minimum_storage_gb = 1024
        if "两周" in normalized or "2周" in normalized:
            requirement.delivery_days = 14
        days_match = re.search(r"(\d+)\s*天", normalized)
        if days_match:
            requirement.delivery_days = int(days_match.group(1))
        if requirement.minimum_storage_gb is None and (
            "开发" in normalized or "docker" in normalized
        ):
            requirement.minimum_storage_gb = 1024
            if "按公司标准补充 1TB 存储" not in requirement.assumptions:
                requirement.assumptions.append("按公司标准补充 1TB 存储")

    def _apply_authorized_defaults(self, requirement: ProcurementRequirement) -> None:
        baseline = self.enterprise.policy["developer_laptop_baseline"]
        if requirement.minimum_ram_gb is None:
            requirement.minimum_ram_gb = int(baseline["minimum_ram_gb"])
            requirement.assumptions.append(
                f"用户授权按公司标准设置最低内存为 {requirement.minimum_ram_gb}GB"
            )
        if requirement.minimum_storage_gb is None:
            requirement.minimum_storage_gb = 1024
            requirement.assumptions.append("用户授权按公司标准设置最低存储为 1TB")

    @staticmethod
    def _clarification(
        requirement: ProcurementRequirement, completed_rounds: int
    ) -> ClarificationDecision:
        required = [
            "quantity",
            "unit_budget",
            "budget_tax_included",
            "delivery_city",
            "minimum_ram_gb",
            "minimum_storage_gb",
        ]
        missing = sorted(
            (field for field in required if getattr(requirement, field) is None),
            key=lambda field: QUESTION_INFORMATION_VALUE[field],
            reverse=True,
        )
        remaining_rounds = max(1, MAX_CLARIFICATION_ROUNDS - completed_rounds)
        question_count = min(3, max(1, (len(missing) + remaining_rounds - 1) // remaining_rounds))
        selected = missing[:question_count]
        questions = [QUESTION_LABELS[field] for field in selected]
        return ClarificationDecision(
            missing_fields=missing,
            questions=questions,
            ready_for_sourcing=not missing,
            can_search_in_parallel=True,
            rationale=(
                "按预算、数量、最低配置、交付地和税口径的信息价值排序；"
                f"剩余 {remaining_rounds} 轮，本轮询问 {len(selected)} 个字段"
            ),
        )

    @staticmethod
    def _calculate_cost(offer: Offer, requirement: ProcurementRequirement) -> CostBreakdown:
        return calculate_landed_cost(offer, requirement)

    @staticmethod
    def _score(offer: Offer, cost: CostBreakdown, decision: PolicyDecision) -> float:
        cost_component = max(0.0, 1.0 - float(cost.unconditional_total.amount / Decimal("240000")))
        delivery_component = max(0.0, 1.0 - offer.delivery_days / 30)
        vendor_component = 1.0 if decision == PolicyDecision.ALLOW else 0.78
        warranty_component = min(1.0, offer.variant.warranty_months / 36)
        score = (
            0.42 * cost_component
            + 0.24 * delivery_component
            + 0.22 * vendor_component
            + 0.12 * warranty_component
        )
        return round(score, 6)

    @staticmethod
    def _quote_snapshot(offer: Offer) -> QuoteSnapshot:
        fingerprint_payload = {
            "offer_id": offer.offer_id,
            "provider": offer.provider,
            "source_ref": offer.evidence.source_ref,
            "content_hash": offer.evidence.content_hash,
            "unit_price": str(offer.unit_price.amount),
            "currency": offer.unit_price.currency,
            "shipping": str(offer.shipping.amount),
            "tax_rate": str(offer.tax_rate),
            "stock": offer.stock,
            "delivery_days": offer.delivery_days,
            "fetched_at": offer.evidence.fetched_at.isoformat(),
            "expires_at": offer.evidence.expires_at.isoformat(),
        }
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()
        return QuoteSnapshot(
            offer_id=offer.offer_id,
            provider=offer.provider,
            source_ref=offer.evidence.source_ref,
            content_hash=offer.evidence.content_hash,
            unit_price=offer.unit_price,
            shipping=offer.shipping,
            tax_rate=offer.tax_rate,
            stock=offer.stock,
            delivery_days=offer.delivery_days,
            fetched_at=offer.evidence.fetched_at,
            expires_at=offer.evidence.expires_at,
            fingerprint=fingerprint,
        )

    @staticmethod
    def _quote_changes(previous: QuoteSnapshot, current: QuoteSnapshot) -> list[RevalidationChange]:
        fields = {
            "unit_price": (previous.unit_price.amount, current.unit_price.amount),
            "shipping": (previous.shipping.amount, current.shipping.amount),
            "tax_rate": (previous.tax_rate, current.tax_rate),
            "stock": (previous.stock, current.stock),
            "delivery_days": (previous.delivery_days, current.delivery_days),
            "content_hash": (previous.content_hash, current.content_hash),
            "expires_at": (previous.expires_at.isoformat(), current.expires_at.isoformat()),
        }
        return [
            RevalidationChange(field=field, previous=str(old), current=str(new))
            for field, (old, new) in fields.items()
            if old != new
        ]

    @staticmethod
    def _assert_owner(run: ProcurementRun, tenant_id: str, user_id: str) -> None:
        if run.tenant_id != tenant_id or run.user_id != user_id:
            raise PermissionError("Run is not available in this tenant/user scope")
