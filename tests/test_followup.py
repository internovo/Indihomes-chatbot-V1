"""
Tests for conversation_tracker.py and followup_scheduler.py.

Run:  python -m unittest tests.test_followup -v   (from the project root)

Uses a temp SQLite file (via APPOINTMENTS_DB_PATH) so tests never touch the
real appointments.db. No network — WATI sends are mocked.
"""

import os
import sys
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Point both conversation_tracker and appointments_db at the same temp file
# before importing them (they read DB_PATH at import time).
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["APPOINTMENTS_DB_PATH"] = _tmp.name

# Force re-import with the temp DB path.
import importlib
import conversation_tracker
importlib.reload(conversation_tracker)
import conversation_lock
import followup_scheduler


def _past_iso(hours: float = 3.0) -> str:
    return (datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(hours=hours)).isoformat()


def _future_iso(hours: float = 1.0) -> str:
    return (datetime.now(timezone.utc).replace(tzinfo=None)
            + timedelta(hours=hours)).isoformat()


def _row(phone: str):
    conn = sqlite3.connect(conversation_tracker.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM conversation_activity WHERE lead_phone = ?", (phone,)
        ).fetchone()
    finally:
        conn.close()


class ConversationTrackerTests(unittest.TestCase):
    def setUp(self):
        self._counter = getattr(ConversationTrackerTests, "_counter", 0) + 1
        ConversationTrackerTests._counter = self._counter
        self.phone = f"91910000{self._counter:04d}"

    def tearDown(self):
        conn = sqlite3.connect(conversation_tracker.DB_PATH)
        try:
            conn.execute("DELETE FROM conversation_activity WHERE lead_phone = ?", (self.phone,))
            conn.commit()
        finally:
            conn.close()

    def test_touch_bot_message_creates_active_row_with_due_at(self):
        conversation_tracker.touch_bot_message(self.phone, name="Rahul")
        row = _row(self.phone)
        self.assertIsNotNone(row)
        self.assertEqual(row["lead_name"], "Rahul")
        self.assertEqual(row["conversation_status"], "active")
        self.assertEqual(row["followup_sent"], 0)
        self.assertIsNotNone(row["last_bot_message"])
        self.assertIsNotNone(row["followup_due_at"])
        # followup_due_at should be roughly 2 hours after last_bot_message
        bot_ts = datetime.fromisoformat(row["last_bot_message"])
        due_ts = datetime.fromisoformat(row["followup_due_at"])
        delta_h = (due_ts - bot_ts).total_seconds() / 3600
        self.assertAlmostEqual(delta_h, conversation_tracker.FOLLOWUP_DELAY_HOURS, delta=0.01)

    def test_touch_bot_message_resets_followup_on_new_search(self):
        conversation_tracker.touch_bot_message(self.phone)
        conversation_tracker.mark_followup_sent(self.phone)
        conversation_tracker.touch_bot_message(self.phone)
        row = _row(self.phone)
        self.assertEqual(row["followup_sent"], 0)
        self.assertEqual(row["conversation_status"], "active")

    def test_get_due_followups_returns_eligible_row(self):
        conversation_tracker.touch_bot_message(self.phone, name="Test")
        conn = sqlite3.connect(conversation_tracker.DB_PATH)
        try:
            conn.execute(
                "UPDATE conversation_activity SET followup_due_at = ? WHERE lead_phone = ?",
                (_past_iso(), self.phone),
            )
            conn.commit()
        finally:
            conn.close()
        due = conversation_tracker.get_due_followups()
        phones = [r["lead_phone"] for r in due]
        self.assertIn(self.phone, phones)

    def test_touch_user_message_excludes_from_due_followups(self):
        """Scenario A: user replied before sweep — no nudge."""
        conversation_tracker.touch_bot_message(self.phone)
        conn = sqlite3.connect(conversation_tracker.DB_PATH)
        try:
            conn.execute(
                "UPDATE conversation_activity SET followup_due_at = ? WHERE lead_phone = ?",
                (_past_iso(), self.phone),
            )
            conn.commit()
        finally:
            conn.close()
        conversation_tracker.touch_user_message(self.phone)
        due = conversation_tracker.get_due_followups()
        phones = [r["lead_phone"] for r in due]
        self.assertNotIn(self.phone, phones)

    def test_future_due_at_not_eligible(self):
        conversation_tracker.touch_bot_message(self.phone)
        due = conversation_tracker.get_due_followups()
        phones = [r["lead_phone"] for r in due]
        self.assertNotIn(self.phone, phones)

    def test_mark_followup_sent_updates_status(self):
        conversation_tracker.touch_bot_message(self.phone)
        conversation_tracker.mark_followup_sent(self.phone)
        row = _row(self.phone)
        self.assertEqual(row["followup_sent"], 1)
        self.assertEqual(row["conversation_status"], "followup_sent")

    def test_close_conversation_excludes_from_due(self):
        conversation_tracker.touch_bot_message(self.phone)
        conn = sqlite3.connect(conversation_tracker.DB_PATH)
        try:
            conn.execute(
                "UPDATE conversation_activity SET followup_due_at = ? WHERE lead_phone = ?",
                (_past_iso(), self.phone),
            )
            conn.commit()
        finally:
            conn.close()
        conversation_tracker.close_conversation(self.phone)
        due = conversation_tracker.get_due_followups()
        phones = [r["lead_phone"] for r in due]
        self.assertNotIn(self.phone, phones)
        row = _row(self.phone)
        self.assertEqual(row["conversation_status"], "closed")


