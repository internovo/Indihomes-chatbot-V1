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

    def test_change_location_unrecognised_area_always_includes_recommendations_key(self):
        # Regression guard for a real bug caught live in production, AFTER
        # the routing itself was fixed: "Send Andheri also" (a real area,
        # just not one we stock) hit change_location's early-exit branch,
        # which returned reply_text but no "recommendations" key at all.
        # WATI's {{recommendations}} is a persistent Contact Attribute, not
        # reset per turn - on a contact that had never had it set before,
        # WATI printed the literal unsubstituted "{{recommendations}}"
        # token straight into the WhatsApp message. The key must always be
        # present (even as "") so WATI always has something real to
        # substitute. See claude.md, "Free-text handling" changelog.
        resp = self.client.post("/interpret-message", json={
            "phone": "919999900103", "message": "Send Andheri also",
        })
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["intent"], "change_location")
        self.assertIn("recommendations", body)
        self.assertEqual(body["recommendations"], "")

    @patch("app.conversation_lock.acquire", return_value=False)
    def test_change_location_lock_contention_also_includes_recommendations_key(self, _mock_acquire):
        # Same regression as above, but for the OTHER early-exit in the
        # change_location branch (a rapid double-tap / concurrent request
        # for the same phone finds the lock already held). Both early-exits
        # share the same bug class - easy to fix one and miss the other.
        resp = self.client.post("/interpret-message", json={
            "phone": "919999900104", "message": "Send Malad West also",
        })
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["intent"], "change_location")
        self.assertIn("recommendations", body)
        self.assertEqual(body["recommendations"], "")

    def test_none_intent_logs_needs_human_with_flow_step(self):
        # See claude.md, "Lead-safety-net": a genuinely unclassifiable
        # message previously vanished with no record anywhere. It must now
        # show up in appointments_db.list_needs_human, tagged with the
        # flow_step the WATI node sent - this is what lets an advisor see
        # WHERE a lead got stuck, not just that one did.
        phone = "919999900105"
        resp = self.client.post("/interpret-message", json={
            "phone": phone, "message": "asdkjfh gibberish",
            "flow_step": "budget", "name": "Test Lead",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["intent"], "none")

        rows = [r for r in appointments_db.list_needs_human(unnotified_only=False, limit=200)
                if r["lead_phone"] == phone]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["flow_step"], "budget")
        self.assertEqual(rows[0]["raw_message"], "asdkjfh gibberish")
        self.assertEqual(rows[0]["notified"], 0)

    def test_none_intent_without_flow_step_defaults_sensibly(self):
        # /property-detail's unparseable-choice branch never sends
        # flow_step (it predates the field) - must not crash, and must
        # fall back to a labelled default rather than an empty string.
        phone = "919999900106"
        resp = self.client.post("/interpret-message", json={
            "phone": phone, "message": "xyzxyz",
        })
        self.assertEqual(resp.status_code, 200)
        rows = [r for r in appointments_db.list_needs_human(unnotified_only=False, limit=200)
                if r["lead_phone"] == phone]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["flow_step"], "property_picker")

    def test_needs_human_leads_endpoint_lists_and_acks(self):
        phone = "919999900107"
        self.client.post("/interpret-message", json={
            "phone": phone, "message": "qwerty nonsense", "flow_step": "possession",
        })
        listing = self.client.get("/needs-human-leads", params={"limit": 200})
        self.assertEqual(listing.status_code, 200)
        matches = [r for r in listing.json()["leads"] if r["lead_phone"] == phone]
        self.assertEqual(len(matches), 1)
        row_id = matches[0]["id"]

        ack = self.client.post("/needs-human-leads/ack", json={"ids": [row_id]})
        self.assertEqual(ack.status_code, 200)
        self.assertEqual(ack.json()["acked"], "yes")

        listing_after = self.client.get("/needs-human-leads", params={"limit": 200})
        matches_after = [r for r in listing_after.json()["leads"] if r["lead_phone"] == phone]
        self.assertEqual(matches_after, [])  # acked rows are hidden by default

    def test_recognised_intent_does_not_log_needs_human(self):
        # A real global intent (stop/talk_to_advisor/reject_all/restart/
        # change_location) must NOT also create a needs_human row - that
        # table is specifically for the "we had nothing to offer" case, not
        # every fallback-triggered message.
        phone = "919999900108"
        self.client.post("/interpret-message", json={
            "phone": phone, "message": "please stop messaging me", "flow_step": "consent",
        })
        rows = [r for r in appointments_db.list_needs_human(unnotified_only=False, limit=200)
                if r["lead_phone"] == phone]
        self.assertEqual(rows, [])


