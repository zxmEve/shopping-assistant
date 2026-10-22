"""Independent semantic labels and scoring for the Requirement Agent boundary.

Gold labels never come from RequirementSpecService.propose(). IDs, confidence and
question wording are intentionally not compared as semantic facts.
"""

from __future__ import annotations

import json
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from procure_pilot.domain.requirements import RequirementSpec

SCORER_VERSION = "requirement-semantic-v1"
CORE_FIELDS = ("category", "quantity", "delivery_city", "delivery_days")
REQUIRED_FIELDS = {
    "category",
    "quantity",
    "budget.maximum",
    "budget.tax_included",
    "delivery_city",
    "minimum_ram_gb",
    "minimum_storage_gb",
}


class Fact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    field: str
    value: Any
    operator: Literal["EQ", "GTE", "LTE", "IN", "PREFER", "STATE"] = "EQ"
    strength: Literal["HARD", "SOFT"] | None = None

    def key(self) -> str:
        return json.dumps(self.model_dump(), ensure_ascii=False, sort_keys=True)


class RequirementCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(pattern=r"^[a-z0-9_-]+$")
    group_id: str
    split: Literal["development", "holdout"]
    category: str
    message: str = Field(min_length=1)
    base_fixture: Literal["complete_laptop"] | None = None
    expected_facts: list[Fact]
    expected_missing: list[str]
    expected_conflicts: list[str] = Field(default_factory=list)
    expected_changes: list[str] | None = None
    rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def unique_facts(self):
        keys = [fact.key() for fact in self.expected_facts]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate gold fact")
        if not set(self.expected_missing) <= REQUIRED_FIELDS:
            raise ValueError("Unknown required field in gold labels")
        if self.expected_changes is not None and self.base_fixture is None:
            raise ValueError("Delta expectations require an explicit base fixture")
        return self


class RequirementDataset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_version: str
    label_status: Literal["draft_engineering_labels", "business_reviewed"]
    scope: str
    cases: list[RequirementCase] = Field(min_length=1)

    @model_validator(mode="after")
    def isolate_groups(self):
        if len({case.case_id for case in self.cases}) != len(self.cases):
            raise ValueError("Duplicate case_id")
        groups: dict[str, str] = {}
        for case in self.cases:
            if groups.setdefault(case.group_id, case.split) != case.split:
                raise ValueError("A paraphrase group cannot cross development/holdout")
        return self


def load_dataset(path: Path) -> RequirementDataset:
    return RequirementDataset.model_validate_json(path.read_text(encoding="utf-8"))


def _value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if hasattr(value, "model_dump"):
        return _value(value.model_dump())
    if isinstance(value, dict):
        return {key: _value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_value(item) for item in value]
    return value


def extract_facts(spec: RequirementSpec) -> list[Fact]:
    facts = []
    for field in CORE_FIELDS:
        item = getattr(spec, field)
        if item is not None:
            facts.append(Fact(field=field, value=_value(item.normalized_value)))
    for field in ("maximum", "target", "tax_included"):
        item = getattr(spec.budget, field)
        if item is not None:
            facts.append(Fact(field=f"budget.{field}", value=_value(item.normalized_value)))
    for constraint in [*spec.hard_constraints, *spec.soft_preferences]:
        # Dedicated delivery_days is scored once. Its constraint representation is
        # removed by the current validator, so delivery preference strength is out
        # of scope for v1 (RAM/storage preference strength is explicitly scored).
        if constraint.field not in CORE_FIELDS:
            facts.append(
                Fact(
                    field=constraint.field,
                    value=_value(constraint.value),
                    operator=constraint.operator,
                    strength=constraint.strength.value,
                )
            )
    for field, resolution in spec.field_resolutions.items():
        if resolution.state.value == "NO_PREFERENCE":
            facts.append(Fact(field=field, operator="STATE", value="NO_PREFERENCE"))
    return facts


def evidence_errors(spec: RequirementSpec, messages: dict[str, str]) -> list[str]:
    errors: list[str] = []
    evidence = {item.evidence_id: item for item in spec.evidence}
    if len(evidence) != len(spec.evidence):
        errors.append("duplicate_evidence_id")
    for item in spec.evidence:
        if item.kind != "USER":
            errors.append(f"unexpected_external_evidence:{item.evidence_id}")
            continue
        text = messages.get(item.message_id)
        if (
            text is None
            or item.start is None
            or item.end is None
            or item.end > len(text)
            or item.end <= item.start
            or text[item.start : item.end].lower() != item.quote.lower()
        ):
            errors.append(f"invalid_quote:{item.evidence_id}")
    values = [getattr(spec, field) for field in (*CORE_FIELDS, "purpose")]
    values += [spec.budget.maximum, spec.budget.target, spec.budget.tax_included]
    values += [*spec.hard_constraints, *spec.soft_preferences]
    values += list(spec.field_resolutions.values())
    values += list(spec.default_authorizations)
    for item in values:
        if item is None:
            continue
        refs = item.evidence_ids
        if not refs and getattr(item, "state", None) != "UNRESOLVED":
            errors.append("value_without_evidence")
        if any(ref not in evidence for ref in refs):
            errors.append("unknown_evidence_ref")
    if spec.reference_bindings:
        errors.append("unauthorized_reference_binding")
    if spec.default_authorizations:
        errors.append("unrequested_default_authorization")
    return sorted(set(errors))


