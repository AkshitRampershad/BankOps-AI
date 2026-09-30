"""Redaction of regulated data before it is logged, persisted, or sent to the model.

Three complementary mechanisms, because no single one is sufficient:

1. Known values — secrets and inputs classified pii/secret are registered at runtime and
   replaced wherever they appear (exact match), e.g. a member number echoed on screen.
2. Patterns — SSNs, card numbers (Luhn-checked), phone numbers, emails.
3. Labels — the app profile lists labels ("SSN:", "Date of Birth:") whose *adjacent* value
   is PII; the observation builder masks those values structurally (see surface/web.py).

Redaction is applied at the sink (event log writer, artifact linter, LLM observation
renderer), so a caller forgetting to redact cannot leak through those paths.
"""

from __future__ import annotations

import re
from typing import Any

SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
PHONE = re.compile(r"\(\d{3}\)\s?\d{3}-\d{4}|\b\d{3}-\d{3}-\d{4}\b")
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
CARDLIKE = re.compile(r"\b(?:\d[ -]?){13,19}\b")


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


class Redactor:
    def __init__(self) -> None:
        self._values: dict[str, str] = {}  # value -> replacement token

    def register(self, value: str | None, label: str) -> None:
        if value is None:
            return
        v = str(value)
        if len(v) >= 3:  # avoid redacting trivial substrings like "1"
            self._values[v] = f"«{label}»"

    def text(self, s: str) -> str:
        if not s:
            return s
        # Longest first so overlapping registrations behave predictably.
        for v in sorted(self._values, key=len, reverse=True):
            if v in s:
                s = s.replace(v, self._values[v])
        s = SSN.sub("«ssn»", s)
        s = EMAIL.sub("«email»", s)
        s = PHONE.sub("«phone»", s)

        def card(m: re.Match) -> str:
            digits = re.sub(r"\D", "", m.group(0))
            return "«pan»" if 13 <= len(digits) <= 19 and _luhn_ok(digits) else m.group(0)

        return CARDLIKE.sub(card, s)

    def labeled(self, text: str, labels: list[str]) -> str:
        """Mask the value that follows a PII label on the same line ("Name:\tJANE DOE").
        Must run on line-structured text, before whitespace is collapsed."""
        for label in labels:
            text = re.sub(rf"(^|\n)(\s*{re.escape(label)})[ \t]*[^\n]*", r"\1\2 «pii»", text)
        return self.text(text)

    def obj(self, o: Any) -> Any:
        """Deep-redact a JSON-like structure."""
        if isinstance(o, str):
            return self.text(o)
        if isinstance(o, dict):
            return {k: self.obj(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [self.obj(v) for v in o]
        return o

    def contains_sensitive(self, s: str) -> bool:
        return self.text(s) != s


def mask_value(value: str) -> str:
    """Partially mask a value for display (keep length hint, never content)."""
    return "«masked:%d»" % len(value)
