"""
Shared core for Indihomes chatbot backend.

DATA SOURCE (changed): inventory now comes from the LIVE Indihomes API
(property_api.py) instead of the frozen properties.json. properties.json is kept
as an OFFLINE FALLBACK so the bot never dies if the API is briefly unreachable.


The normalization maps the live API record into exactly the internal shape
search() already expects, so the search logic and the WATI flow stay identical.
Only the loader changed. Live-data quirks handled here (per the API brief):
  * price: always from startingPrice (flatInventory price is often 0)
  * carpetSize: values may be numbers OR strings -> cast
  * media_urls: may be ["url"] OR [{"url","tag"}] -> normalized to ["url"]
  * displayName: real building name ("38 Avenue") used instead of "A residence in..."
"""

import json
import os
import re
import time
from datetime import date
from typing import List, Dict

import property_api

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(HERE, "properties.json")


# --------------------------------------------------------------------------
# small helpers 
# --------------------------------------------------------------------------
def lakh_to_cr(value) -> float:
    try:
        return round(float(value) / 100.0, 2)
    except (TypeError, ValueError):
        return 0.0


def months_until(possession: str) -> int:
    if not possession:
        return 0
    m = re.match(r"(\d{4})-(\d{1,2})", str(possession).strip())
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
    """Fallback headline ONLY when the API has no displayName."""
    nearby = (nearby or "").strip()
    area = (area_label or "").strip()
    if nearby and area:
        return f"A residence in {nearby}, {area}"
    if nearby:
        return f"A residence in {nearby}"
    if area:
        return f"A residence in {area}"
    return "An Indihomes residence"


# --------------------------------------------------------------------------
# live-data quirk handlers
# --------------------------------------------------------------------------
def _amenity_value(a) -> str:
    if isinstance(a, dict):
        return (a.get("value") or a.get("label") or "").strip()
    if isinstance(a, str):
        return a.strip()
    return ""


def _normalize_media(media) -> List[str]:
    """media_urls / youtube_urls come as ['url', ...] OR [{'url','tag'}, ...]."""
    out = []
    for m in (media or []):
        if isinstance(m, str):
            u = m.strip()
        elif isinstance(m, dict):
            u = (m.get("url") or "").strip()
        else:
            u = ""
        if u:
            out.append(u)
    return out


def _cast_num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _normalize_carpet(c) -> Dict:
    """carpetSize.min/max may be numbers or numeric strings."""
    c = c or {}
    return {"min": _cast_num(c.get("min")), "max": _cast_num(c.get("max")),
            "unit": c.get("unit") or "Sq. Ft."}


def _normalize(r: Dict) -> Dict:
    """Live API record -> internal shape used by search() and detail formatting."""
    loc = r.get("location") or {}
    price = r.get("startingPrice") or {}
    label = (loc.get("label") or "").strip()
    display = (r.get("displayName") or "").strip()
    return {
        "id": r.get("id", ""),
        "code": r.get("projectName", ""),
        "display_name": display,
        "name": display or friendly_name(r.get("nearbyLocality", ""), label),
        "location_label": label,
        "location_value": (loc.get("value") or "").strip().lower(),
        "nearby": r.get("nearbyLocality", ""),
        "landmarks": r.get("landmarks") or r.get("nearbyLandmarks") or [],
        "price_cr": lakh_to_cr(price.get("value")),          # ALWAYS startingPrice
        "configs": [str(c).lower().replace(" ", "") for c in (r.get("flatConfiguration") or [])],
        "configs_display": r.get("flatConfiguration") or [],
        "possession_months": months_until(r.get("possessionStartDate", "")),
        "possession_raw": r.get("possessionStartDate", ""),
        "amenities": [_amenity_value(a).lower() for a in (r.get("amenities") or []) if _amenity_value(a)],
        "amenities_display": [_amenity_value(a) for a in (r.get("amenities") or []) if _amenity_value(a)],
        "brochure_url": (r.get("brochure_url") or "").strip(),
        "media_urls": _normalize_media(r.get("media_urls")),
        "floor_urls": _normalize_media(r.get("floor_urls")),
        "description": (r.get("description") or "").strip(),  # keep newlines; cleaned at format time
        "project_status": (r.get("projectStatus") or "").strip(),
        "carpet": _normalize_carpet(r.get("carpetSize")),
    }


