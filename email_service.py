"""
Advisor email notifications for appointment bookings.

Sends every advisor the lead's full requirements when a site visit is booked.

Transport, in priority order (first one configured wins):
  1. Resend     - HTTPS API,  set RESEND_API_KEY     (works on Railway/PaaS)
  2. SendGrid   - HTTPS API,  set SENDGRID_API_KEY   (works on Railway/PaaS)
  3. Brevo      - HTTPS API,  set BREVO_API_KEY      (works on Railway/PaaS;
                  single-sender verification by email link, no DNS needed)
  4. Gmail SMTP - set SMTP_USER + SMTP_APP_PASSWORD  (LOCAL DEV ONLY)

WHY THE HTTPS APIS: Railway - like most PaaS hosts - blocks outbound SMTP
ports (25/465/587) to curb spam, so smtplib cannot reach smtp.gmail.com there
and fails with "[Errno 101] Network is unreachable". The Resend / SendGrid
REST APIs go over port 443, which is allowed. Gmail SMTP stays here only as a
convenient fallback for running locally.

Setup (.env locally / Railway variables in production):
  # pick ONE provider for production
  RESEND_API_KEY=re_xxx
  # or
  SENDGRID_API_KEY=SG.xxx

  EMAIL_FROM=internovoventures@gmail.com    # a VERIFIED sender for that provider
  EMAIL_FROM_NAME=Indihomes Bookings
  ADVISOR_EMAILS=a@x.com,b@x.com,c@x.com    # recipients (shared with calendar cfg)
  NOTIFY_CC=                                # optional oversight inbox(es)

  # local-only Gmail SMTP fallback
  SMTP_HOST=smtp.gmail.com
  SMTP_PORT=587
  SMTP_USER=internovoventures@gmail.com
  SMTP_APP_PASSWORD=xxxxxxxxxxxxxxxx

Every function is defensive: on any misconfig or failure it logs and returns
False (never raises), so a confirmed booking is never turned into a 500.
"""

import json
import os
import smtplib
import ssl
import urllib.request
import urllib.error
from email.message import EmailMessage
from typing import Dict, List


# ---- provider detection -------------------------------------------------

def _resend_key() -> str:
    return os.environ.get("RESEND_API_KEY", "").strip()


def _sendgrid_key() -> str:
    return os.environ.get("SENDGRID_API_KEY", "").strip()


def _brevo_key() -> str:
    return os.environ.get("BREVO_API_KEY", "").strip()


def _smtp_ready() -> bool:
    return bool(os.environ.get("SMTP_USER") and os.environ.get("SMTP_APP_PASSWORD"))


def is_configured() -> bool:
    """True if any transport is set up, so /health can report 'connected'."""
    return bool(_resend_key() or _sendgrid_key() or _brevo_key() or _smtp_ready())


def _from_email() -> str:
    return (os.environ.get("EMAIL_FROM") or os.environ.get("SMTP_USER") or "").strip()


def _from_name() -> str:
    return (os.environ.get("EMAIL_FROM_NAME")
            or os.environ.get("SMTP_FROM_NAME")
            or "Indihomes Bookings")


def _cc_list() -> List[str]:
    raw = os.environ.get("NOTIFY_CC", "")
    return [e.strip() for e in raw.split(",") if e.strip()]


def _normalize(advisor_emails) -> List[str]:
    if isinstance(advisor_emails, str):
        advisor_emails = [advisor_emails]
    return [e.strip() for e in (advisor_emails or []) if e and e.strip()]


# ---- message content ----------------------------------------------------

def _subject(lead: Dict) -> str:
    name = (lead.get("name") or "").strip()
    slot = (lead.get("slot_label") or "").strip()
    s = "New site visit"
    if name:
        s += f" - {name}"
    if slot:
        s += f" ({slot})"
    return s


