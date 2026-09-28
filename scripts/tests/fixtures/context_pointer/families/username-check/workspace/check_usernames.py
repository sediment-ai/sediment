# SPDX-License-Identifier: AGPL-3.0-or-later
"""Visible checks for username validation."""

from usernames import valid

CASES = (
    ("ordinary name", "maria_k", True),
    ("uppercase", "Maria", False),
    ("starts with digit", "9lives", False),
    ("too short", "ab", False),
)

failures = 0
for name, argument, expected in CASES:
    try:
        actual = valid(argument)
    except Exception as exc:
        actual = type(exc).__name__
    if actual == expected:
        print("ok:", name)
    else:
        failures += 1
        print(f"FAIL: {name}: expected {expected!r}, got {actual!r}")
raise SystemExit(1 if failures else 0)
