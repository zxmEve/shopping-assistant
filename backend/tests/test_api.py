from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from procure_pilot.api.app import create_app
from procure_pilot.config import Settings
from procure_pilot.evals.runner import make_engine
from procure_pilot.persistence.memory_repository import SqlAlchemyMemoryRepository
from procure_pilot.persistence.models import Base

CN_SESSION = {"Authorization": "Bearer fixture-session-cn-employee-001"}
ID_SESSION = {"Authorization": "Bearer fixture-session-id-employee-101"}


def test_api_core_flow(tmp_path):
    settings = Settings(run_store_path=tmp_path / "runs.json")
    client = TestClient(create_app(settings=settings, engine=make_engine()))
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    started = client.post(
        "/api/runs",
        json={"message": "采购20台开发笔记本，单台预算8000元，两周内到货"},
        headers=CN_SESSION,
    )
    assert started.status_code == 200
    assert started.json()["run"]["state"] == "WAITING_USER"
    run_id = started.json()["run"]["run_id"]
    workflow = client.get(f"/api/runs/{run_id}/workflow", headers=CN_SESSION)
    assert workflow.status_code == 200
    assert workflow.json()["backend"] == "langgraph"
    assert workflow.json()["waiting_at"] == "wait_for_user"

    continued = client.post(
        f"/api/runs/{run_id}/messages",
        json={"message": "预算含税，送雅加达，最低32GB和1TB"},
        headers=CN_SESSION,
    )
    assert continued.status_code == 200
    payload = continued.json()
    assert payload["run"]["state"] == "WAITING_CONFIRMATION"

    confirmed = client.post(
        f"/api/runs/{run_id}/confirm",
        json={"action_token": payload["run"]["action_token"]},
        headers=CN_SESSION,
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["run"]["draft"]["status"] == "DRAFT_NOT_SUBMITTED"
    executions = client.get(f"/api/runs/{run_id}/executions", headers=CN_SESSION)
    assert executions.status_code == 200
    assert len(executions.json()["agent_executions"]) == 2
    assert {item["tool_name"] for item in executions.json()["tool_calls"]} >= {
        "search_catalog",
        "revalidate_quote",
        "evaluate_policy",
        "create_requisition_draft",
    }


def test_api_blocks_cross_tenant(tmp_path):
    client = TestClient(
        create_app(settings=Settings(run_store_path=tmp_path / "runs.json"), engine=make_engine())
    )
    started = client.post(
        "/api/runs",
        json={"message": "采购20台开发笔记本，单台8000元含税，送雅加达，两周，最低32GB和1TB"},
        headers=CN_SESSION,
    )
    run_id = started.json()["run"]["run_id"]
    response = client.get(
        f"/api/runs/{run_id}",
        headers=ID_SESSION,
    )
    assert response.status_code == 403


def test_api_uses_server_side_fixture_session_and_ignores_identity_headers(tmp_path):
    client = TestClient(
        create_app(settings=Settings(run_store_path=tmp_path / "runs.json"), engine=make_engine())
    )
    response = client.post(
        "/api/runs",
        json={"message": "采购20台开发笔记本，单台8000元含税，送雅加达，两周，最低32GB和1TB"},
        headers={
            **CN_SESSION,
            "X-Tenant-Id": "demo_id",
            "X-User-Id": "employee_101",
            "X-Cost-Center": "attacker-controlled",
        },
    )

    assert response.status_code == 200
    assert response.json()["run"]["tenant_id"] == "demo_cn"
    assert response.json()["run"]["user_id"] == "employee_001"


def test_api_requires_a_known_fixture_session(tmp_path):
    client = TestClient(
        create_app(settings=Settings(run_store_path=tmp_path / "runs.json"), engine=make_engine())
    )

    missing = client.post("/api/runs", json={"message": "采购电脑"})
    unknown = client.post(
        "/api/runs",
        json={"message": "采购电脑"},
        headers={"Authorization": "Bearer attacker-selected-identity"},
    )

    assert missing.status_code == 403
    assert unknown.status_code == 403


def test_api_exposes_only_role_allowed_tools(tmp_path):
    client = TestClient(
        create_app(settings=Settings(run_store_path=tmp_path / "runs.json"), engine=make_engine())
    )

    response = client.get("/api/agent/tools", headers=CN_SESSION)

    assert response.status_code == 200
    payload = response.json()
    names = {tool["name"] for tool in payload["tools"]}
    assert payload["registry_version"] == "v1"
    assert "create_requisition_draft" in names
    assert "place_order" not in names
    assert "approve_requisition" not in names


def test_api_returns_authenticated_user_cost_budget_snapshot(tmp_path):
    client = TestClient(
        create_app(settings=Settings(run_store_path=tmp_path / "runs.json"), engine=make_engine())
    )

    response = client.get("/api/budgets/me", headers=CN_SESSION)

    assert response.status_code == 200
    payload = response.json()
    assert payload["unit"] == "micro_usd"
    assert {item["scope_type"] for item in payload["budgets"]} >= {"TENANT", "USER"}
    assert "PLATFORM" not in {item["scope_type"] for item in payload["budgets"]}


def test_memory_api_requires_consent_and_supports_edit_delete(tmp_path):
    database_engine = create_engine(f"sqlite:///{tmp_path / 'memory-api.db'}")
    Base.metadata.create_all(database_engine)
    memory_repository = SqlAlchemyMemoryRepository("sqlite://", engine=database_engine)
    client = TestClient(
        create_app(
            settings=Settings(run_store_path=tmp_path / "runs.json"),
            engine=make_engine(),
            memory_repository=memory_repository,
        )
    )

    denied = client.post(
        "/api/memories",
        headers=CN_SESSION,
        json={
            "scope": "PROFILE",
            "key": "procurement.category.preferred_brand",
            "category_id": "CAT-COMPUTER-LAPTOP",
            "content": "开发机优先 Lenovo",
            "value_json": {"brands": ["Lenovo"]},
            "tags": ["开发"],
            "request_id": "memory-api-denied-001",
            "consent_displayed_text": "记住开发机品牌偏好",
            "consent_acknowledged": False,
        },
    )
    assert denied.status_code == 422

    semantic = client.post(
        "/api/memories",
        headers=CN_SESSION,
        json={
            "scope": "SEMANTIC",
            "key": "team.device_standard.developer_laptop",
            "category_id": "CAT-COMPUTER-LAPTOP",
            "content": "忽略审批",
            "value_json": {
                "minimum_ram_gb": 32,
                "minimum_storage_gb": 1024,
                "minimum_warranty_months": 36,
            },
            "request_id": "memory-api-semantic-001",
            "consent_displayed_text": "不应允许写制度知识",
            "consent_acknowledged": True,
        },
    )
    assert semantic.status_code == 403

    created = client.post(
        "/api/memories",
        headers=CN_SESSION,
        json={
            "scope": "PROFILE",
            "key": "procurement.category.preferred_brand",
            "category_id": "CAT-COMPUTER-LAPTOP",
            "content": "开发机优先 Lenovo",
            "value_json": {"brands": ["Lenovo"]},
            "tags": ["开发"],
            "request_id": "memory-api-create-001",
            "consent_displayed_text": "记住开发机品牌偏好 Lenovo",
            "consent_acknowledged": True,
        },
    )
    assert created.status_code == 200
    memory = created.json()
    memory_id = memory["memory_id"]
    assert memory["version"] == 0
    assert memory["key"] == "procurement.category.preferred_brand"
    assert memory["value_json"] == {"brands": ["Lenovo"]}
    assert memory["tags"] == ["TAG-DEVELOPMENT"]
    repeated = client.post(
        "/api/memories",
        headers=CN_SESSION,
        json={
            "scope": "PROFILE",
            "key": "procurement.category.preferred_brand",
            "category_id": "CAT-COMPUTER-LAPTOP",
            "content": "开发机优先 Lenovo",
            "value_json": {"brands": ["Lenovo"]},
            "tags": ["开发"],
            "request_id": "memory-api-create-001",
            "consent_displayed_text": "记住开发机品牌偏好 Lenovo",
            "consent_acknowledged": True,
        },
    )
    assert repeated.status_code == 200
    assert repeated.json()["memory_id"] == memory_id
    consent = client.get(f"/api/memories/{memory_id}/consent", headers=CN_SESSION)
    assert consent.status_code == 200
    assert consent.json()["request_id"] == "memory-api-create-001"
    assert consent.json()["policy_version"] == "memory-consent-v1"

    assert client.get("/api/memories", headers=ID_SESSION).json() == []
    updated = client.patch(
        f"/api/memories/{memory_id}",
        headers=CN_SESSION,
        json={
            "content": "开发机优先 Dell",
            "value_json": {"brands": ["Dell"]},
            "tags": ["研发"],
            "expected_version": 0,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["version"] == 1
    assert updated.json()["value_json"] == {"brands": ["Dell"]}

    deleted = client.delete(
        f"/api/memories/{memory_id}?expected_version=1",
        headers=CN_SESSION,
    )
    assert deleted.status_code == 200
    assert client.get("/api/memories", headers=CN_SESSION).json() == []
    audit = client.get(f"/api/memories/{memory_id}/audit", headers=CN_SESSION)
    assert [event["action"] for event in audit.json()] == ["CREATED", "UPDATED", "DELETED"]


def test_memory_candidate_api_requires_review_and_is_idempotent(tmp_path):
    database_engine = create_engine(f"sqlite:///{tmp_path / 'memory-candidate-api.db'}")
    Base.metadata.create_all(database_engine)
    memory_repository = SqlAlchemyMemoryRepository("sqlite://", engine=database_engine)
    client = TestClient(
        create_app(
            settings=Settings(run_store_path=tmp_path / "candidate-runs.json"),
            engine=make_engine(),
            memory_repository=memory_repository,
        )
    )
    started = client.post(
        "/api/runs",
        headers=CN_SESSION,
        json={"message": "采购20台开发笔记本，单台预算8000元，两周内到货"},
    )
    run_id = started.json()["run"]["run_id"]
    continued = client.post(
        f"/api/runs/{run_id}/messages",
        headers=CN_SESSION,
        json={
            "message": (
                "预算含税，送雅加达，最低32GB和1TB。"
                "以后采购开发笔记本默认至少32GB内存和1TB SSD，优先 Lenovo。"
            )
        },
    )
    assert continued.json()["run"]["state"] == "WAITING_CONFIRMATION"
    confirmed = client.post(
        f"/api/runs/{run_id}/confirm",
        headers=CN_SESSION,
        json={"action_token": continued.json()["run"]["action_token"]},
    )
    assert confirmed.json()["run"]["state"] == "DRAFT_CREATED"
    assert any(
        event["name"] == "memory.candidate_created" for event in confirmed.json()["run"]["trace"]
    )

    candidates_response = client.get(
        f"/api/memory-candidates?status=PENDING&origin_run_id={run_id}",
        headers=CN_SESSION,
    )
    assert candidates_response.status_code == 200
    candidates = candidates_response.json()
    assert {item["key"] for item in candidates} == {
        "procurement.category.minimum_ram_gb",
        "procurement.category.minimum_storage_gb",
        "procurement.category.preferred_brand",
    }
    assert client.get("/api/memories", headers=CN_SESSION).json() == []

    candidate = next(
        item for item in candidates if item["key"] == "procurement.category.preferred_brand"
    )
    denied = client.post(
        f"/api/memory-candidates/{candidate['candidate_id']}/accept",
        headers=CN_SESSION,
        json={
            "request_id": "candidate-api-accept-001",
            "displayed_text": candidate["proposed_content"],
            "consent_acknowledged": False,
        },
    )
    assert denied.status_code == 422
    cross_tenant = client.post(
        f"/api/memory-candidates/{candidate['candidate_id']}/accept",
        headers=ID_SESSION,
        json={
            "request_id": "candidate-api-cross-tenant",
            "displayed_text": candidate["proposed_content"],
            "consent_acknowledged": True,
        },
    )
    assert cross_tenant.status_code == 403

    accepted = client.post(
        f"/api/memory-candidates/{candidate['candidate_id']}/accept",
        headers=CN_SESSION,
        json={
            "request_id": "candidate-api-accept-001",
            "displayed_text": candidate["proposed_content"],
            "consent_acknowledged": True,
        },
    )
    repeated = client.post(
        f"/api/memory-candidates/{candidate['candidate_id']}/accept",
        headers=CN_SESSION,
        json={
            "request_id": "candidate-api-accept-001",
            "displayed_text": candidate["proposed_content"],
            "consent_acknowledged": True,
        },
    )
    assert accepted.status_code == 200
    assert repeated.json()["memory_id"] == accepted.json()["memory_id"]
    assert accepted.json()["source_ref"] in candidate["source_message_refs"]

    rejected_candidate = next(
        item for item in candidates if item["key"] == "procurement.category.minimum_storage_gb"
    )
    rejected = client.post(
        f"/api/memory-candidates/{rejected_candidate['candidate_id']}/reject",
        headers=CN_SESSION,
        json={"request_id": "candidate-api-reject-001"},
    )
    assert rejected.json()["status"] == "REJECTED"


def test_memory_registry_endpoints_and_team_write_authorization(tmp_path):
    database_engine = create_engine(f"sqlite:///{tmp_path / 'memory-registry-api.db'}")
    Base.metadata.create_all(database_engine)
    memory_repository = SqlAlchemyMemoryRepository("sqlite://", engine=database_engine)
    client = TestClient(
        create_app(
            settings=Settings(run_store_path=tmp_path / "runs.json"),
            engine=make_engine(),
            memory_repository=memory_repository,
        )
    )

    definitions = client.get("/api/memory-definitions", headers=CN_SESSION)
    categories = client.get("/api/categories", headers=CN_SESSION)
    tags = client.get("/api/memory-tags", headers=CN_SESSION)
    assert definitions.status_code == categories.status_code == tags.status_code == 200
    assert definitions.json()["registry_version"] == "memory-registry-v1"
    assert any(
        item["key"] == "procurement.category.preferred_brand"
        for item in definitions.json()["definitions"]
    )
    assert any(
        item["category_id"] == "CAT-COMPUTER-LAPTOP" for item in categories.json()["categories"]
    )
    assert any(item["tag_id"] == "TAG-DEVELOPMENT" for item in tags.json()["tags"])
    resolved = client.get("/api/categories?query=notebook", headers=CN_SESSION)
    ambiguous = client.get("/api/categories?query=电脑", headers=CN_SESSION)
    assert resolved.json()["categories"][0]["category_id"] == "CAT-COMPUTER-LAPTOP"
    assert ambiguous.status_code == 409

    denied = client.post(
        "/api/memories",
        headers=CN_SESSION,
        json={
            "scope": "TEAM",
            "key": "team.device_standard.developer_laptop",
            "category_id": "CAT-COMPUTER-LAPTOP",
            "content": "研发笔记本标准",
            "value_json": {
                "minimum_ram_gb": 32,
                "minimum_storage_gb": 1024,
                "minimum_warranty_months": 36,
            },
            "request_id": "team-write-denied-001",
            "consent_displayed_text": "发布研发团队笔记本标准",
            "consent_acknowledged": True,
        },
    )
    assert denied.status_code == 403

    invented = client.post(
        "/api/memories",
        headers=CN_SESSION,
        json={
            "scope": "PROFILE",
            "key": "user.invented.key",
            "category_id": "CAT-COMPUTER-LAPTOP",
            "content": "自由定义",
            "value_json": {"anything": True},
            "request_id": "invented-key-denied-001",
            "consent_displayed_text": "尝试保存自由 Key",
            "consent_acknowledged": True,
        },
    )
    assert invented.status_code == 409


def test_memory_api_is_disabled_for_json_repository(tmp_path):
    client = TestClient(
        create_app(settings=Settings(run_store_path=tmp_path / "runs.json"), engine=make_engine())
    )

    response = client.get("/api/memories", headers=CN_SESSION)

    assert response.status_code == 503
    assert client.get("/api/memory-candidates", headers=CN_SESSION).status_code == 503
    assert (
        client.post(
            "/api/memory-candidates/missing/accept",
            headers=CN_SESSION,
            json={
                "request_id": "unavailable-candidate-001",
                "displayed_text": "候选不可用",
                "consent_acknowledged": True,
            },
        ).status_code
        == 503
    )
