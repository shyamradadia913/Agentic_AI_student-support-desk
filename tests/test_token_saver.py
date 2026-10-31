"""The token savers must not change what the agent does: same answers, same pass/fail, same tools used."""
import json
import re

import pytest

from app import agent, config, tools
from app.agent import _optional_rules, _trim, run_agent
from app.llm import LLMResponse, reasoning_kwargs
from app.tools import compact, model_view, select_tools, tool_schemas
from eval.golden_a2 import CASES
from eval.run import evaluate_case
from app.llm import MockProvider
from tests.test_agent import Scripted, call

TS = re.compile(r"\d{4}-\d{2}-\d{2}T[\d:+\-]+")
est = lambda o: len(json.dumps(o)) // 4
WRITE = {"create_support_request", "request_course_change", "request_profile_correction", "remember_preference", "get_my_course_change_requests"}


# ---------------- tool routing
@pytest.mark.parametrize("q,last,must,must_not", [
    ("What is my current fee status?", "", set(), WRITE - {"get_my_course_change_requests"}),
    ("What is the attendance requirement?", "", set(), WRITE),
    ("Please raise a ticket: my payment portal failed", "", {"create_support_request"}, {"request_course_change", "request_profile_correction"}),
    ("I want to change my programme to Business Analytics", "", {"request_course_change"}, {"request_profile_correction", "remember_preference"}),
    ("Please correct my name to Aarav R Mehta", "", {"request_profile_correction"}, {"request_course_change"}),
    ("Please remember that I prefer concise answers", "", {"remember_preference"}, {"request_course_change", "request_profile_correction"}),
    ("What is the status of my change requests?", "", {"get_my_course_change_requests"}, {"request_profile_correction"}),
    ("yes", "Would you like me to create that support ticket for you now?", {"create_support_request"}, set()),   # a bare yes inherits the topic
    ("yes", "Your fee is PAID.", set(), {"create_support_request"}),
])
def test_routing_offers_what_the_question_needs(q, last, must, must_not):
    names = select_tools(q, last)
    assert must <= names and not (must_not & names)
    assert {"get_my_profile", "get_my_fee_status", "get_my_scholarship_status", "search_student_policy", "get_my_support_requests",
            "get_my_support_request", "get_next_working_day"} <= names                       # read tools are always available


def test_a_tool_that_was_not_offered_cannot_run(db, monkeypatch):
    monkeypatch.setattr(config, "TOOL_ROUTING", "core")
    r = run_agent("S1001", "What is my fee status?", provider=Scripted([call("request_course_change", {"target_programme": "PGDM - Business Analytics", "reason": "just because"}), LLMResponse("ok")]), db_path=db)
    assert r["tools"][0]["error_code"] == "tool_not_offered"
    from app.db import connect
    c = connect(db); assert c.execute("SELECT COUNT(*) FROM pending_actions").fetchone()[0] == 0; c.close()


def test_routing_shrinks_the_tool_list_sent_to_the_model(monkeypatch):
    monkeypatch.setattr(config, "TOKEN_SAVER", True)
    assert est(tool_schemas(select_tools("What is my fee status?"))) < 0.65 * est(tool_schemas())


# ---------------- lean schemas and lean results
def test_lean_schemas_keep_the_same_parameters_but_cost_less(monkeypatch):
    monkeypatch.setattr(config, "TOKEN_SAVER", False); old = tool_schemas()
    monkeypatch.setattr(config, "TOKEN_SAVER", True); new = tool_schemas()
    assert est(new) < est(old)
    assert "anyOf" not in json.dumps(new) and "additionalProperties" not in json.dumps(new)
    for o, n in zip(old, new):
        assert o["function"]["name"] == n["function"]["name"] and o["function"]["description"] == n["function"]["description"]   # descriptions untouched
        assert set(o["function"]["parameters"].get("properties", {})) == set(n["function"]["parameters"].get("properties", {}))
        assert o["function"]["parameters"].get("required", []) == n["function"]["parameters"].get("required", [])


