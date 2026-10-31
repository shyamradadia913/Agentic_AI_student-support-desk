# Privacy, permission and approval rules (Assignment 2)

All rules are enforced in application code (`app/authz.py`, `app/tools.py`, `app/approvals.py`, `app/main.py`).
The prompt tells the model how to behave, but nothing depends on the model obeying it.

## 1. Identity
- Sign-in: `POST /api/auth/login` checks a PBKDF2-SHA256 salted password hash. Unknown users take the same code path and time, and all failures return the same message (no user enumeration).
- Five failed attempts lock the account for 5 minutes. Every login, failure and lock is audited.
- A successful login creates a server-side session. The browser receives a random token in an `HttpOnly`, `SameSite=Lax` cookie; the database stores only its SHA-256.
- The acting student is always taken from that session. `/api/chat` has no `student_id` field; an extra field in the request body is ignored.
- State-changing requests also need the `X-Requested-With: viaa-ui` header (CSRF defence in depth).

## 2. Data privacy (own data only)
- `authorize(principal, action, target)` is default-deny. Students may use the "own data" actions only when `target == principal.student_id`. Reviewers get no blanket read access to student records.
- Private-record tools accept an optional `student_id` only so that a request for someone else's data reaches the backend and is **denied and audited**. A denied call returns a fixed message that is identical whether or not the other student exists.
- A ticket belonging to someone else returns the same `not_found` as a missing ticket, and the attempt is audited as a denial.
- Run traces, memory and conversations are scoped to the owner; a foreign run ID returns 404.

## 3. Sensitive action: course change needs human approval
1. The model can only call `request_course_change`. It creates a `PENDING_APPROVAL` row and **does not change the programme**.
2. The model has no tool to approve, reject or execute. Unknown tool names are rejected.
3. Only an authenticated `reviewer`/`admin` may decide (`POST /api/approvals/{id}/decision`). Students get 403; the requester can never decide their own request.
4. Before executing, the code checks inside one transaction: the action exists, is still `PENDING_APPROVAL` (compare-and-set), has not expired (7 days), and the student's programme is still what it was when requested.
5. **Reject** leaves the record unchanged. **Approve** updates the programme and marks the action `EXECUTED` in the same transaction as its audit rows, so a repeat approval gets 409 and cannot execute twice.
6. Duplicate pending requests for the same student are refused.

## 4. Sensitive input
- OTPs, passwords, PINs, tokens and valid card numbers are redacted before the message is stored, traced, sent to the model, put in a ticket or written to audit. The agent only records that a redaction happened (`sensitive_input_redacted`, with the kind, never the value).
- Memory is allowlist-only, so nothing outside five low-sensitivity categories can persist even if redaction misses a format.

## 5. Audit
`audit_events` is append-only (database triggers abort UPDATE/DELETE). Each row has: event ID, server timestamp, actor and role, run ID, action, target type/ID, authorization decision (ALLOW/DENY), approval status, result and reason code. Audit rows hold no secrets or copies of private records and are readable only by reviewers (`GET /api/audit`).
