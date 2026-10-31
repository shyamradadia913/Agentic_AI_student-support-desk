# Student Support Agent — Assignments 1 & 2

A chat agent for a **fictional** institute. It decides when to answer directly,
call a tool, retrieve handbook policy or use remembered preferences (Assignment 1), and does so safely: signed-in identity,
per-student privacy, human approval for sensitive actions and an append-only audit trail (Assignment 2).
All data and policies are synthetic (`data/policies/handbook.md`, `app/seed.py`).

Git tags: `assignment-1` (baseline) and `assignment-2` (final). Assignment 2 extends the same code; nothing was restarted.

## Architecture

```
Browser (static/index.html) -- session cookie --> FastAPI (app/main.py)
   login / chat / reviewer console                 |-- authn (app/authn.py): hashed passwords, lockout, sessions
                                                   |-- authz (app/authz.py): default-deny rules, in code
                                                   '-- agent loop (app/agent.py)
                                                        |-- context: system rules + selected memory + last 4 msgs + question
                                                        |-- provider (app/llm.py): Groq (live) | Mock (offline stand-in)
                                                        |-- tools (app/tools.py): typed, validated, authorize + audit every private access
                                                        |-- policy RAG (app/retrieval.py): BM25 over per-policy-ID chunks
                                                        |-- memory (app/memory.py): allowlist, owner-scoped
                                                        |-- approvals (app/approvals.py): pending -> reviewer decision -> exactly-once execute
                                                        |-- redaction (app/redact.py) before storage / trace / model / audit
                                                        |-- audit (app/audit.py): append-only audit_events
                                                        '-- tracing (app/tracing.py): agent_runs + agent_events
```

**Agent loop:** model returns a tool request or a final answer; the application validates (known tool, strict pydantic args),
authorizes, executes, appends the observation and calls the model again. Limits: 8 steps, 5 s tool timeout, duplicate-call guard.

**Tools (13):** profile, fee status, scholarship status, policy search, create/list/get support requests, remember preference,
next working day, payment-deadline check, `request_course_change` and `request_profile_correction` (both pending only), `get_my_course_change_requests`. The model has no approve/execute tool.

**Context selection:** only the question, relevant memory items, the last 4 messages of the current conversation, and
policy passages returned by `search_student_policy` (top 3, relevance-thresholded, or `abstain`).

**Assignment 2 controls** are described in `docs/privacy_and_approval_rules.md`; the threat model with matching tests is in
`docs/threat_model.md`.

## Product features (v3)

- **Student workspace:** chat with formatted answers (bold, lists, tables, code), live "what the agent is doing" while it works, follow-up chips (written by the model
  for the conversation, validated in code, with a rule-based fallback), status cards, handbook passages on click, helpful / not-helpful rating, light/dark toggle.
- **Two tracing spaces:** (1) under every answer: latency, tokens in/out, model calls, tools, with an expandable step-by-step trace; (2) the **Activity** tab lists all of
  the student's queries with the same metrics. Reviewers get a **Tracing** console across all students: totals, avg/p50/p95 latency, tokens, model calls, tool usage,
  per-student and per-status filters, feedback counts, drill-down per query.
