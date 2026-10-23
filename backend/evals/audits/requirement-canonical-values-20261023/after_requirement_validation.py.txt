from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any

from procure_pilot.domain.models import Money, ProcurementRequirement, ProductCondition
from procure_pilot.domain.requirements import (
    ClarificationPlan,
    ClarificationQuestion,
    ConstraintStrength,
    FieldResolution,
    FieldResolutions,
    FieldResolutionState,
    MissingRequirementField,
    NormalizedMoneyValue,
    RequirementAuthority,
    RequirementConflict,
    RequirementConstraint,
    RequirementDelta,
    RequirementDeltaOperation,
    RequirementEvidence,
    RequirementSpec,
    RequirementStatus,
    SourcedMoneyValue,
    SourcedRequirementValue,
)
from procure_pilot.services.requirement_evidence import (
    budget_retractions,
    capacity_mentions,
    current_matches,
    is_background,
    quantity_candidates,
    quantity_mention,
    warranty_mentions,
)
from procure_pilot.services.requirement_values import (
    canonical_city,
    canonical_operating_system,
    normalize_requirement_values,
)


@dataclass(frozen=True)
class RequiredFieldPolicy:
    path: str
    impact: float
    question: str


# The single source of truth used by Agent routing, validation and fallback clarification.
REQUIRED_FIELD_POLICY: tuple[RequiredFieldPolicy, ...] = (
    RequiredFieldPolicy("category", 1.00, "当前支持笔记本采购，请确认需要采购哪一类商品？"),
    RequiredFieldPolicy("budget.maximum", 1.00, "单件预算上限是多少，币种是什么？"),
    RequiredFieldPolicy("quantity", 0.95, "需要采购多少件？"),
    RequiredFieldPolicy("minimum_ram_gb", 0.90, "最低内存要求是多少？没有额外要求也可以说明。"),
    RequiredFieldPolicy("minimum_storage_gb", 0.88, "最低存储容量要求是多少？没有额外要求也可以说明。"),
    RequiredFieldPolicy("delivery_city", 0.85, "具体交付城市是哪里？"),
    RequiredFieldPolicy("budget.tax_included", 0.80, "预算是含税还是未税？"),
)

ALLOWED_CONSTRAINT_FIELDS = frozenset(
    {
        "minimum_ram_gb",
        "minimum_storage_gb",
        "minimum_warranty_months",
        "delivery_days",
        "operating_system",
        "condition",
        "brand_preference",
        "requested_model_text",
    }
)

NO_PREFERENCE_FIELDS = frozenset({"minimum_ram_gb", "minimum_storage_gb"})
NO_PREFERENCE_MARKERS = ("无要求", "没有要求", "不限", "不限制", "无硬性要求", "没有硬性要求", "无最低要求", "没有额外要求", "无额外要求", "没有特殊要求", "无特殊要求", "没有配置要求", "无配置要求")
FIELD_TOPICS = {
    "category": ("品类", "商品", "采购", "购买"),
    "budget.maximum": ("预算", "金额", "币种"),
    "budget.tax_included": ("税",),
    "quantity": ("数量", "多少", "几台", "几件"),
    "minimum_ram_gb": ("内存", "ram"),
    "minimum_storage_gb": ("存储", "硬盘", "ssd"),
    "delivery_city": ("交付", "城市", "送到", "收货"),
    "condition": ("成色", "全新", "翻新", "二手"),
    "brand_preference": ("品牌",),
    "requested_model_text": ("型号",),
    "operating_system": ("系统", "windows", "linux", "macos"),
    "delivery_days": ("到货", "交期", "期限"),
}
UNASKED_TOPICS = {
    "budget.maximum": ("预算上限", "币种", "预算金额"),
    "budget.tax_included": ("含税", "未税", "税口径"),
    "quantity": ("数量", "多少台", "多少件", "几台", "几件"),
    "minimum_ram_gb": ("内存", "ram"),
    "minimum_storage_gb": ("存储", "硬盘", "ssd"),
    "delivery_city": ("城市", "收货地", "送到"),
    "condition": ("成色", "翻新", "二手"),
    "brand_preference": ("品牌",),
    "requested_model_text": ("型号",),
    "operating_system": ("操作系统", "windows", "linux", "macos"),
    "delivery_days": ("到货", "交期", "期限"),
}
FIELD_LABELS = {"minimum_ram_gb": "内存", "minimum_storage_gb": "存储", "operating_system": "操作系统", "condition": "成色", "brand_preference": "品牌", "requested_model_text": "型号"}


DEDICATED_CORE_FIELDS = frozenset(
    {
        "category",
        "purpose",
        "quantity",
        "budget.maximum",
        "budget.target",
        "budget.tax_included",
        "delivery_city",
        "delivery_days",
    }
)


_BUDGET_PATTERN = (
    r"(?:(?:整批|整单|总|合计|全批)\s*)?"
    r"(?:单台(?:预算|价格)?|单件(?:预算|价格)?|预算)"
    r"[^\d，,。；;\n]{0,8}(\d{3,7}(?:\.\d{1,2})?)(?!\d)\s*"
    r"(?:人民币|美元|美金|欧元|港元|日元|usd|eur|hkd|jpy|rmb|cny|元)?"
)


def _budget_mention(text: str) -> tuple[re.Match[str], bool] | None:
    """Return a current budget and its scope; never divide a total implicitly."""
    mentions = []
    for match in re.finditer(_BUDGET_PATTERN, text):
        prefix = re.split(r"[,，。;；\n]", text[:match.start()])[-1]
        if any(m.start() <= match.start() < m.end() for m in budget_retractions(text)):
            continue
        if re.search(r"上次|去年|历史|之前", prefix) and not re.search(r"本次|这次|现在", prefix):
            continue
        amount_start = match.start(1) - match.start()
        label = match.group(0)[:amount_start]
        total = bool(re.search(r"(?:整批|整单|总|合计|全批)[^，,。；;\n]{0,8}$", prefix)
                     or re.search(r"^(?:整批|整单|总|合计|全批)|总额|总共|总计|合计", label))
        explicit_unit = bool(re.match(r"单台|单件", label)
                             or re.match(r"\s*每(?:台|件)", text[match.end():]))
        mentions.append((match, total and not explicit_unit))
    unit = [mention for mention in mentions if not mention[1]]
    return unit[0] if unit else mentions[0] if mentions else None


def _explicit_currency(quote: str) -> str | None:
    currencies = {
        "USD": r"美元|美金|(?<![a-z])usd(?![a-z])",
        "EUR": r"欧元|(?<![a-z])eur(?![a-z])",
        "HKD": r"港元|(?<![a-z])hkd(?![a-z])",
        "JPY": r"日元|(?<![a-z])jpy(?![a-z])",
        "CNY": r"人民币|(?<![a-z])(?:rmb|cny)(?![a-z])|(?<![美欧港日])元",
    }
    found = [currency for currency, pattern in currencies.items() if re.search(pattern, quote.lower())]
    return found[0] if len(found) == 1 else None


def _condition_mentions(text: str) -> list[tuple[re.Match[str], str]]:
    """Only affirmative condition claims can ground an equality constraint."""
    mentions = []
    pattern = r"(?P<qualifier>必须|只能|只接受|仅接受|只要|要求|优先|最好|改为|改成|prefer)?\s*(?P<condition>全新(?:机)?|翻新(?:机)?|二手(?:机)?|\bnew\b|\brefurbished\b|\bused\b)"
    for match in re.finditer(pattern, text):
        prefix = re.split(r"[,，。;；\n]|但是|不过|而是|但", text[:match.start()])[-1]
        negated = re.search(r"(?:不|非)\s*$|(?:不要|不接受|不考虑|拒绝|不买|不要求|不必|无需)[^，,。；;\n]*$|\b(?:not|no)\s*$|(?:do not|don't)(?: accept)?[^,.;\n]*$", prefix)
        if negated:
            previous_rejection = prefix[negated.start():]
            explicit_new_claim = match.group("qualifier") in {"只接受", "仅接受", "必须", "只能", "只要"}
            if not (explicit_new_claim and re.search(r"全新|翻新|二手", previous_rejection)):
                continue
        condition = match.group("condition").removesuffix("机")
        mentions.append((match, {"全新": "NEW", "翻新": "REFURBISHED", "二手": "USED", "new": "NEW", "refurbished": "REFURBISHED", "used": "USED"}[condition]))
    return mentions


