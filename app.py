"""
Indihomes chatbot backend - single app exposing:
  POST /location            (LLM location extraction + clarification)
  POST /debug-location      (same, but shows raw LLM output / errors)
  POST /api/property-search (final property search)
  GET  /health


  
Run:
  uvicorn app:app --host 0.0.0.0 --port 8000
Expose:
  ngrok http 8000
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
    LocationRequest,
    GROQ_MODEL,
)

app = FastAPI()


class LeadRequest(BaseModel):
    location: Optional[str] = ""
    configuration: Optional[str] = ""
    budget: Optional[str] = ""
    purpose: Optional[str] = ""
    amenities: Optional[str] = ""
    builder: Optional[str] = ""
    possession: Optional[str] = ""


@app.post("/location")
def location(req: LocationRequest):
    return location_handler(req)


@app.post("/debug-location")
def debug_location(req: LocationRequest):
    return location_debug_handler(req)


@app.post("/api/property-search")
def property_search(lead: LeadRequest):
    return search(
        location=lead.location,
        configuration=lead.configuration,
        budget=lead.budget,
        amenities=lead.amenities,
        possession=lead.possession,
    )


@app.get("/health")
def health():
    groq = os.environ.get("GROQ_API_KEY") or ""
    anthropic = os.environ.get("ANTHROPIC_API_KEY") or ""
    if groq:
        llm = "groq: loaded (ends ..." + groq[-4:] + "), model=" + GROQ_MODEL
    elif anthropic:
        llm = "anthropic: loaded (ends ..." + anthropic[-4:] + ")"
    else:
        llm = "NO KEY LOADED"
    return {
        "status": "ok",
        "properties_loaded": len(PROPERTIES),
        "known_localities": KNOWN_LOCALITIES,
        "llm": llm,
    }