from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from procure_pilot.api.app import create_app
from procure_pilot.config import Settings
from procure_pilot.domain.knowledge import (
    KNOWLEDGE_EMBEDDING_DIMENSIONS,
    PolicyKnowledgeReviewRequired,
)
from procure_pilot.domain.models import RunState, utc_now
from procure_pilot.evals.runner import make_engine
from procure_pilot.persistence.knowledge_repository import SqlAlchemyKnowledgeRepository
from procure_pilot.persistence.models import Base
from procure_pilot.services.embeddings import HashingEmbeddingProvider
from procure_pilot.services.knowledge import (
    PolicyKnowledgeService,
    clean_policy_text,
    split_policy_document,
)

EMPLOYEE_SESSION = {"Authorization": "Bearer fixture-session-cn-employee-001"}
OTHER_TENANT_SESSION = {"Authorization": "Bearer fixture-session-id-employee-101"}
ADMIN_SESSION = {"Authorization": "Bearer fixture-session-cn-knowledge-admin"}
CATEGORY = "CAT-COMPUTER-LAPTOP"


class SemanticFixtureEmbeddingProvider:
    model = "semantic-fixture-v1"
    dimensions = KNOWLEDGE_EMBEDDING_DIMENSIONS

    def embed(self, texts):
        values = []
        for text in texts:
            vector = [0.0] * self.dimensions
            vector[0 if "移动工作终端" in text or "内存" in text else 1] = 1.0
            values.append(vector)
        return values


class QueryFailingEmbeddingProvider(HashingEmbeddingProvider):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        if self.calls > 1:
            raise RuntimeError("fixture embedding provider unavailable")
        return super().embed(texts)


@pytest.fixture
def knowledge_stack(tmp_path):
    database_engine = create_engine(f"sqlite:///{tmp_path / 'knowledge.db'}")
    Base.metadata.create_all(database_engine)
    repository = SqlAlchemyKnowledgeRepository("sqlite://", engine=database_engine)
    return PolicyKnowledgeService(repository), repository


def policy_text(*, minimum_ram: int = 32) -> str:
    return f"""
    DEMO-PROC-2026-01 研发设备采购制度
    第 1 页 / 共 2 页

    第一条 研发笔记本技术标准
    研发笔记本不得低于 {minimum_ram}GB 内存，保修不得少于 36 个月，并且必须为全新商品。

    第二条 审批与供应商准入
    采购金额超过 10000 元需要直属经理审批。供应商未准入时必须由采购专员复核。
    第 2 页 / 共 2 页
    """


def ingest_and_publish(
    service: PolicyKnowledgeService,
    *,
    tenant_id: str = "demo_cn",
    policy_version: str = "3",
    effective_from: datetime | None = None,
    content: str | None = None,
):
    ingested = service.ingest(
        tenant_id=tenant_id,
        actor_user_id="knowledge_admin_001",
        roles=("KNOWLEDGE_STEWARD",),
        document_key="developer-laptop-policy",
        title="研发设备采购制度",
        category_id=CATEGORY,
        source_uri="https://policies.example.test/developer-laptop",
        policy_version=policy_version,
        effective_from=effective_from or utc_now() - timedelta(days=1),
        raw_content=content or policy_text(),
    )
    published = service.publish(
        ingested.document.document_id,
        tenant_id=tenant_id,
        roles=("KNOWLEDGE_STEWARD",),
        version=ingested.version.version,
    )
    return ingested, published


def test_cleaning_and_structural_chunking_preserve_policy_facts():
    raw = policy_text(minimum_ram=64)

    cleaned = clean_policy_text(raw)
    chunks = split_policy_document(cleaned, max_chars=300)

    assert "第 1 页" not in cleaned
    assert "不得低于 64GB" in cleaned
    assert "超过 10000 元" in cleaned
    assert [clause for _, clause, _ in chunks] == [None, "第一条", "第二条"]
    assert all(len(content) <= 300 for _, _, content in chunks)