def _quantity_mention(text: str) -> re.Match[str] | None:
    return quantity_mention(text)


def _evidence(message_id: str, text: str, match: re.Match[str]) -> RequirementEvidence:
    return RequirementEvidence(
        message_id=message_id,
        quote=match.group(0),
        start=match.start(),
        end=match.end(),
    )


def _value(value: Any, normalized: Any, evidence_id: str, confidence: float = 0.99):
    return SourcedRequirementValue(
        value=value,
        normalized_value=normalized,
        confidence=confidence,
        evidence_ids=[evidence_id],
    )


def _money(value: str, amount: Decimal, currency: str, evidence_id: str):
    return SourcedMoneyValue(
        value=value,
        normalized_value=NormalizedMoneyValue(amount=amount, currency=currency),
        confidence=0.99,
        evidence_ids=[evidence_id],
    )


class RequirementCompletenessPolicy:
    @staticmethod
    def schema_snapshot() -> dict[str, Any]:
        return {
            "schema_version": "2.0",
            "required_fields": [
                {
                    "path": item.path,
                    "impact": item.impact,
                    "question": item.question,
                }
                for item in REQUIRED_FIELD_POLICY
            ],
            "constraint_strengths": ["HARD", "SOFT"],
            "allowed_operators": ["EQ", "GTE", "LTE", "IN", "PREFER"],
            "allowed_constraint_fields": sorted(ALLOWED_CONSTRAINT_FIELDS),
            "dedicated_core_fields": sorted(DEDICATED_CORE_FIELDS),
            "clarification_mode": "ALL_CURRENT_BLOCKERS",
            "maximum_questions_per_turn": 12,
            "supported_categories": ["laptop"],
            "no_preference_fields": ["minimum_ram_gb", "minimum_storage_gb"],
            "field_resolution_states": [item.value for item in FieldResolutionState],
            "forbidden_domains": ["product", "offer", "provider", "catalog"],
        }

    @staticmethod
    def unresolved_configuration(requirement: ProcurementRequirement) -> list[str]:
        return [
            field for field in sorted(NO_PREFERENCE_FIELDS)
            if getattr(requirement, field) is None
            and (
                requirement.field_resolutions.get(field) is None
                or requirement.field_resolutions[field].state != FieldResolutionState.NO_PREFERENCE
            )
        ]

    @staticmethod
    def _constraint(spec: RequirementSpec, field: str) -> RequirementConstraint | None:
        candidates = [
            item for item in spec.hard_constraints
            if item.field == field and item.operator in {"GTE", "EQ"}
        ]
        return max(candidates, key=lambda item: float(item.value)) if candidates else None

    @classmethod
    def missing(cls, spec: RequirementSpec) -> list[MissingRequirementField]:
        if spec.category is None or str(spec.category.normalized_value) != "laptop":
            return [MissingRequirementField(
                field="category", reason="请先明确品类；当前目录只支持笔记本采购", impact=1.0
            )]
        values = {
            "category": spec.category,
            "quantity": spec.quantity,
            "budget.maximum": spec.budget.maximum,
            "budget.tax_included": spec.budget.tax_included,
            "delivery_city": spec.delivery_city,
            "minimum_ram_gb": cls._constraint(spec, "minimum_ram_gb"),
            "minimum_storage_gb": cls._constraint(spec, "minimum_storage_gb"),
        }
        return [
            MissingRequirementField(
                field=item.path,
                reason="该字段会影响可行性、成本或商品匹配，不能由系统静默假设",
                impact=item.impact,
            )
            for item in REQUIRED_FIELD_POLICY
            if values[item.path] is None
            and not (
                item.path in NO_PREFERENCE_FIELDS
                and spec.field_resolutions.get(item.path) is not None
                and spec.field_resolutions[item.path].state == FieldResolutionState.NO_PREFERENCE
            )
        ]

    @classmethod
    def clarification_plan(
        cls,
        missing: list[MissingRequirementField],
        conflicts: list[RequirementConflict],
        proposed: ClarificationPlan | None = None,
    ) -> ClarificationPlan:
        """Retain grounded model wording, repair only rejected or missing questions."""
        blocking = {item.conflict_id: item for item in conflicts if item.blocking}
        pending = {item.field for item in missing} | {item.field for item in blocking.values()}
        # Category gates every category-specific question.
        if "category" in pending:
            pending = {"category"}
            blocking = {}
        covered: set[str] = set()
        resolved_conflicts: set[str] = set()
        questions: list[ClarificationQuestion] = []
        rejected = 0
        question_ids: set[str] = set()
        for question in (proposed.questions if proposed else []):
            fields = set(question.field_ids)
            conflict = blocking.get(question.resolves_conflict_id or "")
            conflict_fields = {item.field for item in blocking.values()}
            valid = (
                bool(fields)
                and question.question_id not in question_ids
                and len(fields) == len(question.field_ids)
                and question.field in fields
                and fields <= pending
                and not fields.intersection(covered)
                and (not fields.intersection(conflict_fields) or conflict is not None)
                and (question.resolves_conflict_id is None or conflict is not None)
                and (conflict is None or fields == {conflict.field})
                and all(any(token in question.text.lower() for token in FIELD_TOPICS.get(field, (field,))) for field in fields)
                and not any(token in question.text.lower() for token in ("offer_id", "product_id", "库存", "有货", "报价为", "推荐购买", "颜色", "重量"))
                and not any(
                    field not in fields and any(token in question.text.lower() for token in UNASKED_TOPICS[field])
                    for field in UNASKED_TOPICS
                )
            )
            if not valid:
                rejected += 1
                continue
            accepted = question.model_copy(deep=True)
            accepted.impact = max((item.impact for item in missing if item.field in fields), default=1.0)
            questions.append(accepted)
            question_ids.add(accepted.question_id)
            covered.update(fields)
            if conflict:
                resolved_conflicts.add(conflict.conflict_id)
        repairs = 0
        for conflict in blocking.values():
            if conflict.conflict_id in resolved_conflicts:
                continue
            questions.append(ClarificationQuestion(
                field=conflict.field,
                text=f"你对{FIELD_LABELS.get(conflict.field, conflict.field)}的要求存在冲突：{conflict.reason}，请确认以哪个为准？",
                impact=1.0,
                resolves_conflict_id=conflict.conflict_id,
            ))
            covered.add(conflict.field)
            repairs += 1
        for item in sorted(missing, key=lambda item: item.impact, reverse=True):
            if item.field not in pending or item.field in covered:
                continue
            policy = next(policy for policy in REQUIRED_FIELD_POLICY if policy.path == item.field)
            questions.append(ClarificationQuestion(field=item.field, text=policy.question, impact=item.impact))
            covered.add(item.field)
            repairs += 1
        return ClarificationPlan(
            questions=questions,
            covered_fields=sorted(covered),
            repaired_question_count=(rejected + repairs) if proposed is not None else 0,
        )

    @classmethod
    def next_question(cls, missing, conflicts):
        questions = cls.clarification_plan(missing, conflicts).questions
        return questions[0] if questions else None

    @classmethod
    def refresh(cls, spec: RequirementSpec) -> None:
        spec.missing_fields = cls.missing(spec)
        spec.clarification_plan = cls.clarification_plan(
            spec.missing_fields, spec.conflicts, spec.clarification_plan
        )
        spec.next_question = next(iter(spec.clarification_plan.questions), None)
        spec.status = (
            RequirementStatus.NEEDS_CLARIFICATION
            if spec.clarification_plan.questions else RequirementStatus.READY_FOR_SOURCING
        )


