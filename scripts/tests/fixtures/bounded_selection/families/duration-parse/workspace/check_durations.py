# SPDX-License-Identifier: AGPL-3.0-or-later
"""Visible checks for duration conversion."""

from durations import to_seconds

CASES = (
    ("hours", "2h", 7200),
    ("minutes", "5m", 300),
    ("seconds", "9s", 9),
    ("combined units and whitespace", " 1h30m ", 5400),
    ("spaces between groups", "1h 2m 3s", 3723),
)

failures = 0
for name, argument, expected in CASES:
    try:
        actual = to_seconds(argument)
    except Exception as exc:
        actual = type(exc).__name__
    if actual == expected:
        print("ok:", name)
    else:
        failures += 1
        print(f"FAIL: {name}: expected {expected!r}, got {actual!r}")
raise SystemExit(1 if failures else 0)
