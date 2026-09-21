# SPDX-License-Identifier: AGPL-3.0-or-later
"""Similarity scorer seam for attribution derivations.

Only the Jaccard scorer exists today. The seam is intentionally small so a
future scorer can be evaluated by the precision harness before it changes
attribution behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .similarity import jaccard_tokens


class Scorer(Protocol):
    """Scores pre-tokenized completion text against pre-tokenized diff text."""

    version: str

    def score(self, completion_tokens: set[str], diff_tokens: set[str]) -> float:
        """Return a 0.0-1.0 similarity score for two token sets."""


@dataclass(frozen=True)
class JaccardScorer:
    """The current token-overlap baseline, preserving ``jaccard_tokens``."""

    version: str = "jaccard-v1"

    def score(self, completion_tokens: set[str], diff_tokens: set[str]) -> float:
        """Return Jaccard similarity for two pre-tokenized sets."""
        return jaccard_tokens(completion_tokens, diff_tokens)