def test_only_admin_can_ingest_and_only_published_effective_chunks_are_searchable(
    knowledge_stack,
):
    service, _ = knowledge_stack
    with pytest.raises(PermissionError, match="administrator"):
        service.ingest(
            tenant_id="demo_cn",
            actor_user_id="employee_001",
            roles=("EMPLOYEE",),
            document_key="denied-policy",
            title="不应导入的制度",
            category_id=CATEGORY,
            source_uri="https://policies.example.test/denied",
            policy_version="3",
            effective_from=utc_now(),
            raw_content=policy_text(),
        )

    ingested = service.ingest(
        tenant_id="demo_cn",
        actor_user_id="knowledge_admin_001",
        roles=("KNOWLEDGE_STEWARD",),
        document_key="developer-laptop-policy",
        title="研发设备采购制度",
        category_id=CATEGORY,
        source_uri="https://policies.example.test/developer-laptop",
        policy_version="3",
        effective_from=utc_now() + timedelta(days=1),
        raw_content=policy_text(),
    )
    assert (
        service.search(tenant_id="demo_cn", category_id=CATEGORY, query="研发笔记本保修").citations
        == []
    )
    service.publish(
        ingested.document.document_id,
        tenant_id="demo_cn",
        roles=("KNOWLEDGE_STEWARD",),
    )
    assert (
        service.search(tenant_id="demo_cn", category_id=CATEGORY, query="研发笔记本保修").citations
        == []
    )
    with pytest.raises(PolicyKnowledgeReviewRequired) as not_effective:
        service.verify_policy_evidence(
            tenant_id="demo_cn",
            category_id=CATEGORY,
            query="研发笔记本保修",
            expected_policy_version="3",
        )
    assert not_effective.value.code == "KNOWLEDGE_NO_EFFECTIVE_VERSION"

    result = service.search(
        tenant_id="demo_cn",
        category_id=CATEGORY,
        query="研发笔记本保修",
        as_of=utc_now() + timedelta(days=2),
    )
    assert result.citations
    assert result.citations[0].policy_version == "3"
    assert result.citations[0].source_ref.startswith("knowledge://developer-laptop-policy/v1/")
    assert (
        service.search(
            tenant_id="demo_id",
            category_id=CATEGORY,
            query="研发笔记本保修",
            as_of=utc_now() + timedelta(days=2),
        ).citations
        == []
    )


def test_publishing_new_version_preserves_historical_effective_search(knowledge_stack):
    service, _ = knowledge_stack
    first_at = datetime(2026, 1, 1, tzinfo=UTC)
    second_at = datetime(2026, 7, 1, tzinfo=UTC)
    first, _ = ingest_and_publish(
        service,
        policy_version="2",
        effective_from=first_at,
        content=policy_text(minimum_ram=16),
    )
    second, _ = ingest_and_publish(
        service,
        policy_version="3",
        effective_from=second_at,
        content=policy_text(minimum_ram=32),
    )

    historical = service.search(
        tenant_id="demo_cn",
        category_id=CATEGORY,
        query="最低内存",
        as_of=datetime(2026, 3, 1, tzinfo=UTC),
    )
    current = service.search(
        tenant_id="demo_cn",
        category_id=CATEGORY,
        query="最低内存",
        as_of=datetime(2026, 9, 1, tzinfo=UTC),
    )

    assert {item.document_version for item in historical.citations} == {first.version.version}
    assert {item.policy_version for item in historical.citations} == {"2"}
    assert {item.document_version for item in current.citations} == {second.version.version}
    assert {item.policy_version for item in current.citations} == {"3"}


def test_hybrid_search_can_recover_vector_only_match_and_explains_ranking(tmp_path):
    database_engine = create_engine(f"sqlite:///{tmp_path / 'hybrid.db'}")
    Base.metadata.create_all(database_engine)
    repository = SqlAlchemyKnowledgeRepository("sqlite://", engine=database_engine)
    service = PolicyKnowledgeService(
        repository,
        embedding_provider=SemanticFixtureEmbeddingProvider(),
    )
    ingest_and_publish(service)

    result = service.search(
        tenant_id="demo_cn",
        category_id=CATEGORY,
        query="移动工作终端",
    )

    assert result.retrieval_mode == "hybrid_rrf"
    assert result.fts_candidate_count == 0
    assert result.vector_candidate_count >= 1
    assert result.citations[0].clause_ref == "第一条"
    assert result.citations[0].retrieval_channels == ["vector"]
    assert result.citations[0].fts_rank is None
    assert result.citations[0].fts_score is None
    assert result.citations[0].vector_rank == 1
    assert result.citations[0].rrf_score > 0
    assert result.citations[0].rerank_score == result.citations[0].score


def test_vector_provider_failure_degrades_to_fts_without_losing_policy_answer(tmp_path):
    database_engine = create_engine(f"sqlite:///{tmp_path / 'degraded.db'}")
    Base.metadata.create_all(database_engine)
    repository = SqlAlchemyKnowledgeRepository("sqlite://", engine=database_engine)
    service = PolicyKnowledgeService(
        repository,
        embedding_provider=QueryFailingEmbeddingProvider(),
    )
    ingest_and_publish(service)

    result = service.search(
        tenant_id="demo_cn",
        category_id=CATEGORY,
        query="研发笔记本保修",
    )

    assert result.retrieval_mode == "fts_only"
    assert result.degraded_channels == ["vector"]
    assert result.citations
    assert result.citations[0].retrieval_channels == ["fts"]


