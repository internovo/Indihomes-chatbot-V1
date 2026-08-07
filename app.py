"""
Indihomes chatbot backend.

MAIN ENDPOINT (single-call design):
  POST /search
      Does BOTH location understanding (Groq) and property search in ONE call.
      Send everything the chatbot collected; get back `recommendations`.
      This avoids relying on WATI carrying variables between webhook nodes.

Also kept for testing / future use:
  POST /location            (location extraction only)
  POST /debug-search        (shows what was parsed - use when something looks wrong)
  POST /api/property-search (search only, expects an already-clean location)
  POST /interpret-message   (free-text global-intent fallback - see claude.md,
                            "Free-text handling", and intent_router.py)
  GET  /health

Setup:
  pip install fastapi uvicorn openai python-dotenv
  .env:  GROQ_API_KEY=gsk_...
Run:
  uvicorn app:app --host 0.0.0.0 --port 8000
"""

from dotenv import load_dotenv
load_dotenv()

import os
import re
from fastapi import FastAPI
from pydantic import BaseModel
from typing import Optional

from property_core import search, PROPERTIES, KNOWN_LOCALITIES
from llm_location import (
    location as location_handler,
    location_debug as location_debug_handler,
    call_llm,
    _resolve,
    validate_candidates,
    normalize_location,
    LocationRequest,
    GROQ_MODEL,
)
import calendar_service
import appointments_db
import email_service
import crm_service
import conversation_lock
import conversation_tracker
import intent_router
import business_hours
import wati_client
from models import PriorityParseRequest

app = FastAPI()

# Start the background follow-up scheduler (every 5 min sweep).
# Imported here so it starts exactly once, at process startup, after the
# FastAPI app object exists. Kept after `app = FastAPI()` to match the
# pattern in calendar_service / email_service — deps after app, not before.
import followup_scheduler as _followup_scheduler
_followup_scheduler.start()


def _clean_incoming(value: str) -> str:
    """WATI sometimes sends an unsubstituted {{var}} placeholder. Treat as empty."""
    v = (value or "").strip()
    if v.startswith("{{") and v.endswith("}}"):
        return ""
    return v


# --------------------------------------------------------------------------
# Business-hours gating (10 AM - 7 PM IST). See
# Indihomes_Business_Hours_Gating.docx and claude.md's "Business hours
# gating" task section for the full design rationale.
#
# WATI cannot gate this itself - its own business-hours settings only
# govern WATI's native Team Inbox / Knowbot routing, not a custom
# webhook-driven flow like this one (see business_hours.py's module
# docstring). Every endpoint below that produces text a customer actually
# reads checks _off_hours_text() first and, if closed, skips its normal
# processing entirely (no LLM call, no search, no calendar booking) and
# returns a degraded, endpoint-shaped response instead - never a 500,
# same contract as everything else in this file.
# --------------------------------------------------------------------------

_OFF_HOURS_NOTICE = (
    "Thanks for reaching out! Our team is available 10 AM - 7 PM IST. "
    "We'll get back to you as soon as we're open again. \U0001F642"
)
_OFF_HOURS_SHORT = "We're currently closed - back at 10 AM IST."


def _off_hours_text(phone: str) -> Optional[str]:
    """Returns None if within business hours - caller should proceed with
    its normal processing. Returns a string to use as the customer-facing
    reply if we're off hours: the full one-time notice on the FIRST
    off-hours message today for this phone, a short repeat line on every
    off-hours message after that.

    DESIGN NOTE - this deviates slightly from a literal reading of the
    design doc's "repeat messages... don't trigger repeat notices", which
    could be read as implying true silence on repeat messages. That's not
    expressible here: this is a SYNCHRONOUS, reactive webhook endpoint -
    WATI is blocked waiting on a response to every single call it makes,
    and every downstream WATI Message/Question node needs SOME text to
    render. Returning "" risks an error or a blank chat bubble rather than
    a clean no-op. So repeat off-hours messages get a short, DISTINCT line
    instead of the full notice repeated - the same paragraph never shows
    twice, which satisfies the spirit of "no repeat notices" within what
    this request/response shape can actually do.

    Best-effort on the DB write: a tracker failure must never block the
    customer from getting a reply, so a failure here still returns the
    text - it would just mean this phone's next off-hours message ALSO
    gets the full notice instead of the short one, which is a harmless
    degrade, not a broken conversation.
    """
    if business_hours.is_business_hours():
        return None
    if phone and conversation_tracker.should_send_off_hours_notice(phone):
        try:
            conversation_tracker.mark_off_hours_notified(phone)
        except Exception as e:
            print(f"[app] mark_off_hours_notified failed: {e}")
        return _OFF_HOURS_NOTICE
    return _OFF_HOURS_SHORT


class SearchRequest(BaseModel):
    """Everything the chatbot collected, sent in one go."""
    phone: Optional[str] = ""   # needed so we can remember the shortlist per phone
    # location may arrive as free text (from the open question) and/or as a
    # button value. We accept several names so the flow can send whatever it has.
    message: Optional[str] = ""
    location: Optional[str] = ""
    location_text: Optional[str] = ""
    configuration: Optional[str] = ""
    budget: Optional[str] = ""
    purpose: Optional[str] = ""
    amenities: Optional[str] = ""
    builder: Optional[str] = ""
    builder_pref: Optional[str] = ""
    possession: Optional[str] = ""
    possession_pref: Optional[str] = ""

    def best_location(self) -> str:
        """Pick the most specific non-empty location value provided."""
        for candidate in (self.location_text, self.message, self.location):
            c = _clean_incoming(candidate)
            # "Other Area" is a menu label, not a real place - ignore it.
            if c and c.lower() not in ("other area", "other"):
                return c
        return ""

    def best_possession(self) -> str:
        return _clean_incoming(self.possession) or _clean_incoming(self.possession_pref)

    def best_builder(self) -> str:
        return _clean_incoming(self.builder) or _clean_incoming(self.builder_pref)


