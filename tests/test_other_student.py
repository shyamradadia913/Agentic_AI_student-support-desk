"""Regression for the live trace where a student asked for another student's record and got either their own
record back (the model dropped the ID) or a model-only refusal with no audit row. Both must now be denied in code."""
import json

import pytest

from app.agent import _other_ids, run_agent
from app.db import connect
from app.llm import LLMResponse
from tests.test_agent import Scripted, call


def denials(db, action="read_fee"):
    c = connect(db)
    n = c.execute("SELECT COUNT(*) FROM audit_events WHERE action=? AND authz_decision='DENY'", (action,)).fetchone()[0]
    c.close()
    return n


@pytest.mark.parametrize("text,ids", [
    ("give me fee status of s1002", ["S1002"]), ("give me fee status s1004", ["S1004"]), ("fee status of S 1001", ["S1001"]),
    ("gie me fee status of 1004", ["S1004"]), ("student id 1003 scholarship", ["S1003"]), ("give me fee status of s10001", ["S10001"]),
    ("give me fee status of s1005", []),                   # own ID
    ("what is my fee status?", []), ("late fee of 5000?", []), ("status of TKT-0002", []),
])
def test_other_student_ids_are_found_in_the_question(text, ids):
    assert _other_ids(text, "S1005") == ids


def test_model_drops_the_id_backend_still_denies(db):
    # live trace: the model called get_my_fee_status with no student_id and the student saw their OWN record
    r = run_agent("S1005", "give me fee status of s1002", provider=Scripted([call("get_my_fee_status", {"": ""}), LLMResponse("I can't share that.")]), db_path=db)
    assert r["tools"][0]["error_code"] == "forbidden"
    assert {"type": "access_denied"} in r["cards"] and denials(db) == 1
    c = connect(db); p = json.loads(c.execute("SELECT payload FROM agent_events WHERE run_id=? AND event_type='tool_call'", (r["run_id"],)).fetchone()[0]); c.close()
    assert p["id_from_question"] is True and p["args"]["student_id"] == "S1002"


def test_model_refuses_without_a_tool_still_audited(db):
    # live trace: 0 tool calls, polite refusal, but no audit row and no denial card
    r = run_agent("S1005", "give me fee status of s1001", provider=Scripted([LLMResponse("I can only provide your own records.")]), db_path=db)
    assert r["status"] == "ok" and {"type": "access_denied"} in r["cards"] and denials(db) == 1
    c = connect(db); ev = c.execute("SELECT payload FROM agent_events WHERE run_id=? AND event_type='access_check'", (r["run_id"],)).fetchone(); c.close()
    assert json.loads(ev[0])["target"] == "S1001"


def test_scholarship_question_is_audited_as_scholarship(db):
    run_agent("S1005", "show scholarship status of S1003", provider=Scripted([LLMResponse("No.")]), db_path=db)
    assert denials(db, "read_scholarship") == 1


def test_own_id_in_the_question_is_allowed(db):
    r = run_agent("S1005", "give me fee status of s1005", provider=Scripted([call("get_my_fee_status", {}), LLMResponse("Here it is.")]), db_path=db)
    assert r["tools"][0]["ok"] and r["cards"] == [] and denials(db) == 0


def test_mentioning_a_student_without_asking_for_records_is_not_a_denial(db):
    r = run_agent("S1005", "my friend S1003 says hello", provider=Scripted([LLMResponse("Hello!")]), db_path=db)
    assert r["cards"] == [] and denials(db) == 0


# ---- live trace 16:57: the model invented student_id "S1234" for a name correction the student made for THEMSELF
from app.llm import ProviderError  # noqa: E402


def test_model_invented_id_on_a_self_request_is_dropped_and_the_request_goes_through_first_time(db):
    # live traces 16:57 and 17:26: the model kept adding student_id "S1234" to "Please correct my name to ..."
    steps = [call("request_profile_correction", {"field": "name", "new_value": "Shyam Radadia", "reason": "Name typo", "student_id": "S1234"}),
             LLMResponse("Submitted.")]
    r = run_agent("S1001", "Please correct my name to Shyam Radadia", provider=Scripted(steps), db_path=db)
    assert [t["ok"] for t in r["tools"]] == [True] and r["cards"][0]["type"] == "approval_pending"
    c = connect(db)
    assert c.execute("SELECT COUNT(*) FROM audit_events WHERE authz_decision='DENY'").fetchone()[0] == 0
    p = json.loads(c.execute("SELECT payload FROM agent_events WHERE run_id=? AND event_type='tool_call'", (r["run_id"],)).fetchone()[0]); c.close()
    assert p["dropped_model_id"] is True and "student_id" not in p["args"]


def test_forged_id_without_a_self_request_is_still_refused_and_audited_quietly(db):
    steps = [call("request_profile_correction", {"field": "name", "new_value": "X Y", "reason": "Name typo", "student_id": "S1234"}), LLMResponse("ok")]
    r = run_agent("S1001", "hello there", provider=Scripted(steps), db_path=db)
    assert r["tools"][0]["error_code"] == "forbidden" and r["tools"][0]["model_id"] is True
    assert {"type": "access_denied"} not in r["cards"]
    c = connect(db); n = c.execute("SELECT COUNT(*) FROM audit_events WHERE authz_decision='DENY'").fetchone()[0]; c.close()
    assert n == 1


def test_id_the_student_typed_still_shows_the_warning(db):
    steps = [call("request_profile_correction", {"field": "name", "new_value": "X Y", "reason": "Name correction", "student_id": "S1003"}), LLMResponse("No.")]
    r = run_agent("S1001", "Correct the name of S1003 to X Y", provider=Scripted(steps), db_path=db)
    assert {"type": "access_denied"} in r["cards"]


class _DiesAfter:
    """Replays tool calls, then the provider connection drops (APIConnectionError in the live trace)."""
    name, model = "groq", "x"

    def __init__(self, steps):
        self.steps = list(steps)

    def chat(self, messages, tools_):
        if not self.steps:
            raise ProviderError("provider_error", "APIConnectionError: Connection error.")
        return self.steps.pop(0)


def test_provider_failure_still_returns_what_was_already_fetched(db):
    steps = [call("get_my_profile", {}, "a"), call("get_my_fee_status", {}, "b"),
             call("request_course_change", {"target_programme": "PGDM - AI & Data Science", "reason": "Name correction"}, "c")]
    r = run_agent("S1001", "my name, course and fee status; and change my programme to AI & Data Science", provider=_DiesAfter(steps), db_path=db)
    assert r["status"] == "error" and r["stop_reason"] == "provider_error"
    a = r["answer"]
    assert "**Profile:** Aarav Mehta, PGDM - AI & Data Science" in a and "**Fee status:** PAID" in a
    assert "**Programme change:** You are already enrolled in that programme." in a
