# SPDX-License-Identifier: AGPL-3.0-or-later
"""Moving averages over sensor readings."""


def moving_average(readings: list, size: int) -> list[float]:
    return [sum(readings[i : i + size]) / size for i in range(len(readings))]
