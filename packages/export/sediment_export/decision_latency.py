# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Decision-latency diagnostic.

This report is a pure diagnostic over assembled attributed completions plus the
inference-call facts they point at. Latency is computed as
``decision.captured_at - inference_call.observed_at`` — both are server-side
ingestion timestamps (``Field(default_factory=_now)`` on write, never
supplied by the client), deliberately **not**
``decision.occurred_at`` (the client-stamped event time).
``docs/explanation/attribution.md`` anchors dedup and ordering on
server-stamped facts because client clocks are skewable and forgeable; a
client-vs-server subtraction here would reintroduce that hazard. The
tradeoff: ``captured_at`` measures ingestion latency, not the true
wall-clock decision latency, if a source batches or delays delivery to
the server — an honest, non-forgeable proxy rather than a precise one.

Copilot's implicit-accept ``copilot_chat.edit.survival`` records are a
separate hazard the server-stamped pair does **not** fix: the
record is *emitted* only after its ``time_delay_ms`` measurement window
(0/5s/30s/2m/5m) elapses, so its ingest timestamp inherits the window —
up to five minutes of structural inflation that enriches the slow
buckets with implicit accepts under either timestamp domain. No
timestamp arithmetic recovers the accept moment, so retention-observation
decisions (``observation_delay_ms`` set) are excluded from the buckets and
counted under ``observation_delay``.

Rows with no usable latency are excluded and counted under a closed skip
vocabulary rather than zero-filled. This is intentionally descriptive only:
it surfaces whether accept rate or resolved reward confidence varies across
latency buckets, and never changes reward computation. A decision seen
across multiple file attributed_completions (dedup below) resolves to exactly one
included-or-skipped verdict: if any attributed completion yields a usable latency for that
``decision_id``, it counts as included, regardless of attributed_completion sort order.
Degenerate quantiles where every included decision has the same latency
collapse to one ``all`` bucket rather than producing empty buckets; buckets
built by index position can still coincide at their min/max latency when a
value repeats within (not across) a bucket — that is a display artifact of
sub-bucket ties, not a data or crash issue.

``mean_confidence_delta`` inherits a known circularity: ``resolve_confidence``
folds the decision itself into the reward signal (explicit accept resolves
toward 1.0, explicit reject toward 0.0), so a bucket's mean confidence is
largely a restatement of its accept rate, not an independent reliability
signal. Read ``accept_rate_delta`` as the primary finding and
``mean_confidence_delta`` as corroborating, not additional, evidence.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime

from sediment_core import FactStore
from sediment_derive import (
    MirrorManager,
    InferenceCall,
    RepositoryContext,
    inference_fact_id,
    inference_observed_at,
)

from .label_confidence import LabelConfidencePolicy, resolve_confidence_breakdown
from .attributed_completions import (
    AttributedCompletion,
    AttributedCompletionPolicy,
    _evidence_sort_key,
    assemble_attributed_completions_result,
)

SkipReason = str

SKIP_NO_DECISION = "no_decision"
SKIP_MISSING_INFERENCE_CALL = "missing_inference_call"
# Both InferenceCall.observed_at and DeveloperDecision.captured_at are
# non-nullable in the fact model, so this reason is currently unreachable in
# production -- defensive, not dead code: the None check stays because the
# fact model is what makes the guarantee, not this module, and a schema
# relaxation should not silently reintroduce a crash here.
SKIP_MISSING_TIMESTAMP = "missing_timestamp"
SKIP_INVALID_TIMESTAMP = "invalid_timestamp"
SKIP_NEGATIVE_LATENCY = "negative_latency"
# Retention observation: its ingest time inherits the vendor's
# measurement window by emission-time physics (see module docstring).
SKIP_OBSERVATION_DELAY = "observation_delay"

SKIP_REASONS: tuple[SkipReason, ...] = (
    SKIP_NO_DECISION,
    SKIP_MISSING_INFERENCE_CALL,
    SKIP_MISSING_TIMESTAMP,
    SKIP_INVALID_TIMESTAMP,
    SKIP_NEGATIVE_LATENCY,
    SKIP_OBSERVATION_DELAY,
)