def changed_fields(spec: RequirementSpec) -> set[str]:
    fields: set[str] = set()
    constraints = {
        item.constraint_id: item.field for item in [*spec.hard_constraints, *spec.soft_preferences]
    }
    for operation in spec.delta.operations:
        path = operation.path.strip("/").replace("/", ".")
        parts = path.split(".")
        if parts[0] in {"hard_constraints", "soft_preferences"}:
            value = operation.new_value
            field = value.get("field") if isinstance(value, dict) else None
            field = field or (constraints.get(parts[1]) if len(parts) > 1 else None)
            fields.add(field or path)
        else:
            fields.add(path)
    return fields


def score_requirement(
    case: RequirementCase,
    spec: RequirementSpec | None,
    *,
    messages: dict[str, str],
    base: RequirementSpec | None = None,
    error: str | None = None,
    expected_authority: str | None = None,
) -> dict[str, Any]:
    expected = list(case.expected_facts)
    actual = extract_facts(spec) if spec is not None else []
    remaining_actual = list(actual)
    unmatched_expected: list[Fact] = []
    correct = 0
    for wanted in expected:
        match = next(
            (index for index, fact in enumerate(remaining_actual) if fact.key() == wanted.key()),
            None,
        )
        if match is None:
            unmatched_expected.append(wanted)
        else:
            remaining_actual.pop(match)
            correct += 1
    wrong = []
    omitted = []
    # Pair a wrong value/operator/strength with its field. A wrong fact is not
    # counted again as an omission; unmatched extra claims remain additions.
    for wanted in unmatched_expected:
        match = next(
            (index for index, fact in enumerate(remaining_actual) if fact.field == wanted.field),
            None,
        )
        if match is None:
            omitted.append(wanted.model_dump())
        else:
            wrong.append(
                {
                    "expected": wanted.model_dump(),
                    "actual": remaining_actual.pop(match).model_dump(),
                }
            )
    issues: list[str] = []
    evidence = evidence_errors(spec, messages) if spec is not None else []
    if spec is None:
        issues.append("no_parseable_requirement")
    else:
        if expected_authority is not None and spec.authority.value != expected_authority:
            issues.append("authority_mismatch")
        if {item.field for item in spec.missing_fields} != set(case.expected_missing):
            issues.append("missing_fields_mismatch")
        conflicts = {item.field for item in spec.conflicts if item.blocking}
        if conflicts != set(case.expected_conflicts):
            issues.append("conflicts_mismatch")
        expected_status = (
            "NEEDS_CLARIFICATION"
            if case.expected_missing or case.expected_conflicts
            else "READY_FOR_SOURCING"
        )
        if spec.status.value != expected_status:
            issues.append("status_mismatch")
        blockers = set(case.expected_missing) | set(case.expected_conflicts)
        if "category" in blockers:
            blockers = {"category"}
        covered = {
            field for question in spec.clarification_plan.questions for field in question.field_ids
        }
        if covered != blockers:
            issues.append("clarification_coverage_mismatch")
        if case.expected_changes is not None and changed_fields(spec) != set(case.expected_changes):
            issues.append("delta_fields_mismatch")
        if base is not None and (
            spec.spec_id != base.spec_id
            or spec.base_version != base.version
            or spec.version != base.version + 1
        ):
            issues.append("version_mismatch")
        if base is None and (spec.base_version != 0 or spec.version != 1):
            issues.append("version_mismatch")
        if spec.source_message_id != "msg_current":
            issues.append("source_message_mismatch")
    if evidence:
        issues.append("evidence_invalid")
    if error:
        issues.append(error)
    return {
        "passed": not (omitted or wrong or remaining_actual or issues),
        "expected_count": len(expected),
        "correct_count": correct,
        "omission_count": len(omitted),
        "wrong_count": len(wrong),
        "addition_count": len(remaining_actual),
        "omitted": omitted,
        "wrong": wrong,
        "additions": [item.model_dump() for item in remaining_actual],
        "issues": sorted(set(issues)),
        "evidence_errors": evidence,
    }


def summarize(results: list[dict], phase: str) -> dict:
    scores = [item[phase] for item in results]
    expected = sum(item["expected_count"] for item in scores)
    denominator = expected or 1
    issues = Counter(issue for item in scores for issue in item["issues"])
    return {
        "runs": len(scores),
        "passed": sum(item["passed"] for item in scores),
        "case_exact_match_rate": sum(item["passed"] for item in scores) / max(1, len(scores)),
        "expected_fact_count": expected,
        "fact_accuracy": sum(item["correct_count"] for item in scores) / denominator,
        "omission_rate": sum(item["omission_count"] for item in scores) / denominator,
        "wrong_extraction_rate": sum(item["wrong_count"] for item in scores) / denominator,
        "unsupported_addition_count": sum(item["addition_count"] for item in scores),
        "issue_counts": dict(sorted(issues.items())),
    }