def _format_body(lead: Dict) -> str:
    """Plain-text body. Only include lines we actually have a value for."""
    def line(label, value):
        value = (value or "").strip()
        return f"{label}: {value}" if value else None

    rows = [
        line("Name", lead.get("name")),
        line("Phone", lead.get("phone")),
        line("Appointment", lead.get("slot_label")),
        line("Type", lead.get("appt_type")),
        line("Budget", lead.get("budget")),
        line("Configuration", lead.get("configuration")),
        line("Preferred area", lead.get("location")),
        line("Property of interest", lead.get("property_ref")),
    ]
    rows = [r for r in rows if r]

    body = "A new site visit was booked through the Indihomes WhatsApp assistant.\n\n"
    body += "\n".join(rows) if rows else "(No lead details were captured.)"
    body += ("\n\nThis visit is also on the shared Indihomes Site Visits calendar. "
             "Please reach out to the customer to confirm.")
    return body


# ---- transports ---------------------------------------------------------

def _post_json(url: str, headers: Dict, payload: Dict) -> bool:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8")[:300]
        except Exception:
            detail = ""
        print(f"[email_service] provider HTTP {e.code}: {detail}")
        return False
    except Exception as e:
        print(f"[email_service] provider request failed: {e}")
        return False


def _send_via_resend(advisors: List[str], cc: List[str], subject: str, body: str) -> bool:
    payload = {
        "from": f"{_from_name()} <{_from_email()}>",
        "to": advisors,
        "subject": subject,
        "text": body,
    }
    if cc:
        payload["cc"] = cc
    headers = {"Authorization": f"Bearer {_resend_key()}", "Content-Type": "application/json"}
    return _post_json("https://api.resend.com/emails", headers, payload)


def _send_via_brevo(advisors: List[str], cc: List[str], subject: str, body: str) -> bool:
    payload = {
        "sender": {"email": _from_email(), "name": _from_name()},
        "to": [{"email": a} for a in advisors],
        "subject": subject,
        "textContent": body,
    }
    if cc:
        payload["cc"] = [{"email": c} for c in cc]
    headers = {"api-key": _brevo_key(), "Content-Type": "application/json", "Accept": "application/json"}
    return _post_json("https://api.brevo.com/v3/smtp/email", headers, payload)


def _send_via_sendgrid(advisors: List[str], cc: List[str], subject: str, body: str) -> bool:
    personalization = {"to": [{"email": a} for a in advisors]}
    if cc:
        personalization["cc"] = [{"email": c} for c in cc]
    payload = {
        "personalizations": [personalization],
        "from": {"email": _from_email(), "name": _from_name()},
        "subject": subject,
        "content": [{"type": "text/plain", "value": body}],
    }
    headers = {"Authorization": f"Bearer {_sendgrid_key()}", "Content-Type": "application/json"}
    return _post_json("https://api.sendgrid.com/v3/mail/send", headers, payload)


def _send_via_smtp(advisors: List[str], cc: List[str], subject: str, body: str) -> bool:
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    try:
        port = int(os.environ.get("SMTP_PORT", "587") or 587)
    except (TypeError, ValueError):
        port = 587
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_APP_PASSWORD", "")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{_from_name()} <{user}>"
    msg["To"] = ", ".join(advisors)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg.set_content(body)
    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(host, port, timeout=20) as server:
            server.starttls(context=context)
            server.login(user, password)
            server.send_message(msg, to_addrs=advisors + cc)
        return True
    except Exception as e:
        print(f"[email_service] SMTP send failed: {e}")
        return False


# ---- public entrypoint --------------------------------------------------

def send_booking_notification(advisor_emails, lead: Dict) -> bool:
    """Email EVERY advisor the lead's details via the configured transport.

    Returns True on send, False if nothing is configured or the send failed.
    Never raises - the booking is already confirmed by the time we get here.
    """
    advisors = _normalize(advisor_emails)
    if not advisors or not is_configured():
        return False
    if not _from_email():
        print("[email_service] no EMAIL_FROM / SMTP_USER set; cannot send")
        return False

    cc = _cc_list()
    subject = _subject(lead)
    body = _format_body(lead)

    if _resend_key():
        return _send_via_resend(advisors, cc, subject, body)
    if _sendgrid_key():
        return _send_via_sendgrid(advisors, cc, subject, body)
    if _brevo_key():
        return _send_via_brevo(advisors, cc, subject, body)
    return _send_via_smtp(advisors, cc, subject, body)
