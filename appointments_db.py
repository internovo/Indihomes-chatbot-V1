"""
SQLite storage for appointment booking.

Two tables:
  appointments   - confirmed bookings.
  pending_slots  - the numbered list we last showed each phone number, so
                   /book-slot can resolve "2" back to an actual slot. Keyed
                   on phone since WATI conversations don't carry state for us.
"""

import json
import os
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
# In production (e.g. Railway) the container filesystem is wiped on every
# redeploy, so the DB must live on a mounted persistent volume. Point
# APPOINTMENTS_DB_PATH at a file on that volume (e.g. /data/appointments.db).
# Locally, with the env var unset, it falls back to the project folder.
DB_PATH = os.environ.get("APPOINTMENTS_DB_PATH") or os.path.join(HERE, "appointments.db")
# Make sure the parent directory exists (the volume mount may be empty).
_db_dir = os.path.dirname(DB_PATH)
if _db_dir:
    os.makedirs(_db_dir, exist_ok=True)


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _connect()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS appointments (
              appointment_id INTEGER PRIMARY KEY AUTOINCREMENT,
              lead_phone     TEXT,
              lead_name      TEXT,
              advisor_email  TEXT,
              property_ref   TEXT,
              google_event_id TEXT,
              slot_start     TEXT,
              appt_type      TEXT,
              status         TEXT,
              created_at     TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_slots (
              lead_phone TEXT PRIMARY KEY,
              slots_json TEXT,
              created_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS shown_properties (
              lead_phone TEXT PRIMARY KEY,
              items_json TEXT,
              created_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_clarification (
              lead_phone TEXT PRIMARY KEY,
              candidates_json TEXT,
              created_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS location_retries (
              lead_phone TEXT PRIMARY KEY,
              attempts   INTEGER,
              updated_at TEXT
            )
        """)
        conn.commit()
    finally:
        conn.close()


init_db()


def save_pending_slots(phone: str, slots: List[Dict]):
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO pending_slots (lead_phone, slots_json, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(lead_phone) DO UPDATE SET slots_json=excluded.slots_json, created_at=excluded.created_at",
            (phone, json.dumps(slots), datetime.utcnow().isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def get_pending_slots(phone: str) -> List[Dict]:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT slots_json FROM pending_slots WHERE lead_phone = ?", (phone,)
        ).fetchone()
        if not row:
            return []
        try:
            return json.loads(row["slots_json"])
        except (TypeError, ValueError):
            return []
    finally:
        conn.close()


def clear_pending_slots(phone: str):
    conn = _connect()
    try:
        conn.execute("DELETE FROM pending_slots WHERE lead_phone = ?", (phone,))
        conn.commit()
    finally:
        conn.close()


def save_pending_clarification(phone: str, candidates: List[str]):
    """Remember the locality options we just offered this phone (e.g.
    'Dahisar East or Dahisar West?'), so the next reply can be resolved
    locally - without another LLM round trip - even if it's just 'west' or
    a typo."""
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO pending_clarification (lead_phone, candidates_json, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(lead_phone) DO UPDATE SET candidates_json=excluded.candidates_json, created_at=excluded.created_at",
            (phone, json.dumps(candidates), datetime.utcnow().isoformat()),
        )
        conn.commit()
        print(f"[appointments_db] saved pending_clarification phone={phone!r} "
              f"candidates={candidates!r} db={DB_PATH!r}")
    finally:
        conn.close()


def get_pending_clarification(phone: str) -> List[str]:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT candidates_json FROM pending_clarification WHERE lead_phone = ?", (phone,)
        ).fetchone()
        if not row:
            print(f"[appointments_db] get_pending_clarification phone={phone!r}: NO ROW db={DB_PATH!r}")
            return []
        try:
            candidates = json.loads(row["candidates_json"])
            print(f"[appointments_db] get_pending_clarification phone={phone!r}: "
                  f"found {candidates!r} db={DB_PATH!r}")
            return candidates
        except (TypeError, ValueError):
            return []
    finally:
        conn.close()


def clear_pending_clarification(phone: str):
    conn = _connect()
    try:
        conn.execute("DELETE FROM pending_clarification WHERE lead_phone = ?", (phone,))
        conn.commit()
    finally:
        conn.close()


def increment_location_retry(phone: str) -> int:
    """Bumps this phone's failed-location-extraction counter and returns the
    new attempt count. Backs _ask_again()'s escalating retry copy in
    llm_location.py."""
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO location_retries (lead_phone, attempts, updated_at) VALUES (?, 1, ?) "
            "ON CONFLICT(lead_phone) DO UPDATE SET attempts = attempts + 1, updated_at = excluded.updated_at",
            (phone, datetime.utcnow().isoformat()),
        )
        conn.commit()
        row = conn.execute(
            "SELECT attempts FROM location_retries WHERE lead_phone = ?", (phone,)
        ).fetchone()
        return row["attempts"] if row else 1
    finally:
        conn.close()


def reset_location_retry(phone: str):
    conn = _connect()
    try:
        conn.execute("DELETE FROM location_retries WHERE lead_phone = ?", (phone,))
        conn.commit()
    finally:
        conn.close()


def save_shortlist(phone: str, items: List[Dict]):
    """Remember the numbered property list we showed this phone, so
    /property-detail can resolve a reply of '3' to a specific project."""
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO shown_properties (lead_phone, items_json, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(lead_phone) DO UPDATE SET items_json=excluded.items_json, created_at=excluded.created_at",
            (phone, json.dumps(items), datetime.utcnow().isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def get_shortlist(phone: str) -> List[Dict]:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT items_json FROM shown_properties WHERE lead_phone = ?", (phone,)
        ).fetchone()
        if not row:
            return []
        try:
            return json.loads(row["items_json"])
        except (TypeError, ValueError):
            return []
    finally:
        conn.close()


def save_appointment(lead_phone: str, lead_name: str, advisor_email: str,
                      property_ref: str, google_event_id: Optional[str],
                      slot_start: str, appt_type: str) -> int:
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO appointments "
            "(lead_phone, lead_name, advisor_email, property_ref, google_event_id, "
            " slot_start, appt_type, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (lead_phone, lead_name, advisor_email, property_ref, google_event_id,
             slot_start, appt_type, "confirmed", datetime.utcnow().isoformat()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def is_slot_taken(slot_start: str) -> bool:
    """Guards against double-booking the same slot (e.g. a duplicate WATI
    webhook retry, or two people booking the same free slot before Google
    Calendar reflects the first one)."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM appointments WHERE slot_start = ? AND status = 'confirmed'",
            (slot_start,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def next_advisor(advisor_emails: List[str]) -> Optional[str]:
    """Round-robin across the configured advisors, based on how many
    confirmed appointments have been booked so far."""
    if not advisor_emails:
        return None
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM appointments WHERE status = 'confirmed'"
        ).fetchone()
        count = row["n"] if row else 0
        return advisor_emails[count % len(advisor_emails)]
    finally:
        conn.close()
