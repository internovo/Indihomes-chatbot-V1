"""
Outbound WATI API client for proactive re-engagement messages.

Sends an interactive-buttons WhatsApp message to a phone number that has gone
quiet after receiving property recommendations. This is the ONLY module that
calls WATI outbound; all inbound traffic comes via WATI webhooks to app.py.

Design mirrors email_service.py exactly:
  - is_configured()         — lets /health report whether this feature is live
  - _post_json()            — shared urllib POST helper, same as email_service
  - send_followup_buttons() — primary send path (interactive buttons)
  - send_session_message()  — plain text fallback if buttons fail

Why urllib (no requests/httpx)?
  The project has zero HTTP-client dependencies today (email_service.py proved
  this is fine). Adding urllib keeps the install footprint minimal and matches
  the existing code exactly — someone reading both files sees the same idioms.

Required env vars (add to Railway variables / .env):
  WATI_API_ENDPOINT   e.g.  live-mt-server.wati.io/12345
                            (no https://, no trailing slash — we build the URL)
  WATI_API_KEY        e.g.  Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
                            (copy the full "Bearer ..." string from
                             WATI Dashboard > API Docs > Authorization header)

The 24-hour session window:
  WATI allows interactive buttons only inside an active 24-hour session window
  (meaning the customer sent something in the last 24 hours). We only fire this
  follow-up 2 hours after our own last message, so the customer's last message
  was at most a few minutes before that — we are always well inside the window.
  If the window somehow expired, WATI returns a 4xx and send_followup_buttons()
  returns False; no crash, no retry (the scheduler will not re-queue it).
"""

import json
import os
import urllib.error
import urllib.request
from typing import Optional


# ---- configuration detection -----------------------------------------------

def _endpoint() -> str:
    """Base URL for this tenant's WATI instance, without trailing slash."""
    return os.environ.get("WATI_API_ENDPOINT", "").strip().rstrip("/")


def _api_key() -> str:
    """The full Authorization header value, e.g. 'Bearer eyJ...'."""
    return os.environ.get("WATI_API_KEY", "").strip()


def is_configured() -> bool:
    """True if both required env vars are set. Used by /health and by the
    scheduler to skip the sweep entirely when WATI isn't wired up yet."""
    return bool(_endpoint() and _api_key())


# ---- shared HTTP helper -----------------------------------------------------

def _post_json(url: str, payload: dict) -> bool:
    """POST JSON to `url` with the WATI authorization header.

    Returns True on any 2xx response, False on any error — same contract as
    email_service._post_json(). Never raises; the scheduler tick must not crash.
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": _api_key(),
            "Content-Type": "application/json",
            # Without this, urllib sends its default "Python-urllib/3.x"
            # User-Agent, which Cloudflare's WAF (sitting in front of WATI)
            # blocks outright with error 1010 ("banned based on your
            # browser's signature") before the request ever reaches WATI's
            # application layer. A normal-looking UA avoids that filter.
            "User-Agent": "Mozilla/5.0 (compatible; IndihomesBot/1.0)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8")[:300]
        except Exception:
            detail = ""
        print(f"[wati_client] WATI API HTTP {e.code}: {detail}")
        return False
    except Exception as e:
        print(f"[wati_client] WATI API request failed: {e}")
        return False


# ---- public API -------------------------------------------------------------

def send_followup_buttons(phone: str, name: str = "") -> bool:
    """Send the re-engagement interactive-buttons message to `phone`.

    Uses WATI's sendInteractiveButtonsMessage endpoint. If WATI rejects it
    (e.g. session expired, malformed payload) we fall back to a plain session
    message rather than giving up entirely.

    Buttons:
      Button 1: "Talk to an Advisor"  — user taps → WATI flow sends to /advisor-request
      Button 2: "Still Exploring"     — WATI replies with static text, no backend call

    Returns True if either the buttons or the fallback text was sent successfully.
    Returns False if both fail — the scheduler will NOT mark the row as sent in
    this case, so a future sweep can retry (up to the next time the row becomes
    ineligible by some other path).
    """
    if not is_configured():
        print("[wati_client] WATI not configured — skipping send_followup_buttons")
        return False

    greeting = f"Hi {name}," if name and name.lower() not in ("", "none") else "Hi,"

    url = f"https://{_endpoint()}/api/v1/sendInteractiveButtonsMessage?whatsappNumber={phone}"
    payload = {
        "body": (
            f"{greeting} just checking in! 🏡\n\n"
            "We shared some property recommendations earlier. "
            "Would you like to explore further?"
        ),
        "buttons": [
            {"text": "Talk to an Advisor"},
            {"text": "Still Exploring"},
        ],
    }

    ok = _post_json(url, payload)
    if ok:
        print(f"[wati_client] follow-up buttons sent to {phone}")
        return True

    # Button send failed — try a plain session message as fallback.
    print(f"[wati_client] buttons failed for {phone}, attempting session message fallback")
    return send_session_message(
        phone,
        (
            f"{greeting} just checking in! 🏡\n\n"
            "We shared some property recommendations earlier. "
            "Would you like to talk to an advisor, or are you still exploring? "
            "Just reply and we'll help you from here."
        ),
    )


def send_session_message(phone: str, text: str) -> bool:
    """Send a plain-text session message to `phone`.

    Falls back to this when interactive buttons are not available (e.g. the
    button endpoint returns an error). Can also be used directly from tests
    to verify WATI connectivity without triggering the full button flow.

    Returns True on success, False on any error.
    """
    if not is_configured():
        print("[wati_client] WATI not configured — skipping send_session_message")
        return False

    url = f"https://{_endpoint()}/api/v1/sendSessionMessage/{phone}"
    payload = {"messageText": text}

    ok = _post_json(url, payload)
    if ok:
        print(f"[wati_client] session message sent to {phone}")
    else:
        print(f"[wati_client] session message also failed for {phone}")
    return ok
