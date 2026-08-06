"""
Tests for business_hours.py, conversation_tracker.py's off-hours notice
tracking, and app.py's business-hours gating on every customer-facing
endpoint.

Run:  python -m unittest tests.test_business_hours -v
      (from the project root)

business_hours.is_business_hours() takes an optional `dt` so these tests
pass explicit datetimes rather than mocking the clock - simpler and less
fragile than patching datetime.now() everywhere it's used.

The app-level tests patch "app.business_hours.is_business_hours" (not
"business_hours.is_business_hours" - same lesson as
test_change_location_at_property_detail_runs_a_fresh_search in
test_intent_router.py: app.py does `import business_hours`, so patching
the attribute on the bound module object works either way here since
it's a module import, not a `from x import y` - but the ASSERTION that
matters is that real business logic (search/LLM/calendar) was NEVER
called when the gate fires, which is what these tests check.
"""

import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import appointments_db
import business_hours
import conversation_tracker

IST = ZoneInfo("Asia/Kolkata")


def _ist(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=IST)


class IsBusinessHoursTests(unittest.TestCase):
    def test_mid_morning_is_open(self):
        self.assertTrue(business_hours.is_business_hours(_ist(2026, 8, 6, 11, 0)))

    def test_exact_open_boundary_is_open(self):
        self.assertTrue(business_hours.is_business_hours(_ist(2026, 8, 6, 10, 0)))

    def test_exact_close_boundary_is_open(self):
        # Design doc specifies 10 AM - 7 PM inclusive; is_business_hours
        # uses <= on both ends.
        self.assertTrue(business_hours.is_business_hours(_ist(2026, 8, 6, 19, 0)))

    def test_one_minute_before_open_is_closed(self):
        self.assertFalse(business_hours.is_business_hours(_ist(2026, 8, 6, 9, 59)))

    def test_one_minute_after_close_is_closed(self):
        self.assertFalse(business_hours.is_business_hours(_ist(2026, 8, 6, 19, 1)))

    def test_midnight_is_closed(self):
        self.assertFalse(business_hours.is_business_hours(_ist(2026, 8, 6, 0, 0)))

    def test_converts_from_other_timezones(self):
        # 6:00 AM UTC = 11:30 AM IST (UTC+5:30) - should read as open.
        utc_morning = datetime(2026, 8, 6, 6, 0, tzinfo=ZoneInfo("UTC"))
        self.assertTrue(business_hours.is_business_hours(utc_morning))
        # 3:00 AM UTC = 8:30 AM IST - should read as closed.
        utc_early = datetime(2026, 8, 6, 3, 0, tzinfo=ZoneInfo("UTC"))
        self.assertFalse(business_hours.is_business_hours(utc_early))


class NextBusinessOpenTests(unittest.TestCase):
    def test_already_open_returns_same_time(self):
        now = _ist(2026, 8, 6, 14, 0)
        self.assertEqual(business_hours.next_business_open(now), now)

    def test_before_open_rolls_to_today_10am(self):
        now = _ist(2026, 8, 6, 6, 30)
        expected = _ist(2026, 8, 6, 10, 0)
        self.assertEqual(business_hours.next_business_open(now), expected)

    def test_after_close_rolls_to_tomorrow_10am(self):
        now = _ist(2026, 8, 6, 20, 15)
        expected = _ist(2026, 8, 7, 10, 0)
        self.assertEqual(business_hours.next_business_open(now), expected)


class TodayIstDateTests(unittest.TestCase):
    def test_returns_iso_date_in_ist(self):
        self.assertEqual(business_hours.today_ist_date(_ist(2026, 8, 6, 15, 0)), "2026-08-06")

    def test_utc_late_night_still_next_ist_day(self):
        # 9 PM UTC on the 5th = 2:30 AM IST on the 6th - date must roll.
        utc_dt = datetime(2026, 8, 5, 21, 0, tzinfo=ZoneInfo("UTC"))
        self.assertEqual(business_hours.today_ist_date(utc_dt), "2026-08-06")


class OffHoursNoticeTrackingTests(unittest.TestCase):
    """conversation_tracker's should_send_off_hours_notice / mark_off_hours_notified."""

    def setUp(self):
        import time
        self.phone = "9199997" + str(int(time.time() * 1000))[-5:]

    def test_true_for_never_seen_phone(self):
        self.assertTrue(conversation_tracker.should_send_off_hours_notice(self.phone))

    def test_false_immediately_after_marking(self):
        conversation_tracker.mark_off_hours_notified(self.phone)
        self.assertFalse(conversation_tracker.should_send_off_hours_notice(self.phone))

    def test_marking_does_not_raise_on_brand_new_phone(self):
        # Regression guard: mark_off_hours_notified must INSERT a row if
        # none exists (unlike touch_user_message, which is a no-op on a
        # missing row) - a phone's very first message could land off-hours.
        conversation_tracker.mark_off_hours_notified(self.phone)  # must not raise
        self.assertFalse(conversation_tracker.should_send_off_hours_notice(self.phone))

    def test_empty_phone_is_safe(self):
        self.assertFalse(conversation_tracker.should_send_off_hours_notice(""))
        conversation_tracker.mark_off_hours_notified("")  # must not raise


