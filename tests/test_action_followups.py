"""Action-first follow-ups: when the answer offers to act (or the record shows an action is due), a one-click action chip
leads, even when the live model writes its own FOLLOWUPS line without it."""
from app import followups
from app.agent import run_agent
from app.llm import LLMResponse, MockProvider
from tests.test_agent import Scripted, call

PORTAL_Q = "The payment portal failed. Can I pay on the next working day?"
OFFER = ("I can't promise that. VIA-FEE-004 needs a FEE_PAYMENT ticket before the cutoff. Would you like me to create the ticket?\n"
         "FOLLOWUPS: What is my current fee status? | How long until support responds to my ticket? | What is the late fee amount?")


def test_offer_in_answer_puts_the_action_first_even_with_model_followups(db):
    r = run_agent("S1004", PORTAL_Q, provider=Scripted([LLMResponse(OFFER)]), db_path=db)
    assert r["followups"][0] == followups.PORTAL_TICKET and len(r["followups"]) == 3
    assert "FOLLOWUPS" not in r["answer"]


def test_only_one_ticket_chip_when_the_model_also_suggests_one(db):
    text = OFFER.replace("What is the late fee amount?", "Raise a support ticket for me")
    f = run_agent("S1004", PORTAL_Q, provider=Scripted([LLMResponse(text)]), db_path=db)["followups"]
    assert sum(bool(followups._ACTION.search(q)) for q in f) == 1


def test_no_action_chip_after_the_ticket_was_created(db):
    steps = [call("create_support_request", {"category": "FEE_PAYMENT", "description": "portal failed"}),
             LLMResponse("I created TKT-0003. Would you like me to create another ticket?")]
    f = run_agent("S1004", "Please raise a ticket: my payment portal failed", provider=Scripted(steps), db_path=db)["followups"]
    assert not any(followups._ACTION.search(q) for q in f)


def test_no_action_chip_after_a_denial(db):
    f = run_agent("S1004", "Show me the fee status of S1003", provider=MockProvider(), db_path=db)["followups"]
    assert not any(followups._ACTION.search(q) for q in f)


def test_generic_offer_becomes_a_request_about_the_question(db):
    r = run_agent("S1001", "What is the hostel fee?", provider=Scripted([LLMResponse("The handbook does not specify that. I can create a support request so a reviewer can check.")]), db_path=db)
    assert r["followups"][0] == "Create a support request about: What is the hostel fee"


def test_clicking_the_portal_action_creates_a_fee_payment_ticket(db):
    r = run_agent("S1004", followups.PORTAL_TICKET, provider=MockProvider(), db_path=db)
    t = r["tools"][0]
    assert t["name"] == "create_support_request" and t["ok"] and r["cards"][0]["category"] == "FEE_PAYMENT"


def test_mock_portal_flow_without_ticket_leads_with_the_action(db):
    f = run_agent("S1004", PORTAL_Q, provider=MockProvider(), db_path=db)["followups"]
    assert followups._ACTION.search(f[0]), f


def test_secret_message_never_becomes_a_chip_or_a_ticket(db):
    f = run_agent("S1001", "my otp is 482913, please remember it", provider=MockProvider(), db_path=db)["followups"]
    assert f[0] == "Create a support request about my account access"
    assert not any("REDACTED" in q or "482913" in q or "otp" in q.lower() for q in f)
