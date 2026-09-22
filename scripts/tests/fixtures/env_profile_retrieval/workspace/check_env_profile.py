# SPDX-License-Identifier: AGPL-3.0-or-later
"""Visible checks for environment profile parsing."""

import unittest

from env_profile import parse_profile


class EnvProfileChecks(unittest.TestCase):
    def test_ordinary_rows(self):
        self.assertEqual(
            parse_profile("MODE=dev\nPORT=8080\n"), {"MODE": "dev", "PORT": "8080"}
        )

    def test_values_containing_equals(self):
        self.assertEqual(parse_profile("TOKEN=part=more==\n"), {"TOKEN": "part=more=="})

    def test_blank_lines_comments_and_whitespace(self):
        self.assertEqual(
            parse_profile(
                "\n# profile\n  # comment\n  HOST = local  \n\t\nPORT = 9000\n"
            ),
            {"HOST": "local", "PORT": "9000"},
        )

    def test_empty_profile_and_value(self):
        self.assertEqual(parse_profile(""), {})
        self.assertEqual(parse_profile("LABEL=\n"), {"LABEL": ""})


if __name__ == "__main__":
    unittest.main(verbosity=2)
