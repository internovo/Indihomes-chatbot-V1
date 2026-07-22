"""
Shared core for Indihomes chatbot backend.
Loads inventory once and exposes:
  - PROPERTIES        : normalized records
  - KNOWN_LOCALITIES  : set of real locality labels (for LLM validation)
  - search()          : the property search used at the end of the flow
Both webhook_search.py and llm_location.py import from here, so search
logic lives in exactly one place.
"""

import json
import os
import re
from datetime import date
from typing import List, Dict

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(HERE, "properties.json")

with open(DATA_PATH, "r", encoding="utf-8") as f:
    RAW = json.load(f)


def lakh_to_cr(value) -> float:
    try:
        return round(float(value) / 100.0, 2)
    except (TypeError, ValueError):
        return 0.0


def months_until(possession: str) -> int:
    if not possession:
        return 0
    m = re.match(r"(\d{4})-(\d{1,2})", possession.strip())
    if not m:
        return 0
    y, mo = int(m.group(1)), int(m.group(2))
    today = date.today()
    return max((y - today.year) * 12 + (mo - today.month), 0)


def norm_config(text: str) -> str:
    t = (text or "").lower().replace(" ", "")
    m = re.search(r"(\d)\s*bhk", t)
    return f"{m.group(1)}bhk" if m else t


def friendly_name(nearby: str, area_label: str) -> str:
    nearby = (nearby or "").strip()
    area = (area_label or "").strip()
    if nearby and area:
        return f"A residence near {nearby}, {area}"
    if nearby:
        return f"A residence near {nearby}"
    if area:
        return f"A residence in {area}"
    return "An Indihomes residence"


PROPERTIES: List[Dict] = []
for r in RAW:
    loc = r.get("location") or {}
    price = r.get("startingPrice") or {}
    PROPERTIES.append({
        "code": r.get("projectName", ""),
        "name": friendly_name(r.get("nearbyLocality", ""), loc.get("label", "")),
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
        "brochure_url": r.get("brochure_url", ""),
        "media_urls": r.get("media_urls") or [],
    })

# Real localities that exist in inventory. Used to validate LLM output so we
# never offer a locality we have no properties in.
KNOWN_LOCALITIES = sorted({p["location_label"] for p in PROPERTIES if p["location_label"]})
KNOWN_LOCALITIES_LOWER = {loc.lower(): loc for loc in KNOWN_LOCALITIES}


def clean(val: str) -> str:
    v = (val or "").strip()
    if v.startswith("{{") and v.endswith("}}"):
        return ""
    return v


def budget_ceiling(budget_text: str) -> float:
    t = (budget_text or "").lower().replace(" ", "")
    if "under1" in t or "below1" in t:
        return 1.0
    if "1cr-2cr" in t or ("1cr" in t and "2cr" in t):
        return 2.0
    if "above2" in t or "2cr+" in t:
        return 99.0
    return 99.0


def possession_phrase(p) -> str:
    if p["possession_months"] <= 0:
        return "Ready to move in"
    if p["possession_months"] <= 2:
        return "Possession very soon"
    return f"Possession by {p['possession_raw']}"


def detail_line(p) -> str:
    cfgs = " / ".join(p["configs_display"]) if p["configs_display"] else ""
    price = f"{p['price_cr']} Cr" if p["price_cr"] else "Price on request"
    amen = ", ".join(p["amenities_display"][:3])
    parts = [f"{cfgs}, starting {price}" if cfgs else f"Starting {price}",
             possession_phrase(p)]
    if amen:
        parts.append(f"Highlights: {amen}")
    return "\n".join(parts)


def search(location="", configuration="", budget="", amenities="", possession="", **_) -> Dict:
    """
    The one property search. Accepts already-normalized location text
    (a locality label, or several joined) plus the other filters.
    Returns the recommendations payload used by WATI.
    """
    loc = clean(location).lower()
    cfg = norm_config(clean(configuration))
    ceiling = budget_ceiling(clean(budget))
    wanted_amenities = [a.strip().lower() for a in clean(amenities).replace(",", " ").split() if a.strip()]
    ready_only = "ready" in clean(possession).lower() and "only" in clean(possession).lower()

    # location may be several localities separated by | or , (e.g. Malad East|Malad West)
    loc_terms = [t.strip() for t in re.split(r"[|,]", loc) if t.strip()]

    def loc_ok(p):
        if not loc_terms:
            return True
        for t in loc_terms:
            area = t.split()[0] if t.split() else t
            if t in p["location_value"] or p["location_value"] in t or area in p["location_value"]:
                return True
        return False

    def matches(p):
        if not loc_ok(p):
            return False
        if cfg and not any(cfg == c or cfg in c for c in p["configs"]):
            return False
        if p["price_cr"] > ceiling:
            return False
        if ready_only and p["possession_months"] > 2:
            return False
        return True

    results = [p for p in PROPERTIES if matches(p)]
    results.sort(key=lambda p: sum(1 for a in wanted_amenities if any(a in am for am in p["amenities"])), reverse=True)
    top = results[:3]

    # ---- Option A fallback: NEVER leave the requested location. ----
    # If nothing matches, relax the OTHER filters (config / possession / budget)
    # but keep the location fixed, and say so honestly.
    note = ""
    if not top and loc_terms:
        in_area = [p for p in PROPERTIES if loc_ok(p)]
        if in_area:
            in_area.sort(key=lambda p: sum(1 for a in wanted_amenities if any(a in am for am in p["amenities"])), reverse=True)
            top = in_area[:3]
            note = ("I couldn't find an exact match for everything you asked in that area, "
                    "so here's what is currently available there:")
        else:
            # Genuinely no inventory in that location -> be honest, hand to advisor.
            return {
                "recommendations": ("We don't have anything listed in that area right now. "
                                    "One of our advisors will look into options for you and get back shortly."),
                "min_price": "",
                "max_price": "",
                "count": 0,
                "name1": "", "detail1": "", "image1": "",
                "name2": "", "detail2": "", "image2": "",
                "name3": "", "detail3": "", "image3": "",
            }
    elif not top:
        top = PROPERTIES[:3]

    blocks = [f"{p['name']}\n{detail_line(p)}" for p in top]
    recommendations = "\n\n".join(blocks) if blocks else \
        "No exact matches yet, but our advisor will shortlist options for you."
    if note:
        recommendations = note + "\n\n" + recommendations

    prices = [p["price_cr"] for p in top if p["price_cr"]] or [p["price_cr"] for p in PROPERTIES if p["price_cr"]]

    out = {
        "recommendations": recommendations,
        "min_price": f"{min(prices):.2f} Cr" if prices else "",
        "max_price": f"{max(prices):.2f} Cr" if prices else "",
        "count": len(top),
    }
    for i in range(3):
        idx = i + 1
        if i < len(top):
            p = top[i]
            out[f"name{idx}"] = p["name"]
            out[f"detail{idx}"] = detail_line(p)
            out[f"image{idx}"] = p["brochure_url"] or (p["media_urls"][0] if p["media_urls"] else "")
        else:
            out[f"name{idx}"] = out[f"detail{idx}"] = out[f"image{idx}"] = ""
    return out