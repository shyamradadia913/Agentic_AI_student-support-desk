"""Fictional demo data. All names, amounts and dates are invented (see handbook VIA-FEE-002)."""
from .authn import hash_password
from .db import init_db, tx

STUDENTS = [
    # id, name, programme, enrolment, term_start, registration
    ("S1001", "Aarav Mehta", "PGDM - AI & Data Science", "ACTIVE", "2026-10-12", "CONFIRMED"),
    ("S1002", "Diya Shah", "PGDM - Big Data Analytics", "ACTIVE", "2026-10-12", "CONFIRMED"),
    ("S1003", "Rohan Patel", "PGDM - AI & Data Science", "ACTIVE", "2026-10-12", "CONFIRMED"),
    ("S1004", "Neha Iyer", "PGDM - Business Analytics", "ACTIVE", "2026-10-12", "CONFIRMED"),
    ("S1005", "Kabir Singh", "PGDM - Big Data Analytics", "ACTIVE", "2026-10-12", "IN_PROGRESS"),
    ("S1006", "Meera Nair", "PGDM - Business Analytics", "ON_LEAVE", None, "UNKNOWN"),
]

# id, period, total, paid, due, due_date, status, updated
FEES = [
    ("S1001", "2026-27 Annual", 260000, 260000, 0, "2026-10-20", "PAID", "2026-10-18T10:00:00+05:30"),
    ("S1002", "2026-27 Annual", 245000, 120000, 125000, "2026-11-15", "PARTIALLY_PAID", "2026-10-10T09:30:00+05:30"),
    ("S1003", "2026-27 Annual", 260000, 0, 260000, "2026-10-30", "UNDER_REVIEW", "2026-10-29T14:10:00+05:30"),
    ("S1004", "2026-27 Annual", 230000, 0, 230000, "2026-10-28", "DUE", "2026-10-01T09:00:00+05:30"),
    ("S1005", "2026-27 Annual", 245000, 100000, 145000, "2026-12-01", "PARTIALLY_PAID", "2026-10-05T11:00:00+05:30"),
    ("S1006", "2026-27 Annual", 230000, 230000, 0, "2026-10-20", "PAID", "2026-10-02T09:00:00+05:30"),
]

# student, scheme, status, submitted_on, note
SCHOLARSHIPS = [
    ("S1001", "Merit Demonstration Award", "APPROVED", "2026-09-15", "Decision recorded; disbursement not yet recorded."),
    ("S1002", "Access Demonstration Grant", "UNDER_REVIEW", "2026-10-14", None),
    ("S1003", None, "NOT_STARTED", None, None),
    ("S1004", None, "NOT_STARTED", None, None),
    ("S1005", "Merit Demonstration Award", "INFORMATION_REQUIRED", "2026-10-01", None),
    ("S1006", None, "NOT_STARTED", None, None),
]

# ticket_id, student, category, description, status, priority, created_at
TICKETS = [
    ("TKT-0001", "S1001", "LIBRARY", "Need access to analytics e-books.", "OPEN", "NORMAL", "2026-10-15T11:20:00+05:30"),
    # S1003 reported a portal failure BEFORE the 23:59 cutoff on the due date (2026-10-30).
    ("TKT-0002", "S1003", "FEE_PAYMENT", "Payment portal failed during fee payment; reference not generated.", "OPEN", "HIGH", "2026-10-29T14:05:00+05:30"),
]


# Demo-only credentials for the fictional deployment (documented in README). Real systems never ship these.
REVIEWER = ("u-reviewer1", "reviewer1", "Review#2026!", "reviewer", None, "Reviewer One")


def demo_password(student_id: str) -> str:
    return f"Demo#{student_id[1:]}!"


def seed(path: str | None = None) -> None:
    """Idempotent full reset of demo data (users, sessions, runs, memory, approvals, audit)."""
    init_db(path)
    with tx(path) as c:
        c.execute("DROP TABLE IF EXISTS audit_events")       # drops its append-only triggers too; recreated below
        for t in ("run_feedback", "ticket_updates", "agent_events", "agent_runs", "messages", "conversations", "memory_items", "sessions",
                  "pending_actions", "users", "fee_records", "scholarship_applications", "support_tickets", "students"):
            c.execute(f"DELETE FROM {t}")
        c.executemany("INSERT INTO students VALUES (?,?,?,?,?,?)", STUDENTS)
        c.executemany("INSERT INTO fee_records VALUES (?,?,?,?,?,?,?,?)", FEES)
        c.executemany(
            "INSERT INTO scholarship_applications (student_id,scheme,status,submitted_on,note,last_updated) "
            "VALUES (?,?,?,?,?,'2026-10-18T10:00:00+05:30')", SCHOLARSHIPS)
        c.executemany("INSERT INTO support_tickets VALUES (?,?,?,?,?,?,?,?)", [(*t, t[-1]) for t in TICKETS])
        for sid, name, *_ in STUDENTS:
            c.execute("INSERT INTO users (user_id,username,password_hash,role,student_id,display_name) VALUES (?,?,?,?,?,?)",
                      (f"u-{sid.lower()}", sid.lower(), hash_password(demo_password(sid)), "student", sid, name))
        uid, uname, pw, role, sid, dn = REVIEWER
        c.execute("INSERT INTO users (user_id,username,password_hash,role,student_id,display_name) VALUES (?,?,?,?,?,?)",
                  (uid, uname, hash_password(pw), role, sid, dn))
    init_db(path)                                             # recreate audit table + triggers


if __name__ == "__main__":
    seed()
    print("Seeded demo data.")
