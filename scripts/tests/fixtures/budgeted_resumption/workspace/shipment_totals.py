# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sum shipment units by destination."""


def totals_by_destination(text: str) -> dict[str, int]:
    totals = {}
    for line in text.splitlines():
        destination, units, channel = line.split(",")
        totals[destination] = totals.get(destination, 0) + int(units)
    return totals
