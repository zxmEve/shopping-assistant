from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from procure_pilot.domain.models import Money, ProcurementRequirement
from procure_pilot.domain.requirements import (
    ClarificationQuestion,
    ConstraintStrength,
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


@dataclass(frozen=True)
class RequiredFieldPolicy:
    path: str
    impact: float
    question: str


# The single source of truth used by Agent routing, validation and fallback clarification.
REQUIRED_FIELD_POLICY: tuple[RequiredFieldPolicy, ...] = (
    RequiredFieldPolicy("category", 1.00, "需要采购哪一类商品？"),
    RequiredFieldPolicy("budget.maximum", 1.00, "单件预算上限是多少，币种是什么？"),
    RequiredFieldPolicy("quantity", 0.95, "需要采购多少件？"),
    RequiredFieldPolicy("minimum_ram_gb", 0.90, "最低内存要求是多少？"),
    RequiredFieldPolicy("minimum_storage_gb", 0.88, "最低存储容量要求是多少？"),
    RequiredFieldPolicy("delivery_city", 0.85, "具体交付城市是哪里？"),
    RequiredFieldPolicy("budget.tax_included", 0.80, "预算是含税还是未税？"),
)

ALLOWED_CONSTRAINT_FIELDS = frozenset(
    {
        "minimum_ram_gb",
        "minimum_storage_gb",
        "delivery_days",
        "operating_system",
        "condition",
        "brand_preference",
        "requested_model_text",
    }
)

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
            "schema_version": "1.0",
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
            "maximum_questions_per_turn": 1,
            "forbidden_domains": ["product", "offer", "provider", "catalog"],
        }

    @staticmethod
    def _constraint(spec: RequirementSpec, field: str) -> RequirementConstraint | None:
        return next(
            (
                item
                for item in spec.hard_constraints
                if item.field == field and item.operator in {"GTE", "EQ"}
            ),
            None,
        )

    @classmethod
    def missing(cls, spec: RequirementSpec) -> list[MissingRequirementField]:
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
        ]

    @staticmethod
    def next_question(
        missing: list[MissingRequirementField], conflicts: list[RequirementConflict]
    ) -> ClarificationQuestion | None:
        blocking = next((item for item in conflicts if item.blocking), None)
        if blocking:
            return ClarificationQuestion(
                field=blocking.field,
                text=f"你对 {blocking.field} 的要求存在冲突：{blocking.reason}，请确认以哪个为准？",
                impact=1.0,
                resolves_conflict_id=blocking.conflict_id,
            )
        if not missing:
            return None
        selected = max(missing, key=lambda item: item.impact)
        policy = next(item for item in REQUIRED_FIELD_POLICY if item.path == selected.field)
        return ClarificationQuestion(
            field=selected.field,
            text=policy.question,
            impact=selected.impact,
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
        spec.authority = RequirementAuthority.PROPOSED
        spec.base_version = base_version
        spec.version = base_version + 1
        spec.source_message_id = message_id
        spec.conflicts = []
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
            match = re.search(pattern, normalized)
            if not match:
                return None
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

        found = capture(r"(\d+)\s*(?:台|件)")
        if found:
            match, evidence = found
            value = int(match.group(1))
            old = spec.quantity.normalized_value if spec.quantity else None
            spec.quantity = _value(match.group(0), value, evidence.evidence_id)
            assign("quantity", old, value, evidence.evidence_id)

        found = capture(r"(?:单台(?:预算|价格)?|单件(?:预算|价格)?|预算)[^\d]{0,8}(\d{3,7})\s*(?:元|rmb|cny)?")
        if found:
            match, evidence = found
            amount = Decimal(match.group(1)).quantize(Decimal("0.01"))
            target = any(token in normalized for token in ("左右", "大概", "最好"))
            old_value = spec.budget.target if target else spec.budget.maximum
            sourced = _money(match.group(0), amount, "CNY", evidence.evidence_id)
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

        found = capture(r"(?:不含税|未税|含税)")
        if found:
            match, evidence = found
            included = match.group(0) == "含税"
            old = spec.budget.tax_included.normalized_value if spec.budget.tax_included else None
            spec.budget.tax_included = _value(match.group(0), included, evidence.evidence_id)
            assign("budget.tax_included", old, included, evidence.evidence_id)

        found = capture(r"(?:送到|交付到|到货到|送)?\s*(雅加达|jakarta|上海|深圳)")
        if found:
            match, evidence = found
            city = {"雅加达": "Jakarta", "jakarta": "Jakarta", "上海": "Shanghai", "深圳": "Shenzhen"}[match.group(1)]
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

        self._parse_capacity(spec, normalized, text, message_id, "minimum_ram_gb", r"(?:最低|至少|最好|优先|最多|不超过)?\s*(16|32|64|128)\s*(?:g|gb)(?:\s*内存)?", operations)
        self._parse_capacity(spec, normalized, text, message_id, "minimum_storage_gb", r"(?:最低|至少|最好|优先|最多|不超过)?\s*(512|1024|2048)\s*(?:g|gb)(?:\s*存储)?", operations, storage=True)
        found = capture(r"(?:最低|至少|最好|优先)?\s*1\s*tb")
        if found:
            match, evidence = found
            strength = ConstraintStrength.SOFT if any(x in match.group(0) for x in ("最好", "优先")) else ConstraintStrength.HARD
            self._upsert_constraint(spec, "minimum_storage_gb", "GTE", 1024, strength, evidence.evidence_id)
            operations.append(RequirementDeltaOperation(op="REPLACE", path="minimum_storage_gb", new_value=1024, evidence_ids=[evidence.evidence_id]))

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
        spec.missing_fields = RequirementCompletenessPolicy.missing(spec)
        spec.next_question = RequirementCompletenessPolicy.next_question(
            spec.missing_fields, spec.conflicts
        )
        spec.status = (
            RequirementStatus.NEEDS_CLARIFICATION
            if spec.next_question
            else RequirementStatus.READY_FOR_SOURCING
        )
        return spec

    @staticmethod
    def _parse_capacity(
        spec: RequirementSpec,
        normalized: str,
        original: str,
        message_id: str,
        field: str,
        pattern: str,
        operations: list[RequirementDeltaOperation],
        *,
        storage: bool = False,
    ) -> None:
        matches = list(re.finditer(pattern, normalized))
        if matches and any(token in normalized for token in ("改为", "改成", "现在")):
            spec.hard_constraints[:] = [item for item in spec.hard_constraints if item.field != field]
            spec.soft_preferences[:] = [item for item in spec.soft_preferences if item.field != field]
        for match in matches:
            value = int(match.group(1))
            # A bare capacity is RAM by default; storage requires a storage marker.
            if storage and not any(token in match.group(0) for token in ("存储", "硬盘")):
                continue
            if not storage and any(token in match.group(0) for token in ("存储", "硬盘")):
                continue
            evidence = _evidence(message_id, original, match)
            spec.evidence.append(evidence)
            strength = ConstraintStrength.SOFT if any(x in match.group(0) for x in ("最好", "优先")) else ConstraintStrength.HARD
            operator = "LTE" if any(x in match.group(0) for x in ("最多", "不超过")) else "GTE"
            RequirementSpecService._upsert_constraint(spec, field, operator, value, strength, evidence.evidence_id)
            operations.append(RequirementDeltaOperation(op="REPLACE", path=field, new_value=value, evidence_ids=[evidence.evidence_id]))

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
        for field in {item.field for item in spec.hard_constraints}:
            items = [item for item in spec.hard_constraints if item.field == field]
            gte = [item for item in items if item.operator == "GTE"]
            lte = [item for item in items if item.operator == "LTE"]
            if gte and lte and max(float(item.value) for item in gte) > min(float(item.value) for item in lte):
                conflicts.append(
                    RequirementConflict(
                        field=field,
                        constraint_ids=[max(gte, key=lambda x: float(x.value)).constraint_id, min(lte, key=lambda x: float(x.value)).constraint_id],
                        reason="最低值高于最高值，约束区间为空",
                    )
                )
            equalities = [item for item in items if item.operator == "EQ"]
            if len({str(item.value).lower() for item in equalities}) > 1:
                conflicts.append(
                    RequirementConflict(
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
        self._supplement_explicit_omissions(
            proposed,
            message_id=message_id,
            text=text,
            base=base,
        )
        evidence_ids = {item.evidence_id for item in proposed.evidence}
        referenced = {
            ref
            for item in [proposed.category, proposed.purpose, proposed.quantity, proposed.budget.maximum, proposed.budget.target, proposed.budget.tax_included, proposed.delivery_city, proposed.delivery_days, *proposed.default_authorizations]
            if item is not None
            for ref in item.evidence_ids
        }
        referenced.update(ref for item in [*proposed.hard_constraints, *proposed.soft_preferences] for ref in item.evidence_ids)
        if not referenced.issubset(evidence_ids):
            raise ValueError("RequirementSpec contains unresolved evidence references")
        # Historical values retain evidence from an earlier version; only new refs must resolve here.
        derived_delta_roots = {
            "evidence",
            "missing_fields",
            "next_question",
            "conflicts",
            "status",
        }
        new_refs = {
            ref
            for op in proposed.delta.operations
            if op.path.strip("/").split("/", 1)[0] not in derived_delta_roots
            for ref in op.evidence_ids
        }
        if not new_refs.issubset(evidence_ids) or not new_refs.issubset(referenced):
            raise ValueError("Requirement delta contains ungrounded evidence references")
        if proposed.delta.from_version != expected_base or proposed.delta.to_version != proposed.version:
            raise ValueError("RequirementDelta version does not match RequirementSpec")
        evidence_by_id = {item.evidence_id: item for item in proposed.evidence}
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
                try:
                    self._validate_constraint_value(constraint, quotes)
                except ValueError:
                    # Unsupported inferred facts are discarded at the trust boundary.
                    # Required fields will consequently be recomputed as missing.
                    continue
                grounded.append(constraint)
            setattr(proposed, collection_name, grounded)
        validated = proposed.model_copy(deep=True)
        self._remove_redundant_core_constraints(validated)
        self._canonicalize_delta(validated, message_id=message_id)
        validated.authority = RequirementAuthority.VALIDATED
        validated.conflicts = self._detect_conflicts(validated)
        validated.missing_fields = RequirementCompletenessPolicy.missing(validated)
        validated.next_question = RequirementCompletenessPolicy.next_question(
            validated.missing_fields, validated.conflicts
        )
        validated.status = (
            RequirementStatus.NEEDS_CLARIFICATION
            if validated.next_question
            else RequirementStatus.READY_FOR_SOURCING
        )
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

        for field in ("minimum_ram_gb", "minimum_storage_gb"):
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
            markers = {
                "new": ("新", "全新", "new"),
                "refurbished": ("翻新", "refurb"),
                "used": ("二手", "used"),
            }
            value = str(constraint.value).lower()
            expected = markers.get(value)
            if expected is None or not any(token in normalized_quote for token in expected):
                raise ValueError("Condition constraint is not grounded by its evidence")
        elif constraint.field == "operating_system":
            if str(constraint.value).lower() not in normalized_quote:
                raise ValueError("Operating-system constraint is not grounded by its evidence")
        elif constraint.field in {"brand_preference", "requested_model_text"}:
            if str(constraint.value).lower() not in normalized_quote:
                raise ValueError(f"{constraint.field} is not grounded by its evidence")
        elif constraint.field in {"minimum_ram_gb", "minimum_storage_gb"}:
            value = int(constraint.value)
            grounded = str(value) in normalized_quote
            if constraint.field == "minimum_storage_gb" and value == 1024:
                grounded = grounded or bool(re.search(r"1\s*tb", normalized_quote))
            if not grounded:
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
        derived = {"evidence", "missing_fields", "next_question", "conflicts", "status"}
        current_evidence = {
            item.evidence_id for item in spec.evidence if item.message_id == message_id
        }
        operations: list[RequirementDeltaOperation] = []
        constraint_roots_with_leaf_ops: set[str] = set()
        for operation in spec.delta.operations:
            parts = [part for part in operation.path.strip("/").split("/") if part]
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
        spec: RequirementSpec, *, minimum_ram_gb: int, minimum_storage_gb: int
    ) -> RequirementSpec:
        if not any(
            item.normalized_value == "developer_laptop_baseline"
            for item in spec.default_authorizations
        ):
            return spec
        validated = spec.model_copy(deep=True)
        evidence_id = validated.default_authorizations[-1].evidence_ids[0]
        if RequirementCompletenessPolicy._constraint(validated, "minimum_ram_gb") is None:
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
        if RequirementCompletenessPolicy._constraint(validated, "minimum_storage_gb") is None:
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
        validated.missing_fields = RequirementCompletenessPolicy.missing(validated)
        validated.next_question = RequirementCompletenessPolicy.next_question(
            validated.missing_fields, validated.conflicts
        )
        validated.status = (
            RequirementStatus.NEEDS_CLARIFICATION
            if validated.next_question
            else RequirementStatus.READY_FOR_SOURCING
        )
        return validated

    @staticmethod
    def to_procurement_requirement(spec: RequirementSpec) -> ProcurementRequirement:
        def hard(field: str) -> Any:
            values = [item.value for item in spec.hard_constraints if item.field == field and item.operator in {"GTE", "LTE", "EQ"}]
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
            delivery_city=(str(spec.delivery_city.normalized_value) if spec.delivery_city else None),
            delivery_days=(int(spec.delivery_days.normalized_value) if spec.delivery_days else None),
            minimum_ram_gb=(int(hard("minimum_ram_gb")) if hard("minimum_ram_gb") is not None else None),
            minimum_storage_gb=(int(hard("minimum_storage_gb")) if hard("minimum_storage_gb") is not None else None),
            operating_system=(str(hard("operating_system")) if hard("operating_system") is not None else None),
            version=spec.version,
        )
