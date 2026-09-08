from __future__ import annotations

import pytest

from procure_pilot.agents.runtime import AgentPlan
from procure_pilot.agents.tools import AgentRuntimeContext, build_function_tools
from procure_pilot.domain.models import Message
from procure_pilot.evals.runner import COMPLETE_REQUEST, _EvidenceBackedPlanner, make_engine
from procure_pilot.evals.trajectory import grade_agent_trajectory


@pytest.mark.asyncio
async def test_evidence_backed_trajectory_passes():
    engine = make_engine()
    engine.planner = _EvidenceBackedPlanner()

    response = await engine.start(
        tenant_id="demo_cn",
        user_id="employee_001",
        message=COMPLETE_REQUEST,
    )

    grade = grade_agent_trajectory(response.run)
    assert grade.passed is True
    assert grade.tool_calls == ["search_catalog"]


class MissingToolsAndStopPlanner:
    async def plan(
        self,
        messages: list[Message],
        *,
        context: AgentRuntimeContext | None = None,
    ) -> AgentPlan:
        return AgentPlan.model_validate(
            {
                "next_action": "recommend",
                "specialist": "Procurement Orchestrator",
                "rationale": "unsupported",
                "candidate_proposals": [
                    {
                        "provider": "internal_catalog",
                        "offer_id": "invented",
                        "evidence_refs": ["invented"],
                    }
                ],
                "stop_condition": "continue_without_confirmation",
            }
        )


@pytest.mark.asyncio
async def test_trajectory_grader_rejects_missing_tools_and_bad_stop():
    engine = make_engine()
    engine.planner = MissingToolsAndStopPlanner()

    response = await engine.start(
        tenant_id="demo_cn",
        user_id="employee_001",
        message=COMPLETE_REQUEST,
    )

    grade = grade_agent_trajectory(response.run)
    assert grade.passed is False
    assert {issue.code for issue in grade.issues} >= {
        "missing_required_tool",
        "invalid_stop_condition",
        "ungrounded_candidate",
    }


class DuplicateSearchPlanner:
    def __init__(self) -> None:
        self.search = build_function_tools()["search_catalog"]

    async def plan(
        self,
        messages: list[Message],
        *,
        context: AgentRuntimeContext | None = None,
    ) -> AgentPlan:
        from procure_pilot.evals.runner import _invoke_agent_tool

        assert context is not None
        arguments = '{"provider":"internal_catalog"}'
        await _invoke_agent_tool(self.search, context, arguments)
        await _invoke_agent_tool(self.search, context, arguments)
        return AgentPlan(
            next_action="source_and_review",
            specialist="Sourcing & Comparison Agent",
            rationale="duplicate",
        )


@pytest.mark.asyncio
async def test_trajectory_grader_rejects_identical_duplicate_calls():
    engine = make_engine()
    engine.planner = DuplicateSearchPlanner()

    response = await engine.start(
        tenant_id="demo_cn",
        user_id="employee_001",
        message=COMPLETE_REQUEST,
    )

    grade = grade_agent_trajectory(response.run)
    assert grade.passed is False
    assert "search_catalog:internal_catalog:" in grade.duplicate_calls
