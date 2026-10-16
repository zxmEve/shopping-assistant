from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RequirementAuthority(StrEnum):
    PROPOSED = "PROPOSED"
    VALIDATED = "VALIDATED"


class RequirementStatus(StrEnum):
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    READY_FOR_SOURCING = "READY_FOR_SOURCING"


class ConstraintStrength(StrEnum):
    HARD = "HARD"
    SOFT = "SOFT"


class FieldResolutionState(StrEnum):
    UNRESOLVED = "UNRESOLVED"
    EXPLICIT = "EXPLICIT"
    NO_PREFERENCE = "NO_PREFERENCE"
    AUTHORIZED_DEFAULT = "AUTHORIZED_DEFAULT"
    PROPOSED_REFERENCE = "PROPOSED_REFERENCE"
    CONFIRMED_REFERENCE = "CONFIRMED_REFERENCE"


class FieldResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: FieldResolutionState
    evidence_ids: list[str] = Field(default_factory=list)
    source_ref: str | None = None

    @model_validator(mode="after")
    def require_resolution_evidence(self):
        if self.state != FieldResolutionState.UNRESOLVED and not self.evidence_ids:
            raise ValueError("Resolved fields require user evidence")
        return self


class FieldResolutions(BaseModel):
    """Closed field registry, with mapping access for domain consumers.

    A free-form dict would introduce additionalProperties into native strict model output.
    Aliases preserve the dotted field paths used by existing requirement contracts.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True, serialize_by_alias=True)

    category: FieldResolution | None = None
    quantity: FieldResolution | None = None
    budget_maximum: FieldResolution | None = Field(default=None, alias="budget.maximum")
    budget_tax_included: FieldResolution | None = Field(default=None, alias="budget.tax_included")
    delivery_city: FieldResolution | None = None
    minimum_ram_gb: FieldResolution | None = None
    minimum_storage_gb: FieldResolution | None = None

    @classmethod
    def _attribute(cls, key: str) -> str:
        for name, field in cls.model_fields.items():
            if key == (field.alias or name):
                return name
        raise KeyError(key)

    def get(self, key: str, default=None):
        try:
            value = getattr(self, self._attribute(key))
        except KeyError:
            return default
        return default if value is None else value

    def __getitem__(self, key: str) -> FieldResolution:
        value = self.get(key)
        if value is None:
            raise KeyError(key)
        return value

    def __setitem__(self, key: str, value: FieldResolution) -> None:
        setattr(self, self._attribute(key), value)

    def __contains__(self, key: str) -> bool:
        return self.get(key) is not None

    def items(self):
        return [
            (field.alias or name, getattr(self, name))
            for name, field in type(self).model_fields.items()
            if getattr(self, name) is not None
        ]

    def values(self):
        return [value for _, value in self.items()]


class RequirementEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(default_factory=lambda: f"reqev_{uuid4().hex[:12]}")
    message_id: str
    quote: str = Field(min_length=1, max_length=500)
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, gt=0)
    kind: Literal["USER", "POLICY", "HISTORY"] = "USER"
    source_ref: str | None = None


class SourcedRequirementValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Any
    normalized_value: Any
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str] = Field(min_length=1)


class NormalizedMoneyValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Decimal = Field(gt=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class SourcedMoneyValue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str
    normalized_value: NormalizedMoneyValue
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str] = Field(min_length=1)


class BudgetRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    maximum: SourcedMoneyValue | None = None
    target: SourcedMoneyValue | None = None
    tax_included: SourcedRequirementValue | None = None


class RequirementConstraint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    constraint_id: str = Field(default_factory=lambda: f"con_{uuid4().hex[:12]}")
    field: str
    operator: Literal["EQ", "GTE", "LTE", "IN", "PREFER"]
    value: Any
    strength: ConstraintStrength
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str] = Field(min_length=1)


class RequirementConflict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conflict_id: str = Field(default_factory=lambda: f"conf_{uuid4().hex[:12]}")
    field: str
    constraint_ids: list[str] = Field(min_length=2)
    reason: str
    blocking: bool = True


class RequirementDeltaOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["ADD", "REPLACE", "REMOVE"]
    path: str
    old_value: Any = None
    new_value: Any = None
    evidence_ids: list[str] = Field(default_factory=list)


class RequirementDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_version: int = Field(ge=0)
    to_version: int = Field(gt=0)
    operations: list[RequirementDeltaOperation] = Field(default_factory=list)


class MissingRequirementField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    reason: str
    impact: float = Field(ge=0, le=1)


class ClarificationQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_id: str = Field(default_factory=lambda: f"q_{uuid4().hex[:12]}")
    field: str
    field_ids: list[str] = Field(default_factory=list)
    text: str = Field(min_length=1, max_length=500)
    impact: float = Field(ge=0, le=1)
    resolves_conflict_id: str | None = None

    @model_validator(mode="after")
    def migrate_single_field(self):
        if not self.field_ids:
            self.field_ids = [self.field]
        return self


class ClarificationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    questions: list[ClarificationQuestion] = Field(default_factory=list, max_length=12)
    covered_fields: list[str] = Field(default_factory=list)
    repaired_question_count: int = Field(default=0, ge=0)


class RequirementReferenceBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str
    evidence_id: str


class RequirementSpec(BaseModel):
    """Agent-proposed, server-validated requirement contract.

    The schema deliberately has no product, offer, provider, price quote, or catalog-ID fields.
    Unknown fields are rejected at the model boundary.
    """

    model_config = ConfigDict(extra="forbid")

    spec_id: str = Field(default_factory=lambda: f"reqspec_{uuid4().hex[:12]}")
    run_id: str
    schema_version: str = "2.0"
    authority: RequirementAuthority = RequirementAuthority.PROPOSED
    base_version: int = Field(ge=0)
    version: int = Field(gt=0)
    source_message_id: str
    category: SourcedRequirementValue | None = None
    purpose: SourcedRequirementValue | None = None
    quantity: SourcedRequirementValue | None = None
    budget: BudgetRequirement = Field(default_factory=BudgetRequirement)
    delivery_city: SourcedRequirementValue | None = None
    delivery_days: SourcedRequirementValue | None = None
    reference_bindings: list[RequirementReferenceBinding] = Field(default_factory=list, max_length=20)
    default_authorizations: list[SourcedRequirementValue] = Field(default_factory=list)
    hard_constraints: list[RequirementConstraint] = Field(default_factory=list)
    soft_preferences: list[RequirementConstraint] = Field(default_factory=list)
    evidence: list[RequirementEvidence] = Field(default_factory=list)
    conflicts: list[RequirementConflict] = Field(default_factory=list)
    missing_fields: list[MissingRequirementField] = Field(default_factory=list)
    field_resolutions: FieldResolutions = Field(default_factory=FieldResolutions)
    clarification_plan: ClarificationPlan = Field(default_factory=ClarificationPlan)
    next_question: ClarificationQuestion | None = None
    delta: RequirementDelta
    status: RequirementStatus = RequirementStatus.NEEDS_CLARIFICATION

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_question(cls, value):
        if isinstance(value, dict) and "clarification_plan" not in value:
            value = dict(value)
            question = value.get("next_question")
            value["clarification_plan"] = {
                "questions": [question] if question else [],
                "covered_fields": [question["field"]] if isinstance(question, dict) else [],
            }
        return value
