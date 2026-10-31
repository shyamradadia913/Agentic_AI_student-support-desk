"""Typed tool registry. The LLM can only REQUEST these tools; this code validates and executes them.

Security design: NO tool accepts a student_id. The acting student comes from ToolContext, which the
application sets from trusted state. Unknown tools / extra arguments are rejected (extra='forbid').
"""
import json
import re
import logging
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import approvals, audit, config, memory
from .authz import Principal, authorize
from .db import connect, tx
from .redact import redact
from .retrieval import get_chunk, search_policy
from .timeutil import IST, next_working_day, now_iso

FAULTS: set[str] = set()  # test hook: tool names forced to fail (simulates DB/tool outage)
CATEGORIES = Literal["FEE_PAYMENT", "SCHOLARSHIP", "COURSE_CHANGE", "ACADEMIC_RECORD", "IT_ACCOUNT", "LIBRARY", "GENERAL_QUERY"]


@dataclass
class ToolContext:
    principal: Principal      # trusted: set by the application from the validated session, never by the model
    run_id: str = ""
    db_path: Optional[str] = None

    @property
    def student_id(self) -> str | None:
        return self.principal.student_id


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoArgs(Args):
    pass


SID = r"^S\d{4}$"


class OwnerArgs(Args):
    student_id: Optional[str] = Field(default=None, pattern=SID,
                                      description="Omit for yourself; other students are denied by the backend.")


class PolicyArgs(Args):
    query: str = Field(min_length=3, max_length=200, description="Keyword-style search query for the handbook")


class TicketArgs(Args):
    category: CATEGORIES
    description: str = Field(min_length=5, max_length=500, description="Short non-sensitive summary of the issue")


class TicketIdArgs(Args):
    ticket_id: str = Field(pattern=r"^TKT-\d{4,}$")


PROGRAMMES = Literal["PGDM - AI & Data Science", "PGDM - Big Data Analytics", "PGDM - Business Analytics"]


class CourseChangeArgs(Args):
    target_programme: PROGRAMMES
    reason: str = Field(min_length=3, max_length=300, description="Short non-sensitive reason given by the student")
    student_id: Optional[str] = Field(default=None, pattern=SID, description="Omit for yourself.")


class ProfileCorrectionArgs(Args):
    field: Literal["name"] = Field(description="Only the legal name can be corrected through this tool")
    new_value: str = Field(pattern=r"^[A-Za-z][A-Za-z .'\-]{1,59}$", description="Corrected full name (letters, spaces, . ' - only)")
    reason: str = Field(min_length=3, max_length=300)
    student_id: Optional[str] = Field(default=None, pattern=SID, description="Omit for yourself.")


class RememberArgs(Args):
    category: str = Field(max_length=30, description="one of: language, explanation_style, answer_format, example_style, study_focus")
    value: str = Field(max_length=60)


class DateArgs(Args):
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$", description="ISO date YYYY-MM-DD")


def ok(**data):
    return {"ok": True, "data": data}


def err(code: str, message: str):
    return {"ok": False, "error_code": code, "message": message}


FORBIDDEN_MSG = "You can only access or change your own records."


def guard(ctx: ToolContext, action: str, target: str | None, target_type: str = "student_record"):
    """Authorize in code. A denial is audited immediately and reveals nothing about the target."""
    d = authorize(ctx.principal, action, target)
    if not d.allowed:
        audit.record_for(ctx.principal, action=action, target_type=target_type, target_id=target or ctx.student_id,
                         authz_decision="DENY", result="DENIED", reason_code=d.reason, run_id=ctx.run_id, db_path=ctx.db_path)
    return d


def audit_ok(ctx: ToolContext, action: str, target: str | None, result: str = "SUCCESS", target_type: str = "student_record",
             approval: str = "NONE", detail=None):
    audit.record_for(ctx.principal, action=action, target_type=target_type, target_id=target or ctx.student_id,
                     authz_decision="ALLOW", result=result, approval_status=approval, run_id=ctx.run_id,
                     detail=detail, db_path=ctx.db_path)


