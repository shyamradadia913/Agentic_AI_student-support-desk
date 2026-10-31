"""Staff side of support tickets (VIA-SUP-002: only support staff change status). Every change is audited."""
import uuid

from . import audit
from .authz import Principal, authorize
from .db import connect, tx
from .redact import redact
from .timeutil import now_iso

STATUSES = ("OPEN", "IN_PROGRESS", "WAITING_FOR_STUDENT", "RESOLVED", "CLOSED")


class TicketError(Exception):
    def __init__(self, code: str, status: int):
        super().__init__(code)
        self.code, self.status = code, status


def list_tickets(status: str | None = None, db_path=None) -> list[dict]:
    q = ("SELECT t.*, s.name AS student_name, "
         "(SELECT note FROM ticket_updates u WHERE u.ticket_id=t.ticket_id ORDER BY u.id DESC LIMIT 1) AS staff_note "
         "FROM support_tickets t JOIN students s ON s.student_id=t.student_id")
    args: list = []
    if status:
        q += " WHERE t.status=?"; args.append(status)
    q += " ORDER BY CASE t.status WHEN 'OPEN' THEN 0 WHEN 'IN_PROGRESS' THEN 1 WHEN 'WAITING_FOR_STUDENT' THEN 2 ELSE 3 END, t.created_at DESC LIMIT 200"
    c = connect(db_path)
    try:
        return [dict(r) for r in c.execute(q, args)]
    finally:
        c.close()


def update_status(p: Principal | None, ticket_id: str, new_status: str, note: str | None, db_path=None) -> dict:
    def deny(code, status, result="DENIED", authz="DENY"):
        audit.record_for(p, action="update_ticket", target_type="support_ticket", target_id=ticket_id, authz_decision=authz,
                         result=result, reason_code=code, db_path=db_path)
        raise TicketError(code, status)

    if not authorize(p, "update_ticket").allowed:
        deny("role_not_permitted", 403)
    if new_status not in STATUSES:
        raise TicketError("invalid_status", 422)
    clean_note = redact((note or "").strip()).text[:300] or None
    with tx(db_path) as c:
        t = c.execute("SELECT status FROM support_tickets WHERE ticket_id=?", (ticket_id,)).fetchone()
        if t is None:
            c.rollback()
            deny("not_found", 404, "NOT_FOUND", "ALLOW")
        now = now_iso()
        c.execute("UPDATE support_tickets SET status=?, updated_at=? WHERE ticket_id=?", (new_status, now, ticket_id))
        c.execute("INSERT INTO ticket_updates (ticket_id,actor_user_id,from_status,to_status,note,created_at) VALUES (?,?,?,?,?,?)",
                  (ticket_id, p.user_id, t["status"], new_status, clean_note, now))
        audit.record_for(p, action="update_ticket", target_type="support_ticket", target_id=ticket_id, authz_decision="ALLOW",
                         result="SUCCESS", reason_code=f"{t['status']}->{new_status}", conn=c)
    return {"ticket_id": ticket_id, "status": new_status}
