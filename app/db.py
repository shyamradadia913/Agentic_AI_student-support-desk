"""SQLite persistence: schema + seed. Assignment-1 tables only; Assignment-2 tables are added in app/security_schema.py."""
import sqlite3
from contextlib import contextmanager

from . import config
from .security_schema import SCHEMA_A2

SCHEMA_A1 = """
CREATE TABLE IF NOT EXISTS students (
    student_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    programme TEXT NOT NULL,
    enrolment_status TEXT NOT NULL,
    term_start_date TEXT,
    registration_status TEXT NOT NULL DEFAULT 'UNKNOWN'
);
CREATE TABLE IF NOT EXISTS fee_records (
    student_id TEXT PRIMARY KEY REFERENCES students(student_id),
    fee_period TEXT, total_fee REAL, amount_paid REAL, amount_due REAL,
    due_date TEXT, status TEXT NOT NULL, last_updated TEXT
);
CREATE TABLE IF NOT EXISTS scholarship_applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id TEXT NOT NULL REFERENCES students(student_id),
    scheme TEXT, status TEXT NOT NULL, submitted_on TEXT, note TEXT, last_updated TEXT
);
CREATE TABLE IF NOT EXISTS support_tickets (
    ticket_id TEXT PRIMARY KEY,
    student_id TEXT NOT NULL REFERENCES students(student_id),
    category TEXT NOT NULL, description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN', priority TEXT NOT NULL DEFAULT 'NORMAL',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_items (
    memory_id TEXT PRIMARY KEY,
    student_id TEXT NOT NULL REFERENCES students(student_id),
    category TEXT NOT NULL, value TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(student_id, category)
);
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY,
    student_id TEXT NOT NULL REFERENCES students(student_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_runs (
    run_id TEXT PRIMARY KEY,
    conversation_id TEXT, student_id TEXT,
    provider_mode TEXT, model TEXT,
    question TEXT, answer TEXT,
    status TEXT, stop_reason TEXT,
    started_at TEXT, latency_ms REAL,
    input_tokens INTEGER, output_tokens INTEGER, token_note TEXT,
    tools_used TEXT, policy_chunks TEXT, memory_ids TEXT, redactions TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS agent_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL, seq INTEGER NOT NULL,
    event_type TEXT NOT NULL, payload TEXT, duration_ms REAL, created_at TEXT NOT NULL
);
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def tx(path: str | None = None):
    """Transaction scope: commit on success, rollback on error."""
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _migrate(c) -> None:
    """Additive migrations for databases created by earlier versions."""
    cols = {r["name"] for r in c.execute("PRAGMA table_info(pending_actions)")}
    if cols and "field" not in cols:
        c.execute("ALTER TABLE pending_actions ADD COLUMN field TEXT NOT NULL DEFAULT 'programme'")


def init_db(path: str | None = None) -> None:
    with tx(path) as c:
        c.executescript(SCHEMA_A1)
        c.executescript(SCHEMA_A2)
        _migrate(c)
