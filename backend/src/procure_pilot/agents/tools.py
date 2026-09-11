from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from agents import FunctionTool, RunContextWrapper, function_tool
from pydantic import BaseModel, Field

from procure_pilot.domain.models import (
    CatalogSearchResult,
    CostBreakdown,
    Offer,
    PolicyDecision,
    ProcurementRun,
    ToolBudgetPool,
)

if TYPE_CHECKING:
    from procure_pilot.fixtures.catalog import FixtureCatalogAdapter
    from procure_pilot.fixtures.enterprise import FixtureEnterpriseGateway
    from procure_pilot.gateway.model_gateway import ModelGateway
    from procure_pilot.gateway.tool_gateway import ToolGateway
    from procure_pilot.harness.runtime import AgentHarness
    from procure_pilot.services.context import ContextAssembler


ProviderName = Literal["internal_catalog", "marketplace_alpha", "marketplace_beta"]


def safe_tool_error(_wrapper: RunContextWrapper[Any], error: Exception) -> str:
    if isinstance(error, ValueError) and "Offer must come from" in str(error):
        return "Tool rejected the offer_id because it was not returned by search_catalog."
    guarded_rejection = isinstance(error, ValueError) and any(
        phrase in str(error)
        for phrase in ("already completed", "already requested", "limit already reached")
    )
    if guarded_rejection:
        context = getattr(_wrapper, "context", None)
        tool_name = str(getattr(_wrapper, "tool_name", "unknown"))
        try:
            arguments = json.loads(str(getattr(_wrapper, "tool_arguments", "{}")))
        except json.JSONDecodeError:
            arguments = {}
        provider = arguments.get("provider", "")
        offer_id = arguments.get("offer_id", "")
        if isinstance(context, AgentRuntimeContext):
            context.harness.trace(
                context.run,
                actor="Agent Harness",
                kind="HARNESS",
                name="agent.tool_call_rejected",
                detail={
                    "tool": tool_name,
                    "provider": provider,
                    "offer_id": offer_id,
                    "fingerprint": f"{tool_name}:{provider}:{offer_id}",
                    "reason": str(error),
                },
                status="REJECTED",
            )
        return f"{error} Return the result already obtained and do not call this tool again."
    return (
        "Tool input was invalid or the trusted data source was unavailable. Do not invent a result."
    )


class AgentCandidateProposal(BaseModel):
    provider: ProviderName
    offer_id: str
    evidence_refs: list[str] = Field(min_length=1)


