"""Build referenced requirements without activating them before explicit confirmation."""

from __future__ import annotations

import re
from uuid import uuid4

from procure_pilot.domain.conversation import RunWaitReason
from procure_pilot.domain.models import Message, RunResponse, RunState, ToolBudgetPool
from procure_pilot.domain.references import RequirementResolutionDraft, ResolvedReferenceField
from procure_pilot.domain.requirements import (
    ConstraintStrength,
    FieldResolution,
    FieldResolutionState,
    NormalizedMoneyValue,
    RequirementAuthority,
    RequirementConstraint,
    RequirementDelta,
    RequirementDeltaOperation,
    RequirementEvidence,
    RequirementStatus,
    SourcedMoneyValue,
    SourcedRequirementValue,
)
from procure_pilot.gateway.tool_gateway import GatewayExecutionContext
from procure_pilot.services.requirement_references import fingerprint, reference_pending
from procure_pilot.services.requirement_validation import RequirementCompletenessPolicy

FIELD_LABELS = {
    "category": "品类",
    "purpose": "用途",
    "quantity": "数量",
    "budget.maximum": "单件预算上限",
    "budget.tax_included": "预算含税",
    "delivery_city": "交付城市",
    "minimum_ram_gb": "最低内存（GB）",
    "minimum_storage_gb": "最低存储（GB）",
    "minimum_warranty_months": "最低保修（月）",
    "condition": "成色",
}
SOURCE_LABELS = {"USER": "用户要求", "POLICY": "现行制度", "HISTORY": "模拟历史采购单"}


def display_value(value):
    if isinstance(value, bool):
        return "是" if value else "否"
    return {
        "laptop": "笔记本",
        "NEW": "全新",
        "REFURBISHED": "翻新",
        "USED": "二手",
        "Jakarta": "雅加达",
        "Shanghai": "上海",
        "Shenzhen": "深圳",
    }.get(str(value), str(value))


def is_confirmation_message(message):
    return (
        re.fullmatch(
            r"(?:确认|确认需求|确认需求并开始寻源|就按这个来|可以)[。！!]?", message.strip()
        )
        is not None
    )


def draft_hash(draft):
    return fingerprint(draft.model_dump(mode="json", exclude={"content_hash", "status"}))


def field_value(spec, field):
    if field.startswith("budget."):
        value = getattr(spec.budget, field.split(".")[1])
    elif hasattr(spec, field):
        value = getattr(spec, field)
        if not hasattr(value, "normalized_value"):
            return None
    else:
        values = [c for c in spec.hard_constraints if c.field == field]
        value = (
            max(values, key=lambda c: float(c.value))
            if values and field.startswith("minimum_")
            else values[-1]
            if values
            else None
        )
    return value


def normalized(value):
    if value is None:
        return None
    item = getattr(value, "normalized_value", getattr(value, "value", None))
    return str(item.amount) if hasattr(item, "amount") else item


