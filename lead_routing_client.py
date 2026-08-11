"""
Lead routing client for the Indihomes WhatsApp assistant (Phase 3 hook).

Sends lead info to the NEW indihomes-lead-routing-service, which resolves
each recommended project's salesperson in Cosmos and notifies them on
WhatsApp via WATI. Mirrors crm_service.py's shape (is_dry_run() naming
convention, urllib usage, never-raises contract).

TWO call sites, TWO different functions - deliberately not unified,
because they mean different things:

  notify_recommendations()  <- called from /search, right when a
                                shortlist is first shown to the customer.
                                Fires ONE independent notification PER
                                project code shown. Does NOT touch the
                                CRM and does NOT close the conversation -
                                this is "hey, a lead is looking at your
                                listing right now", not "this lead is
                                done, file it."

  route_lead()               <- called from /save-lead, at the actual
                                end of a conversation (books a visit /
                                asks for an advisor / explicitly not
                                interested). This is the terminal-outcome
                                path, alongside the CRM push.

Why not just call /save-lead right after /search? Two reasons this would
be actively wrong, not just redundant:
  1. /save-lead calls conversation_tracker.close_conversation(phone) -
     closing the conversation the moment search results are SHOWN, before
     the customer has said anything else, would kill the follow-up/
     re-engagement flow for every single search.
  2. /save-lead calls crm_service.push_lead() - creating a CRM lead
     record for every search (including someone testing three different
     areas in one sitting) would flood the CRM with low-intent noise
     that doesn't represent an actual lead handoff.
notify_recommendations() exists specifically to avoid needing either of
those side effects just to get the salesperson pinged early.

==========================  S A F E T Y  ==========================
LEAD_ROUTING_DRY_RUN defaults to "true". In dry-run we LOG the exact
payload we WOULD send and return without calling the routing service -
i.e. ZERO calls to the routing service, ZERO WhatsApp messages to any
salesperson. Flip LEAD_ROUTING_DRY_RUN=false only AFTER the logged
payloads have been reviewed AND indihomes-lead-routing-service's own
WATI_DRY_RUN has separately been confirmed safe to disable.
===================================================================

Neither function raises - a routing-service failure must never break
the conversation or block the existing CRM push in crm_service.py.
Both are best-effort, exactly like crm_service.push_lead().
"""

import json
import os
import urllib.request
import urllib.error
from typing import Dict, List, Optional


def _base_url() -> str:
    return os.environ.get("LEAD_ROUTING_URL", "").strip().rstrip("/")


def _shared_secret() -> str:
    return os.environ.get("LEAD_ROUTING_SHARED_SECRET", "").strip()


def _timeout() -> int:
    try:
        return int(os.environ.get("LEAD_ROUTING_TIMEOUT", "15") or 15)
    except (TypeError, ValueError):
        return 15


def is_configured() -> bool:
    """True if both the URL and shared secret are set. Used by /health
    and to skip the call entirely (not even a dry-run log) when the
    integration hasn't been wired up on this deployment yet."""
    return bool(_base_url() and _shared_secret())


def is_dry_run() -> bool:
    # default TRUE - same safety convention as crm_service.is_dry_run()
    return os.environ.get("LEAD_ROUTING_DRY_RUN", "true").strip().lower() != "false"


def _post(payload: Dict, idempotency_key: str) -> Dict:
    """Shared HTTP layer for both call sites below. Never raises - always
    returns a small status dict. Handles dry-run, missing config, and the
    "207 is a normal partial-success outcome, not an error" nuance in one
    place so both callers get identical behavior."""
    if not payload["phone"]:
        print("[lead_routing_client] no phone on lead - skipping")
        return {"ok": False, "dry_run": is_dry_run(), "reason": "no phone"}

    if not payload["project_codes"]:
        print("[lead_routing_client] no project_code(s) - skipping (nothing to route)")
        return {"ok": False, "dry_run": is_dry_run(), "reason": "no project_code"}

    if not is_configured():
        print("[lead_routing_client] LEAD_ROUTING_URL/LEAD_ROUTING_SHARED_SECRET not set - skipping")
        return {"ok": False, "dry_run": is_dry_run(), "reason": "not_configured"}

    if is_dry_run():
        print("[lead_routing_client] DRY-RUN - would POST /api/v1/leads/save-and-route "
              f"(idempotency_key={idempotency_key!r}) with:\n"
              + json.dumps(payload, indent=2, ensure_ascii=False))
        return {"ok": True, "dry_run": True, "payload": payload}

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        _base_url() + "/api/v1/leads/save-and-route",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Lead-Routing-Secret": _shared_secret(),
            "X-Idempotency-Key": idempotency_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_timeout()) as resp:
            body = resp.read().decode("utf-8")
            print(f"[lead_routing_client] save-and-route ({idempotency_key}) -> HTTP {resp.status}")
            return {"ok": 200 <= resp.status < 300, "dry_run": False,
                    "status": resp.status, "response": body[:500]}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:500]
        except Exception:
            pass
        # 207 (partial success - some projects not_found/needs_assignment/
        # failed) is a NORMAL outcome, not an error worth alarming logs
        # over - the routing service itself already logged per-project
        # detail. urllib treats any non-2xx as HTTPError, so 207 lands
        # here even though it's not really a failure.
        if e.code == 207:
            print(f"[lead_routing_client] save-and-route ({idempotency_key}) partial success (207): {detail}")
            return {"ok": True, "dry_run": False, "status": 207, "response": detail}
        print(f"[lead_routing_client] save-and-route ({idempotency_key}) HTTP {e.code}: {detail}")
        return {"ok": False, "dry_run": False, "status": e.code, "response": detail}
    except Exception as e:
        print(f"[lead_routing_client] save-and-route ({idempotency_key}) failed: {e}")
        return {"ok": False, "dry_run": False, "error": str(e)}


