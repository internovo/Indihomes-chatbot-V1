"""
Indihomes Hybrid Location Intelligence - location extraction handler.

Pipeline:
  user text -> LLM (intent/location extraction, JSON only)
            -> validate candidates against real inventory
            -> normalize (Dahisar -> Dahisar East + Dahisar West, spelling fixes)
            -> if ambiguous / low confidence -> ask clarification
            -> else return normalized location (search runs later in the flow)

The LLM ONLY extracts intent. All search + business rules stay in the backend.

Provider: Groq (free tier, OpenAI-compatible).

NOTE: needs_clarification is returned as the STRING "yes" / "no" (not a boolean)
so WATI's condition node can compare it reliably.

Setup:
    pip install openai
    .env:  GROQ_API_KEY=gsk_...  (free key: https://console.groq.com/keys)
"""

import os
import re
import json
from pydantic import BaseModel
from typing import Optional

from property_core import KNOWN_LOCALITIES_LOWER, search

# Deterministic normalization: generic area -> the specific localities to search.
SPLIT_RULES = {
    "malad": ["Malad East", "Malad West"],
    "andheri": ["Andheri East", "Andheri West"],
    "borivali": ["Borivali East", "Borivali West"],
    "kandivali": ["Kandivali East", "Kandivali West"],
    "goregaon": ["Goregaon East", "Goregaon West"],
    "dahisar": ["Dahisar East", "Dahisar West"],
}

GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_BASE_URL = "https://api.groq.com/openai/v1"

SYSTEM_PROMPT = """You are a location extraction engine for a Mumbai real-estate assistant.
From the user's message, extract their preferred locality or landmark.

Respond with ONLY a valid JSON object. No prose, no markdown, no code fences.
The JSON must have exactly these keys:
{
  "location": <specific locality string, or null>,
  "landmark": <landmark name, or null>,
  "ambiguous": <true if the area or landmark could map to more than one distinct locality, else false>,
  "candidate_localities": [<list of possible locality strings when ambiguous, else empty list>],
  "confidence": <number between 0.0 and 1.0>
}

Rules:
- If the user names a specific locality (e.g. "Malad West"), set location to it and ambiguous to false.
- If they name a generic area that has East and West parts (e.g. "Dahisar", "Kandivali", "Malad"), set location to that area, set ambiguous to true, and list both parts in candidate_localities (e.g. ["Dahisar East", "Dahisar West"]).
- If they name a landmark that exists in MULTIPLE Mumbai localities (e.g. "Infinity Mall" is in both Malad and Andheri), set ambiguous to true and list candidate_localities.
- If they name a landmark that is in one clear locality, set location to that locality and ambiguous to false.
- If uncertain, set confidence below 0.7.
- Correct obvious spelling mistakes (e.g. "Mald" -> "Malad", "Borivli" -> "Borivali").
- Never invent properties and never give recommendations."""


def _extract_json(text: str) -> dict:
    """Open-weight models sometimes wrap JSON in prose or fences. Dig it out."""
    if not text:
        raise ValueError("empty response")
    cleaned = text.strip().replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        return json.loads(match.group(0))
    raise ValueError("no JSON found in response: " + text[:200])


def _passthrough(message, confidence=0.5, **extra):
    out = {"location": message, "landmark": None, "ambiguous": False,
           "candidate_localities": [], "confidence": confidence}
    out.update(extra)
    return out


