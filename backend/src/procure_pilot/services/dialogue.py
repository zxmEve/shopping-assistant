"""Versioned post-recommendation turns; natural language never authorizes a write."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import uuid4

from procure_pilot.agents.tools import AgentRuntimeContext
from procure_pilot.domain.conversation import (
    DialogueProposal,
    DialogueSelection,
    TurnDecision,
    TurnIntent,
)
from procure_pilot.domain.execution import AgentExecution, ExecutionStatus
from procure_pilot.domain.models import Message, RunResponse
from procure_pilot.domain.requirements import (
    ConstraintStrength,
    NormalizedMoneyValue,
    RequirementAuthority,
    RequirementConstraint,
    RequirementDelta,
    RequirementDeltaOperation,
    RequirementEvidence,
    SourcedMoneyValue,
    SourcedRequirementValue,
)
from procure_pilot.services.sourcing import current_facts, fact_digest


def classify_turn_rules(message):
    text = re.sub(r"\s+", "", message.lower())
    proposal = dict(intent="UPDATE_REQUIREMENT", confidence=0.9)
    if re.search(r"接受|同意|采用", text) and re.search(r"方案", text):
        proposal.update(intent="ACCEPT_NEGOTIATION", target_reference=reference(text, "方案"))
    elif re.search(
        r"(?:预算|数量|内存|硬盘|存储|交期|到货|保修|质保|地址|城市).*(?:改|调整|至少|最低|必须)|\d+(?:台|gb|tb|元)",
        text,
    ):
        pass
    elif re.search(r"对比|比较|区别|差别", text):
        proposal.update(intent="COMPARE")
    elif re.search(r"为什么|解释|理由|依据|怎么样|多少|库存|保修(?!优先)|质保|交期|到货", text):
        proposal.update(intent="EXPLAIN", target_reference=reference(text, "款"))
    elif re.search(
        r"便宜|低价|省钱|价格优先|成本优先|速度优先|交付优先|快一点|快些|快点|保修优先|综合优先|均衡",
        text,
    ):
        pref = (
            "PRICE"
            if re.search(r"便宜|低价|省钱|价格|成本", text)
            else "WARRANTY"
            if "保修" in text
            else "BALANCED"
            if re.search(r"综合|均衡", text)
            else "DELIVERY"
        )
        proposal.update(intent="UPDATE_PREFERENCE", preference=pref)
    elif re.search(r"(?:选|换成|采用|就要).*(?:第?[一二三123]款|最低成本|最快交付|综合最优)", text):
        proposal.update(intent="SELECT_CANDIDATE", target_reference=reference(text, "款"))
    elif re.search(r"这个|那个|这款|那款|换一个|换个", text):
        proposal.update(intent="CLARIFY", confidence=0.4)
    return DialogueProposal(**proposal)


def reference(text, noun):
    if noun == "款":
        for label, ref in (("综合最优", "1"), ("最低成本", "2"), ("最快交付", "3")):
            if label in text:
                return ref
    match = re.search(r"(?:第?([一二三123])" + noun + r"|" + noun + r"([一二三123]))", text)
    if match:
        value = match[1] or match[2]
        return str({"一": 1, "二": 2, "三": 3}.get(value, value))
    match = re.search(r"(?:neg_|offer_)[a-z0-9_]+", text)
    return match[0] if match else None


async def model_dialogue(engine, run, phase, payload, *, message_id):
    method = getattr(engine.planner, "dialogue", None)
    if method is None:
        return None
    model = DialogueProposal if phase == "INTENT" else DialogueSelection
    key = f"dialogue:{run.run_id}:{message_id}:{phase}:{run.requirement.version}:{run.recommendation_version}"
    execution = engine.execution_repository.claim_agent(
        AgentExecution(
            tenant_id=run.tenant_id,
            user_id=run.user_id,
            run_id=run.run_id,
            node_name=f"dialogue_{phase.lower()}",
            agent_kind="REQUIREMENT" if phase == "INTENT" else "SOURCING",
            task_id=f"dialogue_{message_id}"[:80],
            state_version=run.state_version,
            input_fingerprint=engine._fingerprint(payload),
            idempotency_key=key,
        )
    )
    if execution.status == ExecutionStatus.SUCCEEDED and execution.result_json:
        return model.model_validate(execution.result_json)
    context = AgentRuntimeContext(
        run=run,
        catalog=engine.catalog,
        enterprise=engine.enterprise,
        harness=engine.harness,
        roles=engine.enterprise.identity(run.tenant_id, run.user_id).roles,
        tool_gateway=engine.tool_gateway,
        model_gateway=engine.model_gateway,
        agent_kind="REQUIREMENT" if phase == "INTENT" else "SOURCING",
    )
    try:
        with engine.harness.agent_execution(execution.execution_id):
            output = await engine.harness.execute_agent_turn(
                run,
                lambda: method(
                    {"phase": phase, "message_id": message_id, **payload}, context=context
                ),
            )
        result = model.model_validate(output)
        engine.execution_repository.complete_agent(
            execution.execution_id, result.model_dump(mode="json")
        )
        return result
    except Exception as exc:
        engine.execution_repository.fail_agent(execution.execution_id, type(exc).__name__)
        engine.harness.trace(
            run,
            actor="Dialogue Service",
            kind="SERVICE",
            name="dialogue.fallback",
            detail={"phase": phase, "error": type(exc).__name__},
            status="DEGRADED",
        )
        return None


def pending_option(run, message_id):
    decision = next(
        (
            d
            for d in run.turn_decisions
            if d.message_id == message_id and d.intent == TurnIntent.ACCEPT_NEGOTIATION
        ),
        None,
    )
    if not decision:
        return None
    option = next(
        (o for o in run.negotiation_options if o.option_id == decision.referenced_option_id), None
    )
    if (
        not option
        or option.base_requirement_version != run.requirement.version
        or option.expires_at <= datetime.now(UTC)
    ):
        raise ValueError("Negotiation option is stale; request refreshed options")
    return option.model_copy(deep=True)


def apply_option(engine, run, option, message):
    """Apply server-validated structured consent; quotes remain the user's actual words."""
    base = run.requirement_spec
    if base is None:
        raise ValueError("Negotiation requires a validated RequirementSpec")
    alternative = run.requirement.model_copy(deep=True)
    from procure_pilot.domain.models import Money

    for change in option.adjustments:
        if change.field == "budget.maximum":
            alternative.unit_budget = Money(
                amount=change.proposed, currency=run.requirement.unit_budget.currency
            )
        else:
            setattr(alternative, change.field, change.proposed)
    offers = [
        o
        for o in run.recalled_offers
        if f"{o.provider}/{o.offer_id}" in option.validated_candidate_refs
    ]
    if not any(
        engine.sourcing_evidence.passes(
            engine.sourcing_evidence.assess(
                run,
                o,
                engine.sourcing_evidence.policy(run, o, alternative),
                requirement=alternative,
            )
        )
        for o in offers
    ):
        raise ValueError(
            "Negotiation option no longer passes validation; request refreshed options"
        )
    spec = base.model_copy(deep=True)
    spec.base_version, spec.version = base.version, base.version + 1
    spec.source_message_id = message.message_id
    spec.authority = RequirementAuthority.VALIDATED
    evidence = RequirementEvidence(
        message_id=message.message_id,
        quote=message.content,
        start=0,
        end=len(message.content),
        source_ref=f"negotiation://{run.run_id}/{option.option_id}",
    )
    spec.evidence.append(evidence)
    operations = []
    for change in option.adjustments:
        refs = [evidence.evidence_id]
        if change.field == "budget.maximum":
            spec.budget.maximum = SourcedMoneyValue(
                value=message.content,
                normalized_value=NormalizedMoneyValue(
                    amount=change.proposed, currency=base.budget.maximum.normalized_value.currency
                ),
                confidence=1,
                evidence_ids=refs,
            )
        elif change.field == "delivery_days":
            spec.delivery_days = SourcedRequirementValue(
                value=message.content,
                normalized_value=change.proposed,
                confidence=1,
                evidence_ids=refs,
            )
        else:
            spec.hard_constraints = [c for c in spec.hard_constraints if c.field != change.field]
            spec.hard_constraints.append(
                RequirementConstraint(
                    field=change.field,
                    operator="GTE",
                    value=change.proposed,
                    strength=ConstraintStrength.HARD,
                    confidence=1,
                    evidence_ids=refs,
                )
            )
        operations.append(
            RequirementDeltaOperation(
                op="REPLACE",
                path=change.field,
                old_value=change.previous,
                new_value=change.proposed,
                evidence_ids=refs,
            )
        )
    spec.delta = RequirementDelta(
        from_version=base.version, to_version=spec.version, operations=operations
    )
    engine.requirement_specs._sync_explicit_resolutions(spec)
    run.requirement_spec = spec
    run.requirement_spec_history.append(spec.model_copy(deep=True))
    run.requirement = engine.requirement_specs.to_procurement_requirement(spec)
    run.processed_message_id = message.message_id
    run.negotiation_consents.append(
        {
            "option_id": option.option_id,
            "message_id": message.message_id,
            "base_requirement_version": base.version,
            "adjustments": [a.model_dump(mode="json") for a in option.adjustments],
        }
    )