# --------------------------------------------------------------------------
# inventory load 
# --------------------------------------------------------------------------
PROPERTIES: List[Dict] = []
KNOWN_LOCALITIES: List[str] = []
KNOWN_LOCALITIES_LOWER: Dict[str, str] = {}
_state = {"ts": 0.0, "source": "none", "count": 0}


def _rebuild(raw_list: List[Dict]):
    PROPERTIES[:] = [_normalize(r) for r in raw_list if r]
    labels = sorted({p["location_label"] for p in PROPERTIES if p["location_label"]})
    KNOWN_LOCALITIES[:] = labels
    KNOWN_LOCALITIES_LOWER.clear()
    KNOWN_LOCALITIES_LOWER.update({loc.lower(): loc for loc in labels})
    _state["count"] = len(PROPERTIES)


def _load_offline():
    with open(DATA_PATH, "r", encoding="utf-8") as f:
        _rebuild(json.load(f))
    _state["source"] = "properties.json (offline fallback)"
    _state["ts"] = time.time()


def load(force: bool = False):
    """Refresh from the live API. Falls back to the offline file only if we have
    no data at all yet. Never raises."""
    try:
        raw = property_api.fetch_all(force=force)
    except Exception as e:
        print(f"[property_core] live fetch error: {e}")
        raw = []
    if raw:
        _rebuild(raw)
        _state["source"] = "live api"
        _state["ts"] = time.time()
        return
    if not PROPERTIES:
        try:
            _load_offline()
        except Exception as e:
            print(f"[property_core] offline fallback failed: {e}")


def _ensure_fresh():
    """Cheap TTL check before a search; swallow errors (keep last good)."""
    ttl = property_api._cache_ttl()
    if (time.time() - _state["ts"]) > ttl:
        load(force=True)


def data_source() -> Dict:
    return {"source": _state["source"], "count": _state["count"]}


# initial population at import
try:
    load()
except Exception as _e:  # pragma: no cover
    print(f"[property_core] initial load failed: {_e}")
if not PROPERTIES:
    try:
        _load_offline()
    except Exception as _e:  # pragma: no cover
        print(f"[property_core] could not load any inventory: {_e}")


# --------------------------------------------------------------------------
# formatting + search 
# --------------------------------------------------------------------------
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


def micro_location(p) -> str:
    bits = []
    for lm in (p.get("landmarks") or [])[:3]:
        lm = (lm or "").strip() if isinstance(lm, str) else ""
        if lm:
            bits.append(lm)
    if bits:
        return "Near " + ", ".join(bits)
    nearby = (p.get("nearby") or "").strip()
    return f"Near {nearby}" if nearby else ""


def detail_line(p) -> str:
    cfgs = " / ".join(p["configs_display"]) if p["configs_display"] else ""
    price = f"{p['price_cr']} Cr" if p["price_cr"] else "Price on request"
    amen = ", ".join(p["amenities_display"][:3])
    parts = []
    micro = micro_location(p)
    if micro:
        parts.append(micro)
    parts.append(f"{cfgs}, starting {price}" if cfgs else f"Starting {price}")
    parts.append(possession_phrase(p))
    if amen:
        parts.append(f"Highlights: {amen}")
    return "\n".join(parts)


def property_url(p) -> str:
    """Link to the property's page on the Indihomes website, built from its
    project code (projectName). Empty if we don't have a code - never send a
    broken link."""
    code = (p.get("code") or "").strip()
    return f"https://indihomes.co.in/properties/{code}" if code else ""



_DESC_SECTION_MARKERS = ("Project Highlights", "Carpet Area", "Configuration:",
                          "Structure:", "Key Features", "Amenities")


def _intro_raw_segment(raw: str) -> str:
    """The opening prose block of the description, before it drops into
    markdown headings / --- rules / structured sections."""
    cut = len(raw)
    m = re.search(r"#{1,6}\s", raw)          # any heading, wherever it falls
    if m:
        cut = min(cut, m.start())
    m = re.search(r"-{3,}", raw)             # horizontal rule
    if m:
        cut = min(cut, m.start())
    for label in _DESC_SECTION_MARKERS:
        i = raw.find(label)
        if i != -1:
            cut = min(cut, i)
    return raw[:cut]


