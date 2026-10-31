"""Authorization in ordinary application code (default-deny). The LLM never decides access."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Principal:
    user_id: str
    role: str                    # student | reviewer | admin
    student_id: str | None = None
    username: str = ""
    display_name: str = ""

    @staticmethod
    def for_student(student_id: str) -> "Principal":
        """Used by tests/eval harness only; the HTTP API builds principals from validated sessions."""
        return Principal(user_id=f"u-{student_id.lower()}", role="student", student_id=student_id, username=student_id.lower())


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


# Actions on the caller's OWN private data (target must equal the principal's student_id)
OWN_DATA = {"read_profile", "read_fee", "read_scholarship", "read_tickets", "read_ticket", "create_ticket",
            "write_memory", "request_course_change", "read_course_requests", "request_profile_correction",
            "read_conversations", "write_conversations"}
# Staff-only actions
STAFF = {"list_approvals", "decide_approval", "read_audit", "read_all_runs", "list_tickets", "update_ticket"}


def authorize(p: Principal | None, action: str, target_student_id: str | None = None) -> Decision:
    if p is None:
        return Decision(False, "unauthenticated")
    if action in OWN_DATA:
        if p.role != "student" or not p.student_id:
            return Decision(False, "role_not_permitted")
        target = target_student_id or p.student_id
        return Decision(True, "owner") if target == p.student_id else Decision(False, "not_owner")
    if action in STAFF:
        return Decision(True, "staff_role") if p.role in ("reviewer", "admin") else Decision(False, "role_not_permitted")
    return Decision(False, "unknown_action")
