"""LLM providers behind one interface: GroqProvider (live) and MockProvider (offline, deterministic).

The mock is NOT an LLM. It is a rule-based stand-in that drives the same tool registry and loop so the
system can be tested without credentials. Results from it must always be labelled 'mock'.
"""
import json
import re
import time
from dataclasses import dataclass, field
from datetime import date

from . import config


class ProviderError(Exception):
    def __init__(self, code: str, detail: str | None = None, retry_after: float | None = None):
        super().__init__(code)
        self.code, self.detail, self.retry_after = code, detail, retry_after     # detail = exception type + status (never the API key)


def retry_seconds(message: str, headers=None) -> float | None:
    """Seconds to wait before retrying, from Groq's 'try again in 19.5s' text or a Retry-After header."""
    try:
        if headers and headers.get("retry-after"):
            return float(headers.get("retry-after"))
    except (TypeError, ValueError):
        pass
    m = re.search(r"try again in ([0-9][0-9a-z.]*)", message or "")
    if not m:
        return None
    parts = re.findall(r"(\d+(?:\.\d+)?)(ms|m|s)", m.group(1))
    if not parts:
        return None
    unit = {"ms": 0.001, "s": 1.0, "m": 60.0}
    return sum(float(n) * unit[u] for n, u in parts)


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list = field(default_factory=list)   # [{"id","name","arguments":str}]
    finish_reason: str = "stop"
    usage: dict | None = None                         # {"input_tokens","output_tokens"} or None
    usage_note: str | None = None


def reasoning_kwargs(model: str) -> dict:
    """gpt-oss models think before answering; 'low' effort cuts hidden reasoning tokens (and latency) for simple support questions."""
    if model.startswith("openai/gpt-oss") and config.GROQ_REASONING_EFFORT in ("low", "medium", "high"):
        return {"extra_body": {"reasoning_effort": config.GROQ_REASONING_EFFORT}}
    return {}


