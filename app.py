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


@app.get("/health")
def health():
    groq = os.environ.get("GROQ_API_KEY") or ""
    llm = ("groq: loaded (ends ..." + groq[-4:] + "), model=" + GROQ_MODEL) if groq else "NO KEY LOADED"
    return {
        "status": "ok",
        "properties_loaded": len(PROPERTIES),
        "known_localities": KNOWN_LOCALITIES,
        "llm": llm,
    }