def _strip_markdown(s: str) -> str:
    s = s.replace("*", "")                            # bold/italic markers, matched or not
    s = re.sub(r"(?m)^[ \t]*[•\-][ \t]+", "", s)       # bullets at a line start
    s = s.replace("•", " ")                            # stray bullets mid-text
    return re.sub(r"\s+", " ", s).strip()              # collapse whitespace/newlines


def clean_description(raw: str, limit: int = 500) -> str:
    """Clean intro paragraph for the detail message: strip all markdown, keep
    only the opening prose (before any structured section), then cap it at
    ~3-4 sentences / `limit` chars, cut at a sentence boundary - never
    mid-sentence, never mid-word, never the whole field."""
    if not raw:
        return ""
    text = _strip_markdown(_intro_raw_segment(raw))
    if not text:
        return ""
    ends = [m.end() for m in re.finditer(r"[.!?](?:\s|$)", text)]
    cut = 0
    for i, e in enumerate(ends):
        if i >= 4 or e > limit:
            break
        cut = e
    if not cut:
        cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = min(len(text), limit)
    return text[:cut].strip()


def format_detail(p) -> str:
    """Rich single-property block built from live API fields. The raw Cosmos
    `description` is markdown WhatsApp can't render, so only a short cleaned
    intro paragraph is kept (see clean_description); the rest of the block is
    structured fields. Shown when the user picks a property number in Phase 3.
    Ends with the Indihomes website link instead of a brochure/media image.

    Asterisks are handled with ONE final guardrail instead of scrubbing every
    field individually: the message is assembled with the displayName in
    plain text (no bold), then every '*' in the finished string is stripped -
    wiping out any stray asterisk any Cosmos field might contain, wherever it
    is - and only THEN is the displayName line re-wrapped in the two
    intentional bold markers. That's the only place '*' can end up in the
    output, so a rogue asterisk downstream (say, in the description or an
    amenity) can never again leak onto the URL line."""
    name = p.get('display_name') or p['name']
    lines = [f"\U0001F3D9️ {name}"]
    loc = ", ".join(x for x in [(p.get("nearby") or "").strip(), p.get("location_label", "")] if x)
    if loc:
        lines.append(f"\U0001F4CD {loc}")
    lines.append("")

    intro = clean_description(p.get("description") or "")
    if intro:
        lines.append(intro)
        lines.append("")

    cfgs = " / ".join(p["configs_display"]) if p["configs_display"] else ""
    if cfgs:
        lines.append(f"\U0001F3E0 Configurations: {cfgs}")

    carpet = p.get("carpet") or {}
    cmin, cmax = carpet.get("min"), carpet.get("max")   # already cast to float/None - handles the string-or-number quirk
    if cmin and cmax:
        lines.append(f"\U0001F4D0 Carpet: {int(cmin)}–{int(cmax)} sq.ft")
    elif cmin or cmax:
        lines.append(f"\U0001F4D0 Carpet: {int(cmin or cmax)} sq.ft")

    price = f"{p['price_cr']} Cr" if p["price_cr"] else "Price on request"
    lines.append(f"\U0001F4B0 Starting {price}")

    if p.get("possession_raw"):
        lines.append(f"\U0001F5D3️ Possession by {p['possession_raw']}")
    else:
        lines.append(f"\U0001F5D3️ {possession_phrase(p)}")

    if p.get("project_status"):
        lines.append(f"\U0001F3D7️ {p['project_status']}")

    amen = ", ".join(p["amenities_display"][:5])
    if amen:
        lines.append(f"✨ Highlights: {amen}")

    code = (p.get("code") or "").strip()
    url = property_url(p)
    if url:
        lines.append("")
        lines.append(url)

    message = "\n".join(lines)

    # THE guardrail: zero every asterisk, then re-apply exactly two, on the
    # displayName line only (a targeted replace on that one line, not a
    # global one - the name could otherwise recur inside the description).
    message = message.replace("*", "")
    body_lines = message.split("\n")
    clean_name = name.replace("*", "")
    body_lines[0] = body_lines[0].replace(clean_name, f"*{clean_name}*", 1)
    result = "\n".join(body_lines)

    # Self-check, not a crash: a WATI reply must never 500 out to the user, so
    # if the guardrail above somehow still didn't leave exactly 2 asterisks
    # (e.g. clean_name wasn't found verbatim on the first line), fix it in
    # place and log which property triggered it, instead of raising.
    if result.count("*") != 2 or result.rstrip().endswith("*"):
        print(f"[property_core] format_detail: asterisk guardrail had to force-correct "
              f"code={code!r} (got {result.count('*')} asterisks)")
        result = result.replace("*", "")
        body_lines = result.split("\n")
        body_lines[0] = f"\U0001F3D9️ *{clean_name}*"
        result = "\n".join(body_lines)

    if url and not result.rstrip().endswith(code or url):
        print(f"[property_core] format_detail: message didn't end with the link, "
              f"appending it. code={code!r}")
        result = result.rstrip() + "\n\n" + url

    return result


