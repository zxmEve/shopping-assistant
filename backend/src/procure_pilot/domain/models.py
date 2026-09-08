from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import (
    BaseModel,
    Field,
    PrivateAttr,
    computed_field,
    field_serializer,
    field_validator,
    model_validator,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


class RunState(StrEnum):
    COLLECTING = "COLLECTING"
    WAITING_USER = "WAITING_USER"
    SOURCING = "SOURCING"
    REVIEWING = "REVIEWING"
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    REVALIDATING = "REVALIDATING"
    DRAFT_CREATING = "DRAFT_CREATING"
    DRAFT_CREATED = "DRAFT_CREATED"
    FAILED = "FAILED"


class BudgetResource(StrEnum):
    STATE_TRANSITIONS = "state_transitions"
    AGENT_TURNS = "agent_turns"
    ORCHESTRATOR_REQUESTS = "orchestrator_requests"
    SPECIALIST_REQUESTS = "specialist_requests"
    STAGE_MODEL_REQUESTS = "stage_model_requests"
    TOOL_CALLS = "tool_calls"
    AGENT_READ_TOOL_CALLS = "agent_read_tool_calls"
    WORKFLOW_READ_TOOL_CALLS = "workflow_read_tool_calls"
    COMMIT_TOOL_CALLS = "commit_tool_calls"
    PROVIDER_CALLS = "provider_calls"
    RETRIES = "retries"
    INPUT_TOKENS = "input_tokens"
    OUTPUT_TOKENS = "output_tokens"
    COST_MICRO_USD = "cost_micro_usd"
    WALL_TIME_MS = "wall_time_ms"


class ToolBudgetPool(StrEnum):
    AGENT_READ = "agent_read"
    WORKFLOW_READ = "workflow_read"
    COMMIT = "commit"


class RiskLevel(StrEnum):
    READ = "READ"
    BUSINESS_WRITE = "BUSINESS_WRITE"


class PolicyDecision(StrEnum):
    ALLOW = "ALLOW"
    ALLOW_WITH_APPROVAL = "ALLOW_WITH_APPROVAL"
    NEEDS_INFO = "NEEDS_INFO"
    DENY = "DENY"


class MatchDecisionType(StrEnum):
    MATCH = "MATCH"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    REJECTED = "REJECTED"


class ProductCondition(StrEnum):
    NEW = "NEW"
    REFURBISHED = "REFURBISHED"
    USED = "USED"


class BundleType(StrEnum):
    DEVICE_ONLY = "DEVICE_ONLY"
    DOCK_BUNDLE = "DOCK_BUNDLE"


class Money(BaseModel):
    amount: Decimal
    currency: str = "CNY"

    @field_validator("amount")
    @classmethod
    def quantize_amount(cls, value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @field_serializer("amount")
    def serialize_amount(self, value: Decimal) -> str:
        return str(value)

    def multiply(self, quantity: int) -> Money:
        return Money(amount=self.amount * quantity, currency=self.currency)


class ProcurementRequirement(BaseModel):
    category: str = "laptop"
    quantity: int | None = Field(default=None, gt=0, le=1000)
    unit_budget: Money | None = None
    budget_tax_included: bool | None = None
    delivery_city: str | None = None
    delivery_days: int | None = Field(default=None, gt=0, le=365)
    minimum_ram_gb: int | None = Field(default=None, ge=4, le=512)
    minimum_storage_gb: int | None = Field(default=None, ge=128, le=16384)
    operating_system: str | None = None
    condition: ProductCondition = ProductCondition.NEW
    assumptions: list[str] = Field(default_factory=list)
    version: int = 1


class ClarificationDecision(BaseModel):
    missing_fields: list[str]
    questions: list[str]
    ready_for_sourcing: bool
    can_search_in_parallel: bool = True
    rationale: str = ""


class Product(BaseModel):
    product_id: str
    brand: str
    model: str
    category: str = "laptop"


class Variant(BaseModel):
    variant_id: str
    product_id: str
    cpu: str
    ram_gb: int
    storage_gb: int
    operating_system: str
    warranty_months: int
    condition: ProductCondition = ProductCondition.NEW
    bundle: BundleType = BundleType.DEVICE_ONLY

    @field_validator("bundle", mode="before")
    @classmethod
    def migrate_legacy_bundle(cls, value: Any) -> Any:
        return BundleType.DEVICE_ONLY if value is None else value


class Evidence(BaseModel):
    evidence_id: str = Field(default_factory=lambda: f"ev_{uuid4().hex[:12]}")
    source: str
    source_ref: str
    fetched_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime
    content_hash: str
    fields: dict[str, Any]


class ProviderRawOffer(BaseModel):
    """Provider-owned payload. Raw text is evidence, never a trusted canonical identity."""

    raw_offer_id: str
    provider: str
    seller: str
    vendor_id: str
    title: str
    brand: str | None = None
    model: str | None = None
    cpu: str | None = None
    ram: str | None = None
    storage: str | None = None
    operating_system: str | None = None
    warranty: str | None = None
    condition: str | None = None
    bundle: str | None = None
    unit_price: Money
    tax_rate: Decimal = Decimal("0.13")
    shipping: Money = Field(default_factory=lambda: Money(amount=Decimal("0")))
    conditional_discount: Money = Field(default_factory=lambda: Money(amount=Decimal("0")))
    discount_condition: str | None = None
    stock: int
    delivery_days: int
    evidence: Evidence


class MatchDecision(BaseModel):
    canonical_product_id: str | None = None
    canonical_variant_id: str | None = None
    confidence: float = Field(ge=0, le=1)
    matched_fields: list[str] = Field(default_factory=list)
    conflict_fields: list[str] = Field(default_factory=list)
    decision: MatchDecisionType
    reasons: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)


class Offer(BaseModel):
    offer_id: str
    provider: str
    seller: str
    vendor_id: str
    product: Product
    variant: Variant
    title: str
    unit_price: Money
    tax_rate: Decimal = Decimal("0.13")
    shipping: Money = Field(default_factory=lambda: Money(amount=Decimal("0")))
    conditional_discount: Money = Field(default_factory=lambda: Money(amount=Decimal("0")))
    discount_condition: str | None = None
    stock: int
    delivery_days: int
    evidence: Evidence
    match_decision: MatchDecision = Field(
        default_factory=lambda: MatchDecision(
            confidence=0,
            decision=MatchDecisionType.REVIEW_REQUIRED,
            reasons=["旧版已保存报价缺少归一决策，请重新寻源"],
        )
    )
    stale: bool = False

    @computed_field
    @property
    def match_confidence(self) -> float:
        return self.match_decision.confidence


class OfferMatchAssessment(BaseModel):
    raw_offer_id: str
    provider: str
    title: str
    condition: ProductCondition | None = None
    bundle: BundleType | None = None
    comparable: bool
    match_decision: MatchDecision


class CatalogSearchResult(BaseModel):
    provider: str
    retrieved_count: int
    offers: list[Offer]
    assessments: list[OfferMatchAssessment] = Field(default_factory=list)


class CostBreakdown(BaseModel):
    subtotal: Money
    tax: Money
    shipping: Money
    unconditional_total: Money
    conditional_total: Money | None = None


class Candidate(BaseModel):
    offer: Offer
    cost: CostBreakdown
    requirement_fit: float
    policy_decision: PolicyDecision
    approval_route: list[str]
    policy_reasons: list[str]
    evidence_refs: list[str]
    warnings: list[str] = Field(default_factory=list)
    score: float = 0


class Recommendation(BaseModel):
    lowest_cost: Candidate
    fastest_delivery: Candidate
    best_overall: Candidate
    compared_offer_count: int
    rejected_offer_count: int
    match_rejected_count: int = 0
    review_required_count: int = 0
    normalization_issues: list[OfferMatchAssessment] = Field(default_factory=list)
    selection_source: str = "deterministic_score"
    selection_rationale: str = ""
    summary: str


class QuoteSnapshot(BaseModel):
    offer_id: str
    provider: str
    source_ref: str
    content_hash: str
    unit_price: Money
    shipping: Money
    tax_rate: Decimal
    stock: int
    delivery_days: int
    fetched_at: datetime
    expires_at: datetime
    fingerprint: str


class RevalidationChange(BaseModel):
    field: str
    previous: str
    current: str
    requires_confirmation: bool = True


class TraceEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: f"tr_{uuid4().hex[:12]}")
    timestamp: datetime = Field(default_factory=utc_now)
    actor: str
    kind: str
    name: str
    status: str = "OK"
    detail: dict[str, Any] = Field(default_factory=dict)
    duration_ms: int | None = None


class StateTransitionEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: f"st_{uuid4().hex[:12]}")
    timestamp: datetime = Field(default_factory=utc_now)
    version: int = Field(gt=0)
    from_state: RunState
    to_state: RunState
    reason: str
    actor: str


class ExecutionBudget(BaseModel):
    max_state_transitions: int = Field(default=20, gt=0)
    max_agent_turns: int = Field(default=12, gt=0)
    max_orchestrator_requests: int = Field(default=2, gt=0)
    max_specialist_requests: int = Field(default=8, gt=0)
    max_stage_model_requests: int = Field(default=10, gt=0)
    max_tool_calls: int = Field(default=21, gt=0)
    max_agent_read_tool_calls: int = Field(default=12, gt=0)
    max_workflow_read_tool_calls: int = Field(default=6, gt=0)
    max_commit_tool_calls: int = Field(default=3, gt=0)
    max_provider_calls: int = Field(default=12, gt=0)
    max_retries: int = Field(default=4, ge=0)
    max_input_tokens: int = Field(default=30_000, gt=0)
    max_output_tokens: int = Field(default=10_000, gt=0)
    max_cost_micro_usd: int = Field(default=100_000, gt=0)
    max_wall_time_ms: int = Field(default=25_000, gt=0)
    max_parallel_provider_calls: int = Field(default=4, gt=0)


