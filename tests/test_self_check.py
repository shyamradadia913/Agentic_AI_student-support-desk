"""Regression for the live trace (17:10) where the model wrote 'has been submitted and is PENDING_APPROVAL' for a
programme change and a name correction without calling either request tool: it copied the claim from earlier history."""
import json

from app.agent import run_agent
from app.db import connect
from app.llm import LLMResponse
from tests.test_agent import Scripted, call

Q = "Please correct my name to Shyam Radadia and I want to change my programme to PGDM Big Data Analytics"
FAKE = "Your request to switch to PGDM Big Data Analytics has been submitted and is PENDING_APPROVAL."


def events(db, run_id, kind):
    c = connect(db); rows = c.execute("SELECT payload FROM agent_events WHERE run_id=? AND event_type=?", (run_id, kind)).fetchall(); c.close()
    return [json.loads(r[0]) for r in rows]


def test_claim_without_a_tool_triggers_a_self_check_and_the_request_is_really_made(db):
    steps = [LLMResponse(FAKE),
             call("request_course_change", {"target_programme": "PGDM - Big Data Analytics", "reason": "Student asked"}, "a"),
             LLMResponse("I submitted course-change request ACT-0001. It is pending approval.")]
    r = run_agent("S1001", Q, provider=Scripted(steps), db_path=db)
    assert [t["name"] for t in r["tools"]] == ["request_course_change"] and r["tools"][0]["ok"]
    assert r["cards"][0]["type"] == "approval_pending" and len(events(db, r["run_id"], "self_check")) == 1
    c = connect(db); n = c.execute("SELECT COUNT(*) FROM pending_actions WHERE student_id='S1001'").fetchone()[0]; c.close()
    assert n == 1


def test_model_that_insists_gets_a_visible_correction(db):
    r = run_agent("S1001", Q, provider=Scripted([LLMResponse(FAKE), LLMResponse(FAKE)]), db_path=db)
    assert "**Correction:** no change request or ticket was submitted" in r["answer"]
    c = connect(db); n = c.execute("SELECT COUNT(*) FROM pending_actions").fetchone()[0]; c.close()
    assert n == 0


def test_real_submission_is_not_second_guessed(db):
    steps = [call("request_course_change", {"target_programme": "PGDM - Big Data Analytics", "reason": "Student asked"}), LLMResponse(FAKE)]
    r = run_agent("S1001", Q, provider=Scripted(steps), db_path=db)
    assert "Correction" not in r["answer"] and events(db, r["run_id"], "self_check") == []


def test_reporting_status_from_the_status_tool_is_fine(db):
    run_agent("S1001", Q, provider=Scripted([call("request_course_change", {"target_programme": "PGDM - Big Data Analytics", "reason": "Student asked"}), LLMResponse("ok")]), db_path=db)
    steps = [call("get_my_course_change_requests", {}), LLMResponse("Your change ACT-0001 is PENDING_APPROVAL.")]
    r = run_agent("S1001", "What is the status of my change requests?", provider=Scripted(steps), db_path=db)
    assert "Correction" not in r["answer"] and events(db, r["run_id"], "self_check") == []
