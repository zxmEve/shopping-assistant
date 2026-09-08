from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field

from procure_pilot.agents.tools import AgentCandidateProposal, AgentRuntimeContext
from procure_pilot.domain.models import Message


class AgentPlan(BaseModel):
    next_action: str
    specialist: str
    rationale: str
    required_evidence: list[str] = Field(default_factory=list)
    candidate_proposals: list[AgentCandidateProposal] = Field(default_factory=list, max_length=3)
    stop_condition: str = ""


class AgentPlanner(Protocol):
    async def plan(
        self,
        messages: list[Message],
        *,
        context: AgentRuntimeContext | None = None,
    ) -> AgentPlan: ...


class DeterministicAgentPlanner:
    """Safe fallback that leaves all decisions to deterministic services."""

    async def plan(
        self,
        messages: list[Message],
        *,
        context: AgentRuntimeContext | None = None,
    ) -> AgentPlan:
        return AgentPlan(
            next_action="clarify_then_source",
            specialist="Application Runtime",
            rationale="模型不可用，继续使用固定规则完成需求检查、比价和政策校验",
            required_evidence=["requirement", "catalog", "budget", "policy", "vendor"],
        )


def topology_description(
    mode: str,
    model: str,
    provider: str = "openai",
    topology: str = "two_agent",
    *,
    models: dict[str, str] | None = None,
) -> dict[str, Any]:
    resolved_models = models or {"requirement": model, "sourcing": model}
    return {
        "mode": mode,
        "topology": "two_agent",
        "requested_topology": topology,
        "model": model if mode == "openai" else "deterministic-two-agent-fixture",
        "model_provider": provider if mode == "openai" else "fixture",
        "model_assignments": resolved_models if mode == "openai" else {},
        "manager": "Application Runtime (deterministic code, not an Agent)",
        "agents": ["Requirement Agent", "Sourcing Agent"],
        "specialists": ["Requirement Agent", "Sourcing Agent"],
        "pattern": "deterministic-runtime-with-two-agents",
        "function_tools": [
            "inspect_procurement_requirement",
            "search_catalog",
        ],
        "deterministic_services": [
            "requirement_validation",
            "catalog_normalization_and_matching",
            "landed_cost_calculation",
            "budget_and_policy_evaluation",
            "evidence_validation",
            "state_and_checkpoint_management",
            "human_confirmation_and_idempotent_write",
        ],
        "conversation_context": "agent-specific-typed-projections",
        "business_writes": "server-side HITL only",
        "rollback_topology": "deterministic",
    }
