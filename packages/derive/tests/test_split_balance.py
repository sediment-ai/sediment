# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Statistical balance checks for the deterministic train/eval split.

The split itself is pure and seedless. The synthetic input population here is
also deterministic: UUID-shaped session ids generated from uuid5 over fixed
names, so the statistical diagnostic is repeatable rather than flaky.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from math import sqrt
from uuid import NAMESPACE_URL, uuid5

import pytest
from sediment_derive import split_of

LARGE_N = 10_000
FRACTIONS = (0.1, 0.2, 0.5)
MAX_STANDARD_ERRORS = 5.0

LOGGER = logging.getLogger(__name__)

SYNTHETIC_SESSION_IDS = tuple(
    str(uuid5(NAMESPACE_URL, f"sediment-split-balance/{index}"))
    for index in range(LARGE_N)
)


def _eval_count(session_ids: Sequence[str], fraction: float) -> int:
    return sum(split_of(session_id, fraction) == "eval" for session_id in session_ids)


def _standard_error(fraction: float, sample_size: int) -> float:
    return sqrt(fraction * (1.0 - fraction) / sample_size)


def _cohort_ranges(sample_size: int, fraction: float) -> tuple[int, int, float]:
    counts = [
        _eval_count(SYNTHETIC_SESSION_IDS[start : start + sample_size], fraction)
        for start in range(0, LARGE_N - sample_size + 1, sample_size)
    ]
    mean_ratio = sum(counts) / (len(counts) * sample_size)
    return min(counts), max(counts), mean_ratio


def test_synthetic_population_has_distinct_uuid_shaped_session_ids() -> None:
    assert len(SYNTHETIC_SESSION_IDS) == LARGE_N
    assert len(set(SYNTHETIC_SESSION_IDS)) == LARGE_N
    assert all(len(session_id) == 36 for session_id in SYNTHETIC_SESSION_IDS)


@pytest.mark.parametrize("fraction", FRACTIONS)
def test_large_population_eval_share_is_within_binomial_error(
    fraction: float,
) -> None:
    eval_count = _eval_count(SYNTHETIC_SESSION_IDS, fraction)
    empirical_ratio = eval_count / LARGE_N
    standard_errors = abs(empirical_ratio - fraction) / _standard_error(
        fraction, LARGE_N
    )

    assert standard_errors <= MAX_STANDARD_ERRORS, (
        f"{eval_count=}, {empirical_ratio=:.4f}, {fraction=:.1f}, "
        f"{standard_errors=:.2f}"
    )


@pytest.mark.parametrize("sample_size", (20, 50, 100))
@pytest.mark.parametrize("fraction", FRACTIONS)
def test_small_population_eval_share_variance_is_characterized(
    sample_size: int, fraction: float
) -> None:
    low, high, mean_ratio = _cohort_ranges(sample_size, fraction)
    standard_errors = abs(mean_ratio - fraction) / _standard_error(fraction, LARGE_N)

    LOGGER.info(
        "split_small_population_characterization",
        extra={
            "sample_size": sample_size,
            "eval_fraction": fraction,
            "min_eval_count": low,
            "max_eval_count": high,
            "min_eval_ratio": low / sample_size,
            "max_eval_ratio": high / sample_size,
        },
    )

    assert 0 <= low <= high <= sample_size
    assert standard_errors <= MAX_STANDARD_ERRORS
