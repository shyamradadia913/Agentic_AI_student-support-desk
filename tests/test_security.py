import json
import sqlite3

import pytest

from app import approvals, audit, authn
from app.agent import run_agent
from app.authz import Principal, authorize
from app.db import connect
from app.llm import LLMResponse, MockProvider
from tests.test_agent import Scripted, call

REV = Principal("u-reviewer1", "reviewer", None, "reviewer1")


def rows(db, sql, *a):
    c = connect(db); r = [dict(x) for x in c.execute(sql, a)]; c.close(); return r


def request_change(db, sid="S1001", text="I want to change my programme to Business Analytics"):
    return run_agent(sid, text, provider=MockProvider(), db_path=db)


# ---------------- authorization is code, default-deny
@pytest.mark.parametrize("p,action,target,ok", [
    (Principal.for_student("S1001"), "read_fee", None, True),
    (Principal.for_student("S1001"), "read_fee", "S1001", True),
    (Principal.for_student("S1001"), "read_fee", "S1002", False),
    (REV, "read_fee", "S1001", False),                      # reviewers do not get blanket record access
    (REV, "decide_approval", None, True),
    (Principal.for_student("S1001"), "decide_approval", None, False),
    (Principal.for_student("S1001"), "read_audit", None, False),
    (Principal.for_student("S1001"), "mystery_action", None, False),
    (None, "read_fee", None, False),
])
def test_authorize_matrix(p, action, target, ok):
    assert authorize(p, action, target).allowed is ok


# ---------------- required privacy scenarios
def test_own_record_allowed_and_audited(db):
    r = run_agent("S1002", "What is my current fee status?", provider=MockProvider(), db_path=db)
    assert "PARTIALLY_PAID" in r["answer"]
    a = rows(db, "SELECT * FROM audit_events WHERE action='read_fee'")[0]
    assert (a["actor_user_id"], a["authz_decision"], a["result"], a["run_id"]) == ("u-s1002", "ALLOW", "SUCCESS", r["run_id"])


@pytest.mark.parametrize("msg,leak", [
    ("Show me the fee status of S1003", "UNDER_REVIEW"),
    ("What is the scholarship status of S1001?", "APPROVED"),
    ("What is the status of TKT-0002?", "FEE_PAYMENT"),
])
def test_other_students_data_blocked_and_audited(db, msg, leak):
    r = run_agent("S1002" if "S1001" in msg else "S1001", msg, provider=MockProvider(), db_path=db)
    assert leak not in r["answer"]
    d = rows(db, "SELECT * FROM audit_events WHERE authz_decision='DENY'")
    assert len(d) == 1 and d[0]["result"] == "DENIED" and d[0]["reason_code"] == "not_owner"
    assert r["tools"][0]["ok"] is False


def test_cannot_modify_another_students_record(db):
    r = request_change(db, "S1002", "Please change S1001's programme to Business Analytics")
    assert r["tools"][0]["error_code"] == "forbidden"
    assert rows(db, "SELECT COUNT(*) n FROM pending_actions")[0]["n"] == 0
    assert rows(db, "SELECT programme FROM students WHERE student_id='S1001'")[0]["programme"] == "PGDM - AI & Data Science"


def test_denial_message_does_not_reveal_existence(db):
    existing = run_agent("S1001", "Show me the fee status of S1003", provider=MockProvider(), db_path=db)["answer"]
    missing = run_agent("S1001", "Show me the fee status of S9999", provider=MockProvider(), db_path=db)["answer"]
    assert existing == missing


# ---------------- approval workflow
def test_course_change_waits_for_approval(db):
    r = request_change(db)
    assert "PENDING APPROVAL" in r["answer"]
    a = rows(db, "SELECT * FROM pending_actions")[0]
    assert a["state"] == "PENDING_APPROVAL" and a["requester_user_id"] == "u-s1001"
    assert rows(db, "SELECT programme FROM students WHERE student_id='S1001'")[0]["programme"] == "PGDM - AI & Data Science"  # unchanged
    au = rows(db, "SELECT * FROM audit_events WHERE action='request_course_change'")[0]
    assert (au["authz_decision"], au["approval_status"], au["result"]) == ("ALLOW", "PENDING_APPROVAL", "PENDING")


