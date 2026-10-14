# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deny force pushes, hard resets, and pushes to main from an agent shell."""

from __future__ import annotations

import json
import re
import sys

_GIT = r"(?:^|[;&|(]\s*)git\s+(?:-C\s+\S+\s+)?"
_RULES = (
    (
        re.compile(_GIT + r"push\b[^;&|\n]*?\s(?:--force(?:-with-lease)?|-f)\b"),
        "Force pushes are blocked.",
    ),
    (re.compile(_GIT + r"reset\s+[^;&|\n]*--hard\b"), "Hard resets are blocked."),
    (
        re.compile(_GIT + r"push\b[^;&|\n]*?\s(?:\S+:)?(?:refs/heads/)?main(?:\s|$)"),
        "Pushes to main are blocked; merging to main needs a maintainer.",
    ),
)


def main() -> None:
    command = json.load(sys.stdin).get("command") or ""
    for pattern, reason in _RULES:
        if pattern.search(command):
            print(
                json.dumps(
                    {
                        "permission": "deny",
                        "user_message": f"Repository hook: {reason}",
                        "agent_message": (
                            f"{reason} Commit to your own branch, push it, and "
                            "open a draft pull request against the demo branch."
                        ),
                    }
                )
            )
            return
    print(json.dumps({"permission": "allow"}))


if __name__ == "__main__":
    main()
