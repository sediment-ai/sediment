# SPDX-License-Identifier: AGPL-3.0-or-later
"""Diagnostic aggregation for attributed changes measured through merge."""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime

from sediment_derive.session_commit import bind_session_commit_keys_result
from sediment_derive.repository_identity import (
    CommitKey,
    IdentifiedRepositoryKey,
    LegacyRepositoryKey,
    RepositoryContext,
    build_repository_context,
    repository_identity_evidence_of,
)
from sediment_derive.repository_context import read_repository_context

from sediment_core import FactStore
from sediment_derive import (
    AttributionPolicy,
    MergeRetention,
    MergeRetentionPolicy,
    MergeRetentionResult,
    MirrorManager,
    Provenance,
    derive_attribution_result,
    derive_merge_retention_result,
    join_decisions_by_call_id_result,
)

from .jsonl import ExportRow

RETENTION_THRESHOLDS = (0.8, 0.9, 1.0)


@dataclass(frozen=True)
class RetentionDistribution:
    count: int
    mean: float | None
    median: float | None
    p10: float | None
    p90: float | None


@dataclass(frozen=True)
class RetentionThreshold:
    threshold: float
    below: int
    total: int
    rate: float | None


@dataclass(frozen=True)
class RetentionScoreSummary:
    distribution: RetentionDistribution
    thresholds: tuple[RetentionThreshold, ...]


@dataclass(frozen=True)
class MergeRetentionReport:
    org_id: str
    attributed_file_candidates: int
    candidates_joined_to_merge: int
    scored_rows: int
    joined_pull_requests: int
    scored_pull_requests: int
    membership: dict[str, int]
    scoring_skips: dict[str, int]
    attribution_skips: dict[str, int]
    decision_attachment_skips: dict[str, int]
    head: RetentionScoreSummary
    merge: RetentionScoreSummary
    explicit_accept_rows: int
    explicit_accept_head_thresholds: tuple[RetentionThreshold, ...]
    explicit_accept_merge_thresholds: tuple[RetentionThreshold, ...]
    attribution_provenance: Provenance
    merge_retention_provenance: Provenance
    repository_skipped: dict[str, dict[str, int]] = field(default_factory=dict)


@dataclass(frozen=True)
class MergeRetentionReportResult:
    """One report and the canonical rows that produced its score summaries."""

    report: MergeRetentionReport
    rows: tuple[MergeRetention, ...]


