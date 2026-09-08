import pytest
from sqlalchemy import create_engine, select

from procure_pilot.domain.memory import LongTermMemory, MemoryScope
from procure_pilot.harness.runtime import PersistenceConflictError
from procure_pilot.persistence.memory_repository import SqlAlchemyMemoryRepository
from procure_pilot.persistence.models import Base, LongTermMemoryRecord


@pytest.fixture
def repository(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'memory.db'}")
    Base.metadata.create_all(engine)
    return SqlAlchemyMemoryRepository("sqlite://", engine=engine)


def make_memory(scope=MemoryScope.PROFILE, *, owner="employee_001", team=None):
    return LongTermMemory(
        tenant_id="demo_cn",
        owner_user_id=owner,
        team_id=team,
        scope=scope,
        key="preferred_laptop_ram",
        content="开发笔记本优先 32GB 内存",
        source_ref="user://explicit-preference",
        consent_id="consent_001",
    )


def test_personal_memory_is_tenant_and_owner_scoped(repository):
    memory = repository.create(make_memory(), actor_user_id="employee_001")

    loaded = repository.get(
        memory.memory_id,
        tenant_id="demo_cn",
        user_id="employee_001",
        team_id="R&D",
    )
    assert loaded.content == memory.content

    with pytest.raises(PermissionError):
        repository.get(
            memory.memory_id,
            tenant_id="demo_cn",
            user_id="employee_002",
            team_id="R&D",
        )
    with pytest.raises(PermissionError):
        repository.get(
            memory.memory_id,
            tenant_id="demo_id",
            user_id="employee_101",
            team_id="R&D",
        )


def test_team_memory_is_visible_to_team_but_only_owner_can_change(repository):
    memory = repository.create(
        make_memory(MemoryScope.TEAM, team="R&D"), actor_user_id="employee_001"
    )

    visible = repository.list(
        tenant_id="demo_cn",
        user_id="employee_002",
        team_id="R&D",
    )
    assert [item.memory_id for item in visible] == [memory.memory_id]

    with pytest.raises(PermissionError):
        repository.update_content(
            memory.memory_id,
            tenant_id="demo_cn",
            actor_user_id="employee_002",
            content="未经所有者授权的修改",
            expected_version=0,
        )


def test_update_delete_and_audit_are_versioned(repository):
    memory = repository.create(make_memory(), actor_user_id="employee_001")
    updated = repository.update_content(
        memory.memory_id,
        tenant_id="demo_cn",
        actor_user_id="employee_001",
        content="开发笔记本至少 64GB 内存",
        expected_version=0,
    )
    assert updated.version == 1

    with pytest.raises(PersistenceConflictError):
        repository.update_content(
            memory.memory_id,
            tenant_id="demo_cn",
            actor_user_id="employee_001",
            content="旧页面覆盖",
            expected_version=0,
        )

    repository.delete(
        memory.memory_id,
        tenant_id="demo_cn",
        actor_user_id="employee_001",
        expected_version=1,
    )
    assert repository.list(tenant_id="demo_cn", user_id="employee_001", team_id="R&D") == []
    audit = repository.audit(
        memory.memory_id,
        tenant_id="demo_cn",
        owner_user_id="employee_001",
    )
    assert [event.action for event in audit] == ["CREATED", "UPDATED", "DELETED"]
    assert all("content" not in (event.before or {}) for event in audit)
    assert all("content" not in (event.after or {}) for event in audit)
    with repository.session_factory() as session:
        stored_content = session.execute(
            select(LongTermMemoryRecord.content).where(
                LongTermMemoryRecord.memory_id == memory.memory_id
            )
        ).scalar_one()
    assert stored_content == "__deleted__"
