"""Versioned message routing, dialogue intent and durable receipt contracts."""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RunWaitReason(StrEnum):
    MISSING_REQUIREMENT = "MISSING_REQUIREMENT"
    READY_FOR_SOURCING = "READY_FOR_SOURCING"
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    CONSTRAINT_NEGOTIATION = "CONSTRAINT_NEGOTIATION"
    FACTS_UNAVAILABLE = "FACTS_UNAVAILABLE"
    HISTORY_SELECTION = "HISTORY_SELECTION"
    REQUIREMENT_CONFIRMATION = "REQUIREMENT_CONFIRMATION"
    REFERENCE_UNAVAILABLE = "REFERENCE_UNAVAILABLE"
    REFERENCE_INFORMATION = "REFERENCE_INFORMATION"


class TurnIntent(StrEnum):
    PROVIDE_REQUIREMENT = "PROVIDE_REQUIREMENT"
    EXPLAIN = "EXPLAIN"
    COMPARE = "COMPARE"
    UPDATE_PREFERENCE = "UPDATE_PREFERENCE"
    UPDATE_REQUIREMENT = "UPDATE_REQUIREMENT"
    ACCEPT_NEGOTIATION = "ACCEPT_NEGOTIATION"
    SELECT_CANDIDATE = "SELECT_CANDIDATE"
    CLARIFY = "CLARIFY"


class TurnDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str
    intent: TurnIntent
    base_requirement_version: int = Field(ge=0)
    recommendation_version: int | None = Field(default=None, gt=0)
    referenced_option_id: str | None = None
    target_reference: str | None = None
    preference: Literal["BALANCED", "PRICE", "DELIVERY", "WARRANTY"] | None = None
    confidence: float = Field(default=1, ge=0, le=1)


class MessageRoute(StrEnum):
    CONTINUE = "CONTINUE"
    NEW = "NEW"
    CLARIFY = "CLARIFY"


class MessageRouteProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route: MessageRoute
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=300)


class MessageRouteResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    route: MessageRoute
    origin_run_id: str
    target_run_id: str | None = None
    reason: str
    source: Literal["model", "deterministic", "fallback", "user"]
    choices: list[Literal["CONTINUE", "NEW"]] = Field(default_factory=list)


class MessageRouteRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    message: str
    scenario: str | None = None
    message_id: str
    base_state_version: int = Field(ge=0)
    base_requirement_version: int = Field(gt=0)
    base_recommendation_version: int = Field(default=0, ge=0)
    expected_requirement_version: int | None = None
    expected_recommendation_version: int | None = None
    expected_resolution_draft_id: str | None = None
    expected_resolution_version: int | None = None
    base_resolution_draft_id: str | None = None
    base_resolution_version: int = 0
    proposal: MessageRouteProposal
    source: Literal["model", "deterministic", "fallback", "user"]
    status: Literal["CLARIFY", "DISPATCHING", "COMPLETED"]
    target_run_id: str | None = None
    assistant_message: str = ""


class DialogueProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: TurnIntent
    confidence: float = Field(ge=0, le=1)
    target_reference: str | None = None
    preference: Literal["BALANCED", "PRICE", "DELIVERY", "WARRANTY"] | None = None


class DialogueSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_refs: list[str] = Field(default_factory=list)
    option_ids: list[str] = Field(default_factory=list, max_length=3)
    rationale: str = Field(min_length=1, max_length=500)