# ---------------- tool implementations ----------------
def get_my_profile(ctx: ToolContext, a: NoArgs):
    # Own-record identity comes only from the authenticated session.
    target = ctx.student_id
    if not guard(ctx, "read_profile", target).allowed:
        return err("forbidden", FORBIDDEN_MSG)
    c = connect(ctx.db_path)
    try:
        r = c.execute("SELECT name, programme, enrolment_status, term_start_date, registration_status "
                      "FROM students WHERE student_id=?", (target,)).fetchone()
    finally:
        c.close()
    audit_ok(ctx, "read_profile", target, "SUCCESS" if r else "NOT_FOUND")
    return ok(**dict(r)) if r else err("not_found", "No profile record found.")


def get_my_fee_status(ctx: ToolContext, a: NoArgs):
    # Own-record identity comes only from the authenticated session.
    target = ctx.student_id
    if not guard(ctx, "read_fee", target).allowed:
        return err("forbidden", FORBIDDEN_MSG)
    c = connect(ctx.db_path)
    try:
        r = c.execute("SELECT fee_period,total_fee,amount_paid,amount_due,due_date,status,last_updated "
                      "FROM fee_records WHERE student_id=?", (target,)).fetchone()
    finally:
        c.close()
    audit_ok(ctx, "read_fee", target, "SUCCESS" if r else "NOT_FOUND")
    if not r:
        return err("not_found", "No fee record found; status cannot be verified.")
    extra = {}
    if r["status"] == "PAID" or (r["amount_due"] or 0) == 0:
        extra["guidance"] = "Nothing is due. The payment-portal failure rule does not apply; do not offer a ticket or extension for payment."
    return ok(currency="INR", **dict(r), **extra)


def get_my_scholarship_status(ctx: ToolContext, a: NoArgs):
    # Own-record identity comes only from the authenticated session.
    target = ctx.student_id
    if not guard(ctx, "read_scholarship", target).allowed:
        return err("forbidden", FORBIDDEN_MSG)
    c = connect(ctx.db_path)
    try:
        rows = c.execute("SELECT scheme,status,submitted_on,note,last_updated FROM scholarship_applications "
                         "WHERE student_id=?", (target,)).fetchall()
    finally:
        c.close()
    audit_ok(ctx, "read_scholarship", target, "SUCCESS" if rows else "NOT_FOUND")
    return ok(applications=[dict(r) for r in rows]) if rows else err("not_found", "No scholarship record found.")


def search_student_policy(ctx: ToolContext, a: PolicyArgs):
    res = search_policy(a.query)
    return ok(**res)


def create_support_request(ctx: ToolContext, a: TicketArgs):
    if not guard(ctx, "create_ticket", ctx.student_id, "support_ticket").allowed:
        return err("forbidden", FORBIDDEN_MSG)
    clean = redact(a.description)
    with tx(ctx.db_path) as c:
        n = c.execute("SELECT COALESCE(MAX(CAST(SUBSTR(ticket_id,5) AS INTEGER)),0)+1 FROM support_tickets").fetchone()[0]
        tid, now = f"TKT-{n:04d}", now_iso()
        c.execute("INSERT INTO support_tickets VALUES (?,?,?,?,?,?,?,?)",
                  (tid, ctx.student_id, a.category, clean.text, "OPEN", "NORMAL", now, now))
    audit_ok(ctx, "create_ticket", tid, "SUCCESS", "support_ticket", detail={"category": a.category})
    return ok(ticket_id=tid, category=a.category, status="OPEN", created_at=now,
              sensitive_content_removed=clean.found)


