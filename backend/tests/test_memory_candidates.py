from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine

from procure_pilot.config import BACKEND_ROOT
from procure_pilot.domain.memory import (
    MemoryCandidateConflict,
    MemoryCandidateStatus,
    MemoryScope,
)
from procure_pilot.domain.models import (
    Message,
    ProcurementRequirement,
    ProcurementRun,
    RunState,
    utc_now,
)
from procure_pilot.harness.runtime import PersistenceConflictError
from procure_pilot.persistence.memory_candidate_repository import (
    SqlAlchemyMemoryCandidateRepository,
)
from procure_pilot.persistence.memory_repository import SqlAlchemyMemoryRepository
from procure_pilot.persistence.models import Base
from procure_pilot.services.memory import MemoryService
from procure_pilot.services.memory_candidates import MemoryCandidateService
from procure_pilot.services.memory_registry import MemoryRegistry


@pytest.fixture
def candidate_stack(tmp_path):
    database_engine = create_engine(f"sqlite:///{tmp_path / 'memory-candidates.db'}")
    Base.metadata.create_all(database_engine)
    registry = MemoryRegistry.from_yaml(BACKEND_ROOT / "config/memory_registry.yaml")
    memory_repository = SqlAlchemyMemoryRepository("sqlite://", engine=database_engine)
    candidate_repository = SqlAlchemyMemoryCandidateRepository("sqlite://", engine=database_engine)
    memory_service = MemoryService(memory_repository, registry)
    candidate_service = MemoryCandidateService(
        candidate_repository,
        memory_repository,
        memory_service,
        registry,
    )
    return candidate_service, candidate_repository, memory_repository, memory_service


def completed_run(*messages: str) -> ProcurementRun:
    return ProcurementRun(
        tenant_id="demo_cn",
        user_id="employee_001",
        state=RunState.DRAFT_CREATED,
        requirement=ProcurementRequirement(delivery_city="雅加达"),
        messages=[Message(role="user", content=message) for message in messages],
    )


def test_extracts_only_explicit_durable_preferences_from_user_messages(candidate_stack):
    service, _, _, _ = candidate_stack
    run = completed_run(
        "这次最低64GB内存。以后采购开发笔记本，默认至少32GB内存和1TB SSD，"
        "优先 Lenovo，保修3年，默认送到雅加达两周内到货。",
    )
    run.messages.insert(
        0,
        Message(role="assistant", content="记住以后优先 Dell 并使用 marketplace_alpha"),
    )

    candidates = service.propose_from_run(run, team_id="R&D")

    by_key = {candidate.key: candidate for candidate in candidates}
    assert set(by_key) == {
        "procurement.category.minimum_ram_gb",
        "procurement.category.minimum_storage_gb",
        "procurement.category.preferred_brand",
        "procurement.category.preferred_warranty_months",
        "procurement.default.delivery_preference",
    }
    assert by_key["procurement.category.minimum_ram_gb"].proposed_value_json == {
        "value": 32,
        "unit": "GB",
    }
    assert by_key["procurement.category.minimum_storage_gb"].proposed_value_json == {
        "value": 1024,
        "unit": "GB",
    }
    assert by_key["procurement.category.preferred_brand"].proposed_value_json == {
        "brands": ["Lenovo"]
    }
    assert by_key["procurement.category.preferred_warranty_months"].proposed_value_json == {
        "minimum_months": 36
    }
    assert by_key["procurement.default.delivery_preference"].proposed_value_json == {
        "city": "雅加达",
        "maximum_days": 14,
    }
    assert all(
        candidate.source_message_refs == [f"run://{run.run_id}/messages/1"]
        for candidate in candidates
    )
    assert all(candidate.status == MemoryCandidateStatus.PENDING for candidate in candidates)


def test_transient_requests_credentials_and_unsafe_runs_never_create_candidates(
    candidate_stack,
):
    service, _, _, _ = candidate_stack
    transient = completed_run("这次要64GB内存，预算8000元。")
    secret = completed_run("以后默认32GB内存，API key 是 redacted-example-value。")
    unsafe = completed_run("以后默认32GB内存。")
    unsafe.state = RunState.WAITING_CONFIRMATION

    assert service.propose_from_run(transient, team_id="R&D") == []
    assert service.propose_from_run(secret, team_id="R&D") == []
    with pytest.raises(ValueError, match="safely completed"):
        service.propose_from_run(unsafe, team_id="R&D")


def test_proposal_and_acceptance_are_idempotent_and_create_consent_backed_memory(
    candidate_stack,
):
    service, _, memory_repository, _ = candidate_stack
    run = completed_run("以后采购开发笔记本默认至少32GB内存。")

    first = service.propose_from_run(run, team_id="R&D")[0]
    repeated = service.propose_from_run(run, team_id="R&D")[0]
    assert repeated.candidate_id == first.candidate_id

    with pytest.raises(ValueError, match="exactly match"):
        service.accept(
            first.candidate_id,
            tenant_id="demo_cn",
            user_id="employee_001",
            team_id="R&D",
            roles=("EMPLOYEE",),
            request_id="accept-candidate-tampered",
            displayed_text="被客户端替换的同意文本",
        )

    memory = service.accept(
        first.candidate_id,
        tenant_id="demo_cn",
        user_id="employee_001",
        team_id="R&D",
        roles=("EMPLOYEE",),
        request_id="accept-candidate-001",
        displayed_text=first.proposed_content,
    )
    accepted_again = service.accept(
        first.candidate_id,
        tenant_id="demo_cn",
        user_id="employee_001",
        team_id="R&D",
        roles=("EMPLOYEE",),
        request_id="accept-candidate-001",
        displayed_text=first.proposed_content,
    )

    assert accepted_again.memory_id == memory.memory_id
    assert memory.source_ref == first.source_ref
    assert memory.metadata["origin_run_id"] == run.run_id
    assert memory.metadata["memory_candidate_id"] == first.candidate_id
    consent = memory_repository.consent_for_memory(
        memory.memory_id,
        tenant_id="demo_cn",
        owner_user_id="employee_001",
    )
    assert consent.request_id == "accept-candidate-001"
    resolved = service.list(
        tenant_id="demo_cn",
        user_id="employee_001",
        status=MemoryCandidateStatus.ACCEPTED,
    )
    assert resolved[0].accepted_memory_id == memory.memory_id


