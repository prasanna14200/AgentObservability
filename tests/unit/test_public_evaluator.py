from __future__ import annotations

from fastapi.testclient import TestClient

from agentsentinel.approval_service import create_app

APPROVER_SECRET = "public-evaluator-test-secret-00000001"


def evaluator_client(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTSENTINEL_PUBLIC_DEMO", "true")
    monkeypatch.setenv("AGENTSENTINEL_SECURE_COOKIES", "true")
    monkeypatch.setenv("APPROVER_SECRET", APPROVER_SECRET)
    for name in (
        "PAYPAL_ENVIRONMENT",
        "PAYPAL_CLIENT_ID",
        "PAYPAL_CLIENT_SECRET",
        "PAYPAL_MERCHANT_ID",
        "PAYPAL_ACCESS_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    app = create_app(
        db_path=tmp_path / "approval.sqlite",
        evaluator_db_path=tmp_path / "evaluator.sqlite",
        telemetry_db_path=tmp_path / "telemetry.sqlite",
        approver_secret=APPROVER_SECRET,
    )
    return TestClient(app)


def run_scenario(client: TestClient, scenario: str):
    return client.post("/evaluator/executions", json={"scenario": scenario})


def test_normal_lookup_records_allowed_tool_execution_and_audit(tmp_path, monkeypatch):
    client = evaluator_client(tmp_path, monkeypatch)

    response = run_scenario(client, "normal_lookup")

    assert response.status_code == 201
    evidence = response.json()
    request_id = evidence["request_id"]
    assert evidence["scenario"] == "normal_lookup"
    assert evidence["started_at"] < evidence["completed_at"]
    assert evidence["status"] == "COMPLETED"
    assert evidence["policy_decision"]["decision"] == "ALLOW"
    assert [call["tool_name"] for call in evidence["tool_calls"]] == [
        "search_customer",
        "get_order",
    ]
    assert all(call["status"] == "success" for call in evidence["tool_calls"])
    assert evidence["tool_calls"][1]["outcome"]["status"] == "SHIPPED"
    assert evidence["trace"]["trace_id"] == request_id
    assert all(event["trace_id"] == request_id for event in evidence["trace"]["events"])
    assert all(call["request_id"] == request_id for call in evidence["tool_calls"])
    assert all(event["request_id"] == request_id for event in evidence["audit_events"])
    assert evidence["detection"]["anomalous"] is False
    assert evidence["audit_events"][-1]["event_type"] == "EXECUTION_COMPLETED"


def test_repeated_calls_run_existing_loop_detection_rules(tmp_path, monkeypatch):
    client = evaluator_client(tmp_path, monkeypatch)

    response = run_scenario(client, "repeated_calls")

    assert response.status_code == 201
    evidence = response.json()
    assert evidence["detection"]["engine"] == "agentsentinel.detectors.rules.apply_rules"
    assert evidence["detection"]["anomalous"] is True
    assert "TOOL_LOOP_DETECTED" in evidence["detection"]["reason_codes"]
    assert evidence["detection"]["features"]["tool_repeat_in_session_max"] == 3
    assert evidence["policy_decision"]["decision"] == "REVIEW"
    assert evidence["status"] == "REVIEW_REQUIRED"


def test_unauthorized_update_is_blocked_before_tool_execution(tmp_path, monkeypatch):
    client = evaluator_client(tmp_path, monkeypatch)

    response = run_scenario(client, "unauthorized_update")

    assert response.status_code == 201
    evidence = response.json()
    update = evidence["tool_calls"][-1]
    assert update["tool_name"] == "update_order"
    assert update["status"] == "error"
    assert update["outcome"] is None
    assert update["reason_code"] == "TOOL_PERMISSION_DENIED"
    assert evidence["policy_decision"]["decision"] == "BLOCK"
    assert evidence["status"] == "BLOCKED"
    assert "orders:write" in evidence["policy_decision"]["explanation"]


def test_execution_detail_and_recent_list_read_persisted_evidence(tmp_path, monkeypatch):
    client = evaluator_client(tmp_path, monkeypatch)
    created = run_scenario(client, "normal_lookup").json()

    retrieved = client.get(f"/evaluator/executions/{created['request_id']}")
    recent = client.get("/evaluator/executions")

    assert retrieved.status_code == 200
    assert retrieved.json()["request_id"] == created["request_id"]
    assert retrieved.json()["tool_calls"] == created["tool_calls"]
    assert retrieved.json()["audit_events"] == created["audit_events"]
    assert recent.status_code == 200
    assert recent.json()[0]["request_id"] == created["request_id"]
    assert recent.json()[0]["decision"] == "ALLOW"
    assert client.get("/evaluator/executions/request_not-a-real-id").status_code == 404


def test_public_evaluator_rejects_arbitrary_scenarios_and_operations(tmp_path, monkeypatch):
    client = evaluator_client(tmp_path, monkeypatch)

    assert run_scenario(client, "capture_order").status_code == 422
    assert client.post(
        "/evaluator/executions",
        json={"scenario": "normal_lookup", "tool_name": "arbitrary_operation"},
    ).status_code == 422
    assert client.post(
        "/evaluator/executions",
        json={"scenario": "normal_lookup", "payment_action": "capture"},
    ).status_code == 422
    assert client.post("/evaluator/executions", content=b"x" * 2048,
                       headers={"content-type": "application/json"}).status_code == 413
    assert client.post(
        "/evaluator/executions",
        content=b"\xff",
        headers={"content-type": "application/json"},
    ).status_code == 422
    assert client.post(
        "/evaluator/executions",
        content=b'{"scenario":"normal_lookup"}',
        headers={"content-type": "text/plain"},
    ).status_code == 415
    assert client.get("/evaluator/executions").json() == []


def test_anonymous_evaluator_cannot_access_operator_actions_or_payment_apis(
    tmp_path, monkeypatch
):
    client = evaluator_client(tmp_path, monkeypatch)

    assert client.get("/approvals").status_code == 401
    assert client.get("/killswitch").status_code == 401
    assert client.get("/dashboard").status_code == 401
    assert client.get("/dashboard/traces").status_code == 401
    assert client.post("/killswitch", json={"enabled": False}).status_code == 403
    assert client.post("/approvals/unknown/approve").status_code == 403
    assert client.post("/dashboard/approvals/unknown/approve").status_code == 403


def test_scenario_rate_limit_is_bounded(tmp_path, monkeypatch):
    client = evaluator_client(tmp_path, monkeypatch)

    for _ in range(10):
        assert run_scenario(client, "normal_lookup").status_code == 201

    limited = run_scenario(client, "normal_lookup")
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) > 0