class ParseChoicesTests(unittest.TestCase):
    """Direct, no-HTTP tests for app._parse_choices() - the multi-select
    parser. See its docstring for the real production transcript that
    motivated it: a customer replied "1 & 2" and only ever saw property #1,
    because the OLD single-number parser (_parse_choice) matches the first
    digit sequence it finds via re.search and stops there."""

    @classmethod
    def setUpClass(cls):
        import app as app_module
        cls.app_module = app_module

    def test_single_number_returns_one_index(self):
        self.assertEqual(self.app_module._parse_choices("2", 3), [2])

    def test_ampersand_separated(self):
        self.assertEqual(self.app_module._parse_choices("1 & 2", 3), [1, 2])

    def test_comma_separated(self):
        self.assertEqual(self.app_module._parse_choices("1, 3", 3), [1, 3])

    def test_word_and_separated(self):
        self.assertEqual(self.app_module._parse_choices("1 and 3", 3), [1, 3])

    def test_preserves_first_mentioned_order_not_numeric_order(self):
        # "2 & 1" should come back [2, 1], not sorted to [1, 2] - the
        # customer's own ordering is preserved (e.g. if they want property
        # 2 shown before property 1 in the combined reply).
        self.assertEqual(self.app_module._parse_choices("2 & 1", 3), [2, 1])

    def test_dedupes_repeated_numbers(self):
        self.assertEqual(self.app_module._parse_choices("1, 1, 2", 3), [1, 2])

    def test_out_of_range_numbers_are_dropped(self):
        # Shortlist only has 3 items - "5" must not appear even though
        # it's a well-formed number.
        self.assertEqual(self.app_module._parse_choices("1 & 5", 3), [1])

    def test_capped_at_max_choices(self):
        self.assertEqual(self.app_module._parse_choices("1,2,3,4,5", 5, max_choices=3), [1, 2, 3])

    def test_empty_or_no_digits_returns_empty_list(self):
        self.assertEqual(self.app_module._parse_choices("", 3), [])
        self.assertEqual(self.app_module._parse_choices("maybe the blue one", 3), [])


class MultiPropertySelectionTests(unittest.TestCase):
    """End-to-end reproduction of the Smriti transcript: replying "1 & 2"
    to "Which one would you like to see in detail?" must show BOTH
    properties, not silently truncate to the first one. See claude.md,
    "Multi-property selection"."""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        import app as app_module
        cls.app_module = app_module
        cls.client = TestClient(app_module.app)

    def setUp(self):
        self.phone = "919999900200"
        appointments_db.save_shortlist(self.phone, [
            {"index": 1, "name": "Siddhivinayak", "detail": "2BHK / 2.5BHK, starting 1.22 Cr",
             "image": "img1.png", "code": "SV1"},
            {"index": 2, "name": "Hitendra Dhamm", "detail": "2BHK / Jodi, starting 1.9 Cr",
             "image": "img2.png", "code": "HD1"},
            {"index": 3, "name": "Silver Serene", "detail": "2BHK, starting 1.98 Cr",
             "image": "img3.png", "code": "SS1"},
        ])

    def test_1_and_2_shows_both_properties_not_just_the_first(self):
        # This is THE regression test for the exact bug: the old parser
        # (re.search, first match only) would return just property #1 here.
        resp = self.client.post("/property-detail", json={
            "phone": self.phone, "choice": "1 & 2",
        })
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["found"], "yes")
        self.assertEqual(body["count"], 2)
        self.assertIn("Siddhivinayak", body["detail"])
        self.assertIn("Hitendra Dhamm", body["detail"])
        # Property #3 was NOT asked for - must not appear.
        self.assertNotIn("Silver Serene", body["detail"])

    def test_per_index_fields_populated_for_multi_select(self):
        resp = self.client.post("/property-detail", json={
            "phone": self.phone, "choice": "1 & 2",
        })
        body = resp.json()
        self.assertEqual(body["name1"], "Siddhivinayak")
        self.assertEqual(body["name2"], "Hitendra Dhamm")
        self.assertEqual(body["code1"], "SV1")
        self.assertEqual(body["code2"], "HD1")

    def test_top_level_image_is_first_picks_image_only(self):
        # WATI can only render ONE inline image per message today - a
        # known, documented limitation (see claude.md), not a bug. The
        # top-level image_url is the FIRST picked property's image.
        resp = self.client.post("/property-detail", json={
            "phone": self.phone, "choice": "1 & 2",
        })
        body = resp.json()
        self.assertEqual(body["image_url"], "img1.png")

    def test_single_number_unaffected_by_the_multi_select_change(self):
        # No regression: a plain "1" must behave exactly as it always did.
        resp = self.client.post("/property-detail", json={
            "phone": self.phone, "choice": "1",
        })
        body = resp.json()
        self.assertEqual(body["found"], "yes")
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["name"], "Siddhivinayak")
        self.assertEqual(body["detail"], "2BHK / 2.5BHK, starting 1.22 Cr")

    def test_three_properties_at_once(self):
        resp = self.client.post("/property-detail", json={
            "phone": self.phone, "choice": "1, 2 and 3",
        })
        body = resp.json()
        self.assertEqual(body["count"], 3)
        for name in ("Siddhivinayak", "Hitendra Dhamm", "Silver Serene"):
            self.assertIn(name, body["detail"])

    def test_multi_select_does_not_trigger_global_intent_classification(self):
        # "1 & 2" must resolve as a multi-select BEFORE ever reaching
        # intent_router.classify() - it should never be misread as some
        # other intent (it isn't one).
        resp = self.client.post("/property-detail", json={
            "phone": self.phone, "choice": "1 & 2",
        })
        body = resp.json()
        self.assertNotIn("intent", body)  # only the no-match branch sets this key


