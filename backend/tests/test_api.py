from fastapi.testclient import TestClient

from procure_pilot.api.app import create_app
from procure_pilot.config import Settings
from procure_pilot.evals.runner import make_engine

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