@dataclass
class AgentRuntimeContext:
    run: ProcurementRun
    catalog: FixtureCatalogAdapter
    enterprise: FixtureEnterpriseGateway
    harness: AgentHarness
    roles: tuple[str, ...]
    tool_gateway: ToolGateway | None = None
    model_gateway: ModelGateway | None = None
    agent_kind: str | None = None
    context_assembler: ContextAssembler | None = None
    requirement_inspected: bool = False
    budget_checked: bool = False
    budget_lookup_started: bool = False
    catalog_results: dict[str, CatalogSearchResult] = field(default_factory=dict)
    catalog_requested_providers: set[str] = field(default_factory=set)
    offers: dict[tuple[str, str], Offer] = field(default_factory=dict)
    cost_checked_offers: set[tuple[str, str]] = field(default_factory=set)
    cost_requested_offers: set[tuple[str, str]] = field(default_factory=set)
    policy_checked_offers: set[tuple[str, str]] = field(default_factory=set)
    policy_evaluation_started: bool = False
    proposed_candidates: list[AgentCandidateProposal] = field(default_factory=list)
    decision_rationale: str = ""

    def require_gateway(self) -> ToolGateway:
        if self.tool_gateway is None:
            from procure_pilot.gateway.tool_gateway import ToolGateway, build_default_tool_registry
            from procure_pilot.persistence.budget_repository import InMemoryBudgetRepository
            from procure_pilot.persistence.execution_repository import InMemoryExecutionRepository
            from procure_pilot.services.budget import (
                InMemoryBudgetService,
                StaticBudgetLimitProvider,
            )

            limit_provider = StaticBudgetLimitProvider(
                platform_daily=10_000_000_000,
                platform_monthly=100_000_000_000,
                tenant_monthly=50_000_000_000,
                user_daily=5_000_000_000,
            )
            self.tool_gateway = ToolGateway(
                registry=build_default_tool_registry(
                    catalog=self.catalog,
                    enterprise=self.enterprise,
                ),
                harness=self.harness,
                execution_repository=InMemoryExecutionRepository(),
                budget_service=InMemoryBudgetService(
                    InMemoryBudgetRepository(), limit_provider=limit_provider
                ),
                budget_limit_provider=limit_provider,
            )
        return self.tool_gateway

    def gateway_context(self, *, budget_pool: ToolBudgetPool) -> Any:
        self.require_gateway()
        from procure_pilot.gateway.tool_gateway import GatewayExecutionContext

        return GatewayExecutionContext(
            run=self.run,
            roles=self.roles,
            node_name=f"agent:{self.agent_kind or 'unknown'}",
            budget_pool=budget_pool,
            agent_kind=self.agent_kind,
            runtime_data={"offers": self.offers},
        )

    def remember_catalog_result(self, result: CatalogSearchResult) -> None:
        self.catalog_results[result.provider] = result
        for offer in result.offers:
            self.offers[(offer.provider, offer.offer_id)] = offer

    def require_offer(self, provider: str, offer_id: str) -> Offer:
        try:
            return self.offers[(provider, offer_id)]
        except KeyError as exc:
            raise ValueError(
                "Offer must come from a successful search_catalog tool result in this run"
            ) from exc


class RequirementToolResult(BaseModel):
    requirement_version: int
    requirement: dict[str, Any]
    missing_fields: list[str]
    ready_for_sourcing: bool


class BudgetToolResult(BaseModel):
    cost_center: str
    available: str
    currency: str
    version: int
    evidence_ref: str


class AgentOfferSummary(BaseModel):
    offer_id: str
    provider: str
    product_id: str
    variant_id: str
    brand: str
    model: str
    ram_gb: int
    storage_gb: int
    unit_price: str
    currency: str
    stock: int
    delivery_days: int
    match_confidence: float
    evidence_refs: list[str]


class CatalogToolResult(BaseModel):
    provider: str
    retrieved_count: int
    verified_offer_count: int
    excluded_offer_count: int
    offers: list[AgentOfferSummary] = Field(max_length=6)


class CostToolResult(BaseModel):
    provider: str
    offer_id: str
    quantity: int
    breakdown: CostBreakdown
    discount_condition: str | None = None


class PolicyToolResult(BaseModel):
    provider: str
    offer_id: str
    decision: PolicyDecision
    approval_route: list[str]
    reasons: list[str]
    evidence_refs: list[str]


