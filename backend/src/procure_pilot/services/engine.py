from __future__ import annotations

import asyncio
import hashlib
import json
import re
from decimal import Decimal

from procure_pilot.agents.runtime import AgentPlanner, DeterministicAgentPlanner
from procure_pilot.agents.tools import AgentRuntimeContext
from procure_pilot.domain.models import (
    BudgetResource,
    Candidate,
    ClarificationDecision,
    CostBreakdown,
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
    ToolBudgetPool,
)
from procure_pilot.fixtures.catalog import PROVIDERS, FixtureCatalogAdapter, ProviderError
from procure_pilot.fixtures.enterprise import FixtureEnterpriseGateway
from procure_pilot.harness.runtime import (
    AgentHarness,
    CircuitOpenError,
    HarnessBudgetExceeded,
    RunRepositoryPort,
    ToolTimeoutError,
)
from procure_pilot.services.context import (
    DEFAULT_CONTEXT_POLICY,
    ContextAssembler,
    ContextPolicy,
    MemoryReader,
)
from procure_pilot.services.costing import calculate_landed_cost
from procure_pilot.services.memory_registry import MemoryRegistry

QUESTION_LABELS = {
    "quantity": "需要采购多少台？",
    "unit_budget": "单台预算是多少，币种是什么？",
    "budget_tax_included": "单台预算是含税还是未税？",
    "delivery_city": "具体交付城市是哪里？",
    "minimum_ram_gb": "最低内存要求是多少？开发机建议明确为 32GB 或 64GB。",
}

QUESTION_INFORMATION_VALUE = {
    "unit_budget": 1.00,
    "quantity": 0.95,
    "minimum_ram_gb": 0.90,
    "delivery_city": 0.85,
    "budget_tax_included": 0.80,
}

