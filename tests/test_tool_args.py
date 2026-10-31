"""Regression for the live trace where one read tool was called four times with 'invalid_arguments'."""
import json

import pytest

from app import config, memory, tools
from app.agent import _needs_all_tools, run_agent
from app.authz import Principal
from app.db import connect
from app.llm import LLMResponse
from tests.test_agent import Scripted, call

READ = ["get_my_profile", "get_my_fee_status", "get_my_scholarship_status", "get_my_support_requests"]


def ctx(db, sid="S1004"):
    return tools.ToolContext(Principal.for_student(sid), "", db)


@pytest.mark.parametrize("name", READ)
@pytest.mark.parametrize("junk", [{"language": "Gujarati"}, {"format": "short", "student_id": None}, {"": ""}])
def test_read_tools_ignore_arguments_they_do_not_define(db, name, junk):
    r = tools.execute(name, dict(junk), ctx(db))
    assert r["ok"] is True
    assert set(r.get("ignored_arguments", [])) == {k for k in junk if k != "student_id"}


@pytest.mark.parametrize("placeholder", ["", "me", "self", "MY", "own", "null", "s1004", " S1004 "])
def test_placeholders_and_own_id_mean_the_signed_in_student(db, placeholder):
    r = tools.execute("get_my_fee_status", {"student_id": placeholder}, ctx(db))
    assert r["ok"] and r["data"]["due_date"] == "2026-10-28"           # S1004's own record


@pytest.mark.parametrize("other", ["S1003", "s1003", "Rohan Patel", 1003])
def test_another_student_is_still_refused_and_audited(db, other):
    r = tools.execute("get_my_fee_status", {"student_id": other}, ctx(db))
    assert r["ok"] is False and r["error_code"] == "forbidden"
    c = connect(db); n = c.execute("SELECT COUNT(*) FROM audit_events WHERE action='read_fee' AND authz_decision='DENY'").fetchone()[0]; c.close()
    assert n == 1


def test_write_tools_stay_strict_about_unknown_arguments(db):
    r = tools.execute("create_support_request", {"category": "LIBRARY", "description": "need e-books", "student_id": "S1001"}, ctx(db))
    assert r["ok"] is False and r["error_code"] == "invalid_arguments"
    c = connect(db); assert c.execute("SELECT COUNT(*) FROM support_tickets WHERE student_id='S1004'").fetchone()[0] == 0; c.close()


def test_invalid_argument_message_tells_the_model_what_the_tool_accepts(db):
    r = tools.execute("search_student_policy", {}, ctx(db))
    assert r["error_code"] == "invalid_arguments" and "query" in r["message"] and "This tool accepts: query" in r["message"]
    r = tools.execute("get_next_working_day", {"date": "tomorrow"}, ctx(db))
    assert "date" in r["message"] and "This tool accepts: date" in r["message"]


# ---------------- the agent loop
def test_junk_argument_no_longer_causes_a_retry_loop(db):
    r = run_agent("S1004", "maru fee status shu che?", provider=Scripted([call("get_my_fee_status", {"language": "Gujarati"}), LLMResponse("Fee status: DUE.")]), db_path=db)
    assert r["tool_calls"] == 1 and r["model_calls"] == 2 and r["tools"][0]["ok"] and r["answer"] == "Fee status: DUE."
    c = connect(db); p = json.loads(c.execute("SELECT payload FROM agent_events WHERE run_id=? AND event_type='tool_call'", (r["run_id"],)).fetchone()[0]); c.close()
    assert p["ignored_arguments"] == ["language"]


def test_a_tool_failing_on_arguments_is_stopped_after_two_tries_and_the_model_still_answers(db):
    steps = [call("get_next_working_day", {"date": "tomorrow"}, "a"), call("get_next_working_day", {"date": "next monday"}, "b"),
             call("get_next_working_day", {"date": "soon"}, "c"), LLMResponse("I could not work out the date.")]
    r = run_agent("S1004", "next working day?", provider=Scripted(steps), db_path=db)
    assert [t["error_code"] for t in r["tools"]] == ["invalid_arguments", "invalid_arguments", "tool_unavailable"]
    assert r["status"] == "ok" and r["answer"] == "I could not work out the date."


def test_repeating_the_same_failed_call_counts_as_a_failure(db):
    same = {"date": "tomorrow"}
    steps = [call("get_next_working_day", same, "a"), call("get_next_working_day", same, "b"), call("get_next_working_day", same, "c"), LLMResponse("Done.")]
    r = run_agent("S1004", "x", provider=Scripted(steps), db_path=db)
    assert [t["error_code"] for t in r["tools"]] == ["invalid_arguments", "invalid_arguments", "tool_unavailable"]
    assert r["status"] == "ok" and r["stop_reason"] == "final_answer"            # not the old "I got stuck repeating the same step"


def test_failed_calls_show_their_reason_and_arguments_in_the_trace(db):
    r = run_agent("S1004", "x", provider=Scripted([call("get_next_working_day", {"date": "tomorrow"}), LLMResponse("ok")]), db_path=db)
    c = connect(db); p = json.loads(c.execute("SELECT payload FROM agent_events WHERE run_id=? AND event_type='tool_call'", (r["run_id"],)).fetchone()[0]); c.close()
    assert p["error_code"] == "invalid_arguments" and "This tool accepts: date" in p["message"] and p["args"] == {"date": "tomorrow"}


# ---------------- routing must not strand students who write in another language
@pytest.mark.parametrize("q,mem,expected", [
    ("What is my fee status?", [], False),
    ("maru fee status shu che?", [{"category": "language", "value": "Gujarati"}], True),
    ("મારી ફી સ્ટેટસ શું છે?", [], True),
    ("मेरा फीस स्टेटस क्या है?", [], True),
    ("What is my fee status?", [{"category": "language", "value": "English"}], False),
])
def test_routing_is_bypassed_for_non_english_students(q, mem, expected):
    assert _needs_all_tools(q, mem) is expected


def test_every_tool_is_offered_when_the_student_prefers_gujarati(db, monkeypatch):
    monkeypatch.setattr(config, "TOOL_ROUTING", "core")
    memory.save("S1004", "language", "Gujarati", db)
    r = run_agent("S1004", "maru fee status shu che?", provider=Scripted([LLMResponse("ok")]), db_path=db)
    c = connect(db); p = json.loads(c.execute("SELECT payload FROM agent_events WHERE run_id=? AND event_type='context_selected'", (r["run_id"],)).fetchone()[0]); c.close()
    assert p["tool_schemas"] == len(tools.tool_schemas())
    memory_ids = [m["memory_id"] for m in memory.list_items("S1004", db)]
    monkeypatch.setattr(config, "TOOL_ROUTING", "core")
    r2 = run_agent("S1001", "What is my fee status?", provider=Scripted([LLMResponse("ok")]), db_path=db)
    c = connect(db); p2 = json.loads(c.execute("SELECT payload FROM agent_events WHERE run_id=? AND event_type='context_selected'", (r2["run_id"],)).fetchone()[0]); c.close()
    assert p2["tool_schemas"] < len(tools.tool_schemas())                    # an English student still gets the lean list
