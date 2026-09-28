# SPDX-License-Identifier: AGPL-3.0-or-later
"""Visible checks for threshold alerts."""

from alerts import alerts

CASES = (
    ("above the limit", ([1, 5, 9], 4), [1, 2]),
    ("fractional readings", ([4.5, 2.2], 4), [0]),
    ("nothing above", ([1, 2], 10), []),
    ("empty readings", ([], 1), []),
)

failures = 0
for name, argument, expected in CASES:
    try:
        actual = alerts(*argument)
    except Exception as exc:
        actual = type(exc).__name__
    if actual == expected:
        print("ok:", name)
    else:
        failures += 1
        print(f"FAIL: {name}: expected {expected!r}, got {actual!r}")
raise SystemExit(1 if failures else 0)
