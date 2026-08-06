"""
intent_router.py

Global intent detection for free-text replies that arrive somewhere OTHER
than the button/number a WATI node was expecting.

WHY THIS EXISTS
----------------
WATI's visual chatflow graph is edges = button clicks. A typed reply that
isn't one of the button labels (or, at a numbered-list node, isn't a valid
number) has nowhere to go in the graph - which is where real leads were
getting lost. See claude.md, "Free-text handling", for the production
transcript that motivated this module: a user typed "No one" at the
property-picker and "Send in borivali east also" at the advisor question,
and both were silently mishandled.

This module does NOT replace the button flow. It is a narrow classifier that
looks for a handful of GLOBAL intents - things a customer might say at
almost any point in the conversation that deserve a real response regardless
of what question the bot just asked:

    change_location    "send in borivali east also", "what about andheri"
    reject_all         "no one", "none of these", "not interested in these"
    talk_to_advisor    "connect me to an advisor", "call me", "talk to someone"
    restart            "start over", "restart"
    stop               "stop", "unsubscribe", "don't message me"

Anything that doesn't match one of these is `intent: "none"` - the caller
(app.py) falls back to whatever local handling that endpoint already had.

DELIBERATELY RULE-BASED, NOT LLM-BASED (v1)
---------------------------------------------
These five intents cover the two failures actually seen in production and
are each expressed in a small, closed set of ways in real WhatsApp replies.
A keyword/regex tier answers them for free, instantly, and deterministically
- important for `stop` / opt-out in particular, where "the model felt like
it" is not an acceptable basis for a compliance-relevant decision.

Location extraction re-uses the SAME whitelist and normalization
(llm_location.normalize_location / property_core.KNOWN_LOCALITIES_LOWER) the
rest of the app already trusts - this module invents no new location logic,
it only invents the "is this even a location-change request" trigger.

If a future case needs looser understanding (Hinglish, indirect phrasing),
add a Tier-3 LLM fallback here the same way llm_location.py does it - but
only after logs show the rule-based tier is actually missing real traffic
(see claude.md's rollout notes for how to check).
"""

import re
from typing import Dict, List, Optional

from property_core import KNOWN_LOCALITIES_LOWER
from llm_location import SPLIT_RULES, normalize_location

# ---------------------------------------------------------------------------
# Keyword tables. Kept as plain phrase lists (not one big compiled regex) so
# a non-engineer can review/extend them later - see claude.md, "How to add
# a new phrase", for the one-line process.
# ---------------------------------------------------------------------------

_STOP_PHRASES = [
    "stop", "unsubscribe", "opt out", "opt-out", "don't message", "dont message",
    "don't contact", "dont contact", "remove me", "do not contact",
    "stop messaging", "stop texting",
]

_ADVISOR_PHRASES = [
    "advisor", "talk to someone", "talk to a person", "talk to a human",
    "speak to someone", "speak to a person", "speak to a human", "call me",
    "human please", "connect me", "agent", "sales person", "salesperson",
    "real person",
]

# Deliberately NOT including a bare "no" - that's a normal answer to plenty
# of yes/no questions elsewhere in the flow (e.g. "is that a firm budget?"),
# and misreading it as "reject everything you showed me" would be worse than
# missing the occasional genuine short rejection.
_REJECT_PHRASES = [
    "no one", "none", "none of these", "none of them", "nothing", "not this",
    "not interested in these", "don't like any", "dont like any",
    "not what i want", "not what i'm looking for", "not what im looking for",
]

_RESTART_PHRASES = [
    "start over", "restart", "begin again", "start again", "reset",
]

# Words that, alongside a recognised locality, signal "search again with
# this new area" rather than the customer simply repeating back the area the
# bot just asked about (which is normal in-flow behaviour, not a global
# interrupt). Kept deliberately loose - a false positive here just means we
# treat a location mention as a global intent one turn early, which is
# harmless; a false negative is the actual failure mode this module exists
# to fix.
_LOCATION_SIGNAL_WORDS = [
    "also", "too", "what about", "how about", "instead", "show me",
    "send", "any in", "options in", "properties in", "near",
]


def _contains_any(text: str, phrases: List[str]) -> Optional[str]:
    for p in phrases:
        if p in text:
            return p
    return None


def _find_location_mention(text: str) -> Optional[str]:
    """Look for a known locality name (or a splittable area name like
    'borivali') inside free text. Returns the raw matched phrase (NOT yet
    normalized - callers should still run it through resolve_location_text,
    same as everywhere else location text is handled in this project).
    Longer names are checked first so 'malad west' matches before the bare
    'malad' area-split rule would swallow it."""
    candidates = sorted(
        list(KNOWN_LOCALITIES_LOWER.keys()) + list(SPLIT_RULES.keys()),
        key=len, reverse=True,
    )
    for cand in candidates:
        if re.search(r"\b" + re.escape(cand) + r"\b", text):
            return cand
    return None


def classify(text: str) -> Dict:
    """Classify one piece of free text for a global intent.

    Returns one of:
      {"intent": "none"}
      {"intent": "stop" | "talk_to_advisor" | "reject_all" | "restart",
       "matched_phrase": <the phrase that triggered it>}
      {"intent": "change_location",
       "matched_phrase": <signal word, or "" if the location alone was
                          clearly the whole message>,
       "location_text": <raw matched location text - still needs
                         resolve_location_text() applied before use>}

    Priority order is deliberate:
      1. stop             - compliance-critical, must win over every other read
      2. talk_to_advisor   - an explicit request for a human should never be
                             reinterpreted as anything else
      3. reject_all        - checked before the looser location scan so it
                             isn't shadowed by an incidental place name
      4. change_location   - only fires if a real locality is present AND
                             (a signal word is present, OR the message is
                             short enough that naming a place alone is
                             clearly the whole intent - e.g. "kandivali
                             east" typed cold)
      5. restart
    """
    raw = (text or "").strip().lower()
    if not raw:
        return {"intent": "none"}

    hit = _contains_any(raw, _STOP_PHRASES)
    if hit:
        return {"intent": "stop", "matched_phrase": hit}

    hit = _contains_any(raw, _ADVISOR_PHRASES)
    if hit:
        return {"intent": "talk_to_advisor", "matched_phrase": hit}

    hit = _contains_any(raw, _REJECT_PHRASES)
    if hit:
        return {"intent": "reject_all", "matched_phrase": hit}

    loc = _find_location_mention(raw)
    if loc:
        signal = _contains_any(raw, _LOCATION_SIGNAL_WORDS)
        if signal or len(raw.split()) <= 6:
            return {"intent": "change_location", "matched_phrase": signal or "",
                    "location_text": loc}

    hit = _contains_any(raw, _RESTART_PHRASES)
    if hit:
        return {"intent": "restart", "matched_phrase": hit}

    return {"intent": "none"}


def resolve_location_text(loc_text: str) -> List[str]:
    """Turn a matched location mention into the same normalized-locality
    list the rest of the app already works with (splits 'borivali' into
    both sides, whitelists against real inventory). Thin wrapper so callers
    in app.py don't need to import llm_location just for this one call."""
    return normalize_location(loc_text)
