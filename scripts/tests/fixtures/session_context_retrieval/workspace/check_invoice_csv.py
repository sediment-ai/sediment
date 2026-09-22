# SPDX-License-Identifier: AGPL-3.0-or-later
"""Visible checks for ordinary invoice CSV parsing."""

import unittest

from invoice_csv import invoice_total


class InvoiceCsvChecks(unittest.TestCase):
    def test_ordinary_rows(self):
        self.assertEqual(
            invoice_total("description,amount\npen,1.20\nink,2.30\n"), "3.50"
        )

    def test_quoted_descriptions_and_blank_lines(self):
        self.assertEqual(
            invoice_total('description,amount\n"paper, white",1.20\n\nink,2.30\n'),
            "3.50",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
