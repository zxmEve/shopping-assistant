from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from procure_pilot.domain.budget import (
    BudgetContext,
    BudgetReservationStatus,
    InvocationKind,
    ProviderKind,
)
from procure_pilot.domain.execution import ExecutionStatus, ToolCall
from procure_pilot.domain.models import (
    CatalogSearchResult,
    CostBreakdown,
    Offer,
    ProcurementDraft,
    ProcurementRun,
    RiskLevel,
    ToolBudgetPool,
)
from procure_pilot.domain.sourcing import OfferFact
from procure_pilot.fixtures.catalog import PROVIDERS, FixtureCatalogAdapter
from procure_pilot.fixtures.enterprise import FixtureEnterpriseGateway
from procure_pilot.fixtures.offer_facts import FACT_TOOLS, fact_scope, query_offer_fact
from procure_pilot.harness.runtime import ACTIVE_AGENT_EXECUTION_ID, AgentHarness
from procure_pilot.persistence.execution_repository import ExecutionRepositoryPort
from procure_pilot.services.budget import (
    BudgetLimitProvider,
    BudgetService,
    build_reservation_request,
)
from procure_pilot.services.costing import calculate_landed_cost

ArgsT = TypeVar("ArgsT", bound=BaseModel)
ResultT = TypeVar("ResultT")

DEFAULT_AGENT_TOOL_NAMES: dict[str, tuple[str, ...]] = {
    "REQUIREMENT": ("query_procurement_policy", "query_purchase_history"),
    "SOURCING": ("estimate_delivery", "query_inventory", "query_warranty", "search_catalog"),
}


class EmptyArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchCatalogArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["internal_catalog", "marketplace_alpha", "marketplace_beta"]


class OfferArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["internal_catalog", "marketplace_alpha", "marketplace_beta"]
    offer_id: str


class CreateDraftArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    business_idempotency_key: str
    offer_id: str
    requirement_version: int


class PolicyBatchArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    offer_keys: list[str]


@dataclass
class GatewayExecutionContext:
    run: ProcurementRun
    roles: tuple[str, ...]
    node_name: str
    budget_pool: ToolBudgetPool
    confirmed: bool = False
    agent_kind: str | None = None
    runtime_data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCostEstimate:
    reserved_micro_usd: int = 0
    actual_micro_usd: int | None = None
    pricing_version: str = "free-local-v1"


ToolHandler = Callable[[GatewayExecutionContext, ArgsT], Awaitable[ResultT] | ResultT]
ProviderResolver = Callable[[GatewayExecutionContext, ArgsT], tuple[ProviderKind, str]]
CostEstimator = Callable[[GatewayExecutionContext, ArgsT], ToolCostEstimate]


@dataclass(frozen=True)
class GatewayToolDefinition[ArgsT: BaseModel, ResultT]:
    name: str
    version: str
    arguments_model: type[ArgsT]
    result_model: type[ResultT] | None
    handler: ToolHandler[ArgsT, ResultT]
    policy_name: str
    risk: RiskLevel
    allowed_roles: frozenset[str]
    allowed_agents: frozenset[str] = frozenset()
    exposure: Literal["AGENT", "WORKFLOW", "INTERNAL"] = "WORKFLOW"
    provider_resolver: ProviderResolver[ArgsT] | None = None
    cost_estimator: CostEstimator[ArgsT] | None = None


class GatewayToolRegistry:
    def __init__(self, definitions: tuple[GatewayToolDefinition[Any, Any], ...]) -> None:
        self._definitions = {item.name: item for item in definitions}
        if len(self._definitions) != len(definitions):
            raise ValueError("Gateway tool definitions must have unique names")

    def get(self, tool_name: str) -> GatewayToolDefinition[Any, Any]:
        try:
            return self._definitions[tool_name]
        except KeyError as exc:
            raise PermissionError(f"Tool is not registered in the Gateway: {tool_name}") from exc

    def for_agent(self, agent_kind: str) -> tuple[GatewayToolDefinition[Any, Any], ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self._definitions.values()
                    if item.exposure == "AGENT" and agent_kind in item.allowed_agents
                ),
                key=lambda item: item.name,
            )
        )

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._definitions))


