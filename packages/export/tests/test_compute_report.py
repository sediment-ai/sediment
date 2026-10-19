# SPDX-License-Identifier: AGPL-3.0-or-later
"""Compute report: median-difference statistic, arms, and comparisons."""

from __future__ import annotations

import json
import random
import statistics
from datetime import UTC, datetime, timedelta

import pytest
from sediment_core import (
    ComputeAllocation,
    ComputeAllocationSource,
    FactTable,
    GatewayProvider,
    InferenceCall,
)
from sediment_derive import ComputeArm, derive_compute_timing_result
from sediment_export import (
    bootstrap_median_difference,
    build_compute_report,
    compute_report_to_json,
    compute_timings_to_export_rows,
    generate_compute_report_result,
)

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
RANDOMIZED = ComputeAllocationSource.RANDOMIZED
REQUESTED = ComputeAllocationSource.AGENT_REQUEST


def test_median_difference_reports_known_medians_and_a_covering_interval() -> None:
    slow = [100, 110, 120, 130, 140, 150, 160]
    fast = [60, 70, 80, 90, 100]
    result = bootstrap_median_difference(slow, fast, bootstrap_iterations=500)
    assert (result.median_a, result.median_b, result.difference) == (130, 80, -50)
    assert result.ci_low <= result.difference <= result.ci_high
    assert result.ci_high < 0
    assert (result.n_a, result.n_b, result.small_n) == (7, 5, True)
    assert result.insufficient_data is False


def test_median_difference_is_order_invariant_and_seeded() -> None:
    rng = random.Random(7)
    a = [rng.randint(1, 10_000) for _ in range(60)]
    b = [rng.randint(1, 8_000) for _ in range(45)]
    expected = bootstrap_median_difference(a, b)
    shuffled_a, shuffled_b = a[:], b[:]
    rng.shuffle(shuffled_a)
    rng.shuffle(shuffled_b)
    assert bootstrap_median_difference(shuffled_a, shuffled_b) == expected
    assert bootstrap_median_difference(a, b, seed=5) != expected
    assert expected.small_n is False


def test_median_difference_flags_insufficient_data_and_rejects_bad_input() -> None:
    result = bootstrap_median_difference([5], [1, 2, 3])
    assert result.insufficient_data is True
    assert (result.median_a, result.median_b) == (5, 2)
    assert (result.difference, result.ci_low, result.ci_high) == (None, None, None)
    assert bootstrap_median_difference([], []).median_a is None
    with pytest.raises(ValueError, match="finite"):
        bootstrap_median_difference([1, float("nan")], [1, 2])
    with pytest.raises(ValueError, match="confidence"):
        bootstrap_median_difference([1, 2], [1, 2], confidence=1.0)
    with pytest.raises(ValueError, match="iterations"):
        bootstrap_median_difference([1, 2], [1, 2], bootstrap_iterations=1)


def _session(
    session_id: str,
    *,
    source: ComputeAllocationSource,
    cpu: int,
    work_s: int,
    model_s: int = 10,
    resized: bool = False,
) -> tuple[list[ComputeAllocation], list[InferenceCall]]:
    """One Session: a model call, a tool gap of ``work_s``, a final call."""
    allocations = [
        ComputeAllocation(
            allocation_id=f"{session_id}-alloc",
            org_id="acme",
            session_id=session_id,
            allocation_source=source,
            cpu_millicores=cpu,
            memory_bytes=8 * 2**30,
            occurred_at=T0 - timedelta(minutes=1),
        )
    ]
    if resized:
        allocations.insert(
            0,
            ComputeAllocation(
                allocation_id=f"{session_id}-start",
                org_id="acme",
                session_id=session_id,
                allocation_source=ComputeAllocationSource.DEFAULT,
                cpu_millicores=1000,
                memory_bytes=8 * 2**30,
                occurred_at=T0 - timedelta(minutes=2),
            ),
        )
    calls = [
        InferenceCall(
            inference_call_id=f"{session_id}-{index}",
            org_id="acme",
            session_id=session_id,
            gateway_provider=GatewayProvider.LITELLM,
            input_messages=[],
            output_messages=[],
            duration_ms=model_s * 1000,
            observed_at=T0 + timedelta(seconds=end),
        )
        for index, end in enumerate((model_s, 2 * model_s + work_s))
    ]
    return allocations, calls


def _population():
    allocations: list[ComputeAllocation] = []
    calls: list[InferenceCall] = []
    for index in range(12):
        for session in (
            _session(f"small-{index}", source=RANDOMIZED, cpu=2000, work_s=60 + index),
            _session(f"large-{index}", source=RANDOMIZED, cpu=8000, work_s=20 + index),
            _session(
                f"asked-{index}",
                source=REQUESTED,
                cpu=8000,
                work_s=90 + index,
                resized=index % 3 == 0,
            ),
        ):
            allocations.extend(session[0])
            calls.extend(session[1])
    return allocations, calls


