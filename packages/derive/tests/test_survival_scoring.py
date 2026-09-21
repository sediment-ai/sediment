# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for standalone survival scoring helpers."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from sediment_capture import parse_otlp_decisions
from sediment_derive.survival_scoring import (
    four_gram_containment,
    four_gram_survival,
)


FIXTURES = Path(__file__).parents[2] / "capture" / "tests" / "fixtures" / "otlp"


def _fixture(agent: str, name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / agent / name).read_text())


def test_four_gram_identical_is_one() -> None:
    text = "def add(a, b):\n    return a + b\n"
    assert four_gram_survival(text, text) == 1.0


def test_four_gram_disjoint_is_zero() -> None:
    assert four_gram_survival("abcdef", "UVWXYZ") == 0.0


def test_four_gram_matches_copilot_helper_case() -> None:
    # Copilot's own EditSurvivalTracker test has "'hello'" -> "'Hello'"
    # scoring 0.5. This pins the raw character 4-gram multiset formula.
    # four_gram_survival is symmetric and deliberately not used for
    # Edit-shaped (snippet-vs-whole-file) pairs -- this test is scoped to
    # Copilot fidelity only, not a claim it's the right scorer for
    # EditObservation.
    assert four_gram_survival("'hello'", "'Hello'") == 0.5


def test_four_gram_matches_copilot_fixture_consumed_value() -> None:
    # The local Copilot OTLP fixture does not include original/final text, only
    # Copilot's already-computed survival_rate_four_gram=1. Match that
    # consumed value with an equivalent unchanged edit pair.
    [decision] = parse_otlp_decisions(
        _fixture("copilot", "edit_survival.json"), org_id="acme-corp"
    )
    original = "return render_report(rows)\n"
    assert decision.edit_retention_score == 1.0
    assert four_gram_survival(original, original) == decision.edit_retention_score


def test_empty_strings_are_full_survival() -> None:
    assert four_gram_survival("", "") == 1.0
    assert four_gram_containment("", "") == 1.0


def test_four_gram_survival_partial_score_stays_in_range() -> None:
    score = four_gram_survival("return user.email.lower()", "return email.lower()")
    assert 0.0 < score < 1.0
    assert math.isfinite(score)


# ── four_gram_containment: the recommended scorer for EditObservation ──────────


def test_containment_identical_is_one() -> None:
    text = "def add(a, b):\n    return a + b\n"
    assert four_gram_containment(text, text) == 1.0


def test_containment_disjoint_is_zero() -> None:
    assert four_gram_containment("abcdef", "UVWXYZ") == 0.0


def test_containment_original_empty_is_full_survival() -> None:
    # An empty Write is legal on the wire -- nothing to lose, so 1.0.
    assert four_gram_containment("", "anything at all") == 1.0


def test_containment_final_empty_is_zero_survival() -> None:
    # The file was deleted before session end -- a real zero-survival
    # observation, not a gap, unless original was also empty.
    assert four_gram_containment("some surviving text", "") == 0.0


def test_containment_short_strings_fall_back_to_substring_check() -> None:
    # Fewer than 4 characters can't form a single 4-gram; containment falls
    # back to an exact substring check instead of always returning 0.0.
    assert four_gram_containment("abc", "xxabcxx") == 1.0
    assert four_gram_containment("abc", "xxxxxxx") == 0.0


def test_snippet_surviving_intact_in_a_large_file_scores_near_one() -> None:
    # The regression this test exists to pin: the original recommendation
    # (four_gram_survival, symmetric) and the since-dropped
    # edit_distance_survival (also symmetric) both scored a 100%-intact
    # surviving snippet near zero once embedded in a much larger file --
    # the dominant real shape for Claude Code's Edit tool (`original` is
    # `new_string`, a snippet; `final` is the whole file at session end).
    snippet = "def compute_total(a, b):\n    return a + b\n" * 3
    big_file = "x" * 30_000 + snippet + "y" * 5_000

    containment_score = four_gram_containment(snippet, big_file)
    symmetric_score = four_gram_survival(snippet, big_file)

    assert containment_score > 0.99
    # The old, wrong answer for this exact case -- kept as an explicit
    # contrast so a future change can't quietly make this assertion vacuous.
    assert symmetric_score < 0.05


def test_partially_rewritten_snippet_in_a_large_file_scores_proportionally() -> None:
    original_snippet = "def compute_total(a, b):\n    return a + b\n"
    # Roughly the first half of the snippet's characters survive verbatim
    # (contiguous, so its interior 4-grams still match); the rest is
    # rewritten. Hand-verified: scores 0.846, clearly below the full-survival
    # ~1.0 case and clearly above the fully-deleted ~0.0 case, not pinned to
    # either extreme.
    surviving_half = "def compute_total(a, b):\n"
    rewritten_half = "    return subtract(a, b)\n"
    big_file = "x" * 30_000 + surviving_half + rewritten_half + "y" * 5_000

    score = four_gram_containment(original_snippet, big_file)
    assert 0.7 < score < 0.95


def test_snippet_fully_deleted_from_a_still_large_file_scores_near_zero() -> None:
    snippet = "def compute_total(a, b):\n    return a + b\n"
    big_file_without_snippet = "x" * 30_000 + "y" * 5_000
    assert four_gram_containment(snippet, big_file_without_snippet) < 0.05


def test_containment_final_has_extra_copies_does_not_overcredit() -> None:
    # A gram appearing more times in `final` than in `original` must not
    # inflate the score past what `original` actually contributed.
    original = "abcdabcd"  # gram "abcd" appears twice
    final = "abcdabcdabcdabcdabcd"  # same gram appears five times
    assert four_gram_containment(original, final) == 1.0
