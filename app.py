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
from sanitize import sanitise
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

app = FastAPI()


def _clean_incoming(value: str, max_len: int = 100) -> str:
    """Every free-text field WATI sends passes through here before it
    reaches search(), a CRM payload, or a calendar/email description.
    Delegates to the shared sanitiser: strips control/zero-width characters,
    collapses whitespace, caps length, and strips unresolved WATI
    placeholders ({{var}}, @var) - e.g. an unsubstituted {{location}}."""
    return sanitise(value, max_len)


class SearchRequest(BaseModel):
    """Everything the chatbot collected, sent in one go."""
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


def run_pipeline(req: SearchRequest):
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
    )

    if llm_note and result.get("count"):
        result["recommendations"] = llm_note + "\n\n" + result["recommendations"]

    result["resolved_location"] = resolved_location
    return result, raw_location


@app.post("/search")
def one_call_search(req: SearchRequest):
    """THE endpoint the chatbot should call at the end of the conversation."""
    result, _ = run_pipeline(req)
    return result


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
        location=_clean_incoming(lead.location),
        configuration=_clean_incoming(lead.configuration),
        budget=_clean_incoming(lead.budget),
        amenities=_clean_incoming(lead.amenities),
        possession=_clean_incoming(lead.possession),
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
    a clarify_question, and normalized_location. phone (if sent) keys the
    retry counter so repeated failed attempts escalate to a handoff instead
    of looping the customer forever."""
    return location_handler(LocationRequest(
        message=req.best(),
        phone=_clean_incoming(req.phone, 32),
    ))


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


class AvailableSlotsRequest(BaseModel):
    phone: Optional[str] = ""
    appt_type: Optional[str] = "site_visit"


@app.post("/available-slots")
def available_slots(req: AvailableSlotsRequest):
    """Shows the customer real free slots from the shared calendar, and
    remembers them (keyed on phone) so /book-slot can resolve their reply."""
    phone = _clean_incoming(req.phone, 32)
    appt_type = _clean_incoming(req.appt_type) or "site_visit"

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
    phone = _clean_incoming(req.phone, 32)
    choice = _clean_incoming(req.choice, 20)
    name = _clean_incoming(req.name)
    appt_type = _clean_incoming(req.appt_type) or "site_visit"
    property_ref = _clean_incoming(req.property_ref, 200)

    fallback_message = "One of our advisors will call you shortly to arrange a time."

    if not phone:
        return {"booked": "no", "message": fallback_message, "advisor": "", "slot_label": ""}

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
    return {
        "booked": "yes",
        "message": f"Appointment confirmed for {slot['label']}. Our Advisor from Indihomes will Contact you.",
        "advisor": advisor_name,
        "slot_label": slot["label"],
    }


@app.get("/health")
def health():
    groq = os.environ.get("GROQ_API_KEY") or ""
    llm = ("groq: loaded (ends ..." + groq[-4:] + "), model=" + GROQ_MODEL) if groq else "NO KEY LOADED"
    calendar_status = "connected" if calendar_service.is_configured() else "NOT CONFIGURED"
    email_status = "connected" if email_service.is_configured() else "NOT CONFIGURED"
    return {
        "status": "ok",
        "properties_loaded": len(PROPERTIES),
        "known_localities": KNOWN_LOCALITIES,
        "llm": llm,
        "calendar": calendar_status,
        "email": email_status,
        "advisors_loaded": len(calendar_service.advisor_emails()),
    }