def run_pipeline(req: SearchRequest, limit: int = 5):
    """Location understanding + property search, in one pass."""
    raw_location = req.best_location()

    # 1) Understand the location (Groq), then normalize against real inventory.
    resolved_location = ""
    llm_note = ""
    if raw_location:
        extracted = call_llm(raw_location)
        resolved = _resolve(extracted)
        resolved_location = resolved.get("normalized_location", "")
        # If it was ambiguous (e.g. "Dahisar"), search BOTH sides rather than
        # stopping to ask - we cannot ask mid-flow in this single-call design.
        if not resolved_location:
            cands = validate_candidates(extracted.get("candidate_localities"))
            if cands:
                resolved_location = "|".join(cands)
                llm_note = ("Since you didn't specify, I've included options across " +
                            " and ".join(cands[:3]) + ".")
        if not resolved_location:
            # Last resort: try normalizing the raw text directly.
            resolved_location = "|".join(normalize_location(raw_location))

    # 2) Search with the clean location.
    result = search(
        location=resolved_location,
        configuration=_clean_incoming(req.configuration),
        budget=_clean_incoming(req.budget),
        amenities=_clean_incoming(req.amenities),
        possession=req.best_possession(),
        limit=limit,
    )

    if llm_note and result.get("count"):
        result["recommendations"] = llm_note + "\n\n" + result["recommendations"]

    result["resolved_location"] = resolved_location
    return result, raw_location


def _search_in_progress_response() -> dict:
    """Flat shape matching /search's normal response, used when the
    conversation lock is already held for this phone (a double-tap or a
    WATI webhook retry arrived while the first request is still running).
    Same keys as the real response so nothing downstream in the WATI flow
    breaks reading @recommendations / @count / etc. from this reply."""
    out = {
        "recommendations": ("Still working on your last request - one moment "
                            "and I'll have your recommendations."),
        "min_price": "", "max_price": "", "count": 0, "shortlist": [],
        "resolved_location": "",
        "search_in_progress": "yes",
        "conversation_locked": "yes",
    }
    for i in range(1, 4):
        out[f"name{i}"] = out[f"detail{i}"] = out[f"image{i}"] = out[f"code{i}"] = ""
    return out


@app.post("/search")
def one_call_search(req: SearchRequest):
    """THE endpoint the chatbot should call at the end of the conversation.
    Now returns up to 5 numbered results and remembers the shortlist (keyed on
    phone) so /property-detail can resolve the number the user replies with.

    Wrapped in the conversation lock: this is the exact webhook node a rapid
    double-tap on the final priority question used to race against (two
    events reading the same pre-transition state). Keyed on phone; a second
    event for the same phone while the first is still running is turned away
    with a lightweight 'still working' response instead of racing it."""
    phone = _clean_incoming(req.phone)
    off_hours_text = _off_hours_text(phone)
    if off_hours_text is not None:
        out = _search_in_progress_response()
        out["recommendations"] = off_hours_text
        out["search_in_progress"] = "no"
        out["conversation_locked"] = "no"
        out["business_hours"] = "no"
        return out
    if not conversation_lock.acquire(phone):
        return _search_in_progress_response()
    try:
        result, _ = run_pipeline(req, limit=5)
        if phone and result.get("shortlist"):
            try:
                appointments_db.save_shortlist(phone, result["shortlist"])
            except Exception as e:
                print(f"[app] could not save shortlist: {e}")
        result["search_in_progress"] = "no"
        result["conversation_locked"] = "no"
        # Start the 2-hour re-engagement timer only if recommendations were
        # actually found — no point chasing a phone where the search returned
        # nothing. Best-effort: a tracker failure must never fail the response.
        if phone and result.get("count"):
            try:
                conversation_tracker.touch_bot_message(phone)
            except Exception as e:
                print(f"[app] conversation_tracker.touch_bot_message failed: {e}")
        return result
    finally:
        conversation_lock.release(phone)


class PropertyDetailRequest(BaseModel):
    phone: Optional[str] = ""
    choice: Optional[str] = ""   # the number the user replied with
    name: Optional[str] = ""
    # Optional context carried over from the flow's collected attributes, so
    # a global intent detected here (see claude.md, "Free-text handling")
    # can act with the full picture - e.g. change_location can re-run search
    # with the customer's already-stated budget/configuration instead of
    # just the new area alone. Safe to omit; WATI simply won't send them if
    # this webhook node isn't updated to forward these attributes.
    location: Optional[str] = ""
    configuration: Optional[str] = ""
    budget: Optional[str] = ""
    purpose: Optional[str] = ""
    amenities: Optional[str] = ""
    builder: Optional[str] = ""
    possession: Optional[str] = ""


