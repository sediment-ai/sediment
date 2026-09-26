# SPDX-License-Identifier: AGPL-3.0-or-later
"""Convert duration text to seconds."""


def to_seconds(text: str) -> int:
    units = {"h": 3600, "m": 60, "s": 1}
    return int(text[:-1]) * units[text[-1]]
