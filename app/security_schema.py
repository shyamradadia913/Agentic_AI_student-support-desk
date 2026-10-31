"""Assignment-2 tables: identity, sessions, approvals, append-only audit."""
SCHEMA_A2 = """
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('student','reviewer','admin')),
    student_id TEXT REFERENCES students(student_id),
    display_name TEXT,
    failed_logins INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL, expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_actions (
    action_id TEXT PRIMARY KEY,
    action_type TEXT NOT NULL,
    requester_user_id TEXT NOT NULL REFERENCES users(user_id),
    student_id TEXT NOT NULL REFERENCES students(student_id),
    field TEXT NOT NULL DEFAULT 'programme',            -- which record field the action changes: programme | name
    current_programme TEXT NOT NULL, requested_programme TEXT NOT NULL,   -- current / requested VALUE of that field
    reason TEXT NOT NULL, within_window INTEGER,
    state TEXT NOT NULL CHECK (state IN ('PENDING_APPROVAL','APPROVED','REJECTED','EXECUTED','FAILED','EXPIRED')),
    created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
    decided_by TEXT, decided_at TEXT, decision_note TEXT, executed_at TEXT, result_code TEXT
);
CREATE TABLE IF NOT EXISTS ticket_updates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL REFERENCES support_tickets(ticket_id),
    actor_user_id TEXT NOT NULL, from_status TEXT, to_status TEXT NOT NULL,
    note TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_feedback (
    run_id TEXT NOT NULL REFERENCES agent_runs(run_id),
    user_id TEXT NOT NULL, rating INTEGER NOT NULL CHECK (rating IN (-1, 1)), comment TEXT, created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, user_id)
);
CREATE TABLE IF NOT EXISTS audit_events (
    event_id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    actor_user_id TEXT, actor_role TEXT, run_id TEXT,
    action TEXT NOT NULL,
    target_type TEXT, target_id TEXT,
    authz_decision TEXT NOT NULL,            -- ALLOW | DENY | NOT_APPLICABLE
    approval_status TEXT NOT NULL DEFAULT 'NONE',
    result TEXT NOT NULL,                    -- SUCCESS | DENIED | FAILED | PENDING | REJECTED | EXPIRED | CONFLICT | NOT_FOUND
    reason_code TEXT, detail TEXT
);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
"""
