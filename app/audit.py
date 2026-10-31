"""Append-only audit trail (VIA-AUD-001/002). Never stores secrets; detail is redacted and size-capped."""
import json
import uuid

from .db import tx
from .redact import redact
from .timeutil import now_iso


def _clean(detail):
    if not detail:
        return None
    return redact(json.dumps(detail, default=str, ensure_ascii=False)).text[:600]


def record(*, actor_user_id, actor_role, action, authz_decision, result, target_type=None, target_id=None,
           approval_status="NONE", reason_code=None, run_id=None, detail=None, db_path=None, conn=None) -> str:
    eid = "aud-" + uuid.uuid4().hex[:12]
    row = (eid, now_iso(), actor_user_id, actor_role, run_id, action, target_type,
           (target_id or None) and str(target_id)[:60], authz_decision, approval_status, result, reason_code, _clean(detail))
    sql = ("INSERT INTO audit_events (event_id,ts,actor_user_id,actor_role,run_id,action,target_type,target_id,"
           "authz_decision,approval_status,result,reason_code,detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)")
    if conn is not None:                 # join the caller's transaction (atomic with the change it describes)
        conn.execute(sql, row)
    else:
        with tx(db_path) as c:
            c.execute(sql, row)
    return eid


def record_for(p, **kw):
    return record(actor_user_id=getattr(p, "user_id", None), actor_role=getattr(p, "role", None), **kw)


def list_events(db_path=None, limit=100, action=None, result=None):
    from .db import connect
    q, args = "SELECT * FROM audit_events WHERE 1=1", []
    if action:
        q += " AND action=?"; args.append(action)
    if result:
        q += " AND result=?"; args.append(result)
    q += " ORDER BY ts DESC, rowid DESC LIMIT ?"; args.append(min(limit, 500))
    c = connect(db_path)
    try:
        return [dict(r) for r in c.execute(q, args)]
    finally:
        c.close()