def call_llm(message: str, debug: bool = False):
    """Call Groq (free tier, OpenAI-compatible) for extraction.
    Returns parsed dict, or (dict, info) when debug=True."""
    groq_key = os.environ.get("GROQ_API_KEY")

    if not message:
        result = _passthrough("", confidence=0.0)
        return (result, {"stage": "empty_message", "raw": ""}) if debug else result

    if not groq_key:
        result = _passthrough(message, _no_key=True)
        return (result, {"stage": "no_key", "raw": ""}) if debug else result

    raw_text = ""
    try:
        from openai import OpenAI
        client = OpenAI(api_key=groq_key, base_url=GROQ_BASE_URL)
        resp = client.chat.completions.create(
            model=GROQ_MODEL,
            max_tokens=300,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": message},
            ],
        )
        raw_text = resp.choices[0].message.content or ""

        parsed = _extract_json(raw_text)
        info = {"stage": "ok", "provider": "groq", "model": GROQ_MODEL, "raw": raw_text}
        return (parsed, info) if debug else parsed
    except Exception as e:
        result = _passthrough(message, confidence=0.3, _error=str(e))
        info = {"stage": "exception", "provider": "groq",
                "error_type": type(e).__name__, "error": str(e), "raw": raw_text}
        return (result, info) if debug else result


# Backwards-compatible alias
call_claude = call_llm


def validate_candidates(cands):
    """Keep only candidate localities we actually have inventory in."""
    valid = []
    for c in cands or []:
        cl = (c or "").strip().lower()
        if not cl:
            continue
        for known_lower, known in KNOWN_LOCALITIES_LOWER.items():
            if cl == known_lower or cl in known_lower or known_lower in cl:
                if known not in valid:
                    valid.append(known)
    return valid


def normalize_location(loc: str):
    """Generic area -> specific localities we stock. Returns a list."""
    if not loc:
        return []
    key = loc.strip().lower()
    for area, targets in SPLIT_RULES.items():
        if key == area:
            valid = validate_candidates(targets)
            return valid or [loc]
    exact = validate_candidates([loc])
    return exact or [loc]


class LocationRequest(BaseModel):
    message: str = ""
    configuration: Optional[str] = ""
    budget: Optional[str] = ""
    amenities: Optional[str] = ""
    possession: Optional[str] = ""


def _resolve(extracted: dict) -> dict:
    ambiguous = bool(extracted.get("ambiguous"))
    try:
        confidence = float(extracted.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    candidates = validate_candidates(extracted.get("candidate_localities"))

    if ambiguous and len(candidates) >= 2:
        opts = " or ".join(candidates[:3])
        landmark = extracted.get("landmark")
        if landmark:
            question = "There's more than one " + str(landmark) + " in Mumbai. Which area did you mean - " + opts + "?"
        else:
            question = "Just to narrow it down - " + opts + "?"
        return {
            "needs_clarification": "yes",
            "clarify_question": question,
            "clarify_options": candidates[:3],
            "normalized_location": "",
        }

    loc = extracted.get("location") or (candidates[0] if candidates else "")
    if not loc and confidence < 0.5:
        return {
            "needs_clarification": "yes",
            "clarify_question": "Could you tell me the area or locality you're interested in?",
            "clarify_options": [],
            "normalized_location": "",
        }

    normalized = normalize_location(loc) or ([loc] if loc else [])
    return {
        "needs_clarification": "no",
        "clarify_question": "",
        "clarify_options": [],
        "normalized_location": "|".join(normalized),
    }


def location(req: "LocationRequest") -> dict:
    raw = (req.message or "").strip()
    if raw.startswith("{{") and raw.endswith("}}"):
        raw = ""

    extracted = call_llm(raw)
    payload = _resolve(extracted)

    if payload["needs_clarification"] == "no" and any(
        [req.configuration, req.budget, req.amenities, req.possession]
    ):
        payload.update(search(
            location=payload["normalized_location"],
            configuration=req.configuration,
            budget=req.budget,
            amenities=req.amenities,
            possession=req.possession,
        ))
    return payload


def location_debug(req: "LocationRequest") -> dict:
    """Same as location() but exposes exactly what the LLM returned / any error."""
    raw = (req.message or "").strip()
    extracted, info = call_llm(raw, debug=True)
    resolved = _resolve(extracted)
    return {"llm_stage": info, "llm_parsed": extracted, "resolved": resolved}