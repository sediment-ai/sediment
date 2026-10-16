# SPDX-License-Identifier: AGPL-3.0-or-later
"""Session compute timing: arithmetic, skips, and determinism."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta, timezone

from sediment_core import (
    ComputeAllocation,
    ComputeAllocationSource,
    GatewayProvider,
    InferenceCall,
)
from sediment_derive import (
    COMPUTE_TIMING_SKIP_REASONS,
    ComputeArm,
    compute_arm_sort_key,
    derive_compute_timing_result,
)

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _call(call_id: str, session_id: str, end_s: float, duration_ms: int | None):
    return InferenceCall(
        inference_call_id=call_id,
        org_id="acme",
        session_id=session_id,
        gateway_provider=GatewayProvider.LITELLM,
        input_messages=[],
        output_messages=[],
        duration_ms=duration_ms,
        observed_at=T0 + timedelta(seconds=end_s),
    )


def _allocation(
    allocation_id: str,
    session_id: str,
    *,
    at_s: float = -60,
    source: ComputeAllocationSource = ComputeAllocationSource.DEFAULT,
    cpu: int | None = 2000,
    memory: int | None = 4 * 2**30,
):
    return ComputeAllocation(
        allocation_id=allocation_id,
        org_id="acme",
        session_id=session_id,
        allocation_source=source,
        cpu_millicores=cpu,
        memory_bytes=memory,
        occurred_at=T0 + timedelta(seconds=at_s),
    )


def test_timing_splits_elapsed_into_model_and_between_call_time() -> None:
    # Call A runs [0s, 10s]; a 30 s tool gap; call B runs [40s, 45s].
    calls = [_call("a", "s", 10, 10_000), _call("b", "s", 45, 5_000)]
    result = derive_compute_timing_result([_allocation("x", "s")], calls)

    [timing] = result.timings
    assert timing.started_at == T0
    assert timing.ended_at == T0 + timedelta(seconds=45)
    assert (timing.elapsed_ms, timing.model_ms, timing.between_calls_ms) == (
        45_000,
        15_000,
        30_000,
    )
    assert timing.inference_calls == 2
    assert timing.final_allocation == ComputeArm(
        ComputeAllocationSource.DEFAULT, 2000, 4 * 2**30
    )
    assert timing.allocation_changed is False
    assert timing.provenance.policy_version == "1"
    assert set(result.skipped) == set(COMPUTE_TIMING_SKIP_REASONS)
    assert sum(result.skipped.values()) == 0


def test_parallel_calls_count_once_in_between_call_time() -> None:
    # Sub-agent calls overlap: [0, 20], [5, 15], [30, 40]. Model time sums all
    # three; between-call time is elapsed minus their union.
    calls = [
        _call("a", "s", 20, 20_000),
        _call("b", "s", 15, 10_000),
        _call("c", "s", 40, 10_000),
    ]
    [timing] = derive_compute_timing_result([_allocation("x", "s")], calls).timings
    assert timing.elapsed_ms == 40_000
    assert timing.model_ms == 40_000
    assert timing.between_calls_ms == 10_000


def test_final_allocation_is_the_latest_and_records_the_change() -> None:
    allocations = [
        _allocation("start", "s", at_s=-60),
        _allocation(
            "resize",
            "s",
            at_s=20,
            source=ComputeAllocationSource.AGENT_REQUEST,
            cpu=8000,
            memory=None,
        ),
    ]
    [timing] = derive_compute_timing_result(
        allocations, [_call("a", "s", 10, 10_000)]
    ).timings
    assert timing.final_allocation == ComputeArm(
        ComputeAllocationSource.AGENT_REQUEST, 8000, None
    )
    assert timing.allocation_changed is True
    assert [item.allocation_id for item in timing.allocations] == ["start", "resize"]


def test_a_repeated_allocation_is_not_a_change() -> None:
    allocations = [_allocation("one", "s", at_s=-60), _allocation("two", "s", at_s=5)]
    [timing] = derive_compute_timing_result(
        allocations, [_call("a", "s", 10, 10_000)]
    ).timings
    assert timing.allocation_changed is False


def test_sessions_without_usable_timing_are_skipped_and_counted() -> None:
    allocations = [
        _allocation("tie-a", "tie", at_s=0, cpu=2000),
        _allocation("tie-b", "tie", at_s=0, cpu=8000),
        _allocation("idle", "idle"),
        _allocation("partial", "partial"),
        _allocation("ok", "ok"),
    ]
    calls = [
        # A tie on the final instant takes precedence over timing evidence.
        _call("tie-call", "tie", 10, None),
        _call("partial-1", "partial", 10, 10_000),
        _call("partial-2", "partial", 20, None),
        _call("ok-call", "ok", 10, 1_000),
        # Calls in a Session without an allocation are outside the population.
        _call("unallocated", "unallocated", 10, 1_000),
    ]
    result = derive_compute_timing_result(allocations, calls)
    assert [timing.session_id for timing in result.timings] == ["ok"]
    assert dict(result.skipped) == {
        "ambiguous_final_allocation": 1,
        "no_inference_calls": 1,
        "inference_call_duration_absent": 1,
        "inference_call_interval_unrepresentable": 0,
    }


def test_an_unrepresentable_call_interval_skips_instead_of_raising() -> None:
    result = derive_compute_timing_result(
        [_allocation("x", "s")], [_call("a", "s", 10, 2**63 - 1)]
    )
    assert result.timings == []
    assert result.skipped["inference_call_interval_unrepresentable"] == 1


def test_organizations_never_mix() -> None:
    other = _allocation("other", "s").model_copy(update={"org_id": "other"})
    result = derive_compute_timing_result(
        [_allocation("x", "s"), other], [_call("a", "s", 10, 1_000)]
    )
    assert [(timing.org_id, timing.session_id) for timing in result.timings] == [
        ("acme", "s")
    ]
    assert result.skipped["no_inference_calls"] == 1


def test_arm_order_is_source_then_cpu_then_memory_with_absent_first() -> None:
    arms = [
        ComputeArm(ComputeAllocationSource.RANDOMIZED, 8000, None),
        ComputeArm(ComputeAllocationSource.RANDOMIZED, 2000, 2**30),
        ComputeArm(ComputeAllocationSource.DEFAULT, None, 2**30),
        ComputeArm(ComputeAllocationSource.RANDOMIZED, 2000, None),
    ]
    assert sorted(arms, key=compute_arm_sort_key) == [
        arms[2],
        arms[3],
        arms[1],
        arms[0],
    ]


def _population():
    allocations = [
        _allocation(f"alloc-{index}", f"s{index % 5}", at_s=index, cpu=1000 * index)
        for index in range(1, 11)
    ]
    calls = [
        _call(f"call-{index}", f"s{index % 6}", index * 7, (index * 1_337) % 9_000)
        for index in range(1, 30)
    ]
    return allocations, calls


def test_same_facts_derive_identical_output() -> None:
    allocations, calls = _population()
    first = derive_compute_timing_result(allocations, calls, quarantine_revision=3)
    second = derive_compute_timing_result(allocations, calls, quarantine_revision=3)
    assert first == second
    assert first.timings


def test_shuffled_ingest_order_and_time_zones_derive_identical_output() -> None:
    allocations, calls = _population()
    expected = derive_compute_timing_result(allocations, calls)
    shifted = timezone(timedelta(hours=-7))
    rng = random.Random(29)
    for _ in range(20):
        shuffled_allocations = [
            item.model_copy(
                update={"occurred_at": item.occurred_at.astimezone(shifted)}
            )
            for item in allocations
        ]
        shuffled_calls = [
            item.model_copy(
                update={"observed_at": item.observed_at.astimezone(shifted)}
            )
            for item in calls
        ]
        rng.shuffle(shuffled_allocations)
        rng.shuffle(shuffled_calls)
        actual = derive_compute_timing_result(shuffled_allocations, shuffled_calls)
        assert [
            (
                timing.session_id,
                timing.final_allocation,
                timing.elapsed_ms,
                timing.model_ms,
                timing.between_calls_ms,
                timing.started_at,
                [item.allocation_id for item in timing.allocations],
            )
            for timing in actual.timings
        ] == [
            (
                timing.session_id,
                timing.final_allocation,
                timing.elapsed_ms,
                timing.model_ms,
                timing.between_calls_ms,
                timing.started_at,
                [item.allocation_id for item in timing.allocations],
            )
            for timing in expected.timings
        ]
        assert actual.skipped == expected.skipped
