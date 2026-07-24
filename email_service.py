"""
Advisor email notifications for appointment bookings.

When a site visit is booked, we email every advisor the lead's full
requirements (name, phone, chosen slot, budget, configuration, area, and the
property they were looking at). This is deliberately NOT done through Google
Calendar invites: sharing/subscribing to the calendar does not send per-event
emails, and our service account cannot add attendees without domain-wide
delegation. Sending the mail ourselves means we control the content and it
works today, with no Workspace admin changes.

Transport: Gmail / Google Workspace SMTP with an APP PASSWORD (not the
account's real password). Generate one at https://myaccount.google.com/apppasswords
(requires 2-Step Verification on that account).

Setup (.env):
  SMTP_HOST=smtp.gmail.com
  SMTP_PORT=587
  SMTP_USER=tech@internovo.in
  SMTP_APP_PASSWORD=xxxxxxxxxxxxxxxx     # 16-char Google app password, no spaces
  SMTP_FROM_NAME=Indihomes Bookings
  NOTIFY_CC=                             # optional, comma-separated oversight inboxes

Like calendar_service, every function here is defensive: if SMTP isn't
configured or the send fails, we log and return False instead of raising, so a
confirmed booking is never lost just because the notification email didn't go.
"""

import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Dict, List


def is_configured() -> bool:
    """Whether we have enough to even attempt an SMTP send."""
    return bool(os.environ.get("SMTP_USER") and os.environ.get("SMTP_APP_PASSWORD"))


def _cc_list() -> List[str]:
    raw = os.environ.get("NOTIFY_CC", "")
    return [e.strip() for e in raw.split(",") if e.strip()]


def _format_body(lead: Dict) -> str:
    """Plain-text body. Only include lines we actually have a value for, so the
    advisor reads a clean summary instead of a wall of 'N/A'."""
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


def send_booking_notification(advisor_emails, lead: Dict) -> bool:
    """Email EVERY advisor the lead's details.

    Accepts a list of advisor addresses (a single string is also tolerated).
    Every advisor is put on the To line so all of them see each booking.

    Returns True if the mail was sent, False if SMTP isn't configured, there
    are no advisor addresses, or the send failed. Never raises - the booking
    has already been confirmed by the time we get here, so a failed email must
    not turn into a 500 back to WATI.
    """
    if isinstance(advisor_emails, str):
        advisor_emails = [advisor_emails]
    advisor_emails = [e.strip() for e in (advisor_emails or []) if e and e.strip()]

    if not is_configured() or not advisor_emails:
        return False

    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    try:
        port = int(os.environ.get("SMTP_PORT", "587") or 587)
    except (TypeError, ValueError):
        port = 587
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_APP_PASSWORD", "")
    from_name = os.environ.get("SMTP_FROM_NAME", "Indihomes Bookings")

    name = (lead.get("name") or "").strip()
    slot = (lead.get("slot_label") or "").strip()
    subject = "New site visit"
    if name:
        subject += f" - {name}"
    if slot:
        subject += f" ({slot})"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{user}>"
    msg["To"] = ", ".join(advisor_emails)
    cc = _cc_list()
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg.set_content(_format_body(lead))

    recipients = advisor_emails + cc
    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(host, port, timeout=20) as server:
            server.starttls(context=context)
            server.login(user, password)
            server.send_message(msg, to_addrs=recipients)
        return True
    except Exception as e:
        print(f"[email_service] failed to send booking notification: {e}")
        return False
