"""
Conversation activity tracking for the 2-hour follow-up scheduler.

Tracks when the bot last spoke to a phone number and when the user last
replied, so the scheduler can decide whether a re-engagement nudge is due.

Deliberately a separate file from appointments_db.py because the concern is
completely distinct: appointments_db.py tracks bookings; this file tracks
whether a conversation has gone quiet. Cramming them together would make
both files harder to read and test independently.

Uses the same SQLite file as appointments (APPOINTMENTS_DB_PATH env var) so
there is only one persistent volume to configure on Railway — one less thing
to forget. The table is created on import, same as appointments_db.py.

Table: conversation_activity
  lead_phone          TEXT PRIMARY KEY  — WhatsApp phone, same key as all other tables
  lead_name           TEXT              — best name seen so far; scheduler uses it in greeting
  last_bot_message    TEXT              — ISO UTC timestamp; set when bot sends recommendations
  last_user_message   TEXT              — ISO UTC timestamp; set on any user reply
  followup_due_at     TEXT              — ISO UTC timestamp; last_bot_message + 2h
  followup_sent       INTEGER DEFAULT 0 — 1 once the re-engagement message is sent
  conversation_status TEXT              — 'active' | 'followup_sent' | 'closed'

The scheduler's eligibility query is:
    followup_sent = 0
    AND followup_due_at <= now()
    AND (last_user_message IS NULL OR last_user_message < last_bot_message)

The last condition is the key safety: if the user replied to ANYTHING after the
bot sent recommendations (property detail, slot pick, etc.) we skip the nudge —
they are already engaged. touch_user_message() sets last_user_message whenever
the user triggers any endpoint, so we don't need to know what they said.
"""

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import business_hours

HERE = os.path.dirname(os.path.abspath(__file__))

# Reuse the same DB file as appointments so a single Railway volume covers both.
# If APPOINTMENTS_DB_PATH is unset (local dev), falls back to appointments.db
# in the project folder — same default as appointments_db.py, so they share
# the same local file automatically.
DB_PATH = os.environ.get("APPOINTMENTS_DB_PATH") or os.path.join(HERE, "appointments.db")
_db_dir = os.path.dirname(DB_PATH)
if _db_dir:
    os.makedirs(_db_dir, exist_ok=True)

# How long after the bot's last message we wait before sending a follow-up.
# 2 hours as specified. Named constant so tests can read it, and so changing
# the window is a one-line edit not a magic-number hunt.
FOLLOWUP_DELAY_HOURS: float = 2.0


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_table() -> None:
    """Create the conversation_activity table if it does not already exist.
    Called once at module import — same pattern as appointments_db.py's init_db().

    Also migrates in the off_hours_notified_date column (business-hours
    gating feature) if it's missing from an existing table. SQLite has no
    'ADD COLUMN IF NOT EXISTS', so this tries the ALTER and swallows the
    'duplicate column' error on every run after the first - the same
    lightweight, no-external-migration-tool pattern already used for every
    other table in this project (CREATE TABLE IF NOT EXISTS on import)."""
    conn = _connect()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS conversation_activity (
              lead_phone          TEXT PRIMARY KEY,
              lead_name           TEXT,
              last_bot_message    TEXT,
              last_user_message   TEXT,
              followup_due_at     TEXT,
              followup_sent       INTEGER DEFAULT 0,
              conversation_status TEXT,
              off_hours_notified_date TEXT
            )
        """)
        try:
            conn.execute(
                "ALTER TABLE conversation_activity ADD COLUMN off_hours_notified_date TEXT"
            )
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise
        conn.commit()
    finally:
        conn.close()


_init_table()


def _now_iso() -> str:
    """UTC ISO timestamp string, consistent format used in all timestamps here."""
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def touch_bot_message(phone: str, name: str = "") -> None:
    """Record that the bot just sent recommendations to this phone.

    Sets last_bot_message to now, schedules followup_due_at = now + FOLLOWUP_DELAY_HOURS,
    resets followup_sent to 0 (a new recommendations round opens a fresh follow-up window),
    and marks the conversation 'active'.

    Call this from one_call_search() immediately after the search succeeds and
    recommendations are returned — but ONLY when result["count"] > 0. There is
    nothing to follow up on if no properties matched.

    name is optional: we store the best name we have so the scheduler can use it
    in the WhatsApp greeting. If the flow sends an empty name here, the existing
    row's name is preserved via the DO UPDATE clause.
    """
    if not phone:
        return
    now = _now_iso()
    due = (datetime.now(timezone.utc).replace(tzinfo=None)
           + timedelta(hours=FOLLOWUP_DELAY_HOURS)).isoformat()
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO conversation_activity
              (lead_phone, lead_name, last_bot_message, followup_due_at,
               followup_sent, conversation_status)
            VALUES (?, ?, ?, ?, 0, 'active')
            ON CONFLICT(lead_phone) DO UPDATE SET
              lead_name           = CASE WHEN excluded.lead_name != '' THEN excluded.lead_name
                                         ELSE lead_name END,
              last_bot_message    = excluded.last_bot_message,
              followup_due_at     = excluded.followup_due_at,
              followup_sent       = 0,
              conversation_status = 'active'
            """,
            (phone, name or "", now, due),
        )
        conn.commit()
    finally:
        conn.close()


