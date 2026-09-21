# SPDX-License-Identifier: AGPL-3.0-or-later
"""Structured provenance shared by canonical derived artifacts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Provenance:
    """The policy and quarantine revision that produced a derived artifact."""

    policy_version: str
    quarantine_revision: int
    policy_digest: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.policy_version, str) or not self.policy_version.strip():
            raise ValueError("policy_version must be a non-empty string")
        if type(self.quarantine_revision) is not int or self.quarantine_revision < 0:
            raise ValueError("quarantine_revision must be a non-negative integer")
        if self.policy_digest is None:
            return
        if len(self.policy_digest) != 64 or any(
            character not in "0123456789abcdef" for character in self.policy_digest
        ):
            raise ValueError("policy_digest must be a lowercase SHA-256 digest")

    def __str__(self) -> str:
        fields = [
            f"policy_version={self.policy_version}",
            f"quarantine_revision={self.quarantine_revision}",
        ]
        if self.policy_digest is not None:
            fields.append(f"policy_digest={self.policy_digest}")
        return " ".join(fields)