def _factual_merge_result(
    org_id: str,
    result: MergeRetentionResult,
    repository_context: RepositoryContext | None,
):
    """Keep report totals and exported rows on the same observed population."""
    boundary = result.as_of or (
        repository_context.as_of
        if repository_context is not None
        else max(
            (item.captured_at for item in result.session_commit_observations),
            default=datetime.min.replace(tzinfo=UTC),
        )
    )
    if repository_context is not None and (
        repository_context.org_id != org_id or repository_context.as_of != boundary
    ):
        raise ValueError(
            "repository context must match report organization and boundary"
        )
    binding_context = repository_context or build_repository_context(
        (
            repository_identity_evidence_of(item)
            for item in result.session_commit_observations
        ),
        (),
        org_id,
        as_of=boundary,
    )
    binding_result = bind_session_commit_keys_result(
        result.session_commit_observations,
        org_id,
        as_of=boundary,
        repository_context=binding_context,
    )
    bindings = binding_result.bindings

    def commit_key(row):
        if row.org_id != org_id:
            return None
        if repository_context is not None:
            return repository_context.commit_key(
                row.org_id,
                row.repo,
                row.source_commit_sha,
                repository_identity=row.repository_identity,
            )
        return (
            CommitKey(LegacyRepositoryKey(row.org_id, row.repo), row.source_commit_sha)
            if row.repository_identity is None
            else None
        )

    def observed(row: MergeRetention) -> bool:
        return row.org_id == org_id and (commit_key(row), row.session_id) in bindings

    declined_edges = {
        (
            row.org_id,
            row.repository_identity,
            row.repo if row.repository_identity is None else None,
            row.source_commit_sha,
            row.session_id,
        )
        for row in (*result.rows, *result.membership_outcomes)
        if not observed(row)
    }
    skipped = Counter(result.skipped)
    if declined_edges:
        skipped["session_commit_unobserved"] += len(declined_edges)
    rows = [row for row in result.rows if observed(row)]
    outcomes = [row for row in result.membership_outcomes if observed(row)]
    # Reconstruct candidate totals from qualified source records, so a direct
    # caller cannot smuggle inferred aggregate counters into factual totals.
    candidates = {
        (
            commit_key(row),
            row.session_id,
            row.inference_call_id,
            row.source_file_path,
        )
        for row in (*rows, *outcomes)
    }
    joined = {
        (
            commit_key(row),
            row.session_id,
            row.inference_call_id,
            row.source_file_path,
        )
        for row in (*rows, *outcomes)
        if isinstance(row, MergeRetention) or row.status == "joined"
    }
    membership_reasons = {
        "without_merge": "attribution_without_merge",
        "ambiguous": "ambiguous_pr_membership",
        "ancestry_unresolved": "ancestry_check_failed",
    }
    qualified = replace(
        result,
        rows=rows,
        membership_outcomes=outcomes,
        skipped=skipped,
        attributed_candidates=len(candidates),
        joined_candidates=len(joined),
        joined_pull_requests=len(
            {
                (commit_key(row).repository, row.pr_number)
                for row in (*rows, *outcomes)
                if isinstance(row, MergeRetention) or row.status == "joined"
            }
        ),
        membership=Counter(
            membership_reasons[row.status]
            for row in outcomes
            if row.status in membership_reasons
        ),
    )
    repository_skipped = {}
    if binding_result.skipped:
        repository_skipped["session_observations"] = dict(
            sorted(binding_result.skipped.items())
        )
    unresolved = {
        (
            row.org_id,
            row.repository_identity,
            row.repo if row.repository_identity is None else None,
            row.source_commit_sha,
            row.session_id,
        )
        for row in (*result.rows, *result.membership_outcomes)
        if commit_key(row) is None
    }
    if unresolved:
        repository_skipped["candidate_edges"] = {
            "repository_identity_unresolved": len(unresolved)
        }
    return qualified, repository_skipped


def build_merge_retention_report(
    org_id: str,
    result: MergeRetentionResult,
    *,
    explicit_accepted_inference_call_ids: set[str],
    attribution_skipped: Counter[str],
    decision_attachment_skipped: Counter[str],
    attribution_provenance: Provenance,
    repository_context: RepositoryContext | None = None,
) -> MergeRetentionReport:
    """Build factual merge summaries from qualified source candidate records."""
    result, repository_skipped = _factual_merge_result(
        org_id, result, repository_context
    )
    head_scores = [row.head_retention_score for row in result.rows]
    merge_scores = [row.merge_retention_score for row in result.rows]
    explicit_rows = [
        row
        for row in result.rows
        if row.inference_call_id in explicit_accepted_inference_call_ids
    ]
    return MergeRetentionReport(
        org_id=org_id,
        attributed_file_candidates=result.attributed_candidates,
        candidates_joined_to_merge=result.joined_candidates,
        scored_rows=len(result.rows),
        joined_pull_requests=result.joined_pull_requests,
        scored_pull_requests=len(
            {
                (
                    IdentifiedRepositoryKey(row.org_id, row.repository_identity)
                    if row.repository_identity
                    else LegacyRepositoryKey(row.org_id, row.repo),
                    row.pr_number,
                )
                for row in result.rows
            }
        ),
        membership=dict(sorted(result.membership.items())),
        scoring_skips=dict(sorted(result.skipped.items())),
        attribution_skips=dict(sorted(attribution_skipped.items())),
        decision_attachment_skips=dict(sorted(decision_attachment_skipped.items())),
        head=_score_summary(head_scores),
        merge=_score_summary(merge_scores),
        explicit_accept_rows=len(explicit_rows),
        explicit_accept_head_thresholds=_thresholds(
            [row.head_retention_score for row in explicit_rows]
        ),
        explicit_accept_merge_thresholds=_thresholds(
            [row.merge_retention_score for row in explicit_rows]
        ),
        attribution_provenance=attribution_provenance,
        merge_retention_provenance=result.provenance,
        repository_skipped=repository_skipped,
    )