@app.post("/property-detail")
def property_detail(req: PropertyDetailRequest):
    """Resolve the number the user picked against the shortlist we showed them,
    and return that project's full detail block + image URL for WATI to send."""
    phone = _clean_incoming(req.phone)
    choice = _clean_incoming(req.choice)
    # User is actively engaging — cancel the pending follow-up timer.
    if phone:
        try:
            conversation_tracker.touch_user_message(phone)
        except Exception as e:
            print(f"[app] conversation_tracker.touch_user_message failed: {e}")

    off_hours_text = _off_hours_text(phone)
    if off_hours_text is not None:
        return {"found": "no", "name": "", "image_url": "", "code": "",
                "detail": off_hours_text, "intent": "none", "is_global": "no",
                "business_hours": "no"}

    items = appointments_db.get_shortlist(phone) if phone else []
    if not items:
        return {"found": "no", "name": "", "image_url": "",
                "detail": "That list has expired. Please tell me your requirements again to see fresh options."}

    idx = _parse_choice(choice, len(items))
    if idx is None:
        # Not a valid number - before falling back to the generic retry
        # copy, check whether this is actually a global intent in disguise.
        # This is the exact production failure documented in claude.md:
        # "No one" (reject_all) and "Send in borivali east also"
        # (change_location) were both typed right here, at this node, and
        # both got the generic "reply 1-3" message instead of a real answer.
        intent = intent_router.classify(choice)
        if intent["intent"] != "none":
            result = _run_global_intent(intent, req, phone)
            # IMPORTANT: reply_text and recommendations are DIFFERENT fields
            # and both can be present (change_location sets both - a short
            # intro line AND the actual listings). Combine them rather than
            # picking one with `or` - reply_text is always truthy when set,
            # so an `or` chain here would silently swallow the real property
            # listings and show only the intro line. Caught by hand-testing
            # the change_location path before wiring it into WATI - see
            # claude.md, "Free-text handling", changelog.
            detail_parts = [p for p in (result.get("reply_text", ""),
                                         result.get("recommendations", "")) if p]
            detail_text = "\n\n".join(detail_parts) or \
                f"Please reply with a number between 1 and {len(items)} to see that property."
            return {
                "found": "no",
                "name": result.get("name1", "") or "",
                "image_url": "",
                "code": "",
                "detail": detail_text,
                "intent": intent["intent"],
                "is_global": "yes",
            }
        return {"found": "no", "name": "", "image_url": "",
                "detail": f"Please reply with a number between 1 and {len(items)} to see that property.",
                "intent": "none", "is_global": "no"}

    item = items[idx - 1]
    return {
        "found": "yes",
        "name": item.get("name", ""),
        "detail": item.get("detail", ""),
        "image_url": item.get("image", ""),
        "code": item.get("code", ""),
    }


@app.post("/debug-search")
def debug_search(req: SearchRequest):
    """Same as /search but shows exactly what was received and parsed."""
    result, raw_location = run_pipeline(req)
    return {
        "received": req.dict(),
        "location_used": raw_location,
        "location_after_llm": result.get("resolved_location"),
        "count": result.get("count"),
        "recommendations": result.get("recommendations"),
    }


class LeadRequest(BaseModel):
    location: Optional[str] = ""
    configuration: Optional[str] = ""
    budget: Optional[str] = ""
    purpose: Optional[str] = ""
    amenities: Optional[str] = ""
    builder: Optional[str] = ""
    possession: Optional[str] = ""


@app.post("/api/property-search")
def property_search(lead: LeadRequest):
    return search(
        location=lead.location,
        configuration=lead.configuration,
        budget=lead.budget,
        amenities=lead.amenities,
        possession=lead.possession,
    )


class FlexLocationRequest(BaseModel):
    """Accepts the location under any of the names the flow might send."""
    message: Optional[str] = ""
    location: Optional[str] = ""
    location_text: Optional[str] = ""
    phone: Optional[str] = "" 

    def best(self) -> str:
        for c in (self.location_text, self.message, self.location):
            v = _clean_incoming(c)
            if v and v.lower() not in ("other area", "other"):
                return v
        return ""


@app.post("/location")
def location(req: FlexLocationRequest):
    """Location understanding only. Returns needs_clarification yes/no,
    a clarify_question, and normalized_location.

    Business-hours gate: off hours, this reuses the EXISTING
    needs_clarification="yes" path (main_condition-loc already routes that
    to main_question-clarify, which displays @clarify_question) to surface
    the off-hours notice - no new WATI node needed. The next thing the
    customer types after that gets re-posted to /location by the flow's own
    main_webhook-loc2 node; if still off hours, they'll see the short
    repeat line the same way. See claude.md, "Business hours gating".
    """
    phone = _clean_incoming(req.phone)
    off_hours_text = _off_hours_text(phone)
    if off_hours_text is not None:
        return {"needs_clarification": "yes", "clarify_question": off_hours_text,
                "clarify_options": [], "normalized_location": "", "handoff": "no",
                "business_hours": "no"}
    return location_handler(LocationRequest(message=req.best() , phone=req.phone))


@app.post("/debug-location")
def debug_location(req: LocationRequest):
    return location_debug_handler(req)


def _advisor_display_name(email: str) -> str:
    """Best-effort friendly name from an advisor's email, e.g.
    'arpit@internovo.in' -> 'Arpit'. We don't store real names anywhere.
    'tech@...' is a generic internal inbox, not a person - show it as
    'Our Advisor' instead of 'Tech'."""
    local = (email or "").split("@")[0]
    if local.lower() == "tech":
        return "Our Advisor"
    parts = re.split(r"[._]+", local)
    return " ".join(p.capitalize() for p in parts if p) or "Your advisor"


def _parse_choice(choice: str, slot_count: int) -> Optional[int]:
    """Accepts '2', 'option 2', '2nd', etc. Returns a 1-based index within
    range, or None if it can't be parsed / is out of range."""
    if not choice:
        return None
    m = re.search(r"\d+", choice)
    if not m:
        return None
    n = int(m.group())
    if 1 <= n <= slot_count:
        return n
    return None


_PRIORITY_LABELS = {
    "possession": "Near Possession",
    "amenities": "Amenities",
    "builder": "A Reputed Builder",
}


