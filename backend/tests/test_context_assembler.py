from __future__ import annotations

from sqlalchemy import create_engine

from procure_pilot.agents.contracts import AgentKind
from procure_pilot.agents.multi_agent import FixtureTwoAgentAdapter, TwoAgentPlanner
from procure_pilot.domain.memory import LongTermMemory, MemoryScope
from procure_pilot.domain.models import Message, Money, ProcurementRequirement, ProcurementRun
from procure_pilot.evals.runner import COMPLETE_REQUEST, make_engine
from procure_pilot.persistence.memory_repository import SqlAlchemyMemoryRepository
from procure_pilot.persistence.models import Base
from procure_pilot.services.context import DEFAULT_CONTEXT_POLICY, ContextAssembler


def make_repository(tmp_path) -> SqlAlchemyMemoryRepository:
    database_engine = create_engine(f"sqlite:///{tmp_path / 'context.db'}")
    Base.metadata.create_all(database_engine)
    return SqlAlchemyMemoryRepository("sqlite://", engine=database_engine)


def add_memory(
    repository: SqlAlchemyMemoryRepository,
    *,
    key: str,
    content: str,
    scope: MemoryScope = MemoryScope.PROFILE,
    tenant_id: str = "demo_cn",
    owner_user_id: str = "employee_001",
    team_id: str | None = None,
) -> LongTermMemory:
    memory = LongTermMemory(
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        team_id=team_id,
        scope=scope,
        key=key,
        content=content,
        source_ref=f"test://{key}",
        consent_id="consent_test",
    )
    return repository.create(memory, actor_user_id=owner_user_id)


def complete_run() -> ProcurementRun:
    return ProcurementRun(
        tenant_id="demo_cn",
        user_id="employee_001",
        requirement=ProcurementRequirement(
            quantity=20,
            unit_budget=Money(amount="8000"),
            budget_tax_included=True,
            delivery_city="Jakarta",
            delivery_days=14,
            minimum_ram_gb=32,
            minimum_storage_gb=1024,
        ),
        messages=[Message(role="user", content=COMPLETE_REQUEST)],
    )


def test_context_is_tenant_scoped_and_projected_per_agent(tmp_path):
    repository = make_repository(tmp_path)
    preferred = add_memory(
        repository,
        key="procurement.laptop.preferred_brand",
        content="优先 Lenovo",
    )
    team_standard = add_memory(
        repository,
        key="team.device_standard.laptop",
        content="团队开发机使用 32GB 内存和三年保修",
        scope=MemoryScope.TEAM,
        team_id="R&D",
    )
    episodic = add_memory(
        repository,
        key="procurement.laptop.preferred_supplier",
        content="上次选择 internal_catalog 后交付稳定",
        scope=MemoryScope.EPISODIC,
    )
    semantic = add_memory(
        repository,
        key="team.device_standard.policy",
        content="这应由 RAG 提供，而不是作为记忆进入 Agent",
        scope=MemoryScope.SEMANTIC,
        team_id="R&D",
    )
    wrong_category = add_memory(
        repository,
        key="team.device_standard.phone",
        content="手机标准不能进入笔记本采购上下文",
        scope=MemoryScope.TEAM,
        team_id="R&D",
    )
    foreign_owner = add_memory(
        repository,
        key="procurement.laptop.preferred_brand",
        content="其他用户偏好 Apple",
        owner_user_id="employee_999",
    )
    foreign_tenant = add_memory(
        repository,
        key="procurement.laptop.preferred_brand",
        content="其他租户偏好 Dell",
        tenant_id="demo_id",
        owner_user_id="employee_101",
    )

    assembler = ContextAssembler(repository)
    run = complete_run()
    requirement = assembler.assemble(agent=AgentKind.REQUIREMENT, run=run, team_id="R&D")
    sourcing = assembler.assemble(agent=AgentKind.SOURCING, run=run, team_id="R&D")

    assert requirement.selected_memory_ids == [team_standard.memory_id]
    assert set(sourcing.selected_memory_ids) == {
        preferred.memory_id,
        team_standard.memory_id,
        episodic.memory_id,
    }
    observed_ids = {
        *sourcing.selected_memory_ids,
        *(item.memory_id for item in sourcing.exclusions),
    }
    assert semantic.memory_id in observed_ids
    assert wrong_category.memory_id in observed_ids
    assert foreign_owner.memory_id not in observed_ids
    assert foreign_tenant.memory_id not in observed_ids
    assert any(
        item.memory_id == semantic.memory_id and item.reason == "KNOWLEDGE_NOT_MEMORY"
        for item in sourcing.exclusions
    )
    assert any(
        item.memory_id == wrong_category.memory_id and item.reason == "KEY_NOT_ALLOWED_FOR_AGENT"
        for item in sourcing.exclusions
    )