MAX_CLARIFICATION_ROUNDS = 3


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
    ) -> None:
        self.catalog = catalog
        self.enterprise = enterprise
        self.repository = repository
        self.harness = harness
        self.planner = planner or DeterministicAgentPlanner()
        self.memory_repository = memory_repository
        self.context_policy = context_policy
        self.memory_registry = memory_registry
        self.context_assembler = ContextAssembler(
            memory_repository,
            policy=context_policy,
            memory_registry=memory_registry,
        )
        self._run_locks: dict[str, asyncio.Lock] = {}

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
        tenant_id: str,
        user_id: str,
        message: str,
        scenario: str | None = None,
    ) -> RunResponse:
        self.enterprise.identity(tenant_id, user_id)
        run = ProcurementRun(tenant_id=tenant_id, user_id=user_id, scenario=scenario)
        self.harness.initialize_run(run)
        try:
            self.harness.trace(run, actor="Application Runtime", kind="STATE", name="run.started")
            run.messages.append(Message(role="user", content=message))
            self._merge_requirement(run.requirement, message)
            if "按公司标准" in message or "按团队标准" in message:
                self._apply_authorized_defaults(run.requirement)
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
        run = self.repository.get(run_id, tenant_id=tenant_id, user_id=user_id)
        self._assert_owner(run, tenant_id, user_id)
        if run.draft:
            return RunResponse(
                run=run, assistant_message=f"采购草稿 {run.draft.draft_id} 已存在，未重复创建。"
            )
        if run.state != RunState.WAITING_CONFIRMATION or not run.recommendation:
            raise ValueError("Run is not waiting for confirmation")
        try:
            self.harness.verify_action_token(action_token, run)
            self.harness.assert_budget_available(
                run,
                BudgetResource.STATE_TRANSITIONS,
                3,
            )
            self.harness.reserve_commit_tool_calls(run, 3)
            self.harness.transition(
                run,
                RunState.REVALIDATING,
                reason="user confirmed the quoted action",
            )
            employee = self.enterprise.identity(run.tenant_id, run.user_id)
            candidate = run.recommendation.best_overall
            refreshed_offer = await self.harness.execute_tool(
                run,
                tool_name="revalidate_quote",
                roles=employee.roles,
                provider=candidate.offer.provider,
                budget_pool=ToolBudgetPool.COMMIT,
                operation=lambda: self.catalog.get_offer(
                    candidate.offer.provider,
                    candidate.offer.offer_id,
                    scenario=run.scenario,
                ),
            )
        except HarnessBudgetExceeded as exc:
            self._restore_waiting_confirmation(
                run,
                reason="confirmation budget was unavailable or exhausted safely",
            )
            response = self._budget_stopped(run, exc)
            self.repository.save(run)
            return response
        except (ProviderError, ToolTimeoutError, CircuitOpenError) as exc:
            self._restore_waiting_confirmation(
                run,
                reason="quote revalidation tool failed safely",
            )
            self.harness.trace(
                run,
                actor="Agent Harness",
                kind="HARNESS",
                name="revalidation.degraded",
                detail={"error_code": getattr(exc, "code", type(exc).__name__)},
                status="DEGRADED",
            )
            message = "报价复核暂时失败，未创建草稿。请稍后重新确认。"
            run.messages.append(Message(role="assistant", content=message))
            self.repository.save(run)
            return RunResponse(run=run, assistant_message=message)
        refreshed_snapshot = self._quote_snapshot(refreshed_offer)
        refreshed_cost = self._calculate_cost(refreshed_offer, run.requirement)
        try:
            self.harness.begin_tool_call(
                run,
                tool_name="evaluate_policy",
                roles=employee.roles,
                confirmed=False,
                budget_pool=ToolBudgetPool.COMMIT,
            )
        except HarnessBudgetExceeded as exc:
            self._restore_waiting_confirmation(
                run,
                reason="policy revalidation budget was exhausted safely",
            )
            response = self._budget_stopped(run, exc)
            self.repository.save(run)
            return response
        refreshed = self.enterprise.evaluate(
            employee=employee,
            vendor_id=refreshed_offer.vendor_id,
            landed_cost=refreshed_cost.unconditional_total.amount,
            ram_gb=refreshed_offer.variant.ram_gb,
            warranty_months=refreshed_offer.variant.warranty_months,
            condition=refreshed_offer.variant.condition,
        )
        if refreshed.decision == PolicyDecision.DENY:
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
        if refreshed_snapshot.fingerprint != run.quote_snapshot.fingerprint:
            changes = self._quote_changes(run.quote_snapshot, refreshed_snapshot)
            run.revalidation_changes = changes
            refreshed_candidate = candidate.model_copy(deep=True)
            refreshed_candidate.offer = refreshed_offer
            refreshed_candidate.cost = refreshed_cost
            refreshed_candidate.policy_decision = refreshed.decision
            refreshed_candidate.approval_route = refreshed.approval_route
            refreshed_candidate.policy_reasons = refreshed.reasons
            refreshed_candidate.evidence_refs = list(
                dict.fromkeys(
                    [
                        *refreshed_offer.match_decision.evidence_refs,
                        refreshed_offer.evidence.evidence_id,
                        *refreshed.evidence_refs,
                    ]
                )
            )
            refreshed_candidate.score = self._score(
                refreshed_offer, refreshed_cost, refreshed.decision
            )
            run.recommendation.best_overall = refreshed_candidate
            run.quote_snapshot = refreshed_snapshot
            run.action_token = self.harness.issue_action_token(run)
            try:
                self.harness.transition(
                    run,
                    RunState.WAITING_CONFIRMATION,
                    reason="quote facts changed and require a new confirmation",
                )
                self.harness.release_commit_tool_calls(run)
            except HarnessBudgetExceeded as exc:
                self._restore_waiting_confirmation(
                    run,
                    reason="quote-change recovery completed safely",
                )
                response = self._budget_stopped(run, exc)
                self.repository.save(run)
                return response
            self.harness.trace(
                run,
                actor="Application Runtime",
                kind="TOOL",
                name="revalidate_quote",
                detail={
                    "risk": "READ",
                    "result": "CHANGED",
                    "changed_fields": [change.field for change in changes],
                    "quote_fingerprint": refreshed_snapshot.fingerprint,
                },
                status="REQUIRES_CONFIRMATION",
            )
            run.error = None
            self.repository.save(run)
            detail = "；".join(
                f"{change.field}: {change.previous} → {change.current}" for change in changes
            )
            return RunResponse(
                run=run,
                assistant_message=f"报价关键事实已变化，未创建草稿。请核对后重新确认：{detail}",
            )

        candidate = candidate.model_copy(deep=True)
        candidate.offer = refreshed_offer
        candidate.cost = refreshed_cost
        candidate.policy_decision = refreshed.decision
        candidate.approval_route = refreshed.approval_route
        candidate.policy_reasons = refreshed.reasons
        candidate.evidence_refs = list(
            dict.fromkeys(
                [
                    *refreshed_offer.match_decision.evidence_refs,
                    refreshed_offer.evidence.evidence_id,
                    *refreshed.evidence_refs,
                ]
            )
        )
        run.revalidation_changes = []
        try:
            self.harness.begin_tool_call(
                run,
                tool_name="create_requisition_draft",
                roles=employee.roles,
                confirmed=True,
                budget_pool=ToolBudgetPool.COMMIT,
            )
            self.harness.transition(
                run,
                RunState.DRAFT_CREATING,
                reason="revalidation passed",
            )
        except HarnessBudgetExceeded as exc:
            self._restore_waiting_confirmation(
                run,
                reason="draft creation budget was exhausted before the write",
            )
            response = self._budget_stopped(run, exc)
            self.repository.save(run)
            return response
        idempotency_key = hashlib.sha256(
            f"{run.run_id}:{candidate.offer.offer_id}:{run.requirement.version}".encode()
        ).hexdigest()[:24]
        run.draft = ProcurementDraft(
            draft_id=f"draft_{idempotency_key[:12]}",
            run_id=run.run_id,
            idempotency_key=idempotency_key,
            candidate=candidate,
            approval_route=refreshed.approval_route,
        )
        self.harness.recovery_transition(
            run,
            RunState.DRAFT_CREATED,
            reason="requisition draft created",
        )
        self.harness.trace(
            run,
            actor="Application Runtime",
            kind="TOOL",
            name="create_requisition_draft",
            detail={"risk": "BUSINESS_WRITE", "draft_id": run.draft.draft_id},
        )
        run.error = None
        self.repository.save(run)
        return RunResponse(
            run=run,
            assistant_message=f"已创建采购草稿 {run.draft.draft_id}。状态为 DRAFT_NOT_SUBMITTED，尚未提交审批或下单。",
        )

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
        assert requirement.quantity is not None
        assert requirement.minimum_ram_gb is not None
        assert requirement.minimum_storage_gb is not None
        employee = self.enterprise.identity(run.tenant_id, run.user_id)
        cached_results = agent_context.catalog_results if agent_context else {}
        tasks = {
            provider: asyncio.create_task(
                self.harness.execute_tool(
                    run,
                    tool_name="search_catalog",
                    roles=employee.roles,
                    provider=provider,
                    budget_pool=ToolBudgetPool.WORKFLOW_READ,
                    operation=lambda provider=provider: self.catalog.search(
                        provider,
                        minimum_ram_gb=requirement.minimum_ram_gb,
                        minimum_storage_gb=requirement.minimum_storage_gb,
                        quantity=requirement.quantity,
                        scenario=scenario,
                    ),
                )
            )
            for provider in PROVIDERS
            if provider not in cached_results
        }
        offers: list[Offer] = []
        normalization_issues = []
        failed_providers: list[str] = []
        executed_results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        provider_results = {
            **cached_results,
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
        self.harness.begin_tool_call(
            run,
            tool_name="evaluate_policy",
            roles=employee.roles,
            confirmed=False,
            budget_pool=ToolBudgetPool.WORKFLOW_READ,
        )
        for offer in offers:
            cost = self._calculate_cost(offer, requirement)
            if requirement.unit_budget and offer.unit_price.amount > requirement.unit_budget.amount:
                rejected += 1
                continue
            if offer.variant.condition != requirement.condition:
                rejected += 1
                continue
            policy = self.enterprise.evaluate(
                employee=employee,
                vendor_id=offer.vendor_id,
                landed_cost=cost.unconditional_total.amount,
                ram_gb=offer.variant.ram_gb,
                warranty_months=offer.variant.warranty_months,
                condition=offer.variant.condition,
            )
            if policy.decision == PolicyDecision.DENY:
                rejected += 1
                continue
            warnings: list[str] = []
            if offer.discount_condition:
                warnings.append(f"条件优惠未计入可信总价：{offer.discount_condition}")
            description = str(offer.evidence.fields.get("description", ""))
            if "ignore company policy" in description.lower():
                warnings.append("检测到不可信网页指令，已作为商品数据隔离")
            score = self._score(offer, cost, policy.decision)
            candidates.append(
                Candidate(
                    offer=offer,
                    cost=cost,
                    requirement_fit=1.0,
                    policy_decision=policy.decision,
                    approval_route=policy.approval_route,
                    policy_reasons=policy.reasons,
                    evidence_refs=list(
                        dict.fromkeys(
                            [
                                *offer.match_decision.evidence_refs,
                                offer.evidence.evidence_id,
                                *policy.evidence_refs,
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
        selection_source = "deterministic_score"
        selection_rationale = "按固定成本、交付、供应商状态和保修权重选择"
        if agent_context and agent_context.proposed_candidates:
            candidate_by_key = {
                (candidate.offer.provider, candidate.offer.offer_id): candidate
                for candidate in candidates
            }
            for proposal in agent_context.proposed_candidates:
                proposal_key = (proposal.provider, proposal.offer_id)
                proposed = candidate_by_key.get(proposal_key)
                proposal_evidence = set(proposal.evidence_refs)
                catalog_evidence = (
                    {
                        *proposed.offer.match_decision.evidence_refs,
                        proposed.offer.evidence.evidence_id,
                    }
                    if proposed
                    else set()
                )
                evidence_is_valid = bool(
                    proposed
                    and proposal_key in agent_context.offers
                    and proposed.offer.evidence.evidence_id in proposal_evidence
                    and proposal_evidence.issubset(catalog_evidence)
                )
                if proposed and evidence_is_valid:
                    best = proposed
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
                        "reason": "candidate or required evidence did not pass server validation",
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

    async def _plan(self, run: ProcurementRun) -> AgentRuntimeContext:
        employee = self.enterprise.identity(run.tenant_id, run.user_id)
        context = AgentRuntimeContext(
            run=run,
            catalog=self.catalog,
            enterprise=self.enterprise,
            harness=self.harness,
            roles=employee.roles,
            context_assembler=self.context_assembler,
        )
        plan = await self.harness.execute_agent_turn(
            run,
            lambda: self.planner.plan(run.messages, context=context),
        )
        context.proposed_candidates = plan.candidate_proposals
        context.decision_rationale = plan.rationale
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