@app.post("/parse-priorities")
def parse_priorities(req: PriorityParseRequest):
    """Turns the free-text reply to 'Which factors matter? (1,2,3)' into
    flat yes/no strings WATI's Condition nodes can branch on directly with
    Equal (the only comparison confirmed to work reliably in this workflow -
    see claude.md). Accepts digits ('1,2'), words ('possession and
    amenities'), or a mix; unrecognisable input asks about all three rather
    than silently dropping the customer's stated interest in 'multiple'.

    Stateless and side-effect free (no DB write, no external call), so this
    endpoint does not need the conversation lock - there is nothing here a
    duplicate/rapid call could corrupt."""
    raw = _clean_incoming(req.priority_selection).lower()

    numbers = set(re.findall(r"\d", raw))
    want_possession = "1" in numbers or "possession" in raw
    want_amenities = "2" in numbers or "amenit" in raw
    want_builder = "3" in numbers or "builder" in raw

    if not (want_possession or want_amenities or want_builder):
        want_possession = want_amenities = want_builder = True

    chosen = []
    if want_possession:
        chosen.append(_PRIORITY_LABELS["possession"])
    if want_amenities:
        chosen.append(_PRIORITY_LABELS["amenities"])
    if want_builder:
        chosen.append(_PRIORITY_LABELS["builder"])

    return {
        "want_possession": "yes" if want_possession else "no",
        "want_amenities": "yes" if want_amenities else "no",
        "want_builder": "yes" if want_builder else "no",
        "priority_list": ", ".join(chosen),
        "multiple_priority": "yes",
    }


# --------------------------------------------------------------------------
# Follow-up: user tapped "Talk to an Advisor" from the re-engagement message.
# Notifies all advisors by email and permanently closes the conversation so
# the scheduler never sends another nudge to this phone.
# --------------------------------------------------------------------------

class AdvisorRequestRequest(BaseModel):
    phone: Optional[str] = ""
    name: Optional[str] = ""


@app.post("/advisor-request")
def advisor_request(req: AdvisorRequestRequest):
    """Called when a user taps 'Talk to an Advisor' on the WATI follow-up
    message. Emails all advisors and closes the conversation tracking row.

    Reuses email_service.send_booking_notification as-is — it already handles
    missing lead details gracefully (no slot_label, no property_ref etc.) and
    logs + returns False on any failure without raising. The response is always
    200 so WATI doesn't retry; we degrade to "advisor will call" if email fails.

    Wrapped in the conversation lock — a rapid double-tap on the button would
    otherwise fire two advisor emails for the same lead."""
    phone = _clean_incoming(req.phone)
    name = _clean_incoming(req.name)

    if not phone:
        return {"notified": "no", "message": "Couldn't identify your number. An advisor will be in touch shortly."}

    off_hours_text = _off_hours_text(phone)
    if off_hours_text is not None:
        return {"notified": "no", "message": off_hours_text, "business_hours": "no"}

    if not conversation_lock.acquire(phone):
        return {"notified": "no", "message": "Still processing — please wait a moment."}

    try:
        advisors = calendar_service.advisor_emails()
        email_service.send_booking_notification(advisors, {
            "name": name,
            "phone": phone,
            "appt_type": "followup_advisor_request",
        })
        # Mark the conversation closed so the scheduler never sends another nudge.
        try:
            conversation_tracker.close_conversation(phone)
        except Exception as e:
            print(f"[app] conversation_tracker.close_conversation failed: {e}")
        return {
            "notified": "yes",
            "message": "Perfect! An Indihomes advisor will contact you shortly.",
        }
    finally:
        conversation_lock.release(phone)


# --------------------------------------------------------------------------
# Global intent handling: free text that doesn't match whatever button/
# number a WATI node was expecting. See claude.md, "Free-text handling",
# for the production transcript that motivated this and the required WATI
# wiring to get full coverage (not just the /property-detail node, which is
# patched directly above to cover the exact case that was observed live).
# --------------------------------------------------------------------------

