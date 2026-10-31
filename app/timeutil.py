"""Server-side time helpers. IST is a fixed +05:30 offset (no DST), so no tz database is needed on Windows."""
from datetime import date, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def now_iso() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


def next_working_day(d: date) -> date:
    """Next Mon-Fri strictly after d. No public-holiday calendar exists (VIA-GEN-005)."""
    n = d + timedelta(days=1)
    while n.weekday() >= 5:
        n += timedelta(days=1)
    return n
