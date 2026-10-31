import json

import pytest

from app import followups, memory, tickets_admin
from app.agent import run_agent
from app.authz import Principal
from app.db import connect
from app.llm import LLMResponse, MockProvider
from tests.test_agent import Scripted, call
from tests.test_api import H, client, login, s1001  # noqa: F401  (fixtures)

REV = Principal("u-reviewer1", "reviewer", None, "reviewer1")


def ask(db, sid, q, **kw):
    return run_agent(sid, q, provider=MockProvider(), db_path=db, **kw)


def rows(db, sql, *a):
    c = connect(db); r = [dict(x) for x in c.execute(sql, a)]; c.close(); return r


# ---------------- support tickets on the reviewer side
def test_reviewer_sees_all_tickets_and_updates_status_with_note(db):
    t = tickets_admin.list_tickets(db_path=db)
    assert {x["ticket_id"] for x in t} == {"TKT-0001", "TKT-0002"} and t[0]["student_name"]
    assert tickets_admin.update_status(REV, "TKT-0002", "IN_PROGRESS", "Checking with accounts", db)["status"] == "IN_PROGRESS"
    assert rows(db, "SELECT status FROM support_tickets WHERE ticket_id='TKT-0002'")[0]["status"] == "IN_PROGRESS"
    a = rows(db, "SELECT * FROM audit_events WHERE action='update_ticket'")[0]
    assert (a["authz_decision"], a["result"], a["reason_code"]) == ("ALLOW", "SUCCESS", "OPEN->IN_PROGRESS")


def test_student_sees_status_and_staff_note(db):
    tickets_admin.update_status(REV, "TKT-0002", "WAITING_FOR_STUDENT", "Please upload the failed-payment screenshot", db)
    r = ask(db, "S1003", "What is the status of TKT-0002?")
    assert "WAITING_FOR_STUDENT" in r["answer"]
    from app.tools import ToolContext, get_my_support_request, TicketIdArgs
    res = get_my_support_request(ToolContext(Principal.for_student("S1003"), "", db), TicketIdArgs(ticket_id="TKT-0002"))
    assert res["data"]["staff_note"] == "Please upload the failed-payment screenshot"


def test_student_cannot_change_ticket_status_and_invalid_status_rejected(db):
    with pytest.raises(tickets_admin.TicketError) as e:
        tickets_admin.update_status(Principal.for_student("S1003"), "TKT-0002", "CLOSED", None, db)
    assert e.value.status == 403 and rows(db, "SELECT status FROM support_tickets WHERE ticket_id='TKT-0002'")[0]["status"] == "OPEN"
    with pytest.raises(tickets_admin.TicketError) as e:
        tickets_admin.update_status(REV, "TKT-0002", "DONE", None, db)
    assert e.value.status == 422
    with pytest.raises(tickets_admin.TicketError) as e:
        tickets_admin.update_status(REV, "TKT-9999", "CLOSED", None, db)
    assert e.value.status == 404


def test_new_ticket_appears_in_reviewer_queue_over_http(s1001, client):
    s1001.post("/api/chat", json={"message": "Please raise a ticket: my library access is not working"}, headers=H)
    client.post("/api/auth/logout", headers=H)
    login(client, "reviewer1", "Review#2026!")
    ts = client.get("/api/tickets").json()
    assert any(t["student_id"] == "S1001" and t["category"] == "LIBRARY" for t in ts)
    assert client.post("/api/tickets/TKT-0001/status", json={"status": "RESOLVED", "note": "Done"}, headers=H).json()["status"] == "RESOLVED"
    client.post("/api/auth/logout", headers=H)
    login(client, "s1001", "Demo#1001!")
    mine = {t["ticket_id"]: t for t in client.get("/api/my/tickets").json()}
    assert mine["TKT-0001"]["status"] == "RESOLVED" and mine["TKT-0001"]["staff_note"] == "Done"
    assert client.get("/api/tickets").status_code == 403