def get_my_support_requests(ctx: ToolContext, a: NoArgs):
    # Own-record identity comes only from the authenticated session.
    target = ctx.student_id
    if not guard(ctx, "read_tickets", target, "support_ticket").allowed:
        return err("forbidden", FORBIDDEN_MSG)
    c = connect(ctx.db_path)
    try:
        rows = c.execute("SELECT t.ticket_id,t.category,t.description,t.status,t.priority,t.created_at,t.updated_at,"
                         "(SELECT note FROM ticket_updates u WHERE u.ticket_id=t.ticket_id ORDER BY u.id DESC LIMIT 1) AS staff_note "
                         "FROM support_tickets t WHERE t.student_id=? ORDER BY t.created_at DESC LIMIT 10", (target,)).fetchall()
    finally:
        c.close()
    audit_ok(ctx, "read_tickets", target, "SUCCESS", "support_ticket")
    return ok(tickets=[dict(r) for r in rows])


def get_my_support_request(ctx: ToolContext, a: TicketIdArgs):
    if not authorize(ctx.principal, "read_ticket", ctx.student_id).allowed:
        audit.record_for(ctx.principal, action="read_ticket", target_type="support_ticket", target_id=a.ticket_id,
                         authz_decision="DENY", result="DENIED", reason_code="role_not_permitted", run_id=ctx.run_id, db_path=ctx.db_path)
        return err("forbidden", FORBIDDEN_MSG)
    c = connect(ctx.db_path)
    try:  # ownership is part of the query itself; a foreign ticket is indistinguishable from a missing one
        r = c.execute("SELECT ticket_id,category,description,status,priority,created_at,updated_at,student_id,"
                      "(SELECT note FROM ticket_updates u WHERE u.ticket_id=support_tickets.ticket_id ORDER BY u.id DESC LIMIT 1) AS staff_note "
                      "FROM support_tickets WHERE ticket_id=?", (a.ticket_id,)).fetchone()
    finally:
        c.close()
    if r is not None and r["student_id"] != ctx.student_id:
        audit.record_for(ctx.principal, action="read_ticket", target_type="support_ticket", target_id=a.ticket_id,
                         authz_decision="DENY", result="DENIED", reason_code="not_owner", run_id=ctx.run_id, db_path=ctx.db_path)
        return err("not_found", "No such support request on your account.")
    if r is None:
        audit_ok(ctx, "read_ticket", a.ticket_id, "NOT_FOUND", "support_ticket")
        return err("not_found", "No such support request on your account.")
    audit_ok(ctx, "read_ticket", a.ticket_id, "SUCCESS", "support_ticket")
    d = dict(r); d.pop("student_id")
    return ok(**d)


def remember_preference(ctx: ToolContext, a: RememberArgs):
    if not guard(ctx, "write_memory", ctx.student_id, "memory").allowed:
        return err("forbidden", FORBIDDEN_MSG)
    try:
        saved = memory.save(ctx.student_id, a.category, a.value, ctx.db_path)
    except memory.MemoryRejected as e:
        audit_ok(ctx, "write_memory", a.category[:30], "DENIED", "memory", detail={"reason": e.code})
        return err("memory_rejected", f"Not saved ({e.code}). Only language, explanation style, answer format, "
                                      "example style and a short study focus can be remembered; secrets never are.")
    return ok(**saved)


def get_next_working_day(ctx: ToolContext, a: DateArgs):
    try:
        d = date.fromisoformat(a.date)
    except ValueError:
        return err("invalid_arguments", "Not a valid date.")
    n = next_working_day(d)
    return ok(input_date=a.date, input_weekday=d.strftime("%A"), next_working_day=n.isoformat(),
              next_working_weekday=n.strftime("%A"), note="Mon-Fri only; no public-holiday calendar (VIA-GEN-005).")


