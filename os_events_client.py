"""
Lead-events client for the Indihomes WhatsApp assistant.

Pushes conversation checkpoints (requirements shared, options shared,
property details shared, advisor requested, tagging sent, no-reply,
follow-up sent) to indihomes-os's lead-events pipeline, so the Lead
Capture UI's "AI Activity" tick and "Lead Journey" vertical tracker have
real data to show. Mirrors lead_routing_client.py's shape exactly
(is_dry_run() naming convention, urllib usage, never-raises contract) -
see that file if anything here looks unfamiliar.

==========================  S A F E T Y  ==========================
OS_EVENTS_DRY_RUN defaults to "true". In dry-run we LOG the exact payload
we WOULD send and return without calling indihomes-os - i.e. ZERO calls
out, ZERO risk of this new integration affecting anything a real customer
sees. Flip OS_EVENTS_DRY_RUN=false only after the logged payloads have
been reviewed AND indihomes-os's own POST /api/lead-events endpoint has
been confirmed reachable (it requires indihomes-os's server.cjs to be
restored and the new router wired in - see that repo's
backend/LEAD_EVENTS_INTEGRATION.md; it is NOT live yet as of this
writing).
===================================================================

emit() never raises and never blocks the caller's real response to WATI -
every call site in app.py/followup_scheduler.py wraps this in the same
try/except-and-log pattern already used for every other best-effort side
effect in this project (crm push, lead routing, tracker writes).

PHONE FORMAT: sent exactly as this project already has it (typically
"91XXXXXXXXXX", no leading +, whatever WATI gave us) - normalization to
indihomes-os's bare-10-digit convention happens on THEIR side
(lead-events.cjs calls the same normalizePhone() the rest of that app
already trusts). This client does not attempt to normalize the phone
itself - guessing at a convention it doesn't own would risk silently
mangling a number, and it's indihomes-os's dedup key to own, not this
repo's.
"""

import json
import os
import urllib.request
import urllib.error
from typing import Dict, Optional


def _base_url() -> str:
    return os.environ.get("OS_EVENTS_URL", "").strip().rstrip("/")


def _shared_secret() -> str:
    return os.environ.get("OS_EVENTS_SHARED_SECRET", "").strip()


def _timeout() -> int:
    try:
        return int(os.environ.get("OS_EVENTS_TIMEOUT", "10") or 10)
    except (TypeError, ValueError):
        return 10


def is_configured() -> bool:
    """True if OS_EVENTS_URL is set. Shared secret is optional (the
    ingest endpoint doesn't require one today - see indihomes-os's
    lead-events.cjs) but sent as a header when present, for whenever
    that changes."""
    return bool(_base_url())


def is_dry_run() -> bool:
    # default TRUE - same safety convention as lead_routing_client.is_dry_run()
    return os.environ.get("OS_EVENTS_DRY_RUN", "true").strip().lower() != "false"


def emit(
    phone: str,
    checkpoint: str,
    payload: Optional[Dict] = None,
    source_ref: str = "",
    idempotency_key: str = "",
) -> Dict:
    """Fire one WhatsApp checkpoint event. channel is always "whatsapp" -
    this repo has no voice concept (that's Sarvam, posting directly to
    indihomes-os, not through this client).

    idempotency_key should be unique per real-world occurrence of this
    checkpoint (e.g. f"{phone}:{checkpoint}:{shortlist_hash}" for
    options_shared, so a retried /search webhook call doesn't double-log
    the same shortlist as two separate events) - if omitted,
    indihomes-os falls back to a phone+checkpoint+timestamp key, which is
    weaker (won't dedupe two genuinely-close-together identical events).

    Returns a small status dict; never raises. A failure here must never
    affect what the calling endpoint returns to WATI - every call site
    wraps this in its own try/except regardless, but emit() itself is
    also defensive so a bug here can't become a 500 mid-conversation.
    """
    if not phone:
        print("[os_events_client] no phone - skipping")
        return {"ok": False, "dry_run": is_dry_run(), "reason": "no phone"}

    if not is_configured():
        # Silent no-op, no log spam - this is the expected state until
        # OS_EVENTS_URL is set (indihomes-os's server.cjs isn't restored
        # yet as of this writing - see this function's module docstring).
        return {"ok": False, "dry_run": is_dry_run(), "reason": "not_configured"}

    body = {
        "phone": phone,
        "channel": "whatsapp",
        "checkpoint": checkpoint,
        "payload": payload or None,
        "source_ref": source_ref or None,
        "idempotency_key": idempotency_key or None,
    }

    if is_dry_run():
        print(f"[os_events_client] DRY-RUN - would POST /api/lead-events "
              f"(checkpoint={checkpoint!r}) with:\n"
              + json.dumps(body, indent=2, ensure_ascii=False))
        return {"ok": True, "dry_run": True, "payload": body}

    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    secret = _shared_secret()
    if secret:
        headers["X-OS-Events-Secret"] = secret

    req = urllib.request.Request(
        _base_url() + "/api/lead-events",
        data=data,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_timeout()) as resp:
            resp_body = resp.read().decode("utf-8")
            print(f"[os_events_client] {checkpoint} -> HTTP {resp.status}")
            return {"ok": 200 <= resp.status < 300, "dry_run": False,
                    "status": resp.status, "response": resp_body[:500]}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:500]
        except Exception:
            pass
        print(f"[os_events_client] {checkpoint} HTTP {e.code}: {detail}")
        return {"ok": False, "dry_run": False, "status": e.code, "response": detail}
    except Exception as e:
        print(f"[os_events_client] {checkpoint} failed: {e}")
        return {"ok": False, "dry_run": False, "error": str(e)}