class AppGatingTests(unittest.TestCase):
    """Every customer-facing endpoint must gate off-hours: skip its real
    processing entirely and return a degraded, endpoint-shaped response
    with business_hours: "no"."""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        import app as app_module
        cls.app_module = app_module
        cls.client = TestClient(app_module.app)

    def setUp(self):
        import time
        self.phone = "9199996" + str(int(time.time() * 1000))[-5:]
        appointments_db.save_shortlist(self.phone, [
            {"index": 1, "name": "Test Project", "detail": "detail", "image": "", "code": "T1"},
        ])

    def test_search_gates_off_hours_and_never_calls_llm(self):
        with patch.object(self.app_module, "business_hours") as mock_bh, \
             patch.object(self.app_module, "call_llm") as mock_llm:
            mock_bh.is_business_hours.return_value = False
            resp = self.client.post("/search", json={
                "phone": self.phone, "location": "Malad West",
            })
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(body["business_hours"], "no")
            mock_llm.assert_not_called()

    def test_property_detail_gates_off_hours(self):
        with patch.object(self.app_module, "business_hours") as mock_bh:
            mock_bh.is_business_hours.return_value = False
            resp = self.client.post("/property-detail", json={
                "phone": self.phone, "choice": "1",
            })
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(body["business_hours"], "no")
            self.assertEqual(body["found"], "no")

    def test_location_gates_off_hours_via_existing_clarify_path(self):
        with patch.object(self.app_module, "business_hours") as mock_bh, \
             patch("llm_location.call_llm") as mock_llm:
            mock_bh.is_business_hours.return_value = False
            resp = self.client.post("/location", json={
                "location": "Malad", "phone": self.phone,
            })
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(body["business_hours"], "no")
            # Reuses the existing needs_clarification path - no new WATI
            # node required. See app.py's location() docstring.
            self.assertEqual(body["needs_clarification"], "yes")
            mock_llm.assert_not_called()

    def test_interpret_message_gates_off_hours(self):
        with patch.object(self.app_module, "business_hours") as mock_bh:
            mock_bh.is_business_hours.return_value = False
            resp = self.client.post("/interpret-message", json={
                "phone": self.phone, "message": "send malad west also",
            })
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(body["business_hours"], "no")
            self.assertEqual(body["intent"], "none")
            self.assertEqual(body["is_global"], "no")
            # Must include "recommendations" even off-hours, to avoid the
            # {{recommendations}} literal-token leak bug documented in
            # claude.md's "Free-text handling" changelog.
            self.assertIn("recommendations", body)

    def test_available_slots_gates_off_hours(self):
        with patch.object(self.app_module, "business_hours") as mock_bh, \
             patch.object(self.app_module, "calendar_service") as mock_cal:
            mock_bh.is_business_hours.return_value = False
            resp = self.client.post("/available-slots", json={"phone": self.phone})
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(body["business_hours"], "no")
            self.assertEqual(body["has_slots"], "no")
            mock_cal.get_free_slots.assert_not_called()

    def test_book_slot_gates_off_hours_and_never_creates_calendar_event(self):
        with patch.object(self.app_module, "business_hours") as mock_bh, \
             patch.object(self.app_module, "calendar_service") as mock_cal:
            mock_bh.is_business_hours.return_value = False
            resp = self.client.post("/book-slot", json={
                "phone": self.phone, "choice": "1", "name": "Test",
            })
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(body["business_hours"], "no")
            self.assertEqual(body["booked"], "no")
            mock_cal.create_event.assert_not_called()

    def test_advisor_request_gates_off_hours_and_sends_no_email(self):
        with patch.object(self.app_module, "business_hours") as mock_bh, \
             patch.object(self.app_module, "email_service") as mock_email:
            mock_bh.is_business_hours.return_value = False
            resp = self.client.post("/advisor-request", json={
                "phone": self.phone, "name": "Test",
            })
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(body["business_hours"], "no")
            self.assertEqual(body["notified"], "no")
            mock_email.send_booking_notification.assert_not_called()

    def test_health_reports_business_hours_status(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(resp.json()["business_hours"], ("open", "closed"))

    def test_within_business_hours_proceeds_normally(self):
        # Sanity check the mock itself: forcing True must NOT trigger the
        # gate, so the endpoint behaves exactly as it did before this
        # feature existed.
        with patch.object(self.app_module, "business_hours") as mock_bh:
            mock_bh.is_business_hours.return_value = True
            resp = self.client.post("/property-detail", json={
                "phone": self.phone, "choice": "1",
            })
            body = resp.json()
            self.assertNotIn("business_hours", body)
            self.assertEqual(body["found"], "yes")

    def test_first_off_hours_message_gets_full_notice_second_gets_short(self):
        with patch.object(self.app_module, "business_hours") as mock_bh:
            mock_bh.is_business_hours.return_value = False
            first = self.client.post("/interpret-message", json={
                "phone": self.phone, "message": "hello",
            }).json()
            second = self.client.post("/interpret-message", json={
                "phone": self.phone, "message": "hello again",
            }).json()
            self.assertEqual(first["reply_text"], self.app_module._OFF_HOURS_NOTICE)
            self.assertEqual(second["reply_text"], self.app_module._OFF_HOURS_SHORT)
            self.assertNotEqual(first["reply_text"], second["reply_text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