def test_model_view_drops_diagnostics_but_keeps_the_evidence(monkeypatch):
    monkeypatch.setattr(config, "TOKEN_SAVER", True)
    full = {"ok": True, "data": {"retrieval_mode": "lexical-bm25", "query_terms": ["a"], "top_k": 3, "min_score": 3.0, "min_coverage": .4, "abstain": False,
            "results": [{"chunk_id": "VIA-FEE-004", "title": "T", "section": "S", "score": 9.1, "coverage": .9, "matched_terms": ["x"], "excerpt": "rule text", "source": "h.md"}]}}
    v = model_view("search_student_policy", full)
    assert v["data"] == {"abstain": False, "results": [{"chunk_id": "VIA-FEE-004", "title": "T", "excerpt": "rule text"}]}
    assert model_view("get_my_scholarship_status", {"ok": True, "data": {"scheme": None, "status": "NOT_STARTED"}})["data"] == {"status": "NOT_STARTED"}
    assert "detail" not in model_view("x", {"ok": False, "error_code": "tool_error", "detail": "AttributeError: secret"})
    assert " " not in compact({"a": 1, "b": [1, 2]})


def test_saver_off_restores_the_old_wire_format(monkeypatch):
    monkeypatch.setattr(config, "TOKEN_SAVER", False)
    assert compact({"a": 1}) == '{"a": 1}'
    assert model_view("search_student_policy", {"ok": True, "data": {"abstain": False, "results": [], "top_k": 3}})["data"]["top_k"] == 3


# ---------------- optional prompt rules
def test_optional_prompt_rules_appear_only_when_relevant(monkeypatch):
    monkeypatch.setattr(config, "TOOL_ROUTING", "core"); monkeypatch.setattr(config, "TOKEN_SAVER", True)
    assert _optional_rules("What is my current fee status?", "") == ""
    assert "Payment-portal checks" in _optional_rules("The payment portal failed", "")
    assert "Payment-portal checks" in _optional_rules("yes", "…create that ticket? The payment portal rule…")
    assert "Where things are" in _optional_rules("Where can I see my requests?", "")
    monkeypatch.setattr(config, "TOOL_ROUTING", "off")
    assert "Where things are" in _optional_rules("hi", "") and "Payment-portal checks" in _optional_rules("hi", "")


# ---------------- history trimming keeps the ending
def test_trim_keeps_the_closing_question_and_the_length_limit():
    msg = "Yes, under the policy you may pay later. " * 20 + "Would you like me to create that support ticket for you now?"
    out = _trim(msg, 300)
    assert len(out) <= 300 and out.endswith("create that support ticket for you now?") and out.startswith("Yes, under")
    assert _trim("short", 300) == "short"


def test_reasoning_effort_only_for_gpt_oss(monkeypatch):
    monkeypatch.setattr(config, "GROQ_REASONING_EFFORT", "low")
    assert reasoning_kwargs("openai/gpt-oss-20b") == {"extra_body": {"reasoning_effort": "low"}}
    assert reasoning_kwargs("llama-3.3-70b-versatile") == {}
    monkeypatch.setattr(config, "GROQ_REASONING_EFFORT", "off")
    assert reasoning_kwargs("openai/gpt-oss-20b") == {}


# ---------------- the important one: same behaviour, whole golden set
def _run_all(saver, routing, monkeypatch):
    monkeypatch.setattr(config, "TOKEN_SAVER", saver); monkeypatch.setattr(config, "TOOL_ROUTING", routing)
    out = {}
    for c in CASES:
        r = evaluate_case(c, MockProvider)
        out[c["id"]] = (r["passed"], TS.sub("<ts>", r["answer"]), r["tools_used"], r["stop_reason"])
    return out


def test_savers_and_routing_change_nothing_the_student_or_the_eval_can_see(monkeypatch):
    before = _run_all(False, "off", monkeypatch)
    after = _run_all(True, "core", monkeypatch)
    assert after == before
    assert sum(1 for v in after.values() if not v[0]) == 1          # only the known R07 retrieval miss, in both settings
    assert not any("tool_not_offered" in str(v) for v in after.values())
