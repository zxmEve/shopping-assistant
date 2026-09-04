from procure_pilot.agents.runtime import OpenAIAgentsPlanner, topology_description


def test_openai_agents_sdk_topology_builds_without_calling_network():
    planner = OpenAIAgentsPlanner("gpt-5-mini", "test-key-not-used")
    assert planner.manager.name == "Procurement Orchestrator"
    assert len(planner.manager.tools) == 3
    assert {tool.name for tool in planner.manager.tools} == {
        "clarification_specialist",
        "sourcing_specialist",
        "policy_specialist",
    }


def test_topology_description_discloses_fixture_mode():
    topology = topology_description("deterministic", "gpt-5-mini")
    assert topology["model"] == "deterministic-fixture"
    assert topology["business_writes"] == "server-side HITL only"
