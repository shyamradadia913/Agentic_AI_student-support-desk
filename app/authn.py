"""Authentication: PBKDF2 password hashing, lockout, server-side sessions (only a SHA-256 of the token is stored)."""
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from . import audit, config
from .authz import Principal
from .db import connect, tx
from .timeutil import IST, now_iso


class AuthError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def hash_password(pw: str, iters: int | None = None) -> str:
    iters = iters or config.PBKDF2_ITERS
    salt = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, iters)
    return f"pbkdf2${iters}${salt.hex()}${h.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, iters, salt, h = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(iters))
        return hmac.compare_digest(calc.hex(), h)
    except Exception:
        return False


_DUMMY = None


def _now() -> datetime:
    return datetime.now(IST)


def login(username: str, password: str, db_path=None) -> tuple[str, Principal]:
    global _DUMMY
    username = (username or "").strip().lower()[:40]
    c = connect(db_path)
    try:
        u = c.execute("SELECT u.*, st.name AS student_name FROM users u LEFT JOIN students st ON st.student_id=u.student_id "
                      "WHERE u.username=?", (username,)).fetchone()
    finally:
        c.close()

    def deny(code, uid=None, role=None):
        audit.record(actor_user_id=uid, actor_role=role, action="login", target_type="user", target_id=username,
                     authz_decision="DENY", result="DENIED", reason_code=code, db_path=db_path)
        raise AuthError("invalid_credentials" if code != "locked" else "locked")

    if u is None:
        _DUMMY = _DUMMY or hash_password("x")            # equalise timing for unknown users
        verify_password(password, _DUMMY)
        deny("unknown_user")
    if u["locked_until"] and datetime.fromisoformat(u["locked_until"]) > _now():
        deny("locked", u["user_id"], u["role"])
    if not verify_password(password, u["password_hash"]):
        fails = u["failed_logins"] + 1
        lock = (_now() + timedelta(minutes=config.LOGIN_LOCK_MINUTES)).isoformat(timespec="seconds") if fails >= config.LOGIN_MAX_FAILURES else None
        with tx(db_path) as c:
            c.execute("UPDATE users SET failed_logins=?, locked_until=? WHERE user_id=?", (0 if lock else fails, lock, u["user_id"]))
        deny("bad_password", u["user_id"], u["role"])
    token = secrets.token_urlsafe(32)
    th = hashlib.sha256(token.encode()).hexdigest()
    with tx(db_path) as c:
        c.execute("UPDATE users SET failed_logins=0, locked_until=NULL WHERE user_id=?", (u["user_id"],))
        c.execute("INSERT INTO sessions VALUES (?,?,?,?)", (th, u["user_id"], now_iso(),
                  (_now() + timedelta(hours=config.SESSION_TTL_HOURS)).isoformat(timespec="seconds")))
    p = Principal(u["user_id"], u["role"], u["student_id"], u["username"], u["student_name"] or u["display_name"] or u["username"])
    audit.record_for(p, action="login", target_type="user", target_id=u["username"], authz_decision="ALLOW",
                     result="SUCCESS", db_path=db_path)
    return token, p


def principal_from_token(token: str | None, db_path=None) -> Principal | None:
    if not token:
        return None
    th = hashlib.sha256(token.encode()).hexdigest()
    c = connect(db_path)
    try:
        r = c.execute("SELECT u.*, s.expires_at, st.name AS student_name FROM sessions s JOIN users u ON u.user_id=s.user_id "
                      "LEFT JOIN students st ON st.student_id=u.student_id WHERE s.token_hash=?", (th,)).fetchone()
    finally:
        c.close()
    if not r or datetime.fromisoformat(r["expires_at"]) <= _now():
        return None
    return Principal(r["user_id"], r["role"], r["student_id"], r["username"], r["student_name"] or r["display_name"] or r["username"])


def logout(token: str | None, db_path=None) -> None:
    if token:
        with tx(db_path) as c:
            c.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
