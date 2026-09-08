from __future__ import annotations

from collections import Counter

from pydantic import BaseModel, Field

from procure_pilot.domain.models import ProcurementRun, TraceEvent

ALLOWED_AGENT_TOOL_EVENTS = frozenset(
    {
        "inspect_requirement",
        "search_catalog",
    }
)
VALID_STOP_CONDITIONS = frozenset(
    {
        "wait_for_user",
        "wait_for_user_confirmation",
        "stop_failed",
    }
)


class TrajectoryIssue(BaseModel):
    code: str
    message: str


class AgentTrajectoryGrade(BaseModel):
    passed: bool
    tool_calls: list[str] = Field(default_factory=list)
    duplicate_calls: list[str] = Field(default_factory=list)
    issues: list[TrajectoryIssue] = Field(default_factory=list)


def _call_fingerprint(event: TraceEvent) -> str:
    provider = event.detail.get("provider", "")
    offer_id = event.detail.get("offer_id", "")
    return ":".join(str(part) for part in (event.name, provider, offer_id))


def grade_agent_trajectory(run: ProcurementRun) -> AgentTrajectoryGrade:
    """Grade only model-owned activity before the runtime applies each plan."""
    issues: list[TrajectoryIssue] = []
    tool_calls: list[str] = []
    duplicate_calls: list[str] = []
    window_start = 0
    plans = [
        (index, event)
        for index, event in enumerate(run.trace)
        if event.name == "runtime.plan_applied"
    ]
    if not plans:
        issues.append(
            TrajectoryIssue(code="missing_plan", message="No runtime Agent plan was recorded.")
        )

    for plan_index, plan_event in plans:
        window = run.trace[window_start:plan_index]
        calls = [event for event in window if event.kind == "TOOL"]
        tool_calls.extend(event.name for event in calls)

        forbidden = sorted({event.name for event in calls} - ALLOWED_AGENT_TOOL_EVENTS)
        if forbidden:
            issues.append(
                TrajectoryIssue(
                    code="forbidden_tool",
                    message=f"Agent trajectory used tools outside its read-only surface: {forbidden}",
                )
            )

        fingerprints = [_call_fingerprint(event) for event in calls]
        fingerprints.extend(
            str(event.detail["fingerprint"])
            for event in window
            if event.name == "agent.tool_call_rejected" and "fingerprint" in event.detail
        )
        duplicates = sorted(
            fingerprint for fingerprint, count in Counter(fingerprints).items() if count > 1
        )
        duplicate_calls.extend(duplicates)
        if duplicates:
            issues.append(
                TrajectoryIssue(
                    code="duplicate_tool_call",
                    message=f"Agent repeated identical tool calls: {duplicates}",
                )
            )

        plan = plan_event.detail
        proposals = plan.get("candidate_proposals", [])
        if proposals:
            names = {event.name for event in calls}
            missing = sorted({"search_catalog"} - names)
            if missing:
                issues.append(
                    TrajectoryIssue(
                        code="missing_required_tool",
                        message=f"Candidate proposal omitted required tools: {missing}",
                    )
                )
            stop_condition = plan.get("stop_condition", "")
            if stop_condition != "wait_for_user_confirmation":
                issues.append(
                    TrajectoryIssue(
                        code="invalid_stop_condition",
                        message=(
                            "A recommendation must stop and wait for explicit user confirmation."
                        ),
                    )
                )
            proposal_providers = {item.get("provider") for item in proposals}
            searched_providers = {
                event.detail.get("provider") for event in calls if event.name == "search_catalog"
            }
            has_evidence = all(item.get("evidence_refs") for item in proposals)
            if not proposal_providers.issubset(searched_providers) or not has_evidence:
                issues.append(
                    TrajectoryIssue(
                        code="ungrounded_candidate",
                        message="Every proposal must come from a searched provider and carry catalog evidence.",
                    )
                )
        elif plan.get("stop_condition") not in ("", *VALID_STOP_CONDITIONS):
            issues.append(
                TrajectoryIssue(
                    code="unknown_stop_condition",
                    message=f"Unknown stop condition: {plan.get('stop_condition')}",
                )
            )
        window_start = plan_index + 1

    return AgentTrajectoryGrade(
        passed=not issues,
        tool_calls=tool_calls,
        duplicate_calls=duplicate_calls,
        issues=issues,
    )