- **Profile tab:** the student's record (name, programme, status), fees and scholarship. It refreshes every 12 s and after each chat, and toasts when an approved change lands.
- **Requests tab:** support tickets (with the support team's notes) and change requests (course change, name correction) with their approval state.
- **Memory tab:** a preferences form, plus the topics the assistant has noted (never statuses or content). Everything is deletable. **Chats tab:** past conversations to reopen, continue or delete.
- **Download chat:** Markdown or text (optionally with the per-answer trace) from the server, or Print / Save as PDF from the browser.
- **Reviewer console:** approvals for course changes and name corrections; the **support-ticket queue** (update status, add a note the student sees); tracing; the audit log.
- **Multi-step agent behaviour (default):** the payment-portal question is solved by chaining tools: `search_student_policy` + `get_my_fee_status` + `get_my_support_requests`
  (requested together), then `get_next_working_day(due_date)`, which depends on the fee record, then the answer. That is 4 tool calls over 3-5 model calls (3 when the model requests the
  independent lookups in parallel, 5 when it goes one at a time). Every other query is 1 tool call. Set `PAYMENT_TOOL_MODE=composite` in `.env` to switch to one composite tool
  (`check_payment_deadline`: 1 tool call, 2 model calls), a latency optimisation that shows less agent behaviour. Eval reports now include model calls and tool calls per case.
- Latency levers: earlier turns are truncated in the prompt; tool schemas are compact; independent lookups are requested together; provider rate-limit errors are reported with a safe reason.

## UI and follow-up questions

- After each answer the agent offers up to three **follow-up questions** as clickable chips (`app/followups.py`). They are rule-based and built from what the
  agent just did (fee status, scholarship status, portal-rule answer, ticket created, policy topic, abstention, denial, secret redaction), so they cost no extra model
  call, add no latency or tokens, and cannot hallucinate. They only ever refer to the student's own account or the handbook, never repeat the question just asked, and
  never contain another student's ID. Every static suggestion is executed against the agent in a test (`tests/test_followups.py`).
- Status cards appear inline for a pending course-change request, a created ticket, and a denied access attempt. Cited policy IDs open the handbook passage.
- The inspector has Trace / Requests / Memory tabs; the reviewer console has Approvals and a filterable Audit log. Light and dark themes follow the OS setting;
  the layout works on phones. A hint appears if you start typing an OTP, password or card number.
- Optional browser smoke test: `pip install playwright`, `playwright install chromium`, start the app on port 8765, then `python scripts_ui_smoke.py`.

## Run (Windows PowerShell)

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env          # then edit .env and set GROQ_API_KEY (never commit it)
python -m app.seed              # (re)creates demo data
python -m uvicorn app.main:app --reload
# open http://127.0.0.1:8000
```
Without `GROQ_API_KEY` the app runs in **mock mode** (rule-based stand-in, clearly bannered).

**Demo credentials (fictional, seed data only):** students `s1001`…`s1006` with passwords `Demo#1001!`…`Demo#1006!`
(the digits match the username); reviewer `reviewer1` / `Review#2026!`.

### Walkthrough of the required scenarios
1. Sign in as `s1001`: "What is my current fee status?" → answered from your record (audit: `read_fee ALLOW`).
2. "Show me the fee status of S1003" → blocked (audit: `read_fee DENY not_owner`).
3. "I want to change my programme to Business Analytics" → request is **pending**, programme unchanged.
4. Sign out, sign in as `reviewer1`: Reject → nothing executes. Make a new request and Approve → executes once; Approve again → refused.
5. As a student: "my otp is 482913, please remember it" → value is not stored anywhere; only a `sensitive_input_redacted` audit row exists.

## Tests and evaluation

```powershell
python -m pytest -q                                   # 262 tests, no network
python -m eval.run --mode groq --dataset eval.golden_a2 --label assignment2 --sleep 1        # full A2 set (60 cases), live model
python -m eval.run --mode groq --dataset eval.golden_a1 --label a2_regression --sleep 1      # the 37 A1 cases on A2 code
python -m eval.export_evidence --mode groq            # sample traces + audit rows for the required scenarios
```
Like-for-like Assignment 1 baseline (same 37 cases on the A1 code):
```powershell
git worktree add ..\viaa-a1 assignment-1
copy .env ..\viaa-a1\.env
cd ..\viaa-a1
python -m eval.run --mode groq --dataset eval.golden_a1 --label assignment1 --sleep 1
cd ..\<this-repo-folder>
python -m eval.compare --a1 ..\viaa-a1\outputs\assignment1_groq_summary.json --a2 outputs\a2_regression_groq_summary.json --a2full outputs\assignment2_groq_summary.json --out outputs\comparison_groq.md
```
Outputs (`outputs/`): per-case JSONL, summary JSON, Markdown report tables, comparison table, sample trace/audit evidence.
Metrics: task success, tool choice, retrieval hit@k, abstention, citation validity, redaction, privacy blocked, unsafe actions gated,
audit evidence, trace completeness, latency (mean/p50/p95), tokens (provider-reported; `n/a` if absent).

## Known limitations
- Retrieval is lexical (case R07 fails on purpose); the live model's own keyword queries usually avoid this.
- Secret detection is pattern-based; memory is also allowlist-only as a second layer.
- Mock-mode numbers only exercise the pipeline and are not model-quality evidence. Their latency is meaningless as an absolute value.
- Simulation scope: no MFA, password reset, TLS, or external identity provider.