def request_course_change(ctx: ToolContext, a: CourseChangeArgs):
    target = a.student_id or ctx.student_id
    if not guard(ctx, "request_course_change", target, "student_record").allowed:
        return err("forbidden", FORBIDDEN_MSG)
    c = connect(ctx.db_path)
    try:
        st = c.execute("SELECT programme FROM students WHERE student_id=?", (target,)).fetchone()
        dup = c.execute("SELECT action_id FROM pending_actions WHERE student_id=? AND action_type='COURSE_CHANGE' "
                        "AND state='PENDING_APPROVAL'", (target,)).fetchone()
    finally:
        c.close()
    if st is None:
        return err("not_found", "No profile record found.")
    if st["programme"] == a.target_programme:
        return err("no_change", "You are already enrolled in that programme.")
    if dup:
        audit_ok(ctx, "request_course_change", dup["action_id"], "CONFLICT", "pending_action", "PENDING_APPROVAL")
        return err("already_pending", f"You already have a pending course-change request ({dup['action_id']}).")
    reason = redact(a.reason).text
    res = approvals.create_course_change(ctx.principal, target, st["programme"], a.target_programme, reason, ctx.run_id, ctx.db_path)
    return ok(action_id=res["action_id"], state="PENDING_APPROVAL", current_programme=st["programme"],
              requested_programme=a.target_programme, within_request_window=res["within_window"],
              note="Request recorded. Your programme is UNCHANGED until a reviewer approves it.")


def request_profile_correction(ctx: ToolContext, a: ProfileCorrectionArgs):
    target = a.student_id or ctx.student_id
    if not guard(ctx, "request_profile_correction", target, "student_record").allowed:
        return err("forbidden", FORBIDDEN_MSG)
    c = connect(ctx.db_path)
    try:
        st = c.execute("SELECT name FROM students WHERE student_id=?", (target,)).fetchone()
        dup = c.execute("SELECT action_id FROM pending_actions WHERE student_id=? AND action_type='PROFILE_CORRECTION' "
                        "AND state='PENDING_APPROVAL'", (target,)).fetchone()
    finally:
        c.close()
    if st is None:
        return err("not_found", "No profile record found.")
    if st["name"] == a.new_value.strip():
        return err("no_change", "That is already the name on your record.")
    if dup:
        audit_ok(ctx, "request_profile_correction", dup["action_id"], "CONFLICT", "pending_action", "PENDING_APPROVAL")
        return err("already_pending", f"You already have a pending profile correction ({dup['action_id']}).")
    res = approvals.create_action("PROFILE_CORRECTION", ctx.principal, target, st["name"], a.new_value.strip(),
                                  redact(a.reason).text, ctx.run_id, ctx.db_path)
    return ok(action_id=res["action_id"], state="PENDING_APPROVAL", current_value=st["name"], requested_value=a.new_value.strip(),
              note="Request recorded. Your name is UNCHANGED until a reviewer approves it.")


def check_payment_deadline(ctx: ToolContext, a: NoArgs):
    """One call for the 'payment portal failed / can I pay next working day' question: rule + fee record + tickets + date maths."""
    sid = ctx.student_id
    if not (guard(ctx, "read_fee", sid).allowed and guard(ctx, "read_tickets", sid, "support_ticket").allowed):
        return err("forbidden", FORBIDDEN_MSG)
    c = connect(ctx.db_path)
    try:
        fee = c.execute("SELECT status,due_date,amount_due FROM fee_records WHERE student_id=?", (sid,)).fetchone()
        tix = c.execute("SELECT ticket_id,created_at FROM support_tickets WHERE student_id=? AND category='FEE_PAYMENT' "
                        "ORDER BY created_at", (sid,)).fetchall()
    finally:
        c.close()
    audit_ok(ctx, "read_fee", sid, "SUCCESS" if fee else "NOT_FOUND")
    audit_ok(ctx, "read_tickets", sid, "SUCCESS", "support_ticket")
    rule = get_chunk("VIA-FEE-004")
    if not fee or not fee["due_date"]:
        return err("not_found", "No fee record with a due date was found, so the rule cannot be applied.")
    due = date.fromisoformat(fee["due_date"])
    cutoff = datetime(due.year, due.month, due.day, 23, 59, 59, tzinfo=IST)
    qualifying = [t for t in tix if datetime.fromisoformat(t["created_at"]) <= cutoff]
    nwd = next_working_day(due)
    if fee["status"] == "PAID":
        verdict = "ALREADY_PAID"
    elif qualifying:
        verdict = "TICKET_ON_FILE_BEFORE_CUTOFF"
    elif datetime.now(IST) <= cutoff:
        verdict = "NO_TICKET_YET_CUTOFF_NOT_PASSED"
    else:
        verdict = "NO_TICKET_CUTOFF_PASSED"
    return ok(policy_ids=["VIA-FEE-004"], rule=(rule or {}).get("text"), fee_status=fee["status"], amount_due=fee["amount_due"],
              due_date=fee["due_date"], ticket_cutoff=f"{fee['due_date']} 23:59 IST",
              qualifying_ticket=({"ticket_id": qualifying[0]["ticket_id"], "created_at": qualifying[0]["created_at"]} if qualifying else None),
              pay_by=f"{nwd.isoformat()} 18:00 IST ({nwd.strftime('%A')})", verdict=verdict,
              note="No public-holiday calendar exists (VIA-GEN-005); a ticket proves the failure was reported, not that payment was made.")


