# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bounded, read-only operational report assembly."""

from __future__ import annotations

from collections import Counter, defaultdict
import json
from dataclasses import asdict, dataclass, replace
from datetime import timedelta
from math import ceil

from sediment_core import FactStore, OrgId
from sediment_core.store import CIOutcomeProjection
from sediment_derive import (
    CommitRef,
    MirrorManager,
    derive_abandonment,
    derive_attribution_result,
    derive_fate_result,
    four_gram_containment,
    bind_session_commit_keys_result,
    read_repository_context,
)
from sediment_derive.repository_identity import (
    CommitKey,
    IdentifiedRepositoryKey,
    RepositoryContext,
    repository_read_key,
)
from sediment_derive.attribution import tokenize
from sediment_derive.inference_call import render_scoring_text
from sediment_export import (
    AcceptedWorkLifecycleReport,
    AttributedCompletion,
    AttributedCompletionPolicy,
    ModelOutcomeReportResult,
    OperationalReportScope,
    OutcomeReportPolicy,
    assemble_attributed_completions_result,
    build_abandonment_summary,
    build_model_report_result,
    derive_model_report_attribution_share,
    generate_accepted_work_lifecycle_report,
)

from sediment_export.outcome_report import with_model_assembly_diagnostics

from ..reports.attribution_share_report import (
    attribution_share_alert_payload,
    attribution_share_row_payload,
)

_SUPPORTING_FACT_LIMIT = 50_000


@dataclass(frozen=True)
class LifecycleReportRequest:
    """Inputs for one accepted-work lifecycle report."""

    org_id: OrgId
    scope: OperationalReportScope | None = None


@dataclass(frozen=True)
class LifecycleReportEnvelope:
    """The request scope and its canonical lifecycle artifact."""

    scope: OperationalReportScope | None
    report: AcceptedWorkLifecycleReport

    def to_json(self) -> str:
        """Serialize the canonical report payload without delivery-side state."""

        return json.dumps(asdict(self.report), sort_keys=True)


def generate_lifecycle_report(
    store: FactStore,
    mirrors: MirrorManager,
    request: LifecycleReportRequest,
) -> LifecycleReportEnvelope:
    """Generate one lifecycle report without persistence or presentation."""

    report = generate_accepted_work_lifecycle_report(
        store,
        mirrors,
        request.org_id,
        scope=request.scope,
    )
    return LifecycleReportEnvelope(scope=request.scope, report=report)


@dataclass(frozen=True)
class OperationalModelReport:
    """Canonical report plus scoped inputs needed by CLI-only comparisons."""

    result: ModelOutcomeReportResult
    completions: list
    attributed_completions: list[AttributedCompletion]
    repository_context: RepositoryContext | None = None
    ci_population: tuple[CIOutcomeProjection, ...] = ()


def base_model_report_payload(result: ModelOutcomeReportResult) -> dict[str, object]:
    return {
        "rows": [asdict(row) for row in result.rows],
        "stratification": [asdict(check) for check in result.stratification],
        "signal_funnel": [asdict(row) for row in result.signal_funnel],
        "abandonment": asdict(result.abandonment),
        "fate_skipped": result.fate_skipped,
        "repository_skipped": result.repository_skipped,
        "ci_skipped": result.ci_skipped,
        "fate_provenance": (
            asdict(result.fate_provenance)
            if result.fate_provenance is not None
            else None
        ),
        "attribution_share": [
            attribution_share_row_payload(row) for row in result.attribution_share
        ],
        "attribution_alerts": [
            attribution_share_alert_payload(alert)
            for alert in result.attribution_alerts
        ],
    }


