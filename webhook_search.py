"""
Indihomes property-search webhook for WATI chatbot.
Loads real inventory from properties.json (same folder as this file).

Install:  pip install fastapi uvicorn
Run:      uvicorn webhook_search:app --host 0.0.0.0 --port 8000
Expose:   ngrok http 8000
"""

import json
import os
import re
from datetime import date
from fastapi import FastAPI
from pydantic import BaseModel
from typing import Optional

app = FastAPI()

# ---------------------------------------------------------------------------
# Load inventory once at startup.
# ---------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(HERE, "properties.json")

with open(DATA_PATH, "r", encoding="utf-8") as f:
    RAW = json.load(f)


def lakh_to_cr(value) -> float:
    """startingPrice.value is in Lakh. 129 Lakh -> 1.29 Cr."""
    try:
        return round(float(value) / 100.0, 2)
    except (TypeError, ValueError):
        return 0.0


def months_until(possession: str) -> int:
    """'YYYY-MM' -> whole months from today. Past/blank -> 0 (ready)."""
    if not possession:
        return 0
    m = re.match(r"(\d{4})-(\d{1,2})", possession.strip())
    if not m:
        return 0
    y, mo = int(m.group(1)), int(m.group(2))
    today = date.today()
    delta = (y - today.year) * 12 + (mo - today.month)
    return max(delta, 0)


def norm_config(text: str) -> str:
    """'2 BHK', '2bhk', '2 bhk' -> '2bhk'. Returns just the digit+bhk token."""
    t = (text or "").lower().replace(" ", "")
    m = re.search(r"(\d)\s*bhk", t)
    return f"{m.group(1)}bhk" if m else t


# Pre-process the raw records into a clean internal shape.
PROPERTIES = []
for r in RAW:
    loc = r.get("location") or {}
    price = r.get("startingPrice") or {}
    PROPERTIES.append({
        "name": r.get("projectName", "Indihomes project"),
        "location_label": (loc.get("label") or "").strip(),
        "location_value": (loc.get("value") or "").strip().lower(),
        "nearby": r.get("nearbyLocality", ""),
        "price_cr": lakh_to_cr(price.get("value")),
        "configs": [c.lower().replace(" ", "") for c in (r.get("flatConfiguration") or [])],
        "configs_display": r.get("flatConfiguration") or [],
        "possession_months": months_until(r.get("possessionStartDate", "")),
        "possession_raw": r.get("possessionStartDate", ""),
        "amenities": [(a.get("value") or "").lower() for a in (r.get("amenities") or [])],
        "amenities_display": [a.get("value") for a in (r.get("amenities") or [])],
        "description": (r.get("description") or "").replace("\n", " ").strip(),
        "brochure_url": r.get("brochure_url", ""),
        "media_urls": r.get("media_urls") or [],
        "carpet": r.get("carpetSize") or {},
    })


class LeadRequest(BaseModel):
    location: Optional[str] = ""
    configuration: Optional[str] = ""
    budget: Optional[str] = ""
    purpose: Optional[str] = ""
    amenities: Optional[str] = ""
    builder: Optional[str] = ""
    possession: Optional[str] = ""


def clean(val: str) -> str:
    """Strip unresolved WATI placeholders like {{location}} to empty."""
    v = (val or "").strip()
    if v.startswith("{{") and v.endswith("}}"):
        return ""
    return v


def budget_ceiling(budget_text: str) -> float:
    """Map button labels (and loose text) to an upper bound in crores."""
    t = (budget_text or "").lower().replace(" ", "")
    if "under1" in t or "below1" in t:
        return 1.0
    if "1cr-2cr" in t or ("1cr" in t and "2cr" in t):
        return 2.0
    if "above2" in t or "2cr+" in t:
        return 99.0
    return 99.0  # no/unknown budget -> don't filter on price


@app.post("/api/property-search")
def property_search(lead: LeadRequest):
    loc = clean(lead.location).lower()
    cfg = norm_config(clean(lead.configuration))
    ceiling = budget_ceiling(clean(lead.budget))
    wanted_amenities = [a.strip().lower() for a in clean(lead.amenities).replace(",", " ").split() if a.strip()]
    ready_only = "ready" in clean(lead.possession).lower() and "only" in clean(lead.possession).lower()

    def matches(p):
        # Location: partial match either way ("goregaon" matches "goregaon west")
        if loc:
            if loc not in p["location_value"] and p["location_value"] not in loc:
                # also try just the first word (area name)
                area = loc.split()[0] if loc.split() else loc
                if area not in p["location_value"]:
                    return False
        if cfg and not any(cfg == c or cfg in c for c in p["configs"]):
            return False
        if p["price_cr"] > ceiling:
            return False
        if ready_only and p["possession_months"] > 2:
            return False
        return True

    results = [p for p in PROPERTIES if matches(p)]

    # Rank by amenity overlap (each requested amenity that appears anywhere in the list)
    def amenity_score(p):
        return sum(1 for a in wanted_amenities if any(a in am for am in p["amenities"]))

    results.sort(key=amenity_score, reverse=True)
    top = results[:3]

    # Graceful fallback: loosen to location-only so a lead is never dropped.
    if not top:
        loose = [p for p in PROPERTIES if not loc or loc.split()[0] in p["location_value"]]
        top = loose[:3]

    # Build a WhatsApp-friendly text block.
    lines = []
    for p in top:
        if p["possession_months"] <= 0:
            poss = "Ready to move in"
        elif p["possession_months"] <= 2:
            poss = "Possession very soon"
        else:
            poss = f"Possession by {p['possession_raw']}"
        cfgs = " / ".join(p["configs_display"]) if p["configs_display"] else ""
        amen = ", ".join(p["amenities_display"][:3])
        area = p["location_label"] or p["location_value"].title()
        price = f"{p['price_cr']} Cr" if p["price_cr"] else "Price on request"
        line = f"{p['name']} - {area}"
        if cfgs:
            line += f"\n{cfgs}, starting {price}"
        else:
            line += f"\nStarting {price}"
        line += f"\n{poss}"
        if amen:
            line += f". Highlights: {amen}"
        lines.append(line)

    recommendations = "\n\n".join(lines) if lines else \
        "No exact matches yet, but our advisor will shortlist options for you."

    prices = [p["price_cr"] for p in top if p["price_cr"]] or \
             [p["price_cr"] for p in PROPERTIES if p["price_cr"]]

    return {
        "recommendations": recommendations,
        "min_price": f"{min(prices):.2f} Cr" if prices else "",
        "max_price": f"{max(prices):.2f} Cr" if prices else "",
        "count": len(top),
    }


@app.get("/health")
def health():
    return {"status": "ok", "properties_loaded": len(PROPERTIES)}