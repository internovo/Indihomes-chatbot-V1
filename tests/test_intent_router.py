"""
Tests for intent_router.py, plus the app-level global-intent fallback paths
in app.py's /property-detail and /interpret-message.

Run:  python -m unittest tests.test_intent_router -v
      (from the project root, so `app`, `intent_router`, etc. import cleanly)

Reproduces the exact production transcript from claude.md, "Free-text
handling": a user replying "No one" and "Send in borivali east also" at
points in the flow the button graph didn't expect.

The Groq/LLM location call is never exercised here - intent_router.py is
rule-based by design (see its module docstring), and the app-level tests
mock property_core.search directly so they run with no network access and
no GROQ_API_KEY, same pattern as test_hardening.py.
"""

import unittest
from unittest.mock import patch
import time

import appointments_db
import intent_router


class ClassifyTests(unittest.TestCase):
    """Direct, no-DB, no-HTTP unit tests for intent_router.classify()."""

    def test_empty_text_is_none(self):
        self.assertEqual(intent_router.classify(""), {"intent": "none"})
        self.assertEqual(intent_router.classify("   "), {"intent": "none"})

    # --- stop --------------------------------------------------------
    def test_stop_variants(self):
        for text in ["stop", "STOP", "please unsubscribe", "don't message me again",
                     "do not contact me"]:
            self.assertEqual(intent_router.classify(text)["intent"], "stop", msg=text)

    def test_stop_wins_over_advisor_phrase_in_same_message(self):
        out = intent_router.classify("stop sending me messages, don't send an advisor either")
        self.assertEqual(out["intent"], "stop")

    # --- talk_to_advisor ----------------------------------------------
    def test_advisor_variants(self):
        for text in ["can I talk to someone", "connect me to an agent", "call me",
                     "I want to speak to a human"]:
            self.assertEqual(intent_router.classify(text)["intent"], "talk_to_advisor", msg=text)

    # --- reject_all (the "No one" case from the transcript) ------------
    def test_reject_all_variants(self):
        for text in ["No one", "none of these", "nothing here works",
                     "not interested in these"]:
            self.assertEqual(intent_router.classify(text)["intent"], "reject_all", msg=text)

    def test_bare_no_is_not_reject_all(self):
        # "no" alone must NOT be read as rejecting a shortlist - it's a
        # normal answer to unrelated yes/no questions elsewhere in the flow
        # (e.g. "is that a firm budget?"). See intent_router.py's
        # _REJECT_PHRASES comment.
        self.assertEqual(intent_router.classify("no")["intent"], "none")

    # --- change_location (the "Send in borivali east also" case) -------
    def test_change_location_with_signal_word(self):
        out = intent_router.classify("Send in borivali east also")
        self.assertEqual(out["intent"], "change_location")
        self.assertEqual(out["location_text"], "borivali east")

    def test_change_location_bare_short_message(self):
        # A short cold reply naming a place is the whole intent by itself -
        # no signal word required.
        out = intent_router.classify("Kandivali East")
        self.assertEqual(out["intent"], "change_location")
        self.assertEqual(out["location_text"], "kandivali east")

    def test_change_location_prefers_longest_match(self):
        out = intent_router.classify("what about malad west instead")
        self.assertEqual(out["location_text"], "malad west")  # not bare "malad"

    def test_unrecognised_area_is_not_change_location(self):
        # "Thane" isn't in KNOWN_LOCALITIES / SPLIT_RULES (no inventory) -
        # classify() must not invent a match.
        out = intent_router.classify("Thane, mulund")
        self.assertEqual(out["intent"], "none")

    # --- restart ---------------------------------------------------------
    def test_restart_variants(self):
        for text in ["start over please", "can we restart", "reset"]:
            self.assertEqual(intent_router.classify(text)["intent"], "restart", msg=text)

    # --- resolve_location_text -------------------------------------------
    def test_resolve_location_text_splits_generic_area(self):
        sides = intent_router.resolve_location_text("borivali")
        self.assertIn("Borivali East", sides)
        self.assertIn("Borivali West", sides)

    def test_resolve_location_text_unknown_returns_empty(self):
        self.assertEqual(intent_router.resolve_location_text("thane"), [])