class ToolGateway:
    """The only runtime allowed to resolve and execute registered function tools."""

    def __init__(
        self,
        *,
        registry: GatewayToolRegistry,
        harness: AgentHarness,
        execution_repository: ExecutionRepositoryPort,
        budget_service: BudgetService,
        budget_limit_provider: BudgetLimitProvider,
        reservation_ttl_seconds: int = 300,
    ) -> None:
        self.registry = registry
        self.harness = harness
        self.execution_repository = execution_repository
        self.budget_service = budget_service
        self.budget_limit_provider = budget_limit_provider
        self.reservation_ttl_seconds = reservation_ttl_seconds

    async def invoke(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        context: GatewayExecutionContext,
        idempotency_key: str | None = None,
    ) -> Any:
        definition = self.registry.get(tool_name)
        try:
            self._authorize(definition, context)
        except PermissionError as exc:
            self.harness.trace(
                context.run,
                actor="Tool Gateway",
                kind="TOOL",
                name=definition.policy_name,
                detail={"tool": definition.name, "reason": str(exc)},
                status="REJECTED",
            )
            raise
        parsed = definition.arguments_model.model_validate(dict(arguments))
        if tool_name in {"query_procurement_policy", "query_purchase_history"}:
            reference_reader = getattr(self, "reference_reader", None)
            if reference_reader is not None:
                reference_reader._authorize(context.run, "POLICY" if tool_name == "query_procurement_policy" else "HISTORY")
            elif context.run.reference_request is None:
                raise PermissionError("Reference lookup is not currently authorized")
        fingerprint = self._fingerprint(
            {
                "tool": definition.name,
                "version": definition.version,
                "arguments": parsed.model_dump(mode="json"),
                "requirement_version": context.run.requirement.version,
                "scenario": context.run.scenario,
                **({"reference_authorization": context.run.reference_request.model_dump(mode="json") if context.run.reference_request else None}
                    if tool_name in {"query_procurement_policy", "query_purchase_history"} else {}),
            }
        )
        if tool_name in FACT_TOOLS:
            kind = FACT_TOOLS[tool_name]
            scope = fact_scope(context.run, kind)
            if not any(o.provider == parsed.provider and o.offer_id == parsed.offer_id for o in context.run.recalled_offers):
                raise ValueError("Offer must come from this Run's successful catalog recall")
            if not context.runtime_data.get("force_refresh"):
                cached = next((f for f in reversed(context.run.fact_evidence)
                               if f.provider == parsed.provider and f.offer_id == parsed.offer_id
                               and f.kind == kind and f.scope == scope and f.expires_at > datetime.now(UTC)
                               and (f.status.value == "KNOWN" or f.requirement_version == context.run.requirement.version)), None)
                if cached is not None:
                    self.harness.trace(context.run, actor="Evidence Service", kind="SERVICE", name="fact.cache_hit", detail={"fact_id": cached.fact_id, "tool": tool_name})
                    return cached
            freshness = (f"confirm:{context.run.confirmation_attempt}" if context.runtime_data.get("force_refresh") else str(int(datetime.now(UTC).timestamp()) // 300))
            fingerprint = self._fingerprint({"base": fingerprint, "scope": scope, "freshness": freshness})
        resolved_idempotency = idempotency_key or (
            f"gateway:{context.run.run_id}:{definition.name}:{fingerprint}"
        )
        current = self.execution_repository.get_tool(resolved_idempotency)
        if current is not None and current.status == ExecutionStatus.SUCCEEDED:
            if current.result_json is None:
                raise ValueError("Completed Gateway tool call is missing its result")
            self.harness.trace(
                context.run,
                actor="Tool Gateway",
                kind="TOOL",
                name="tool.result_reused",
                detail={"tool": tool_name, "tool_call_id": current.tool_call_id},
                status="RECOVERED",
            )
            self.harness.begin_tool_call(
                context.run,
                tool_name=definition.policy_name,
                roles=context.roles,
                confirmed=context.confirmed,
                budget_pool=context.budget_pool,
            )
            restored = self._restore_result(definition, current.result_json)
            if tool_name in FACT_TOOLS:
                if restored.expires_at <= datetime.now(UTC):
                    # Expiry at a bucket boundary starts a new execution; recovery within TTL reuses it.
                    return await self.invoke(tool_name=tool_name, arguments=arguments, context=context,
                        idempotency_key=resolved_idempotency + ":expired:" + restored.fact_id)
                if not any(f.fact_id == restored.fact_id for f in context.run.fact_evidence):
                    context.run.fact_evidence.append(restored)
            return restored

        provider_kind, provider_id = self._provider(definition, context, parsed)
        estimate = self._estimate(definition, context, parsed)
        proposed_call = ToolCall(
                tenant_id=context.run.tenant_id,
                user_id=context.run.user_id,
                run_id=context.run.run_id,
                node_name=context.node_name,
                agent_execution_id=ACTIVE_AGENT_EXECUTION_ID.get(),
                tool_name=definition.name,
                provider=provider_id,
                risk=definition.risk.value,
                budget_pool=context.budget_pool.value,
                input_fingerprint=fingerprint,
                idempotency_key=resolved_idempotency,
                arguments_json=parsed.model_dump(mode="json"),
            )
        claimed = self.execution_repository.claim_tool(proposed_call)
        if claimed.lease_owner != proposed_call.lease_owner:
            raise RuntimeError(
                f"Tool call is already leased by another worker: {claimed.tool_call_id}"
            )
        budget_context = BudgetContext(
            tenant_id=context.run.tenant_id,
            user_id=context.run.user_id,
            run_id=context.run.run_id,
            agent_execution_id=ACTIVE_AGENT_EXECUTION_ID.get(),
            tool_call_id=claimed.tool_call_id,
            invocation_kind=InvocationKind.TOOL,
            provider_kind=provider_kind,
            provider_id=provider_id,
        )
        reservation = None
        try:
            reservation = await self.budget_service.reserve(
                build_reservation_request(
                    context=budget_context,
                    idempotency_key=f"budget:{resolved_idempotency}",
                    estimated_cost_micro_usd=estimate.reserved_micro_usd,
                    pricing_version=estimate.pricing_version,
                    limit_provider=self.budget_limit_provider,
                    ttl_seconds=self.reservation_ttl_seconds,
                    metadata_json={"tool": definition.name, "tool_version": definition.version},
                )
            )
            if reservation.status != BudgetReservationStatus.RESERVED:
                raise RuntimeError(
                    "Tool budget reservation cannot be dispatched from "
                    f"{reservation.status.value}"
                )
            provider_invocation_id = f"pi_{uuid4().hex[:20]}"
            await self.budget_service.mark_dispatched(
                reservation.reservation_id,
                provider_invocation_id=provider_invocation_id,
            )

            async def operation() -> Any:
                value = definition.handler(context, parsed)
                return await value if inspect.isawaitable(value) else value

            result = await self.harness.execute_tool(
                context.run,
                tool_name=definition.policy_name,
                roles=context.roles,
                confirmed=context.confirmed,
                provider=(provider_id if provider_kind != ProviderKind.LOCAL else None),
                budget_pool=context.budget_pool,
                operation=operation,
            )
            actual = (
                estimate.actual_micro_usd
                if estimate.actual_micro_usd is not None
                else estimate.reserved_micro_usd
            )
            await self.budget_service.settle(
                reservation.reservation_id,
                actual_cost_micro_usd=actual,
                settlement_basis="TOOL_PRICE_CATALOG",
            )
            payload = self._dump_result(result)
            self.execution_repository.complete_tool(
                claimed.tool_call_id,
                payload,
                expected_fencing_token=claimed.fencing_token,
            )
            return result
        except Exception as exc:
            if reservation is not None:
                if reservation.reserved_amount == 0:
                    await self.budget_service.settle(
                        reservation.reservation_id,
                        actual_cost_micro_usd=0,
                        settlement_basis="ZERO_COST_FAILURE",
                    )
                else:
                    await self.budget_service.mark_reconciling(
                        reservation.reservation_id,
                        reason=f"{type(exc).__name__}: provider result unknown",
                    )
            self.execution_repository.fail_tool(
                claimed.tool_call_id,
                f"{type(exc).__name__}: {exc}",
                expected_fencing_token=claimed.fencing_token,
            )
            raise

    @staticmethod
    def _authorize(
        definition: GatewayToolDefinition[Any, Any], context: GatewayExecutionContext
    ) -> None:
        if not set(context.roles).intersection(definition.allowed_roles):
            raise PermissionError(f"Current role is not allowed to use tool: {definition.name}")
        if context.agent_kind is not None and context.agent_kind not in definition.allowed_agents:
            raise PermissionError(
                f"Agent {context.agent_kind} is not allowed to use tool: {definition.name}"
            )
        if definition.risk == RiskLevel.BUSINESS_WRITE and not context.confirmed:
            raise PermissionError("BUSINESS_WRITE requires explicit confirmation")

    @staticmethod
    def _provider(
        definition: GatewayToolDefinition[Any, Any],
        context: GatewayExecutionContext,
        arguments: BaseModel,
    ) -> tuple[ProviderKind, str]:
        if definition.provider_resolver is None:
            return ProviderKind.LOCAL, "local"
        return definition.provider_resolver(context, arguments)

    @staticmethod
    def _estimate(
        definition: GatewayToolDefinition[Any, Any],
        context: GatewayExecutionContext,
        arguments: BaseModel,
    ) -> ToolCostEstimate:
        if definition.cost_estimator is None:
            return ToolCostEstimate()
        return definition.cost_estimator(context, arguments)

    @staticmethod
    def _dump_result(result: Any) -> dict[str, Any]:
        if isinstance(result, BaseModel):
            return {"value": result.model_dump(mode="json")}
        if hasattr(result, "__dict__"):
            return {"value": dict(result.__dict__)}
        return {"value": result}

    @staticmethod
    def _restore_result(
        definition: GatewayToolDefinition[Any, Any], payload: dict[str, Any]
    ) -> Any:
        value = payload.get("value")
        if definition.result_model is None:
            return value
        model = definition.result_model
        if isinstance(model, type) and issubclass(model, BaseModel):
            return model.model_validate(value)
        return value

    @staticmethod
    def _fingerprint(payload: dict[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()


def build_default_tool_registry(
    *,
    catalog: FixtureCatalogAdapter,
    enterprise: FixtureEnterpriseGateway,
    provider_costs_micro_usd: Mapping[str, int] | None = None,
    reference_reader=None,
) -> GatewayToolRegistry:
    costs = dict(provider_costs_micro_usd or {})
    if reference_reader is None:
        from procure_pilot.fixtures.purchase_history import FixturePurchaseHistoryAdapter
        from procure_pilot.services.requirement_references import RequirementReferenceReader
        reference_reader = RequirementReferenceReader(enterprise, FixturePurchaseHistoryAdapter())

    def agents_for(tool_name: str) -> frozenset[str]:
        return frozenset(
            agent for agent, names in DEFAULT_AGENT_TOOL_NAMES.items() if tool_name in names
        )

    async def inspect_requirement(
        context: GatewayExecutionContext, _arguments: EmptyArguments
    ) -> dict[str, Any]:
        requirement = context.run.requirement
        required = (
            "quantity",
            "unit_budget",
            "budget_tax_included",
            "delivery_city",
            "minimum_ram_gb",
            "minimum_storage_gb",
        )
        missing = [name for name in required if getattr(requirement, name) is None]
        return {
            "requirement_version": requirement.version,
            "requirement": requirement.model_dump(mode="json"),
            "missing_fields": missing,
            "ready_for_sourcing": not missing,
        }

    async def query_policy(context, _arguments):
        return reference_reader.policy(context.run)

    async def query_history(context, _arguments):
        return reference_reader.purchase_history(context.run)

    async def get_budget(
        context: GatewayExecutionContext, _arguments: EmptyArguments
    ) -> dict[str, Any]:
        employee = enterprise.identity(context.run.tenant_id, context.run.user_id)
        budget = enterprise.budget(employee)
        return {
            "cost_center": budget.cost_center,
            "available": str(budget.available),
            "currency": budget.currency,
            "version": budget.version,
            "evidence_ref": enterprise.BUDGET_REF,
        }

    async def search_catalog(
        context: GatewayExecutionContext, arguments: SearchCatalogArguments
    ) -> CatalogSearchResult:
        requirement = context.run.requirement
        from procure_pilot.services.requirement_references import assert_reference_ready
        assert_reference_ready(context.run)
        from procure_pilot.services.requirement_validation import RequirementCompletenessPolicy

        if requirement.quantity is None or RequirementCompletenessPolicy.unresolved_configuration(requirement):
            raise ValueError("Requirement is incomplete; clarify it before catalog search")
        return await catalog.search(
            arguments.provider,
            # An absent user limit does not remove the enterprise floor. Apply the floor
            # before catalog truncation so cheap, disallowed 16GB offers cannot crowd it out.
            minimum_ram_gb=(
                requirement.minimum_ram_gb if requirement.minimum_ram_gb is not None
                else int(enterprise.policy["developer_laptop_baseline"]["minimum_ram_gb"])
            ),
            minimum_storage_gb=requirement.minimum_storage_gb,
            quantity=requirement.quantity,
            scenario=context.run.scenario,
            include_near_matches=True,
        )

    def fact_handler(kind):
        async def handler(context, arguments):
            fact = await query_offer_fact(catalog, run=context.run, provider=arguments.provider, offer_id=arguments.offer_id, kind=kind)
            context.run.fact_evidence.append(fact)
            return fact
        return handler

    async def landed_cost(
        context: GatewayExecutionContext, arguments: OfferArguments
    ) -> CostBreakdown:
        offers: dict[tuple[str, str], Offer] = context.runtime_data.get("offers", {})
        try:
            offer = offers[(arguments.provider, arguments.offer_id)]
        except KeyError as exc:
            raise ValueError("Offer must come from a successful Gateway catalog search") from exc
        return calculate_landed_cost(offer, context.run.requirement)

    async def evaluate_policy(
        context: GatewayExecutionContext, arguments: OfferArguments
    ) -> dict[str, Any]:
        offers: dict[tuple[str, str], Offer] = context.runtime_data.get("offers", {})
        try:
            offer = offers[(arguments.provider, arguments.offer_id)]
        except KeyError as exc:
            raise ValueError("Offer must come from a successful Gateway catalog search") from exc
        cost = calculate_landed_cost(offer, context.run.requirement)
        employee = enterprise.identity(context.run.tenant_id, context.run.user_id)
        decision = enterprise.evaluate(
            employee=employee,
            vendor_id=offer.vendor_id,
            landed_cost=cost.unconditional_total.amount,
            ram_gb=offer.variant.ram_gb,
            warranty_months=offer.variant.warranty_months,
            condition=offer.variant.condition,
        )
        return {
            "decision": decision.decision.value,
            "approval_route": decision.approval_route,
            "reasons": decision.reasons,
            "evidence_refs": decision.evidence_refs,
        }

    async def evaluate_policy_batch(
        context: GatewayExecutionContext, arguments: PolicyBatchArguments
    ) -> dict[str, dict[str, Any]]:
        offers: dict[tuple[str, str], Offer] = context.runtime_data.get("offers", {})
        employee = enterprise.identity(context.run.tenant_id, context.run.user_id)
        decisions: dict[str, dict[str, Any]] = {}
        for key in arguments.offer_keys:
            provider_id, offer_id = key.split("/", 1)
            try:
                offer = offers[(provider_id, offer_id)]
            except KeyError as exc:
                raise ValueError("Policy batch contains an offer not returned by the Gateway") from exc
            cost = calculate_landed_cost(offer, context.run.requirement)
            decision = enterprise.evaluate(
                employee=employee,
                vendor_id=offer.vendor_id,
                landed_cost=cost.unconditional_total.amount,
                ram_gb=offer.variant.ram_gb,
                warranty_months=offer.variant.warranty_months,
                condition=offer.variant.condition,
            )
            decisions[key] = {
                "decision": decision.decision.value,
                "approval_route": decision.approval_route,
                "reasons": decision.reasons,
                "evidence_refs": decision.evidence_refs,
            }
        return decisions

    async def revalidate_quote(
        context: GatewayExecutionContext, arguments: OfferArguments
    ) -> Offer:
        return await catalog.get_offer(
            arguments.provider, arguments.offer_id, scenario=context.run.scenario
        )

    async def create_draft(
        context: GatewayExecutionContext, arguments: CreateDraftArguments
    ) -> ProcurementDraft:
        candidate = context.runtime_data.get("candidate")
        if candidate is None:
            raise ValueError("Draft creation requires a server-validated candidate")
        return ProcurementDraft(
            draft_id=f"draft_{arguments.business_idempotency_key[:12]}",
            run_id=context.run.run_id,
            idempotency_key=arguments.business_idempotency_key,
            candidate=candidate,
            approval_route=candidate.approval_route,
        )

    def provider(
        _context: GatewayExecutionContext, arguments: BaseModel
    ) -> tuple[ProviderKind, str]:
        provider_id = str(arguments.provider)
        if provider_id not in PROVIDERS:
            raise ValueError(f"Unknown catalog provider: {provider_id}")
        return ProviderKind.CATALOG, provider_id

    def provider_cost(
        _context: GatewayExecutionContext, arguments: BaseModel
    ) -> ToolCostEstimate:
        provider_id = str(arguments.provider)
        amount = int(costs.get(provider_id, 0))
        return ToolCostEstimate(
            reserved_micro_usd=amount,
            actual_micro_usd=amount,
            pricing_version=f"configured-tool-price:{provider_id}:v1",
        )

    employee_roles = frozenset({"EMPLOYEE", "PROCUREMENT_SPECIALIST"})
    from procure_pilot.domain.references import ReferenceLookupResult
    definitions: tuple[GatewayToolDefinition[Any, Any], ...] = (
        *(GatewayToolDefinition(name=name, version="v1", arguments_model=EmptyArguments,
            result_model=ReferenceLookupResult, handler=handler, policy_name=name, risk=RiskLevel.READ,
            allowed_roles=employee_roles, allowed_agents=agents_for(name), exposure="AGENT")
          for name,handler in (("query_procurement_policy",query_policy),("query_purchase_history",query_history))),
        GatewayToolDefinition(
            name="inspect_procurement_requirement",
            version="v1",
            arguments_model=EmptyArguments,
            result_model=None,
            handler=inspect_requirement,
            policy_name="inspect_requirement",
            risk=RiskLevel.READ,
            allowed_roles=employee_roles,
            allowed_agents=agents_for("inspect_procurement_requirement"),
            exposure="INTERNAL",
        ),
        GatewayToolDefinition(
            name="get_budget",
            version="v1",
            arguments_model=EmptyArguments,
            result_model=None,
            handler=get_budget,
            policy_name="get_budget",
            risk=RiskLevel.READ,
            allowed_roles=employee_roles,
        ),
        GatewayToolDefinition(
            name="search_catalog",
            version="v1",
            arguments_model=SearchCatalogArguments,
            result_model=CatalogSearchResult,
            handler=search_catalog,
            policy_name="search_catalog",
            risk=RiskLevel.READ,
            allowed_roles=employee_roles,
            allowed_agents=agents_for("search_catalog"),
            exposure="AGENT",
            provider_resolver=provider,
            cost_estimator=provider_cost,
        ),
        *(GatewayToolDefinition(name=name, version="v1", arguments_model=OfferArguments,
              result_model=OfferFact, handler=fact_handler(kind), policy_name=name, risk=RiskLevel.READ,
              allowed_roles=employee_roles, allowed_agents=agents_for(name), exposure="AGENT",
              provider_resolver=provider, cost_estimator=provider_cost)
          for name, kind in FACT_TOOLS.items()),
        GatewayToolDefinition(
            name="calculate_landed_cost",
            version="v1",
            arguments_model=OfferArguments,
            result_model=CostBreakdown,
            handler=landed_cost,
            policy_name="calculate_landed_cost",
            risk=RiskLevel.READ,
            allowed_roles=employee_roles,
        ),
        GatewayToolDefinition(
            name="evaluate_policy",
            version="v1",
            arguments_model=OfferArguments,
            result_model=None,
            handler=evaluate_policy,
            policy_name="evaluate_policy",
            risk=RiskLevel.READ,
            allowed_roles=employee_roles,
        ),
        GatewayToolDefinition(
            name="evaluate_policy_batch",
            version="v1",
            arguments_model=PolicyBatchArguments,
            result_model=None,
            handler=evaluate_policy_batch,
            policy_name="evaluate_policy",
            risk=RiskLevel.READ,
            allowed_roles=employee_roles,
            exposure="INTERNAL",
        ),
        GatewayToolDefinition(
            name="revalidate_quote",
            version="v1",
            arguments_model=OfferArguments,
            result_model=Offer,
            handler=revalidate_quote,
            policy_name="revalidate_quote",
            risk=RiskLevel.READ,
            allowed_roles=employee_roles,
            provider_resolver=provider,
            cost_estimator=provider_cost,
        ),
        GatewayToolDefinition(
            name="create_requisition_draft",
            version="v1",
            arguments_model=CreateDraftArguments,
            result_model=ProcurementDraft,
            handler=create_draft,
            policy_name="create_requisition_draft",
            risk=RiskLevel.BUSINESS_WRITE,
            allowed_roles=employee_roles,
        ),
    )
    return GatewayToolRegistry(definitions)
