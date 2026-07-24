"""
Google Calendar integration for appointment booking.

One shared calendar, service-account owned. Advisors are added as event
attendees by email so Google emails them the invite directly - this works
whether the calendar itself is a Workspace resource or not.

Setup (.env):
  GOOGLE_CALENDAR_ID=<shared calendar id>
  GOOGLE_SERVICE_ACCOUNT_FILE=service_account.json
  ADVISOR_EMAILS=advisor1@x.com,advisor2@x.com,advisor3@x.com
  BUSINESS_HOURS_START=10
  BUSINESS_HOURS_END=19
  SLOT_MINUTES=60
  TIMEZONE=Asia/Kolkata

Every function here is defensive: if credentials are missing or Google is
unreachable, callers get an empty list / None back instead of an exception,
so app.py can fall back to "an advisor will call" instead of a 500.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9 fallback, shouldn't hit this here
    ZoneInfo = None

SCOPES = ["https://www.googleapis.com/auth/calendar"]

HERE = os.path.dirname(os.path.abspath(__file__))


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _tz():
    if not ZoneInfo:
        return None
    tz_name = os.environ.get("TIMEZONE", "Asia/Kolkata")
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return None


def _calendar_id() -> str:
    return os.environ.get("GOOGLE_CALENDAR_ID", "")


def _key_file_path() -> str:
    """Absolute path to the service-account key file, if one is configured."""
    key_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "")
    if not key_file:
        return ""
    return key_file if os.path.isabs(key_file) else os.path.join(HERE, key_file)


def _load_credentials():
    """Build service-account credentials from whichever source is available.

    Prefers GOOGLE_SERVICE_ACCOUNT_JSON (the full key JSON pasted into an env
    var) - this is how we ship creds to hosts like Railway where the .json
    file isn't committed to the repo. Falls back to the on-disk key file for
    local development. Returns None (never raises) if neither works."""
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if raw:
        try:
            info = json.loads(raw)
            return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        except Exception:
            return None
    path = _key_file_path()
    if not path or not os.path.isfile(path):
        return None
    try:
        return service_account.Credentials.from_service_account_file(path, scopes=SCOPES)
    except Exception:
        return None


def is_configured() -> bool:
    """Whether we have enough env/creds to even try talking to Google."""
    if not _calendar_id():
        return False
    # Either inline JSON creds (hosted) or a real key file on disk (local).
    if os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip():
        return True
    path = _key_file_path()
    return bool(path) and os.path.isfile(path)


def _service():
    """Builds an authenticated Calendar API client, or None if unavailable."""
    if not is_configured():
        return None
    creds = _load_credentials()
    if not creds:
        return None
    try:
        return build("calendar", "v3", credentials=creds, cache_discovery=False)
    except Exception:
        return None


def advisor_emails() -> List[str]:
    raw = os.environ.get("ADVISOR_EMAILS", "")
    return [e.strip() for e in raw.split(",") if e.strip()]


def get_busy_periods(days_ahead: int = 5) -> Optional[List[Dict]]:
    """Busy time ranges on the shared calendar over the next N days.
    Returns None (never raises) if the calendar could not be reached at all,
    so callers can tell "no busy events" apart from "couldn't check" -
    treating an unreachable calendar as empty would show slots as free when
    we actually have no idea."""
    service = _service()
    cal_id = _calendar_id()
    if not service or not cal_id:
        return None

    tz = _tz()
    now = datetime.now(tz) if tz else datetime.now(timezone.utc)
    time_min = now.isoformat()
    time_max = (now + timedelta(days=days_ahead)).isoformat()

    try:
        body = {
            "timeMin": time_min,
            "timeMax": time_max,
            "items": [{"id": cal_id}],
        }
        result = service.freebusy().query(body=body).execute()
        cal_result = result.get("calendars", {}).get(cal_id, {})
        if cal_result.get("errors"):
            print(f"[calendar_service] freebusy returned errors: {cal_result['errors']}")
            return None
        return cal_result.get("busy", [])
    except HttpError as e:
        print(f"[calendar_service] freebusy query failed: {e}")
        return None
    except Exception as e:
        print(f"[calendar_service] freebusy query failed: {e}")
        return None


def generate_candidate_slots(days_ahead: int = 5) -> List[datetime]:
    """Every slot in business hours over the next N days, skipping Sundays
    and skipping anything less than 2 hours from now."""
    tz = _tz()
    now = datetime.now(tz) if tz else datetime.now(timezone.utc)
    earliest = now + timedelta(hours=2)

    start_hour = _env_int("BUSINESS_HOURS_START", 10)
    end_hour = _env_int("BUSINESS_HOURS_END", 19)
    slot_minutes = _env_int("SLOT_MINUTES", 60)

    slots = []
    for day_offset in range(days_ahead + 1):
        day = (now + timedelta(days=day_offset)).replace(hour=0, minute=0, second=0, microsecond=0)
        if day.weekday() == 6:  # Sunday
            continue
        cursor = day.replace(hour=start_hour)
        day_end = day.replace(hour=end_hour)
        while cursor + timedelta(minutes=slot_minutes) <= day_end:
            if cursor >= earliest:
                slots.append(cursor)
            cursor += timedelta(minutes=slot_minutes)
    return slots


def _overlaps_busy(slot_start: datetime, slot_minutes: int, busy: List[Dict]) -> bool:
    slot_end = slot_start + timedelta(minutes=slot_minutes)
    for b in busy:
        try:
            b_start = datetime.fromisoformat(b["start"].replace("Z", "+00:00"))
            b_end = datetime.fromisoformat(b["end"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if slot_start < b_end and slot_end > b_start:
            return True
    return False


def get_free_slots(days_ahead: int = 5, limit: int = 5) -> List[Dict]:
    """Candidate slots minus busy periods, formatted for display.
    Returns [] if Calendar isn't configured or reachable - caller decides
    what to tell the user in that case."""
    if not is_configured():
        return []

    slot_minutes = _env_int("SLOT_MINUTES", 60)
    candidates = generate_candidate_slots(days_ahead)
    busy = get_busy_periods(days_ahead)
    if busy is None:
        return []

    free = []
    for slot in candidates:
        if len(free) >= limit:
            break
        if _overlaps_busy(slot, slot_minutes, busy):
            continue
        free.append({
            "index": len(free) + 1,
            "start_iso": slot.isoformat(),
            "label": slot.strftime("%a %d %b, %I:%M %p").replace(" 0", " "),
        })
    return free


def create_event(slot_iso: str, customer_name: str, customer_phone: str,
                  advisor_email: str, notes: str = "") -> Optional[str]:
    """Creates the calendar event with the advisor as attendee so Google
    emails them the invite. Returns the Google event id, or None if the
    event could not be created at all."""
    service = _service()
    cal_id = _calendar_id()
    if not service or not cal_id:
        return None

    slot_minutes = _env_int("SLOT_MINUTES", 60)
    try:
        start_dt = datetime.fromisoformat(slot_iso)
    except ValueError:
        return None
    end_dt = start_dt + timedelta(minutes=slot_minutes)
    tz_name = os.environ.get("TIMEZONE", "Asia/Kolkata")

    summary = f"Indihomes site visit - {customer_name or customer_phone}"
    description = (
        f"Customer: {customer_name or 'N/A'}\n"
        f"Phone: {customer_phone or 'N/A'}\n"
        f"Advisor: {advisor_email or 'N/A'}\n"
        f"{notes}"
    ).strip()

    event = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": tz_name},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": tz_name},
    }

    # Try with the advisor as an attendee first (this is what triggers the
    # email invite). Service accounts can fail to invite attendees without
    # domain-wide delegation - if so, fall back to a plain event with the
    # advisor's details in the description rather than failing the booking.
    if advisor_email:
        event["attendees"] = [{"email": advisor_email}]

    try:
        created = service.events().insert(
            calendarId=cal_id, body=event, sendUpdates="all"
        ).execute()
        return created.get("id")
    except HttpError as e:
        if advisor_email:
            print(f"[calendar_service] attendee invite failed ({e}); retrying without attendee")
            event.pop("attendees", None)
            try:
                created = service.events().insert(
                    calendarId=cal_id, body=event, sendUpdates="none"
                ).execute()
                return created.get("id")
            except Exception as e2:
                print(f"[calendar_service] event creation failed entirely: {e2}")
                return None
        print(f"[calendar_service] event creation failed: {e}")
        return None
    except Exception as e:
        print(f"[calendar_service] unexpected error creating event: {e}")
        return None
