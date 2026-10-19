# SPDX-License-Identifier: AGPL-3.0-or-later
"""Compute report: does a larger Compute allocation shorten Sessions?

The report groups Session compute timings by final allocation (its *arm*:
who chose it, plus its CPU and memory limits) and compares the arms' median
durations. Two durations matter:

- ``elapsed_ms`` — the Session's agent work from its first Inference call to
  its last. This is the completion time a person waits for.
- ``between_calls_ms`` — elapsed time with no Inference call in flight. CPU
  and memory can change this part; they can't change model latency.

**Comparisons stay inside one allocation source.** A ``randomized`` arm pair
supports a causal reading, because chance chose which Sessions received more
compute, as long as each randomized Session keeps a ``randomized`` final
allocation; ``randomized_reassigned_sessions`` counts the ones that didn't.
Every other pair is observational: an agent that asks for more compute is
often working on a harder task, so ``agent_request`` arms can run slower
*because* of the request's cause. The report never compares across
sources, and it marks each comparison ``randomized`` or not rather than
presenting an observational gap as an effect.

Each comparison is ``median(arm_b) - median(arm_a)`` in arm order (CPU, then
memory, absent first), with a fixed-seed bootstrap interval; a negative
difference means arm B finished faster. Session compute timing is a
diagnostic: no Evidence recipe or training row consumes it.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import combinations

from pydantic import TypeAdapter
from sediment_core import (
    COMPOSITE_FILTER_KEY_LIMIT,
    ComputeAllocationSource,
    FactStore,
)
from sediment_derive import (
    ComputeArm,
    ComputeTimingResult,
    Provenance,
    SessionComputeTiming,
    compute_arm_sort_key,
    derive_compute_timing_result,
)

from .jsonl import ExportRow
from .significance import (
    DEFAULT_BOOTSTRAP_ITERATIONS,
    DEFAULT_BOOTSTRAP_SEED,
    MedianDifference,
    _percentile,
    bootstrap_median_difference,
)


@dataclass(frozen=True)
class DurationDistribution:
    """Order statistics of one duration across an arm's Sessions."""

    count: int
    median: float | None
    p25: float | None
    p75: float | None


@dataclass(frozen=True)
class ComputeArmSummary:
    """One final-allocation arm and its Session durations."""

    arm: ComputeArm
    sessions: int
    allocation_changed_sessions: int
    elapsed_ms: DurationDistribution
    model_ms: DurationDistribution
    between_calls_ms: DurationDistribution


@dataclass(frozen=True)
class ComputeArmComparison:
    """``arm_b`` minus ``arm_a`` within one allocation source."""

    allocation_source: ComputeAllocationSource
    randomized: bool
    arm_a: ComputeArm
    arm_b: ComputeArm
    elapsed_ms: MedianDifference
    between_calls_ms: MedianDifference


@dataclass(frozen=True)
class ComputeReport:
    """Arms, within-source comparisons, and the Sessions the report skipped.

    ``sessions_with_allocation`` counts every Session that has a visible
    Compute allocation: ``timed_sessions`` plus the sum of ``skipped``.
    ``randomized_reassigned_sessions`` counts timed Sessions with a
    ``randomized`` allocation whose final allocation came from another
    source. Those Sessions leave the randomized arms, and a hard task that
    asks again is the likely cause, so a nonzero count weakens the causal
    reading of every randomized comparison."""

    sessions_with_allocation: int
    timed_sessions: int
    skipped: dict[str, int]
    randomized_reassigned_sessions: int
    arms: list[ComputeArmSummary]
    comparisons: list[ComputeArmComparison]
    provenance: Provenance


@dataclass(frozen=True)
class ComputeReportResult:
    report: ComputeReport
    timings: list[SessionComputeTiming]


_REPORT_JSON = TypeAdapter(ComputeReport)
_TIMING_JSON = TypeAdapter(SessionComputeTiming)


def _distribution(values: Sequence[int]) -> DurationDistribution:
    if not values:
        return DurationDistribution(0, None, None, None)
    ordered = sorted(float(value) for value in values)
    return DurationDistribution(
        count=len(ordered),
        median=statistics.median(ordered),
        p25=_percentile(ordered, 0.25),
        p75=_percentile(ordered, 0.75),
    )


