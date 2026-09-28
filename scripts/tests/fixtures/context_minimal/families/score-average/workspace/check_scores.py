# SPDX-License-Identifier: AGPL-3.0-or-later
"""Visible checks for score averages."""

from scores import average

CASES = (
    ("two scores", [80, 91], 85.5),
    ("rounding", [1, 2], 1.5),
    ("single score", [70], 70.0),
    ("empty list", [], 0.0),
)

failures = 0
for name, argument, expected in CASES:
    try:
        actual = average(argument)
    except Exception as exc:
        actual = type(exc).__name__
    if actual == expected:
        print("ok:", name)
    else:
        failures += 1
        print(f"FAIL: {name}: expected {expected!r}, got {actual!r}")
raise SystemExit(1 if failures else 0)
