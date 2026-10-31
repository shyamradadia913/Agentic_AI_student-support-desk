"""Sensitive-action workflow: request -> PENDING_APPROVAL -> reviewer decision -> exactly-once execution.

The model can only *request*. Approval/rejection/execution are plain code behind role checks, a compare-and-set on
state, an expiry check and a stale-record check, all inside one transaction with the audit rows.
"""
import uuid
from datetime import date, datetime, timedelta

from . import audit, config
from .authz import Principal, authorize
from .db import connect, tx
from .timeutil import IST, now_iso

PROGRAMMES = ["PGDM - AI & Data Science", "PGDM - Big Data Analytics", "PGDM - Business Analytics"]


class ApprovalError(Exception):
    def __init__(self, code: str, status: int):
        super().__init__(code)
        self.code, self.status = code, status


def _deny(p, action_id, code, status, result="DENIED", authz="DENY", approval="NONE", db_path=None):
    audit.record_for(p, action="decide_approval", target_type="pending_action", target_id=action_id, authz_decision=authz,
                     result=result, approval_status=approval, reason_code=code, db_path=db_path)
    raise ApprovalError(code, status)


FIELDS = {  # action_type -> (students column, audit action names)
    "COURSE_CHANGE": ("programme", "request_course_change", "execute_course_change"),
    "PROFILE_CORRECTION": ("name", "request_profile_correction", "execute_profile_correction"),
}


def create_course_change(p: Principal, student_id: str, current: str, requested: str, reason: str, run_id: str | None, db_path=None) -> dict:
    return create_action("COURSE_CHANGE", p, student_id, current, requested, reason, run_id, db_path)


def create_action(action_type: str, p: Principal, student_id: str, current: str, requested: str, reason: str, run_id: str | None, db_path=None) -> dict:
    field, req_audit, _ = FIELDS[action_type]
    now = datetime.now(IST)
    with tx(db_path) as c:
        ts = c.execute("SELECT term_start_date FROM students WHERE student_id=?", (student_id,)).fetchone()["term_start_date"]
        within = None
        if ts and action_type == "COURSE_CHANGE":
            within = int(0 <= (now.date() - date.fromisoformat(ts)).days <= 14)
        n = c.execute("SELECT COALESCE(MAX(CAST(SUBSTR(action_id,5) AS INTEGER)),0)+1 FROM pending_actions").fetchone()[0]
        aid = f"ACT-{n:04d}"
        c.execute("INSERT INTO pending_actions (action_id,action_type,field,requester_user_id,student_id,current_programme,requested_programme,"
                  "reason,within_window,state,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (aid, action_type, field, p.user_id, student_id, current, requested, reason, within, "PENDING_APPROVAL",
                   now_iso(), (now + timedelta(days=config.ACTION_EXPIRY_DAYS)).isoformat(timespec="seconds")))
        audit.record_for(p, action=req_audit, target_type="pending_action", target_id=aid, authz_decision="ALLOW",
                         result="PENDING", approval_status="PENDING_APPROVAL", run_id=run_id,
                         detail={"to": requested}, conn=c)
    return {"action_id": aid, "within_window": within}


def decide(p: Principal | None, action_id: str, decision: str, note: str | None = None, db_path=None) -> dict:
    if decision not in ("approve", "reject"):
        raise ApprovalError("invalid_decision", 422)
    if not authorize(p, "decide_approval").allowed:
        _deny(p, action_id, "role_not_permitted", 403, db_path=db_path)
    c0 = connect(db_path)
    try:
        a = c0.execute("SELECT * FROM pending_actions WHERE action_id=?", (action_id,)).fetchone()
    finally:
        c0.close()
    if a is None:
        _deny(p, action_id, "not_found", 404, result="NOT_FOUND", authz="ALLOW", db_path=db_path)
    if a["requester_user_id"] == p.user_id:
        _deny(p, action_id, "self_approval_forbidden", 403, approval=a["state"], db_path=db_path)
    if a["state"] != "PENDING_APPROVAL":
        _deny(p, action_id, "already_decided", 409, result="CONFLICT", authz="ALLOW", approval=a["state"], db_path=db_path)
    if datetime.fromisoformat(a["expires_at"]) <= datetime.now(IST):
        with tx(db_path) as c:
            c.execute("UPDATE pending_actions SET state='EXPIRED', result_code='expired' WHERE action_id=? AND state='PENDING_APPROVAL'", (action_id,))
        _deny(p, action_id, "expired", 409, result="EXPIRED", authz="ALLOW", approval="EXPIRED", db_path=db_path)

    now = now_iso()
    with tx(db_path) as c:
        # compare-and-set: only one decision can ever move the action out of PENDING_APPROVAL
        new_state = "REJECTED" if decision == "reject" else "APPROVED"
        cur = c.execute("UPDATE pending_actions SET state=?, decided_by=?, decided_at=?, decision_note=? "
                        "WHERE action_id=? AND state='PENDING_APPROVAL'", (new_state, p.user_id, now, (note or "")[:200], action_id))
        if cur.rowcount != 1:
            c.rollback()
            _deny(p, action_id, "already_decided", 409, result="CONFLICT", authz="ALLOW", db_path=db_path)
        audit.record_for(p, action="decide_approval", target_type="pending_action", target_id=action_id, authz_decision="ALLOW",
                         result="SUCCESS", approval_status=new_state, reason_code=decision, conn=c)
        if decision == "reject":
            c.execute("UPDATE pending_actions SET result_code='rejected_no_change' WHERE action_id=?", (action_id,))
            return {"action_id": action_id, "state": "REJECTED"}
        # ---- execution (same transaction as the approval)
        col, _, exec_audit = FIELDS[a["action_type"]]          # column name comes from a fixed map, never from user input
        cur = c.execute(f"UPDATE students SET {col}=? WHERE student_id=? AND {col}=?",
                        (a["requested_programme"], a["student_id"], a["current_programme"]))
        if cur.rowcount != 1:                                 # record changed since the request -> do not execute
            c.execute("UPDATE pending_actions SET state='FAILED', result_code='stale_record' WHERE action_id=?", (action_id,))
            audit.record_for(p, action=exec_audit, target_type="pending_action", target_id=action_id, authz_decision="ALLOW",
                             result="FAILED", approval_status="FAILED", reason_code="stale_record", conn=c)
            return {"action_id": action_id, "state": "FAILED", "reason": "stale_record"}
        c.execute("UPDATE pending_actions SET state='EXECUTED', executed_at=?, result_code='ok' WHERE action_id=?", (now, action_id))
        audit.record_for(p, action=exec_audit, target_type="pending_action", target_id=action_id, authz_decision="ALLOW",
                         result="SUCCESS", approval_status="EXECUTED", conn=c)
    return {"action_id": action_id, "state": "EXECUTED"}


def list_actions(student_id: str | None = None, states: tuple | None = None, db_path=None) -> list[dict]:
    q, args = "SELECT * FROM pending_actions WHERE 1=1", []
    if student_id:
        q += " AND student_id=?"; args.append(student_id)
    if states:
        q += f" AND state IN ({','.join('?' * len(states))})"; args += list(states)
    q += " ORDER BY created_at DESC LIMIT 50"
    c = connect(db_path)
    try:
        return [dict(r) for r in c.execute(q, args)]
    finally:
        c.close()