class ExecutionUsage(BaseModel):
    state_transitions: int = Field(default=0, ge=0)
    agent_turns: int = Field(default=0, ge=0)
    orchestrator_requests: int = Field(default=0, ge=0)
    specialist_requests: int = Field(default=0, ge=0)
    stage_model_requests: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    agent_read_tool_calls: int = Field(default=0, ge=0)
    workflow_read_tool_calls: int = Field(default=0, ge=0)
    commit_tool_calls: int = Field(default=0, ge=0)
    reserved_commit_tool_calls: int = Field(default=0, ge=0)
    provider_calls: int = Field(default=0, ge=0)
    retries: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_micro_usd: int = Field(default=0, ge=0)
    wall_time_ms: int = Field(default=0, ge=0)


class Message(BaseModel):
    role: str
    content: str
    created_at: datetime = Field(default_factory=utc_now)


class ProcurementDraft(BaseModel):
    draft_id: str
    run_id: str
    idempotency_key: str
    status: str = "DRAFT_NOT_SUBMITTED"
    candidate: Candidate
    approval_route: list[str]
    created_at: datetime = Field(default_factory=utc_now)


class ProcurementRun(BaseModel):
    _persistence_version: int | None = PrivateAttr(default=None)

    run_id: str = Field(default_factory=lambda: f"run_{uuid4().hex[:12]}")
    tenant_id: str
    user_id: str
    state: RunState = RunState.COLLECTING
    state_version: int = Field(default=0, ge=0)
    state_events: list[StateTransitionEvent] = Field(default_factory=list)
    execution_budget: ExecutionBudget = Field(default_factory=ExecutionBudget)
    execution_usage: ExecutionUsage = Field(default_factory=ExecutionUsage)
    scenario: str | None = None
    clarification_rounds: int = Field(default=0, ge=0, le=3)
    requirement: ProcurementRequirement = Field(default_factory=ProcurementRequirement)
    messages: list[Message] = Field(default_factory=list)
    trace: list[TraceEvent] = Field(default_factory=list)
    recommendation: Recommendation | None = None
    quote_snapshot: QuoteSnapshot | None = None
    revalidation_changes: list[RevalidationChange] = Field(default_factory=list)
    action_token: str | None = None
    draft: ProcurementDraft | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_state_history(self) -> ProcurementRun:
        if not self.state_events:
            if self.state_version != 0:
                raise ValueError("state_version requires state_events")
            return self
        expected_versions = list(range(1, len(self.state_events) + 1))
        if [event.version for event in self.state_events] != expected_versions:
            raise ValueError("state event versions must be contiguous")
        for previous, current in zip(self.state_events, self.state_events[1:], strict=False):
            if previous.to_state != current.from_state:
                raise ValueError("state event history must be contiguous")
        if self.state_version != self.state_events[-1].version:
            raise ValueError("state_version does not match state event history")
        if self.state != self.state_events[-1].to_state:
            raise ValueError("current state does not match state event history")
        return self


class RunResponse(BaseModel):
    run: ProcurementRun
    assistant_message: str
    suggested_replies: list[str] = Field(default_factory=list)