def _base_payload(lead: Dict, project_codes: List[str], outcome: str) -> Dict:
    return {
        "source": "direct_website",
        "phone": (lead.get("phone") or "").strip(),
        "name": (lead.get("name") or "").strip(),
        "location": (lead.get("location") or "").strip(),
        "budget": (lead.get("budget") or "").strip(),
        "configuration": (lead.get("configuration") or "").strip(),
        "possession_pref": (lead.get("possession_pref") or "").strip(),
        "purpose": (lead.get("purpose") or "").strip(),
        "amenities": (lead.get("amenities") or "").strip(),
        "project_codes": project_codes,
        "recommendations": (lead.get("recommendations") or "").strip(),
        "outcome": outcome,
        "source_lead_id": (lead.get("source_lead_id") or None),
    }


def route_lead(lead: Dict) -> Dict:
    """Called from /save-lead - the terminal-outcome path. See module
    docstring for why this is a SEPARATE function from
    notify_recommendations() rather than the same call reused."""
    project_code = (lead.get("project_code") or "").strip()
    payload = _base_payload(lead, [project_code] if project_code else [], (lead.get("outcome") or "details_shared").strip())
    idempotency_key = f"direct_website:{lead.get('source_lead_id') or payload['phone']}"
    return _post(payload, idempotency_key)


def notify_recommendations(lead: Dict) -> Dict:
    """Called from /search, right when a shortlist is first shown to the
    customer - see module docstring for the full "why a separate
    function" rationale.

    `lead["project_codes"]` should be every project code in the shortlist
    just shown (result["shortlist"]'s "code" values). Fires ONE
    independent POST per code, each with its own idempotency key scoped
    to (phone, project_code) - NOT the whole shortlist as one unit:

      - Showing the SAME property again later (a refined search, or a
        new session) does NOT re-notify that salesperson - the routing
        service's own idempotency already recorded that (phone, code)
        pair as sent.
      - A DIFFERENT phone being shown the SAME property DOES get its own
        independent notification - the key includes phone, not just code.
      - A NEW property the phone hasn't seen before DOES notify, even if
        other codes in the same shortlist were already notified earlier
        (in an earlier search this session, or a prior session).
    This is what keeps repeated/refined searches from spamming the same
    salesperson over and over, without needing any extra state on this
    side - the dedup lives entirely in the routing service's own
    idempotency store.

    Known trade-off, accepted deliberately: at /search time this flow
    usually doesn't have the customer's name yet (WATI typically collects
    it later). `lead.get("name")` is passed through if the caller has it;
    otherwise the WATI template shows "(no name)" for that field - see
    indihomes-lead-routing-service's wati_service.py default. This was
    weighed against firing later (at /property-detail, which already
    carries `name`) and firing at /search was chosen anyway, per product
    decision - see claude.md for the tradeoff writeup and how to switch
    trigger points if that decision changes later.

    Returns a summary dict; never raises. Fires each code independently -
    one failing does not stop the others.
    """
    phone = (lead.get("phone") or "").strip()
    codes = [c.strip() for c in (lead.get("project_codes") or []) if c and c.strip()]

    if not phone:
        print("[lead_routing_client] notify_recommendations: no phone - skipping")
        return {"ok": False, "dry_run": is_dry_run(), "reason": "no phone"}
    if not codes:
        print("[lead_routing_client] notify_recommendations: no project codes in shortlist - skipping")
        return {"ok": False, "dry_run": is_dry_run(), "reason": "no project_codes"}
    if not is_configured():
        print("[lead_routing_client] LEAD_ROUTING_URL/LEAD_ROUTING_SHARED_SECRET not set - skipping")
        return {"ok": False, "dry_run": is_dry_run(), "reason": "not_configured"}

    per_code_results = {}
    for code in codes:
        payload = _base_payload(lead, [code], "details_shared")
        idempotency_key = f"direct_website:search:{phone}:{code}"
        per_code_results[code] = _post(payload, idempotency_key)

    return {
        "ok": all(r.get("ok") for r in per_code_results.values()),
        "dry_run": is_dry_run(),
        "per_code": per_code_results,
    }
