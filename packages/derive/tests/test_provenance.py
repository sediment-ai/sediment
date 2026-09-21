# SPDX-License-Identifier: AGPL-3.0-or-later
"""Validation contract of the structured ``Provenance`` value object."""

from __future__ import annotations

import pytest
from sediment_derive import Provenance

DIGEST = "a" * 64


def test_provenance_renders_with_and_without_digest() -> None:
    bare = Provenance(policy_version="1", quarantine_revision=0)
    assert str(bare) == "policy_version=1 quarantine_revision=0"
    stamped = Provenance(
        policy_version="2", quarantine_revision=3, policy_digest=DIGEST
    )
    assert str(stamped) == (
        f"policy_version=2 quarantine_revision=3 policy_digest={DIGEST}"
    )


@pytest.mark.parametrize("policy_version", ["", "   ", 1, None])
def test_provenance_rejects_non_string_or_blank_policy_version(
    policy_version: object,
) -> None:
    with pytest.raises(ValueError, match="policy_version"):
        Provenance(policy_version=policy_version, quarantine_revision=0)


@pytest.mark.parametrize("revision", [-1, True, 1.0, "0", None])
def test_provenance_rejects_non_integer_quarantine_revision(
    revision: object,
) -> None:
    with pytest.raises(ValueError, match="quarantine_revision"):
        Provenance(policy_version="1", quarantine_revision=revision)


@pytest.mark.parametrize(
    "digest",
    ["", "a" * 63, "a" * 65, "A" * 64, "z" * 64],
)
def test_provenance_rejects_malformed_policy_digest(digest: str) -> None:
    with pytest.raises(ValueError, match="policy_digest"):
        Provenance(policy_version="1", quarantine_revision=0, policy_digest=digest)
