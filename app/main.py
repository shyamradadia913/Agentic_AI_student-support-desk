"""HTTP API + static UI (Assignment 2). Identity comes ONLY from a server-side session cookie.
Every endpoint derives the acting principal from the session and authorizes in code (app/authz.py)."""
import json
import queue
import re
import statistics
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import approvals, audit, authn, config, memory, tickets_admin
from .agent import run_agent
from .authz import Principal, authorize
from .db import connect, init_db, tx
from .llm import ProviderError
from .seed import seed
from .timeutil import now_iso

STATIC = Path(__file__).resolve().parent.parent / "static"
COOKIE = "viaa_session"


@asynccontextmanager
async def lifespan(_):
    init_db()
    c = connect()
    empty = c.execute("SELECT COUNT(*) FROM students").fetchone()[0] == 0
    c.close()
    if empty:
        seed()
    yield


app = FastAPI(title="Student Support Agent (safe & production-aware)", lifespan=lifespan)


# ---------------------------------------------------------------- dependencies
def csrf_guard(request: Request):
    """State-changing calls must carry a custom header a cross-site form cannot set (plus SameSite=Lax cookie)."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.headers.get("x-requested-with") != "viaa-ui":
        raise HTTPException(403, "Missing request header")


def current_principal(request: Request) -> Principal:
    p = authn.principal_from_token(request.cookies.get(COOKIE))
    if p is None:
        raise HTTPException(401, "Not signed in")
    return p


def require(action: str):
    def dep(p: Principal = Depends(current_principal)) -> Principal:
        d = authorize(p, action)
        if not d.allowed:
            audit.record_for(p, action=action, target_type="endpoint", authz_decision="DENY", result="DENIED", reason_code=d.reason)
            raise HTTPException(403, "Not permitted")
        return p
    return dep


# ---------------------------------------------------------------- auth
class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=100)


def _me(p: Principal) -> dict:
    return {"username": p.username, "role": p.role, "student_id": p.student_id, "display_name": p.display_name}


@app.post("/api/auth/login", dependencies=[Depends(csrf_guard)])
def login(body: LoginIn, response: Response):
    try:
        token, p = authn.login(body.username, body.password)
    except authn.AuthError as e:
        raise HTTPException(423 if e.code == "locked" else 401, "Account temporarily locked" if e.code == "locked" else "Invalid username or password")
    response.set_cookie(COOKIE, token, httponly=True, samesite="lax", secure=config.COOKIE_SECURE,
                        max_age=config.SESSION_TTL_HOURS * 3600, path="/")
    return _me(p)


@app.post("/api/auth/logout", dependencies=[Depends(csrf_guard)])
def logout(request: Request, response: Response):
    authn.logout(request.cookies.get(COOKIE))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@app.get("/api/me")
def me(p: Principal = Depends(current_principal)):
    return _me(p)


@app.get("/api/session")
def session(request: Request):
    """Like /api/me but returns 200 with user=null when signed out (keeps the login page free of 401 noise)."""
    p = authn.principal_from_token(request.cookies.get(COOKIE))
    return {"user": _me(p) if p else None}


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/provider")
def provider():
    live = config.AGENT_PROVIDER_MODE == "groq" and bool(config.GROQ_API_KEY)
    return {"mode": "groq" if live else "mock", "model": config.GROQ_MODEL if live else "mock-rules-v1",
            "notice": None if live else "Demo mode: model responses are simulated by a rule-based stand-in, not a real LLM. "
                                         "Set GROQ_API_KEY to enable live mode."}


# ---------------------------------------------------------------- chat (students only; identity from session)
class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = Field(default=None, max_length=40)


@app.post("/api/chat", dependencies=[Depends(csrf_guard)])
def chat(body: ChatIn, p: Principal = Depends(current_principal)):
    if p.role != "student" or not p.student_id:
        raise HTTPException(403, "Only students can use the support chat")
    try:
        return run_agent(p, body.message, body.conversation_id)
    except ProviderError as e:
        raise HTTPException(503, f"Model provider unavailable ({e.code}).")


# ---------------------------------------------------------------- handbook passage (public policy text, sign-in required)
@app.get("/api/policy/{chunk_id}")
def policy_chunk(chunk_id: str, p: Principal = Depends(current_principal)):
    from .retrieval import get_chunk
    if not re.fullmatch(r"VIA-[A-Z]+-\d{3}", chunk_id) or (c := get_chunk(chunk_id)) is None:
        raise HTTPException(404, "Policy not found")
    return c


@app.post("/api/chat/stream", dependencies=[Depends(csrf_guard)])
def chat_stream(body: ChatIn, p: Principal = Depends(current_principal)):
    """Same as /api/chat but streams newline-delimited JSON: {type:'activity'} events while tools run, then {type:'final'}."""
    if p.role != "student" or not p.student_id:
        raise HTTPException(403, "Only students can use the support chat")
    q: queue.Queue = queue.Queue()

    def work():
        try:
            q.put({"type": "final", "result": run_agent(p, body.message, body.conversation_id, on_event=q.put)})
        except ProviderError as e:
            q.put({"type": "error", "detail": f"Model provider unavailable ({e.code})."})
        except Exception:
            q.put({"type": "error", "detail": "Something went wrong on my side."})
        finally:
            q.put(None)

    threading.Thread(target=work, daemon=True).start()

    def gen():
        while (e := q.get()) is not None:
            yield json.dumps(e) + "\n"
    return StreamingResponse(gen(), media_type="application/x-ndjson")


# ---------------------------------------------------------------- traces (own runs; staff may see all)
_RUN_COLS = ("r.run_id, r.student_id, r.conversation_id, r.question, r.status, r.stop_reason, r.started_at, r.latency_ms, r.input_tokens, "
             "r.output_tokens, r.provider_mode, r.model, r.tools_used, r.policy_chunks, "
             "(SELECT COUNT(*) FROM agent_events e WHERE e.run_id=r.run_id AND e.event_type='model_call') AS model_calls, "
             "(SELECT COUNT(*) FROM agent_events e WHERE e.run_id=r.run_id AND e.event_type='tool_call') AS tool_calls, "
             "(SELECT rating FROM run_feedback f WHERE f.run_id=r.run_id LIMIT 1) AS feedback")


def _runs_query(p: Principal, limit: int, student_id: str | None = None, status: str | None = None, conversation_id: str | None = None):
    staff = authorize(p, "read_all_runs").allowed
    q, args = f"SELECT {_RUN_COLS} FROM agent_runs r WHERE 1=1", []
    if not staff:
        q += " AND r.student_id=?"; args.append(p.student_id)
    elif student_id:
        q += " AND r.student_id=?"; args.append(student_id)
    if status:
        q += " AND r.status=?"; args.append(status)
    if conversation_id:
        q += " AND r.conversation_id=?"; args.append(conversation_id)
    q += " ORDER BY r.started_at DESC LIMIT ?"; args.append(min(limit, 500))
    c = connect()
    try:
        return [dict(r) for r in c.execute(q, args)]
    finally:
        c.close()


@app.get("/api/runs")
def runs(limit: int = 30, student_id: str | None = None, status: str | None = None, conversation_id: str | None = None,
         p: Principal = Depends(current_principal)):
    return _runs_query(p, limit, student_id, status, conversation_id)


@app.get("/api/runs/stats")
def runs_stats(limit: int = 500, p: Principal = Depends(require("read_all_runs"))):
    rows = _runs_query(p, limit)
    if not rows:
        return {"runs": 0}
    lat = sorted(r["latency_ms"] for r in rows if r["latency_ms"] is not None)
    tok_in = [r["input_tokens"] for r in rows if r["input_tokens"] is not None]
    tok_out = [r["output_tokens"] for r in rows if r["output_tokens"] is not None]
    tools: dict = {}
    for r in rows:
        for t in json.loads(r["tools_used"] or "[]"):
            tools[t["name"]] = tools.get(t["name"], 0) + 1
    by_student: dict = {}
    for r in rows:
        by_student[r["student_id"]] = by_student.get(r["student_id"], 0) + 1
    p95 = lat[min(len(lat) - 1, int(round(0.95 * (len(lat) - 1))))] if lat else None
    return {"runs": len(rows), "errors": sum(1 for r in rows if r["status"] != "ok"),
            "latency_ms": {"mean": round(statistics.mean(lat), 1), "p50": round(statistics.median(lat), 1), "p95": round(p95, 1), "max": round(lat[-1], 1)} if lat else None,
            "tokens": {"input_total": sum(tok_in), "output_total": sum(tok_out), "runs_with_usage": len(tok_in)},
            "avg_model_calls": round(statistics.mean(r["model_calls"] for r in rows), 2),
            "avg_tool_calls": round(statistics.mean(r["tool_calls"] for r in rows), 2),
            "tools": dict(sorted(tools.items(), key=lambda kv: -kv[1])), "by_student": by_student,
            "thumbs_up": sum(1 for r in rows if r["feedback"] == 1), "thumbs_down": sum(1 for r in rows if r["feedback"] == -1)}


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str, p: Principal = Depends(current_principal)):
    staff = authorize(p, "read_all_runs").allowed
    c = connect()
    try:
        run = c.execute(f"SELECT {_RUN_COLS}, r.answer, r.token_note, r.error FROM agent_runs r WHERE r.run_id=?", (run_id,)).fetchone()
        if not run or (not staff and run["student_id"] != p.student_id):
            raise HTTPException(404, "Run not found")           # same answer for missing and foreign
        ev = c.execute("SELECT seq,event_type,payload,duration_ms FROM agent_events WHERE run_id=? ORDER BY seq", (run_id,)).fetchall()
    finally:
        c.close()
    return {"run": dict(run), "events": [{**dict(e), "payload": json.loads(e["payload"] or "{}")} for e in ev]}


class FeedbackIn(BaseModel):
    rating: int = Field(ge=-1, le=1)
    comment: str | None = Field(default=None, max_length=300)


@app.post("/api/runs/{run_id}/feedback", dependencies=[Depends(csrf_guard)])
def run_feedback(run_id: str, body: FeedbackIn, p: Principal = Depends(require("read_conversations"))):
    if body.rating == 0:
        raise HTTPException(422, "rating must be 1 or -1")
    c = connect()
    try:
        own = c.execute("SELECT 1 FROM agent_runs WHERE run_id=? AND student_id=?", (run_id, p.student_id)).fetchone()
    finally:
        c.close()
    if not own:
        raise HTTPException(404, "Run not found")
    from .redact import redact
    with tx() as c:
        c.execute("INSERT INTO run_feedback (run_id,user_id,rating,comment,created_at) VALUES (?,?,?,?,?) "
                  "ON CONFLICT(run_id,user_id) DO UPDATE SET rating=excluded.rating, comment=excluded.comment, created_at=excluded.created_at",
                  (run_id, p.user_id, body.rating, redact(body.comment or "").text or None, now_iso()))
    return {"ok": True}


# ---------------------------------------------------------------- memory (own only)
@app.get("/api/memory")
def memory_list(p: Principal = Depends(require("write_memory"))):
    return memory.list_items(p.student_id)


class PrefIn(BaseModel):
    category: str = Field(max_length=30)
    value: str = Field(max_length=60)


@app.post("/api/memory", dependencies=[Depends(csrf_guard)])
def memory_set(body: PrefIn, p: Principal = Depends(require("write_memory"))):
    try:
        saved = memory.save(p.student_id, body.category, body.value)
    except memory.MemoryRejected as e:
        audit.record_for(p, action="write_memory", target_type="memory", target_id=body.category[:30], authz_decision="ALLOW", result="DENIED", reason_code=e.code)
        raise HTTPException(422, f"Not saved ({e.code})")
    audit.record_for(p, action="write_memory", target_type="memory", target_id=saved["memory_id"], authz_decision="ALLOW", result="SUCCESS", detail={"category": saved["category"], "via": "ui"})
    return saved


@app.delete("/api/memory/{memory_id}", dependencies=[Depends(csrf_guard)])
def memory_delete(memory_id: str, p: Principal = Depends(require("write_memory"))):
    if not memory.delete(p.student_id, memory_id):
        raise HTTPException(404, "Memory item not found")
    audit.record_for(p, action="delete_memory", target_type="memory", target_id=memory_id, authz_decision="ALLOW", result="SUCCESS")
    return {"deleted": memory_id}


# ---------------------------------------------------------------- student: profile overview, requests, tickets
@app.get("/api/my/profile")
def my_profile(p: Principal = Depends(require("read_profile"))):
    c = connect()
    try:
        prof = c.execute("SELECT student_id,name,programme,enrolment_status,term_start_date,registration_status FROM students WHERE student_id=?", (p.student_id,)).fetchone()
        fee = c.execute("SELECT fee_period,total_fee,amount_paid,amount_due,due_date,status FROM fee_records WHERE student_id=?", (p.student_id,)).fetchone()
        sch = c.execute("SELECT scheme,status FROM scholarship_applications WHERE student_id=?", (p.student_id,)).fetchall()
        open_t = c.execute("SELECT COUNT(*) FROM support_tickets WHERE student_id=? AND status NOT IN ('RESOLVED','CLOSED')", (p.student_id,)).fetchone()[0]
        pend = c.execute("SELECT COUNT(*) FROM pending_actions WHERE student_id=? AND state='PENDING_APPROVAL'", (p.student_id,)).fetchone()[0]
    finally:
        c.close()
    return {"profile": dict(prof), "fee": dict(fee) if fee else None, "scholarships": [dict(r) for r in sch], "open_tickets": open_t, "pending_requests": pend}


@app.get("/api/my/requests")
def my_requests(p: Principal = Depends(require("read_course_requests"))):
    return [{"action_id": r["action_id"], "type": r["action_type"], "field": r["field"], "current_value": r["current_programme"],
             "requested_value": r["requested_programme"], "state": r["state"], "created_at": r["created_at"],
             "decided_at": r["decided_at"], "decision_note": r["decision_note"]} for r in approvals.list_actions(student_id=p.student_id)]


@app.get("/api/my/course-requests")        # kept for older clients
def my_course_requests(p: Principal = Depends(require("read_course_requests"))):
    return [{"action_id": r["action_id"], "requested_programme": r["requested_programme"], "state": r["state"],
             "created_at": r["created_at"], "decided_at": r["decided_at"]} for r in approvals.list_actions(student_id=p.student_id)]


@app.get("/api/my/tickets")
def my_tickets(p: Principal = Depends(require("read_tickets"))):
    c = connect()
    try:
        rows = c.execute("SELECT t.ticket_id,t.category,t.description,t.status,t.created_at,t.updated_at,"
                         "(SELECT note FROM ticket_updates u WHERE u.ticket_id=t.ticket_id ORDER BY u.id DESC LIMIT 1) AS staff_note "
                         "FROM support_tickets t WHERE t.student_id=? ORDER BY t.created_at DESC LIMIT 50", (p.student_id,)).fetchall()
    finally:
        c.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- conversations (own only): list, open, delete, export
def _own_conversation(cid: str, p: Principal):
    c = connect()
    try:
        row = c.execute("SELECT conversation_id,created_at FROM conversations WHERE conversation_id=? AND student_id=?", (cid, p.student_id)).fetchone()
        if not row:
            raise HTTPException(404, "Conversation not found")
        msgs = c.execute("SELECT role,content,created_at FROM messages WHERE conversation_id=? ORDER BY id", (cid,)).fetchall()
    finally:
        c.close()
    return dict(row), [dict(m) for m in msgs]


@app.get("/api/conversations")
def conversations(p: Principal = Depends(require("read_conversations"))):
    c = connect()
    try:
        rows = c.execute(
            "SELECT c.conversation_id, c.created_at, COUNT(m.id) AS messages, MAX(m.created_at) AS updated_at, "
            "(SELECT content FROM messages x WHERE x.conversation_id=c.conversation_id AND x.role='user' ORDER BY x.id LIMIT 1) AS first_question "
            "FROM conversations c JOIN messages m ON m.conversation_id=c.conversation_id WHERE c.student_id=? "
            "GROUP BY c.conversation_id ORDER BY updated_at DESC LIMIT 40", (p.student_id,)).fetchall()
    finally:
        c.close()
    return [{"conversation_id": r["conversation_id"], "title": (r["first_question"] or "New chat")[:70], "messages": r["messages"],
             "updated_at": r["updated_at"]} for r in rows]


@app.get("/api/conversations/{cid}")
def conversation_open(cid: str, p: Principal = Depends(require("read_conversations"))):
    meta, msgs = _own_conversation(cid, p)
    return {**meta, "messages": msgs}


@app.delete("/api/conversations/{cid}", dependencies=[Depends(csrf_guard)])
def conversation_delete(cid: str, p: Principal = Depends(require("write_conversations"))):
    _own_conversation(cid, p)
    with tx() as c:
        c.execute("DELETE FROM messages WHERE conversation_id=?", (cid,))
        c.execute("DELETE FROM conversations WHERE conversation_id=? AND student_id=?", (cid, p.student_id))
    audit.record_for(p, action="delete_conversation", target_type="conversation", target_id=cid, authz_decision="ALLOW", result="SUCCESS")
    return {"deleted": cid}


@app.get("/api/conversations/{cid}/export")
def conversation_export(cid: str, format: str = "md", include_trace: bool = False, p: Principal = Depends(require("read_conversations"))):
    if format not in ("md", "txt"):
        raise HTTPException(422, "format must be md or txt")
    meta, msgs = _own_conversation(cid, p)
    runs_ = list(reversed(_runs_query(p, 200, conversation_id=cid)))          # oldest first, one run per assistant answer
    lines = [f"# Student Support Desk chat" if format == "md" else "STUDENT SUPPORT DESK CHAT",
             f"Student: {p.display_name} ({p.student_id})", f"Started: {meta['created_at']}", f"Exported: {now_iso()}", ""]
    ai = 0
    for m in msgs:
        who = p.display_name if m["role"] == "user" else "Assistant"
        stamp = m["created_at"].replace("T", " ")[:16]
        lines.append(f"**{who}** ({stamp})" if format == "md" else f"[{stamp}] {who}:")
        lines += [m["content"], ""]
        if m["role"] == "assistant":
            if include_trace and ai < len(runs_):
                r = runs_[ai]
                tools_ = ", ".join(t["name"] for t in json.loads(r["tools_used"] or "[]")) or "none"
                tk = f"{r['input_tokens']}/{r['output_tokens']}" if r["input_tokens"] is not None else "n/a"
                lines += [f"_Trace {r['run_id']}: {round(r['latency_ms'])} ms, tokens in/out {tk}, {r['model_calls']} model calls, tools: {tools_}, {r['provider_mode']}/{r['model']}_" if format == "md"
                          else f"  (trace {r['run_id']}: {round(r['latency_ms'])} ms, tokens {tk}, {r['model_calls']} model calls, tools: {tools_})", ""]
            ai += 1
    audit.record_for(p, action="export_conversation", target_type="conversation", target_id=cid, authz_decision="ALLOW", result="SUCCESS", detail={"format": format})
    body = "\n".join(lines)
    return PlainTextResponse(body, headers={"Content-Disposition": f'attachment; filename="support-chat-{cid}.{format}"'},
                             media_type="text/markdown" if format == "md" else "text/plain")


# ---------------------------------------------------------------- reviewer: approvals + audit
class DecisionIn(BaseModel):
    decision: str = Field(pattern=r"^(approve|reject)$")
    note: str | None = Field(default=None, max_length=200)


@app.get("/api/approvals")
def approvals_list(state: str = "PENDING_APPROVAL", p: Principal = Depends(require("list_approvals"))):
    return approvals.list_actions(states=(state,) if state != "ALL" else None)


@app.post("/api/approvals/{action_id}/decision", dependencies=[Depends(csrf_guard)])
def approvals_decide(action_id: str, body: DecisionIn, p: Principal = Depends(current_principal)):
    try:
        return approvals.decide(p, action_id, body.decision, body.note)
    except approvals.ApprovalError as e:
        raise HTTPException(e.status, e.code)


class TicketStatusIn(BaseModel):
    status: str = Field(max_length=30)
    note: str | None = Field(default=None, max_length=300)


@app.get("/api/tickets")
def tickets_list(status: str | None = None, p: Principal = Depends(require("list_tickets"))):
    return tickets_admin.list_tickets(status)


@app.post("/api/tickets/{ticket_id}/status", dependencies=[Depends(csrf_guard)])
def tickets_update(ticket_id: str, body: TicketStatusIn, p: Principal = Depends(current_principal)):
    try:
        return tickets_admin.update_status(p, ticket_id, body.status, body.note)
    except tickets_admin.TicketError as e:
        raise HTTPException(e.status, e.code)


@app.get("/api/audit")
def audit_list(limit: int = 100, action: str | None = None, result: str | None = None, p: Principal = Depends(require("read_audit"))):
    return audit.list_events(limit=limit, action=action, result=result)


app.mount("/assets", StaticFiles(directory=STATIC), name="assets")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
