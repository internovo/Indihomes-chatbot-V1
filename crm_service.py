"""
CRM lead push for the Indihomes WhatsApp assistant.

Sends a completed lead to the EXISTING createLead API:
    POST {INDIHOMES_API_BASE}/createLead
That endpoint already writes the leads container, builds the CRM record,
de-dupes by phone, appends to the Google Sheet, and emails the team. We do NOT
write to Cosmos directly.

==========================  S A F E T Y  ==========================
CRM_DRY_RUN defaults to "true". In dry-run we LOG the exact payload we WOULD
send and return without calling the API - i.e. ZERO writes to production.
Flip CRM_DRY_RUN=false only AFTER the logged payloads have been reviewed.
===================================================================

createLead body schema (exactly as documented in the API brief):
  name, phone, email, flat_config, projectCode, targetPossessionDate,
  budget, preferred_location, notes, lead_source, user_type

push_lead() never raises - a CRM failure must never break the conversation.
"""

import json
import os
import urllib.request
import urllib.error
from typing import Dict


def _base() -> str:
    return os.environ.get("INDIHOMES_API_BASE", "https://api.indihomes.co.in/api/v1").rstrip("/")


def _timeout() -> int:
    try:
        return int(os.environ.get("INDIHOMES_API_TIMEOUT", "15") or 15)
    except (TypeError, ValueError):
        return 15


def is_dry_run() -> bool:
    # default TRUE - production writes are opt-in via CRM_DRY_RUN=false
    return os.environ.get("CRM_DRY_RUN", "true").strip().lower() != "false"


def build_payload(lead: Dict) -> Dict:
    """Map our collected fields to the EXACT createLead body. Extra keys the
    bot passes (like status text) are folded into notes by the caller, not sent
    as top-level fields."""
    return {
        "name": (lead.get("name") or "").strip(),
        "phone": (lead.get("phone") or "").strip(),
        "email": (lead.get("email") or "").strip(),
        "flat_config": (lead.get("configuration") or "").strip(),
        "projectCode": (lead.get("project_code") or "").strip(),
        "targetPossessionDate": (lead.get("target_possession") or "").strip(),
        "budget": (lead.get("budget") or "").strip(),
        "preferred_location": (lead.get("location") or "").strip(),
        "notes": (lead.get("notes") or "").strip(),
        "lead_source": (lead.get("lead_source") or "WhatsApp Bot").strip(),
        "user_type": (lead.get("user_type") or "").strip(),
    }


def push_lead(lead: Dict) -> Dict:
    """Create the lead via createLead. Returns a small status dict; never raises."""
    payload = build_payload(lead)

    if not payload["phone"]:
        print("[crm_service] no phone on lead - skipping")
        return {"ok": False, "dry_run": is_dry_run(), "reason": "no phone"}

    if is_dry_run():
        print("[crm_service] DRY-RUN - would POST /createLead with:\n"
              + json.dumps(payload, indent=2, ensure_ascii=False))
        return {"ok": True, "dry_run": True, "payload": payload}

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        _base() + "/createLead", data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_timeout()) as resp:
            body = resp.read().decode("utf-8")
            print(f"[crm_service] createLead -> HTTP {resp.status}")
            return {"ok": 200 <= resp.status < 300, "dry_run": False,
                    "status": resp.status, "response": body[:300]}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:300]
        except Exception:
            pass
        print(f"[crm_service] createLead HTTP {e.code}: {detail}")
        return {"ok": False, "dry_run": False, "status": e.code, "response": detail}
    except Exception as e:
        print(f"[crm_service] createLead failed: {e}")
        return {"ok": False, "dry_run": False, "error": str(e)}
