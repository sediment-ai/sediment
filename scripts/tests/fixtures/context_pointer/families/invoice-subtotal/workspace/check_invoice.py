# SPDX-License-Identifier: AGPL-3.0-or-later
"""Visible checks for invoice subtotals."""

from invoice import subtotal

CASES = (
    ("quantity multiplies", [{"sku": "A", "unit_cents": 150, "quantity": 3}], 450),
    (
        "several lines and extra keys",
        [
            {"sku": "A", "unit_cents": 10, "quantity": 1, "memo": "x"},
            {"sku": "B", "unit_cents": 5, "quantity": 4},
        ],
        30,
    ),
    ("zero quantity", [{"sku": "A", "unit_cents": 99, "quantity": 0}], 0),
    ("empty invoice", [], 0),
)

failures = 0
for name, argument, expected in CASES:
    try:
        actual = subtotal(argument)
    except Exception as exc:
        actual = type(exc).__name__
    if actual == expected:
        print("ok:", name)
    else:
        failures += 1
        print(f"FAIL: {name}: expected {expected!r}, got {actual!r}")
raise SystemExit(1 if failures else 0)
