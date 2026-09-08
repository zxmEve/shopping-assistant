from procure_pilot.agents.runtime import topology_description


def test_topology_description_discloses_exactly_two_agents():
    topology = topology_description("deterministic", "gpt-5-mini")

    assert topology["topology"] == "two_agent"
    assert topology["pattern"] == "deterministic-runtime-with-two-agents"
    assert topology["model"] == "deterministic-two-agent-fixture"
    assert topology["agents"] == ["Requirement Agent", "Sourcing Agent"]
    assert topology["manager"].endswith("not an Agent)")
    assert topology["function_tools"] == [
        "inspect_procurement_requirement",
        "search_catalog",
    ]


def test_topology_description_discloses_deterministic_business_services():
    topology = topology_description(
        "openai",
        "deepseek-chat",
        provider="deepseek",
        models={"requirement": "deepseek-chat", "sourcing": "deepseek-chat"},
    )

    assert topology["model_provider"] == "deepseek"
    assert topology["model_assignments"] == {
        "requirement": "deepseek-chat",
        "sourcing": "deepseek-chat",
    }
    assert "landed_cost_calculation" in topology["deterministic_services"]
    assert "budget_and_policy_evaluation" in topology["deterministic_services"]
    assert "human_confirmation_and_idempotent_write" in topology["deterministic_services"]
    assert topology["business_writes"] == "server-side HITL only"


def test_old_explicit_name_is_reported_as_two_agent_migration_alias():
    topology = topology_description("deterministic", "gpt-5-mini", topology="explicit_multi_agent")

    assert topology["requested_topology"] == "explicit_multi_agent"
    assert topology["topology"] == "two_agent"
    assert "Verification Agent" not in topology["agents"]