def build_function_tools(*, dynamic_availability: bool = True) -> dict[str, FunctionTool]:
    """Build the read-only SDK tools used by specialists in OpenAI mode."""

    def requirement_available(wrapper: RunContextWrapper[AgentRuntimeContext], _agent: Any) -> bool:
        return not wrapper.context.requirement_inspected

    def budget_available(wrapper: RunContextWrapper[AgentRuntimeContext], _agent: Any) -> bool:
        return not wrapper.context.budget_lookup_started

    def catalog_available(wrapper: RunContextWrapper[AgentRuntimeContext], _agent: Any) -> bool:
        return len(wrapper.context.catalog_requested_providers) < 3

    def cost_available(wrapper: RunContextWrapper[AgentRuntimeContext], _agent: Any) -> bool:
        return len(wrapper.context.cost_requested_offers) < 3

    def policy_available(wrapper: RunContextWrapper[AgentRuntimeContext], _agent: Any) -> bool:
        return bool(wrapper.context.offers) and not wrapper.context.policy_evaluation_started

    @function_tool(
        name_override="inspect_procurement_requirement",
        strict_mode=True,
        failure_error_function=safe_tool_error,
        is_enabled=requirement_available if dynamic_availability else True,
    )
    async def inspect_requirement(
        wrapper: RunContextWrapper[AgentRuntimeContext],
    ) -> RequirementToolResult:
        """Read the server-owned requirement and report fields still required for sourcing."""
        context = wrapper.context
        if context.requirement_inspected:
            raise ValueError("Requirement inspection already completed for this Agent run.")
        payload = await context.require_gateway().invoke(
            tool_name="inspect_procurement_requirement",
            arguments={},
            context=context.gateway_context(budget_pool=ToolBudgetPool.AGENT_READ),
        )
        result = RequirementToolResult.model_validate(payload)
        missing = result.missing_fields
        requirement = context.run.requirement
        context.requirement_inspected = True
        context.harness.trace(
            context.run,
            actor="Requirement Agent",
            kind="TOOL",
            name="inspect_requirement",
            detail={"missing_fields": missing, "requirement_version": requirement.version},
        )
        return result

    @function_tool(
        name_override="get_budget",
        strict_mode=True,
        failure_error_function=safe_tool_error,
        is_enabled=budget_available if dynamic_availability else True,
    )
    async def get_budget(
        wrapper: RunContextWrapper[AgentRuntimeContext],
    ) -> BudgetToolResult:
        """Read the authenticated employee's server-owned cost-center budget snapshot."""
        context = wrapper.context
        if context.budget_lookup_started:
            raise ValueError("Budget lookup already completed for this Agent run.")
        context.budget_lookup_started = True
        payload = await context.require_gateway().invoke(
            tool_name="get_budget",
            arguments={},
            context=context.gateway_context(budget_pool=ToolBudgetPool.AGENT_READ),
        )
        result = BudgetToolResult.model_validate(payload)
        context.budget_checked = True
        context.harness.trace(
            context.run,
            actor="Budget Service",
            kind="TOOL",
            name="get_budget",
            detail={
                "cost_center": result.cost_center,
                "version": result.version,
                "evidence_ref": result.evidence_ref,
            },
        )
        return result

    @function_tool(
        name_override="search_catalog",
        strict_mode=True,
        failure_error_function=safe_tool_error,
        is_enabled=catalog_available if dynamic_availability else True,
    )
    async def search_catalog(
        wrapper: RunContextWrapper[AgentRuntimeContext],
        provider: ProviderName,
    ) -> CatalogToolResult:
        """Search one approved provider using the current server-owned requirement."""
        context = wrapper.context
        requirement = context.run.requirement
        if (
            requirement.quantity is None
            or requirement.minimum_ram_gb is None
            or requirement.minimum_storage_gb is None
        ):
            raise ValueError("Requirement is incomplete; inspect and clarify it before sourcing")
        if provider in context.catalog_requested_providers:
            raise ValueError(f"Catalog provider {provider} was already requested.")
        context.catalog_requested_providers.add(provider)
        result = await context.require_gateway().invoke(
            tool_name="search_catalog",
            arguments={"provider": provider},
            context=context.gateway_context(budget_pool=ToolBudgetPool.AGENT_READ),
        )
        context.remember_catalog_result(result)
        summaries = [
            AgentOfferSummary(
                offer_id=offer.offer_id,
                provider=offer.provider,
                product_id=offer.product.product_id,
                variant_id=offer.variant.variant_id,
                brand=offer.product.brand,
                model=offer.product.model,
                ram_gb=offer.variant.ram_gb,
                storage_gb=offer.variant.storage_gb,
                unit_price=str(offer.unit_price.amount),
                currency=offer.unit_price.currency,
                stock=offer.stock,
                delivery_days=offer.delivery_days,
                match_confidence=offer.match_confidence,
                evidence_refs=list(
                    dict.fromkeys([*offer.match_decision.evidence_refs, offer.evidence.evidence_id])
                ),
            )
            for offer in result.offers[:6]
        ]
        context.harness.trace(
            context.run,
            actor="Sourcing Agent",
            kind="TOOL",
            name="search_catalog",
            detail={
                "provider": provider,
                "retrieved": result.retrieved_count,
                "verified_offers": len(result.offers),
                "excluded_offers": len(result.assessments),
            },
        )
        return CatalogToolResult(
            provider=provider,
            retrieved_count=result.retrieved_count,
            verified_offer_count=len(result.offers),
            excluded_offer_count=len(result.assessments),
            offers=summaries,
        )

    @function_tool(
        name_override="calculate_landed_cost",
        strict_mode=True,
        failure_error_function=safe_tool_error,
        is_enabled=cost_available if dynamic_availability else True,
    )
    async def landed_cost(
        wrapper: RunContextWrapper[AgentRuntimeContext],
        provider: ProviderName,
        offer_id: str,
    ) -> CostToolResult:
        """Calculate exact landed cost for an offer returned by search_catalog."""
        context = wrapper.context
        offer = context.require_offer(provider, offer_id)
        offer_key = (provider, offer_id)
        if offer_key in context.cost_requested_offers:
            raise ValueError(f"Landed cost for {provider}/{offer_id} was already requested.")
        if len(context.cost_requested_offers) >= 3:
            raise ValueError("Landed-cost call limit already reached for this Agent run.")
        context.cost_requested_offers.add(offer_key)
        breakdown = await context.require_gateway().invoke(
            tool_name="calculate_landed_cost",
            arguments={"provider": provider, "offer_id": offer_id},
            context=context.gateway_context(budget_pool=ToolBudgetPool.AGENT_READ),
        )
        context.cost_checked_offers.add((provider, offer_id))
        context.harness.trace(
            context.run,
            actor="Cost Service",
            kind="TOOL",
            name="calculate_landed_cost",
            detail={
                "provider": provider,
                "offer_id": offer_id,
                "unconditional_total": str(breakdown.unconditional_total.amount),
            },
        )
        return CostToolResult(
            provider=provider,
            offer_id=offer_id,
            quantity=context.run.requirement.quantity or 0,
            breakdown=breakdown,
            discount_condition=offer.discount_condition,
        )

    @function_tool(
        name_override="evaluate_policy",
        strict_mode=True,
        failure_error_function=safe_tool_error,
        is_enabled=policy_available if dynamic_availability else True,
    )
    async def evaluate_policy(
        wrapper: RunContextWrapper[AgentRuntimeContext],
        provider: ProviderName,
        offer_id: str,
    ) -> PolicyToolResult:
        """Evaluate budget, vendor and procurement policy for a searched canonical offer."""
        context = wrapper.context
        context.require_offer(provider, offer_id)
        if context.policy_evaluation_started:
            raise ValueError("Policy evaluation already completed for this Agent run.")
        context.policy_evaluation_started = True
        payload = await context.require_gateway().invoke(
            tool_name="evaluate_policy",
            arguments={"provider": provider, "offer_id": offer_id},
            context=context.gateway_context(budget_pool=ToolBudgetPool.AGENT_READ),
        )
        result = PolicyToolResult.model_validate(
            {"provider": provider, "offer_id": offer_id, **payload}
        )
        context.policy_checked_offers.add((provider, offer_id))
        context.harness.trace(
            context.run,
            actor="Policy Rule Service",
            kind="TOOL",
            name="evaluate_policy",
            detail={
                "provider": provider,
                "offer_id": offer_id,
                "decision": result.decision,
                "evidence_refs": result.evidence_refs,
            },
        )
        return result

    return {
        tool.name: tool
        for tool in (
            inspect_requirement,
            get_budget,
            search_catalog,
            landed_cost,
            evaluate_policy,
        )
    }
