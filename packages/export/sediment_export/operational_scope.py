# SPDX-License-Identifier: AGPL-3.0-or-later
"""Explicit, reproducible bounds for operational report cohorts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class OperationalReportScope:
    """Inference-call cohort bounds and the latest eligible evidence time."""

    cohort_start: datetime
    cohort_end: datetime
    as_of: datetime
    max_inference_calls: int = 50_000

    def __post_init__(self) -> None:
        if any(
            value.tzinfo is None
            for value in (self.cohort_start, self.cohort_end, self.as_of)
        ):
            raise ValueError("operational report bounds must be timezone-aware")
        if self.cohort_start >= self.cohort_end:
            raise ValueError("cohort_start must precede cohort_end")
        if self.cohort_end > self.as_of:
            raise ValueError("cohort_end must not follow as_of")
        if type(self.max_inference_calls) is not int or self.max_inference_calls <= 0:
            raise ValueError("max_inference_calls must be positive")

    @classmethod
    def trailing_days(
        cls,
        days: int,
        *,
        as_of: datetime,
        max_inference_calls: int = 50_000,
    ) -> OperationalReportScope:
        if type(days) is not int or days <= 0:
            raise ValueError("days must be a positive integer")
        return cls(
            cohort_start=as_of - timedelta(days=days),
            cohort_end=as_of,
            as_of=as_of,
            max_inference_calls=max_inference_calls,
        )