@dataclass(frozen=True)
class DecisionLatencyBucket:
    """One quantile bucket of decision latencies."""

    bucket: str
    decisions: int
    accepts: int
    rejects: int
    accept_rate: float
    min_latency_ms: float
    max_latency_ms: float
    mean_latency_ms: float
    confidence_count: int
    mean_confidence: float | None


@dataclass(frozen=True)
class DecisionLatencyReport:
    """Decision-latency diagnostic summary.

    ``accept_rate_delta`` and ``mean_confidence_delta`` are slowest bucket
    minus fastest bucket, so a positive accept delta means slower decisions
    accepted more often in this sample.
    """

    buckets_requested: int
    buckets_returned: int
    total_decisions: int
    included_decisions: int
    skipped_decisions: dict[SkipReason, int] = field(default_factory=dict)
    accept_rate_delta: float | None = None
    mean_confidence_delta: float | None = None
    buckets: list[DecisionLatencyBucket] = field(default_factory=list)


@dataclass(frozen=True)
class _Observation:
    decision_id: str
    latency_ms: float
    accepted: bool
    confidence: float | None


def _milliseconds_between(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() * 1000


def _empty_skips() -> Counter[SkipReason]:
    return Counter({reason: 0 for reason in SKIP_REASONS})


def _bucket(label: str, observations: list[_Observation]) -> DecisionLatencyBucket:
    accepts = sum(1 for o in observations if o.accepted)
    confidence_values = [o.confidence for o in observations if o.confidence is not None]
    latencies = [o.latency_ms for o in observations]
    return DecisionLatencyBucket(
        bucket=label,
        decisions=len(observations),
        accepts=accepts,
        rejects=len(observations) - accepts,
        accept_rate=accepts / len(observations),
        min_latency_ms=min(latencies),
        max_latency_ms=max(latencies),
        mean_latency_ms=sum(latencies) / len(latencies),
        confidence_count=len(confidence_values),
        mean_confidence=(
            sum(confidence_values) / len(confidence_values)
            if confidence_values
            else None
        ),
    )


def _quantile_buckets(
    observations: list[_Observation], buckets_requested: int
) -> list[DecisionLatencyBucket]:
    if not observations:
        return []

    ordered = sorted(observations, key=lambda o: (o.latency_ms, o.decision_id))
    if buckets_requested <= 1 or ordered[0].latency_ms == ordered[-1].latency_ms:
        return [_bucket("all", ordered)]

    bucket_count = min(buckets_requested, len(ordered))
    buckets: list[DecisionLatencyBucket] = []
    for index in range(bucket_count):
        start = index * len(ordered) // bucket_count
        end = (index + 1) * len(ordered) // bucket_count
        buckets.append(_bucket(f"q{index + 1}", ordered[start:end]))
    return buckets


def build_decision_latency_report(
    inference_calls: Iterable[InferenceCall],
    attributed_completions: Iterable[AttributedCompletion],
    *,
    latency_buckets: int = 4,
    policy: LabelConfidencePolicy | None = None,
    repository_context: RepositoryContext | None = None,
) -> DecisionLatencyReport:
    """Bucket unique decisions by completion-to-decision latency.

    Both timestamps come from server-side observation fields:
    ``InferenceCall.observed_at`` and ``DeveloperDecision.captured_at`` (see
    module docstring for why, not ``occurred_at``). The report counts each
    ``DeveloperDecision.decision_id`` once, because one decision may be
    attached to multiple file-grained attributed completions: a decision resolves to exactly one
    included-or-skipped verdict across all the attributed completions it appears on — if
    any attributed completion yields a usable latency, the decision counts as included,
    never double-counted as both skipped (from one attributed completion) and included
    (from another). When a repeated decision has multiple resolved
    confidences, the lowest confidence is used for that decision's bucket.
    Identified CI evidence requires the caller's complete repository context.
    """

    if latency_buckets <= 0:
        raise ValueError("latency_buckets must be a positive integer")

    policy = policy or LabelConfidencePolicy()
    inference_calls_by_id = {inference_fact_id(call): call for call in inference_calls}
    no_decision_attributed_completions = 0
    observations_by_decision: dict[str, _Observation] = {}
    skip_reason_by_decision: dict[str, SkipReason] = {}

    for t in sorted(
        attributed_completions,
        key=_evidence_sort_key,
    ):
        if not t.decisions:
            no_decision_attributed_completions += 1
            continue

        inference_call = inference_calls_by_id.get(t.inference_call_id)
        breakdown = resolve_confidence_breakdown(
            t, policy, repository_context=repository_context
        )
        confidence = breakdown.final if breakdown is not None else None
        for decision in sorted(
            t.decisions, key=lambda d: (d.captured_at, d.decision_id)
        ):
            if decision.decision_id in observations_by_decision:
                existing = observations_by_decision[decision.decision_id]
                if confidence is not None and (
                    existing.confidence is None or confidence < existing.confidence
                ):
                    observations_by_decision[decision.decision_id] = replace(
                        existing, confidence=confidence
                    )
                continue

            if decision.observation_delay_ms is not None:
                # Checked before the inference-call lookup so the verdict is
                # intrinsic to the decision, not dependent on which attributed completions
                # it appears on.
                skip_reason_by_decision.setdefault(
                    decision.decision_id, SKIP_OBSERVATION_DELAY
                )
                continue

            if inference_call is None:
                skip_reason_by_decision.setdefault(
                    decision.decision_id, SKIP_MISSING_INFERENCE_CALL
                )
                continue

            inference_call_time = inference_observed_at(inference_call)
            decision_time = decision.captured_at
            if inference_call_time is None or decision_time is None:
                skip_reason_by_decision.setdefault(
                    decision.decision_id, SKIP_MISSING_TIMESTAMP
                )
                continue

            try:
                latency_ms = _milliseconds_between(inference_call_time, decision_time)
            except TypeError:
                skip_reason_by_decision.setdefault(
                    decision.decision_id, SKIP_INVALID_TIMESTAMP
                )
                continue

            if latency_ms < 0:
                skip_reason_by_decision.setdefault(
                    decision.decision_id, SKIP_NEGATIVE_LATENCY
                )
                continue

            skip_reason_by_decision.pop(decision.decision_id, None)
            observations_by_decision[decision.decision_id] = _Observation(
                decision_id=decision.decision_id,
                latency_ms=latency_ms,
                accepted=decision.accepted,
                confidence=confidence,
            )

    skips = _empty_skips()
    skips[SKIP_NO_DECISION] = no_decision_attributed_completions
    for decision_id, reason in skip_reason_by_decision.items():
        if decision_id not in observations_by_decision:
            skips[reason] += 1

    buckets = _quantile_buckets(
        list(observations_by_decision.values()), latency_buckets
    )
    accept_delta = None
    confidence_delta = None
    if len(buckets) >= 2:
        accept_delta = buckets[-1].accept_rate - buckets[0].accept_rate
        if (
            buckets[0].mean_confidence is not None
            and buckets[-1].mean_confidence is not None
        ):
            confidence_delta = buckets[-1].mean_confidence - buckets[0].mean_confidence

    return DecisionLatencyReport(
        buckets_requested=latency_buckets,
        buckets_returned=len(buckets),
        total_decisions=len(observations_by_decision)
        + sum(count for reason, count in skips.items() if reason != SKIP_NO_DECISION),
        included_decisions=len(observations_by_decision),
        skipped_decisions=dict(skips),
        accept_rate_delta=accept_delta,
        mean_confidence_delta=confidence_delta,
        buckets=buckets,
    )


def generate_decision_latency_report(
    store: FactStore,
    mirrors: MirrorManager,
    org_id: str,
    *,
    latency_buckets: int = 4,
    attributed_completion_policy: AttributedCompletionPolicy | None = None,
    label_confidence_policy: LabelConfidencePolicy | None = None,
) -> DecisionLatencyReport:
    """Read facts, assemble attributed completions, and build the latency diagnostic."""

    with store.read_snapshot() as snapshot:
        completions = snapshot.read_inference_call_summaries(org_id)
        assembled = assemble_attributed_completions_result(
            snapshot, mirrors, org_id, attributed_completion_policy
        )
        return build_decision_latency_report(
            completions,
            assembled.rows,
            latency_buckets=latency_buckets,
            policy=label_confidence_policy,
            repository_context=assembled.repository_context,
        )
