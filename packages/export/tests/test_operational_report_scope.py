# SPDX-License-Identifier: AGPL-3.0-or-later
"""Canonical bounds for synchronous operational reports reports."""

from datetime import UTC, datetime, timedelta

import pytest

from sediment_export import OperationalReportScope


def test_operational_scope_resolves_trailing_days_to_explicit_bounds() -> None:
    as_of = datetime(2026, 9, 6, 12, tzinfo=UTC)

    scope = OperationalReportScope.trailing_days(30, as_of=as_of)

    assert scope.cohort_start == as_of - timedelta(days=30)
    assert scope.cohort_end == as_of
    assert scope.as_of == as_of
    assert scope.max_inference_calls == 50_000


@pytest.mark.parametrize(
    ("start", "end", "as_of", "message"),
    [
        (
            datetime(2026, 8, 1),
            datetime(2026, 9, 1, tzinfo=UTC),
            datetime(2026, 9, 7, tzinfo=UTC),
            "timezone-aware",
        ),
        (
            datetime(2026, 9, 1, tzinfo=UTC),
            datetime(2026, 9, 1, tzinfo=UTC),
            datetime(2026, 9, 7, tzinfo=UTC),
            "cohort_start must precede cohort_end",
        ),
        (
            datetime(2026, 8, 1, tzinfo=UTC),
            datetime(2026, 9, 1, tzinfo=UTC),
            datetime(2026, 8, 31, tzinfo=UTC),
            "cohort_end must not follow as_of",
        ),
    ],
)
def test_operational_scope_rejects_invalid_boundaries(
    start: datetime, end: datetime, as_of: datetime, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        OperationalReportScope(start, end, as_of)


def test_operational_scope_rejects_non_positive_cohort_cap() -> None:
    with pytest.raises(ValueError, match="max_inference_calls must be positive"):
        OperationalReportScope(
            datetime(2026, 8, 1, tzinfo=UTC),
            datetime(2026, 9, 1, tzinfo=UTC),
            datetime(2026, 9, 7, tzinfo=UTC),
            max_inference_calls=0,
        )
