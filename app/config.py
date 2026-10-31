"""Central configuration. Values come from environment variables / .env (never hard-coded secrets)."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=True)  # project .env wins over stale shell/system variables

DB_PATH = os.getenv("DB_PATH", str(ROOT / "data" / "support.db"))
HANDBOOK_PATH = os.getenv("HANDBOOK_PATH", str(ROOT / "data" / "policies" / "handbook.md"))

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL") or "llama-3.3-70b-versatile"
# mock = deterministic offline provider (NOT real LLM inference); groq = live API.
AGENT_PROVIDER_MODE = (os.getenv("AGENT_PROVIDER_MODE") or ("groq" if GROQ_API_KEY else "mock")).lower()  # empty value = unset

MAX_AGENT_STEPS = int(os.getenv("MAX_AGENT_STEPS", "8"))   # a 6-lookup question needs 6 tool steps + the answer
TOOL_TIMEOUT_S = float(os.getenv("TOOL_TIMEOUT_S", "5"))
HISTORY_TURNS = int(os.getenv("HISTORY_TURNS", "4"))      # recent turns sent to the model

# Retrieval settings (documented in README)
RAG_TOP_K = int(os.getenv("RAG_TOP_K", "3"))
RAG_MIN_SCORE = float(os.getenv("RAG_MIN_SCORE", "3.0"))
RAG_MIN_COVERAGE = float(os.getenv("RAG_MIN_COVERAGE", "0.4"))

TIMEZONE = os.getenv("TIMEZONE", "Asia/Kolkata")
RAG_EXCERPT_CHARS = int(os.getenv("RAG_EXCERPT_CHARS", "1000"))

PBKDF2_ITERS = int(os.getenv("PBKDF2_ITERS", "200000"))
SESSION_TTL_HOURS = int(os.getenv("SESSION_TTL_HOURS", "8"))
LOGIN_MAX_FAILURES = int(os.getenv("LOGIN_MAX_FAILURES", "5"))
LOGIN_LOCK_MINUTES = int(os.getenv("LOGIN_LOCK_MINUTES", "5"))
ACTION_EXPIRY_DAYS = int(os.getenv("ACTION_EXPIRY_DAYS", "7"))
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "0") == "1"
HISTORY_CHARS = int(os.getenv("HISTORY_CHARS", "500"))   # per earlier message sent back to the model
# Payment-portal question: "multi" = the agent chains search_student_policy + fee + tickets + get_next_working_day (several model/tool calls, the agentic default);
# "composite" = one check_payment_deadline call (fewer round trips; a latency optimisation, not the default).
PAYMENT_TOOL_MODE = (os.getenv("PAYMENT_TOOL_MODE") or "multi").lower()
RAG_EXTRA_EXCERPT_CHARS = int(os.getenv("RAG_EXTRA_EXCERPT_CHARS", "450"))   # chunks ranked 2+ are shortened to save tokens
GROQ_MAX_RETRIES = int(os.getenv("GROQ_MAX_RETRIES", "1"))   # SDK retries on 429/5xx; each retry can add 10-20 s. Set 0 to fail fast.

# ---- token savers (all default ON; set to 0 / off to compare against the old behaviour)
TOKEN_SAVER = os.getenv("TOKEN_SAVER", "1") != "0"            # compact JSON, lean tool results, lean tool schemas
TOOL_ROUTING = (os.getenv("TOOL_ROUTING") or "core").lower()   # core = offer write tools only when the question needs them; off = offer every tool
GROQ_REASONING_EFFORT = os.getenv("GROQ_REASONING_EFFORT", "low")   # only used for openai/gpt-oss-* models (low | medium | high)