def _run_global_intent(intent: dict, req, phone: str) -> dict:
    """Executes the side effects for one classified global intent (see
    intent_router.classify) and returns a flat, WATI-friendly dict.

    Shared by /interpret-message (the general "any node, any time" fallback)
    and /property-detail's unparseable-choice branch (the specific node
    where this was actually observed failing in production). `req` may be
    an InterpretMessageRequest or a PropertyDetailRequest - both carry the
    same optional slot field names (location, configuration, budget, etc.),
    accessed defensively with getattr so either shape works and a field
    missing from one model is never a crash.

    Every branch is best-effort and defensive by the same contract every
    other endpoint in this file follows: a failure in a side effect (DB
    write, email, tracker touch) is logged and swallowed, never allowed to
    turn into a 500 back to WATI mid-conversation.
    """
    name = _clean_incoming(getattr(req, "name", "") or "")
    kind = intent.get("intent", "none")

    if kind == "stop":
        # Compliance-critical: permanently do-not-contact, independent of
        # whatever conversation_tracker row exists right now or later.
        if phone:
            try:
                appointments_db.mark_opted_out(phone)
                conversation_tracker.close_conversation(phone)
            except Exception as e:
                print(f"[app] opt-out handling failed: {e}")
        return {"handled": "yes", "action": "stop",
                "reply_text": "You won't hear from us again. Take care!"}

    if kind == "talk_to_advisor":
        # Reuse advisor_request() as-is (locking, email, tracker close all
        # already handled there) rather than duplicating that logic here.
        result = advisor_request(AdvisorRequestRequest(phone=phone, name=name))
        return {"handled": "yes", "action": "talk_to_advisor",
                "reply_text": result.get("message", "")}

    if kind == "reject_all":
        # Deliberately does NOT re-run search on its own - "none of these"
        # doesn't tell us what WOULD fit. Offer the two real next steps and
        # let the customer's next message (a new area/budget, or "advisor")
        # drive the actual next action.
        return {"handled": "yes", "action": "reject_all", "offer_widen": "yes",
                "reply_text": ("No problem - would you like me to widen the search "
                                "(a different area or a higher budget), or have an "
                                "advisor call you instead?")}

    if kind == "restart":
        if phone:
            try:
                appointments_db.clear_pending_slots(phone)
                appointments_db.clear_pending_clarification(phone)
                appointments_db.reset_location_retry(phone)
            except Exception as e:
                print(f"[app] restart cleanup failed: {e}")
        return {"handled": "yes", "action": "restart",
                "reply_text": "Sure, let's start again - which area are you looking in?"}

    if kind == "none":
        # Genuinely unrecognized free text at some button/question node that
        # has no dedicated path for it - the exact class of bug behind the
        # Hitesh transcript (Malad/Goregaon/"Other Area" at the area picker)
        # and its siblings at every OTHER InteractiveButtons node in the
        # flow. Previously this fell all the way through to the final
        # `return {"handled": "no", "action": "none", "reply_text": ""}`
        # at the bottom of this function - WATI's own is_global=="no" branch
        # already shows a real static "please tap a button" message rather
        # than this empty reply_text (see main_message-intfallback in the
        # flow), so the customer was never shown a blank bubble - but
        # NOTHING was ever logged anywhere. The lead just silently sat
        # there, indistinguishable in the CRM from someone who simply never
        # replied. See claude.md, "Lead-safety-net", for the full writeup.
        #
        # flow_step identifies WHERE this happened, so an advisor opening
        # the queue sees e.g. "budget" or "location_selection", not just a
        # phone number - see appointments_db.mark_needs_human. Callers set
        # this per node (InterpretMessageRequest.flow_step); /property-detail
        # always means the same node, so it passes a fixed value.
        flow_step = _clean_incoming(getattr(req, "flow_step", "") or "") or "property_picker"
        raw_text = _clean_incoming(getattr(req, "message", "") or getattr(req, "choice", "") or "")
        if phone:
            try:
                appointments_db.mark_needs_human(phone, name, flow_step, raw_text)
            except Exception as e:
                print(f"[app] mark_needs_human failed: {e}")
        # reply_text left as-is below (empty) - is_global="no" callers
        # (both /interpret-message and /property-detail) already fall
        # through to their own real, non-blank local copy and must keep
        # doing so; this branch's only job is the logging side effect above.

    if kind == "change_location":
        normalized = intent_router.resolve_location_text(intent.get("location_text", ""))
        if not normalized:
            # IMPORTANT: always include "recommendations" here, even empty,
            # even though this branch never calls search(). WATI's
            # {{recommendations}} is a Contact Attribute (persistent per
            # contact, not reset per turn) - if this contact has NEVER had
            # it set before, WATI has nothing to substitute and prints the
            # literal unsubstituted "{{recommendations}}" token straight
            # into the WhatsApp message instead of leaving it blank. Same
            # failure mode _clean_incoming() already guards against on the
            # INBOUND side; this is the outbound mirror of it. Caught live
            # in production - see claude.md, "Free-text handling" changelog.
            return {"handled": "no", "action": "change_location",
                    "reply_text": "Sorry, I didn't catch which area you meant.",
                    "recommendations": ""}

        # Same lock discipline as one_call_search() - a rapid double-tap or
        # a WATI retry for the same phone must not race two searches.
        if not conversation_lock.acquire(phone):
            return {"handled": "no", "action": "change_location",
                    "reply_text": "Still working on your last request - one moment.",
                    "recommendations": ""}
        try:
            # Calls property_core.search() directly (already imported at the
            # top of this file) rather than going through run_pipeline() -
            # the location is already normalized/whitelisted here, so a
            # second Groq round trip on the same text would just be wasted
            # latency and cost for the same answer.
            result = search(
                location="|".join(normalized),
                configuration=_clean_incoming(getattr(req, "configuration", "")),
                budget=_clean_incoming(getattr(req, "budget", "")),
                amenities=_clean_incoming(getattr(req, "amenities", "")),
                possession=(_clean_incoming(getattr(req, "possession", "")) or
                            _clean_incoming(getattr(req, "possession_pref", ""))),
                limit=5,
            )
            if phone and result.get("shortlist"):
                try:
                    appointments_db.save_shortlist(phone, result["shortlist"])
                except Exception as e:
                    print(f"[app] could not save shortlist (global intent): {e}")
            if phone and result.get("count"):
                try:
                    conversation_tracker.touch_bot_message(phone, name)
                except Exception as e:
                    print(f"[app] conversation_tracker.touch_bot_message failed: {e}")
        finally:
            conversation_lock.release(phone)

        result["handled"] = "yes"
        result["action"] = "change_location"
        result["resolved_location"] = "|".join(normalized)
        result["reply_text"] = "Sure — here's what's available there:"
        return result

    return {"handled": "no", "action": "none", "reply_text": ""}


