# SPDX-License-Identifier: AGPL-3.0-or-later
"""Username validation."""


def valid(name: str) -> bool:
    return 3 <= len(name) <= 16