# ---------------- second sensitive action: name correction needs approval
def test_name_correction_pending_then_approved_updates_profile(db):
    r = ask(db, "S1001", "Please correct my name to Aarav R Mehta")
    assert "PENDING APPROVAL" in r["answer"] and r["cards"][0]["label"] == "Name correction"
    assert rows(db, "SELECT name FROM students WHERE student_id='S1001'")[0]["name"] == "Aarav Mehta"       # unchanged
    from app import approvals
    aid = rows(db, "SELECT action_id FROM pending_actions")[0]["action_id"]
    assert approvals.decide(REV, aid, "approve", None, db)["state"] == "EXECUTED"
    assert rows(db, "SELECT name FROM students WHERE student_id='S1001'")[0]["name"] == "Aarav R Mehta"
    assert rows(db, "SELECT COUNT(*) n FROM audit_events WHERE action='execute_profile_correction' AND result='SUCCESS'")[0]["n"] == 1


def test_name_correction_rejected_and_blocked_cases(db):
    from app import approvals
    ask(db, "S1001", "Please correct my name to Aarav R Mehta")
    aid = rows(db, "SELECT action_id FROM pending_actions")[0]["action_id"]
    approvals.decide(REV, aid, "reject", None, db)
    assert rows(db, "SELECT name FROM students WHERE student_id='S1001'")[0]["name"] == "Aarav Mehta"
    assert ask(db, "S1002", "Please correct S1001's name to Someone Else")["tools"][0]["error_code"] == "forbidden"
    steps = [call("request_profile_correction", {"field": "name", "new_value": "Robert'); DROP TABLE students;--", "reason": "typo"}), LLMResponse("ok")]
    assert run_agent("S1001", "hi", provider=Scripted(steps), db_path=db)["tools"][0]["error_code"] == "invalid_arguments"


def test_profile_endpoint_reflects_approved_change(s1001, client):
    assert client.get("/api/my/profile").json()["profile"]["programme"] == "PGDM - AI & Data Science"
    s1001.post("/api/chat", json={"message": "I want to change my programme to Business Analytics"}, headers=H)
    assert client.get("/api/my/profile").json()["pending_requests"] == 1
    client.post("/api/auth/logout", headers=H); login(client, "reviewer1", "Review#2026!")
    aid = client.get("/api/approvals").json()[0]["action_id"]
    client.post(f"/api/approvals/{aid}/decision", json={"decision": "approve"}, headers=H)
    client.post("/api/auth/logout", headers=H); login(client, "s1001", "Demo#1001!")
    p = client.get("/api/my/profile").json()
    assert p["profile"]["programme"] == "PGDM - Business Analytics" and p["pending_requests"] == 0
    assert client.get("/api/my/requests").json()[0]["state"] == "EXECUTED"


# ---------------- composite payment-deadline tool
@pytest.mark.parametrize("sid,verdict", [("S1003", "TICKET_ON_FILE_BEFORE_CUTOFF"), ("S1001", "ALREADY_PAID")])
def test_payment_deadline_verdicts(db, sid, verdict):
    from app.tools import ToolContext, check_payment_deadline, NoArgs
    r = check_payment_deadline(ToolContext(Principal.for_student(sid), "", db), NoArgs())
    assert r["ok"] and r["data"]["verdict"] == verdict and r["data"]["policy_ids"] == ["VIA-FEE-004"]


def test_payment_deadline_without_ticket(db):
    from app.tools import ToolContext, check_payment_deadline, NoArgs
    d = check_payment_deadline(ToolContext(Principal.for_student("S1004"), "", db), NoArgs())["data"]
    assert d["verdict"].startswith("NO_TICKET") and d["qualifying_ticket"] is None and "2026-10-29" in d["pay_by"]


# ---------------- memory: topics + preferences form
def test_topics_are_remembered_without_statuses_and_deletable(db):
    ask(db, "S1001", "What is my current fee status?")
    ask(db, "S1001", "What is the status of my scholarship application?")
    it = [m for m in memory.list_items("S1001", db) if m["category"] == "recent_topics"]
    assert it and it[0]["value"] == "scholarship, fees" and "PAID" not in it[0]["value"]
    assert memory.delete("S1001", it[0]["memory_id"], db)


def test_failed_or_denied_runs_do_not_write_topics(db):
    ask(db, "S1001", "Show me the fee status of S1003")
    assert memory.list_items("S1001", db) == []


