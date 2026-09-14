from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from procure_pilot.agents.contracts import AgentKind, AgentTask, AgentTaskEnvelope
from procure_pilot.agents.multi_agent import AGENT_TOOL_ALLOWLIST
from procure_pilot.agents.provider_adapter import (
    OpenAICompatibleTwoAgentAdapter,
    ProviderAgentResult,
    ProviderRequirementResult,
    ProviderSourcingResult,
)
from procure_pilot.agents.tools import AgentCandidateProposal, AgentRuntimeContext
from procure_pilot.evals.runner import make_engine
from procure_pilot.services.requirement_validation import RequirementSpecService


def make_adapter(**overrides):
    return OpenAICompatibleTwoAgentAdapter(
        api_key="test-key-not-used",
        provider="deepseek",
        requirement_model="deepseek-requirement",
        sourcing_model="deepseek-sourcing",
        **overrides,
    )


def test_provider_adapter_builds_exactly_two_independent_agents_with_phase_tools():
    adapter = make_adapter()

    assert len(adapter.agents) == 2
    assert len({id(agent) for agent in adapter.agents.values()}) == 2
    assert len({id(model) for model in adapter._models.values()}) == 2
    assert adapter.transport == "chat_completions"
    assert adapter.model_assignments[AgentKind.REQUIREMENT] == "deepseek-requirement"
    assert adapter.model_assignments[AgentKind.SOURCING] == "deepseek-sourcing"
    for kind, expected in AGENT_TOOL_ALLOWLIST.items():
        agent = adapter.agents[kind]
        assert agent.output_type is None
        assert tuple(tool.name for tool in agent.tools) == expected
        if kind == AgentKind.SOURCING:
            assert all(callable(tool.is_enabled) for tool in agent.tools)


def test_openai_provider_keeps_responses_native_structured_output():
    adapter = OpenAICompatibleTwoAgentAdapter(
        api_key="test-key-not-used",
        provider="openai",
        requirement_model="gpt-requirement",
        sourcing_model="gpt-sourcing",
    )

    assert adapter.transport == "responses"
    expected_outputs = {
        AgentKind.REQUIREMENT: ProviderRequirementResult,
        AgentKind.SOURCING: ProviderSourcingResult,
    }
    for kind, agent in adapter.agents.items():
        assert "Responses" in type(agent.model).__mro__[1].__name__
        assert agent.output_type is expected_outputs[kind]
        assert agent.model_settings.extra_body is None


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def run(self, agent, input_text, *, context, max_turns):
        payload = json.loads(input_text)
        self.calls.append(
            {"agent": agent, "payload": payload, "context": context, "max_turns": max_turns}
        )
        envelope = payload["envelope"]
        source = context.run.messages[-1]
        spec = RequirementSpecService().propose(
            run_id=context.run.run_id,
            message_id=source.message_id,
            text=source.content,
            base=context.run.requirement_spec,
        )
        output = ProviderRequirementResult(
            task_id=envelope["task"]["task_id"],
            agent=envelope["task"]["agent"],
            # Reproduce DeepSeek confusing RequirementSpec.version with run state_version.
            state_version=envelope["state_version"] + 1,
            status="SUCCEEDED",
            missing_fields=["quantity"],
            requirement_spec=spec,
        )
        usage = SimpleNamespace(requests=1, input_tokens=101, output_tokens=17)
        return SimpleNamespace(final_output=output, context_wrapper=SimpleNamespace(usage=usage))


@pytest.mark.asyncio
async def test_provider_adapter_uses_one_isolated_typed_agent_call():
    runner = FakeRunner()
    adapter = make_adapter(runner=runner)
    engine = make_engine()
    run = (await engine.start(tenant_id="demo_cn", user_id="employee_001", message="采购电脑")).run
    employee = engine.enterprise.identity(run.tenant_id, run.user_id)
    context = AgentRuntimeContext(
        run=run,
        catalog=engine.catalog,
        enterprise=engine.enterprise,
        harness=engine.harness,
        roles=employee.roles,
    )
    task = AgentTask(
        task_id="requirement_3",
        agent=AgentKind.REQUIREMENT,
        objective="Identify missing fields",
        max_model_requests=2,
        max_tool_calls=1,
    )
    envelope = AgentTaskEnvelope(
        run_id=run.run_id,
        state_version=3,
        task=task,
        requirement={},
        missing_fields=["quantity"],
        context_summary="采购电脑",
    )

    result = await adapter.execute_agent(
        envelope,
        context=context,
        allowed_tools=AGENT_TOOL_ALLOWLIST[AgentKind.REQUIREMENT],
    )

    assert result.output.task_id == "requirement_3"
    assert result.output.state_version == 3
    assert result.usage.input_tokens == 101
    assert runner.calls[0]["agent"] is adapter.agents[AgentKind.REQUIREMENT]
    assert runner.calls[0]["context"] is context
    assert runner.calls[0]["max_turns"] == 2


