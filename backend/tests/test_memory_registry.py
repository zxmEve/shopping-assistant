from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select

from procure_pilot.agents.contracts import AgentKind
from procure_pilot.config import BACKEND_ROOT
from procure_pilot.domain.memory import LongTermMemory, MemoryScope
from procure_pilot.domain.models import (
    Message,
    Money,
    ProcurementRequirement,
    ProcurementRun,
    utc_now,
)
from procure_pilot.harness.runtime import PersistenceConflictError
from procure_pilot.persistence.memory_repository import SqlAlchemyMemoryRepository
from procure_pilot.persistence.models import (
    Base,
    LongTermMemoryRecord,
    MemoryConsentRecord,
)
from procure_pilot.services.context import ContextAssembler
from procure_pilot.services.memory import MemoryService
from procure_pilot.services.memory_registry import MemoryRegistry


@pytest.fixture
def registry() -> MemoryRegistry:
    return MemoryRegistry.from_yaml(BACKEND_ROOT / "config/memory_registry.yaml")


@pytest.fixture
def repository(tmp_path) -> SqlAlchemyMemoryRepository:
    database_engine = create_engine(f"sqlite:///{tmp_path / 'structured-memory.db'}")
    Base.metadata.create_all(database_engine)
    return SqlAlchemyMemoryRepository("sqlite://", engine=database_engine)


def create_brand_memory(
    service: MemoryService,
    *,
    request_id: str,
    brand: str = "Lenovo",
    scope: MemoryScope = MemoryScope.PROFILE,
    roles: tuple[str, ...] = ("EMPLOYEE",),
):
    return service.create(
        tenant_id="demo_cn",
        user_id="employee_001",
        team_id="R&D",
        roles=roles,
        scope=scope,
        key="procurement.category.preferred_brand",
        category_id="CAT-COMPUTER-LAPTOP",
        content=f"开发笔记本优先 {brand}",
        value_json={"brands": [brand]},
        tags=["开发"],
        effective_at=None,
        expires_at=None,
        request_id=request_id,
        displayed_text=f"记住开发笔记本优先 {brand}",
    )


def test_registry_resolves_controlled_vocabulary_and_rejects_ambiguity(registry):
    assert registry.resolve_category("notebook").category_id == "CAT-COMPUTER-LAPTOP"
    assert registry.canonicalize_tags(
        ["开发", "warranty-priority"],
        category_id="CAT-COMPUTER-LAPTOP",
    ) == ["TAG-DEVELOPMENT", "TAG-WARRANTY-PRIORITY"]

    with pytest.raises(ValueError, match="Ambiguous category alias"):
        registry.resolve_category("电脑")
    with pytest.raises(ValueError, match="not registered"):
        registry.validate_memory(
            key="user.invented.key",
            definition_version=None,
            category_id="CAT-COMPUTER-LAPTOP",
            scope=MemoryScope.PROFILE,
            value_json={"anything": True},
            tags=[],
        )


def test_registry_validates_value_schema_scope_and_tags(registry):
    definition, tags = registry.validate_memory(
        key="procurement.category.minimum_ram_gb",
        definition_version=None,
        category_id="CAT-COMPUTER-LAPTOP",
        scope=MemoryScope.PROFILE,
        value_json={"value": 32, "unit": "GB"},
        tags=["研发"],
    )
    assert definition.definition_version == 1
    assert tags == ["TAG-DEVELOPMENT"]

    with pytest.raises(ValueError, match="below the minimum"):
        registry.validate_memory(
            key="procurement.category.minimum_ram_gb",
            definition_version=None,
            category_id="CAT-COMPUTER-LAPTOP",
            scope=MemoryScope.PROFILE,
            value_json={"value": 2, "unit": "GB"},
            tags=[],
        )
    with pytest.raises(ValueError, match="does not allow scope"):
        registry.validate_memory(
            key="team.device_standard.developer_laptop",
            definition_version=None,
            category_id="CAT-COMPUTER-LAPTOP",
            scope=MemoryScope.PROFILE,
            value_json={
                "minimum_ram_gb": 32,
                "minimum_storage_gb": 1024,
                "minimum_warranty_months": 36,
            },
            tags=[],
        )
    with pytest.raises(ValueError, match="Tag is not registered"):
        registry.validate_memory(
            key="procurement.category.preferred_brand",
            definition_version=None,
            category_id="CAT-COMPUTER-LAPTOP",
            scope=MemoryScope.PROFILE,
            value_json={"brands": ["Lenovo"]},
            tags=["模型临时创造的标签"],
        )