def get_my_course_change_requests(ctx: ToolContext, a: NoArgs):
    if not guard(ctx, "read_course_requests", ctx.student_id, "pending_action").allowed:
        return err("forbidden", FORBIDDEN_MSG)
    rows = approvals.list_actions(student_id=ctx.student_id, db_path=ctx.db_path)
    audit_ok(ctx, "read_course_requests", ctx.student_id, "SUCCESS", "pending_action")
    return ok(requests=[{"action_id": r["action_id"], "type": r["action_type"], "field": r["field"], "requested_value": r["requested_programme"],
                         "state": r["state"], "created_at": r["created_at"], "decided_at": r["decided_at"]} for r in rows])


@dataclass
class Tool:
    name: str
    description: str
    args: type[Args]
    fn: Callable
    activity: str  # safe UI label (VIA-AGENT-001)


TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("get_my_profile", "Get the signed-in student's own profile (programme, enrolment status, term start, registration status).",
         NoArgs, get_my_profile, "Checking your profile"),
    Tool("get_my_fee_status", "Get the signed-in student's own fee record: status, amounts, due date. Uses the authenticated session; no student ID is accepted.",
         NoArgs, get_my_fee_status, "Checking your fee record"),
    Tool("get_my_scholarship_status", "Get the signed-in student's own scholarship application status. Uses the authenticated session; no student ID is accepted.",
         NoArgs, get_my_scholarship_status, "Checking your scholarship record"),
    Tool("search_student_policy", "Search the student handbook for rules/policies. Use keyword-style queries. Returns passages with policy IDs to cite. Use for ANY policy or procedure question.",
         PolicyArgs, search_student_policy, "Searching the student handbook"),
    Tool("create_support_request", "Create a support ticket for the signed-in student. Only call after the student asks for a ticket or agrees to one.",
         TicketArgs, create_support_request, "Creating a support request"),
    Tool("get_my_support_requests", "List the signed-in student's own recent support tickets, with exact status and creation time. Uses the authenticated session; no student ID is accepted.",
         NoArgs, get_my_support_requests, "Checking your support requests"),
    Tool("get_my_support_request", "Get one of the signed-in student's own support tickets by ticket ID.",
         TicketIdArgs, get_my_support_request, "Looking up a support request"),
    Tool("remember_preference", "Save a long-term preference ONLY if the student explicitly asks to remember it. Allowed categories: language, explanation_style (concise|detailed), answer_format (bullets|table|paragraph), example_style (technical|simple|analogy), study_focus (short topic). Never for temporary facts or secrets.",
         RememberArgs, remember_preference, "Saving your preference"),
    Tool("request_course_change", "Request a change to another programme for the signed-in student. This only CREATES A PENDING REQUEST that a reviewer must approve; it never changes the programme itself. Never tell the student it is approved.",
         CourseChangeArgs, request_course_change, "Submitting your course-change request for approval"),
    Tool("request_profile_correction", "Request correction of the signed-in student's legal NAME. Creates a PENDING request a reviewer must approve; never changes the record itself.",
         ProfileCorrectionArgs, request_profile_correction, "Submitting your profile correction for approval"),
    Tool("check_payment_deadline", "Use for 'payment portal failed' / 'can I pay on the next working day' questions. ONE call returns the rule, the student's due date, their FEE_PAYMENT tickets, the pay-by date and a verdict.",
         NoArgs, check_payment_deadline, "Checking the payment-deadline rule against your records"),
    Tool("get_my_course_change_requests", "List the signed-in student's own course-change requests and their approval state. (this also lists name-correction requests).",
         NoArgs, get_my_course_change_requests, "Checking your course-change requests"),
    Tool("get_next_working_day", "Compute the next working day (Mon-Fri) after a date. Use instead of calculating weekdays yourself.",
         DateArgs, get_next_working_day, "Calculating the next working day"),
]}


