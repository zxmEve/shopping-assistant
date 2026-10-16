from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from procure_pilot.agents.tools import AgentCandidateProposal
from procure_pilot.domain.models import ValidatedCandidateSet
from procure_pilot.domain.references import ReferenceRequest
from procure_pilot.domain.requirements import RequirementSpec


class AgentKind(StrEnum):
    """The only two model roles in ProcurePilot."""

    REQUIREMENT = "REQUIREMENT"
    SOURCING = "SOURCING"


class AgentTask(BaseModel):
    task_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    agent: AgentKind
    objective: str = Field(min_length=1, max_length=500)
    max_model_requests: int = Field(default=4, gt=0, le=4)
    max_tool_calls: int = Field(default=21, ge=0, le=21)


class ContextMessage(BaseModel):
    role: str
    content: str


class ContextMemoryItem(BaseModel):
    memory_id: str
    scope: str
    key: str
    key_definition_version: int = Field(default=1, gt=0)
    category_id: str | None = None
    content: str
    value_json: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    source_ref: str
    version: int = Field(ge=0)
    confidence: float = Field(default=1.0, ge=0, le=1)
    relevance_score: float = Field(ge=0, le=1)


class AgentContextPayload(BaseModel):
    policy_version: str
    recent_messages: list[ContextMessage] = Field(default_factory=list)
    memories: list[ContextMemoryItem] = Field(default_factory=list)
    estimated_tokens: int = Field(default=0, ge=0)




class AgentTaskEnvelope(BaseModel):
    run_id: str
    state_version: int = Field(ge=0)
    task: AgentTask
    requirement: dict[str, Any]
    missing_fields: list[str] = Field(default_factory=list)
    context_summary: str = Field(default="", max_length=2000)
    context: AgentContextPayload | None = None
    requirement_spec: RequirementSpec | None = None
    source_message_id: str | None = None
    validated_candidates: ValidatedCandidateSet | None = None
    requirement_schema: dict[str, Any] | None = None
    reference_request: ReferenceRequest | None = None


class AgentResult(BaseModel):
    task_id: str
    agent: AgentKind
    state_version: int = Field(ge=0)
    status: Literal["SUCCEEDED", "DEGRADED", "FAILED"]
    output: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)
    candidate_proposals: list[AgentCandidateProposal] = Field(
        default_factory=list,
        max_length=3,
    )
    warnings: list[str] = Field(default_factory=list)
    requirement_spec: RequirementSpec | None = None


class TwoAgentOutcome(BaseModel):
    state_version: int = Field(ge=0)
    executed_agent: AgentKind
    result: AgentResult
    candidate_proposals: list[AgentCandidateProposal] = Field(
        default_factory=list,
        max_length=3,
    )
    stop_condition: Literal["wait_for_user", "wait_for_user_confirmation"]
    rationale: str