def build_compute_report(
    result: ComputeTimingResult,
    *,
    confidence: float = 0.95,
    bootstrap_iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
) -> ComputeReport:
    """Summarize arms and compare every arm pair inside one allocation source."""

    by_arm: defaultdict[ComputeArm, list[SessionComputeTiming]] = defaultdict(list)
    for timing in result.timings:
        by_arm[timing.final_allocation].append(timing)
    arms = sorted(by_arm, key=compute_arm_sort_key)
    summaries = [
        ComputeArmSummary(
            arm=arm,
            sessions=len(by_arm[arm]),
            allocation_changed_sessions=sum(
                timing.allocation_changed for timing in by_arm[arm]
            ),
            elapsed_ms=_distribution([timing.elapsed_ms for timing in by_arm[arm]]),
            model_ms=_distribution([timing.model_ms for timing in by_arm[arm]]),
            between_calls_ms=_distribution(
                [timing.between_calls_ms for timing in by_arm[arm]]
            ),
        )
        for arm in arms
    ]

    def difference(arm_a, arm_b, metric: str) -> MedianDifference:
        return bootstrap_median_difference(
            [getattr(timing, metric) for timing in by_arm[arm_a]],
            [getattr(timing, metric) for timing in by_arm[arm_b]],
            confidence=confidence,
            bootstrap_iterations=bootstrap_iterations,
            seed=seed,
        )

    comparisons = [
        ComputeArmComparison(
            allocation_source=arm_a.allocation_source,
            randomized=arm_a.allocation_source is ComputeAllocationSource.RANDOMIZED,
            arm_a=arm_a,
            arm_b=arm_b,
            elapsed_ms=difference(arm_a, arm_b, "elapsed_ms"),
            between_calls_ms=difference(arm_a, arm_b, "between_calls_ms"),
        )
        for arm_a, arm_b in combinations(arms, 2)
        if arm_a.allocation_source is arm_b.allocation_source
    ]
    skipped = dict(sorted(result.skipped.items()))
    return ComputeReport(
        sessions_with_allocation=len(result.timings) + sum(skipped.values()),
        timed_sessions=len(result.timings),
        skipped=skipped,
        randomized_reassigned_sessions=sum(
            timing.final_allocation.allocation_source
            is not ComputeAllocationSource.RANDOMIZED
            and any(
                allocation.allocation_source is ComputeAllocationSource.RANDOMIZED
                for allocation in timing.allocations
            )
            for timing in result.timings
        ),
        arms=summaries,
        comparisons=comparisons,
        provenance=result.provenance,
    )


def generate_compute_report_result(
    store: FactStore, org_id: str, **report_options
) -> ComputeReportResult:
    """Read one snapshot, derive Session compute timings, and build the report.

    Only Sessions with a visible Compute allocation are read; their complete
    visible Inference call populations come from the same snapshot."""

    # ponytail: all-history read of allocations and call summaries (no message
    # content); add a cohort window like `report model --since-days` if one
    # organization's allocated Sessions outgrow memory.
    with store.read_snapshot() as snapshot:
        quarantine_revision = snapshot.quarantine_revision(org_id)
        allocations = snapshot.read_compute_allocations(org_id)
        session_ids = sorted({allocation.session_id for allocation in allocations})
        calls = []
        for start in range(0, len(session_ids), COMPOSITE_FILTER_KEY_LIMIT):
            calls.extend(
                snapshot.read_inference_call_summaries(
                    org_id,
                    session_ids=set(
                        session_ids[start : start + COMPOSITE_FILTER_KEY_LIMIT]
                    ),
                )
            )
    result = derive_compute_timing_result(
        allocations, calls, quarantine_revision=quarantine_revision
    )
    return ComputeReportResult(
        report=build_compute_report(result, **report_options),
        timings=result.timings,
    )


def compute_report_to_json(report: ComputeReport) -> dict:
    """Plain JSON values for ``--json`` output."""
    return _REPORT_JSON.dump_python(report, mode="json")


def compute_timings_to_export_rows(
    timings: Iterable[SessionComputeTiming],
) -> list[ExportRow]:
    """Adapt Session compute timings to the shared JSONL writer."""
    return [
        ExportRow(split="train", body=_TIMING_JSON.dump_python(timing, mode="json"))
        for timing in timings
    ]
