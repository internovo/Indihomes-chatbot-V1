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

app = FastAPI()


def _clean_incoming(value: str) -> str:
    """WATI sometimes sends an unsubstituted {{var}} placeholder. Treat as empty."""
    v = (value or "").strip()
    if v.startswith("{{") and v.endswith("}}"):
        return ""
    return v


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

    def best(self) -> str:
        for c in (self.location_text, self.message, self.location):
            v = _clean_incoming(c)
            if v and v.lower() not in ("other area", "other"):
                return v
        return ""


@app.post("/location")
def location(req: FlexLocationRequest):
    """Location understanding only. Returns needs_clarification yes/no,
    a clarify_question, and normalized_location."""
    return location_handler(LocationRequest(message=req.best()))


@app.post("/debug-location")
def debug_location(req: LocationRequest):
    return location_debug_handler(req)


def _advisor_display_name(email: str) -> str:
    """Best-effort friendly name from an advisor's email, e.g.
    'arpit@internovo.in' -> 'Arpit'. We don't store real names anywhere."""
    local = (email or "").split("@")[0]
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
    phone = _clean_incoming(req.phone)
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


@app.post("/book-slot")
def book_slot(req: BookSlotRequest):
    """Resolves the customer's numbered reply against the slots we showed
    them, books the calendar event, and stores the appointment."""
    phone = _clean_incoming(req.phone)
    choice = _clean_incoming(req.choice)
    name = _clean_incoming(req.name)
    appt_type = _clean_incoming(req.appt_type) or "site_visit"
    property_ref = _clean_incoming(req.property_ref)

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
    return {
        "status": "ok",
        "properties_loaded": len(PROPERTIES),
        "known_localities": KNOWN_LOCALITIES,
        "llm": llm,
        "calendar": calendar_status,
        "advisors_loaded": len(calendar_service.advisor_emails()),
    }