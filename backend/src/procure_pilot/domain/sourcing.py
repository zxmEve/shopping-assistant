"""Versioned sourcing facts, candidate assessments and negotiation contracts."""

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FactStatus(StrEnum):
    KNOWN = "KNOWN"
    UNKNOWN = "UNKNOWN"
    UNAVAILABLE = "UNAVAILABLE"


class OfferFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact_id: str
    provider: str
    offer_id: str
    kind: Literal["INVENTORY", "DELIVERY", "WARRANTY"]
    requirement_version: int = Field(gt=0)
    scope: dict[str, Any]
    status: FactStatus
    value: dict[str, Any] | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    source_ref: str
    fetched_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def validate_fact(self):
        if self.fetched_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("Fact timestamps require a timezone")
        if self.expires_at <= self.fetched_at:
            raise ValueError("Fact expiry must follow fetch time")
        if self.status == FactStatus.KNOWN and (self.value is None or not self.evidence_refs):
            raise ValueError("Known facts require a value and evidence")
        if self.status != FactStatus.KNOWN and self.value is not None:
            raise ValueError("Unknown or unavailable facts cannot assert a value")
        return self


class ConstraintCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    constraint_id: str
    field: str
    authority: Literal["USER", "POLICY"]
    result: Literal["PASS", "FAIL", "UNKNOWN"]
    expected: Any
    actual: Any = None
    evidence_refs: list[str] = Field(default_factory=list)
    adjustable: bool = False

    @model_validator(mode="after")
    def protect_policy(self):
        if self.authority == "POLICY" and self.adjustable:
            raise ValueError("Policy constraints are not negotiable")
        return self


class CandidateAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    offer_id: str
    requirement_version: int = Field(gt=0)
    checks: list[ConstraintCheck]


class NegotiationAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: Literal["budget.maximum", "delivery_days", "minimum_ram_gb", "minimum_storage_gb"]
    previous: Any
    proposed: Any


class NegotiationOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    option_id: str
    base_requirement_version: int = Field(gt=0)
    adjustments: list[NegotiationAdjustment] = Field(min_length=1, max_length=4)
    validated_candidate_refs: list[str] = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)
    expires_at: datetime
