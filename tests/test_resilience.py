"""Regression tests for the failures seen in the live gpt-oss-20b trace: hidden tool errors, retry loops, step-limit dead ends, rate limits."""
import json

import pytest

from app import config, tools
from app.agent import run_agent
from app.llm import LLMResponse, ProviderError, retry_seconds
from app.retrieval import search_policy
from tests.test_agent import Scripted, call


class Spy:
    """Records what the model was offered on each call, then replays scripted responses."""
    name, model = "spy", "spy"

    def __init__(self, steps):
        self.steps, self.seen = list(steps), []

    def chat(self, messages, tools_):
        self.seen.append({"messages": json.loads(json.dumps(messages)), "tools": tools_})
        return self.steps.pop(0)


def test_policy_search_never_depends_on_an_optional_config_line(monkeypatch):
    monkeypatch.delattr(config, "RAG_EXTRA_EXCERPT_CHARS", raising=False)
    r = search_policy("What is the attendance requirement?")
    assert len(r["results"]) >= 2 and all(len(x["excerpt"]) <= 450 for x in r["results"][1:])


def _break_search(monkeypatch, counter):
    def boom(ctx, a):
        counter.append(1)
        raise RuntimeError("boom: secret internal detail")
    monkeypatch.setitem(tools.TOOLS, "search_student_policy", tools.Tool("search_student_policy", "d", tools.PolicyArgs, boom, "Searching the student handbook"))


def test_tool_exception_reason_is_in_the_trace_but_not_shown_to_the_model(db, monkeypatch):
    _break_search(monkeypatch, [])
    spy = Spy([call("search_student_policy", {"query": "attendance rule"}), LLMResponse("done")])
    r = run_agent("S1001", "attendance?", provider=spy, db_path=db)
    assert r["tools"][0]["error_code"] == "tool_error"
    tool_msg = [m for m in spy.seen[1]["messages"] if m["role"] == "tool"][0]["content"]
    assert "boom" not in tool_msg
    from app.db import connect
    c = connect(db); payload = json.loads(c.execute("SELECT payload FROM agent_events WHERE run_id=? AND event_type='tool_call'", (r["run_id"],)).fetchone()[0]); c.close()
    assert payload["detail"].startswith("RuntimeError: boom")


def test_a_tool_that_failed_twice_is_not_called_a_third_time(db, monkeypatch):
    runs = []
    _break_search(monkeypatch, runs)
    steps = [call("search_student_policy", {"query": f"attendance rule {i}"}, f"c{i}") for i in range(4)] + [LLMResponse("I could not check the handbook.")]
    r = run_agent("S1001", "attendance?", provider=Scripted(steps), db_path=db)
    assert [t["error_code"] for t in r["tools"]] == ["tool_error", "tool_error", "tool_unavailable", "tool_unavailable"]
    assert len(runs) == 2 and r["status"] == "ok" and "could not check" in r["answer"]


def test_last_step_offers_no_tools_and_the_model_answers(db, monkeypatch):
    monkeypatch.setattr(config, "MAX_AGENT_STEPS", 3)
    spy = Spy([call("get_my_profile", {}, "a"), call("get_my_fee_status", {}, "b"), LLMResponse("Here is what I found.")])
    r = run_agent("S1001", "who am I and what do I owe?", provider=spy, db_path=db)
    assert r["status"] == "ok" and r["answer"] == "Here is what I found."
    assert spy.seen[0]["tools"] and spy.seen[1]["tools"] and spy.seen[2]["tools"] == []
    assert spy.seen[2]["messages"][-1]["role"] == "system" and "no tool steps left" in spy.seen[2]["messages"][-1]["content"]


def test_a_model_that_still_asks_for_tools_on_the_last_step_is_stopped(db, monkeypatch):
    monkeypatch.setattr(config, "MAX_AGENT_STEPS", 2)
    r = run_agent("S1001", "x", provider=Spy([call("get_my_profile", {}, "a"), call("get_my_fee_status", {}, "b")]), db_path=db)
    assert r["status"] == "error" and r["stop_reason"] == "max_steps"
    assert [t["name"] for t in r["tools"]] == ["get_my_profile"]          # the over-limit request was never executed


def test_a_six_lookup_question_fits_in_the_default_step_budget(db):
    assert config.MAX_AGENT_STEPS == 8
    names = ["get_my_profile", "get_my_fee_status", "get_my_scholarship_status", "search_student_policy", "get_my_support_requests", "get_next_working_day"]
    args = [{}, {}, {}, {"query": "payment portal failure"}, {}, {"date": "2026-10-20"}]
    steps = [call(n, a, f"c{i}") for i, (n, a) in enumerate(zip(names, args))] + [LLMResponse("All six checked.")]
    r = run_agent("S1001", "name, course, fee, scholarship, and can I pay next working day?", provider=Scripted(steps), db_path=db)
    assert r["status"] == "ok" and r["tool_calls"] == 6 and r["model_calls"] == 7


@pytest.mark.parametrize("msg,secs", [("Please try again in 19.5s. Need more", 19.5), ("try again in 5m4.2s", 304.2), ("try again in 850ms", 0.85), ("no hint here", None)])
def test_retry_wait_is_parsed_from_the_provider_message(msg, secs):
    got = retry_seconds(msg)
    assert (got is None) if secs is None else abs(got - secs) < 1e-6


def test_retry_after_header_wins():
    assert retry_seconds("try again in 19.5s", {"retry-after": "7"}) == 7.0


def test_rate_limit_message_tells_the_student_how_long_to_wait(db):
    class Limited:
        name, model = "groq", "x"
        def chat(self, m, t): raise ProviderError("provider_rate_limited", "RateLimitError: 429", 19.5)
    r = run_agent("S1001", "What is my fee status?", provider=Limited(), db_path=db)
    assert "about 20 seconds" in r["answer"] and r["stop_reason"] == "provider_error"
