# SPDX-License-Identifier: AGPL-3.0-or-later
"""Course score averages."""


def average(scores: list[int]) -> float:
    return sum(scores) / len(scores)