class FollowupSchedulerTests(unittest.TestCase):
    def setUp(self):
        self._counter = getattr(FollowupSchedulerTests, "_counter", 0) + 1
        FollowupSchedulerTests._counter = self._counter
        self.phone = f"91920000{self._counter:04d}"

    def tearDown(self):
        conn = sqlite3.connect(conversation_tracker.DB_PATH)
        try:
            conn.execute("DELETE FROM conversation_activity WHERE lead_phone = ?", (self.phone,))
            conn.commit()
        finally:
            conn.close()
        conversation_lock.release(self.phone)

    def _make_due(self):
        conversation_tracker.touch_bot_message(self.phone, name="Priya")
        conn = sqlite3.connect(conversation_tracker.DB_PATH)
        try:
            conn.execute(
                "UPDATE conversation_activity SET followup_due_at = ? WHERE lead_phone = ?",
                (_past_iso(), self.phone),
            )
            conn.commit()
        finally:
            conn.close()

    @patch("wati_client.is_configured", return_value=True)
    @patch("wati_client.send_followup_buttons", return_value=True)
    def test_sweep_sends_and_marks_sent(self, mock_send, _mock_cfg):
        self._make_due()
        followup_scheduler.run_followup_sweep()
        mock_send.assert_called_once_with(self.phone, "Priya")
        row = _row(self.phone)
        self.assertEqual(row["followup_sent"], 1)
        self.assertEqual(row["conversation_status"], "followup_sent")

    @patch("wati_client.is_configured", return_value=True)
    @patch("wati_client.send_followup_buttons", return_value=False)
    def test_sweep_does_not_mark_on_wati_failure(self, mock_send, _mock_cfg):
        self._make_due()
        followup_scheduler.run_followup_sweep()
        mock_send.assert_called_once()
        row = _row(self.phone)
        self.assertEqual(row["followup_sent"], 0)

    @patch("wati_client.is_configured", return_value=False)
    @patch("wati_client.send_followup_buttons")
    def test_sweep_skips_when_wati_not_configured(self, mock_send, _mock_cfg):
        self._make_due()
        followup_scheduler.run_followup_sweep()
        mock_send.assert_not_called()
        row = _row(self.phone)
        self.assertEqual(row["followup_sent"], 0)

    @patch("wati_client.is_configured", return_value=True)
    @patch("wati_client.send_followup_buttons", return_value=True)
    def test_concurrent_sweeps_only_one_send(self, mock_send, _mock_cfg):
        """Two overlapping sweeps for the same phone — lock prevents duplicate."""
        self._make_due()
        send_count = {"n": 0}
        send_guard = threading.Lock()
        first_acquired = threading.Event()

        def slow_send(phone, name):
            with send_guard:
                send_count["n"] += 1
            first_acquired.set()
            # Hold the lock long enough for the second sweep to try and fail.
            import time
            time.sleep(0.3)
            return True

        mock_send.side_effect = slow_send

        t1 = threading.Thread(target=followup_scheduler.run_followup_sweep)
        t2 = threading.Thread(target=followup_scheduler.run_followup_sweep)
        t1.start()
        first_acquired.wait(timeout=2.0)
        t2.start()
        t1.join(timeout=5.0)
        t2.join(timeout=5.0)

        self.assertEqual(send_count["n"], 1)
        row = _row(self.phone)
        self.assertEqual(row["followup_sent"], 1)


class FollowupSchedulerStartTests(unittest.TestCase):
    def test_start_returns_running_scheduler(self):
        sched = followup_scheduler.start()
        try:
            self.assertTrue(sched.running)
            self.assertTrue(followup_scheduler._scheduler_running())
        finally:
            sched.shutdown(wait=False)


if __name__ == "__main__":
    unittest.main()
