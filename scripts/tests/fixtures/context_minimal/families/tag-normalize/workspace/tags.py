# SPDX-License-Identifier: AGPL-3.0-or-later
"""Normalize catalog tags."""


def normalize(tags: list[str]) -> list[str]:
    return [tag.lower() for tag in tags]