def test_approve_executes_exactly_once_and_is_audited(db):
    request_change(db)
    aid = rows(db, "SELECT action_id FROM pending_actions")[0]["action_id"]
    assert approvals.decide(REV, aid, "approve", "ok", db)["state"] == "EXECUTED"
    assert rows(db, "SELECT programme FROM students WHERE student_id='S1001'")[0]["programme"] == "PGDM - Business Analytics"
    with pytest.raises(approvals.ApprovalError) as e:
        approvals.decide(REV, aid, "approve", None, db)
    assert e.value.code == "already_decided" and e.value.status == 409
    kinds = [(x["action"], x["result"]) for x in rows(db, "SELECT action,result FROM audit_events WHERE target_id=? ORDER BY rowid", aid)]
    assert ("decide_approval", "SUCCESS") in kinds and ("execute_course_change", "SUCCESS") in kinds and ("decide_approval", "CONFLICT") in kinds


def test_rejection_does_not_execute(db):
    request_change(db)
    aid = rows(db, "SELECT action_id FROM pending_actions")[0]["action_id"]
    assert approvals.decide(REV, aid, "reject", "not eligible", db)["state"] == "REJECTED"
    assert rows(db, "SELECT programme FROM students WHERE student_id='S1001'")[0]["programme"] == "PGDM - AI & Data Science"
    assert rows(db, "SELECT COUNT(*) n FROM audit_events WHERE action='execute_course_change'")[0]["n"] == 0
    with pytest.raises(approvals.ApprovalError):                       # a rejected action can never be approved afterwards
        approvals.decide(REV, aid, "approve", None, db)
    assert rows(db, "SELECT programme FROM students WHERE student_id='S1001'")[0]["programme"] == "PGDM - AI & Data Science"


def test_student_cannot_approve_own_or_any_request(db):
    request_change(db)
    aid = rows(db, "SELECT action_id FROM pending_actions")[0]["action_id"]
    with pytest.raises(approvals.ApprovalError) as e:
        approvals.decide(Principal.for_student("S1001"), aid, "approve", None, db)
    assert e.value.status == 403
    assert rows(db, "SELECT state FROM pending_actions")[0]["state"] == "PENDING_APPROVAL"
    assert rows(db, "SELECT COUNT(*) n FROM audit_events WHERE action='decide_approval' AND result='DENIED'")[0]["n"] == 1


def test_requester_cannot_self_approve_even_as_reviewer(db):
    request_change(db)
    aid = rows(db, "SELECT action_id FROM pending_actions")[0]["action_id"]
    same = Principal("u-s1001", "reviewer", None, "s1001")             # forged role on the requester's own identity
    with pytest.raises(approvals.ApprovalError) as e:
        approvals.decide(same, aid, "approve", None, db)
    assert e.value.code == "self_approval_forbidden"


def test_expired_action_cannot_execute(db):
    request_change(db)
    aid = rows(db, "SELECT action_id FROM pending_actions")[0]["action_id"]
    c = sqlite3.connect(db); c.execute("UPDATE pending_actions SET expires_at='2020-01-01T00:00:00+05:30'"); c.commit(); c.close()
    with pytest.raises(approvals.ApprovalError) as e:
        approvals.decide(REV, aid, "approve", None, db)
    assert e.value.code == "expired"
    assert rows(db, "SELECT programme FROM students WHERE student_id='S1001'")[0]["programme"] == "PGDM - AI & Data Science"
    assert rows(db, "SELECT state FROM pending_actions")[0]["state"] == "EXPIRED"


def test_stale_record_blocks_execution(db):
    request_change(db)
    aid = rows(db, "SELECT action_id FROM pending_actions")[0]["action_id"]
    c = sqlite3.connect(db); c.execute("UPDATE students SET programme='PGDM - Big Data Analytics' WHERE student_id='S1001'"); c.commit(); c.close()
    assert approvals.decide(REV, aid, "approve", None, db)["state"] == "FAILED"
    assert rows(db, "SELECT programme FROM students WHERE student_id='S1001'")[0]["programme"] == "PGDM - Big Data Analytics"