class OptOutDbTests(unittest.TestCase):
    """appointments_db's opted_out table, used by the 'stop' intent.

    appointments_db deliberately has no delete_opted_out() (a do-not-contact
    record shouldn't be casually erasable from code - see
    appointments_db.py's comment above mark_opted_out). That means state
    persists in the real appointments.db across test runs, so a FIXED phone
    number here would pass once and then fail forever after. Each test gets
    its own phone, scoped to this run via time.time(), so re-running the
    suite never collides with a previous run's leftover state.
    """

    def setUp(self):
        self._unique = str(int(time.time() * 1000))

    def _fresh_phone(self) -> str:
        self._unique = str(int(self._unique) + 1)
        return "9199998" + self._unique[-5:]

    def test_mark_and_check(self):
        phone = self._fresh_phone()
        self.assertFalse(appointments_db.is_opted_out(phone))
        appointments_db.mark_opted_out(phone)
        self.assertTrue(appointments_db.is_opted_out(phone))

    def test_idempotent(self):
        phone = self._fresh_phone()
        appointments_db.mark_opted_out(phone)
        appointments_db.mark_opted_out(phone)  # must not raise
        self.assertTrue(appointments_db.is_opted_out(phone))

    def test_unknown_phone_is_not_opted_out(self):
        self.assertFalse(appointments_db.is_opted_out(self._fresh_phone()))


class AppGlobalIntentTests(unittest.TestCase):
    """End-to-end (via FastAPI TestClient) reproduction of the production
    transcript in claude.md - both messages that previously fell through to
    a dead-end reply now get a real response."""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        import app as app_module
        cls.app_module = app_module
        cls.client = TestClient(app_module.app)

    def setUp(self):
        self.phone = "919999900100"
        appointments_db.save_shortlist(self.phone, [
            {"index": 1, "name": "Sethia Pride", "detail": "1BHK / 2BHK, starting 0.98 Cr",
             "image": "", "code": "SP1"},
            {"index": 2, "name": "Samarpan Goldmist", "detail": "1BHK / 2BHK / 3BHK, starting 1.2 Cr",
             "image": "", "code": "SG1"},
            {"index": 3, "name": "Mahindra Vista", "detail": "1BHK / 2BHK / 3BHK / 4BHK, starting 1.4 Cr",
             "image": "", "code": "MV1"},
        ])

    def test_no_one_at_property_detail_offers_to_widen(self):
        # Reproduces: Megha replied "No one" to "Which one would you like to
        # see in detail?" and got "Please reply with a number between 1 and 3."
        resp = self.client.post("/property-detail", json={
            "phone": self.phone, "choice": "No one",
        })
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["intent"], "reject_all")
        self.assertEqual(body["is_global"], "yes")
        self.assertIn("widen", body["detail"].lower())

    def test_invalid_number_with_no_intent_still_gets_generic_retry(self):
        # A genuinely unparseable, non-global reply must keep the original
        # behaviour - no regression for the common case.
        resp = self.client.post("/property-detail", json={
            "phone": self.phone, "choice": "maybe the blue one",
        })
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["intent"], "none")
        self.assertIn("between 1 and 3", body["detail"])

    @patch("app.search")
    def test_change_location_at_property_detail_runs_a_fresh_search(self, mock_search):
        # Reproduces: "Send in borivali east also" typed at a node that
        # expected a 1-3 number. Now runs a real search instead of being
        # dropped/misread as an advisor decline.
        #
        # Patched as "app.search", NOT "property_core.search": app.py does
        # `from property_core import search`, which binds its own local name
        # in app's module namespace at import time. Patching the source
        # module's attribute after that binding already happened has no
        # effect on app's reference - the patch target has to be where the
        # name is actually looked up from.
        mock_search.return_value = {
            "recommendations": "1. Some Borivali Project\n...",
            "count": 1, "shortlist": [{"index": 1, "name": "Some Borivali Project",
                                        "detail": "", "image": "", "code": "BE1"}],
            "min_price": "", "max_price": "",
        }
        resp = self.client.post("/property-detail", json={
            "phone": self.phone, "choice": "Send in borivali east also",
        })
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["intent"], "change_location")
        self.assertEqual(body["is_global"], "yes")
        mock_search.assert_called_once()
        called_location = mock_search.call_args.kwargs.get("location", "")
        self.assertIn("Borivali East", called_location)
        # Regression guard for a real bug caught before this ever reached
        # WATI: `detail` was built as `reply_text or recommendations`, and
        # since reply_text is always truthy when set, the actual listings
        # never showed up - the customer would only see the intro line
        # "Sure - here's what's available there:" with nothing after it.
        # `detail` must contain BOTH the intro line AND the real listings.
        self.assertIn("Sure", body["detail"])
        self.assertIn("Some Borivali Project", body["detail"])

    def test_stop_via_interpret_message_marks_opted_out(self):
        phone = "919999900101"
        resp = self.client.post("/interpret-message", json={
            "phone": phone, "message": "please stop messaging me",
        })
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["intent"], "stop")
        self.assertEqual(body["is_global"], "yes")
        self.assertTrue(appointments_db.is_opted_out(phone))

    def test_none_intent_via_interpret_message(self):
        resp = self.client.post("/interpret-message", json={
            "phone": "919999900102", "message": "asdkjfh not a real intent",
        })
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["intent"], "none")
        self.assertEqual(body["is_global"], "no")

    def test_interpret_message_never_500s_on_empty_body(self):
        resp = self.client.post("/interpret-message", json={})
        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
