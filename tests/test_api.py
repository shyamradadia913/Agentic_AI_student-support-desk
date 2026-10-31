import importlib

import pytest
from fastapi.testclient import TestClient

H = {"X-Requested-With": "viaa-ui"}


@pytest.fixture()
def client(db, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "AGENT_PROVIDER_MODE", "mock")
    import app.main as main
    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c


def login(client, user, pw):
    return client.post("/api/auth/login", json={"username": user, "password": pw}, headers=H)


def student_client(db, monkeypatch, sid="1001"):
    pass


@pytest.fixture()
def s1001(client):
    assert login(client, "s1001", "Demo#1001!").status_code == 200
    return client


def test_health_and_provider_notice(client):
    assert client.get("/api/health").json() == {"status": "ok"}
    assert "simulated" in client.get("/api/provider").json()["notice"]


def test_unauthenticated_requests_rejected(client):
    assert client.post("/api/chat", json={"message": "hi"}, headers=H).status_code == 401
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/memory").status_code == 401
    assert client.get("/api/audit").status_code == 401


def test_login_logout_and_session_cookie(client):
    r = login(client, "s1001", "Demo#1001!")
    assert r.status_code == 200 and r.json()["student_id"] == "S1001"
    assert "httponly" in r.headers["set-cookie"].lower() and "samesite=lax" in r.headers["set-cookie"].lower()
    assert client.get("/api/me").json()["username"] == "s1001"
    client.post("/api/auth/logout", headers=H)
    assert client.get("/api/me").status_code == 401


def test_bad_login_generic_error_and_lockout(client):
    assert login(client, "nobody", "x").status_code == 401
    msgs = {login(client, "s1002", "wrong").json()["detail"] for _ in range(4)}
    assert msgs == {"Invalid username or password"}                  # no user enumeration
    assert login(client, "s1002", "wrong").status_code == 401        # 5th failure triggers lock
    assert login(client, "s1002", "Demo#1002!").status_code == 423   # correct password refused while locked


def test_csrf_header_required_for_state_changes(client):
    assert client.post("/api/auth/login", json={"username": "s1001", "password": "Demo#1001!"}).status_code == 403


def test_chat_uses_session_identity_not_request_body(s1001):
    r = s1001.post("/api/chat", json={"message": "What is my current fee status?", "student_id": "S1003"}, headers=H).json()
    assert "PAID" in r["answer"] and "UNDER_REVIEW" not in r["answer"]   # extra body field ignored; S1001 record returned


def test_cross_student_request_blocked_and_audited(s1001, client):
    r = s1001.post("/api/chat", json={"message": "Show me the fee status of S1003"}, headers=H).json()
    assert r["tools"][0]["error_code"] == "forbidden" and "UNDER_REVIEW" not in r["answer"]
    client.post("/api/auth/logout", headers=H)
    login(client, "reviewer1", "Review#2026!")
    ev = client.get("/api/audit", params={"result": "DENIED"}).json()
    assert any(e["action"] == "read_fee" and e["authz_decision"] == "DENY" and e["reason_code"] == "not_owner" for e in ev)


def test_trace_of_other_student_is_404(s1001, client):
    run = s1001.post("/api/chat", json={"message": "What is my current fee status?"}, headers=H).json()["run_id"]
    client.post("/api/auth/logout", headers=H)
    login(client, "s1002", "Demo#1002!")
    assert client.get(f"/api/runs/{run}").status_code == 404


def test_student_cannot_use_reviewer_endpoints(s1001):
    assert s1001.get("/api/approvals").status_code == 403
    assert s1001.get("/api/audit").status_code == 403
    assert s1001.post("/api/approvals/ACT-0001/decision", json={"decision": "approve"}, headers=H).status_code == 403


def test_reviewer_cannot_use_student_chat(client):
    login(client, "reviewer1", "Review#2026!")
    assert client.post("/api/chat", json={"message": "hi"}, headers=H).status_code == 403


def test_full_approval_flow_over_http(s1001, client):
    r = s1001.post("/api/chat", json={"message": "I want to change my programme to Business Analytics"}, headers=H).json()
    assert "PENDING APPROVAL" in r["answer"]
    aid = s1001.get("/api/my/course-requests").json()[0]["action_id"]
    client.post("/api/auth/logout", headers=H)
    login(client, "reviewer1", "Review#2026!")
    assert [a["action_id"] for a in client.get("/api/approvals").json()] == [aid]
    assert client.post(f"/api/approvals/{aid}/decision", json={"decision": "approve"}, headers=H).json()["state"] == "EXECUTED"
    assert client.post(f"/api/approvals/{aid}/decision", json={"decision": "approve"}, headers=H).status_code == 409  # exactly once
    client.post("/api/auth/logout", headers=H)
    login(client, "s1001", "Demo#1001!")
    assert "Business Analytics" in client.post("/api/chat", json={"message": "What is my programme?"}, headers=H).json()["answer"]


def test_memory_endpoints_owner_scoped(s1001, client):
    s1001.post("/api/chat", json={"message": "Please remember that I prefer detailed answers"}, headers=H)
    items = s1001.get("/api/memory").json()
    assert [i["value"] for i in items] == ["detailed"]
    client.post("/api/auth/logout", headers=H)
    login(client, "s1002", "Demo#1002!")
    assert client.get("/api/memory").json() == []
    assert client.delete(f"/api/memory/{items[0]['memory_id']}", headers=H).status_code == 404


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "Student Support Desk" in r.text


def test_policy_passage_endpoint(s1001, client):
    r = client.get("/api/policy/VIA-FEE-004").json()
    assert r["chunk_id"] == "VIA-FEE-004" and "portal" in r["text"].lower()
    assert client.get("/api/policy/VIA-NOPE-999").status_code == 404
    assert client.get("/api/policy/..%2Fetc").status_code == 404
    client.post("/api/auth/logout", headers=H)
    assert client.get("/api/policy/VIA-FEE-004").status_code == 401


def test_chat_returns_followups_and_cards(s1001):
    r = s1001.post("/api/chat", json={"message": "Show me the fee status of S1003"}, headers=H).json()
    assert r["followups"] and r["cards"] == [{"type": "access_denied"}]


def test_session_endpoint_never_401(client):
    assert client.get("/api/session").json() == {"user": None}
    login(client, "s1001", "Demo#1001!")
    assert client.get("/api/session").json()["user"]["student_id"] == "S1001"