COMPOSITE_ONLY = {"check_payment_deadline"}


def is_enabled(name: str) -> bool:
    return name not in COMPOSITE_ONLY or config.PAYMENT_TOOL_MODE == "composite"


def _strip_titles(node):
    """Pydantic adds an auto-generated "title" to every field; it carries no meaning and costs tokens on every model call."""
    if isinstance(node, dict):
        return {k: _strip_titles(v) for k, v in node.items() if not (k == "title" and isinstance(v, str))}
    if isinstance(node, list):
        return [_strip_titles(v) for v in node]
    return node


def _lean(schema: dict) -> dict:
    """Collapse Optional[...] (anyOf string|null + default null) into the plain type and drop additionalProperties. Same meaning, fewer tokens."""
    for prop in (schema.get("properties") or {}).values():
        alts = [a for a in prop.get("anyOf", []) if a.get("type") != "null"]
        if "anyOf" in prop and len(alts) == 1:
            rest = {k: v for k, v in prop.items() if k not in ("anyOf", "default")}
            prop.clear(); prop.update({**alts[0], **rest})
        prop.pop("default", None) if prop.get("default") is None else None
    schema.pop("additionalProperties", None)
    return schema


# Tools that are only offered when the question (or the "yes" it answers) points at them. Everything else is always offered.
_ROUTES = {
    "create_support_request": r"ticket|support request|\braise\b|complain|\b(file|log|open|submit)\b|report (a|the|an) ",
    "request_course_change": r"course|programme|program|switch|transfer|move to|branch|stream|specialis",
    "get_my_course_change_requests": r"course change|programme|change request|name correction|pending|approv|requests?\b",
    "request_profile_correction": r"\bname\b|correct|spelling|rename",
    "remember_preference": r"remember|prefer|from now on|always|keep (it|answers|replies)|focus|language|concise|detailed|bullets?|table|paragraph|analogy",
    "check_payment_deadline": r"portal|payment|\bpay\b|working day",
}
_AFFIRM = re.compile(r"^\W*(yes|yep|yeah|ok|okay|sure|please do|go ahead|do it|confirm|proceed|correct)\b", re.I)


def select_tools(question: str, last_assistant: str = "") -> set[str]:
    """Context selection for tools: read tools are always offered; write/optional tools only when relevant.
    A bare 'yes' inherits the topic of the assistant's previous message."""
    names = {t for t in TOOLS if t not in _ROUTES}
    hay = question.lower() + (" " + last_assistant.lower() if _AFFIRM.match(question.strip()) else "")
    names |= {n for n, pat in _ROUTES.items() if re.search(pat, hay)}
    return names


def tool_schemas(names: set[str] | None = None) -> list[dict]:
    out = []
    for t in TOOLS.values():
        if not is_enabled(t.name) or (names is not None and t.name not in names):
            continue
        schema = _strip_titles(t.args.model_json_schema())
        if config.TOKEN_SAVER:
            schema = _lean(schema)
        out.append({"type": "function", "function": {"name": t.name, "description": t.description, "parameters": schema}})
    return out


_pool = ThreadPoolExecutor(max_workers=4)


OWN_RECORD_READ_TOOLS = {
    "get_my_profile": "read_profile",
    "get_my_fee_status": "read_fee",
    "get_my_scholarship_status": "read_scholarship",
    "get_my_support_requests": "read_tickets",
}