class GroqProvider:
    name = "groq"
    _reasoning_ok = True

    def __init__(self):
        from groq import Groq
        self.model = config.GROQ_MODEL
        self.client = Groq(api_key=config.GROQ_API_KEY, timeout=30.0, max_retries=config.GROQ_MAX_RETRIES)

    def chat(self, messages: list, tools: list) -> LLMResponse:
        import groq
        last = None
        _tools = tools                                                                  # FIX: may be cleared to [] on tool_use_failed retry
        for attempt in range(2):  # one retry for Groq's occasional malformed-tool-call 400
            try:
                kw = {"tools": _tools, "tool_choice": "auto"} if _tools else {}        # no tools offered = the model must answer
                extra = reasoning_kwargs(self.model) if self._reasoning_ok else {}
                r = self.client.chat.completions.create(model=self.model, messages=messages, temperature=0, max_tokens=900, **kw, **extra)
                break
            except groq.APITimeoutError as e:
                raise ProviderError("provider_timeout", f"{type(e).__name__}")
            except groq.APIConnectionError as e:          # network blip between this PC and Groq: one quick retry
                if attempt == 0:
                    time.sleep(1.0)
                    continue
                raise ProviderError("provider_error", f"{type(e).__name__}: {str(e)[:200]}")
            except groq.RateLimitError as e:
                wait = retry_seconds(str(e), getattr(getattr(e, "response", None), "headers", None))
                if wait is not None and wait <= 20 and attempt == 0:                    # FIX: short wait → one silent retry instead of an error
                    time.sleep(wait + 0.5)
                    continue
                raise ProviderError("provider_rate_limited", f"{type(e).__name__}: {str(e)[:160]}", wait)
            except groq.AuthenticationError as e:
                raise ProviderError("provider_auth_failed", f"{type(e).__name__}: {str(e)[:160]}")
            except groq.BadRequestError as e:
                last = e
                if "reasoning" in str(e).lower() and self._reasoning_ok:               # this model/API version rejects the option: retry without it
                    self._reasoning_ok = False
                    continue
                if "tool_use_failed" in str(e) and attempt == 0:
                    _tools = []                                                         # FIX: retry without tools → model answers from existing context
                    continue
                raise ProviderError("provider_bad_request", f"{type(e).__name__}: {str(e)[:200]}")
            except groq.APIError as e:
                raise ProviderError("provider_error", f"{type(e).__name__}: {str(e)[:200]}")
        else:
            raise ProviderError("provider_bad_request", "retries exhausted")
        m = r.choices[0].message
        calls = [{"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments or "{}"}
                 for tc in (m.tool_calls or [])]
        u = getattr(r, "usage", None)
        usage = {"input_tokens": u.prompt_tokens, "output_tokens": u.completion_tokens} if u else None
        cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", None) if u else None
        if usage and cached:
            usage["cached_tokens"] = cached                      # prompt-cache hits, when the provider reports them
        return LLMResponse(m.content, calls, r.choices[0].finish_reason, usage,
                           None if usage else "provider returned no usage block")


# ------------------------------------------------------------------ mock
_PREF_LINE = re.compile(r"^- (\w+): (.+?) \[(MEM-\w+)\]", re.M)
_LANGS = ["english", "hindi", "gujarati", "marathi", "tamil", "telugu", "bengali", "kannada", "punjabi", "urdu"]


def _category_guess(t: str) -> str:
    t = t.lower()
    if re.search(r"portal|fee|pay|refund|tuition", t): return "FEE_PAYMENT"
    if re.search(r"scholarship|grant", t): return "SCHOLARSHIP"
    if re.search(r"course change|change.*(course|programme)", t): return "COURSE_CHANGE"
    if re.search(r"password|login|account|otp", t): return "IT_ACCOUNT"
    if re.search(r"library|book", t): return "LIBRARY"
    if re.search(r"marks|grade|transcript|attendance", t): return "ACADEMIC_RECORD"
    return "GENERAL_QUERY"


class MockProvider:
    """Deterministic rule-based 'model'. Decides tool calls from keywords; composes answers from tool results."""
    name = "mock"
    model = "mock-rules-v1"

    def chat(self, messages: list, tools: list) -> LLMResponse:
        system = messages[0]["content"]
        last_user_idx = max(i for i, m in enumerate(messages) if m["role"] == "user")
        text = messages[last_user_idx]["content"]
        results: dict[str, dict] = {}
        id2name = {}
        for m in messages[last_user_idx + 1:]:
            if m["role"] == "assistant":
                for tc in m.get("tool_calls", []):
                    id2name[tc["id"]] = tc["function"]["name"]
            elif m["role"] == "tool":
                results[id2name.get(m["tool_call_id"], "?")] = json.loads(m["content"])
        prefs = {k: v for k, v, _ in _PREF_LINE.findall(system)}
        action = self._next(text, results, prefs)
        usage_note = "mock provider: no real token usage"
        if action[0] == "call":
            _, name, args = action
            call = {"id": f"call_{len(results)+1}_{name}", "name": name, "arguments": json.dumps(args)}
            return LLMResponse(None, [call], "tool_calls", None, usage_note)
        return LLMResponse(action[1], [], "stop", None, usage_note)

    # ---- decision logic
    def _next(self, text: str, R: dict, prefs: dict):
        t = text.lower()
        concise = prefs.get("explanation_style") == "concise"
        if "[redacted" in t:
            return ("say", "I removed a sensitive value from your message and did not store or use it. Please never share "
                           "passwords, OTPs, PINs or card details in chat. For account problems I can create an IT_ACCOUNT support request.")
        # --- no-tool answers
        if re.fullmatch(r"\W*(hi|hello|hey|thanks|thank you|good (morning|evening))\W*", t):
            return ("say", "Hello! I can check your fee and scholarship status, search the (fictional) student handbook, "
                           "manage support requests, and remember simple preferences.")
        if (m := re.search(r"what is (\d+)\s*([\+\-\*x/])\s*(\d+)", t)):
            a, op, b = int(m.group(1)), m.group(2), int(m.group(3))
            val = {"+": a + b, "-": a - b, "*": a * b, "x": a * b, "/": (a / b if b else "undefined")}[op]
            return ("say", f"{a} {op} {b} = {val}.")
        if re.search(r"what.*(study focus|did i (tell|say)|preference)|remind me.*(focus|prefer)", t):
            if "study_focus" in prefs:
                return ("say", f"Your saved study focus is {prefs['study_focus']}.")
            return ("say", "I don't have a saved study focus for you yet.")
        if re.search(r"(suggest|recommend).*(study|learn|read)", t) and "study_focus" in prefs:
            return ("say", f"Given your saved focus on {prefs['study_focus']}, start with the programme materials on that topic. "
                           "I can't see course catalogues, so check with your faculty for specific titles.")
        other = re.search(r"\bS\d{4}\b", text, re.I)
        sid_arg = {"student_id": other.group(0).upper()} if other else {}
        if re.search(r"weather|forecast|\bnews\b|python|javascript|write (me )?(a )?(code|program|script)|\bjoke\b|recipe|cricket score", t):
            return ("say", "I can only help with student-support topics: your fees, scholarship, profile, support requests, course or name changes, and handbook policies. "
                           "For example, I can check your fee status or explain the attendance requirement.")
        if re.search(r"(correct|fix|change|update|spelling).{0,25}\bname\b", t) and not re.search(r"how|policy|rule", t):
            if "request_profile_correction" not in R:
                m = re.search(r"(?:to|as)\s+([A-Z][A-Za-z .'\-]{1,59})\s*[.!]?\s*$", text.strip())
                if not m:
                    return ("say", "What should the corrected name be? Please give the full name as it should appear on your record.")
                return ("call", "request_profile_correction", {"field": "name", "new_value": m.group(1).strip(), "reason": text[:280], **sid_arg})
            r = R["request_profile_correction"]
            if r.get("ok"):
                d = r["data"]
                return ("say", f"I submitted name-correction request {d['action_id']} to \"{d['requested_value']}\". It is PENDING APPROVAL by a reviewer; your name is unchanged until they decide.")
            code = r.get("error_code")
            if code == "forbidden":
                return ("say", "I can only submit changes for your own record, so I did not do that.")
            if code in ("already_pending", "no_change"):
                return ("say", r["message"])
            return ("say", "I couldn't submit the request right now, so nothing was changed.")
        # --- sensitive: course change (request only; approval is a human action)
        if re.search(r"(course change|programme change|name correction|change request)", t) and re.search(r"status|pending|approved|where", t) and not re.search(r"\bi want\b|how|policy|rule", t):
            if "get_my_course_change_requests" not in R:
                return ("call", "get_my_course_change_requests", {})
            r = R["get_my_course_change_requests"]
            if not r.get("ok"):
                return ("say", "I couldn't retrieve your course-change requests right now.")
            rq = r["data"]["requests"]
            return ("say", ("Your change requests: " + "; ".join(f"{x['action_id']} ({x['field']}) to {x['requested_value']}: {x['state']}" for x in rq) + ".") if rq else "You have no change requests.")
        if re.search(r"(change|switch|move).{0,25}(programme|program|course)|(programme|program|course).{0,25}(change|switch)", t) and not re.search(r"how|policy|rule|deadline|window|days|who approves", t):
            if "request_course_change" not in R:
                tgt = ("PGDM - Business Analytics" if re.search(r"business analytics", t) else "PGDM - Big Data Analytics" if re.search(r"big data", t)
                       else "PGDM - AI & Data Science" if re.search(r"\bai\b|data science", t) else None)
                if not tgt:
                    return ("say", "Which programme would you like to move to: AI & Data Science, Big Data Analytics or Business Analytics?")
                return ("call", "request_course_change", {"target_programme": tgt, "reason": text[:280], **sid_arg})
            r = R["request_course_change"]
            if r.get("ok"):
                d = r["data"]
                return ("say", f"I submitted course-change request {d['action_id']} to {d['requested_programme']}. It is PENDING APPROVAL by a reviewer; your programme is unchanged until they decide.")
            code = r.get("error_code")
            if code == "forbidden":
                return ("say", "I can only submit changes for your own record, so I did not do that.")
            if code == "already_pending":
                return ("say", r["message"])
            if code == "no_change":
                return ("say", "You are already enrolled in that programme.")
            return ("say", "I couldn't submit the request right now, so nothing was changed.")
        # --- portal failure / next working day
        wants_ticket = re.search(r"(create|raise|open|file|submit|log).{0,25}(ticket|support request|complaint)", t)
        if not wants_ticket and re.search(r"portal|payment (failed|failure)|next working day|could ?n.t pay|unable to pay", t):
            if config.PAYMENT_TOOL_MODE == "composite":
                if "check_payment_deadline" not in R:
                    return ("call", "check_payment_deadline", {})
                return ("say", self._portal_answer(R["check_payment_deadline"]))
            if "search_student_policy" not in R:
                return ("call", "search_student_policy", {"query": "payment portal failure next working day support ticket due date"})
            if "get_my_fee_status" not in R:
                return ("call", "get_my_fee_status", {})
            if "get_my_support_requests" not in R:
                return ("call", "get_my_support_requests", {})
            fee = R["get_my_fee_status"]
            if fee.get("ok") and "get_next_working_day" not in R and fee["data"].get("due_date"):
                return ("call", "get_next_working_day", {"date": fee["data"]["due_date"]})
            return ("say", self._portal_answer(self._verdict_from_steps(R)))
        # --- ticket creation
        if re.search(r"(create|raise|open|file|submit|log).{0,25}(ticket|support request|complaint)", t):
            if "create_support_request" not in R:
                return ("call", "create_support_request", {"category": _category_guess(text), "description": text[:480]})
            r = R["create_support_request"]
            if r.get("ok"):
                d = r["data"]
                return ("say", f"I created support request {d['ticket_id']} ({d['category']}), status {d['status']}, "
                               f"saved at {d['created_at']}. This records your request; it does not resolve it.")
            return ("say", "I could not create the support request, so nothing was saved. Please try again later.")
        # --- ticket lookup
        if (m := re.search(r"tkt-\d{4,}", t)):
            if "get_my_support_request" not in R:
                return ("call", "get_my_support_request", {"ticket_id": m.group(0).upper()})
            r = R["get_my_support_request"]
            if r.get("ok"):
                d = r["data"]
                return ("say", f"{d['ticket_id']}: {d['category']}, status {d['status']}, created {d['created_at']}.")
            return ("say", "I couldn't find that support request on your account.")
        if re.search(r"\b(my|status of).{0,15}(ticket|support request)", t) and not re.search(r"how long|respon|repl|target|when will", t):
            if "get_my_support_requests" not in R:
                return ("call", "get_my_support_requests", {})
            r = R["get_my_support_requests"]
            if not r.get("ok"):
                return ("say", "I could not retrieve your support requests right now.")
            tk = r["data"]["tickets"]
            if not tk:
                return ("say", "You have no support requests on record.")
            return ("say", "Your support requests: " + "; ".join(f"{x['ticket_id']} {x['category']} {x['status']}" for x in tk) + ".")
        # --- remember preference
        if re.search(r"\bremember\b|from now on|always (answer|reply|explain|keep)|i prefer", t):
            if "remember_preference" not in R:
                cat, val = self._pref(t)
                return ("call", "remember_preference", {"category": cat, "value": val})
            r = R["remember_preference"]
            if r.get("ok"):
                return ("say", f"Saved: {r['data']['category']} = {r['data']['value']}.")
            return ("say", "I can't store that. I only remember language, explanation style, answer format, example style "
                           "and a short study focus. One-off details and secrets are never saved.")
        # --- record status tools
        if re.search(r"\bfee|balance|tuition|amount due|dues?\b", t) and not re.search(r"late fee|refund|waiver|hostel|policy|rule", t):
            if "get_my_fee_status" not in R:
                return ("call", "get_my_fee_status", sid_arg)
            return ("say", self._fee_answer(R["get_my_fee_status"], concise))
        if re.search(r"scholarship|grant", t) and re.search(r"\bmy\b|\bs\d{4}\b", t) and not re.search(r"tax|criteria|eligib|rule", t):
            if "get_my_scholarship_status" not in R:
                return ("call", "get_my_scholarship_status", sid_arg)
            return ("say", self._sch_answer(R["get_my_scholarship_status"]))
        if re.search(r"\bmy (programme|program|course|profile|enrolment|enrollment)", t) or (other and re.search(r"programme|program|profile|enrol", t)):
            if "get_my_profile" not in R:
                return ("call", "get_my_profile", sid_arg)
            r = R["get_my_profile"]
            if r.get("error_code") == "forbidden":
                return ("say", "I can only share your own records. I can't access another student's information.")
            if not r.get("ok"):
                return ("say", "I couldn't verify your profile right now.")
            d = r["data"]
            return ("say", f"Programme: {d['programme']}; enrolment status: {d['enrolment_status']}.")
        # --- default: anything else about the institute -> policy search
        if "search_student_policy" not in R:
            return ("call", "search_student_policy", {"query": text[:190]})
        return ("say", self._policy_answer(R["search_student_policy"], concise))

    @staticmethod
    def _pref(t: str):
        if re.search(r"concise|short|brief", t): return "explanation_style", "concise"
        if re.search(r"detailed|in detail|thorough", t): return "explanation_style", "detailed"
        for f in ("bullets", "table", "paragraph"):
            if f in t: return "answer_format", f
        for lang in _LANGS:
            if lang in t: return "language", lang
        if (m := re.search(r"(?:study focus|focus on|interested in)\s+(?:is\s+)?([a-z][a-z &/\-]{2,38})", t)):
            return "study_focus", m.group(1).strip()
        return "note", t[:30]   # not an allowlisted category -> the application must reject it

    @staticmethod
    def _fee_answer(r, concise):
        if r.get("error_code") == "forbidden":
            return "I can only share your own records. I can't access another student's information."
        if not r.get("ok"):
            return "I couldn't retrieve your fee record, so I can't confirm your fee status."
        d = r["data"]
        if concise:
            return f"Fee status: {d['status']}. Due: INR {d['amount_due']:,.0f} by {d['due_date']}."
        return (f"Your fee status is {d['status']} for {d['fee_period']}: total INR {d['total_fee']:,.0f}, paid INR "
                f"{d['amount_paid']:,.0f}, due INR {d['amount_due']:,.0f}, due date {d['due_date']} (record last updated {d['last_updated']}).")

    @staticmethod
    def _sch_answer(r):
        if r.get("error_code") == "forbidden":
            return "I can only share your own records. I can't access another student's information."
        if not r.get("ok"):
            return "I couldn't find a scholarship record, so I can't confirm a status."
        parts = []
        for a in r["data"]["applications"]:
            parts.append(f"{a['scheme'] or 'No application'}: {a['status']}" + (f" ({a['note']})" if a.get("note") else ""))
        return "Scholarship status: " + "; ".join(parts) + ". APPROVED means a decision is recorded, not that funds were paid."

    @staticmethod
    def _policy_answer(r, concise):
        if not r.get("ok"):
            return "I couldn't search the handbook right now."
        res = r["data"]["results"]
        if not res:
            return ("The synthetic handbook does not specify an answer to that. I can create a support request so a reviewer can look at it.")
        top = res[0]
        ex = top["excerpt"]
        first = re.split(r"(?<=[.!?])\s", ex)
        if re.search(r"not specified|no general|not defined|unspecified|does not specify", first[0], re.I):
            return (f"The handbook does not specify that ({top['chunk_id']}). I can create a support request for review.")
        body = first[0] if concise else " ".join(first[:2])
        return f"{body} ({top['chunk_id']}) [fictional policy]"

    @staticmethod
    def _verdict_from_steps(R):
        """Build the same verdict structure the composite tool returns, from the separate tool results (mock reasoning)."""
        fee, tix, nwd, pol = R["get_my_fee_status"], R["get_my_support_requests"], R.get("get_next_working_day"), R["search_student_policy"]
        if not (fee.get("ok") and tix.get("ok") and pol.get("ok")):
            return {"ok": False, "error_code": "tool_error"}
        d = fee["data"]
        if not d["due_date"]:
            return {"ok": False, "error_code": "not_found"}
        q = [x for x in tix["data"]["tickets"] if x["category"] == "FEE_PAYMENT" and x["created_at"][:10] <= d["due_date"]]
        pay_by = f"{nwd['data']['next_working_day']} 18:00 IST ({nwd['data']['next_working_weekday']})" if nwd and nwd.get("ok") else "the next working day"
        verdict = "ALREADY_PAID" if d["status"] == "PAID" else "TICKET_ON_FILE_BEFORE_CUTOFF" if q else "NO_TICKET_YET_CUTOFF_NOT_PASSED"
        return {"ok": True, "data": {"verdict": verdict, "qualifying_ticket": q[0] if q else None, "ticket_cutoff": f"{d['due_date']} 23:59 IST", "pay_by": pay_by}}

    @staticmethod
    def _portal_answer(r):
        if r.get("error_code") == "forbidden":
            return "I can only share your own records. I can't access another student's information."
        if not r.get("ok"):
            return "I couldn't verify your records right now, so I can't confirm whether the next-working-day rule applies."
        d = r["data"]
        if d["verdict"] == "ALREADY_PAID":
            return "Your fee is already recorded as PAID, so the payment-portal rule does not apply."
        if d["verdict"] == "TICKET_ON_FILE_BEFORE_CUTOFF":
            q = d["qualifying_ticket"]
            return (f"Yes, under VIA-FEE-004: your FEE_PAYMENT ticket {q['ticket_id']} was saved {q['created_at']}, on or before the "
                    f"cutoff ({d['ticket_cutoff']}). You may pay by {d['pay_by']} without it being marked late. The ticket proves you "
                    "reported the failure, not that a payment was made. If a public holiday applies, the holiday calendar is unavailable, so ask for review.")
        if d["verdict"] == "NO_TICKET_YET_CUTOFF_NOT_PASSED":
            return (f"I can't promise that. VIA-FEE-004 requires a FEE_PAYMENT support ticket saved before {d['ticket_cutoff']}. "
                    f"You have no qualifying ticket on record. If you create one by that cutoff, the policy permits payment by {d['pay_by']}. "
                    "Would you like me to create the ticket?")
        return (f"I can't promise that. The ticket cutoff ({d['ticket_cutoff']}) has passed and you have no qualifying FEE_PAYMENT ticket on record "
                "(VIA-FEE-004). I can create a support request so a reviewer can assess your case.")
