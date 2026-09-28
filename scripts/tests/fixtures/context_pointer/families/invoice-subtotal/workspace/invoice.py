# SPDX-License-Identifier: AGPL-3.0-or-later
"""Invoice subtotals."""


def subtotal(lines: list[dict]) -> int:
    return sum(line["unit_cents"] for line in lines)