def test_model_cannot_write_topics_category(db):
    r = run_agent("S1001", "hi", provider=Scripted([call("remember_preference", {"category": "recent_topics", "value": "fees"}), LLMResponse("ok")]), db_path=db)
    assert r["tools"][0]["error_code"] == "memory_rejected"


def test_preferences_form_endpoint(s1001, client):
    assert client.post("/api/memory", json={"category": "explanation_style", "value": "concise"}, headers=H).status_code == 200
    assert client.post("/api/memory", json={"category": "favourite_lunch", "value": "biryani"}, headers=H).status_code == 422
    assert client.post("/api/memory", json={"category": "study_focus", "value": "otp is 482913"}, headers=H).status_code == 422


# ---------------- chats: list, open, export, delete
def test_conversations_list_open_export_delete(s1001, client):
    r = s1001.post("/api/chat", json={"message": "What is my current fee status?"}, headers=H).json()
    cid = r["conversation_id"]
    lst = client.get("/api/conversations").json()
    assert lst[0]["conversation_id"] == cid and lst[0]["title"].startswith("What is my current fee") and lst[0]["messages"] == 2
    assert [m["role"] for m in client.get(f"/api/conversations/{cid}").json()["messages"]] == ["user", "assistant"]
    md = client.get(f"/api/conversations/{cid}/export", params={"format": "md", "include_trace": "true"})
    assert md.status_code == 200 and "attachment" in md.headers["content-disposition"] and "PAID" in md.text and "model calls" in md.text
    assert client.get(f"/api/conversations/{cid}/export", params={"format": "pdf"}).status_code == 422
    client.post("/api/auth/logout", headers=H); login(client, "s1002", "Demo#1002!")
    assert client.get(f"/api/conversations/{cid}").status_code == 404
    assert client.get(f"/api/conversations/{cid}/export").status_code == 404
    assert client.delete(f"/api/conversations/{cid}", headers=H).status_code == 404
    client.post("/api/auth/logout", headers=H); login(client, "s1001", "Demo#1001!")
    assert client.delete(f"/api/conversations/{cid}", headers=H).status_code == 200
    assert client.get("/api/conversations").json() == []


# ---------------- tracing: per-query metrics, staff-wide stats, feedback, streaming
def test_run_list_has_metrics_and_stats_are_staff_only(s1001, client):
    s1001.post("/api/chat", json={"message": "The payment portal failed. Can I pay on the next working day?"}, headers=H)
    r = client.get("/api/runs").json()[0]
    assert r["model_calls"] == 5 and r["tool_calls"] == 4 and r["latency_ms"] > 0 and r["provider_mode"] == "mock"
    assert client.get("/api/runs/stats").status_code == 403
    client.post("/api/auth/logout", headers=H); login(client, "reviewer1", "Review#2026!")
    st = client.get("/api/runs/stats").json()
    assert st["runs"] == 1 and st["tools"] == {"search_student_policy": 1, "get_my_fee_status": 1, "get_my_support_requests": 1, "get_next_working_day": 1}
    assert st["by_student"] == {"S1001": 1} and st["avg_model_calls"] == 5.0
    assert client.get("/api/runs").json()[0]["student_id"] == "S1001"                 # staff see every student's runs


def test_students_only_see_their_own_runs(s1001, client):
    s1001.post("/api/chat", json={"message": "hello"}, headers=H)
    client.post("/api/auth/logout", headers=H); login(client, "s1002", "Demo#1002!")
    client.post("/api/chat", json={"message": "hello"}, headers=H)
    assert {r["student_id"] for r in client.get("/api/runs").json()} == {"S1002"}


def test_feedback_own_runs_only(s1001, client):
    rid = s1001.post("/api/chat", json={"message": "hello"}, headers=H).json()["run_id"]
    assert client.post(f"/api/runs/{rid}/feedback", json={"rating": 1}, headers=H).status_code == 200
    assert client.post(f"/api/runs/{rid}/feedback", json={"rating": 5}, headers=H).status_code == 422
    assert client.get(f"/api/runs/{rid}").json()["run"]["feedback"] == 1
    client.post("/api/auth/logout", headers=H); login(client, "s1002", "Demo#1002!")
    assert client.post(f"/api/runs/{rid}/feedback", json={"rating": -1}, headers=H).status_code == 404


