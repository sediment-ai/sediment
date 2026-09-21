# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Jaccard token overlap similarity scorer — the attribution fallback.
No embeddings, no external dependencies — fast and auditable.
"""

from __future__ import annotations

import re


def tokenize(text: str) -> set[str]:
    """
    Split code into tokens for overlap scoring.
    Strips punctuation-only tokens, lowercases, removes empty strings.
    """
    tokens = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*|[0-9]+", text)
    return {t.lower() for t in tokens if t}


def jaccard_tokens(tokens_a: set[str], tokens_b: set[str]) -> float:
    """
    Jaccard similarity between two pre-tokenized sets.
    Returns 0.0–1.0. Returns 0.0 if both are empty.

    Takes token sets (not raw strings) so callers that score one text against
    many others can tokenize each side once instead of per comparison.
    """
    if not tokens_a and not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)
