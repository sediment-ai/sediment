# SPDX-License-Identifier: AGPL-3.0-or-later
"""Visible checks for moving averages."""

from window import moving_average

CASES = (
    ("window of two", ([1, 2, 3], 2), [1.5, 2.5]),
    ("rounding", ([1, 1, 2], 3), [1.33]),
    ("too few readings", ([5], 2), []),
    ("window of one", ([4, 6], 1), [4.0, 6.0]),
)

failures = 0
for name, argument, expected in CASES:
    try:
        actual = moving_average(*argument)
    except Exception as exc:
        actual = type(exc).__name__
    if actual == expected:
        print("ok:", name)
    else:
        failures += 1
        print(f"FAIL: {name}: expected {expected!r}, got {actual!r}")
raise SystemExit(1 if failures else 0)
