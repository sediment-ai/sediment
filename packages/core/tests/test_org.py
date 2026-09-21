# SPDX-License-Identifier: AGPL-3.0-or-later
"""org_id normalization: accept/reject table, case folding, idempotence."""

from __future__ import annotations

import pytest
from sediment_core import normalize_org_id

ACCEPT = [
    ("acme", "acme"),
    ("Acme", "acme"),  # case folds
    ("ACME-Corp.2", "acme-corp.2"),
    ("0org", "0org"),
    ("a", "a"),
    ("a" * 64, "a" * 64),  # max length
    ("a_b-c.d", "a_b-c.d"),
]

REJECT = [
    "",
    " ",
    "\t\n",
    " acme",  # leading whitespace is rejected, not stripped
    "acme ",
    "ac me",
    "a" * 65,  # over max length
    "-acme",  # separator can't lead
    ".acme",
    "_acme",
    "../acme",  # path traversal
    "a/b",
    "a\\b",
    "a%2Fb",  # percent-encoded slash
    "acmé",  # unicode
    "аcme",  # U+0430 CYRILLIC SMALL A lookalike
    "Kcelvin",  # U+212A KELVIN SIGN — str.lower() maps it to "k"
]


@pytest.mark.parametrize(("raw", "expected"), ACCEPT)
def test_accept(raw: str, expected: str) -> None:
    assert normalize_org_id(raw) == expected


@pytest.mark.parametrize("raw", REJECT)
def test_reject(raw: str) -> None:
    with pytest.raises(ValueError, match="org_id"):
        normalize_org_id(raw)


@pytest.mark.parametrize(("raw", "_expected"), ACCEPT)
def test_idempotent(raw: str, _expected: str) -> None:
    once = normalize_org_id(raw)
    assert normalize_org_id(once) == once
