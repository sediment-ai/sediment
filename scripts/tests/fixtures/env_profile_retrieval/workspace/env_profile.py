# SPDX-License-Identifier: AGPL-3.0-or-later
"""Parse environment profile text into a dictionary."""


def parse_profile(text: str) -> dict[str, str]:
    result = {}
    for line in text.splitlines():
        key, value = line.split("=")
        result[key] = value
    return result
