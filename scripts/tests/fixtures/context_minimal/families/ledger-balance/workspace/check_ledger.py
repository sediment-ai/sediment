# SPDX-License-Identifier: AGPL-3.0-or-later
"""Visible checks for account balances."""

from ledger import balances

CASES = (
    (
        "sums per account",
        [
            {"account": "ops", "amount_cents": 500, "status": "posted"},
            {"account": "ops", "amount_cents": -200, "status": "posted"},
            {"account": "tax", "amount_cents": 90, "status": "posted"},
        ],
        {"ops": 300, "tax": 90},
    ),
    (
        "whitespace and case",
        [
            {"account": " ops ", "amount_cents": 1, "status": "posted"},
            {"account": "Ops", "amount_cents": 2, "status": "posted"},
        ],
        {"ops": 1, "Ops": 2},
    ),
    (
        "zero balance and extra keys",
        [{"account": "r", "amount_cents": 0, "status": "posted", "memo": "x"}],
        {"r": 0},
    ),
    ("empty ledger", [], {}),
)

failures = 0
for name, argument, expected in CASES:
    try:
        actual = balances(argument)
    except Exception as exc:
        actual = type(exc).__name__
    if actual == expected:
        print("ok:", name)
    else:
        failures += 1
        print(f"FAIL: {name}: expected {expected!r}, got {actual!r}")
raise SystemExit(1 if failures else 0)
