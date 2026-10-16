"""Read-only requirement references and separately confirmed requirement drafts."""

from datetime import datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from procure_pilot.domain.knowledge import KnowledgeCitation
from procure_pilot.domain.requirements import RequirementSpec

ReferenceField = Literal[
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
    "requested_model_text",
]


class ReferenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy: bool = False
    history: bool = False
    query_only: bool = False
    authorization_message_id: str
    authorization_quote: str
    selected_order_id: str | None = None
    latest_order: bool = False
    history_year: int | None = Field(default=None, ge=2000, le=2100)


class ReferenceValue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_id: str
    field: ReferenceField
    value: str | int | bool
    operator: Literal["EQ", "GTE", "LTE", "PREFER"] = "EQ"
    authority: Literal["MANDATORY", "REFERENCE"]
    source_kind: Literal["POLICY", "HISTORY"]
    source_ref: str
    quote: str
    source_version: str


class HistoricalPurchaseOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str
    tenant_id: str
    owner_user_id: str
    department: str
    category: str
    purchased_at: datetime
    status: Literal["APPROVED", "FULFILLED", "CANCELLED", "DRAFT_NOT_SUBMITTED"]
    model_text: str
    fields: list[ReferenceValue]
    simulated: bool = True


class ReferenceLookupResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["POLICY", "HISTORY"]
    status: Literal["KNOWN", "UNAVAILABLE"]
    values: list[ReferenceValue] = Field(default_factory=list)
    orders: list[HistoricalPurchaseOrder] = Field(default_factory=list)
    source_version: str
    effective_from: datetime | None = None
    citations: list[KnowledgeCitation] = Field(default_factory=list)
    source_hash: str
    fetched_at: datetime
    detail: str


class ResolvedReferenceField(BaseModel):
    field: str
    value: str | int | bool
    source: Literal["USER", "POLICY", "HISTORY"]
    source_ref: str
    reason: str


class RequirementResolutionDraft(BaseModel):
    draft_id: str = Field(default_factory=lambda: f"reqdraft_{uuid4().hex[:12]}")
    version: int = Field(gt=0)
    base_requirement_version: int
    source_message_id: str
    status: Literal["HISTORY_SELECTION", "NEEDS_INPUT", "BLOCKED", "READY", "CONFIRMED"]
    proposed_spec: RequirementSpec
    fields: list[ResolvedReferenceField]
    changes: list[str]
    missing_fields: list[str]
    blockers: list[str]
    policy_snapshot: ReferenceLookupResult | None = None
    history_snapshot: ReferenceLookupResult | None = None
    policy_hash: str | None
    history_hash: str | None
    selected_order_id: str | None
    content_hash: str = ""


class RequirementConfirmation(BaseModel):
    request_id: str
    draft_id: str
    draft_version: int
    content_hash: str
    message_id: str
    status: Literal["DISPATCHING", "COMPLETED", "INVALIDATED"] = "DISPATCHING"
