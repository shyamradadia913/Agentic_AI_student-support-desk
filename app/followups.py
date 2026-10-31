"""Context-aware follow-up suggestions. Deterministic (no extra model call => no extra latency, tokens or hallucination).

Every suggestion is something the signed-in student may legitimately ask about THEIR OWN account or the handbook:
never another student's data, never an approval. A test runs every static suggestion through the agent to prove it works.
"""
import re

DEFAULTS = ["What is my current fee status?", "What is the status of my scholarship application?",
            "What is the attendance requirement?"]
PORTAL = "The payment portal failed. Can I pay on the next working day?"
FEE = "What is my current fee status?"
SCH = "What is the status of my scholarship application?"
TICKETS = "What is the status of my support requests?"
COURSE_STATUS = "What is the status of my change requests?"

_BY_POLICY_PREFIX = {
    "VIA-FEE": [FEE, PORTAL], "VIA-SCH": [SCH, "What are the scholarship award criteria?"],
    "VIA-COURSE": [COURSE_STATUS, "What is the attendance requirement?"], "VIA-ID": ["What is my programme and enrolment status?"], "VIA-SUP": [TICKETS, "How long until support responds to my ticket?"],
    "VIA-IT": ["Create a support request about my account access"], "VIA-ACAD": ["Raise a ticket about my attendance record"],
    "VIA-PRIV": [FEE], "VIA-MEM": ["Please remember that I prefer concise answers"],
}

STATIC = sorted({*DEFAULTS, "What is my programme and enrolment status?", PORTAL, FEE, SCH, TICKETS, COURSE_STATUS, "How long does a scholarship review take?",
                 "What does the APPROVED scholarship status mean?", "What does INFORMATION_REQUIRED scholarship status mean?",
                 "What are the scholarship award criteria?", "Raise a support ticket about my fee payment",
                 "Please raise a ticket: my payment portal failed", "Raise a ticket about my scholarship documents",
                 "How long until support responds to my ticket?", "Create a support request about my account access",
                 "Raise a ticket about my attendance record", "Please remember that I prefer concise answers",
                 "How do I reset my password?", "What is the refund policy?", "Yes, create a FEE_PAYMENT ticket for the portal failure"} | {q for v in _BY_POLICY_PREFIX.values() for q in v})


def _ok(results: dict, name: str):
    r = results.get(name)
    return r["data"] if r and r.get("ok") else None


def suggest(question: str, results: dict, answer: str, redactions: list, denied: bool, limit: int = 3) -> list[str]:
    """results: {tool_name: last result dict}. Returns up to `limit` follow-ups, never repeating the question just asked."""
    out: list[str] = []

    def add(*qs):
        out.extend(qs)

    if redactions:
        add("Create a support request about my account access", "How do I reset my password?")
    if denied:
        add(FEE, SCH)
    if _ok(results, "request_course_change") or _ok(results, "request_profile_correction"):
        add(COURSE_STATUS, "What is my programme and enrolment status?")
    fee = _ok(results, "get_my_fee_status")
    pd = _ok(results, "check_payment_deadline")
    if fee and pd is None and _ok(results, "get_next_working_day") is None:
        add({"PAID": SCH, "UNDER_REVIEW": TICKETS}.get(fee["status"], PORTAL))
        if fee["status"] in ("DUE", "PARTIALLY_PAID"):
            add("Raise a support ticket about my fee payment")
        elif fee["status"] == "PAID":
            add(TICKETS)
    if pd:                                                      # portal-failure flow was answered
        v = pd["verdict"]
        if v == "TICKET_ON_FILE_BEFORE_CUTOFF":
            add(TICKETS, FEE)
        elif v == "ALREADY_PAID":
            add(SCH, TICKETS)
        else:
            add("Please raise a ticket: my payment portal failed", FEE)
    nwd = _ok(results, "get_next_working_day")
    if nwd and fee is not None and not pd:                      # multi-step flow: policy + fee + tickets + date
        tix = (_ok(results, "get_my_support_requests") or {}).get("tickets", [])
        if any(t["category"] == "FEE_PAYMENT" and t["created_at"][:10] <= (fee["due_date"] or "") for t in tix):
            add(TICKETS, FEE)
        else:
            add("Please raise a ticket: my payment portal failed", FEE)
    prof = _ok(results, "get_my_profile")
    if prof:
        add(FEE, SCH)
    sch = _ok(results, "get_my_scholarship_status")
    if sch:
        states = {a["status"] for a in sch["applications"]}
        if "INFORMATION_REQUIRED" in states:
            add("Raise a ticket about my scholarship documents", "What does INFORMATION_REQUIRED scholarship status mean?")
        elif "APPROVED" in states:
            add("What does the APPROVED scholarship status mean?", FEE)
        elif "UNDER_REVIEW" in states or "SUBMITTED" in states:
            add("How long does a scholarship review take?", FEE)
        else:
            add("What are the scholarship award criteria?", FEE)
    t = _ok(results, "create_support_request")
    if t:
        add(TICKETS, "How long until support responds to my ticket?")
    elif "create_support_request" in results:                  # tried and failed
        add(TICKETS)
    if _ok(results, "get_my_support_requests") and not pd and not nwd:
        add("How long until support responds to my ticket?", FEE)
    pol = _ok(results, "search_student_policy")
    if pol is not None:
        if pol["abstain"] or re.search(r"does not specify|doesn't specify|not specified", answer, re.I):
            safe = re.sub(r"\s+", " ", question).strip()[:70].rstrip("?.! ")
            add(f"Create a support request about: {safe}")
        else:
            for c in pol["results"][:1]:
                for prefix, qs in _BY_POLICY_PREFIX.items():
                    if c["chunk_id"].startswith(prefix):
                        add(*qs)
    if "remember_preference" in results:
        r = results["remember_preference"]
        add(FEE if r.get("ok") else "Please remember that I prefer concise answers")
    if not results and re.search(r"student-support topics", answer):
        add(FEE, SCH, TICKETS)                                   # out-of-scope request: steer back to what the desk does
    # de-duplicate, drop the question just asked, then pad with defaults
    seen, final = {re.sub(r"\W+", " ", question).strip().lower()}, []
    for q in out + DEFAULTS:
        k = re.sub(r"\W+", " ", q).strip().lower()
        if k not in seen:
            seen.add(k); final.append(q)
        if len(final) == limit:
            break
    return final


