"""Evidence coverage, deterministic qualification and bounded constraint negotiation."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from procure_pilot.domain.models import Money, PolicyDecision, ToolBudgetPool
from procure_pilot.domain.sourcing import (
    CandidateAssessment,
    ConstraintCheck,
    FactStatus,
    NegotiationAdjustment,
    NegotiationOption,
)
from procure_pilot.fixtures.offer_facts import FACT_TOOLS, fact_scope
from procure_pilot.gateway.tool_gateway import GatewayExecutionContext
from procure_pilot.services.costing import calculate_landed_cost


class EvidenceRequirementPolicy:
    @staticmethod
    def tools(run):
        tools = ["query_inventory", "query_warranty"]
        spec = run.requirement_spec
        if (
            run.requirement.delivery_days is not None
            or run.ranking_preference == "DELIVERY"
            or (spec and spec.delivery_days is not None)
        ):
            tools.append("estimate_delivery")
        return tools


def current_facts(run, offer):
    now = datetime.now(UTC)
    facts = {}
    for fact in run.fact_evidence:
        if (
            fact.provider == offer.provider
            and fact.offer_id == offer.offer_id
            and fact.scope == fact_scope(run, fact.kind)
            and fact.expires_at > now
        ):
            facts[fact.kind] = fact
    return facts


def fact_digest(run, offer):
    facts = current_facts(run, offer)
    payload = {
        FACT_TOOLS[t]: {
            "status": facts[FACT_TOOLS[t]].status,
            "value": facts[FACT_TOOLS[t]].value,
            "scope": facts[FACT_TOOLS[t]].scope,
        }
        if FACT_TOOLS[t] in facts
        else None
        for t in EvidenceRequirementPolicy.tools(run)
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


class SourcingEvidenceService:
    SHORTLIST_LIMIT = 6

    def __init__(self, engine):
        self.engine = engine

    def shortlist(self, run, offers, preferred_refs=()):
        baseline = self.engine.enterprise.policy["developer_laptop_baseline"]

        def distance(offer):
            req = run.requirement
            policy = self.policy(run, offer)
            return (
                policy["decision"] == PolicyDecision.DENY,
                offer.variant.ram_gb < int(baseline["minimum_ram_gb"]),
                sum(
                    (
                        bool(req.unit_budget and offer.unit_price.amount > req.unit_budget.amount),
                        bool(req.minimum_ram_gb and offer.variant.ram_gb < req.minimum_ram_gb),
                        bool(
                            req.minimum_storage_gb
                            and offer.variant.storage_gb < req.minimum_storage_gb
                        ),
                        offer.stock < (req.quantity or 1),
                        bool(req.delivery_days and offer.delivery_days > req.delivery_days),
                    )
                ),
                offer.unit_price.amount,
            )

        ordered = sorted(offers, key=distance)
        preferred = [o for o in ordered if (o.provider, o.offer_id) in preferred_refs]
        feasible = [o for o in ordered if not any(distance(o)[:3])]
        selected = []
        # Keep model proposals plus price / speed / utility extremes inside a six-offer limit.
        if feasible:
            preferred += [
                feasible[0],
                min(feasible, key=lambda o: o.delivery_days),
                max(
                    feasible,
                    key=lambda o: self.engine._score(
                        o,
                        calculate_landed_cost(o, run.requirement),
                        self.policy(run, o)["decision"],
                    ),
                ),
                min(
                    feasible,
                    key=lambda o: self.engine._score(
                        o,
                        calculate_landed_cost(o, run.requirement),
                        self.policy(run, o)["decision"],
                    ),
                ),
            ]
        for offer in [*preferred, *ordered]:
            if all((o.provider, o.offer_id) != (offer.provider, offer.offer_id) for o in selected):
                selected.append(offer)
            if len(selected) == self.SHORTLIST_LIMIT:
                break
        return selected

    async def enrich(self, run, offers, *, force=False, commit=False):
        employee = self.engine.enterprise.identity(run.tenant_id, run.user_id)
        for offer in offers:
            for tool in EvidenceRequirementPolicy.tools(run):
                await self.engine.tool_gateway.invoke(
                    tool_name=tool,
                    arguments={"provider": offer.provider, "offer_id": offer.offer_id},
                    context=GatewayExecutionContext(
                        run=run,
                        roles=employee.roles,
                        node_name="confirm_facts" if commit else "enrich_candidates",
                        budget_pool=ToolBudgetPool.COMMIT
                        if commit
                        else ToolBudgetPool.WORKFLOW_READ,
                        runtime_data={"force_refresh": force},
                    ),
                )
                # Save each fact for restart; journal saves completed calls independently too.
                if run.state.value in {"SOURCING", "REVIEWING", "REVALIDATING"}:
                    self.engine.repository.save(run)

    def assess(self, run, offer, policy, *, requirement=None):
        req = requirement or run.requirement
        facts = current_facts(run, offer)
        checks = []
        refs = [offer.evidence.evidence_id]

        def check(
            field, expected, actual, passed, *, authority="USER", adjustable=False, evidence=None
        ):
            checks.append(
                ConstraintCheck(
                    constraint_id=f"{field}:{len(checks)}",
                    field=field,
                    authority=authority,
                    expected=expected,
                    actual=actual,
                    result="UNKNOWN" if passed is None else "PASS" if passed else "FAIL",
                    evidence_refs=evidence or refs,
                    adjustable=adjustable,
                )
            )

        check(
            "currency",
            req.unit_budget.currency if req.unit_budget else "CNY",
            offer.unit_price.currency,
            req.unit_budget is None or req.unit_budget.currency == offer.unit_price.currency,
        )
        if req.unit_budget:
            check(
                "budget.maximum",
                str(req.unit_budget.amount),
                str(offer.unit_price.amount),
                offer.unit_price.amount <= req.unit_budget.amount,
                adjustable=True,
            )
        for field, actual in (
            ("minimum_ram_gb", offer.variant.ram_gb),
            ("minimum_storage_gb", offer.variant.storage_gb),
        ):
            expected = getattr(req, field)
            if expected is not None:
                check(field, expected, actual, actual >= expected, adjustable=True)
        check(
            "condition",
            req.condition.value,
            offer.variant.condition.value,
            offer.variant.condition == req.condition,
        )
        if req.operating_system:
            check(
                "operating_system",
                req.operating_system,
                offer.variant.operating_system,
                req.operating_system.lower() in offer.variant.operating_system.lower(),
            )
        baseline = self.engine.enterprise.policy["developer_laptop_baseline"]
        check(
            "minimum_ram_gb",
            int(baseline["minimum_ram_gb"]),
            offer.variant.ram_gb,
            offer.variant.ram_gb >= int(baseline["minimum_ram_gb"]),
            authority="POLICY",
        )
        for tool in EvidenceRequirementPolicy.tools(run):
            kind = FACT_TOOLS[tool]
            fact = facts.get(kind)
            value = fact.value if fact and fact.status == FactStatus.KNOWN else None
            evidence = fact.evidence_refs if fact else []
            if kind == "INVENTORY":
                actual = value["stock"] if value else None
                check(
                    "stock",
                    req.quantity,
                    actual,
                    actual >= req.quantity if actual is not None else None,
                    evidence=evidence,
                )
            elif kind == "DELIVERY":
                actual = value["days"] if value else None
                check(
                    "delivery_days",
                    req.delivery_days,
                    actual,
                    (req.delivery_days is None or actual <= req.delivery_days)
                    if actual is not None
                    else None,
                    adjustable=req.delivery_days is not None,
                    evidence=evidence,
                )
            elif kind == "WARRANTY":
                actual = value["months"] if value else None
                minimum = int(baseline["minimum_warranty_months"])
                check(
                    "minimum_warranty_months",
                    minimum,
                    actual,
                    actual >= minimum if actual is not None else None,
                    authority="POLICY",
                    evidence=evidence,
                )
                if req.minimum_warranty_months:
                    check(
                        "minimum_warranty_months",
                        req.minimum_warranty_months,
                        actual,
                        actual >= req.minimum_warranty_months if actual is not None else None,
                        evidence=evidence,
                    )
        # All supported hard constraints are enforced, including upper bounds and exact values.
        if run.requirement_spec:
            for c in run.requirement_spec.hard_constraints:
                if (
                    c.field in {"minimum_ram_gb", "minimum_storage_gb", "delivery_days"}
                    and requirement is not None
                ):
                    continue  # Alternative requirement has already applied these changes above.
                actual = {
                    "minimum_ram_gb": offer.variant.ram_gb,
                    "minimum_storage_gb": offer.variant.storage_gb,
                    "operating_system": offer.variant.operating_system,
                    "condition": offer.variant.condition.value,
                    "brand_preference": offer.product.brand,
                    "requested_model_text": offer.product.model,
                    "minimum_warranty_months": facts.get("WARRANTY").value["months"]
                    if facts.get("WARRANTY") and facts["WARRANTY"].value
                    else None,
                }.get(c.field)
                if c.field == "delivery_days":
                    actual = (
                        facts["DELIVERY"].value["days"]
                        if facts.get("DELIVERY") and facts["DELIVERY"].value
                        else None
                    )
                result = None
                if actual is not None:
                    if c.operator == "GTE":
                        result = actual >= c.value
                    elif c.operator == "LTE":
                        result = actual <= c.value
                    elif c.operator == "EQ":
                        result = (
                            str(c.value).lower() in str(actual).lower()
                            if isinstance(actual, str)
                            else actual == c.value
                        )
                    elif c.operator == "IN":
                        result = actual in c.value
                check(
                    c.field,
                    c.value,
                    actual,
                    result,
                    adjustable=c.field in {"minimum_ram_gb", "minimum_storage_gb", "delivery_days"},
                    evidence=(
                        facts["WARRANTY"].evidence_refs
                        if c.field == "minimum_warranty_months" and facts.get("WARRANTY")
                        else None
                    ),
                )
        check(
            "enterprise_policy",
            "ALLOW or ALLOW_WITH_APPROVAL",
            policy["decision"],
            policy["decision"] in {PolicyDecision.ALLOW, PolicyDecision.ALLOW_WITH_APPROVAL},
            authority="POLICY",
            evidence=policy["evidence_refs"],
        )
        return CandidateAssessment(
            provider=offer.provider,
            offer_id=offer.offer_id,
            requirement_version=run.requirement.version,
            checks=checks,
        )

    @staticmethod
    def passes(assessment):
        return all(c.result == "PASS" for c in assessment.checks)

    def policy(self, run, offer, requirement=None):
        req = requirement or run.requirement
        facts = current_facts(run, offer)
        warranty = facts.get("WARRANTY")
        months = (
            warranty.value["months"]
            if warranty and warranty.value
            else offer.variant.warranty_months
        )
        decision = self.engine.enterprise.evaluate(
            employee=self.engine.enterprise.identity(run.tenant_id, run.user_id),
            vendor_id=offer.vendor_id,
            landed_cost=calculate_landed_cost(offer, req).unconditional_total.amount,
            ram_gb=offer.variant.ram_gb,
            warranty_months=months,
            condition=offer.variant.condition,
        )
        return {
            "decision": decision.decision,
            "approval_route": decision.approval_route,
            "reasons": decision.reasons,
            "evidence_refs": decision.evidence_refs,
        }


class ConstraintNegotiationService:
    def __init__(self, evidence):
        self.evidence = evidence

    def propose(self, run, offers):
        if run.negotiation_rounds >= 3:
            return []
        options = []
        for offer in offers:
            assessment = self.evidence.assess(run, offer, self.evidence.policy(run, offer))
            failed = [c for c in assessment.checks if c.result != "PASS"]
            if any(
                c.field in {"minimum_ram_gb", "minimum_storage_gb"} and c.operator != "GTE"
                for c in (run.requirement_spec.hard_constraints if run.requirement_spec else [])
            ):
                continue
            if not failed or any(c.result == "UNKNOWN" or not c.adjustable for c in failed):
                continue
            changes = {c.field: c for c in failed}
            alternative = run.requirement.model_copy(deep=True)
            adjustments = []
            for field, c in changes.items():
                if field == "budget.maximum":
                    alternative.unit_budget = Money(
                        amount=Decimal(c.actual), currency=run.requirement.unit_budget.currency
                    )
                    previous, proposed = str(run.requirement.unit_budget.amount), str(c.actual)
                else:
                    previous, proposed = getattr(run.requirement, field), c.actual
                    # Never offer a lower RAM value than enterprise minimum.
                    if field == "minimum_ram_gb" and proposed < int(
                        self.evidence.engine.enterprise.policy["developer_laptop_baseline"][
                            "minimum_ram_gb"
                        ]
                    ):
                        break
                    setattr(alternative, field, proposed)
                adjustments.append(
                    NegotiationAdjustment(field=field, previous=previous, proposed=proposed)
                )
            else:
                validated = self.evidence.assess(
                    run,
                    offer,
                    self.evidence.policy(run, offer, alternative),
                    requirement=alternative,
                )
                if self.evidence.passes(validated):
                    refs = list(
                        dict.fromkeys(ref for c in validated.checks for ref in c.evidence_refs)
                    )
                    facts = current_facts(run, offer)
                    expiry = min(
                        [f.expires_at for f in facts.values()]
                        or [datetime.now(UTC) + timedelta(minutes=5)]
                    )
                    signature = [(a.field, str(a.proposed)) for a in adjustments]
                    if any(
                        [(a.field, str(a.proposed)) for a in o.adjustments] == signature
                        for o in options
                    ):
                        continue
                    options.append(
                        NegotiationOption(
                            option_id=f"neg_{uuid4().hex[:12]}",
                            base_requirement_version=run.requirement.version,
                            adjustments=adjustments,
                            validated_candidate_refs=[f"{offer.provider}/{offer.offer_id}"],
                            evidence_refs=refs,
                            expires_at=expiry,
                        )
                    )
            if len(options) == 3:
                break
        return options
