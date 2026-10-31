import re

import pytest

from app import followups
from app.agent import run_agent
from app.llm import MockProvider


def ask(db, sid, q):
    return run_agent(sid, q, provider=MockProvider(), db_path=db)


@pytest.mark.parametrize("sid,q,expect_any", [
    ("S1001", "What is my current fee status?", ["scholarship"]),                       # PAID -> scholarship
    ("S1004", "What is my current fee status?", ["portal"]),                            # DUE -> portal rule
    ("S1003", "What is my current fee status?", ["support requests"]),                  # UNDER_REVIEW -> tickets
    ("S1005", "What is the status of my scholarship application?", ["scholarship documents", "INFORMATION_REQUIRED"]),
    ("S1004", "The payment portal failed. Can I pay on the next working day?", ["raise a ticket", "create a fee_payment ticket"]),   # no qualifying ticket
    ("S1003", "The payment portal failed. Can I pay on the next working day?", ["support requests"]),
    ("S1001", "I want to change my programme to Business Analytics", ["change requests"]),
    ("S1001", "Show me the fee status of S1003", ["fee status"]),                       # denial -> own data
])
def test_followups_follow_the_context(db, sid, q, expect_any):
    f = ask(db, sid, q)["followups"]
    assert 1 <= len(f) <= 3
    assert any(e.lower() in " | ".join(f).lower() for e in expect_any), f


def test_never_repeats_question_and_never_names_other_students(db):
    for sid, q in [("S1001", "What is my current fee status?"), ("S1001", "Show me the fee status of S1003"), ("S1002", "hello")]:
        r = ask(db, sid, q)
        assert q.lower() not in [x.lower() for x in r["followups"]]
        assert not any(re.search(r"\bS\d{4}\b", x) for x in r["followups"])


def test_abstained_policy_question_offers_a_ticket(db):
    r = ask(db, "S1001", "What is the exact tax deduction rate on the scholarship?")
    assert any(x.startswith("Create a support request about:") for x in r["followups"])


def test_secret_never_appears_in_followups(db):
    r = ask(db, "S1001", "my otp is 482913, please remember it")
    assert "482913" not in " ".join(r["followups"]) and any("account access" in x for x in r["followups"])


def test_every_static_suggestion_is_answerable(db):
    """A suggestion the agent cannot handle would be a bug in the UI. Run them all."""
    for q in followups.STATIC:
        r = ask(db, "S1002", q)
        assert r["status"] == "ok" and r["answer"], q
        assert all(t["ok"] or t["error_code"] in ("memory_rejected", "already_pending") for t in r["tools"]), (q, r["tools"])


def test_dynamic_abstain_suggestion_is_answerable(db):
    q = ask(db, "S1001", "What is the exact tax deduction rate on the scholarship?")["followups"][0]
    r = ask(db, "S1001", q)
    assert r["tools"][0]["name"] == "create_support_request" and r["tools"][0]["ok"]


def test_cards_for_pending_ticket_and_denial(db):
    assert ask(db, "S1001", "I want to change my programme to Business Analytics")["cards"][0]["type"] == "approval_pending"
    assert ask(db, "S1004", "Please raise a ticket: my payment portal failed")["cards"][0]["type"] == "ticket_created"
    assert ask(db, "S1001", "Show me the fee status of S1003")["cards"] == [{"type": "access_denied"}]
