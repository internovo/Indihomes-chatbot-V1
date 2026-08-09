"""
Lead routing client for the Indihomes WhatsApp assistant (Phase 3 hook).

Sends the finished lead to the NEW indihomes-lead-routing-service, which
resolves each recommended project's salesperson in Cosmos and notifies
them on WhatsApp via WATI. Mirrors crm_service.py's shape exactly
(same is_dry_run() naming convention, same urllib usage, same never-
raises contract) since this fires from the same call site in app.py's
/save-lead handler, right alongside the existing createLead push.

==========================  S A F E T Y  ==========================
LEAD_ROUTING_DRY_RUN defaults to "true". In dry-run we LOG the exact
payload we WOULD send and return without calling the routing service -
i.e. ZERO calls to the routing service, ZERO WhatsApp messages to any
salesperson. Flip LEAD_ROUTING_DRY_RUN=false only AFTER the logged
payloads have been reviewed AND indihomes-lead-routing-service's own
WATI_DRY_RUN has separately been confirmed safe to disable.
===================================================================

route_lead() never raises - a routing-service failure must never break
the conversation or block the existing CRM push in crm_service.py.
This call is best-effort, exactly like crm_service.push_lead().
"""

import json
import os
import urllib.request
import urllib.error
from typing import Dict, Optional


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


def build_payload(lead: Dict) -> Dict:
    """Maps our collected fields to the routing service's canonical
    contract (Indihomes_WATI_Salesperson_Routing_Implementation_Plan.docx
    section 5). `project_code` (singular, what save_lead already
    collects as @code1 from /search) is sent as a one-item project_codes
    array - the routing service also accepts the singular field
    directly, but sending the canonical array form here keeps this
    caller forward-compatible if a future flow ever recommends more
    than one project."""
    project_code = (lead.get("project_code") or "").strip()
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
        "project_codes": [project_code] if project_code else [],
        "recommendations": (lead.get("recommendations") or "").strip(),
        "outcome": (lead.get("outcome") or "details_shared").strip(),
        "source_lead_id": (lead.get("source_lead_id") or None),
    }


def route_lead(lead: Dict) -> Dict:
    """POSTs to indihomes-lead-routing-service. Returns a small status
    dict; never raises. Skips entirely (no call, no log) if
    LEAD_ROUTING_URL/LEAD_ROUTING_SHARED_SECRET aren't set - lets this
    hook exist in code before the routing service is actually deployed
    without doing anything until someone sets the env vars.

    No project code, no call: a lead that never got a recommendation
    (e.g. "not_interested" before /search ever ran) has nothing for the
    routing service to resolve - calling it anyway would just be a
    guaranteed 400."""
    payload = build_payload(lead)

    if not payload["phone"]:
        print("[lead_routing_client] no phone on lead - skipping")
        return {"ok": False, "dry_run": is_dry_run(), "reason": "no phone"}

    if not payload["project_codes"]:
        print("[lead_routing_client] no project_code on lead - skipping (nothing to route)")
        return {"ok": False, "dry_run": is_dry_run(), "reason": "no project_code"}

    if not is_configured():
        print("[lead_routing_client] LEAD_ROUTING_URL/LEAD_ROUTING_SHARED_SECRET not set - skipping")
        return {"ok": False, "dry_run": is_dry_run(), "reason": "not_configured"}

    if is_dry_run():
        print("[lead_routing_client] DRY-RUN - would POST /api/v1/leads/save-and-route with:\n"
              + json.dumps(payload, indent=2, ensure_ascii=False))
        return {"ok": True, "dry_run": True, "payload": payload}

    idempotency_key = f"direct_website:{lead.get('source_lead_id') or payload['phone']}"
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
            print(f"[lead_routing_client] save-and-route -> HTTP {resp.status}")
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
            print(f"[lead_routing_client] save-and-route partial success (207): {detail}")
            return {"ok": True, "dry_run": False, "status": 207, "response": detail}
        print(f"[lead_routing_client] save-and-route HTTP {e.code}: {detail}")
        return {"ok": False, "dry_run": False, "status": e.code, "response": detail}
    except Exception as e:
        print(f"[lead_routing_client] save-and-route failed: {e}")
        return {"ok": False, "dry_run": False, "error": str(e)}
