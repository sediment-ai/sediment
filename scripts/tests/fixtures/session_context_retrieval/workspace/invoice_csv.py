# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read invoice CSV data and return a decimal total formatted to cents."""

from decimal import Decimal


def invoice_total(text: str) -> str:
    total = Decimal("0")
    for line in text.splitlines()[1:]:
        if not line.strip():
            continue
        description, amount = line.split(",")
        total += Decimal(amount)
    return format(total, ".2f")
