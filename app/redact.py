"""Sensitive-input detection and redaction (VIA-IT-002 / VIA-MEM-003).

Runs BEFORE anything is persisted, traced, ticketed or sent to the model.
Heuristic: it can miss unusual formats, so memory is also allowlist-only.
"""
import re
from dataclasses import dataclass, field

_PATTERNS = [
    # keyword + value: "otp is 482913", "password: hunter2", "pin 4821"
    ("otp", re.compile(r"(?i)\b(otp|one[- ]time (?:password|code)|verification code|auth(?:entication)? code)\b[^\w\n]{0,12}(?:is|:|=|-)?\s*((?=[A-Za-z0-9]*\d)[A-Za-z0-9]{4,10})\b")),
    ("password", re.compile(r"(?i)\b(password|passwd|pwd|passcode)\b\s*(?:is|:|=|-)?\s*(\S{3,})")),
    ("pin", re.compile(r"(?i)\b(pin|cvv|cvc|security code)\b\s*(?:is|:|=|-)?\s*(\d{3,6})\b")),
    ("token", re.compile(r"(?i)\b(?:bearer\s+[A-Za-z0-9._\-]{16,}|gsk_[A-Za-z0-9]{16,}|sk-[A-Za-z0-9]{16,})")),
    ("secret_assignment", re.compile(r"(?i)\b(?:api[_-]?key|secret|token)\s*[:=]\s*[\"']?[A-Za-z0-9_\-]{8,}")),
    ("card", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
]


@dataclass
class RedactionResult:
    text: str
    kinds: list = field(default_factory=list)

    @property
    def found(self) -> bool:
        return bool(self.kinds)


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


OUTPUT_KINDS = ("card", "token")   # assistant output: only high-confidence secrets (keeps code samples with placeholders intact)


def redact(text: str, only: tuple | None = None) -> RedactionResult:
    """Return text with secrets replaced by [REDACTED:<kind>] and the kinds found. `only` restricts which kinds are checked."""
    kinds: list = []
    out = text
    for kind, pat in _PATTERNS:
        if only is not None and kind not in only:
            continue
        def _sub(m, kind=kind):
            if kind == "card":
                digits = re.sub(r"\D", "", m.group(0))
                if not (13 <= len(digits) <= 19 and _luhn_ok(digits)):
                    return m.group(0)  # long number that is not a card (e.g. transaction ref)
            kinds.append(kind)
            if kind in ("otp", "password", "pin"):
                return f"{m.group(1)} [REDACTED:{kind}]"
            return f"[REDACTED:{kind}]"
        out = pat.sub(_sub, out)
    return RedactionResult(out, sorted(set(kinds)))
