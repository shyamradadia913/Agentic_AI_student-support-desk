import pytest

from app import memory, retrieval
from app.db import connect
from app.redact import redact


# ---------- redaction ----------
@pytest.mark.parametrize("text,kind", [
    ("my otp is 482913", "otp"), ("password: hunter2", "password"),
    ("pin 4821", "pin"), ("card 4111 1111 1111 1111", "card"), ("api_key=abcd1234efgh5678", "secret_assignment"),
])
def test_secrets_redacted(text, kind):
    r = redact(text)
    assert kind in r.kinds
    for secret in ("482913", "hunter2", "4821", "4111 1111 1111 1111", "abcd1234efgh5678"):
        assert secret not in r.text


def test_non_secret_untouched():
    assert not redact("What is my current fee status?").found
    assert not redact("txn ref 1234567890123456").found      # not Luhn-valid
    assert not redact("my OTP expired, please help").found   # no digits => not a code


# ---------- retrieval ----------
@pytest.fixture(scope="module")
def ix():
    return retrieval.PolicyIndex(retrieval.load_chunks())


def test_chunk_ids_are_real_policy_ids(ix):
    assert len(ix.chunks) >= 60
    assert all(c.chunk_id.startswith("VIA-") for c in ix.chunks)
    assert len({c.chunk_id for c in ix.chunks}) == len(ix.chunks)


@pytest.mark.parametrize("q,expected", [
    ("The payment portal failed. Can I pay on the next working day?", "VIA-FEE-004"),
    ("can I get a refund", "VIA-FEE-007"),
    ("what is the late fee amount", "VIA-FEE-005"),
    ("what does APPROVED scholarship status mean", "VIA-SCH-001"),
    ("initial response target after ticket creation", "VIA-SUP-003"),
    ("my password reset", "VIA-IT-001"),
    ("course change reviewer approval required", "VIA-COURSE-003"),
])
def test_relevant_policy_hit(ix, q, expected):
    ids = [r["chunk_id"] for r in ix.search(q)["results"]]
    assert expected in ids


@pytest.mark.parametrize("q", ["what is on the cafeteria menu today", "who won the cricket world cup",
                               "what is the capital of France", "write me a poem about the sea"])
def test_out_of_scope_abstains(ix, q):
    r = ix.search(q)
    assert r["abstain"] and r["results"] == []


def test_retrieval_returns_small_context(ix):
    r = ix.search("The payment portal failed. Can I pay on the next working day?")
    assert len(r["results"]) <= 3 and all(len(x["excerpt"]) <= 1000 for x in r["results"])


# ---------- seed ----------
def test_seed_is_idempotent_and_consistent(db):
    from app import seed
    seed.seed(db)
    c = connect(db)
    assert c.execute("SELECT COUNT(*) FROM students").fetchone()[0] == 6
    assert c.execute("SELECT COUNT(*) FROM fee_records").fetchone()[0] == 6
    t = c.execute("SELECT created_at FROM support_tickets WHERE student_id='S1003'").fetchone()[0]
    due = c.execute("SELECT due_date FROM fee_records WHERE student_id='S1003'").fetchone()[0]
    assert t[:10] <= due  # portal-failure ticket filed on/before the due date


# ---------- memory ----------
def test_allowed_preference_persists_and_is_owner_scoped(db):
    saved = memory.save("S1001", "explanation_style", "Concise", db)
    assert saved["value"] == "concise"
    assert [m["value"] for m in memory.list_items("S1001", db)] == ["concise"]
    assert memory.list_items("S1002", db) == []
    assert memory.delete("S1002", saved["memory_id"], db) is False   # not the owner
    assert memory.delete("S1001", saved["memory_id"], db) is True


@pytest.mark.parametrize("cat,val,code", [
    ("favourite_lunch", "biryani", "category_not_allowed"),      # temporary fact, not allowlisted
    ("explanation_style", "rude", "invalid_value"),
    ("study_focus", "otp is 482913", "sensitive_value"),
    ("study_focus", "code 4821", "invalid_value"),               # digits not allowed
    ("language", "Klingon", "invalid_value"),
])
def test_disallowed_memory_rejected_and_not_stored(db, cat, val, code):
    with pytest.raises(memory.MemoryRejected) as e:
        memory.save("S1001", cat, val, db)
    assert e.value.code == code
    assert memory.list_items("S1001", db) == []


def test_memory_selection_is_not_everything(db):
    memory.save("S1001", "explanation_style", "concise", db)
    memory.save("S1001", "study_focus", "machine learning", db)
    only_style = memory.select_relevant("S1001", "What is my fee status?", db)
    assert [m["category"] for m in only_style] == ["explanation_style"]
    both = memory.select_relevant("S1001", "Can you suggest what I should study next?", db)
    assert {m["category"] for m in both} == {"explanation_style", "study_focus"}
