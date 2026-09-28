# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shipping weight for an order."""


def total_grams(items: list[dict]) -> int:
    return sum(item["grams"] for item in items)
