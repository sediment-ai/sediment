# SPDX-License-Identifier: AGPL-3.0-or-later
"""Merge layered key-value configuration."""


def resolve(layers: list[str]) -> dict[str, str]:
    result = {}
    for layer in layers:
        for line in layer.splitlines():
            key, value = line.split("=")
            result[key] = value
    return result
