"""
business_hours.py

Single source of truth for the Indihomes business-hours window
(10:00 AM - 7:00 PM IST), shared by every service that needs to gate a
reply or an outbound send to hours a human can actually back up.

WHY THIS LIVES IN THE BACKEND, NOT IN WATI
--------------------------------------------
WATI's business-hours / "Default Action" features only govern WATI's own
Team Inbox and AI Agent (Knowbot) routing - they decide what happens when
NO chatbot flow or webhook is actively handling a conversation. In this
project, WATI is purely a communication layer: every reply and every
outbound template send is triggered by our own code (this backend, and
the separate Phase 2 Campaign Service). WATI has no visibility into, and
no control over, when our backend chooses to call its Send Message / Send
Template API - so there is no WATI setting that stops a reply at 11 PM.
The time-window logic has to live here.

See Indihomes_Business_Hours_Gating.docx for the full design rationale,
and claude.md's "Business hours gating" task section for exactly which
endpoints in app.py this is wired into and why.

Deliberately zero dependencies beyond the stdlib - this module is shared
by two different codebases (this Phase 1 bot, and the separate Phase 2
Campaign Service), so keeping it dependency-free means dropping a copy of
this one file into the other project is enough; it never needs THIS
project's requirements.txt installed alongside it.
"""

from datetime import datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
BUSINESS_START = time(10, 0)
BUSINESS_END = time(19, 0)


def is_business_hours(dt: Optional[datetime] = None) -> bool:
    """True if `dt` (default: now) falls within 10:00-19:00 IST, inclusive
    of both endpoints. Always converts to IST first, so callers never need
    to worry about the server's own timezone (Railway containers run UTC).

    A naive (tzinfo-less) `dt` is assumed to already be in IST - callers
    passing a naive datetime are responsible for that being true. Every
    caller in this codebase passes either None (uses now()) or an
    already-tz-aware datetime, so this hasn't been an issue in practice,
    but it's worth knowing if a new caller ever passes a naive value from
    somewhere else.
    """
    dt = dt or datetime.now(IST)
    return BUSINESS_START <= dt.astimezone(IST).time() <= BUSINESS_END


def today_ist_date(dt: Optional[datetime] = None) -> str:
    """The current IST calendar date as 'YYYY-MM-DD'. Used to gate the
    once-per-day off-hours notice (see conversation_tracker.py's
    should_send_off_hours_notice) - a phone should get the notice once per
    IST calendar day it messages outside hours, not once per UTC day
    (which could flip mid-evening IST and cause a spurious second notice)."""
    dt = dt or datetime.now(IST)
    return dt.astimezone(IST).date().isoformat()


def next_business_open(dt: Optional[datetime] = None) -> datetime:
    """The next moment business hours open, in IST.

    If `dt` is already within business hours, returns `dt` unchanged -
    callers that specifically want a FUTURE open time should check
    is_business_hours() first and only call this when it's False.

    Before 10 AM -> today's 10 AM. After 7 PM -> tomorrow's 10 AM.
    """
    dt = (dt or datetime.now(IST)).astimezone(IST)
    if is_business_hours(dt):
        return dt
    open_today = dt.replace(hour=BUSINESS_START.hour, minute=BUSINESS_START.minute,
                             second=0, microsecond=0)
    if dt.time() < BUSINESS_START:
        return open_today
    return open_today + timedelta(days=1)
