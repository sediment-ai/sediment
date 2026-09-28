# SPDX-License-Identifier: AGPL-3.0-or-later
"""Visible checks for shipping weights."""

from shipping import total_grams

CASES = (
    (
        "quantity multiplies",
        [{"sku": "A", "grams": 100, "quantity": 3}],
        300,
    ),
    (
        "several items and extra keys",
        [
            {"sku": "A", "grams": 10, "quantity": 1, "title": "x"},
            {"sku": "B", "grams": 5, "quantity": 4},
        ],
        30,
    ),
    ("zero quantity", [{"sku": "A", "grams": 50, "quantity": 0}], 0),
    ("empty order", [], 0),
)

failures = 0
for name, argument, expected in CASES:
    try:
        actual = total_grams(argument)
    except Exception as exc:
        actual = type(exc).__name__
    if actual == expected:
        print("ok:", name)
    else:
        failures += 1
        print(f"FAIL: {name}: expected {expected!r}, got {actual!r}")
raise SystemExit(1 if failures else 0)