def generate_operational_model_report(
    store: FactStore,
    mirrors: MirrorManager,
    org_id: str,
    scope: OperationalReportScope,
    *,
    assembly_policy: AttributedCompletionPolicy | None = None,
    report_policy: OutcomeReportPolicy | None = None,
    include_trends: bool = False,
) -> OperationalModelReport:
    """Build a scoped model report from one Fact and mirror snapshot."""

    assembly_policy = assembly_policy or AttributedCompletionPolicy()
    with store.read_snapshot() as snapshot:
        repository_context = read_repository_context(
            snapshot, org_id, as_of=scope.as_of
        )
        ci_population = snapshot.read_ci_outcome_projections(
            org_id, captured_through=scope.as_of, limit=_SUPPORTING_FACT_LIMIT
        )
        summaries = snapshot.read_inference_call_summaries(
            org_id,
            observed_between=(scope.cohort_start, scope.cohort_end),
            limit=scope.max_inference_calls,
        )
        call_ids = {item.inference_call_id for item in summaries}
        completions = snapshot.read_report_inference_calls(
            org_id, inference_call_ids=call_ids
        )
        session_ids = {item.session_id for item in summaries}
        decisions = snapshot.read_decisions(
            org_id,
            captured_through=scope.as_of,
            session_ids=session_ids,
            limit=_SUPPORTING_FACT_LIMIT,
        )
        decision_identities = (
            snapshot.read_inference_call_identities(
                org_id, observed_through=scope.as_of, limit=_SUPPORTING_FACT_LIMIT
            )
            if any(decision.call_id is not None for decision in decisions)
            else []
        )
        observations = snapshot.read_edit_observations(
            org_id,
            captured_through=scope.as_of,
            session_ids=session_ids,
            limit=_SUPPORTING_FACT_LIMIT,
        )
        share_pushes = snapshot.read_pushes(
            org_id,
            captured_between=(
                scope.cohort_start - timedelta(days=30),
                scope.as_of + timedelta(microseconds=1),
            ),
            limit=_SUPPORTING_FACT_LIMIT,
        )
        pushes = [
            push
            for push in share_pushes
            if push.captured_at
            >= scope.cohort_start
            - timedelta(
                minutes=assembly_policy.attribution.post_push_grace_period_minutes
            )
        ]
        # Observation Facts are metadata. Read their bounded population once so
        # legacy observations can retain an exact identified source-Push anchor.
        all_commit_observations = snapshot.read_session_commit_observations(
            org_id,
            as_of=scope.as_of,
            limit=_SUPPORTING_FACT_LIMIT,
        )
        commit_observations = [
            item for item in all_commit_observations if item.session_id in session_ids
        ]
        bindings = bind_session_commit_keys_result(
            commit_observations,
            org_id,
            as_of=scope.as_of,
            repository_context=repository_context,
        )
        note_sessions = defaultdict(set)
        session_commits = defaultdict(set)
        for commit, session_id in bindings.bindings:
            note_sessions[commit].add(session_id)
            session_commits[session_id].add(
                CommitRef(
                    repo=repository_context.repo_for(commit.repository),
                    commit_sha=commit.commit_sha,
                    repository_identity=commit.repository.identity
                    if isinstance(commit.repository, IdentifiedRepositoryKey)
                    else None,
                )
            )
        note_session_ids_by_commit = {
            key: frozenset(value) for key, value in note_sessions.items()
        }
        candidates = [
            (call, tokenize(render_scoring_text(call))) for call in completions
        ]
        resolved_pushes = [
            (push, repository_context.resolve_fact(push)) for push in share_pushes
        ]
        push_skips = Counter(
            resolved.reason for _, resolved in resolved_pushes if resolved.key is None
        )
        keys = {
            resolved.key for _, resolved in resolved_pushes if resolved.key is not None
        }
        with mirrors.read_repository_snapshot(keys):
            share_commits = set()
            for push, resolved in resolved_pushes:
                if resolved.key is None:
                    continue
                mirror = mirrors.open_repository(resolved.key)
                if mirror is not None:
                    share_commits.update(
                        CommitKey(resolved.key, sha)
                        for sha in mirror.list_push_commits(
                            push, assembly_policy.attribution.max_commits_per_push
                        )
                    )
            share_bindings = bind_session_commit_keys_result(
                all_commit_observations,
                org_id,
                as_of=scope.as_of,
                repository_context=repository_context,
            )
            share_notes = defaultdict(set)
            for commit, session_id in share_bindings.bindings:
                if commit in share_commits:
                    share_notes[commit].add(session_id)
            share_note_session_ids_by_commit = {
                key: frozenset(value) for key, value in share_notes.items()
            }
            attribution = derive_attribution_result(
                snapshot,
                mirrors,
                org_id,
                assembly_policy.attribution,
                pushes=pushes,
                candidates=candidates,
                note_session_ids_by_commit=note_session_ids_by_commit,
                repository_context=repository_context,
                as_of=scope.as_of,
            )
            commits = {
                repository_context.commit_key(
                    item.org_id,
                    item.repo,
                    item.commit_sha,
                    repository_identity=item.repository_identity,
                )
                for item in attribution.attributions
            }
            ci_outcomes = snapshot.read_ci_outcomes(
                org_id,
                captured_through=scope.as_of,
                repository_commits={
                    (repository_read_key(key.repository), key.commit_sha)
                    for key in commits
                    if key is not None
                },
                limit=_SUPPORTING_FACT_LIMIT,
            )
            abandonment = derive_abandonment(
                snapshot,
                mirrors,
                org_id,
                assembly_policy.abandonment,
                attributions=attribution.attributions,
                decisions=decisions,
                pushes=pushes,
                completions=completions,
                as_of=scope.as_of,
                session_commits=dict(session_commits),
                session_commit_observations=commit_observations,
                repository_context=repository_context,
            )
            assembly = assemble_attributed_completions_result(
                snapshot,
                mirrors,
                org_id,
                assembly_policy,
                attributions=attribution.attributions,
                completions=completions,
                decisions=decisions,
                decision_identities=decision_identities,
                edit_observations=observations,
                ci_outcomes=ci_outcomes,
                ci_population=ci_population,
                abandonment=abandonment,
                session_commit_observations=commit_observations,
                as_of=scope.as_of,
                repository_context=repository_context,
            )
            since_days = max(
                1,
                ceil((scope.cohort_end - scope.cohort_start).total_seconds() / 86_400),
            )
            attribution_share, attribution_alerts = (
                derive_model_report_attribution_share(
                    snapshot,
                    mirrors,
                    org_id,
                    since_days,
                    now=scope.as_of,
                    pushes=share_pushes,
                    candidate_limit=_SUPPORTING_FACT_LIMIT,
                    note_session_ids_by_commit=share_note_session_ids_by_commit,
                    repository_context=repository_context,
                )
            )
        quarantine_revision = snapshot.quarantine_revision(org_id)
        fate = derive_fate_result(
            observations,
            four_gram_containment,
            quarantine_revision=quarantine_revision,
        )
        result = build_model_report_result(
            completions,
            assembly.rows,
            ci_population=ci_population,
            scope=scope,
            repository_context=repository_context,
            decisions=decisions,
            decision_identities=decision_identities,
            policy=report_policy,
            include_trends=include_trends,
            attribution_share=attribution_share,
            attribution_alerts=attribution_alerts,
            abandonment=build_abandonment_summary(assembly),
            abandonment_is_scoped=True,
            quarantine_revision=quarantine_revision,
            fate_result=fate,
        )
        result = with_model_assembly_diagnostics(result, assembly)
        skipped = dict(result.repository_skipped)
        if push_skips:
            skipped["push_sources"] = dict(sorted(push_skips.items()))
        if bindings.skipped:
            skipped["preload_session_observations"] = dict(bindings.skipped)
        result = replace(result, repository_skipped=skipped)

    return OperationalModelReport(
        result, summaries, assembly.rows, repository_context, tuple(ci_population)
    )


def model_report_payload(
    store: FactStore,
    mirrors: MirrorManager,
    org_id: str,
    scope: OperationalReportScope,
    **kwargs,
) -> dict[str, object]:
    """Return the canonical base JSON payload for one scoped model report."""

    return base_model_report_payload(
        generate_operational_model_report(
            store, mirrors, org_id, scope, **kwargs
        ).result
    )
