"""
Tests for the burst-message / stale-clarification fixes in llm_location.py's
location() handler: the "Other Area" sentinel and the fresh-location
override path.

Run: python -m unittest tests.test_location_burst -v

Reproduces the production transcript that motivated both fixes (see
claude.md, "Burst messages / location debounce"):

    Hitesh: Malad
    Hitesh: Goregaon           <- sent 1 second later, raced the flow
    Bot: Just to narrow it down - Malad East or Malad West?
    Hitesh: Other Area

No network/GROQ_API_KEY needed - both fixed paths are deterministic
(normalize_location's whitelist) and return before call_llm() is ever
reached. Seeds pending-clarification state directly via appointments_db,
same technique as tests/test_pending_clarification_live.py, so this
doesn't depend on the LLM's non-deterministic ambiguous/candidate output
on a given run.
"""

import time
import unittest

import appointments_db
from llm_location import LocationRequest, location


def _fresh_phone() -> str:
    return "9199991" + str(int(time.time() * 1000))[-5:]


def _seed_malad_clarification(phone: str) -> None:
    """Puts `phone` into the exact state _resolve() leaves it in right
    after asking 'Malad East or Malad West?' - matching Hitesh's real
    transcript, no LLM call needed to set this up."""
    appointments_db.save_pending_clarification(phone, ["Malad East", "Malad West"])
    appointments_db.reset_location_retry(phone)


class OtherAreaSentinelTests(unittest.TestCase):
    """The second half of Hitesh's transcript: 'Other Area' typed/tapped
    while a clarification is pending."""

    def test_other_area_clears_pending_and_reopens_the_question(self):
        phone = _fresh_phone()
        _seed_malad_clarification(phone)

        out = location(LocationRequest(message="Other Area", phone=phone))

        self.assertEqual(out["needs_clarification"], "yes")
        self.assertIn("which area", out["clarify_question"].lower())
        self.assertEqual(appointments_db.get_pending_clarification(phone), [])

    def test_other_case_insensitive_and_bare_other(self):
        for text in ["OTHER AREA", "other", "  Other Area  "]:
            phone = _fresh_phone()
            _seed_malad_clarification(phone)
            out = location(LocationRequest(message=text, phone=phone))
            self.assertEqual(out["needs_clarification"], "yes", msg=text)
            self.assertEqual(appointments_db.get_pending_clarification(phone), [], msg=text)

    def test_other_area_does_not_count_toward_retry_handoff(self):
        phone = _fresh_phone()
        _seed_malad_clarification(phone)
        appointments_db.increment_location_retry(phone)
        appointments_db.increment_location_retry(phone)  # 2 prior "real" failures

        location(LocationRequest(message="Other Area", phone=phone))

        # reset_location_retry() was called, not increment - the counter
        # must be back at 0, not sitting at 3 (which would trigger handoff
        # on the customer's very next, possibly perfectly clear, answer).
        appointments_db.save_pending_clarification(phone, ["Malad East", "Malad West"])
        out = location(LocationRequest(message="banana", phone=phone))
        # A single bad guess right after should NOT itself hand off -
        # if the retry counter had stayed at 2+, this would.
        self.assertEqual(out.get("handoff", "no"), "no")

    def test_other_area_with_no_pending_clarification_falls_through_normally(self):
        # "Other Area" reaching /location OUTSIDE a pending clarification
        # (e.g. from the very first area-selection menu) is a pre-existing
        # path (FlexLocationRequest.best() already filters it there) -
        # this fix must not interfere with that; with nothing pending,
        # the sentinel branch is never entered at all.
        phone = _fresh_phone()
        appointments_db.clear_pending_clarification(phone)
        out = location(LocationRequest(message="Other Area", phone=phone))
        # Just must not crash and must not silently return a fabricated
        # normalized_location.
        self.assertNotEqual(out.get("normalized_location"), "Malad East")


class FreshLocationOverrideTests(unittest.TestCase):
    """The first half of Hitesh's transcript: 'Goregaon' arriving as the
    reply to a 'Malad East or Malad West?' clarification (a burst message
    racing the flow)."""

    def test_unrelated_known_area_overrides_stale_clarification(self):
        phone = _fresh_phone()
        _seed_malad_clarification(phone)

        out = location(LocationRequest(message="Goregaon", phone=phone))

        # Goregaon is itself East/West-ambiguous (SPLIT_RULES), so this
        # should produce a FRESH clarification about Goregaon, not the
        # stale Malad one, and not silently fail/loop.
        self.assertEqual(out["needs_clarification"], "yes")
        self.assertIn("goregaon", out["clarify_question"].lower())
        pending = appointments_db.get_pending_clarification(phone)
        self.assertEqual(set(pending), {"Goregaon East", "Goregaon West"})

    def test_unambiguous_known_area_resolves_immediately(self):
        phone = _fresh_phone()
        _seed_malad_clarification(phone)

        out = location(LocationRequest(message="Goregaon West", phone=phone))

        self.assertEqual(out["needs_clarification"], "no")
        self.assertEqual(out["normalized_location"], "Goregaon West")
        self.assertEqual(appointments_db.get_pending_clarification(phone), [])

    def test_reply_matching_offered_candidate_still_wins_over_override(self):
        # Regression guard: the override must only kick in when the reply
        # does NOT match the originally offered candidates - a real answer
        # to the actual question asked must still resolve normally.
        phone = _fresh_phone()
        _seed_malad_clarification(phone)

        out = location(LocationRequest(message="Malad West", phone=phone))

        self.assertEqual(out["needs_clarification"], "no")
        self.assertEqual(out["normalized_location"], "Malad West")

    def test_unrecognisable_text_falls_through_to_llm_path_unchanged(self):
        # Text that ISN'T a known area (and isn't "other area" either)
        # must still fall through to the existing LLM-based resolution -
        # this fix must not change behaviour for genuinely unclear input.
        phone = _fresh_phone()
        _seed_malad_clarification(phone)

        out = location(LocationRequest(message="xyzzy nonsense text", phone=phone))

        # Must not crash, and must not fabricate a normalized_location
        # from nothing.
        self.assertIn(out.get("needs_clarification"), ("yes", "no"))
        if out["needs_clarification"] == "no":
            self.assertEqual(out.get("normalized_location", ""), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
