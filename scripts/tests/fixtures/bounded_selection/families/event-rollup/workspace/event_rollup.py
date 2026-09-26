# SPDX-License-Identifier: AGPL-3.0-or-later
"""Count JSON Lines events by action."""

import json


def action_counts(text: str) -> dict[str, int]:
    counts = {}
    for line in text.split("\n"):
        event = json.loads(line)
        action = event["action"]
        counts[action] = counts.get(action, 0) + 1
    return counts