class RequirementSpecService:
    """Deterministic fixture and trust-boundary validator for Requirement Agent output."""

    def propose(
        self,
        *,
        run_id: str,
        message_id: str,
        text: str,
        base: RequirementSpec | None,
    ) -> RequirementSpec:
        base_version = base.version if base else 0
        spec = (
            base.model_copy(deep=True)
            if base
            else RequirementSpec(
                run_id=run_id,
                base_version=0,
                version=1,
                source_message_id=message_id,
                delta=RequirementDelta(from_version=0, to_version=1),
            )
        )
        spec.schema_version = "2.0"
        spec.authority = RequirementAuthority.PROPOSED
        spec.base_version = base_version
        spec.version = base_version + 1
        spec.source_message_id = message_id
        operations: list[RequirementDeltaOperation] = []
        normalized = text.lower().replace("，", ",")

        def assign(path: str, old: Any, new: Any, evidence_id: str) -> None:
            if old == new:
                return
            operations.append(
                RequirementDeltaOperation(
                    op="ADD" if old is None else "REPLACE",
                    path=path,
                    old_value=old,
                    new_value=new,
                    evidence_ids=[evidence_id],
                )
            )

        def capture(pattern: str) -> tuple[re.Match[str], RequirementEvidence] | None:
            matches = current_matches(pattern, normalized)
            if not matches:
                return None
            match = matches[0]
            evidence = _evidence(message_id, text, match)
            spec.evidence.append(evidence)
            return match, evidence

        found = capture(r"(?:开发|研发)?(?:笔记本|电脑|laptop)|(?:手机|phone)|(?:显示器|monitor)")
        if found:
            match, evidence = found
            category = (
                "phone"
                if any(token in match.group(0) for token in ("手机", "phone"))
                else "monitor"
                if any(token in match.group(0) for token in ("显示器", "monitor"))
                else "laptop"
            )
            old = spec.category.normalized_value if spec.category else None
            spec.category = _value(match.group(0), category, evidence.evidence_id)
            assign("category", old, category, evidence.evidence_id)

        found = capture(r"(?:用于|做|给)?\s*(研发|开发|docker|设计|剪辑|办公)")
        if found:
            match, evidence = found
            purpose = "开发" if match.group(1) == "docker" else match.group(1)
            old = spec.purpose.normalized_value if spec.purpose else None
            spec.purpose = _value(purpose, purpose, evidence.evidence_id, 0.95)
            assign("purpose", old, purpose, evidence.evidence_id)

        match = _quantity_mention(normalized)
        if match is not None:
            evidence = _evidence(message_id, text, match)
            spec.evidence.append(evidence)
            value = int(match.group(1))
            old = spec.quantity.normalized_value if spec.quantity else None
            spec.quantity = _value(match.group(0), value, evidence.evidence_id)
            assign("quantity", old, value, evidence.evidence_id)

        budget_mention = _budget_mention(normalized)
        if budget_mention is not None:
            match, total_budget = budget_mention
            evidence = _evidence(message_id, text, match)
            spec.evidence.append(evidence)
            if total_budget:
                # The domain stores a per-item ceiling, not a total budget. Keep
                # the total in user evidence and ask for its intended unit ceiling.
                for field in ("maximum", "target"):
                    old_value = getattr(spec.budget, field)
                    if old_value is not None:
                        setattr(spec.budget, field, None)
                        operations.append(RequirementDeltaOperation(
                            op="REMOVE", path=f"budget.{field}",
                            old_value=old_value.normalized_value.model_dump(mode="json"),
                            evidence_ids=[evidence.evidence_id],
                        ))
                spec.field_resolutions.budget_maximum = None
            else:
                amount = Decimal(match.group(1)).quantize(Decimal("0.01"))
                target = any(token in match.group(0) for token in ("左右", "大概", "最好"))
                old_value = spec.budget.target if target else spec.budget.maximum
                sourced = _money(match.group(0), amount, _explicit_currency(match.group(0)) or "CNY", evidence.evidence_id)
                if target:
                    spec.budget.target = sourced
                    assign(
                        "budget.target",
                        (
                            old_value.normalized_value.model_dump(mode="json")
                            if old_value
                            else None
                        ),
                        sourced.normalized_value.model_dump(mode="json"),
                        evidence.evidence_id,
                    )
                else:
                    spec.budget.maximum = sourced
                    assign(
                        "budget.maximum",
                        (
                            old_value.normalized_value.model_dump(mode="json")
                            if old_value
                            else None
                        ),
                        sourced.normalized_value.model_dump(mode="json"),
                        evidence.evidence_id,
                    )

        retractions = budget_retractions(normalized)
        latest_unit = _budget_mention(normalized)
        if retractions and not (latest_unit and not latest_unit[1]
                               and latest_unit[0].start() > retractions[-1].end()):
            evidence = _evidence(message_id, text, retractions[-1])
            spec.evidence.append(evidence)
            for field in ("maximum", "target"):
                previous = getattr(spec.budget, field)
                if previous is not None:
                    setattr(spec.budget, field, None)
                    operations.append(RequirementDeltaOperation(
                        op="REMOVE", path=f"budget.{field}",
                        old_value=previous.normalized_value.model_dump(mode="json"),
                        evidence_ids=[evidence.evidence_id],
                    ))
            spec.field_resolutions.budget_maximum = None

        found = capture(r"(?:不含税|未税|含税)")
        if found:
            match, evidence = found
            included = match.group(0) == "含税"
            old = spec.budget.tax_included.normalized_value if spec.budget.tax_included else None
            spec.budget.tax_included = _value(match.group(0), included, evidence.evidence_id)
            assign("budget.tax_included", old, included, evidence.evidence_id)

        found = capture(r"(?:送到|交付到|到货到|送)?\s*(雅加达|jakarta|上海市?|shanghai|深圳市?|shenzhen)(?![a-z])")
        if found:
            match, evidence = found
            city = canonical_city(match.group(1))
            old = spec.delivery_city.normalized_value if spec.delivery_city else None
            spec.delivery_city = _value(match.group(0), city, evidence.evidence_id)
            assign("delivery_city", old, city, evidence.evidence_id)

        found = capture(r"按(?:公司|团队)标准")
        if found:
            match, evidence = found
            authorization = _value(
                match.group(0), "developer_laptop_baseline", evidence.evidence_id
            )
            if not any(
                item.normalized_value == authorization.normalized_value
                for item in spec.default_authorizations
            ):
                spec.default_authorizations.append(authorization)
                assign(
                    "default_authorizations",
                    None,
                    authorization.normalized_value,
                    evidence.evidence_id,
                )

        for match, months in warranty_mentions(normalized):
            evidence = _evidence(message_id, text, match)
            spec.evidence.append(evidence)
            self._upsert_constraint(spec, "minimum_warranty_months", "GTE", months,
                                    ConstraintStrength.HARD, evidence.evidence_id)
            operations.append(RequirementDeltaOperation(
                op="REPLACE", path="minimum_warranty_months", new_value=months,
                evidence_ids=[evidence.evidence_id],
            ))

        days_match = capture(r"(?:最好|优先|必须|最晚)?\s*(?:两周|2\s*周|\d+\s*天)(?:内)?")
        if days_match:
            match, evidence = days_match
            days = 14 if ("周" in match.group(0)) else int(re.search(r"\d+", match.group(0)).group(0))
            old = spec.delivery_days.normalized_value if spec.delivery_days else None
            spec.delivery_days = _value(match.group(0), days, evidence.evidence_id)
            assign("delivery_days", old, days, evidence.evidence_id)
            strength = (
                ConstraintStrength.SOFT
                if any(token in match.group(0) for token in ("最好", "优先"))
                else ConstraintStrength.HARD
            )
            self._upsert_constraint(
                spec, "delivery_days", "LTE", days, strength, evidence.evidence_id
            )

        mentions = capacity_mentions(normalized)
        for field in {item.field for item in mentions}:
            hard_operators = {item.operator for item in mentions if item.field == field and not item.soft}
            # A newly stated bound supersedes the same old bound; a soft wish
            # does not delete an already confirmed hard floor.
            spec.hard_constraints[:] = [
                item for item in spec.hard_constraints
                if item.field != field or item.operator not in hard_operators
            ]
        for mention in mentions:
            evidence = RequirementEvidence(
                message_id=message_id, quote=text[mention.start:mention.end],
                start=mention.start, end=mention.end,
            )
            spec.evidence.append(evidence)
            strength = ConstraintStrength.SOFT if mention.soft else ConstraintStrength.HARD
            self._upsert_constraint(spec, mention.field, mention.operator, mention.value,
                                    strength, evidence.evidence_id)
            operations.append(RequirementDeltaOperation(
                op="REPLACE", path=mention.field, new_value=mention.value,
                evidence_ids=[evidence.evidence_id],
            ))

        for match, condition in _condition_mentions(normalized):
            evidence = _evidence(message_id, text, match)
            spec.evidence.append(evidence)
            strength = ConstraintStrength.SOFT if match.group("qualifier") in {"最好", "优先", "prefer"} else ConstraintStrength.HARD
            operator = "PREFER" if strength == ConstraintStrength.SOFT else "EQ"
            self._upsert_constraint(spec, "condition", operator, condition, strength, evidence.evidence_id)
            operations.append(RequirementDeltaOperation(
                op="REPLACE", path="condition", new_value=condition,
                evidence_ids=[evidence.evidence_id],
            ))

        for match in re.finditer(
            r"(?:必须|只能|最好|优先)?\s*(windows|macos|linux)", normalized
        ):
            evidence = _evidence(message_id, text, match)
            spec.evidence.append(evidence)
            strength = (
                ConstraintStrength.SOFT
                if any(token in match.group(0) for token in ("最好", "优先"))
                else ConstraintStrength.HARD
            )
            operator = "PREFER" if strength == ConstraintStrength.SOFT else "EQ"
            self._upsert_constraint(
                spec,
                "operating_system",
                operator,
                match.group(1),
                strength,
                evidence.evidence_id,
            )
            operations.append(
                RequirementDeltaOperation(
                    op="REPLACE",
                    path="operating_system",
                    new_value=match.group(1),
                    evidence_ids=[evidence.evidence_id],
                )
            )

        spec.conflicts = self._detect_conflicts(spec)
        spec.delta = RequirementDelta(
            from_version=base_version,
            to_version=base_version + 1,
            operations=operations,
        )
        self._parse_no_preference(spec, text, message_id)
        self._sync_explicit_resolutions(spec)
        spec.conflicts = self._detect_conflicts(spec)
        spec.clarification_plan = ClarificationPlan()
        RequirementCompletenessPolicy.refresh(spec)
        spec.clarification_plan.repaired_question_count = 0
        return spec

    @staticmethod
    def _upsert_constraint(
        spec: RequirementSpec,
        field: str,
        operator: str,
        value: Any,
        strength: ConstraintStrength,
        evidence_id: str,
    ) -> None:
        target = spec.hard_constraints if strength == ConstraintStrength.HARD else spec.soft_preferences
        if any(token in spec.evidence[-1].quote for token in ("改为", "改成", "现在")):
            target[:] = [item for item in target if item.field != field]
        target.append(
            RequirementConstraint(
                field=field,
                operator=operator,
                value=value,
                strength=strength,
                confidence=0.98,
                evidence_ids=[evidence_id],
            )
        )

    @staticmethod
    def _detect_conflicts(spec: RequirementSpec) -> list[RequirementConflict]:
        conflicts: list[RequirementConflict] = []
        for field in sorted({item.field for item in spec.hard_constraints}):
            items = [item for item in spec.hard_constraints if item.field == field]
            gte = [item for item in items if item.operator == "GTE"]
            lte = [item for item in items if item.operator == "LTE"]
            if gte and lte and max(float(item.value) for item in gte) > min(float(item.value) for item in lte):
                conflicts.append(
                    RequirementConflict(
                        conflict_id="conf_" + sha256((field + ":" + ":".join(sorted(item.constraint_id for item in items))).encode()).hexdigest()[:12],
                        field=field,
                        constraint_ids=[max(gte, key=lambda x: float(x.value)).constraint_id, min(lte, key=lambda x: float(x.value)).constraint_id],
                        reason="最低值高于最高值，约束区间为空",
                    )
                )
            equalities = [item for item in items if item.operator == "EQ"]
            if len({str(item.value).lower() for item in equalities}) > 1:
                conflicts.append(
                    RequirementConflict(
                        conflict_id="conf_" + sha256((field + ":" + ":".join(sorted(item.constraint_id for item in items))).encode()).hexdigest()[:12],
                        field=field,
                        constraint_ids=[item.constraint_id for item in equalities],
                        reason="同一字段存在多个互斥的必选值",
                    )
                )
        return conflicts

    def validate(
        self,
        proposed: RequirementSpec,
        *,
        message_id: str,
        text: str,
        base: RequirementSpec | None,
    ) -> RequirementSpec:
        if proposed.schema_version not in {"1.0", "2.0"}:
            raise ValueError("Unsupported requirement schema version")
        expected_base = base.version if base else 0
        if proposed.authority != RequirementAuthority.PROPOSED:
            raise ValueError("Only PROPOSED RequirementSpec may cross the Agent boundary")
        if proposed.run_id != (base.run_id if base else proposed.run_id):
            raise ValueError("RequirementSpec run_id does not match its base")
        if base is not None and proposed.spec_id != base.spec_id:
            raise ValueError("RequirementSpec identity must remain stable across versions")
        if proposed.base_version != expected_base or proposed.version != expected_base + 1:
            raise ValueError("RequirementSpec version must advance its base by exactly one")
        if proposed.source_message_id != message_id:
            raise ValueError("RequirementSpec source_message_id does not match current input")
        historical_evidence = {
            item.evidence_id: item for item in (base.evidence if base else [])
        }
        for evidence in proposed.evidence:
            if evidence.kind != "USER":
                if historical_evidence.get(evidence.evidence_id) != evidence:
                    raise ValueError("Unknown external requirement evidence")
                continue
            if evidence.start is None or evidence.end is None:
                raise ValueError("User evidence requires character offsets")
            if evidence.message_id == message_id:
                if (
                    evidence.end > len(text)
                    or text[evidence.start : evidence.end].lower()
                    != evidence.quote.lower()
                ):
                    # Character offsets are easy for providers to count incorrectly around
                    # punctuation. An exact, unique quote is safe to realign server-side.
                    starts = [
                        match.start()
                        for match in re.finditer(
                            re.escape(evidence.quote), text, flags=re.IGNORECASE
                        )
                    ]
                    if len(starts) != 1:
                        raise ValueError(
                            "Requirement evidence offsets do not match the source message"
                        )
                    evidence.start = starts[0]
                    evidence.end = starts[0] + len(evidence.quote)
            elif historical_evidence.get(evidence.evidence_id) != evidence:
                raise ValueError("RequirementSpec contains unknown historical evidence")
        if base is not None:
            external_ids = {e.evidence_id for e in base.evidence if e.kind != "USER"}
            for path in DEDICATED_CORE_FIELDS:
                obj = proposed.budget if path.startswith("budget.") else proposed
                old_obj = base.budget if path.startswith("budget.") else base
                name = path.split(".")[-1]
                value, old = getattr(obj, name, None), getattr(old_obj, name, None)
                if value is not None and external_ids.intersection(value.evidence_ids) and value != old:
                    raise ValueError("External requirement value cannot be rewritten by a model")
            for c in [*proposed.hard_constraints, *proposed.soft_preferences]:
                if external_ids.intersection(c.evidence_ids) and c not in [*base.hard_constraints, *base.soft_preferences]:
                    raise ValueError("External constraint cannot be rewritten by a model")
        if base is not None:
            external_ids = {e.evidence_id for e in base.evidence if e.kind != "USER"}
            for path in DEDICATED_CORE_FIELDS:
                obj = proposed.budget if path.startswith("budget.") else proposed
                old_obj = base.budget if path.startswith("budget.") else base
                name = path.split(".")[-1]
                value, old = getattr(obj, name, None), getattr(old_obj, name, None)
                if value is not None and external_ids.intersection(value.evidence_ids) and value != old:
                    raise ValueError("External requirement value cannot be rewritten by a model")
            for constraint in [*proposed.hard_constraints, *proposed.soft_preferences]:
                if external_ids.intersection(constraint.evidence_ids) and constraint not in [*base.hard_constraints, *base.soft_preferences]:
                    raise ValueError("External constraint cannot be rewritten by a model")
        current_quantity = _quantity_mention(text.lower())
        quantity_refs = [e for e in proposed.evidence
                         if proposed.quantity is not None and e.evidence_id in proposed.quantity.evidence_ids
                         and e.kind == "USER" and e.message_id == message_id]
        if (current_quantity is None and quantity_refs
                and all(is_background(text.lower(), e.start, e.end) for e in quantity_refs)):
            raise ValueError("Background quantity is not a current requirement numeric value")
        if current_quantity is None and len({m.group(1) for m in quantity_candidates(text.lower())}) > 1 and proposed.quantity is not None:
            raise ValueError("Ambiguous current quantities require clarification")
        if (current_quantity is not None and proposed.quantity is not None
                and proposed.quantity.normalized_value != int(current_quantity.group(1))):
            raise ValueError("Requirement quantity ignores the current explicit quantity or correction")
        current_budget = _budget_mention(text.lower())
        if current_budget is not None and current_budget[1] and (
            proposed.budget.maximum is not None or proposed.budget.target is not None
        ):
            raise ValueError("Total budget cannot be used as a per-item budget without user confirmation")
        retractions = budget_retractions(text.lower())
        if (retractions and not (current_budget and not current_budget[1]
                                 and current_budget[0].start() > retractions[-1].end())
                and (proposed.budget.maximum is not None or proposed.budget.target is not None)):
            raise ValueError("A withdrawn budget must remain unresolved")
        for value in (proposed.budget.maximum, proposed.budget.target):
            if value is None:
                continue
            quotes = " ".join(e.quote for e in proposed.evidence
                              if e.evidence_id in value.evidence_ids and e.message_id == message_id
                              and e.kind == "USER")
            currency = _explicit_currency(quotes)
            mentions = list(re.finditer(_BUDGET_PATTERN, text.lower()))
            if currency is None and len(mentions) == 1:
                currency = _explicit_currency(mentions[0].group(0))
            if currency is not None and value.normalized_value.currency != currency:
                raise ValueError("Requirement budget currency conflicts with current user evidence")
        # Numeric values must be present in their exact user evidence. External values are
        # applied by the trusted reference resolver, not invented in a USER quote.
        for value, old in ((proposed.quantity, base.quantity if base else None),
                           (proposed.budget.maximum, base.budget.maximum if base else None),
                           (proposed.budget.target, base.budget.target if base else None)):
            if value is None or value == old:
                continue
            refs = [e for e in proposed.evidence if e.evidence_id in value.evidence_ids]
            if refs and all(e.kind == "USER" and e.message_id == message_id for e in refs):
                quoted = " ".join(e.quote for e in refs)
                normalized_value = value.normalized_value.amount if hasattr(value.normalized_value, "amount") else value.normalized_value
                digits = [Decimal(m[1]) * (10000 if m[2] == "万" else 1000 if m[2] == "千" else 1)
                          for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(万|千)?", quoted)]
                for match in re.finditer(r"[零一二三四五六七八九十百千两万]+", quoted):
                    total = section = number = 0
                    for char in match[0]:
                        if char in "零一二三四五六七八九两":
                            number = {"零":0,"一":1,"二":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9,"两":2}[char]
                        elif char == "万":
                            total += (section + number or 1) * 10000
                            section = number = 0
                        else:
                            section += (number or 1) * {"十":10,"百":100,"千":1000}[char]
                            number = 0
                    digits.append(Decimal(total + section + number))
                try:
                    numeric = Decimal(str(normalized_value))
                except (InvalidOperation, TypeError) as exc:
                    raise ValueError("Requirement numeric value is invalid") from exc
                if numeric not in digits:
                    raise ValueError("Requirement numeric value is not grounded in user evidence")
        self._supplement_explicit_omissions(
            proposed,
            message_id=message_id,
            text=text,
            base=base,
        )
        # EXPLICIT metadata is derived from actual values, never an independent source of truth.
        proposed.field_resolutions = FieldResolutions.model_validate({
            key: resolution for key, resolution in proposed.field_resolutions.items()
            if resolution.state in {FieldResolutionState.NO_PREFERENCE, FieldResolutionState.AUTHORIZED_DEFAULT, FieldResolutionState.CONFIRMED_REFERENCE}
        })
        self._sync_explicit_resolutions(proposed)
        evidence_ids = {item.evidence_id for item in proposed.evidence}
        referenced = {
            ref
            for item in [proposed.category, proposed.purpose, proposed.quantity, proposed.budget.maximum, proposed.budget.target, proposed.budget.tax_included, proposed.delivery_city, proposed.delivery_days, *proposed.default_authorizations]
            if item is not None
            for ref in item.evidence_ids
        }
        referenced.update(ref for item in [*proposed.hard_constraints, *proposed.soft_preferences] for ref in item.evidence_ids)
        referenced.update(ref for item in proposed.field_resolutions.values() for ref in item.evidence_ids)
        if not referenced.issubset(evidence_ids):
            raise ValueError("RequirementSpec contains unresolved evidence references")
        # Historical values retain evidence from an earlier version; only new refs must resolve here.
        derived_delta_roots = {
            "evidence",
            "missing_fields",
            "next_question",
            "clarification_plan",
            "reference_bindings",
            "conflicts",
            "status",
        }
        new_refs = {
            ref
            for op in proposed.delta.operations
            if op.path.strip("/").split("/", 1)[0] not in derived_delta_roots
            for ref in op.evidence_ids
        }
        for operation in proposed.delta.operations:
            path = operation.path.strip("/").replace("/", ".")
            if path not in {"budget.maximum", "budget.target"} or operation.new_value is not None:
                continue
            if operation.op not in {"REMOVE", "REPLACE"}:
                continue
            old_value = getattr(base.budget, path.split(".")[1]) if base else None
            scope_change = current_budget is not None and current_budget[1]
            allowed_spans = (
                [current_budget[0]] if scope_change else []
            ) + budget_retractions(text.lower())
            proof = [e for e in proposed.evidence if e.evidence_id in operation.evidence_ids]
            grounded = proof and len(proof) == len(set(operation.evidence_ids)) and all(
                e.kind == "USER" and e.message_id == message_id
                and any(m.start() <= e.start and e.end <= m.end() for m in allowed_spans)
                and (bool(budget_retractions(e.quote.lower()))
                     or (scope_change and e.start == current_budget[0].start()
                         and e.end == current_budget[0].end()))
                for e in proof
            )
            previous = operation.old_value
            if isinstance(previous, dict) and "normalized_value" in previous:
                previous = previous["normalized_value"]
            try:
                old_matches = old_value is not None and NormalizedMoneyValue.model_validate(previous) == old_value.normalized_value
            except ValueError:
                old_matches = False
            if (not grounded or not old_matches
                    or getattr(proposed.budget, path.split(".")[1]) is not None):
                raise ValueError("Budget removal lacks grounded scope-change evidence")
            operation.op = "REMOVE"
            operation.path = path
            operation.old_value = old_value.normalized_value.model_dump(mode="json")
            referenced.update(operation.evidence_ids)
        if not new_refs.issubset(evidence_ids) or not new_refs.issubset(referenced):
            raise ValueError("Requirement delta contains ungrounded evidence references")
        if proposed.delta.from_version != expected_base or proposed.delta.to_version != proposed.version:
            raise ValueError("RequirementDelta version does not match RequirementSpec")
        evidence_by_id = {item.evidence_id: item for item in proposed.evidence}
        self._validate_resolutions(proposed, base=base, message_id=message_id)
        for collection_name, expected_strength in (
            ("hard_constraints", ConstraintStrength.HARD),
            ("soft_preferences", ConstraintStrength.SOFT),
        ):
            grounded: list[RequirementConstraint] = []
            for constraint in getattr(proposed, collection_name):
                if constraint.field not in ALLOWED_CONSTRAINT_FIELDS | DEDICATED_CORE_FIELDS:
                    raise ValueError(f"Unknown requirement constraint field: {constraint.field}")
                if constraint.strength != expected_strength:
                    raise ValueError(
                        f"{collection_name} contains an item with the wrong strength"
                    )
                quotes = " ".join(
                    evidence_by_id[ref].quote for ref in constraint.evidence_ids
                )
                if any(token in quotes for token in ("最好", "优先", "倾向")) and (
                    constraint.strength != ConstraintStrength.SOFT
                ):
                    raise ValueError("A soft preference was promoted to a hard constraint")
                if any(
                    token in quotes
                    for token in ("必须", "至少", "最低", "最多", "不超过")
                ) and constraint.strength != ConstraintStrength.HARD:
                    raise ValueError("An explicit hard constraint was weakened")
                inherited_default = (
                    base is not None
                    and constraint in base.hard_constraints
                    and base.field_resolutions.get(constraint.field) is not None
                    and base.field_resolutions[constraint.field].state == FieldResolutionState.AUTHORIZED_DEFAULT
                )
                try:
                    inherited_consent = (base is not None and base.authority == RequirementAuthority.VALIDATED
                        and constraint in [*base.hard_constraints, *base.soft_preferences]
                        and all(ref in historical_evidence for ref in constraint.evidence_ids)
                        and any((historical_evidence[ref].source_ref or "").startswith(f"negotiation://{base.run_id}/") for ref in constraint.evidence_ids))
                    inherited_reference = base is not None and constraint in [*base.hard_constraints, *base.soft_preferences] and all(ref in historical_evidence and historical_evidence[ref].kind != "USER" for ref in constraint.evidence_ids)
                    if not inherited_default and not inherited_consent and not inherited_reference:
                        self._validate_constraint_value(constraint, quotes)
                except ValueError:
                    if constraint.field == "condition" and _condition_mentions(text.lower()):
                        raise ValueError("Condition proposal conflicts with affirmative current user evidence") from None
                    # Unsupported inferred facts are discarded at the trust boundary.
                    # Required fields will consequently be recomputed as missing.
                    continue
                grounded.append(constraint)
            setattr(proposed, collection_name, grounded)
        constraint_ids = [c.constraint_id for c in [*proposed.hard_constraints, *proposed.soft_preferences]]
        if len(constraint_ids) != len(set(constraint_ids)):
            raise ValueError("Requirement constraints must have unique IDs")
        validated = proposed.model_copy(deep=True)
        normalize_requirement_values(validated)
        self._remove_redundant_core_constraints(validated)
        self._canonicalize_delta(validated, message_id=message_id)
        validated.schema_version = "2.0"
        validated.authority = RequirementAuthority.VALIDATED
        validated.conflicts = self._detect_conflicts(validated)
        self._sync_explicit_resolutions(validated)
        RequirementCompletenessPolicy.refresh(validated)
        return validated

    def _supplement_explicit_omissions(
        self,
        proposed: RequirementSpec,
        *,
        message_id: str,
        text: str,
        base: RequirementSpec | None,
    ) -> None:
        """Recover only exact, rule-extractable facts omitted by a model response."""
        fallback = self.propose(
            run_id=proposed.run_id,
            message_id=message_id,
            text=text,
            base=base,
        )
        merged_paths: set[str] = set()
        used_evidence: set[str] = set()

        for field in (
            "category",
            "purpose",
            "quantity",
            "delivery_city",
            "delivery_days",
        ):
            current = getattr(proposed, field)
            replacement = getattr(fallback, field)
            if current is None and replacement is not None:
                copied = replacement.model_copy(deep=True)
                setattr(proposed, field, copied)
                merged_paths.add(field)
                used_evidence.update(copied.evidence_ids)

        for field in ("maximum", "target", "tax_included"):
            current = getattr(proposed.budget, field)
            replacement = getattr(fallback.budget, field)
            if current is None and replacement is not None:
                copied = replacement.model_copy(deep=True)
                setattr(proposed.budget, field, copied)
                merged_paths.add(f"budget.{field}")
                used_evidence.update(copied.evidence_ids)

        known_authorizations = {
            str(item.normalized_value) for item in proposed.default_authorizations
        }
        for authorization in fallback.default_authorizations:
            if str(authorization.normalized_value) in known_authorizations:
                continue
            copied = authorization.model_copy(deep=True)
            proposed.default_authorizations.append(copied)
            merged_paths.add("default_authorizations")
            used_evidence.update(copied.evidence_ids)

        for field in ("minimum_ram_gb", "minimum_storage_gb", "condition"):
            # A preference does not remove a previously confirmed hard floor.
            for inherited in fallback.hard_constraints:
                if inherited.field != field or base is None or inherited not in base.hard_constraints:
                    continue
                current_ids = {e.evidence_id for e in proposed.evidence if e.message_id == message_id}
                if any(item.field == field and item.operator == inherited.operator
                       and current_ids.intersection(item.evidence_ids)
                       for item in proposed.hard_constraints):
                    continue
                if any(
                    item.field == field and item.operator == inherited.operator and item.value == inherited.value
                    for item in proposed.hard_constraints
                ):
                    continue
                resolution = proposed.field_resolutions.get(field)
                if resolution is not None and resolution.state == FieldResolutionState.NO_PREFERENCE:
                    continue
                proposed.hard_constraints.append(inherited.model_copy(deep=True))
                used_evidence.update(inherited.evidence_ids)
            if any(
                item.field == field
                for item in [*proposed.hard_constraints, *proposed.soft_preferences]
            ):
                continue
            replacements = [
                item
                for item in [*fallback.hard_constraints, *fallback.soft_preferences]
                if item.field == field
            ]
            for replacement in replacements:
                copied = replacement.model_copy(deep=True)
                target = (
                    proposed.hard_constraints
                    if copied.strength == ConstraintStrength.HARD
                    else proposed.soft_preferences
                )
                target.append(copied)
                used_evidence.update(copied.evidence_ids)
            if replacements:
                merged_paths.add(field)

        for field, resolution in fallback.field_resolutions.items():
            if resolution.state != FieldResolutionState.NO_PREFERENCE:
                continue
            if field in proposed.field_resolutions:
                continue
            if any(item.field == field for item in proposed.hard_constraints):
                continue
            proposed.field_resolutions[field] = resolution.model_copy(deep=True)
            used_evidence.update(resolution.evidence_ids)
            merged_paths.add(f"field_resolutions/{field}")

        existing_evidence = {item.evidence_id for item in proposed.evidence}
        proposed.evidence.extend(
            item.model_copy(deep=True)
            for item in fallback.evidence
            if item.evidence_id in used_evidence
            and item.evidence_id not in existing_evidence
        )
        proposed.delta.operations.extend(
            operation.model_copy(deep=True)
            for operation in fallback.delta.operations
            if operation.path.strip("/") in merged_paths
        )

    @staticmethod
    def _validate_constraint_value(
        constraint: RequirementConstraint, quotes: str
    ) -> None:
        normalized_quote = quotes.lower()
        if constraint.field == "condition":
            allowed = {value for _, value in _condition_mentions(normalized_quote)}
            if str(constraint.value).upper() not in allowed:
                raise ValueError("Condition constraint is not grounded by affirmative evidence")
        elif constraint.field == "operating_system":
            if str(canonical_operating_system(constraint.value)).casefold() not in normalized_quote:
                raise ValueError("Operating-system constraint is not grounded by its evidence")
        elif constraint.field in {"brand_preference", "requested_model_text"}:
            if str(constraint.value).lower() not in normalized_quote:
                raise ValueError(f"{constraint.field} is not grounded by its evidence")
        elif constraint.field == "minimum_warranty_months":
            values = {months for _, months in warranty_mentions(normalized_quote)}
            if Decimal(str(constraint.value)) not in values:
                raise ValueError("Warranty constraint is not grounded by its evidence")
        elif constraint.field in {"minimum_ram_gb", "minimum_storage_gb"}:
            # Compare exact, field-scoped quantities after unit conversion. Never
            # accept 16 merely because the quote happens to contain 116/1536.
            mentions = [m for m in capacity_mentions(
                normalized_quote, default_field=constraint.field
            ) if m.field == constraint.field]
            if constraint.operator in {"GTE", "LTE"}:
                mentions = [m for m in mentions if m.operator == constraint.operator]
            values = {m.value for m in mentions}
            if len(values) == 1:
                # An exact user quote with one field-scoped quantity is a safe
                # source of canonical units, even if the model used 1000/TB.
                constraint.value = next(iter(values))
            elif Decimal(str(constraint.value)) not in values:
                raise ValueError(f"{constraint.field} is not grounded by its evidence")

    @staticmethod
    def _core_value(spec: RequirementSpec, field: str) -> Any:
        if field.startswith("budget."):
            item = getattr(spec.budget, field.split(".", 1)[1])
        else:
            item = getattr(spec, field)
        if item is None:
            return None
        normalized = item.normalized_value
        if hasattr(normalized, "model_dump"):
            return normalized.model_dump(mode="json")
        return normalized

    @classmethod
    def _remove_redundant_core_constraints(cls, spec: RequirementSpec) -> None:
        for collection_name in ("hard_constraints", "soft_preferences"):
            kept: list[RequirementConstraint] = []
            for constraint in getattr(spec, collection_name):
                if constraint.field not in DEDICATED_CORE_FIELDS:
                    kept.append(constraint)
                    continue
                expected = cls._core_value(spec, constraint.field)
                actual = constraint.value
                if isinstance(expected, dict) and isinstance(actual, dict):
                    matches = {
                        key: str(value) for key, value in expected.items()
                    } == {key: str(value) for key, value in actual.items()}
                else:
                    matches = str(expected).lower() == str(actual).lower()
                if not matches:
                    raise ValueError(
                        f"Constraint {constraint.field} conflicts with its dedicated field"
                    )
            setattr(spec, collection_name, kept)

    @staticmethod
    def _canonicalize_delta(spec: RequirementSpec, *, message_id: str) -> None:
        derived = {"evidence", "missing_fields", "next_question", "clarification_plan", "conflicts", "status"}
        current_evidence = {
            item.evidence_id for item in spec.evidence if item.message_id == message_id
        }
        operations: list[RequirementDeltaOperation] = []
        constraint_roots_with_leaf_ops: set[str] = set()
        for operation in spec.delta.operations:
            path = operation.path.strip("/")
            if path.startswith(("hard_constraints.", "soft_preferences.")):
                path = path.replace(".", "/")
            parts = [part for part in path.split("/") if part]
            if len(parts) > 1 and parts[0] in {"hard_constraints", "soft_preferences"}:
                collection = getattr(spec, parts[0])
                matches = [c for c in collection if c.constraint_id == parts[1]]
                if not matches:
                    value = operation.new_value
                    expected = value.get("value") if isinstance(value, dict) else value
                    matches = [c for c in collection if c.field == parts[1]
                               and (expected is None or c.value == expected)
                               and set(c.evidence_ids).intersection(operation.evidence_ids)]
                if len(matches) == 1:
                    parts = [parts[0], matches[0].constraint_id]
                    operation = operation.model_copy(update={"new_value": matches[0].model_dump(mode="json")})
            if not parts or parts[0] in derived:
                continue
            if parts[0] in {"hard_constraints", "soft_preferences"}:
                if len(parts) == 1:
                    continue
                constraint_roots_with_leaf_ops.add(parts[0])
            operations.append(operation.model_copy(update={"path": "/".join(parts)}))
        for collection_name in ("hard_constraints", "soft_preferences"):
            if collection_name in constraint_roots_with_leaf_ops:
                continue
            for constraint in getattr(spec, collection_name):
                refs = set(constraint.evidence_ids)
                if not refs.intersection(current_evidence):
                    continue
                operations.append(
                    RequirementDeltaOperation(
                        op="ADD",
                        path=f"{collection_name}/{constraint.constraint_id}",
                        new_value=constraint.model_dump(mode="json"),
                        evidence_ids=constraint.evidence_ids,
                    )
                )
        spec.delta = RequirementDelta(
            from_version=spec.base_version,
            to_version=spec.version,
            operations=operations,
        )

    @staticmethod
    def apply_authorized_defaults(
        spec: RequirementSpec, *, minimum_ram_gb: int, minimum_storage_gb: int, source_ref: str = "requirement-defaults://developer_laptop/v1"
    ) -> RequirementSpec:
        if spec.category is None or spec.category.normalized_value != "laptop":
            return spec
        if not any(
            item.normalized_value == "developer_laptop_baseline"
            for item in spec.default_authorizations
        ):
            return spec
        validated = spec.model_copy(deep=True)
        evidence_id = validated.default_authorizations[-1].evidence_ids[0]
        if (
            RequirementCompletenessPolicy._constraint(validated, "minimum_ram_gb") is None
            and validated.field_resolutions.get("minimum_ram_gb", FieldResolution(state=FieldResolutionState.UNRESOLVED)).state != FieldResolutionState.NO_PREFERENCE
        ):
            RequirementSpecService._upsert_constraint(
                validated,
                "minimum_ram_gb",
                "GTE",
                minimum_ram_gb,
                ConstraintStrength.HARD,
                evidence_id,
            )
            validated.delta.operations.append(
                RequirementDeltaOperation(
                    op="ADD",
                    path="minimum_ram_gb",
                    new_value=minimum_ram_gb,
                    evidence_ids=[evidence_id],
                )
            )
        if (
            RequirementCompletenessPolicy._constraint(validated, "minimum_storage_gb") is None
            and validated.field_resolutions.get("minimum_storage_gb", FieldResolution(state=FieldResolutionState.UNRESOLVED)).state != FieldResolutionState.NO_PREFERENCE
        ):
            RequirementSpecService._upsert_constraint(
                validated,
                "minimum_storage_gb",
                "GTE",
                minimum_storage_gb,
                ConstraintStrength.HARD,
                evidence_id,
            )
            validated.delta.operations.append(
                RequirementDeltaOperation(
                    op="ADD",
                    path="minimum_storage_gb",
                    new_value=minimum_storage_gb,
                    evidence_ids=[evidence_id],
                )
            )
        for field in NO_PREFERENCE_FIELDS:
            constraint = RequirementCompletenessPolicy._constraint(validated, field)
            if constraint and evidence_id in constraint.evidence_ids:
                validated.field_resolutions[field] = FieldResolution(
                    state=FieldResolutionState.AUTHORIZED_DEFAULT,
                    evidence_ids=[evidence_id], source_ref=(
                        source_ref if field == "minimum_ram_gb" else "requirement-defaults://developer_laptop/v1"
                    ),
                )
        RequirementSpecService._sync_explicit_resolutions(validated)
        RequirementCompletenessPolicy.refresh(validated)
        return validated

    @staticmethod
    def to_procurement_requirement(spec: RequirementSpec) -> ProcurementRequirement:
        def hard(field: str) -> Any:
            resolution = spec.field_resolutions.get(field)
            if resolution and resolution.state == FieldResolutionState.NO_PREFERENCE:
                return None
            values = [item.value for item in spec.hard_constraints if item.field == field and item.operator in {"GTE", "EQ"}]
            if field in NO_PREFERENCE_FIELDS:
                return max(values, key=int) if values else None
            return values[-1] if values else None

        return ProcurementRequirement(
            category=str(spec.category.normalized_value) if spec.category else "laptop",
            quantity=int(spec.quantity.normalized_value) if spec.quantity else None,
            unit_budget=(
                Money(
                    amount=spec.budget.maximum.normalized_value.amount,
                    currency=spec.budget.maximum.normalized_value.currency,
                )
                if spec.budget.maximum
                else None
            ),
            budget_tax_included=(bool(spec.budget.tax_included.normalized_value) if spec.budget.tax_included else None),
            delivery_city=(str(canonical_city(spec.delivery_city.normalized_value)) if spec.delivery_city else None),
            delivery_days=(int(spec.delivery_days.normalized_value) if spec.delivery_days and not any(marker in " ".join(e.quote for e in spec.evidence if e.evidence_id in spec.delivery_days.evidence_ids) for marker in ("最好", "优先", "倾向")) else None),
            minimum_warranty_months=(int(hard("minimum_warranty_months")) if hard("minimum_warranty_months") else None),
            minimum_ram_gb=(int(hard("minimum_ram_gb")) if hard("minimum_ram_gb") is not None else None),
            minimum_storage_gb=(int(hard("minimum_storage_gb")) if hard("minimum_storage_gb") is not None else None),
            operating_system=(str(canonical_operating_system(hard("operating_system"))) if hard("operating_system") is not None else None),
            condition=(ProductCondition(str(hard("condition")).upper()) if hard("condition") is not None else ProductCondition.NEW),
            field_resolutions={key: item.model_copy(deep=True) for key, item in spec.field_resolutions.items()},
            assumptions=[
                f"用户授权按公司标准设置{FIELD_LABELS.get(field, field)}为 {hard(field)}GB"
                for field, resolution in spec.field_resolutions.items()
                if resolution.state == FieldResolutionState.AUTHORIZED_DEFAULT
            ],
            version=spec.version,
        )


    @staticmethod
    def _parse_no_preference(spec: RequirementSpec, text: str, message_id: str) -> None:
        for match in re.finditer(r"[^，,。；;\n]+", text):
            quote = match.group(0)
            if not any(marker in quote for marker in NO_PREFERENCE_MARKERS):
                continue
            # Never erase a numeric constraint from an ambiguous mixed clause.
            if re.search(r"\d", quote):
                continue
            fields = [
                field for field in NO_PREFERENCE_FIELDS
                if any(token in quote.lower() for token in FIELD_TOPICS[field]) or "配置" in quote
            ]
            if not fields:
                continue
            evidence = _evidence(message_id, text, match)
            spec.evidence.append(evidence)
            for field in fields:
                previous = spec.field_resolutions.get(field)
                spec.field_resolutions[field] = FieldResolution(
                    state=FieldResolutionState.NO_PREFERENCE, evidence_ids=[evidence.evidence_id]
                )
                for collection in ("hard_constraints", "soft_preferences"):
                    removed = [item for item in getattr(spec, collection) if item.field == field]
                    for item in removed:
                        spec.delta.operations.append(RequirementDeltaOperation(
                            op="REMOVE", path=f"{collection}/{item.constraint_id}",
                            old_value=item.model_dump(mode="json"), evidence_ids=[evidence.evidence_id],
                        ))
                    setattr(spec, collection, [item for item in getattr(spec, collection) if item.field != field])
                spec.delta.operations.append(RequirementDeltaOperation(
                    op="REPLACE" if previous else "ADD", path=f"field_resolutions/{field}",
                    old_value=previous.model_dump(mode="json") if previous else None,
                    new_value=spec.field_resolutions[field].model_dump(mode="json"),
                    evidence_ids=[evidence.evidence_id],
                ))

    @staticmethod
    def _sync_explicit_resolutions(spec: RequirementSpec) -> None:
        current_ids = {item.evidence_id for item in spec.evidence if item.message_id == spec.source_message_id}
        values = {
            "category": spec.category, "quantity": spec.quantity,
            "budget.maximum": spec.budget.maximum, "budget.tax_included": spec.budget.tax_included,
            "delivery_city": spec.delivery_city,
        }
        for field in NO_PREFERENCE_FIELDS:
            values[field] = RequirementCompletenessPolicy._constraint(spec, field)
        for field, value in values.items():
            if value is None:
                continue
            old = spec.field_resolutions.get(field)
            if (
                old is not None and old.state in {FieldResolutionState.AUTHORIZED_DEFAULT, FieldResolutionState.CONFIRMED_REFERENCE}
                and set(old.evidence_ids) == set(value.evidence_ids)
            ):
                continue
            if (
                old is not None and old.state == FieldResolutionState.NO_PREFERENCE
                and not current_ids.intersection(value.evidence_ids)
            ):
                continue
            spec.field_resolutions[field] = FieldResolution(
                state=FieldResolutionState.EXPLICIT, evidence_ids=value.evidence_ids
            )

    @staticmethod
    def _validate_resolutions(spec: RequirementSpec, *, base: RequirementSpec | None, message_id: str) -> None:
        evidence = {item.evidence_id: item for item in spec.evidence}
        allowed = {item.path for item in REQUIRED_FIELD_POLICY}
        for field, resolution in spec.field_resolutions.items():
            if field not in allowed:
                raise ValueError(f"Unknown resolution field: {field}")
            inherited = base is not None and base.field_resolutions.get(field) == resolution
            if resolution.state in {FieldResolutionState.AUTHORIZED_DEFAULT, FieldResolutionState.CONFIRMED_REFERENCE} and not inherited:
                raise ValueError("Only the server may resolve authorized defaults")
            if resolution.state != FieldResolutionState.NO_PREFERENCE:
                continue
            if field not in NO_PREFERENCE_FIELDS:
                raise ValueError(f"No preference is not allowed for {field}")
            quotes = " ".join(evidence[ref].quote for ref in resolution.evidence_ids)
            if not inherited and not all(evidence[ref].message_id == message_id for ref in resolution.evidence_ids):
                raise ValueError("A new field resolution requires current user evidence")
            if not any(token in quotes.lower() for token in FIELD_TOPICS[field]) and "配置" not in quotes:
                raise ValueError("No-preference evidence must identify the field")
            if not any(marker in quotes for marker in NO_PREFERENCE_MARKERS) or re.search(r"\d", quotes):
                raise ValueError("No preference is not grounded by its evidence")
            if any(item.field == field for item in spec.hard_constraints):
                raise ValueError("No preference conflicts with an explicit hard constraint")