def generate_merge_retention_report(
    store: FactStore,
    mirrors: MirrorManager,
    org_id: str,
    *,
    attribution_policy: AttributionPolicy | None = None,
    merge_retention_policy: MergeRetentionPolicy | None = None,
    policy_digest: str | None = None,
    repository_context: RepositoryContext | None = None,
    as_of: datetime | None = None,
) -> MergeRetentionReport:
    """Read one stable Fact and mirror snapshot and generate the report."""
    return generate_merge_retention_report_result(
        store,
        mirrors,
        org_id,
        attribution_policy=attribution_policy,
        merge_retention_policy=merge_retention_policy,
        policy_digest=policy_digest,
        repository_context=repository_context,
        as_of=as_of,
    ).report


def generate_merge_retention_report_result(
    store: FactStore,
    mirrors: MirrorManager,
    org_id: str,
    *,
    attribution_policy: AttributionPolicy | None = None,
    merge_retention_policy: MergeRetentionPolicy | None = None,
    policy_digest: str | None = None,
    repository_context: RepositoryContext | None = None,
    as_of: datetime | None = None,
) -> MergeRetentionReportResult:
    """Derive one report and its canonical rows from one stable snapshot."""
    attribution_policy = attribution_policy or AttributionPolicy()
    merge_retention_policy = merge_retention_policy or MergeRetentionPolicy()
    as_of = as_of or (
        repository_context.as_of if repository_context else datetime.now(UTC)
    )
    with store.read_snapshot() as snapshot:
        repository_context = repository_context or read_repository_context(
            snapshot, org_id, as_of=as_of
        )
        with mirrors.read_repository_snapshot(repository_context.repository_keys()):
            attribution_result = derive_attribution_result(
                snapshot,
                mirrors,
                org_id,
                attribution_policy,
                policy_digest=policy_digest,
                repository_context=repository_context,
                as_of=as_of,
            )
            retention_result = derive_merge_retention_result(
                snapshot,
                mirrors,
                org_id,
                merge_retention_policy,
                attributions=attribution_result.attributions,
                policy_digest=policy_digest,
                repository_context=repository_context,
                as_of=as_of,
            )
            attachments = join_decisions_by_call_id_result(
                snapshot.read_inference_call_identities(
                    org_id, observed_through=as_of, limit=50_000
                ),
                snapshot.read_decisions(org_id, captured_through=as_of, limit=50_000),
            )
    explicit_ids = {
        inference_call_id
        for inference_call_id, decisions in attachments.decisions_by_completion.items()
        if any(decision.accepted and decision.explicit for decision in decisions)
    }
    attribution_provenance = Provenance(
        policy_version=attribution_policy.policy_version,
        quarantine_revision=retention_result.provenance.quarantine_revision,
        policy_digest=policy_digest,
    )
    return MergeRetentionReportResult(
        report=build_merge_retention_report(
            org_id,
            retention_result,
            explicit_accepted_inference_call_ids=explicit_ids,
            attribution_skipped=attribution_result.skipped,
            decision_attachment_skipped=attachments.skipped,
            attribution_provenance=attribution_provenance,
            repository_context=repository_context,
        ),
        rows=tuple(
            _factual_merge_result(org_id, retention_result, repository_context)[0].rows
        ),
    )


def merge_retention_to_export_rows(
    rows: Iterable[MergeRetention],
) -> list[ExportRow]:
    """Adapt canonical merge-retention rows to the shared JSONL writer."""
    return [ExportRow(split="train", body=asdict(row)) for row in rows]


def _score_summary(scores: list[float]) -> RetentionScoreSummary:
    return RetentionScoreSummary(
        distribution=_distribution(scores),
        thresholds=_thresholds(scores),
    )


def _distribution(scores: list[float]) -> RetentionDistribution:
    if not scores:
        return RetentionDistribution(0, None, None, None, None)
    ordered = sorted(scores)
    return RetentionDistribution(
        count=len(ordered),
        mean=statistics.fmean(ordered),
        median=statistics.median(ordered),
        p10=_percentile(ordered, 0.1),
        p90=_percentile(ordered, 0.9),
    )


def _percentile(ordered: list[float], fraction: float) -> float:
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _thresholds(scores: list[float]) -> tuple[RetentionThreshold, ...]:
    total = len(scores)
    return tuple(
        RetentionThreshold(
            threshold=threshold,
            below=sum(score < threshold for score in scores),
            total=total,
            rate=(
                sum(score < threshold for score in scores) / total if total else None
            ),
        )
        for threshold in RETENTION_THRESHOLDS
    )