class RequirementResolutionService:
    def __init__(self, engine):
        self.engine = engine

    async def ensure_lookups(self, run, context=None, *, fresh_key=None):
        request = run.reference_request
        if request is None:
            return
        spec = run.requirement_spec or (context.requirement_spec if context else None)
        if spec and spec.category and spec.category.normalized_value != "laptop":
            return
        needed = []
        if request.policy or request.history:
            needed.append(("query_procurement_policy", "reference_policy"))
        if request.history:
            needed.append(("query_purchase_history", "reference_history"))
        for tool, attr in needed:
            if getattr(run, attr) is not None and fresh_key is None:
                continue
            result = await self.engine.tool_gateway.invoke(
                tool_name=tool,
                arguments={},
                context=GatewayExecutionContext(
                    run=run,
                    roles=self.engine.enterprise.identity(run.tenant_id, run.user_id).roles,
                    node_name="requirement_references"
                    if fresh_key is None
                    else "confirm_requirement_references",
                    budget_pool=ToolBudgetPool.WORKFLOW_READ,
                ),
                idempotency_key=f"reference-confirm:{run.run_id}:{fresh_key}:{tool}"
                if fresh_key
                else None,
            )
            setattr(run, attr, result)
        # The enclosing durable Agent node persists lookups and usage as a single snapshot.

    def wait_response(self, run):
        request = run.reference_request
        if not reference_pending(run) or not run.requirement_spec:
            return None
        base = run.requirement_spec
        if (not base.category and not request.history) or (
            base.category and base.category.normalized_value != "laptop"
        ):
            return None
        policy, history = run.reference_policy, run.reference_history
        blockers = []
        if policy is None or policy.status != "KNOWN":
            blockers.append(policy.detail if policy else "制度查询尚未成功，请重试查询。")
        order = None
        selection = False
        if request.history:
            if history is None or history.status != "KNOWN":
                blockers.append("历史采购单查询尚未成功，请重试查询。")
            elif not history.orders:
                blockers.append(
                    "没有可访问的已批准／已完成历史采购单。可以修改参考依据或手动补充。"
                )
            elif request.selected_order_id:
                order = next(
                    (o for o in history.orders if o.order_id == request.selected_order_id), None
                )
                if order is None:
                    blockers.append("指定采购单不在可访问的历史结果中，请重新选择。")
            elif request.latest_order:
                order = history.orders[0]
                request.selected_order_id = order.order_id
            else:
                selection = True
        proposed = base.model_copy(deep=True)
        proposed.reference_bindings = []
        proposed.base_version = base.version
        proposed.version = base.version + 1
        changes = []
        if not request.query_only and not selection and not blockers:
            bindings = {b.evidence_id: b.field for b in base.reference_bindings}
            permitted = {
                v.evidence_id: v
                for v in [*(policy.values if policy else []), *(order.fields if order else [])]
            }
            if any(
                eid not in permitted or permitted[eid].field != field
                for eid, field in bindings.items()
            ):
                blockers.append("模型引用了本次未选定／未查询的需求来源，已拒绝应用，请重试。")
            else:
                if order:
                    for value in order.fields:
                        if bindings and value.evidence_id not in bindings:
                            continue
                        old = field_value(proposed, value.field)
                        ids = old.evidence_ids if old else []
                        user_value = any(
                            e.evidence_id in ids and e.kind == "USER" for e in proposed.evidence
                        )
                        resolution = proposed.field_resolutions.get(value.field)
                        if user_value or (
                            resolution and resolution.state == FieldResolutionState.NO_PREFERENCE
                        ):
                            continue
                        self._set(proposed, value, request.authorization_message_id)
                        changes.append(
                            f"{FIELD_LABELS.get(value.field, value.field)} 参考模拟采购单 {order.order_id}：{display_value(value.value)}"
                        )
                for value in policy.values:
                    old = field_value(proposed, value.field)
                    actual = normalized(old)
                    user_value = old and any(
                        e.evidence_id in old.evidence_ids and e.kind == "USER"
                        for e in proposed.evidence
                    )
                    if value.operator == "GTE":
                        if actual is not None and float(actual) >= float(value.value):
                            continue
                        if old and old.operator in {"EQ", "LTE"} and user_value:
                            blockers.append(
                                f"{value.field} 的本轮要求与制度最低值 {value.value} 冲突。"
                            )
                            continue
                    elif (
                        actual is not None
                        and str(actual).upper() != str(value.value).upper()
                        and user_value
                    ):
                        blockers.append(f"{value.field} 的本轮要求与制度要求 {value.value} 冲突。")
                        continue
                    if actual != value.value:
                        self._set(proposed, value, request.authorization_message_id)
                        changes.append(
                            f"{value.field}：{actual if actual is not None else '未指定'} → {value.value}（现行制度）"
                        )
        self.engine.requirement_specs._sync_explicit_resolutions(proposed)
        for _field, resolution in proposed.field_resolutions.items():
            if any(
                e.evidence_id in resolution.evidence_ids and e.kind != "USER"
                for e in proposed.evidence
            ):
                resolution.state = FieldResolutionState.PROPOSED_REFERENCE
        proposed.conflicts = self.engine.requirement_specs._detect_conflicts(proposed)
        blockers.extend(c.reason for c in proposed.conflicts if c.blocking)
        RequirementCompletenessPolicy.refresh(proposed)
        values = []
        for field in (
            "category",
            "purpose",
            "quantity",
            "budget.maximum",
            "budget.tax_included",
            "delivery_city",
            "minimum_ram_gb",
            "minimum_storage_gb",
            "minimum_warranty_months",
            "condition",
        ):
            item = field_value(proposed, field)
            if item is None:
                continue
            evidence = next(
                (e for e in proposed.evidence if e.evidence_id in item.evidence_ids), None
            )
            values.append(
                ResolvedReferenceField(
                    field=field,
                    value=normalized(item),
                    source=evidence.kind if evidence else "USER",
                    source_ref=evidence.source_ref or f"message://{evidence.message_id}"
                    if evidence
                    else "user",
                    reason="本轮已明确要求"
                    if not evidence or evidence.kind == "USER"
                    else "查询来源提议，确认后生效",
                )
            )
        old = run.resolution_draft
        missing = [m.field for m in proposed.missing_fields]
        status = (
            "BLOCKED"
            if blockers
            else "HISTORY_SELECTION"
            if selection
            else "NEEDS_INPUT"
            if missing
            else "READY"
        )
        draft = RequirementResolutionDraft(
            **({"draft_id": old.draft_id} if old else {}),
            version=old.version + 1 if old else 1,
            base_requirement_version=base.version,
            source_message_id=base.source_message_id,
            status=status,
            proposed_spec=proposed,
            fields=values,
            changes=changes,
            missing_fields=missing,
            blockers=blockers,
            policy_snapshot=policy.model_copy(deep=True) if policy else None,
            history_snapshot=history.model_copy(deep=True) if history else None,
            policy_hash=policy.source_hash if policy else None,
            history_hash=history.source_hash if history else None,
            selected_order_id=order.order_id if order else request.selected_order_id,
        )
        draft.proposed_spec.delta = RequirementDelta(
            from_version=base.version,
            to_version=proposed.version,
            operations=[
                RequirementDeltaOperation(
                    op="ADD",
                    path=v.field,
                    new_value=normalized(field_value(proposed, v.field)),
                    evidence_ids=field_value(proposed, v.field).evidence_ids,
                )
                for v in values
                if v.source != "USER"
            ],
        )
        draft.content_hash = draft_hash(draft)
        run.resolution_draft = draft
        if run.state == RunState.COLLECTING:
            self.engine.harness.transition(
                run,
                RunState.WAITING_USER,
                reason="reference-derived requirements require user review",
            )
        if request.query_only:
            run.wait_reason = RunWaitReason.REFERENCE_INFORMATION
            message = "查询结果：\n" + (
                "\n".join(blockers)
                if blockers
                else "\n".join(
                    f"{FIELD_LABELS.get(v.field, v.field)}：{display_value(v.value)}（{v.source_ref}）"
                    for v in policy.values
                )
            )
            if history:
                message += "\n可访问模拟历史单：" + "、".join(o.order_id for o in history.orders)
            message += "\n本次仅查询；如要用于本次采购，请明确说明按制度或参考历史订单生成需求。"
        elif blockers:
            run.wait_reason = RunWaitReason.REFERENCE_UNAVAILABLE
            message = "需求参考依据尚未通过校验，未进入寻源：\n" + "\n".join(blockers)
        elif selection:
            run.wait_reason = RunWaitReason.HISTORY_SELECTION
            message = (
                "找到多条可参考的模拟采购单，请明确选择一条；选择后仍需确认生成的需求：\n"
                + "\n".join(
                    f"{i}. {o.order_id} · {o.purchased_at.date()} · {o.model_text}"
                    for i, o in enumerate(history.orders, 1)
                )
            )
        elif missing:
            run.wait_reason = RunWaitReason.MISSING_REQUIREMENT
            message = "已根据查询依据整理需求草案，以下必要信息仍需补充：\n" + "\n".join(
                f"{i}. {q.text}" for i, q in enumerate(proposed.clarification_plan.questions, 1)
            )
        else:
            run.wait_reason = RunWaitReason.REQUIREMENT_CONFIRMATION
            message = (
                "已生成待确认的需求草案（尚未寻源）：\n"
                + "\n".join(
                    f"{FIELD_LABELS.get(v.field, v.field)}：{display_value(v.value)}，来源 {SOURCE_LABELS[v.source]}"
                    for v in values
                )
                + "\n请核对来源与变更，确认需求后开始寻源；这不会创建采购草稿。"
            )
        run.messages.append(Message(role="assistant", content=message))
        return RunResponse(run=run, assistant_message=message)

    @staticmethod
    def _set(spec, value, message_id):
        ev = RequirementEvidence(
            evidence_id=value.evidence_id,
            message_id=message_id,
            quote=value.quote,
            kind=value.source_kind,
            source_ref=value.source_ref,
            start=None,
            end=None,
        )
        spec.evidence = [e for e in spec.evidence if e.evidence_id != ev.evidence_id] + [ev]
        sourced = SourcedRequirementValue(
            value=value.value,
            normalized_value=value.value,
            confidence=1,
            evidence_ids=[ev.evidence_id],
        )
        field = value.field
        if field in {"minimum_ram_gb", "minimum_storage_gb"}:
            spec.field_resolutions[field] = FieldResolution(
                state=FieldResolutionState.PROPOSED_REFERENCE,
                evidence_ids=[ev.evidence_id],
                source_ref=value.source_ref,
            )
        if field == "budget.maximum":
            spec.budget.maximum = SourcedMoneyValue(
                value=str(value.value),
                normalized_value=NormalizedMoneyValue(amount=value.value, currency="CNY"),
                confidence=1,
                evidence_ids=[ev.evidence_id],
            )
        elif field == "budget.tax_included":
            spec.budget.tax_included = sourced
        elif field in {"category", "purpose", "quantity", "delivery_city"}:
            setattr(spec, field, sourced)
        else:
            spec.hard_constraints = [c for c in spec.hard_constraints if c.field != field]
            spec.hard_constraints.append(
                RequirementConstraint(
                    field=field,
                    operator=value.operator,
                    value=value.value,
                    strength=ConstraintStrength.HARD,
                    confidence=1,
                    evidence_ids=[ev.evidence_id],
                )
            )

    async def verify_confirmation(self, run, *, draft_id, draft_version, content_hash, request_id):
        draft = run.resolution_draft
        if (
            not draft
            or draft.status != "READY"
            or run.wait_reason != RunWaitReason.REQUIREMENT_CONFIRMATION
            or draft.draft_id != draft_id
            or draft.version != draft_version
            or draft.content_hash != content_hash
            or draft_hash(draft) != content_hash
            or run.requirement.version != draft.base_requirement_version
        ):
            raise ValueError(
                "Requirement draft is stale, incomplete or not waiting for confirmation"
            )
        if draft.proposed_spec.status != RequirementStatus.READY_FOR_SOURCING or draft.blockers:
            raise ValueError("Requirement draft is not complete")
        await self.ensure_lookups(run, fresh_key=f"{request_id}:{uuid4().hex}")
        if (
            run.reference_policy is None
            or run.reference_policy.status != "KNOWN"
            or run.reference_policy.source_hash != draft.policy_hash
            or (
                run.reference_request.history
                and (
                    run.reference_history is None
                    or run.reference_history.source_hash != draft.history_hash
                )
            )
        ):
            run.requirement_spec.reference_bindings = []
            self.wait_response(run)
            self.engine.repository.save(run)
            raise ValueError("Reference sources changed; review the refreshed requirement draft")
        if draft.selected_order_id and not any(
            o.order_id == draft.selected_order_id for o in run.reference_history.orders
        ):
            raise PermissionError("Selected historical order is no longer accessible")

    def apply_confirmation(self, run, receipt, message):
        draft = run.resolution_draft
        if (
            not draft
            or draft.draft_id != receipt.draft_id
            or draft.version != receipt.draft_version
            or draft_hash(draft) != receipt.content_hash
        ):
            raise ValueError("Requirement confirmation no longer matches its draft")
        spec = draft.proposed_spec.model_copy(deep=True)
        spec.authority = RequirementAuthority.VALIDATED
        for resolution in spec.field_resolutions.values():
            if resolution.state == FieldResolutionState.PROPOSED_REFERENCE:
                resolution.state = FieldResolutionState.CONFIRMED_REFERENCE
        spec.source_message_id = message.message_id
        run.requirement_spec = spec
        run.requirement_spec_history.append(spec.model_copy(deep=True))
        run.requirement = self.engine.requirement_specs.to_procurement_requirement(spec)
        run.processed_message_id = message.message_id
        draft.status = "CONFIRMED"
        if not any(
            d.draft_id == draft.draft_id and d.version == draft.version
            for d in run.confirmed_requirement_drafts
        ):
            run.confirmed_requirement_drafts.append(draft.model_copy(deep=True))
        self.engine.harness.trace(
            run,
            actor="Requirement Resolution Service",
            kind="SERVICE",
            name="requirement_draft.confirmed",
            detail={
                "draft_id": draft.draft_id,
                "draft_version": draft.version,
                "content_hash": draft.content_hash,
                "request_id": receipt.request_id,
            },
        )
