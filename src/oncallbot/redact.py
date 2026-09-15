"""Mask obvious PII before a body leaves this machine.

This inbox carries health records, so bodies routinely contain phone numbers,
government IDs and card fragments. Those are never needed to triage a ticket,
so they are masked. Identifiers that *are* needed -- order ids, patient ids,
record ids, the reporter's email -- are deliberately left intact.

This is a blunt instrument, not a compliance control. Read docs/phi.md before
pointing this at a real inbox.
"""

from __future__ import annotations

import re

# Order: most specific first, so a card number is not eaten by the phone rule.
_RULES: list[tuple[str, re.Pattern[str]]] = [
    # 13-19 digit card numbers, optionally space/dash grouped.
    ("CARD", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    # Aadhaar: 12 digits, often in 4-4-4 groups.
    ("AADHAAR", re.compile(r"\b\d{4}[ -]?\d{4}[ -]?\d{4}\b")),
    # Indian PAN.
    ("PAN", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")),
    # Indian mobile, with or without +91 / 0 prefix.
    ("PHONE", re.compile(r"(?:\+91[\s-]?|\b0)?\b[6-9]\d{9}\b")),
]


def redact(text: str) -> str:
    """Replace PII spans with [REDACTED:<kind>], keeping a last-4 hint on phones."""
    if not text:
        return text
    for kind, pattern in _RULES:
        if kind == "PHONE":
            text = pattern.sub(lambda m: f"[REDACTED:PHONE …{_digits(m.group(0))[-4:]}]", text)
        else:
            text = pattern.sub(f"[REDACTED:{kind}]", text)
    return text


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)