class InterpretMessageRequest(BaseModel):
    """Body for POST /interpret-message - the generic fallback endpoint.
    Wire this to WATI's catch-all / "no match" node so free text typed at
    ANY point in the flow gets a chance at a real response instead of a dead
    end. See claude.md, "Free-text handling", for the WATI-side wiring this
    requires (it is a separate node from the per-question webhooks, so it
    needs its own edge from the flow's fallback path).

    Every slot field mirrors SearchRequest and is OPTIONAL - WATI should
    forward whatever custom attributes it has collected so far (they persist
    across the whole conversation as Custom Attributes), so a change_location
    intent can re-run search with the customer's existing budget/
    configuration/etc. instead of starting from nothing.
    """
    phone: Optional[str] = ""
    name: Optional[str] = ""
    message: Optional[str] = ""
    location: Optional[str] = ""
    location_text: Optional[str] = ""
    configuration: Optional[str] = ""
    budget: Optional[str] = ""
    purpose: Optional[str] = ""
    amenities: Optional[str] = ""
    builder: Optional[str] = ""
    builder_pref: Optional[str] = ""
    possession: Optional[str] = ""
    possession_pref: Optional[str] = ""
    # Which WATI node's default/no-match path called this. Set to a
    # distinct hardcoded value in each button node's webhook body (e.g.
    # "budget", "location_selection", "consent") - see
    # Indihomes-main_updated.json and claude.md, "Lead-safety-net". Purely
    # for the needs_human log (appointments_db.mark_needs_human); has no
    # effect on intent classification or routing. Falls back to
    # "property_picker" if omitted, since that's the one call site
    # (/property-detail) that pre-dates this field.
    flow_step: Optional[str] = ""


@app.post("/interpret-message")
def interpret_message(req: InterpretMessageRequest):
    """Classifies free text for a global intent (see intent_router.py) and
    acts on it if one is found. Point WATI's fallback/"no match" node at
    this endpoint so a customer typing something the button graph didn't
    expect - anywhere in the conversation - gets routed to a real response
    (widen the search, connect an advisor, change area, restart, opt out)
    instead of a dead end or a repeated "I didn't understand" loop.

    Returns `is_global: "yes"/"no"` so a WATI Condition node can decide
    whether to use `reply_text` (and, for change_location, the usual
    @recommendations / @count / @name1.. fields) or fall through to
    whatever local retry copy that node already had.
    """
    phone = _clean_incoming(req.phone)
    text = _clean_incoming(req.message)

    if phone:
        try:
            conversation_tracker.touch_user_message(phone)
        except Exception as e:
            print(f"[app] conversation_tracker.touch_user_message failed: {e}")

    off_hours_text = _off_hours_text(phone)
    if off_hours_text is not None:
        return {"intent": "none", "is_global": "no", "handled": "no",
                "action": "off_hours", "reply_text": off_hours_text,
                "recommendations": "", "business_hours": "no"}

    intent = intent_router.classify(text)
    out = _run_global_intent(intent, req, phone)
    out["intent"] = intent.get("intent", "none")
    out["is_global"] = "yes" if intent.get("intent", "none") != "none" else "no"
    return out


class AvailableSlotsRequest(BaseModel):
    phone: Optional[str] = ""
    appt_type: Optional[str] = "site_visit"


@app.post("/available-slots")
def available_slots(req: AvailableSlotsRequest):
    """Shows the customer real free slots from the shared calendar, and
    remembers them (keyed on phone) so /book-slot can resolve their reply."""
    phone = _clean_incoming(req.phone)
    appt_type = _clean_incoming(req.appt_type) or "site_visit"
    # User actively requesting slots — cancel any pending follow-up.
    if phone:
        try:
            conversation_tracker.touch_user_message(phone)
        except Exception as e:
            print(f"[app] conversation_tracker.touch_user_message failed: {e}")

    off_hours_text = _off_hours_text(phone)
    if off_hours_text is not None:
        return {"slots_text": off_hours_text, "has_slots": "no", "slot_count": 0,
                "business_hours": "no"}

    if not phone:
        return {
            "slots_text": "I couldn't find your number to check slots against. "
                          "One of our advisors will call you shortly to arrange a time.",
            "has_slots": "no",
            "slot_count": 0,
        }

    try:
        slots = calendar_service.get_free_slots(days_ahead=5, limit=5)
    except Exception:
        slots = []

    if not slots:
        return {
            "slots_text": "I couldn't find an open slot right now, but one of our advisors "
                          "will call you shortly to arrange a time that works for you.",
            "has_slots": "no",
            "slot_count": 0,
        }

    appointments_db.save_pending_slots(phone, slots)
    slots_text = "\n".join(f"{s['index']}. {s['label']}" for s in slots)
    return {
        "slots_text": slots_text,
        "has_slots": "yes",
        "slot_count": len(slots),
    }


class BookSlotRequest(BaseModel):
    phone: Optional[str] = ""
    choice: Optional[str] = ""
    name: Optional[str] = ""
    appt_type: Optional[str] = "site_visit"
    property_ref: Optional[str] = ""
    # Lead requirements, forwarded from the WATI flow so the advisor's email
    # carries the full picture, not just a name and a slot. All optional - if
    # the flow doesn't send them, the email simply omits those lines.
    budget: Optional[str] = ""
    configuration: Optional[str] = ""
    location: Optional[str] = ""


