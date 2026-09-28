# SPDX-License-Identifier: AGPL-3.0-or-later
"""Visible checks for tag normalization."""

from tags import normalize

CASES = (
    ("case and duplicates", ["Red", "red", "Blue"], ["red", "blue"]),
    ("whitespace runs", ["  dark   blue ", "dark blue"], ["dark-blue"]),
    ("empty tags", ["", "   ", "x"], ["x"]),
    ("empty list", [], []),
)

failures = 0
for name, argument, expected in CASES:
    try:
        actual = normalize(argument)
    except Exception as exc:
        actual = type(exc).__name__
    if actual == expected:
        print("ok:", name)
    else:
        failures += 1
        print(f"FAIL: {name}: expected {expected!r}, got {actual!r}")
raise SystemExit(1 if failures else 0)