def touch_user_message(phone: str) -> None:
    """Record that the user just sent something (any reply at all).

    Sets last_user_message = now. This single timestamp is what prevents the
    scheduler from nudging someone who is actively engaged — if last_user_message
    >= last_bot_message the eligibility query returns no row for this phone.

    Does NOT need to know what the user said. Call it at the top of
    property_detail(), available_slots(), and book_slot() — any endpoint a live
    user action triggers.

    If no row exists yet (e.g. the user hit a direct endpoint without a prior
    search), this is a safe no-op because the WHERE clause won't match a
    non-existent row.
    """
    if not phone:
        return
    now = _now_iso()
    conn = _connect()
    try:
        conn.execute(
            "UPDATE conversation_activity SET last_user_message = ? WHERE lead_phone = ?",
            (now, phone),
        )
        conn.commit()
    finally:
        conn.close()


def get_due_followups() -> List[Dict]:
    """Return all rows that are eligible for a follow-up nudge right now.

    Eligibility:
      - followup_sent = 0              (not already sent)
      - followup_due_at <= now         (the 2-hour window has elapsed)
      - last_user_message < last_bot_message OR last_user_message IS NULL
        (user has NOT replied since the bot sent recommendations)
      - conversation_status = 'active' (not manually closed or already finalised)

    Returns a list of plain dicts (not sqlite3.Row objects) so the scheduler
    can iterate safely without holding a DB connection open.
    """
    now = _now_iso()
    conn = _connect()
    try:
        rows = conn.execute(
            """
            SELECT lead_phone, lead_name, followup_due_at
            FROM conversation_activity
            WHERE followup_sent = 0
              AND followup_due_at <= ?
              AND conversation_status = 'active'
              AND (last_user_message IS NULL OR last_user_message < last_bot_message)
            """,
            (now,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def mark_followup_sent(phone: str) -> None:
    """Mark the follow-up as sent so the scheduler never sends it again.

    Sets followup_sent = 1 and conversation_status = 'followup_sent'.
    Called by the scheduler immediately after a successful WATI push.
    """
    if not phone:
        return
    conn = _connect()
    try:
        conn.execute(
            """
            UPDATE conversation_activity
            SET followup_sent = 1, conversation_status = 'followup_sent'
            WHERE lead_phone = ?
            """,
            (phone,),
        )
        conn.commit()
    finally:
        conn.close()


def close_conversation(phone: str) -> None:
    """Mark a conversation as closed so the scheduler permanently skips it.

    Call this from:
      - /advisor-request (the follow-up button was tapped — conversation resolved)
      - /book-slot success path (site visit booked — fully resolved)
      - /save-lead (lead saved — no further nudging needed)

    A closed conversation will never appear in get_due_followups() again because
    conversation_status = 'closed' fails the WHERE clause.
    """
    if not phone:
        return
    conn = _connect()
    try:
        conn.execute(
            "UPDATE conversation_activity SET conversation_status = 'closed' WHERE lead_phone = ?",
            (phone,),
        )
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Business-hours off-hours notice tracking. See business_hours.py and
# claude.md's "Business hours gating" task section for the full design.
#
# Stores the notice date on the SAME conversation_activity row every other
# piece of per-phone state already lives on - per the design doc's own
# instruction ("conversation_tracker.py (SQLite) stores the notification
# flag alongside existing conversation state - no new table required"),
# this deliberately does NOT get its own table.
# --------------------------------------------------------------------------

def should_send_off_hours_notice(phone: str) -> bool:
    """True if this phone has NOT already been sent the off-hours notice
    today (IST calendar date - see business_hours.today_ist_date). Ensures
    the notice fires once per off-hours WINDOW per day, not once per
    off-hours message - a phone that sends five messages at 11 PM gets the
    notice on the first one only.

    True for a phone with no row at all yet (nothing to compare against -
    same "nothing stored yet" convention used everywhere else in this
    file, e.g. touch_user_message's no-op-on-missing-row).
    """
    if not phone:
        return False
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT off_hours_notified_date FROM conversation_activity WHERE lead_phone = ?",
            (phone,),
        ).fetchone()
        if not row:
            return True
        return row["off_hours_notified_date"] != business_hours.today_ist_date()
    finally:
        conn.close()


def mark_off_hours_notified(phone: str) -> None:
    """Record that this phone was just sent today's off-hours notice.
    Creates a row if none exists yet (a phone's very first message could
    land off-hours, before any /search has ever run touch_bot_message for
    them) - unlike touch_user_message, this must not be a no-op on a
    missing row, or the notice would re-send on every message from a brand
    new off-hours contact."""
    if not phone:
        return
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO conversation_activity (lead_phone, off_hours_notified_date)
            VALUES (?, ?)
            ON CONFLICT(lead_phone) DO UPDATE SET
              off_hours_notified_date = excluded.off_hours_notified_date
            """,
            (phone, business_hours.today_ist_date()),
        )
        conn.commit()
    finally:
        conn.close()
