"""
Tests that a bare area name which splits into two localities asks the
East/West question instead of silently searching both.

Run: python -m unittest tests.test_split_locality_clarification -v

Reproduces the production transcript from 2026-09-21 (phone 7208713112,
12:24 IST): the customer tapped "Goregaon", the backend logged
`/location call: phone='9172...' raw='Goregaon'` and answered
`needs_clarification: "no"` with `normalized_location:
"Goregaon East|Goregaon West"`. The flow's `@needs_clarification == yes`
check correctly evaluated false, so the East/West question was skipped and
both sides were searched together.

Cause: _resolve() only asked when the LLM itself set ambiguous=true with
>=2 candidates. Groq returns ambiguous=false for a bare "Goregaon", so
execution fell through to the success return, where normalize_location()
had ALREADY expanded it to two localities via SPLIT_RULES.

Calls _resolve() directly with a fabricated `extracted` dict - no network,
no GROQ_API_KEY, and phone="" throughout so nothing touches the database.
"""

import unittest

from llm_location import _resolve


def _extracted(loc, ambiguous=False, candidates=None, confidence=0.9):
    """What call_llm() returns for a confident, single-location read -
    exactly the shape that used to fall through to the success return."""
    return {
        "location": loc,
        "ambiguous": ambiguous,
        "candidate_localities": candidates or [],
        "confidence": confidence,
    }


class SplitLocalityAsksForClarificationTests(unittest.TestCase):
    def test_goregaon_asks_east_or_west(self):
        """The exact production case."""
        out = _resolve(_extracted("Goregaon"), phone="")

        self.assertEqual(out["needs_clarification"], "yes")
        self.assertEqual(out["clarify_options"], ["Goregaon East", "Goregaon West"])
        self.assertIn("Goregaon East", out["clarify_question"])
        self.assertIn("Goregaon West", out["clarify_question"])
        # Must not hand the flow a pipe-joined pair as if it were resolved.
        self.assertEqual(out["normalized_location"], "")
        self.assertEqual(out["handoff"], "no")

    def test_malad_also_asks(self):
        """Not special-cased to Goregaon - any SPLIT_RULES area behaves the
        same, which is what the burst-message fix already assumed."""
        out = _resolve(_extracted("Malad"), phone="")

        self.assertEqual(out["needs_clarification"], "yes")
        self.assertEqual(out["clarify_options"], ["Malad East", "Malad West"])

    def test_already_specific_locality_resolves_untouched(self):
        """Regression guard: a customer who said "Malad West" outright must
        NOT be asked to disambiguate something they already answered."""
        out = _resolve(_extracted("Malad West"), phone="")

        self.assertEqual(out["needs_clarification"], "no")
        self.assertEqual(out["normalized_location"], "Malad West")
        self.assertEqual(out["clarify_question"], "")

    def test_llm_flagged_ambiguity_still_wins_first(self):
        """The pre-existing ambiguous=true branch is untouched and still
        runs before the new one, so its landmark-aware copy survives."""
        out = _resolve(
            _extracted("", ambiguous=True,
                       candidates=["Malad East", "Malad West"]),
            phone="",
        )

        self.assertEqual(out["needs_clarification"], "yes")
        self.assertEqual(out["clarify_options"], ["Malad East", "Malad West"])


if __name__ == "__main__":
    unittest.main()