@app.post("/book-slot")
def book_slot(req: BookSlotRequest):
    """Resolves the customer's numbered reply against the slots we showed
    them, books the calendar event, and stores the appointment."""
    phone = _clean_incoming(req.phone)
    choice = _clean_incoming(req.choice)
    # User actively booking — cancel follow-up timer immediately, before any
    # lock acquisition, so even a lock-miss doesn't leave a stale timer.
    if phone:
        try:
            conversation_tracker.touch_user_message(phone)
        except Exception as e:
            print(f"[app] conversation_tracker.touch_user_message failed: {e}")

    off_hours_text = _off_hours_text(phone)
    if off_hours_text is not None:
        # Booking has real side effects (a calendar event, an advisor
        # notification) - off hours, we must not create either. Skipping
        # entirely here, before even parsing `choice`, is deliberate: no
        # slot should ever be confirmed outside business hours.
        return {"booked": "no", "message": off_hours_text, "advisor": "",
                "slot_label": "", "business_hours": "no"}

    name = _clean_incoming(req.name)
    appt_type = _clean_incoming(req.appt_type) or "site_visit"
    property_ref = _clean_incoming(req.property_ref)

    fallback_message = "One of our advisors will call you shortly to arrange a time."

    if not phone:
        return {"booked": "no", "message": fallback_message, "advisor": "", "slot_label": ""}

    # Booking has real side effects (Google Calendar event, DB row, advisor
    # email) - a duplicate webhook retry or a fast double-tap on the slot
    # number must not book twice. A lock-miss returns the SAME shape as
    # "couldn't parse your choice" so it lands on the existing retry message
    # in the WATI flow rather than needing a new branch.
    if not conversation_lock.acquire(phone):
        return {
            "booked": "no",
            "message": "Still processing your previous request - please wait a moment.",
            "advisor": "", "slot_label": "",
        }

    try:
        return _book_slot_locked(phone, choice, name, appt_type, property_ref, req, fallback_message)
    finally:
        conversation_lock.release(phone)


def _book_slot_locked(phone, choice, name, appt_type, property_ref, req, fallback_message):
    """The original /book-slot body, now run only while the conversation
    lock for `phone` is held (see book_slot() above). Split out so the route
    function itself stays a thin acquire/release wrapper."""
    pending = appointments_db.get_pending_slots(phone)
    if not pending:
        return {
            "booked": "no",
            "message": "That slot list has expired. Please ask for available slots again.",
            "advisor": "", "slot_label": "",
        }

    idx = _parse_choice(choice, len(pending))
    if idx is None:
        return {
            "booked": "no",
            "message": f"Please reply with a number between 1 and {len(pending)} to pick a slot.",
            "advisor": "", "slot_label": "",
        }

    slot = pending[idx - 1]

    if appointments_db.is_slot_taken(slot["start_iso"]):
        return {
            "booked": "no",
            "message": "Sorry, that slot was just taken. Please ask for available slots again.",
            "advisor": "", "slot_label": "",
        }

    advisors = calendar_service.advisor_emails()
    advisor_email = appointments_db.next_advisor(advisors) or ""

    event_id = None
    try:
        event_id = calendar_service.create_event(
            slot_iso=slot["start_iso"],
            customer_name=name,
            customer_phone=phone,
            advisor_email=advisor_email,
            notes=f"Type: {appt_type}" + (f", Property: {property_ref}" if property_ref else ""),
        )
    except Exception:
        event_id = None

    if event_id is None:
        return {"booked": "no", "message": fallback_message, "advisor": "", "slot_label": ""}

    appointments_db.save_appointment(
        lead_phone=phone,
        lead_name=name,
        advisor_email=advisor_email,
        property_ref=property_ref,
        google_event_id=event_id,
        slot_start=slot["start_iso"],
        appt_type=appt_type,
    )
    appointments_db.clear_pending_slots(phone)

    # Notify ALL advisors by email with the lead's full requirements.
    # Best-effort only: the booking is already confirmed (calendar event +
    # stored appointment), so a failed email must never change the result we
    # return to WATI. email_service logs and returns False on any problem.
    # (advisor_email above is still the round-robin pick used for the calendar
    # event and the stored record; the email just goes to everyone.)
    email_service.send_booking_notification(advisors, {
        "name": name,
        "phone": phone,
        "slot_label": slot["label"],
        "appt_type": appt_type,
        "budget": _clean_incoming(req.budget),
        "configuration": _clean_incoming(req.configuration),
        "location": _clean_incoming(req.location),
        "property_ref": property_ref,
    })

    advisor_name = _advisor_display_name(advisor_email)
    # Booking confirmed — close the conversation so the scheduler never
    # sends a follow-up nudge to a lead who just booked a site visit.
    try:
        conversation_tracker.close_conversation(phone)
    except Exception as e:
        print(f"[app] conversation_tracker.close_conversation failed: {e}")
    return {
        "booked": "yes",
        "message": f"Appointment confirmed for {slot['label']}. Our Advisor from Indihomes will Contact you.",
        "advisor": advisor_name,
        "slot_label": slot["label"],
    }


# --------------------------------------------------------------------------
# CRM: save the lead at the END of the conversation (one call per lead).
# The `outcome` WATI sends decides the status we record.
# --------------------------------------------------------------------------
OUTCOME_STATUS = {
    "site_visit_scheduled": ("Interested", "Site Visit Scheduled"),
    "details_shared":       ("Interested", "Details Shared"),
    "not_interested":       ("Not Interested", ""),
    "wip":                  ("WIP", ""),
}


class SaveLeadRequest(BaseModel):
    phone: Optional[str] = ""
    name: Optional[str] = ""
    location: Optional[str] = ""
    budget: Optional[str] = ""
    configuration: Optional[str] = ""
    possession_pref: Optional[str] = ""
    purpose: Optional[str] = ""
    amenities: Optional[str] = ""
    project_code: Optional[str] = ""      # e.g. @code1 from /search, or the picked one
    recommendations: Optional[str] = ""   # the shortlist text we showed
    outcome: Optional[str] = "details_shared"


@app.post("/save-lead")
def save_lead(req: SaveLeadRequest):
    """Push the finished conversation to the CRM via createLead. Safe by default:
    crm_service runs in DRY-RUN (logs the payload, writes nothing) until
    CRM_DRY_RUN=false. Never returns a 500 to WATI."""
    phone = _clean_incoming(req.phone)
    if not phone:
        return {"saved": "no", "message": "No phone on record.", "dry_run": "yes"}

    # A duplicate webhook retry must not push the same lead to the CRM twice.
    if not conversation_lock.acquire(phone):
        return {
            "saved": "no",
            "message": "Still processing your previous request - please wait a moment.",
            "dry_run": "yes",
        }
    try:
        return _save_lead_locked(req, phone)
    finally:
        conversation_lock.release(phone)