def test_current_input_and_scope_authority_resolve_conflicts(tmp_path):
    repository = make_repository(tmp_path)
    profile_minimum = add_memory(
        repository,
        key="procurement.laptop.minimum_ram_gb",
        content="64GB",
    )
    team_minimum = add_memory(
        repository,
        key="procurement.laptop.minimum_ram_gb",
        content="32GB",
        scope=MemoryScope.TEAM,
        team_id="R&D",
    )
    profile_preference = add_memory(
        repository,
        key="procurement.laptop.preferred_brand",
        content="Lenovo",
    )
    team_preference = add_memory(
        repository,
        key="procurement.laptop.preferred_brand",
        content="Dell",
        scope=MemoryScope.TEAM,
        team_id="R&D",
    )
    assembler = ContextAssembler(repository)

    incomplete = ProcurementRun(tenant_id="demo_cn", user_id="employee_001")
    requirement = assembler.assemble(
        agent=AgentKind.REQUIREMENT,
        run=incomplete,
        team_id="R&D",
    )
    sourcing = assembler.assemble(
        agent=AgentKind.SOURCING,
        run=complete_run(),
        team_id="R&D",
    )

    assert team_minimum.memory_id in requirement.selected_memory_ids
    assert profile_minimum.memory_id not in requirement.selected_memory_ids
    assert any(
        item.memory_id == profile_minimum.memory_id and item.reason == "CONFLICT_SUPERSEDED"
        for item in requirement.exclusions
    )
    assert profile_preference.memory_id in sourcing.selected_memory_ids
    assert team_preference.memory_id not in sourcing.selected_memory_ids

    explicit = complete_run()
    explicit_requirement = assembler.assemble(
        agent=AgentKind.REQUIREMENT,
        run=explicit,
        team_id="R&D",
    )
    assert team_minimum.memory_id not in explicit_requirement.selected_memory_ids
    assert {
        item.memory_id
        for item in explicit_requirement.exclusions
        if item.reason == "CURRENT_INPUT_WINS"
    } == {profile_minimum.memory_id, team_minimum.memory_id}


def test_context_trimming_keeps_current_message_and_whole_memory_items(tmp_path):
    repository = make_repository(tmp_path)
    small = add_memory(
        repository,
        key="procurement.laptop.preferred_brand",
        content="Lenovo",
    )
    oversized = add_memory(
        repository,
        key="procurement.laptop.preferred_warranty",
        content="三年保修" * 500,
    )
    policy = DEFAULT_CONTEXT_POLICY.model_copy(deep=True)
    policy.agents[AgentKind.SOURCING].max_context_tokens = 350
    assembler = ContextAssembler(repository, policy=policy)
    run = complete_run()
    run.messages = [
        Message(role="user", content="旧消息" * 250),
        Message(role="assistant", content="旧回复" * 250),
        Message(role="user", content="本次明确请求"),
    ]

    result = assembler.assemble(agent=AgentKind.SOURCING, run=run, team_id="R&D")

    assert [item.content for item in result.payload.recent_messages] == ["本次明确请求"]
    assert result.dropped_message_count == 2
    assert small.memory_id in result.selected_memory_ids
    assert oversized.memory_id not in result.selected_memory_ids
    assert any(
        item.memory_id == oversized.memory_id and item.reason == "TOKEN_BUDGET_EXCEEDED"
        for item in result.exclusions
    )
    assert result.payload.estimated_tokens <= 350


class ContextRecordingAdapter(FixtureTwoAgentAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.envelopes = []

    async def execute_agent(self, envelope, *, context, allowed_tools):
        self.envelopes.append(envelope)
        return await super().execute_agent(envelope, context=context, allowed_tools=allowed_tools)


async def test_engine_passes_assembled_context_to_agent_and_traces_only_metadata(tmp_path):
    repository = make_repository(tmp_path)
    memory = add_memory(
        repository,
        key="procurement.laptop.preferred_brand",
        content="只在 Agent envelope 中使用的私人偏好",
    )
    adapter = ContextRecordingAdapter()
    engine = make_engine()
    engine.planner = TwoAgentPlanner(adapter, fallback_on_error=False)
    engine.configure_memory_context(repository)

    response = await engine.start(
        tenant_id="demo_cn",
        user_id="employee_001",
        message=COMPLETE_REQUEST,
    )

    assert len(adapter.envelopes) == 1
    envelope = adapter.envelopes[0]
    assert envelope.task.agent == AgentKind.SOURCING
    assert envelope.context is not None
    assert [item.memory_id for item in envelope.context.memories] == [memory.memory_id]
    trace = next(event for event in response.run.trace if event.name == "context.assembly_completed")
    assert trace.detail["selected"][0]["memory_id"] == memory.memory_id
    assert "只在 Agent envelope 中使用的私人偏好" not in str(trace.detail)
    assert "tenant_id" not in envelope.context.model_dump()
    assert "user_id" not in envelope.context.model_dump()
