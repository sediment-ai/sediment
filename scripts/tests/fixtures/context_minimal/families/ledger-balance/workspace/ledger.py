# SPDX-License-Identifier: AGPL-3.0-or-later
"""Account balances from ledger rows."""


def balances(rows: list[dict]) -> dict[str, int]:
    result = {}
    for row in rows:
        result[row["account"]] = row["amount_cents"]
    return result