def test_policy_evidence_fails_closed_after_governed_knowledge_is_enabled(knowledge_stack):
    service, _ = knowledge_stack
    ingest_and_publish(service, policy_version="2")

    with pytest.raises(PolicyKnowledgeReviewRequired) as conflict:
        service.verify_policy_evidence(
            tenant_id="demo_cn",
            category_id=CATEGORY,
            query="研发笔记本内存保修",
            expected_policy_version="3",
        )
    assert conflict.value.code == "POLICY_VERSION_CONFLICT"

    with pytest.raises(PolicyKnowledgeReviewRequired) as missing:
        service.verify_policy_evidence(
            tenant_id="demo_cn",
            category_id=CATEGORY,
            query="量子卫星发射许可",
            expected_policy_version="2",
        )
    assert missing.value.code == "KNOWLEDGE_NO_ANSWER"

    assert (
        service.verify_policy_evidence(
            tenant_id="demo_id",
            category_id=CATEGORY,
            query="研发笔记本内存保修",
            expected_policy_version="3",
        )
        is None
    )


@pytest.mark.asyncio
async def test_engine_adds_version_matched_policy_citations_and_blocks_conflicts(
    knowledge_stack,
):
    service, _ = knowledge_stack
    ingest_and_publish(service, policy_version="3")
    engine = make_engine()
    engine.knowledge_service = service

    completed = await engine.start(
        tenant_id="demo_cn",
        user_id="employee_001",
        message=(
            "采购20台开发笔记本，单台预算8000元含税，送雅加达，两周内到货，最低32GB内存和1TB存储"
        ),
    )

    assert completed.run.state == RunState.WAITING_CONFIRMATION
    assert any(
        ref.startswith("knowledge://developer-laptop-policy/v1/")
        for ref in completed.run.recommendation.best_overall.evidence_refs
    )
    assert any(event.name == "knowledge.policy_evidence_resolved" for event in completed.run.trace)

    conflict_service, _ = knowledge_stack
    # A second, isolated service is not available from this fixture, so change the executable
    # expectation to prove that the knowledge version veto remains server controlled.
    original_version = engine.enterprise.policy["version"]
    engine.enterprise.policy["version"] = 4
    blocked = await engine.start(
        tenant_id="demo_cn",
        user_id="employee_001",
        message=(
            "采购20台开发笔记本，单台预算8000元含税，送雅加达，两周内到货，最低32GB内存和1TB存储"
        ),
    )
    engine.enterprise.policy["version"] = original_version

    assert conflict_service is service
    assert blocked.run.state == RunState.FAILED
    assert any(
        event.detail.get("code") == "POLICY_VERSION_CONFLICT"
        for event in blocked.run.trace
        if event.name == "knowledge.policy_review_required"
    )


def test_knowledge_api_enforces_admin_write_and_tenant_scoped_read(knowledge_stack, tmp_path):
    _, repository = knowledge_stack
    engine = make_engine()
    client = TestClient(
        create_app(
            settings=Settings(run_store_path=tmp_path / "runs.json"),
            engine=engine,
            knowledge_repository=repository,
        )
    )
    payload = {
        "document_key": "developer-laptop-policy",
        "title": "研发设备采购制度",
        "category_id": CATEGORY,
        "source_uri": "https://policies.example.test/developer-laptop",
        "policy_version": "3",
        "effective_from": (utc_now() - timedelta(days=1)).isoformat(),
        "raw_content": policy_text(),
    }

    assert (
        client.post(
            "/internal/knowledge/documents", headers=EMPLOYEE_SESSION, json=payload
        ).status_code
        == 403
    )
    created = client.post("/internal/knowledge/documents", headers=ADMIN_SESSION, json=payload)
    assert created.status_code == 200
    document_id = created.json()["document"]["document_id"]
    published = client.post(
        f"/internal/knowledge/documents/{document_id}/publish",
        headers=ADMIN_SESSION,
        json={"version": 1},
    )
    assert published.status_code == 200

    found = client.get(
        "/api/knowledge/search",
        headers=EMPLOYEE_SESSION,
        params={"query": "笔记本保修", "category_id": CATEGORY},
    )
    isolated = client.get(
        "/api/knowledge/search",
        headers=OTHER_TENANT_SESSION,
        params={"query": "笔记本保修", "category_id": CATEGORY},
    )
    assert found.status_code == 200
    assert found.json()["citations"]
    assert found.json()["retrieval_mode"] == "hybrid_rrf"
    assert found.json()["citations"][0]["rrf_score"] > 0
    assert isolated.status_code == 200
    assert isolated.json()["citations"] == []

    reindexed = client.post(
        "/internal/knowledge/reindex",
        headers=ADMIN_SESSION,
        json={"category_id": CATEGORY},
    )
    assert reindexed.json()["reindexed_chunks"] >= 1
