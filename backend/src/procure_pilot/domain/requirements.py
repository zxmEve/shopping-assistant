from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class RequirementAuthority(StrEnum):
    PROPOSED = "PROPOSED"
    VALIDATED = "VALIDATED"


class RequirementStatus(StrEnum):
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    READY_FOR_SOURCING = "READY_FOR_SOURCING"


class ConstraintStrength(StrEnum):
    HARD = "HARD"
    SOFT = "SOFT"


class RequirementEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(default_factory=lambda: f"reqev_{uuid4().hex[:12]}")
    message_id: str
    quote: str = Field(min_length=1, max_length=500)
    start: int = Field(ge=0)
    end: int = Field(gt=0)


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
    text: str
    impact: float = Field(ge=0, le=1)
    resolves_conflict_id: str | None = None


class RequirementSpec(BaseModel):
    """Agent-proposed, server-validated requirement contract.

    The schema deliberately has no product, offer, provider, price quote, or catalog-ID fields.
    Unknown fields are rejected at the model boundary.
    """

    model_config = ConfigDict(extra="forbid")

    spec_id: str = Field(default_factory=lambda: f"reqspec_{uuid4().hex[:12]}")
    run_id: str
    schema_version: str = "1.0"
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
    default_authorizations: list[SourcedRequirementValue] = Field(default_factory=list)
    hard_constraints: list[RequirementConstraint] = Field(default_factory=list)
    soft_preferences: list[RequirementConstraint] = Field(default_factory=list)
    evidence: list[RequirementEvidence] = Field(default_factory=list)
    conflicts: list[RequirementConflict] = Field(default_factory=list)
    missing_fields: list[MissingRequirementField] = Field(default_factory=list)
    next_question: ClarificationQuestion | None = None
    delta: RequirementDelta
    status: RequirementStatus = RequirementStatus.NEEDS_CLARIFICATION