class ProcurementDialogue:
    def __init__(self, engine):
        self.engine = engine

    async def handle(self, *, run_id, tenant_id, user_id, message, message_id=None, **_kwargs):
        run = self.engine.get(run_id, tenant_id=tenant_id, user_id=user_id)
        receipt = next((r for r in run.message_routes if r.message_id == message_id), None)
        if (
            receipt
            and not any(m.message_id == message_id for m in run.messages)
            and (
                run.requirement.version != receipt.base_requirement_version
                or run.state_version != receipt.base_state_version
                or run.recommendation_version != receipt.base_recommendation_version
                or (run.resolution_draft.draft_id if run.resolution_draft else None) != receipt.base_resolution_draft_id
                or (run.resolution_draft.version if run.resolution_draft else 0) != receipt.base_resolution_version
            )
        ):
            raise ValueError("The procurement changed before this dialogue turn was accepted")
        if run.reference_request is not None or re.search(r"制度|公司标准|团队标准|研发标准|历史|上次|参考订单", message):
            return None
        if not (run.recommendation or run.negotiation_options):
            return None
        message_id = message_id or f"msg_{uuid4().hex[:12]}"
        if any(m.message_id == message_id for m in run.messages):
            return RunResponse(run=run, assistant_message=run.messages[-1].content)
        existing = next((d for d in run.turn_decisions if d.message_id == message_id), None)
        if existing and existing.intent in {
            TurnIntent.UPDATE_REQUIREMENT,
            TurnIntent.ACCEPT_NEGOTIATION,
        }:
            return None
        rules = classify_turn_rules(message)
        proposal = await model_dialogue(
            self.engine,
            run,
            "INTENT",
            {
                "message": message,
                "requirement": run.requirement.model_dump(mode="json"),
                "displayed_candidate_refs": run.displayed_candidate_refs,
                "negotiation_options": [o.model_dump(mode="json") for o in run.negotiation_options],
            },
            message_id=message_id,
        )
        proposal = proposal or rules
        # Explicit field changes, consent and selection cannot be weakened or hallucinated.
        if rules.intent in {
            TurnIntent.ACCEPT_NEGOTIATION,
            TurnIntent.SELECT_CANDIDATE,
        } or re.search(r"改成|改为|调整为|最低|必须|至少", message):
            proposal = rules
        if (
            proposal.intent == TurnIntent.ACCEPT_NEGOTIATION
            and rules.intent != TurnIntent.ACCEPT_NEGOTIATION
        ):
            proposal = DialogueProposal(intent="CLARIFY", confidence=0)
        decision = TurnDecision(
            message_id=message_id,
            intent=proposal.intent,
            confidence=proposal.confidence,
            base_requirement_version=run.requirement.version,
            recommendation_version=run.recommendation_version or None,
            target_reference=proposal.target_reference,
            preference=proposal.preference,
        )
        answer = "请明确要解释／比较哪一款，或要修改哪项条件。候选编号对应本轮展示的 1 综合最优、2 最低成本、3 最快交付。"
        if proposal.confidence < 0.8 or proposal.intent == TurnIntent.CLARIFY:
            decision.intent = TurnIntent.CLARIFY
        elif (
            proposal.intent == TurnIntent.UPDATE_REQUIREMENT
            or proposal.intent == TurnIntent.PROVIDE_REQUIREMENT
        ):
            run.turn_decisions.append(decision)
            self.engine.repository.save(run)
            return None
        elif proposal.intent == TurnIntent.ACCEPT_NEGOTIATION:
            ref = proposal.target_reference
            option = (
                run.negotiation_options[int(ref) - 1]
                if ref in {"1", "2", "3"} and int(ref) <= len(run.negotiation_options)
                else next((o for o in run.negotiation_options if o.option_id == ref), None)
            )
            if option:
                decision.referenced_option_id = option.option_id
                run.turn_decisions.append(decision)
                pending_option(run, message_id)  # validates version / expiry before any mutation
                self.engine.repository.save(run)
                return None
            answer = (
                "请明确接受哪个协商方案，例如“接受方案1”。接受后会重新寻源，创建草稿仍需另行确认。"
            )
        elif not run.recommendation:
            answer = "当前尚无通过校验的推荐，请选择协商方案或修改条件。"
        elif proposal.intent in {TurnIntent.EXPLAIN, TurnIntent.COMPARE}:
            targets = self._targets(run, proposal)
            if targets:
                # Query delivery/warranty if requested, with server-owned scope and bounded targets.
                from procure_pilot.domain.models import ToolBudgetPool
                from procure_pilot.gateway.tool_gateway import GatewayExecutionContext

                requested = [
                    name
                    for name, pattern in (
                        ("query_inventory", "库存"),
                        ("estimate_delivery", "交期|到货|交付"),
                        ("query_warranty", "保修|质保"),
                    )
                    if re.search(pattern, message)
                ]
                for c in targets:
                    for tool in requested:
                        await self.engine.tool_gateway.invoke(
                            tool_name=tool,
                            arguments={"provider": c.offer.provider, "offer_id": c.offer.offer_id},
                            context=GatewayExecutionContext(
                                run=run,
                                roles=self.engine.enterprise.identity(tenant_id, user_id).roles,
                                node_name="dialogue_facts",
                                budget_pool=ToolBudgetPool.WORKFLOW_READ,
                            ),
                        )
                selection = await model_dialogue(
                    self.engine,
                    run,
                    "EXPLAIN",
                    {
                        "message": message,
                        "candidates": [c.model_dump(mode="json") for c in targets],
                        "facts": [
                            f.model_dump(mode="json")
                            for f in run.fact_evidence
                            if any(
                                f.offer_id == c.offer.offer_id and f.provider == c.offer.provider
                                for c in targets
                            )
                        ],
                    },
                    message_id=message_id,
                )
                allowed_refs = {ref for c in targets for ref in c.evidence_refs} | {
                    r
                    for c in targets
                    for f in current_facts(run, c.offer).values()
                    for r in f.evidence_refs
                }
                if selection and not set(selection.evidence_refs).issubset(allowed_refs):
                    self.engine.harness.trace(
                        run,
                        actor="Dialogue Service",
                        kind="SERVICE",
                        name="dialogue.invalid_evidence",
                        status="REJECTED",
                    )
                if (
                    fact_digest(run, run.recommendation.best_overall.offer)
                    != run.confirmed_fact_digest
                ):
                    run.action_token = None
                focus = (
                    set(selection.evidence_refs)
                    if selection
                    and selection.evidence_refs
                    and set(selection.evidence_refs).issubset(allowed_refs)
                    else None
                )
                answer = "\n".join(self._describe(run, c, focus) for c in targets)
                answer += (
                    "\\n综合排序依据："
                    + (
                        "按用户指定候选选择"
                        if run.recommendation.selection_source == "user_selection"
                        else "在满足本轮硬条件与企业制度的候选池内，按当前价格、交期、供应商状态和保修偏好排序"
                    )
                    + "。当前需求未改变；创建草稿需要单独确认。"
                )
        elif proposal.intent in {TurnIntent.UPDATE_PREFERENCE, TurnIntent.SELECT_CANDIDATE}:
            pool = run.candidate_pool
            if proposal.intent == TurnIntent.UPDATE_PREFERENCE and proposal.preference:
                previous = run.ranking_preference
                run.ranking_preference = proposal.preference
                await self.engine.sourcing_evidence.enrich(run, [c.offer for c in pool])
                feasible = [
                    c
                    for c in pool
                    if self.engine.sourcing_evidence.passes(
                        self.engine.sourcing_evidence.assess(
                            run, c.offer, self.engine.sourcing_evidence.policy(run, c.offer)
                        )
                    )
                ]
                for c in feasible:
                    facts = current_facts(run, c.offer)
                    if "DELIVERY" in facts and facts["DELIVERY"].value:
                        c.offer.delivery_days = facts["DELIVERY"].value["days"]
                    if "WARRANTY" in facts and facts["WARRANTY"].value:
                        c.offer.variant.warranty_months = facts["WARRANTY"].value["months"]
                    if "INVENTORY" in facts and facts["INVENTORY"].value:
                        c.offer.stock = facts["INVENTORY"].value["stock"]
                    c.evidence_refs = list(
                        dict.fromkeys(
                            [
                                *c.evidence_refs,
                                *(ref for fact in facts.values() for ref in fact.evidence_refs),
                            ]
                        )
                    )
                    c.score = self.engine._preference_score(run, c.offer, c.cost, c.policy_decision)
                run.candidate_pool = feasible
                selected = max(feasible, key=lambda c: c.score) if feasible else None
                if selected and hasattr(self.engine.planner, "rank_candidates"):
                    validated = self.engine._build_validated_set(run, feasible)
                    run.validated_candidate_set = validated
                    ctx = AgentRuntimeContext(
                        run=run,
                        catalog=self.engine.catalog,
                        enterprise=self.engine.enterprise,
                        harness=self.engine.harness,
                        roles=self.engine.enterprise.identity(tenant_id, user_id).roles,
                        tool_gateway=self.engine.tool_gateway,
                        model_gateway=self.engine.model_gateway,
                    )
                    try:
                        ranking = await self.engine._rank_validated_candidates(
                            run, validated_set=validated, agent_context=ctx
                        )
                        candidate = next(
                            (
                                c
                                for c in feasible
                                if ranking
                                and c.offer.provider == ranking.provider
                                and c.offer.offer_id == ranking.offer_id
                            ),
                            None,
                        )
                        if (
                            candidate
                            and set(ranking.evidence_refs).issubset(candidate.evidence_refs)
                            and selected.score - candidate.score
                            <= self.engine.max_agent_utility_regret
                        ):
                            selected = candidate
                            run.sourcing_ranking = ranking
                    except Exception as exc:
                        self.engine.harness.trace(
                            run,
                            actor="Dialogue Service",
                            kind="SERVICE",
                            name="dialogue.rank_fallback",
                            status="DEGRADED",
                            detail={"error": type(exc).__name__},
                        )
                if selected is None:
                    run.ranking_preference = previous
            else:
                ref = run.displayed_candidate_refs.get(proposal.target_reference)
                selected = next(
                    (c for c in pool if f"{c.offer.provider}/{c.offer.offer_id}" == ref), None
                )
            if selected:
                await self.engine.sourcing_evidence.enrich(run, [selected.offer])
                if not self.engine.sourcing_evidence.passes(
                    self.engine.sourcing_evidence.assess(
                        run,
                        selected.offer,
                        self.engine.sourcing_evidence.policy(run, selected.offer),
                    )
                ):
                    selected = None
                    run.action_token = None
            if selected:
                for c in run.candidate_pool:
                    facts = current_facts(run, c.offer)
                    if "INVENTORY" in facts and facts["INVENTORY"].value:
                        c.offer.stock = facts["INVENTORY"].value["stock"]
                    c.evidence_refs = list(
                        dict.fromkeys(
                            [
                                *c.evidence_refs,
                                *(r for f in facts.values() for r in f.evidence_refs),
                            ]
                        )
                    )
                run.recommendation.best_overall = selected.model_copy(deep=True)
                run.recommendation.lowest_cost = min(
                    run.candidate_pool, key=lambda c: c.cost.unconditional_total.amount
                )
                run.recommendation.fastest_delivery = min(
                    run.candidate_pool, key=lambda c: c.offer.delivery_days
                )
                run.recommendation.selection_source = (
                    "user_preference"
                    if proposal.intent == TurnIntent.UPDATE_PREFERENCE
                    else "user_selection"
                )
                run.recommendation.selection_rationale = (
                    "按用户本轮偏好或指定候选，在已验证候选中调整"
                )
                run.validated_candidate_set = self.engine._build_validated_set(
                    run, run.candidate_pool
                )
                run.recommendation.candidate_set_hash = (
                    run.validated_candidate_set.candidate_set_hash
                )
                self.engine._publish_recommendation(run)
                run.quote_snapshot = self.engine._quote_snapshot(selected.offer)
                run.action_token = self.engine.harness.issue_action_token(run)
                answer = (
                    "已调整推荐：" + self._describe(run, selected) + "。请核对后重新确认创建草稿。"
                )
                run.recommendation.summary = answer
            else:
                answer = "未能确定仍满足条件的目标候选。请明确候选编号，或发送重试查询以重新寻源。"
        run.turn_decisions.append(decision)
        run.messages.extend(
            [
                Message(message_id=message_id, role="user", content=message),
                Message(role="assistant", content=answer.replace("\\n", "\n")),
            ]
        )
        run.processed_message_id = message_id
        self.engine.repository.save(run)
        return RunResponse(run=run, assistant_message=run.messages[-1].content)

    @staticmethod
    def _targets(run, proposal):
        rec = run.recommendation
        candidates = [rec.best_overall, rec.lowest_cost, rec.fastest_delivery]
        if proposal.intent == TurnIntent.EXPLAIN:
            if proposal.target_reference:
                ref = run.displayed_candidate_refs.get(proposal.target_reference)
                return [c for c in candidates if f"{c.offer.provider}/{c.offer.offer_id}" == ref][
                    :1
                ]
            return [rec.best_overall]
        return list({(c.offer.provider, c.offer.offer_id): c for c in candidates}.values())

    @staticmethod
    def _describe(run, c, focused_refs=None):
        facts = current_facts(run, c.offer)
        parts = [
            f"{c.offer.product.brand} {c.offer.product.model}（{c.offer.variant.ram_gb}GB/{c.offer.variant.storage_gb}GB），可信总价 {c.cost.unconditional_total.amount} {c.cost.unconditional_total.currency}"
        ]
        for kind, label, field in (
            ("INVENTORY", "库存", "stock"),
            ("DELIVERY", "交期（天）", "days"),
            ("WARRANTY", "保修（月）", "months"),
        ):
            fact = facts.get(kind)
            if focused_refs and fact and not focused_refs.intersection(fact.evidence_refs):
                continue
            parts.append(
                f"{label}：{fact.value[field] if fact and fact.value else '尚未确认'}"
                + (f" [{fact.fact_id}]" if fact else "")
            )
        return "；".join(parts)
