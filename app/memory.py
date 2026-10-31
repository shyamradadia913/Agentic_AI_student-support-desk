"""Persistent memory (VIA-MEM-001..003). Allowlist-only, owner-scoped, secrets never stored.

Memory != chat history. A write needs (a) an allowlisted category, (b) a valid value, (c) no secret.
"""
import re
import uuid
from datetime import datetime, timezone

from .db import tx, connect
from .redact import redact

ENUM_CATEGORIES = {
    "explanation_style": {"concise", "detailed"},
    "answer_format": {"bullets", "table", "paragraph"},
    "example_style": {"technical", "simple", "analogy"},
}
LANGUAGES = {"english", "hindi", "gujarati", "marathi", "tamil", "telugu", "bengali", "kannada", "punjabi", "urdu"}
ALLOWED_CATEGORIES = set(ENUM_CATEGORIES) | {"language", "study_focus"}   # what a student / the model may write
TOPIC_CATEGORY = "recent_topics"        # system-managed: written only by note_topics() from a fixed vocabulary
TOPIC_VOCAB = {"fees", "payment deadline", "scholarship", "support requests", "course change", "profile", "attendance",
               "account access", "handbook policy"}
_FOCUS_RE = re.compile(r"^[A-Za-z][A-Za-z &/\-]{2,39}$")  # short, letters only: no digits => no codes/IDs


class MemoryRejected(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate(category: str, value: str) -> str:
    """Return the sanitised value or raise MemoryRejected(code). Never echoes the rejected value."""
    category = (category or "").strip().lower()
    if category not in ALLOWED_CATEGORIES:
        raise MemoryRejected("category_not_allowed")
    value = (value or "").strip()
    if not value or len(value) > 40:
        raise MemoryRejected("invalid_value")
    if redact(value).found:
        raise MemoryRejected("sensitive_value")
    v = value.lower()
    if category in ENUM_CATEGORIES:
        if v not in ENUM_CATEGORIES[category]:
            raise MemoryRejected("invalid_value")
        return v
    if category == "language":
        if v not in LANGUAGES:
            raise MemoryRejected("invalid_value")
        return v.capitalize()
    if not _FOCUS_RE.match(value):  # study_focus
        raise MemoryRejected("invalid_value")
    return value


def save(student_id: str, category: str, value: str, path: str | None = None) -> dict:
    clean = validate(category, value)
    category = category.strip().lower()
    now = _now()
    with tx(path) as c:
        row = c.execute("SELECT memory_id FROM memory_items WHERE student_id=? AND category=?",
                        (student_id, category)).fetchone()
        if row:
            mid = row["memory_id"]
            c.execute("UPDATE memory_items SET value=?, updated_at=? WHERE memory_id=?", (clean, now, mid))
        else:
            mid = "MEM-" + uuid.uuid4().hex[:8]
            c.execute("INSERT INTO memory_items VALUES (?,?,?,?,?,?)", (mid, student_id, category, clean, now, now))
    return {"memory_id": mid, "category": category, "value": clean}


def list_items(student_id: str, path: str | None = None) -> list[dict]:
    c = connect(path)
    try:
        rows = c.execute("SELECT memory_id, category, value, updated_at FROM memory_items WHERE student_id=? ORDER BY category",
                         (student_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()


def delete(student_id: str, memory_id: str, path: str | None = None) -> bool:
    """Owner-scoped delete: another student's memory_id deletes nothing."""
    with tx(path) as c:
        cur = c.execute("DELETE FROM memory_items WHERE memory_id=? AND student_id=?", (memory_id, student_id))
        return cur.rowcount == 1


def note_topics(student_id: str, topics: list[str], path: str | None = None) -> None:
    """Remember WHAT KIND of thing the student asked about (never the content or statuses, which change)."""
    topics = [t for t in topics if t in TOPIC_VOCAB]
    if not topics:
        return
    existing = [m for m in list_items(student_id, path) if m["category"] == TOPIC_CATEGORY]
    old = [t.strip() for t in existing[0]["value"].split(",")] if existing else []
    merged = []
    for t in topics + old:                      # newest first, de-duplicated, capped
        if t in TOPIC_VOCAB and t not in merged:
            merged.append(t)
    value = ", ".join(merged[:5])
    now = _now()
    with tx(path) as c:
        if existing:
            c.execute("UPDATE memory_items SET value=?, updated_at=? WHERE memory_id=?", (value, now, existing[0]["memory_id"]))
        else:
            c.execute("INSERT INTO memory_items VALUES (?,?,?,?,?,?)", ("MEM-" + uuid.uuid4().hex[:8], student_id, TOPIC_CATEGORY, value, now, now))


_STYLE = {"explanation_style", "answer_format", "example_style", "language"}
_FOCUS_TRIGGER = re.compile(r"(?i)\b(focus|interest|recommend|suggest|resource|remind|study|learn|topic)\w*")


def select_relevant(student_id: str, question: str, path: str | None = None) -> list[dict]:
    """Context selection: style preferences always apply to wording; study_focus only when the
    question asks for something it could personalise. Never returns everything blindly."""
    items = list_items(student_id, path)
    chosen = []
    for it in items:
        if it["category"] in _STYLE or it["category"] == TOPIC_CATEGORY:
            chosen.append(it)
        elif it["category"] == "study_focus":
            words = {w.lower() for w in re.findall(r"[A-Za-z]{4,}", it["value"])}
            if _FOCUS_TRIGGER.search(question) or any(w in question.lower() for w in words):
                chosen.append(it)
    return chosen
