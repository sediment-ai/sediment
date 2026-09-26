# SPDX-License-Identifier: AGPL-3.0-or-later
"""Visible checks for JSON Lines parsing and action counts."""

from event_rollup import action_counts

CASES = (
    (
        "ordinary actions",
        '{"id":"e1","action":"login"}\n{"id":"e2","action":"logout"}\n'
        '{"id":"e3","action":"login"}\n',
        {"login": 2, "logout": 1},
    ),
    (
        "blank lines and whitespace",
        '\n \t\n{"id":"e1","action":" login "}\n\n{"id":"e2","action":"login"}',
        {"login": 2},
    ),
    (
        "CRLF, extra fields, and case",
        '{"id":"e1","action":"export","user":"u1"}\r\n{"id":"e2","action":"Export"}\r\n',
        {"export": 1, "Export": 1},
    ),
    ("empty input", "", {}),
)

failures = 0
for name, argument, expected in CASES:
    try:
        actual = action_counts(argument)
    except Exception as exc:
        actual = type(exc).__name__
    if actual == expected:
        print("ok:", name)
    else:
        failures += 1
        print(f"FAIL: {name}: expected {expected!r}, got {actual!r}")
raise SystemExit(1 if failures else 0)