def test_report_compares_arms_only_within_one_allocation_source() -> None:
    allocations, calls = _population()
    report = build_compute_report(
        derive_compute_timing_result(allocations, calls, quarantine_revision=4),
        bootstrap_iterations=300,
    )

    assert (report.sessions_with_allocation, report.timed_sessions) == (36, 36)
    assert [summary.arm for summary in report.arms] == [
        ComputeArm(REQUESTED, 8000, 8 * 2**30),
        ComputeArm(RANDOMIZED, 2000, 8 * 2**30),
        ComputeArm(RANDOMIZED, 8000, 8 * 2**30),
    ]
    requested = report.arms[0]
    assert requested.sessions == 12
    assert requested.allocation_changed_sessions == 4
    assert requested.model_ms.median == 20_000
    assert requested.between_calls_ms.median == statistics.median(
        [(90 + index) * 1000 for index in range(12)]
    )
    # The agent_request arm is never compared with a randomized arm.
    [comparison] = report.comparisons
    assert comparison.randomized is True
    assert comparison.allocation_source is RANDOMIZED
    assert (comparison.arm_a.cpu_millicores, comparison.arm_b.cpu_millicores) == (
        2000,
        8000,
    )
    assert comparison.between_calls_ms.difference == -40_000
    assert comparison.elapsed_ms.difference == -40_000
    assert comparison.elapsed_ms.ci_high < 0
    assert report.provenance.quarantine_revision == 4


def test_report_counts_randomized_sessions_that_end_under_another_source() -> None:
    allocations: list[ComputeAllocation] = []
    calls: list[InferenceCall] = []
    for index in range(4):
        for name, cpu in (("declined", 2000), ("granted", 8000)):
            session_allocations, session_calls = _session(
                f"{name}-{index}", source=RANDOMIZED, cpu=cpu, work_s=60
            )
            allocations.extend(session_allocations)
            calls.extend(session_calls)
    # Two declined Sessions ask again and receive a non-randomized grant.
    for index in range(2):
        allocations.append(
            ComputeAllocation(
                allocation_id=f"declined-{index}-regrant",
                org_id="acme",
                session_id=f"declined-{index}",
                allocation_source=REQUESTED,
                cpu_millicores=8000,
                memory_bytes=8 * 2**30,
                occurred_at=T0 + timedelta(seconds=5),
            )
        )

    report = build_compute_report(
        derive_compute_timing_result(allocations, calls), bootstrap_iterations=50
    )

    assert report.randomized_reassigned_sessions == 2
    [randomized] = [item for item in report.comparisons if item.randomized]
    assert (randomized.elapsed_ms.n_a, randomized.elapsed_ms.n_b) == (2, 4)


def test_report_and_rows_serialize_as_plain_deterministic_json() -> None:
    allocations, calls = _population()
    result = derive_compute_timing_result(allocations, calls)
    first = compute_report_to_json(
        build_compute_report(result, bootstrap_iterations=50)
    )
    rng = random.Random(3)
    rng.shuffle(allocations)
    rng.shuffle(calls)
    again = compute_report_to_json(
        build_compute_report(
            derive_compute_timing_result(allocations, calls), bootstrap_iterations=50
        )
    )
    assert json.dumps(first, allow_nan=False) == json.dumps(again, allow_nan=False)
    assert first["comparisons"][0]["arm_a"]["allocation_source"] == "randomized"

    [row, *_] = compute_timings_to_export_rows(result.timings)
    body = json.loads(json.dumps(row.body, allow_nan=False))
    assert body["session_id"] == "asked-0"
    assert body["final_allocation"] == {
        "allocation_source": "agent_request",
        "cpu_millicores": 8000,
        "memory_bytes": 8 * 2**30,
    }
    assert [item["allocation_id"] for item in body["allocations"]] == [
        "asked-0-start",
        "asked-0-alloc",
    ]
    assert body["started_at"] == "2026-10-01T12:00:00Z"
    assert body["provenance"]["policy_version"] == "1"


def test_generated_report_reads_one_snapshot_and_honors_quarantine(
    postgres_store,
) -> None:
    allocations, calls = _population()
    for allocation in allocations:
        assert postgres_store.store_compute_allocation(allocation)
    for call in calls:
        assert postgres_store.store_inference_call(call)
    # A call in a Session without an allocation stays outside the report.
    postgres_store.store_inference_call(
        calls[0].model_copy(
            update={"inference_call_id": "unallocated", "session_id": "unallocated"}
        )
    )
    postgres_store.quarantine_fact(
        "acme", FactTable.INFERENCE_CALLS, "small-0-1", reason="replayed capture"
    )

    result = generate_compute_report_result(
        postgres_store, "acme", bootstrap_iterations=50
    )

    assert result.report.sessions_with_allocation == 36
    assert result.report.provenance.quarantine_revision == 1
    small_zero = next(
        timing for timing in result.timings if timing.session_id == "small-0"
    )
    assert small_zero.inference_calls == 1
    assert small_zero.between_calls_ms == 0
    assert "unallocated" not in {timing.session_id for timing in result.timings}