WRITE_TOOLS = {"create_support_request", "remember_preference", "request_course_change", "request_profile_correction"}
_SELF_WORDS = {"", "me", "self", "my", "own", "mine", "current", "null", "none", "n/a"}       # placeholders meaning "the signed-in student"


def execute(name: str, raw_args: dict, ctx: ToolContext) -> dict:
    """Validate + run one tool request. Never raises; always returns {ok, ...}."""
    tool = TOOLS.get(name)
    if tool is None or not is_enabled(name):
        return err("unknown_tool", "That tool does not exist.")

    # Backward-compatible safety net: older models/clients may still send student_id.
    # Never use it to select a record. Mismatched IDs are denied and audited; a matching
    # redundant ID is discarded, leaving the authenticated session as the source of identity.
    args_in = dict(raw_args or {})
    if name in OWN_RECORD_READ_TOOLS and "student_id" in args_in:
        supplied_id = args_in.pop("student_id")
        sid = supplied_id.strip() if isinstance(supplied_id, str) else supplied_id
        is_self = sid is None or (isinstance(sid, str) and (sid.lower() in _SELF_WORDS or sid.upper() == ctx.student_id))
        if not is_self:
            guard(ctx, OWN_RECORD_READ_TOOLS[name], str(sid))
            return err("forbidden", FORBIDDEN_MSG)

    # Read-only tools tolerate arguments they do not define (models often add a language/format hint): drop them and say so.
    # Write tools stay strict. Dropping changes nothing about identity or access: those never come from arguments.
    ignored: list[str] = []
    if name not in WRITE_TOOLS:
        ignored = sorted(k for k in args_in if k not in tool.args.model_fields)
        for k in ignored:
            args_in.pop(k)

    try:
        args = tool.args.model_validate(args_in)
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(str(p) for p in x['loc']) or 'args'}: {x['msg']}" for x in e.errors()[:3])
        accepted = ", ".join(sorted(tool.args.model_fields)) or "no arguments"
        return err("invalid_arguments", f"Invalid arguments ({problems})."[:260] + f" This tool accepts: {accepted}. Call it again with only those.")
    if name in FAULTS:
        return err("tool_error", "The service is temporarily unavailable.")
    fut = _pool.submit(tool.fn, ctx, args)
    try:
        res = fut.result(timeout=config.TOOL_TIMEOUT_S)
        if ignored and isinstance(res, dict):
            res["ignored_arguments"] = ignored
        return res
    except FutTimeout:
        return err("timeout", "The operation timed out.")
    except Exception as e:
        logging.getLogger(__name__).exception("tool %s failed", name)
        out = err("tool_error", "The operation could not be completed.")
        out["detail"] = f"{type(e).__name__}: {str(e)[:160]}"           # shown in the trace only; stripped before the model sees it
        return out


_NEVER_TO_MODEL = {"detail", "duplicate_call"}


def model_view(name: str, result: dict) -> dict:
    """What the model is shown. The trace keeps the full result; the model does not need scores, thresholds or null fields."""
    out = {k: v for k, v in result.items() if k not in _NEVER_TO_MODEL}
    if not config.TOKEN_SAVER:
        return {k: v for k, v in result.items() if k != "detail"}
    d = out.get("data")
    if name == "search_student_policy" and isinstance(d, dict) and "results" in d:
        out["data"] = {"abstain": d["abstain"], "results": [{"chunk_id": r["chunk_id"], "title": r["title"], "excerpt": r["excerpt"]} for r in d["results"]]}
        if d["abstain"]:
            out["data"]["reason"] = d.get("reason")
    elif isinstance(d, dict) and name != "check_payment_deadline":
        out["data"] = {k: v for k, v in d.items() if v is not None}
    return out


def compact(result: dict, limit: int = 4000) -> str:
    s = json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":") if config.TOKEN_SAVER else None)
    return s if len(s) <= limit else s[:limit] + '..."truncated"}'
