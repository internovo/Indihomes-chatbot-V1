"""
Shared free-text sanitiser. Every endpoint that accepts user-typed text
(location, amenities, builder, name, visit notes, ...) should run the value
through sanitise() before it reaches search(), a CRM payload, a calendar
event description, or an email body.

Strips control characters and zero-width/bidi characters (used to hide or
spoof text), collapses whitespace, caps length, and removes unresolved WATI
placeholders ({{var}}) and @variable tokens so a webhook mis-wiring can never
leak the literal placeholder syntax into a message or a stored record.
"""

import re

_WATI_BRACE = re.compile(r"\{\{.*?\}\}")
_WATI_AT = re.compile(r"@[A-Za-z_][A-Za-z0-9_]*")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
# Zero-width space/joiners, bidi override/embedding marks, BOM - invisible
# characters sometimes used to hide text or spoof RTL/LTR rendering.
_ZERO_WIDTH = re.compile(r"[​-‏‪-‮⁠﻿]")
_WHITESPACE = re.compile(r"\s+")


def sanitise(text, max_len: int = 100) -> str:
    """Defensive cleanup for a single free-text field. Never raises - bad
    input degrades to an empty or truncated string, never an exception."""
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)

    text = _WATI_BRACE.sub(" ", text)
    text = _WATI_AT.sub(" ", text)
    text = _CONTROL_CHARS.sub(" ", text)
    text = _ZERO_WIDTH.sub("", text)
    text = _WHITESPACE.sub(" ", text).strip()

    return text[:max_len]