class CleanIncomingAtVariableLeakTests(unittest.TestCase):
    """Direct, no-HTTP tests for app._clean_incoming()'s @variable guard.

    Real production bug (see claude.md): main_webhook-dcBeI's /search body
    always sends "amenities":"@amenities", but that Flow Variable is only
    ever assigned when the customer picks "Amenities" as their top
    priority - every other priority path never touches it. For those
    sessions WATI sent the literal string "@amenities" straight through,
    silently corrupting the amenity-based sort ranking on the majority of
    real searches (confirmed live for two different phone numbers, both of
    whom picked a DIFFERENT priority).
    """

    @classmethod
    def setUpClass(cls):
        import app as app_module
        cls.app_module = app_module

    def test_bare_at_identifier_is_treated_as_empty(self):
        for leaked in ["@amenities", "@builder_pref", "@possession_pref", "@x"]:
            self.assertEqual(self.app_module._clean_incoming(leaked), "", msg=leaked)

    def test_leading_trailing_whitespace_still_matches(self):
        self.assertEqual(self.app_module._clean_incoming("  @amenities  "), "")

    def test_real_message_containing_at_symbol_is_not_touched(self):
        # Only a WHOLE-STRING match against the bare @identifier pattern is
        # stripped - a real customer message that happens to contain "@"
        # (an email address, or an "@" used conversationally) must survive
        # untouched, same discipline as the existing {{...}} guard.
        for real in ["user@example.com", "call me @ 5pm", "my email is a@b.com please"]:
            self.assertEqual(self.app_module._clean_incoming(real), real, msg=real)

    def test_curly_brace_leak_still_handled_unchanged(self):
        # Regression guard: adding the @ check must not disturb the
        # pre-existing {{ }} guard.
        self.assertEqual(self.app_module._clean_incoming("{{recommendations}}"), "")

    def test_normal_values_pass_through_unchanged(self):
        for real in ["2 BHK", "1 Cr - 2 Cr", "Goregaon West", ""]:
            self.assertEqual(self.app_module._clean_incoming(real), real, msg=real)

    @patch("app.search")
    def test_search_never_receives_a_leaked_amenities_placeholder(self, mock_search):
        # End-to-end: /search called with the exact malformed body WATI
        # sent live ("amenities": "@amenities") must call property_core's
        # search() with amenities="", never the literal garbage string.
        mock_search.return_value = {
            "recommendations": "1. Some Project", "count": 1,
            "shortlist": [{"index": 1, "name": "Some Project", "detail": "",
                           "image": "", "code": "X1"}],
            "min_price": "", "max_price": "",
        }
        from fastapi.testclient import TestClient
        client = TestClient(self.app_module.app)
        resp = client.post("/search", json={
            "phone": "919999900201", "location": "Goregaon West",
            "configuration": "2 BHK", "budget": "1 Cr - 2 Cr",
            "amenities": "@amenities",
        })
        self.assertEqual(resp.status_code, 200)
        mock_search.assert_called_once()
        self.assertEqual(mock_search.call_args.kwargs.get("amenities", "MISSING"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