# ---- action-first follow-ups: when the answer offers to act, or the record shows an action is due, the one-click action leads
PORTAL_TICKET = "Yes, create a FEE_PAYMENT ticket for the portal failure"
_OFFER = re.compile(r"\b(would you like|shall i|do you want|want me to|should i|i can)\b[^.?!\n]{0,60}\b(create|raise|open|log|file|submit)\b[^.?!\n]{0,40}\b(ticket|support request)", re.I)
_PORTAL = re.compile(r"portal|payment (failed|failure)|next working day|could ?n.t pay|unable to pay", re.I)
_ACTION = re.compile(r"^(yes, )?(please )?(create|raise|open|log|file|submit)\b.{0,40}\b(ticket|support request)", re.I)


def actions(question: str, results: dict, answer: str, denied: bool, redactions: list | None = None) -> list[str]:
    """Deterministic action chips. Never after a denial, never when a ticket was already created in this reply."""
    if denied or _ok(results, "create_support_request"):
        return []
    acts = []
    if redactions or "[REDACTED" in question:              # never build a chip (or a ticket) from a message that held a secret
        return ["Create a support request about my account access"]
    if _OFFER.search(answer):
        if _PORTAL.search(question) or "FEE_PAYMENT" in answer:
            acts.append(PORTAL_TICKET)
        else:
            safe = re.sub(r"\s+", " ", question).strip()[:70].rstrip("?.! ")
            acts.append(f"Create a support request about: {safe}")
    acts += [q for q in suggest(question, results, answer, [], denied, limit=6) if _ACTION.search(q)]
    return acts


def merge(question: str, results: dict, answer: str, redactions: list, denied: bool, model: list[str], limit: int = 3) -> list[str]:
    """Up to one action chip first, then the model's own suggestions (or the rule-based ones), no near-duplicates."""
    acts = actions(question, results, answer, denied, redactions)[:1]
    rest = model or suggest(question, results, answer, redactions, denied)
    if acts:
        rest = [q for q in rest if not _ACTION.search(q)]        # one ticket chip, not two worded differently
    q0, out = re.sub(r"\W+", " ", question).strip().lower(), []
    for q in acts + rest:
        k = re.sub(r"\W+", " ", q).strip().lower()
        if k != q0 and k not in [re.sub(r"\W+", " ", x).strip().lower() for x in out]:
            out.append(q)
    return out[:limit]


_ID = re.compile(r"\bS\d{4}\b", re.I)
_BAD = re.compile(r"\botp\b|\bpin\b|\bcvv\b|card number|password (is|:)|approve|reject|ignore|instruction|other student|another student", re.I)


def parse_model_followups(text: str, question: str, limit: int = 3) -> tuple[str, list[str]]:
    """Split a trailing 'FOLLOWUPS: a | b | c' line off the model's answer. Returns (answer_without_line, validated_suggestions).
    Suggestions that name any student ID, mention secrets/approvals, repeat the question or look malformed are dropped."""
    m = re.search(r"(?im)^\W*follow[- ]?ups?\s*:\s*(.+)$", text)
    if not m:
        return text, []
    clean = (text[:m.start()] + text[m.end():]).rstrip()
    q0 = re.sub(r"\W+", " ", question).strip().lower()
    out: list[str] = []
    for raw in m.group(1).split("|"):
        q = re.sub(r"^[\s\d.\-*\u2022\"']+|[\s\"']+$", "", raw)
        if not (8 <= len(q) <= 110) or _ID.search(q) or _BAD.search(q) or re.sub(r"\W+", " ", q).strip().lower() == q0:
            continue
        if q not in out:
            out.append(q)
    return clean, out[:limit]