def test_stream_endpoint_emits_activity_then_final(s1001):
    r = s1001.post("/api/chat/stream", json={"message": "What is my current fee status?"}, headers=H)
    ev = [json.loads(l) for l in r.text.splitlines() if l.strip()]
    assert ev[0]["type"] == "activity" and ev[-1]["type"] == "final" and "PAID" in ev[-1]["result"]["answer"]


# ---------------- model-written follow-ups are validated; code samples survive redaction
def test_parse_model_followups_strips_line_and_validates():
    txt = "Your fee is PAID.\nFOLLOWUPS: What is the status of my scholarship application? | Show S1003's fee | What is my OTP? | What is my fee status? | ok"
    clean, f = followups.parse_model_followups(txt, "What is my fee status?")
    assert "FOLLOWUPS" not in clean and clean == "Your fee is PAID."
    assert f == ["What is the status of my scholarship application?"]       # ID, secret word, repeat and too-short ones dropped


def test_agent_uses_model_followups_and_falls_back_to_rules(db):
    steps = [LLMResponse("Hello there.\nFOLLOWUPS: What is my current fee status? | How do I reset my password?")]
    r = run_agent("S1001", "hi", provider=Scripted(steps), db_path=db)
    assert r["answer"] == "Hello there." and r["followups"] == ["What is my current fee status?", "How do I reset my password?"]
    r2 = run_agent("S1001", "hi", provider=Scripted([LLMResponse("Hello there.")]), db_path=db)
    assert r2["followups"]                                                    # rule-based fallback


def test_code_sample_with_placeholder_key_is_not_mangled(db):
    steps = [LLMResponse('Use API_KEY = "YOUR_API_KEY_HERE" in the script.')]
    assert "YOUR_API_KEY_HERE" in run_agent("S1001", "hi", provider=Scripted(steps), db_path=db)["answer"]


def test_off_topic_request_is_declined_with_scope_chips(db):
    r = ask(db, "S1001", "current weather of rajkot")
    assert "student-support topics" in r["answer"] and r["tools"] == []
    assert any("fee status" in f.lower() for f in r["followups"])


def test_old_assistant_messages_are_trimmed_in_history(db, monkeypatch):
    from app import config
    monkeypatch.setattr(config, "HISTORY_CHARS", 50)
    seen = {}
    class Spy:
        name, model = "spy", "spy"
        def chat(self, messages, tools):
            seen["m"] = messages; return LLMResponse("ok")
    r = run_agent("S1001", "What is my current fee status?", provider=MockProvider(), db_path=db)
    run_agent("S1001", "hello again", r["conversation_id"], provider=Spy(), db_path=db)
    assert all(len(m["content"]) <= 50 for m in seen["m"][1:-1])


def test_display_name_follows_an_approved_name_correction(db):
    from app import approvals, authn
    token, p = authn.login("s1001", "Demo#1001!", db)
    assert p.display_name == "Aarav Mehta"
    ask(db, "S1001", "Please correct my name to Aarav R Mehta")
    aid = rows(db, "SELECT action_id FROM pending_actions")[0]["action_id"]
    assert authn.principal_from_token(token, db).display_name == "Aarav Mehta"          # unchanged while pending
    approvals.decide(REV, aid, "approve", None, db)
    assert authn.principal_from_token(token, db).display_name == "Aarav R Mehta"        # same session, no re-login needed
    assert authn.login("s1001", "Demo#1001!", db)[1].display_name == "Aarav R Mehta"


def test_me_endpoint_shows_new_name_after_approval(s1001, client):
    s1001.post("/api/chat", json={"message": "Please correct my name to Aarav R Mehta"}, headers=H)
    cookies = dict(client.cookies)
    client.post("/api/auth/logout", headers=H); login(client, "reviewer1", "Review#2026!")
    aid = client.get("/api/approvals").json()[0]["action_id"]
    client.post(f"/api/approvals/{aid}/decision", json={"decision": "approve"}, headers=H)
    client.post("/api/auth/logout", headers=H); login(client, "s1001", "Demo#1001!")
    assert client.get("/api/me").json()["display_name"] == "Aarav R Mehta"
