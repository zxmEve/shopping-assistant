"""Authenticated, bounded reference reads used by Requirement Agent tools."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime

from procure_pilot.domain.knowledge import PolicyKnowledgeReviewRequired
from procure_pilot.domain.references import ReferenceLookupResult, ReferenceRequest, ReferenceValue


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, ensure_ascii=False).encode()
    ).hexdigest()


def update_reference_request(run, message):
    text = message.content
    old = run.reference_request
    history = bool(
        re.search(
            r"历史(?:采购|订单|采购单)?|上次(?:的)?(?:采购|订单|配置)?|之前.*(?:采购|订单)|旧单|参考订单",
            text,
        )
    )
    policy = bool(re.search(r"制度|(?:公司|团队|研发|采购)标准", text))
    withdraw_history = bool(
        re.search(r"(?:不再|不用|不要|不)(?:再)?(?:参考|按|依照|用)?(?:历史|上次|之前|旧单)", text)
    )
    withdraw_policy = bool(
        re.search(
            r"(?:不再|不用|不要|不)(?:再)?(?:参考|按|依照|用)?(?:制度|公司标准|团队标准)", text
        )
    )
    history = history and not withdraw_history
    policy = policy and not withdraw_policy
    selection_index = re.search(r"(?:选择|选|参考)第?([一二三123])(?:单|张|个|条)", text)
    if selection_index and old and old.history and run.reference_history:
        index = (
            int({"一": "1", "二": "2", "三": "3"}.get(selection_index[1], selection_index[1])) - 1
        )
        if index < len(run.reference_history.orders):
            old.selected_order_id = run.reference_history.orders[index].order_id
            old.latest_order = False
            if run.resolution_draft:
                run.resolution_draft.status = "NEEDS_INPUT"
            return
    if old and re.search(r"重试查询|重新查询", text):
        run.reference_policy = None
        run.reference_history = None
        if run.resolution_draft:
            run.resolution_draft.status = "NEEDS_INPUT"
        return
    selected = re.search(r"\bHIST-[A-Z]+-[A-Z0-9]+\b", text, re.I)
    is_selection = selected and old and old.history and not policy and not withdraw_history
    if is_selection:
        old.selected_order_id = selected[0].upper()
        old.latest_order = False
        if run.resolution_draft:
            run.resolution_draft.status = "NEEDS_INPUT"
        return
    if (withdraw_history or withdraw_policy) and old:
        history = history or (old.history and not withdraw_history)
        policy = policy or (old.policy and not withdraw_policy)
    if history or policy:
        apply = bool(re.search(r"按|参考|照|依照|采用|遵循|依据|根据|标准来", text))
        run.reference_request = ReferenceRequest(
            policy=policy,
            history=history,
            query_only=not apply,
            authorization_message_id=message.message_id,
            authorization_quote=text,
            selected_order_id=selected[0].upper() if selected else None,
            latest_order=bool(re.search(r"上次|最近|最新", text)),
            history_year=(
                datetime.now(UTC).year - 1
                if "去年" in text
                else int(re.search(r"(20\d{2})年", text)[1])
                if re.search(r"(20\d{2})年", text)
                else None
            ),
        )
        run.reference_policy = None
        run.reference_history = None
        if run.resolution_draft:
            run.resolution_draft.status = "NEEDS_INPUT"
    elif (
        withdraw_history
        or withdraw_policy
        or (old and old.query_only)
        or (run.resolution_draft and run.resolution_draft.status == "CONFIRMED")
    ):
        run.reference_request = None
        run.reference_policy = None
        run.reference_history = None
        run.resolution_draft = None


def reference_pending(run):
    return run.reference_request is not None and (
        run.resolution_draft is None or run.resolution_draft.status != "CONFIRMED"
    )


def assert_reference_ready(run):
    if reference_pending(run):
        raise ValueError("Requirement references must be confirmed before sourcing")


class RequirementReferenceReader:
    def __init__(self, enterprise, history, knowledge_service=None):
        self.enterprise = enterprise
        self.history = history
        self.knowledge_service = knowledge_service

    def _authorize(self, run, kind):
        request = run.reference_request
        if (
            request is None
            or (kind == "HISTORY" and not request.history)
            or (kind == "POLICY" and not (request.policy or request.history))
        ):
            raise PermissionError("Reference query requires the current user reference request")
        message = next(
            (
                m
                for m in run.messages
                if m.message_id == request.authorization_message_id and m.role == "user"
            ),
            None,
        )
        if message is None or message.content != request.authorization_quote:
            raise PermissionError("Reference query authorization has no matching user evidence")
        return self.enterprise.identity(run.tenant_id, run.user_id)

    def policy(self, run):
        self._authorize(run, "POLICY")
        now = datetime.now(UTC)
        version = str(self.enterprise.policy["version"])
        source = f"policy://{self.enterprise.policy['policy_id']}/v{version}"
        values = []
        detail = "当前执行制度；模拟企业制度，仅供演示。"
        status = "KNOWN"
        citations = []
        effective = None
        try:
            effective = self.enterprise.policy["effective_from"]
            if isinstance(effective, str):
                effective = datetime.fromisoformat(effective)
            if effective > now:
                raise ValueError("制度尚未生效")
            knowledge_service = (
                self.knowledge_service()
                if callable(self.knowledge_service)
                else self.knowledge_service
            )
            if knowledge_service is not None:
                evidence = knowledge_service.verify_policy_evidence(
                    tenant_id=run.tenant_id,
                    category_id="CAT-COMPUTER-LAPTOP",
                    query="研发 笔记本 内存 保修 全新",
                    expected_policy_version=version,
                )
                if evidence:
                    citations = [c.model_dump(mode="json") for c in evidence.citations]
            baseline = self.enterprise.policy["developer_laptop_baseline"]
            for field, key in [
                ("minimum_ram_gb", "minimum_ram_gb"),
                ("minimum_warranty_months", "minimum_warranty_months"),
                ("condition", "condition"),
            ]:
                value = baseline[key]
                values.append(
                    ReferenceValue(
                        evidence_id="policy:" + fingerprint([source, field, value])[:20],
                        field=field,
                        value=value,
                        operator="EQ" if field == "condition" else "GTE",
                        authority="MANDATORY",
                        source_kind="POLICY",
                        source_ref=f"{source}#developer_laptop_baseline.{key}",
                        quote=f"研发笔记本制度：{field} = {value}",
                        source_version=version,
                    )
                )
        except (PolicyKnowledgeReviewRequired, ValueError, KeyError) as exc:
            status = "UNAVAILABLE"
            detail = f"制度依据暂不可用：{getattr(exc, 'code', str(exc))}"
            values = []
        return ReferenceLookupResult(
            kind="POLICY",
            status=status,
            values=values,
            source_version=version,
            effective_from=effective,
            citations=citations,
            source_hash=fingerprint(
                {
                    "policy": self.enterprise.policy,
                    "citations": [
                        {k: c[k] for k in ("source_ref", "excerpt", "policy_version")}
                        for c in citations
                    ],
                }
            ),
            fetched_at=now,
            detail=detail,
        )

    def purchase_history(self, run):
        employee = self._authorize(run, "HISTORY")
        orders = self.history.search(
            tenant_id=run.tenant_id,
            user_id=run.user_id,
            department=employee.department,
            roles=employee.roles,
            category="laptop",
            year=run.reference_request.history_year,
        )
        return ReferenceLookupResult(
            kind="HISTORY",
            status="KNOWN",
            orders=orders,
            source_version="fixture-history-v1",
            source_hash=fingerprint([o.model_dump(mode="json") for o in orders]),
            fetched_at=datetime.now(UTC),
            detail="模拟已批准／已完成采购单；本人及已授权团队范围；历史价格和交付事实不代表本次报价。",
        )