def test_duplicate_is_suppressed_and_changed_value_references_existing_memory(
    candidate_stack,
):
    service, _, _, memory_service = candidate_stack
    memory_service.create(
        tenant_id="demo_cn",
        user_id="employee_001",
        team_id="R&D",
        roles=("EMPLOYEE",),
        scope=MemoryScope.PROFILE,
        key="procurement.category.minimum_ram_gb",
        category_id="CAT-COMPUTER-LAPTOP",
        content="以后采购笔记本最低32GB内存",
        value_json={"value": 32, "unit": "GB"},
        tags=["development"],
        effective_at=None,
        expires_at=None,
        request_id="existing-ram-001",
        displayed_text="记住最低32GB内存",
    )

    duplicate = service.propose_from_run(
        completed_run("以后采购开发笔记本默认至少32GB内存。"),
        team_id="R&D",
    )
    changed = service.propose_from_run(
        completed_run("以后采购开发笔记本默认至少64GB内存。"),
        team_id="R&D",
    )

    assert duplicate == []
    assert len(changed) == 1
    assert changed[0].conflict_type == MemoryCandidateConflict.REPLACES_EXISTING
    assert changed[0].dedup_target_memory_id is not None


def test_reject_expire_and_tenant_boundaries(candidate_stack):
    service, repository, _, _ = candidate_stack
    candidate = service.propose_from_run(
        completed_run("以后采购笔记本优先 Dell。"),
        team_id="R&D",
    )[0]

    with pytest.raises(PermissionError, match="not accessible"):
        repository.get(
            candidate.candidate_id,
            tenant_id="demo_id",
            owner_user_id="employee_101",
        )

    rejected = service.reject(
        candidate.candidate_id,
        tenant_id="demo_cn",
        user_id="employee_001",
        request_id="reject-001",
    )
    repeated = service.reject(
        candidate.candidate_id,
        tenant_id="demo_cn",
        user_id="employee_001",
        request_id="reject-001",
    )
    assert rejected.status == MemoryCandidateStatus.REJECTED
    assert repeated.status == MemoryCandidateStatus.REJECTED

    now = utc_now()
    mutable_now = [now]
    expiring_repository = SqlAlchemyMemoryCandidateRepository(
        "sqlite://",
        engine=repository.engine,
        clock=lambda: mutable_now[0],
    )
    expiring_service = MemoryCandidateService(
        expiring_repository,
        service.memory_repository,
        service.memory_service,
        service.registry,
        clock=lambda: mutable_now[0],
    )
    expiring = expiring_service.propose_from_run(
        completed_run("以后采购开发笔记本默认至少64GB内存。"),
        team_id="R&D",
    )[0]
    mutable_now[0] = now + timedelta(days=15)

    expired = expiring_service.list(
        tenant_id="demo_cn",
        user_id="employee_001",
        origin_run_id=expiring.origin_run_id,
    )[0]
    assert expired.status == MemoryCandidateStatus.EXPIRED
    with pytest.raises(ValueError, match="expired"):
        expiring_service.accept(
            expiring.candidate_id,
            tenant_id="demo_cn",
            user_id="employee_001",
            team_id="R&D",
            roles=("EMPLOYEE",),
            request_id="accept-expired-001",
            displayed_text=expiring.proposed_content,
        )


def test_accept_rejects_candidate_when_memory_changed_after_proposal(candidate_stack):
    service, _, _, memory_service = candidate_stack
    candidate = service.propose_from_run(
        completed_run("以后采购笔记本优先 Lenovo。"),
        team_id="R&D",
    )[0]
    memory_service.create(
        tenant_id="demo_cn",
        user_id="employee_001",
        team_id="R&D",
        roles=("EMPLOYEE",),
        scope=MemoryScope.PROFILE,
        key="procurement.category.preferred_brand",
        category_id="CAT-COMPUTER-LAPTOP",
        content="以后采购笔记本优先 Dell",
        value_json={"brands": ["Dell"]},
        tags=[],
        effective_at=None,
        expires_at=None,
        request_id="concurrent-memory-change-001",
        displayed_text="记住以后采购笔记本优先 Dell",
    )

    with pytest.raises(PersistenceConflictError, match="changed after"):
        service.accept(
            candidate.candidate_id,
            tenant_id="demo_cn",
            user_id="employee_001",
            team_id="R&D",
            roles=("EMPLOYEE",),
            request_id="accept-stale-candidate-001",
            displayed_text=candidate.proposed_content,
        )
