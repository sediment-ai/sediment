# SPDX-License-Identifier: AGPL-3.0-or-later
"""Session compute timing: each Session's final Compute allocation beside the
time its gateway-captured Inference calls span.

The Derivation answers one question per Session: which allocation did the
Session finish under, and how long did its agent work take? It reads two
Facts and no clock:

- ``ComputeAllocation`` — the limits and who chose them. The final allocation
  is the one with the latest ``occurred_at``; it is the arm the compute report
  groups by. Allocations order only against each other, on the sender's
  clock, and never against gateway time.
- ``InferenceCall`` — ``observed_at`` approximates call completion and
  ``duration_ms`` is the gateway-measured latency, so each call spans
  ``[observed_at - duration_ms, observed_at]`` in one clock domain.

Elapsed time runs from the earliest call start to the latest call end.
Model time is the sum of call durations; parallel calls can make it exceed
elapsed time. Between-call time is elapsed time with no call in flight: tool
execution, harness work, and waiting, which a CPU or memory limit can change
and model latency can't. The Derivation doesn't name it tool time, because a
person waiting lands in the same gap.

Sessions without a Compute allocation are outside the population. A Session
with allocations but no usable timing is skipped and counted under the closed
vocabulary below, never emitted half-formed. Session compute timing is a
diagnostic: no Evidence recipe, Reward, or training row consumes it.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

from sediment_core import (
    ComputeAllocation,
    ComputeAllocationSource,
    InferenceCall,
    NonEmptyId,
    OrgId,
)
from sediment_core.store import InferenceCallSummary

from .provenance import Provenance

logger = logging.getLogger("sediment.derive.compute_timing")

COMPUTE_TIMING_IMPLEMENTATION_VERSION = "1"

ComputeTimingSkipReason = Literal[
    "ambiguous_final_allocation",
    "no_inference_calls",
    "inference_call_duration_absent",
    "inference_call_interval_unrepresentable",
]
# Precedence order: a skipped Session counts under its first reason only.
COMPUTE_TIMING_SKIP_REASONS: tuple[ComputeTimingSkipReason, ...] = (
    "ambiguous_final_allocation",
    "no_inference_calls",
    "inference_call_duration_absent",
    # A duration_ms the Fact admits (up to int64) can reach past the earliest
    # representable instant; the Session is skipped, never a crash.
    "inference_call_interval_unrepresentable",
)

_MILLISECOND = timedelta(milliseconds=1)


@dataclass(frozen=True)
class ComputeArm:
    """One allocation value: who chose it and the limits it set.

    An absent limit is unknown, and ``None`` keeps it distinct from every
    reported value."""

    allocation_source: ComputeAllocationSource
    cpu_millicores: int | None
    memory_bytes: int | None


def compute_arm_of(allocation: ComputeAllocation) -> ComputeArm:
    return ComputeArm(
        allocation_source=allocation.allocation_source,
        cpu_millicores=allocation.cpu_millicores,
        memory_bytes=allocation.memory_bytes,
    )


def compute_arm_sort_key(arm: ComputeArm) -> tuple[str, int, int]:
    """Deterministic arm order: source, then CPU, then memory, absent first."""
    return (
        arm.allocation_source.value,
        -1 if arm.cpu_millicores is None else arm.cpu_millicores,
        -1 if arm.memory_bytes is None else arm.memory_bytes,
    )


@dataclass(frozen=True)
class SessionComputeTiming:
    """One Session's Compute allocations and the time its Inference calls span.

    ``allocations`` keeps every visible allocation in ``occurred_at`` order, so
    a reader sees each resize and who chose it. ``final_allocation`` is the
    latest one. ``allocation_changed`` is true when the allocations carry more
    than one arm; the timing then covers more than one allocation."""

    org_id: OrgId
    session_id: NonEmptyId
    final_allocation: ComputeArm
    allocation_changed: bool
    allocations: tuple[ComputeAllocation, ...]
    inference_calls: int
    started_at: datetime
    ended_at: datetime
    elapsed_ms: int
    model_ms: int
    between_calls_ms: int
    provenance: Provenance


@dataclass
class ComputeTimingResult:
    """Session compute timings plus closed per-Session skip counts."""

    provenance: Provenance
    timings: list[SessionComputeTiming] = field(default_factory=list)
    skipped: Counter[str] = field(
        default_factory=lambda: Counter(
            {reason: 0 for reason in COMPUTE_TIMING_SKIP_REASONS}
        )
    )


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def _intervals(
    calls: list[InferenceCall | InferenceCallSummary],
) -> list[tuple[datetime, datetime]] | None:
    try:
        return sorted(
            (
                _utc(call.observed_at) - timedelta(milliseconds=call.duration_ms),
                _utc(call.observed_at),
            )
            for call in calls
        )
    except OverflowError:
        return None


def _busy(intervals: list[tuple[datetime, datetime]]) -> timedelta:
    """Length of the union of closed intervals, so parallel calls count once."""
    busy = timedelta(0)
    current_start, current_end = intervals[0]
    for start, end in intervals[1:]:
        if start > current_end:
            busy += current_end - current_start
            current_start, current_end = start, end
        else:
            current_end = max(current_end, end)
    return busy + (current_end - current_start)


def derive_compute_timing_result(
    allocations: Iterable[ComputeAllocation],
    inference_calls: Iterable[InferenceCall | InferenceCallSummary],
    *,
    quarantine_revision: int = 0,
) -> ComputeTimingResult:
    """Derive one ``SessionComputeTiming`` per Session that has allocations.

    The output depends only on the supplied Facts: input order, capture time,
    and ingest order never change it."""

    provenance = Provenance(
        policy_version=COMPUTE_TIMING_IMPLEMENTATION_VERSION,
        quarantine_revision=quarantine_revision,
    )
    result = ComputeTimingResult(provenance=provenance)
    allocations_by_session: defaultdict[tuple[str, str], list[ComputeAllocation]] = (
        defaultdict(list)
    )
    for allocation in allocations:
        allocations_by_session[(allocation.org_id, allocation.session_id)].append(
            allocation
        )
    calls_by_session: defaultdict[
        tuple[str, str], list[InferenceCall | InferenceCallSummary]
    ] = defaultdict(list)
    for call in inference_calls:
        key = (call.org_id, call.session_id)
        if key in allocations_by_session:
            calls_by_session[key].append(call)

    for org_id, session_id in sorted(allocations_by_session):
        session_allocations = tuple(
            sorted(
                allocations_by_session[(org_id, session_id)],
                key=lambda item: (_utc(item.occurred_at), item.allocation_id),
            )
        )
        calls = sorted(
            calls_by_session[(org_id, session_id)],
            key=lambda item: (_utc(item.observed_at), item.inference_call_id),
        )
        final_at = _utc(session_allocations[-1].occurred_at)
        final_arms = {
            compute_arm_of(item)
            for item in session_allocations
            if _utc(item.occurred_at) == final_at
        }
        if len(final_arms) > 1:
            reason: ComputeTimingSkipReason | None = "ambiguous_final_allocation"
        elif not calls:
            reason = "no_inference_calls"
        elif any(call.duration_ms is None for call in calls):
            reason = "inference_call_duration_absent"
        elif (intervals := _intervals(calls)) is None:
            reason = "inference_call_interval_unrepresentable"
        else:
            reason = None
        if reason is not None:
            result.skipped[reason] += 1
            logger.debug(
                "compute_timing_skipped",
                extra={"org_id": org_id, "session_id": session_id, "reason": reason},
            )
            continue

        started_at = intervals[0][0]
        ended_at = max(end for _start, end in intervals)
        elapsed = ended_at - started_at
        result.timings.append(
            SessionComputeTiming(
                org_id=org_id,
                session_id=session_id,
                final_allocation=final_arms.pop(),
                allocation_changed=len(set(map(compute_arm_of, session_allocations)))
                > 1,
                allocations=session_allocations,
                inference_calls=len(calls),
                started_at=started_at,
                ended_at=ended_at,
                elapsed_ms=elapsed // _MILLISECOND,
                model_ms=sum(call.duration_ms for call in calls),
                between_calls_ms=(elapsed - _busy(intervals)) // _MILLISECOND,
                provenance=provenance,
            )
        )
    return result