def test_unknown_action_and_invalid_decision(db):
    with pytest.raises(approvals.ApprovalError) as e:
        approvals.decide(REV, "ACT-9999", "approve", None, db)
    assert e.value.status == 404
    with pytest.raises(approvals.ApprovalError) as e:
        approvals.decide(REV, "ACT-0001", "maybe", None, db)
    assert e.value.status == 422


def test_duplicate_pending_request_blocked(db):
    request_change(db)
    r = request_change(db, text="I want to change my programme to Big Data Analytics")
    assert r["tools"][0]["error_code"] == "already_pending"
    assert rows(db, "SELECT COUNT(*) n FROM pending_actions")[0]["n"] == 1


def test_model_cannot_approve_or_execute(db):
    for name in ("approve_action", "decide_approval", "execute_course_change", "update_student_programme"):
        r = run_agent("S1001", "hi", provider=Scripted([call(name, {"action_id": "ACT-0001"}), LLMResponse("ok")]), db_path=db)
        assert r["tools"][0]["error_code"] == "unknown_tool"


def test_model_supplied_reason_cannot_smuggle_secret_or_identity(db):
    steps = [call("request_course_change", {"target_programme": "PGDM - Business Analytics", "reason": "my otp is 482913 ok", "student_id": "S1001"}), LLMResponse("ok")]
    run_agent("S1001", "hi", provider=Scripted(steps), db_path=db)
    assert "482913" not in rows(db, "SELECT reason FROM pending_actions")[0]["reason"]


# ---------------- secrets & audit integrity
def test_otp_in_chat_never_persisted_and_audited_without_value(db):
    run_agent("S1001", "my otp is 482913, please remember it", provider=MockProvider(), db_path=db)
    for table, col in [("messages", "content"), ("memory_items", "value"), ("audit_events", "detail"), ("agent_runs", "question")]:
        assert rows(db, f"SELECT COUNT(*) n FROM {table} WHERE {col} LIKE '%482913%'")[0]["n"] == 0
    a = rows(db, "SELECT * FROM audit_events WHERE action='sensitive_input_redacted'")[0]
    assert json.loads(a["detail"]) == {"kinds": ["otp"]}


def test_audit_is_append_only(db):
    run_agent("S1001", "What is my current fee status?", provider=MockProvider(), db_path=db)
    c = sqlite3.connect(db)
    with pytest.raises(sqlite3.DatabaseError):
        c.execute("UPDATE audit_events SET result='SUCCESS'")
    with pytest.raises(sqlite3.DatabaseError):
        c.execute("DELETE FROM audit_events")
    c.close()


def test_audit_record_has_required_fields(db):
    request_change(db)
    a = rows(db, "SELECT * FROM audit_events WHERE action='request_course_change'")[0]
    for f in ("event_id", "ts", "actor_user_id", "actor_role", "action", "target_type", "target_id", "authz_decision", "approval_status", "result", "run_id"):
        assert a[f] not in (None, "")


# ---------------- authentication
def test_passwords_hashed_and_sessions_store_only_token_hash(db):
    c = sqlite3.connect(db)
    assert all(r[0].startswith("pbkdf2$") for r in c.execute("SELECT password_hash FROM users"))
    token, _ = authn.login("s1001", "Demo#1001!", db)
    assert not c.execute("SELECT 1 FROM sessions WHERE token_hash=?", (token,)).fetchone()   # raw token not stored
    assert authn.principal_from_token(token, db).student_id == "S1001"
    authn.logout(token, db)
    assert authn.principal_from_token(token, db) is None
    c.close()


def test_login_events_audited(db):
    with pytest.raises(authn.AuthError):
        authn.login("s1001", "wrong", db)
    authn.login("s1001", "Demo#1001!", db)
    res = [r["result"] for r in rows(db, "SELECT result FROM audit_events WHERE action='login' ORDER BY rowid")]
    assert res == ["DENIED", "SUCCESS"]