def search(location="", configuration="", budget="", amenities="", possession="", limit=3, **_) -> Dict:
    """The one property search. Now returns up to `limit` results (default 3;
    pass 5 for the new pick-a-property flow). Filtering stays local Python over
    the live inventory; behaviour is otherwise identical to before."""
    _ensure_fresh()

    loc = clean(location).lower()
    cfg = norm_config(clean(configuration))
    ceiling = budget_ceiling(clean(budget))
    wanted_amenities = [a.strip().lower() for a in clean(amenities).replace(",", " ").split() if a.strip()]
    ready_only = "ready" in clean(possession).lower() and "only" in clean(possession).lower()

    loc_terms = [t.strip() for t in re.split(r"[|,]", loc) if t.strip()]
    DIRECTIONS = ("east", "west", "north", "south")

    def loc_ok(p):
        if not loc_terms:
            return True
        lv = p["location_value"]
        for t in loc_terms:
            if any(t.endswith(" " + dirn) for dirn in DIRECTIONS):
                if t == lv or lv == t:
                    return True
                continue
            area = t.split()[0] if t.split() else t
            if t == lv or t in lv or lv in t or area in lv:
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

    def amenity_score(p):
        return sum(1 for a in wanted_amenities if any(a in am for am in p["amenities"]))

    results = [p for p in PROPERTIES if matches(p)]
    results.sort(key=amenity_score, reverse=True)
    top = results[:limit]

    note = ""
    if not top and loc_terms:
        in_area = [p for p in PROPERTIES if loc_ok(p)]
        if in_area:
            in_area.sort(key=amenity_score, reverse=True)
            top = in_area[:limit]
            note = ("I couldn't find an exact match for everything you asked in that area, "
                    "so here's what is currently available there:")
        else:
            return {
                "recommendations": ("We don't have anything listed in that area right now. "
                                    "One of our advisors will look into options for you and get back shortly."),
                "min_price": "", "max_price": "", "count": 0,
                "name1": "", "detail1": "", "image1": "",
                "name2": "", "detail2": "", "image2": "",
                "name3": "", "detail3": "", "image3": "",
                "shortlist": [],
            }
    elif not top:
        top = PROPERTIES[:limit]

    blocks = [f"{i + 1}. {p['name']}\n{detail_line(p)}" for i, p in enumerate(top)]
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
        # machine-readable shortlist for the pick-a-property flow (Phase 3).
        # Includes the rich detail + image so /property-detail needs no 2nd API call.
        "shortlist": [{"index": i + 1, "id": p["id"], "code": p["code"],
                       "name": p["name"], "label": p["location_label"],
                       "detail": format_detail(p),
                       "image": (p["media_urls"][0] if p["media_urls"] else p["brochure_url"])}
                      for i, p in enumerate(top)],
    }
    for i in range(max(3, limit)):
        idx = i + 1
        if i < len(top):
            p = top[i]
            out[f"name{idx}"] = p["name"]
            out[f"detail{idx}"] = detail_line(p)
            out[f"image{idx}"] = p["brochure_url"] or (p["media_urls"][0] if p["media_urls"] else "")
            out[f"code{idx}"] = p["code"]          # projectName, for CRM projectCode
        else:
            out[f"name{idx}"] = out[f"detail{idx}"] = out[f"image{idx}"] = out[f"code{idx}"] = ""
    return out