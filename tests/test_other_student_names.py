"""Another student asked for by NAME (or a name the desk has never heard of) is denied and audited in code,
whatever the model does. Live trace: the model called the own-record tool and the student's OWN record came back labelled with the other name."""
import pytest

from app.agent import _other_names, run_agent
from app.db import connect
from app.llm import LLMResponse
from tests.test_agent import Scripted, call


def denials(db):
    c = connect(db)
    n = c.execute("SELECT COUNT(*) FROM audit_events WHERE authz_decision='DENY'").fetchone()[0]
    c.close()
    return n


@pytest.mark.parametrize("text,found", [
    ("give me fee status of Diya Shah", "S1002"), ("scholarship status of rohan", "S1003"), ("what is Neha's fee status", "S1004"),
    ("give me fee status of zorbak", "zorbak"), ("give me scholarship status of zorbak kumar", "zorbak kumar"),
])
def test_names_are_found(db, text, found):
    assert _other_names(text, "S1005", db) == [found]


@pytest.mark.parametrize("text", [
    "give me fee status of kabir", "what is my fee status?", "status of my scholarship application", "fee status of the hostel",
    "status of attendance", "late fee of 5000?", "what is the refund policy for fee payment", "status of TKT-0002", "hello Diya",
])
def test_own_and_policy_questions_are_not_flagged(db, text):
    assert _other_names(text, "S1005", db) == []


@pytest.mark.parametrize("q", ["give me fee status of zorbak", "give me fee status of Diya Shah"])
def test_model_ignores_the_name_and_reads_own_record_code_still_denies(db, q):
    r = run_agent("S1005", q, provider=Scripted([call("get_my_fee_status", {}), LLMResponse("Here is the fee status.")]), db_path=db)
    assert r["tools"][0]["error_code"] == "forbidden" and {"type": "access_denied"} in r["cards"] and denials(db) == 1


def test_model_refuses_in_words_still_audited(db):
    r = run_agent("S1005", "give me fee status of zorbak", provider=Scripted([LLMResponse("I can only show your own fees.")]), db_path=db)
    assert {"type": "access_denied"} in r["cards"] and denials(db) == 1


def test_own_name_is_allowed(db):
    r = run_agent("S1005", "give me fee status of Kabir Singh", provider=Scripted([call("get_my_fee_status", {}), LLMResponse("Here it is.")]), db_path=db)
    assert r["tools"][0]["ok"] and r["cards"] == [] and denials(db) == 0
