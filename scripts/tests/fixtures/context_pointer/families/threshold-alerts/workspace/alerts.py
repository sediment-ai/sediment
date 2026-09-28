# SPDX-License-Identifier: AGPL-3.0-or-later
"""Threshold alerts for sensor readings."""


def alerts(readings: list, limit: float) -> list[int]:
    return [i for i, r in enumerate(readings) if r >= limit + 1]