def test_consent_idempotency_and_new_request_supersedes_previous(
    repository,
    registry,
):
    service = MemoryService(repository, registry)
    first = create_brand_memory(service, request_id="req-memory-001")
    repeated = create_brand_memory(service, request_id="req-memory-001")
    with pytest.raises(PersistenceConflictError, match="different payload"):
        create_brand_memory(
            service,
            request_id="req-memory-001",
            brand="Dell",
        )
    replacement = create_brand_memory(
        service,
        request_id="req-memory-002",
        brand="Dell",
    )

    assert repeated.memory_id == first.memory_id
    assert replacement.memory_id != first.memory_id
    assert replacement.metadata["supersedes_memory_id"] == first.memory_id
    visible = repository.list(
        tenant_id="demo_cn",
        user_id="employee_001",
        team_id="R&D",
    )
    assert [item.memory_id for item in visible] == [replacement.memory_id]
    with repository.session_factory() as session:
        records = session.execute(
            select(LongTermMemoryRecord).order_by(LongTermMemoryRecord.created_at)
        ).scalars()
        assert [item.status for item in records] == ["SUPERSEDED", "ACTIVE"]
        assert len(session.execute(select(MemoryConsentRecord)).scalars().all()) == 2


def test_team_memory_requires_trusted_role(repository, registry):
    service = MemoryService(repository, registry)
    with pytest.raises(PermissionError, match="TEAM memory"):
        service.create(
            tenant_id="demo_cn",
            user_id="employee_001",
            team_id="R&D",
            roles=("EMPLOYEE",),
            scope=MemoryScope.TEAM,
            key="team.device_standard.developer_laptop",
            category_id="CAT-COMPUTER-LAPTOP",
            content="研发笔记本标准",
            value_json={
                "minimum_ram_gb": 32,
                "minimum_storage_gb": 1024,
                "minimum_warranty_months": 36,
            },
            tags=["开发"],
            effective_at=None,
            expires_at=None,
            request_id="req-team-denied",
            displayed_text="发布团队研发笔记本标准",
        )

    allowed = service.create(
        tenant_id="demo_cn",
        user_id="employee_001",
        team_id="R&D",
        roles=("MEMORY_STEWARD",),
        scope=MemoryScope.TEAM,
        key="team.device_standard.developer_laptop",
        category_id="CAT-COMPUTER-LAPTOP",
        content="研发笔记本标准",
        value_json={
            "minimum_ram_gb": 32,
            "minimum_storage_gb": 1024,
            "minimum_warranty_months": 36,
        },
        tags=["开发"],
        effective_at=None,
        expires_at=None,
        request_id="req-team-allowed",
        displayed_text="发布团队研发笔记本标准",
    )
    assert allowed.team_id == "R&D"


def test_repository_excludes_future_and_expired_memories(repository):
    now = utc_now()
    future = LongTermMemory(
        tenant_id="demo_cn",
        owner_user_id="employee_001",
        scope=MemoryScope.PROFILE,
        key="procurement.category.preferred_brand",
        category_id="CAT-COMPUTER-LAPTOP",
        content="明天才生效",
        value_json={"brands": ["Lenovo"]},
        source_ref="test://future",
        consent_id="consent_future",
        effective_at=now + timedelta(days=1),
    )
    expired = LongTermMemory(
        tenant_id="demo_cn",
        owner_user_id="employee_001",
        scope=MemoryScope.PROFILE,
        key="procurement.category.preferred_brand",
        category_id="CAT-COMPUTER-LAPTOP",
        content="已经过期",
        value_json={"brands": ["Dell"]},
        source_ref="test://expired",
        consent_id="consent_expired",
        effective_at=now - timedelta(days=2),
        expires_at=now - timedelta(days=1),
    )
    repository.create(future, actor_user_id="employee_001")
    repository.create(expired, actor_user_id="employee_001")

    assert (
        repository.list(
            tenant_id="demo_cn",
            user_id="employee_001",
            team_id="R&D",
        )
        == []
    )


def test_structured_memory_projection_uses_registry_agent_and_category_rules(
    repository,
    registry,
):
    memory = create_brand_memory(
        MemoryService(repository, registry),
        request_id="req-context-structured",
    )
    run = ProcurementRun(
        tenant_id="demo_cn",
        user_id="employee_001",
        requirement=ProcurementRequirement(
            quantity=5,
            unit_budget=Money(amount="8000"),
            budget_tax_included=True,
            delivery_city="Jakarta",
            delivery_days=14,
            minimum_ram_gb=32,
            minimum_storage_gb=1024,
        ),
        messages=[Message(role="user", content="采购五台开发笔记本")],
    )
    assembler = ContextAssembler(repository, memory_registry=registry)

    requirement = assembler.assemble(
        agent=AgentKind.REQUIREMENT,
        run=run,
        team_id="R&D",
    )
    sourcing = assembler.assemble(
        agent=AgentKind.SOURCING,
        run=run,
        team_id="R&D",
    )

    assert memory.memory_id not in requirement.selected_memory_ids
    assert any(
        item.memory_id == memory.memory_id and item.reason == "KEY_NOT_ALLOWED_FOR_AGENT"
        for item in requirement.exclusions
    )
    assert sourcing.selected_memory_ids == [memory.memory_id]
    selected = sourcing.payload.memories[0]
    assert selected.category_id == "CAT-COMPUTER-LAPTOP"
    assert selected.key_definition_version == 1
    assert selected.value_json == {"brands": ["Lenovo"]}
    assert selected.tags == ["TAG-DEVELOPMENT"]
