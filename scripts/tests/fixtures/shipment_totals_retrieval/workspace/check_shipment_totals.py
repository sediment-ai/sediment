# SPDX-License-Identifier: AGPL-3.0-or-later
"""Visible checks for shipment CSV parsing and totals."""

import unittest

from shipment_totals import totals_by_destination


class ShipmentChecks(unittest.TestCase):
    def test_ordinary_destinations(self):
        self.assertEqual(
            totals_by_destination("North,3,web\nSouth,2,manual\nNorth,4,api\n"),
            {"North": 7, "South": 2},
        )

    def test_quoted_destination(self):
        self.assertEqual(
            totals_by_destination('"Dock, East",3,web\n"Dock, East",5,api\n'),
            {"Dock, East": 8},
        )

    def test_blank_rows_and_whitespace(self):
        self.assertEqual(
            totals_by_destination("\n North ,3, web \n\nNorth,2, manual \n"),
            {"North": 5},
        )

    def test_empty_input(self):
        self.assertEqual(totals_by_destination(""), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