def test_provider_adapter_repairs_only_schema_valid_json():
    recovered = OpenAICompatibleTwoAgentAdapter._recover_output(
        [
            {
                "content": [
                    {
                        "text": (
                            "result follows\n"
                            '{"task_id":"source_4","agent":"SOURCING",'
                            '"state_version":4,"status":"SUCCEEDED"}'
                        )
                    }
                ]
            }
        ]
    )

    assert recovered.agent == AgentKind.SOURCING
    assert recovered.state_version == 4


def test_provider_sourcing_result_is_reduced_to_one_candidate():
    result = ProviderAgentResult(
        task_id="source_1",
        agent=AgentKind.SOURCING,
        state_version=1,
        status="SUCCEEDED",
        candidate_proposals=[
            AgentCandidateProposal(
                provider="internal_catalog", offer_id="offer_1", evidence_refs=["ev_1"]
            ),
            AgentCandidateProposal(
                provider="marketplace_alpha", offer_id="offer_2", evidence_refs=["ev_2"]
            ),
        ],
    ).to_agent_result()

    assert [proposal.offer_id for proposal in result.candidate_proposals] == ["offer_1"]


def test_requirement_missing_fields_normalizes_failure_semantics():
    result = ProviderAgentResult(
        task_id="requirement_1",
        agent=AgentKind.REQUIREMENT,
        state_version=1,
        status="FAILED",
        missing_fields=["delivery_city"],
    ).to_agent_result()

    assert result.status == "SUCCEEDED"
    assert result.output["missing_fields"] == ["delivery_city"]


def test_requirement_legacy_top_level_evidence_is_safely_normalized():
    spec = RequirementSpecService().propose(
        run_id="run_legacy",
        message_id="msg_legacy",
        text="采购电脑",
        base=None,
    )
    evidence_refs = [item.evidence_id for item in spec.evidence]
    legacy = ProviderAgentResult(
        task_id="requirement_0",
        agent=AgentKind.REQUIREMENT,
        state_version=0,
        status="SUCCEEDED",
        evidence_refs=evidence_refs,
        requirement_spec=spec,
    )

    parsed = OpenAICompatibleTwoAgentAdapter._parse_output(
        AgentKind.REQUIREMENT, legacy.model_dump(mode="json")
    )

    assert parsed.evidence_refs == []
    assert parsed.requirement_spec == spec


def test_requirement_legacy_foreign_evidence_is_rejected():
    spec = RequirementSpecService().propose(
        run_id="run_foreign",
        message_id="msg_foreign",
        text="采购电脑",
        base=None,
    )
    legacy = ProviderAgentResult(
        task_id="requirement_0",
        agent=AgentKind.REQUIREMENT,
        state_version=0,
        status="SUCCEEDED",
        evidence_refs=["catalog_offer_123"],
        requirement_spec=spec,
    )

    with pytest.raises(ValueError):
        OpenAICompatibleTwoAgentAdapter._parse_output(
            AgentKind.REQUIREMENT, legacy.model_dump(mode="json")
        )


def test_server_normalizes_requirement_version_from_legacy_run_version():
    spec = RequirementSpecService().propose(
        run_id="run_version",
        message_id="msg_version",
        text="采购电脑",
        base=None,
    )
    spec.base_version = 7
    spec.version = 8
    spec.delta.from_version = 7
    spec.delta.to_version = 8
    output = ProviderAgentResult(
        task_id="requirement_0",
        agent=AgentKind.REQUIREMENT,
        state_version=0,
        status="SUCCEEDED",
        requirement_spec=spec,
    )
    envelope = AgentTaskEnvelope(
        run_id="run_version",
        state_version=0,
        task=AgentTask(
            task_id="requirement_0",
            agent=AgentKind.REQUIREMENT,
            objective="extract",
            max_tool_calls=0,
        ),
        requirement={},
    )

    OpenAICompatibleTwoAgentAdapter._normalize_requirement_version(output, envelope)

    assert output.requirement_spec is not None
    assert output.requirement_spec.base_version == 0
    assert output.requirement_spec.version == 1
    assert output.requirement_spec.delta.from_version == 0
    assert output.requirement_spec.delta.to_version == 1


def test_server_binds_model_routing_metadata_to_invoking_envelope():
    output = ProviderAgentResult(
        task_id="wrong_task",
        agent=AgentKind.SOURCING,
        state_version=99,
        status="SUCCEEDED",
    )
    envelope = AgentTaskEnvelope(
        run_id="run_binding",
        state_version=4,
        task=AgentTask(
            task_id="requirement_4",
            agent=AgentKind.REQUIREMENT,
            objective="extract",
            max_tool_calls=0,
        ),
        requirement={},
    )

    OpenAICompatibleTwoAgentAdapter._bind_result_to_envelope(output, envelope)

    assert output.task_id == "requirement_4"
    assert output.agent == AgentKind.REQUIREMENT
    assert output.state_version == 4
    assert output.warnings
