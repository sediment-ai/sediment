# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the Jaccard similarity scorer."""

from __future__ import annotations

from sediment_derive.similarity import jaccard_tokens, tokenize


def _jaccard(a: str, b: str) -> float:
    return jaccard_tokens(tokenize(a), tokenize(b))


def test_tokenize_splits_code_into_tokens() -> None:
    tokens = tokenize("def add(a, b): return a + b")
    assert tokens == {"def", "add", "a", "b", "return"}


def test_tokenize_lowercases_and_drops_punctuation() -> None:
    assert tokenize("Foo() + BAR;") == {"foo", "bar"}


def test_jaccard_identical_is_one() -> None:
    code = "def add(a, b):\n    return a + b"
    assert _jaccard(code, code) == 1.0


def test_jaccard_disjoint_is_zero() -> None:
    assert _jaccard("alpha beta gamma", "delta epsilon zeta") == 0.0


def test_jaccard_both_empty_is_zero() -> None:
    assert _jaccard("", "") == 0.0


def test_jaccard_partial_overlap_in_range() -> None:
    # Shared tokens {def, return} but otherwise different identifiers, so the
    # score lands strictly between 0 and 1. (Operator-only differences like
    # `+` vs `-` are stripped by the tokenizer and would score 1.0.)
    score = _jaccard(
        "def add(a, b): return a + b",
        "def subtract(x, y): return x - y",
    )
    assert 0.0 < score < 1.0
