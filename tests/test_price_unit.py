"""
Tests for property_core.price_to_cr() honouring startingPrice.unit.

Run: python -m unittest tests.test_price_unit -v

Reproduces the production defect found 2026-09-21: INV_MW_441 (Chaitanya
Ethics Orovia) comes back from the CRM as {"value": 1.79, "unit": "Crore"}
and was rendered to WhatsApp customers as "starting 0.02 Cr", because the
old lakh_to_cr() divided by 100 unconditionally. search() filters on the
same number, so the 1.79 Cr flat also passed an "Under 1 Cr" budget and
was the ONLY result shown to anyone in that band.

Pure arithmetic + one _normalize() pass - no network, no GROQ_API_KEY.
"""

import unittest

from property_core import _normalize, price_to_cr


class PriceToCrTests(unittest.TestCase):
    def test_lakh_unit_divides_by_100(self):
        """The common case - 152 of 153 live records carry unit "lakh"."""
        self.assertEqual(price_to_cr({"value": 223, "unit": "lakh"}), 2.23)
        self.assertEqual(price_to_cr({"value": 143, "unit": "Lakh"}), 1.43)

    def test_crore_unit_is_taken_as_is(self):
        """The bug: 1.79 Crore must stay 1.79, not become 0.02."""
        self.assertEqual(price_to_cr({"value": 1.79, "unit": "Crore"}), 1.79)
        self.assertEqual(price_to_cr({"value": 2.5, "unit": "cr"}), 2.5)

    def test_missing_unit_still_assumes_lakh(self):
        """Unit absent/blank keeps the old behaviour - most of the corpus
        predates the field being populated at all."""
        self.assertEqual(price_to_cr({"value": 159}), 1.59)
        self.assertEqual(price_to_cr({"value": 159, "unit": ""}), 1.59)

    def test_junk_and_missing_values_are_zero_not_a_crash(self):
        """Callers render 'Price on request' for 0.0 - never a 500."""
        for junk in ({}, None, {"value": None}, {"value": ""}, {"value": "abc"}):
            self.assertEqual(price_to_cr(junk), 0.0, junk)

    def test_normalize_uses_the_unit_end_to_end(self):
        """The real INV_MW_441 record shape, through the same path search()
        and the WhatsApp detail block read price_cr from."""
        record = {
            "projectName": "INV_MW_441",
            "displayName": "Chaitanya Ethics Orovia",
            "location": {"label": "Malad West", "value": "malad west"},
            "startingPrice": {"value": 1.79, "unit": "Crore"},
            "flatConfiguration": ["2BHK", "3BHK"],
        }
        self.assertEqual(_normalize(record)["price_cr"], 1.79)

        record["startingPrice"] = {"value": 223, "unit": "lakh"}
        self.assertEqual(_normalize(record)["price_cr"], 2.23)


if __name__ == "__main__":
    unittest.main()