def _save_lead_locked(req: SaveLeadRequest, phone: str):
    """The original /save-lead body, now run only while the conversation
    lock for `phone` is held (see save_lead() above)."""
    outcome = (_clean_incoming(req.outcome) or "details_shared").lower()
    main_status, sub_status = OUTCOME_STATUS.get(outcome, ("Interested", "Details Shared"))

    purpose = _clean_incoming(req.purpose).lower()
    user_type = "Investor" if "invest" in purpose else ("Buyer" if purpose else "")

    # All the human context goes into notes (createLead's free-text field the
    # calling team reads). Status is recorded here too until we confirm whether
    # createLead accepts a status field / has an update endpoint.
    status_line = f"Status: {main_status}" + (f" / {sub_status}" if sub_status else "")
    note_bits = [status_line]
    if _clean_incoming(req.location):
        note_bits.append(f"Preferred area: {_clean_incoming(req.location)}")
    if _clean_incoming(req.budget):
        note_bits.append(f"Budget: {_clean_incoming(req.budget)}")
    if _clean_incoming(req.configuration):
        note_bits.append(f"Configuration: {_clean_incoming(req.configuration)}")
    if _clean_incoming(req.possession_pref):
        note_bits.append(f"Possession: {_clean_incoming(req.possession_pref)}")
    if _clean_incoming(req.purpose):
        note_bits.append(f"Purpose: {_clean_incoming(req.purpose)}")
    if _clean_incoming(req.amenities):
        note_bits.append(f"Amenities wanted: {_clean_incoming(req.amenities)}")
    if _clean_incoming(req.recommendations):
        note_bits.append("Shortlist shown:\n" + _clean_incoming(req.recommendations))
    notes = "\n".join(note_bits)

    result = crm_service.push_lead({
        "name": _clean_incoming(req.name),
        "phone": phone,
        "email": "",  # not collected over WhatsApp
        "configuration": _clean_incoming(req.configuration),
        "project_code": _clean_incoming(req.project_code),
        "target_possession": "",  # only a preference; kept in notes
        "budget": _clean_incoming(req.budget),
        "location": _clean_incoming(req.location),
        "notes": notes,
        "lead_source": "WhatsApp Bot",
        "user_type": user_type,
    })

    # Lead saved — close the conversation regardless of CRM success so the
    # scheduler doesn't nudge a lead who has already reached this endpoint.
    try:
        conversation_tracker.close_conversation(phone)
    except Exception as e:
        print(f"[app] conversation_tracker.close_conversation failed: {e}")
    return {
        "saved": "yes" if result.get("ok") else "no",
        "message": "Your details are noted; our team will be in touch."
                   if result.get("ok") else
                   "Our team will follow up with you shortly.",
        "dry_run": "yes" if result.get("dry_run") else "no",
    }


@app.get("/needs-human-leads")
def needs_human_leads(limit: int = 100, include_notified: bool = False):
    """Advisor-facing worklist: every unresolved free-text dead end
    (see appointments_db.mark_needs_human / _run_global_intent's "none"
    branch above), newest first. `include_notified=false` (the default)
    hides rows already acknowledged via /needs-human-leads/ack, so a
    dashboard or Phase 2 poller only ever sees what's actually new.

    Deliberately unauthenticated for now, same trust boundary as every
    other endpoint in this file (all are behind the same private backend
    URL WATI/Phase 2 call) - add auth before exposing this publicly."""
    rows = appointments_db.list_needs_human(unnotified_only=not include_notified, limit=limit)
    return {"count": len(rows), "leads": rows}


class AckNeedsHumanRequest(BaseModel):
    ids: list = []


@app.post("/needs-human-leads/ack")
def ack_needs_human_leads(req: AckNeedsHumanRequest):
    """Marks the given needs_human row ids as notified/handled - called by
    whatever advisor tool (or Phase 2's polling worker) just surfaced them,
    so the next /needs-human-leads call doesn't show the same rows again."""
    try:
        ids = [int(i) for i in (req.ids or [])]
    except (TypeError, ValueError):
        return {"acked": "no", "message": "ids must be a list of integers"}
    appointments_db.mark_needs_human_notified(ids)
    return {"acked": "yes", "count": len(ids)}


@app.get("/health")
def health():
    groq = os.environ.get("GROQ_API_KEY") or ""
    llm = ("groq: loaded (ends ..." + groq[-4:] + "), model=" + GROQ_MODEL) if groq else "NO KEY LOADED"
    calendar_status = "connected" if calendar_service.is_configured() else "NOT CONFIGURED"
    email_status = "connected" if email_service.is_configured() else "NOT CONFIGURED"
    wati_status = "connected" if wati_client.is_configured() else "NOT CONFIGURED"
    scheduler_status = "running" if _followup_scheduler._scheduler_running() else "NOT RUNNING"
    return {
        "status": "ok",
        "properties_loaded": len(PROPERTIES),
        "known_localities": KNOWN_LOCALITIES,
        "llm": llm,
        "calendar": calendar_status,
        "email": email_status,
        "wati": wati_status,
        "scheduler": scheduler_status,
        "advisors_loaded": len(calendar_service.advisor_emails()),
        "opted_out_count": appointments_db.opted_out_count(),
        "needs_human_count": appointments_db.needs_human_count(),
        "business_hours": "open" if business_hours.is_business_hours() else "closed",
    }