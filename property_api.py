"""
Live Indihomes property API client.

Read-only. Talks to the public REST API (no auth required for reads):
  Base: https://api.indihomes.co.in/api/v1

Endpoints used:
  GET  fetchPaginatedFilteredProjectList   - list 
  GET  fetchProject?id={id}                - full single record
  GET  fetchProjectByName?projectName={code} - full single record by code

Design:
  - fetch_all() pages through the whole catalogue and returns raw project dicts.
  - A short-lived in-memory cache (TTL) avoids hammering the API on every search.
  - "Last good response" fallback: if a refresh fails, we keep serving the last
    successful result instead of breaking the conversation.
  - Everything is defensive: on any failure the callers get [] / None, never an
    exception. property_core layers a properties.json offline fallback on top.

No secrets. Configure the base + timings via env if needed:
  INDIHOMES_API_BASE   (default https://api.indihomes.co.in/api/v1)
  INDIHOMES_API_TIMEOUT (seconds, default 15)
  INDIHOMES_CACHE_TTL   (seconds, default 300)
"""

import json
import os
import time
import urllib.parse
import urllib.request
import urllib.error
from typing import Dict, List, Optional


def _base() -> str:
    return os.environ.get("INDIHOMES_API_BASE", "https://api.indihomes.co.in/api/v1").rstrip("/")


def _timeout() -> int:
    try:
        return int(os.environ.get("INDIHOMES_API_TIMEOUT", "15") or 15)
    except (TypeError, ValueError):
        return 15


def _cache_ttl() -> int:
    try:
        return int(os.environ.get("INDIHOMES_CACHE_TTL", "300") or 300)
    except (TypeError, ValueError):
        return 300


# module-level "last good" cache for the full list
_cache = {"items": None, "ts": 0.0}


def _get_json(path: str, params: Dict) -> Optional[dict]:
    """GET {base}{path}?{params} -> parsed JSON, or None on any failure."""
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    url = f"{_base()}{path}"
    if qs:
        url += "?" + qs
    req = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=_timeout()) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"[property_api] HTTP {e.code} for {path}")
        return None
    except Exception as e:
        print(f"[property_api] request failed for {path}: {e}")
        return None


def _fetch_all_uncached(page_size: int = 100, max_pages: int = 20) -> Optional[List[Dict]]:
    """Page through fetchPaginatedFilteredProjectList and collect every project.
    Returns None (not []) if the very first page fails, so the caller can tell
    'API unreachable' apart from 'genuinely zero projects'."""
    first = _get_json("/fetchPaginatedFilteredProjectList", {"limit": page_size, "page": 1})
    if not first or not first.get("success"):
        return None

    projects = list(first.get("projects") or [])
    total_pages = int(first.get("totalPages") or 1)
    # totalPages is relative to the page_size we asked for
    page = 2
    while page <= total_pages and page <= max_pages:
        nxt = _get_json("/fetchPaginatedFilteredProjectList", {"limit": page_size, "page": page})
        if not nxt or not nxt.get("success"):
            break  # keep what we have rather than failing outright
        projects.extend(nxt.get("projects") or [])
        page += 1
    return projects


def fetch_all(force: bool = False) -> List[Dict]:
    """All projects, cached for INDIHOMES_CACHE_TTL seconds. On refresh failure,
    returns the last good cached list (or [] if we never had one)."""
    now = time.time()
    if not force and _cache["items"] is not None and (now - _cache["ts"]) < _cache_ttl():
        return _cache["items"]

    fresh = _fetch_all_uncached()
    if fresh is not None:
        _cache["items"] = fresh
        _cache["ts"] = now
        return fresh
    # refresh failed -> serve last good if we have it
    return _cache["items"] if _cache["items"] is not None else []


def fetch_by_id(project_id: str) -> Optional[Dict]:
    if not project_id:
        return None
    data = _get_json("/fetchProject", {"id": project_id})
    if not data:
        return None
    # endpoint may wrap in {success, project} or return the record directly
    return data.get("project") or (data if data.get("id") else None)


def fetch_by_name(project_name: str) -> Optional[Dict]:
    if not project_name:
        return None
    data = _get_json("/fetchProjectByName", {"projectName": project_name})
    if not data:
        return None
    return data.get("project") or (data if data.get("id") else None)


def is_reachable() -> bool:
    """Cheap health probe for /health - one tiny page."""
    d = _get_json("/fetchPaginatedFilteredProjectList", {"limit": 1, "page": 1})
    return bool(d and d.get("success"))
