# SPDX-License-Identifier: AGPL-3.0-or-later
"""Attributed-completion assembly, the first canonical derived artifact.

``assemble_attributed_completions`` is a pure function of (facts, mirrors,
policy). It reads the organization's attributions, session abandonments,
decisions, and CI outcomes through quarantine-excluding fact-store reads. It
emits attribution evidence and explicit-accept abandonment evidence when the
accepted decision joins uniquely to an inference call.

- the developer decision(s) joined by ``call_id``, unique-or-drop
  (``sediment_derive.attachment.join_decisions_by_call_id`` — the same
  primitive ``rollout.py`` uses, at completion/org scope instead of session
  scope);
- the CI outcome(s) for ``(repo, commit_sha)``, never ``commit_sha`` alone
  (``sediment_derive.attachment.index_ci_outcomes_by_repo_commit`` — the same
  keying ``rollout.py`` uses).

Before the decision join, decisions pass through the transcript-survival
fill (``sediment_derive.attach_edit_retention`` scored by ``four_gram_containment``):
an EditObservation sharing the decision's
``(org, agent_harness, session, call_id)`` fills a missing
``edit_retention_score``; a vendor-supplied score stands. This
feeds the reward ladder's survival-interpolation rung for implicit accepts —
the graded evidence path once auto-approval is the harness default.

Every attribution produces one attributed completion, even without a decision
or CI outcome (ADR 0004). An abandoned session contributes one additional row
per inference call carrying a joined
explicit accept. Implicit-only sessions and unjoined explicit accepts remain
visible in the assembly result's closed skip counter instead of becoming
guessed labels.

Provenance and the eval split are stamped here, once, so every projection
built over attributed completions inherits them. ``split`` uses the same
``sediment_derive.split.split_of`` primitive as the rollout derivation, so an
inference call carries one split across both artifacts.

Purity constraints (ADR 0001): ordering is a pure function of the facts —
attributed completions sort attribution evidence on
``(repo, commit_sha, file_path, inference_call_id)`` and abandonment evidence on
``(session_id, inference_call_id)``. Every internal tie breaks on fact
identity, never ingest or read order, so shuffled inputs reproduce a
byte-identical list. The organization's ``quarantine_revision`` and the policy
version form each artifact's provenance.
"""

from __future__ import annotations

import logging
import json
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field, fields, replace
from itertools import islice

from sediment_core import (
    FactTable,
    NonEmptyId,
    SessionCommitObservation,
    OperationalReportLimitExceeded,
    REPOSITORY_IDENTITY_LIMIT,
)
from sediment_core import CIOutcome, DeveloperDecision, EditObservation, FactStore
from sediment_core.store import CIOutcomeProjection, InferenceCallIdentity
from sediment_derive import (
    AbandonmentPolicy,
    AbandonmentResult,
    AttributionSource,
    Attribution,
    AttributionPolicy,
    MirrorManager,
    InferenceCall,
    Provenance,
    SessionAbandonment,
    Split,
    attach_edit_retention,
    derive_abandonment,
    derive_attribution_result,
    drop_subsumed_codex_decisions,
    four_gram_containment,
    inference_fact_id,
    inference_observed_at,
    join_decisions_by_call_id_result,
    split_of,
)

from sediment_derive.ci_resolution import derive_ci_resolution_result
from sediment_derive.repository_context import read_repository_context
from sediment_derive.repository_identity import (
    CommitKey,
    RepositoryContext,
    RepositoryIdentity,
    repository_identity_evidence_of,
    repository_identity_of,
)
from sediment_derive.session_commit import bind_session_commit_keys_result
from datetime import UTC, datetime

logger = logging.getLogger("sediment.export.attributed_completions")

SKIP_IMPLICIT_ONLY_SESSION = "implicit_only_session"
SKIP_EXPLICIT_ACCEPT_UNJOINED = "explicit_accept_unjoined"


ATTRIBUTED_COMPLETION_IMPLEMENTATION_VERSION = "5"


@dataclass(frozen=True)
class AttributedCompletion:
    """One resolved *(completion, decision, survival, reward)* record — the
    attributed completion (ADR 0004), the canonical derived export artifact.
    Derived, never persisted (ADR 0001). Evidence is exactly one complete
    attribution or one session abandonment."""

    org_id: str
    session_id: str  # the attributed completion's REAL session (ADR 0002)
    inference_call_id: str
    repo: str | None
    commit_sha: str | None
    file_path: str | None
    similarity_score: float | None
    attribution_source: AttributionSource | None  # GIT_NOTES | JACCARD | absent
    decisions: list[DeveloperDecision]  # joined by call_id, unique-or-drop
    ci_outcomes: list[CIOutcome]  # joined by (repo, commit_sha), never sha alone
    provenance: Provenance
    split: Split  # split_of(session_id, eval_fraction) — same primitive as Rollout
    abandonment: SessionAbandonment | None = None
    session_commit_observations: tuple[SessionCommitObservation, ...] = ()
    repository_identity: RepositoryIdentity | None = None
    source_push_id: NonEmptyId | None = None

    def __post_init__(self) -> None:
        attribution_fields = (
            self.repo,
            self.commit_sha,
            self.file_path,
            self.similarity_score,
            self.attribution_source,
        )
        has_complete_attribution = all(
            value is not None for value in attribution_fields
        )
        has_no_attribution = all(value is None for value in attribution_fields)
        if self.abandonment is None:
            valid_variant = has_complete_attribution
        else:
            valid_variant = (
                has_no_attribution
                and self.repository_identity is None
                and self.source_push_id is None
            )
        if not valid_variant:
            raise ValueError(
                "AttributedCompletion must carry exactly one evidence variant: "
                "complete attribution fields or abandonment"
            )
        if self.abandonment is None:
            return
        if (
            self.org_id != self.abandonment.org_id
            or self.session_id != self.abandonment.session_id
        ):
            raise ValueError(
                "AttributedCompletion and abandonment evidence must share org_id "
                "and session_id"
            )
        if self.ci_outcomes:
            raise ValueError("abandonment evidence cannot carry CI outcomes")
        if self.abandonment.explicit_accepted_decisions < 1 or not any(
            decision.session_id == self.session_id
            and decision.accepted
            and decision.explicit
            for decision in self.decisions
        ):
            raise ValueError(
                "abandonment evidence requires a joined explicit accepted decision"
            )


@dataclass(frozen=True)
class AttributedCompletionAssemblyResult:
    """Attributed completions plus abandonment derivation and projection skips."""

    rows: list[AttributedCompletion] = field(default_factory=list)
    abandonment: AbandonmentResult = field(default_factory=AbandonmentResult)
    abandonment_skipped: Counter[str] = field(default_factory=Counter)
    skipped: Counter[str] = field(default_factory=Counter)
    repository_context: RepositoryContext | None = None


@dataclass(frozen=True)
class AbandonmentSummary:
    """Audit counts for abandonment derivation and label projection."""

    abandoned_sessions: int = 0
    grade_eligible_sessions: int = 0
    implicit_only_sessions: int = 0
    negative_completions: int = 0
    explicit_accepts_unjoined: int = 0
    derivation_skipped: dict[str, int] = field(default_factory=dict)
    provenance: Provenance | None = None


def build_abandonment_summary(
    result: AttributedCompletionAssemblyResult,
) -> AbandonmentSummary:
    """Build the shared deterministic abandonment audit summary."""
    grade_eligible_sessions = sum(
        1
        for session in result.abandonment.sessions
        if session.explicit_accepted_decisions > 0
    )
    return AbandonmentSummary(
        abandoned_sessions=len(result.abandonment.sessions),
        grade_eligible_sessions=grade_eligible_sessions,
        implicit_only_sessions=(
            len(result.abandonment.sessions) - grade_eligible_sessions
        ),
        negative_completions=sum(
            1 for row in result.rows if row.abandonment is not None
        ),
        explicit_accepts_unjoined=result.abandonment_skipped[
            SKIP_EXPLICIT_ACCEPT_UNJOINED
        ],
        derivation_skipped=dict(sorted(result.abandonment.skipped.items())),
        provenance=result.abandonment.provenance,
    )


@dataclass(frozen=True)
class AttributedCompletionPolicy:
    """The tunable attributed-completion-assembly semantics. ``attribution`` drives the
    underlying attribution derivation (notes/jaccard commit binding) —
    reused, not reforked. ``policy_version`` stamps every attributed_completion's
    provenance; bump it when tuning any knob so derived datasets stay
    distinguishable."""

    attribution: AttributionPolicy = field(default_factory=AttributionPolicy)
    abandonment: AbandonmentPolicy = field(default_factory=AbandonmentPolicy)
    # Version 4 qualifies decisions and captured Session observations.
    policy_version: str = ATTRIBUTED_COMPLETION_IMPLEMENTATION_VERSION
    # Eval-holdout share, hashed on session_id. The canonical version-1 policy
    # defaults to 0.1. A caller can set 0.0 to disable the split.
    eval_fraction: float = 0.1

    def __post_init__(self) -> None:
        if self.abandonment.attribution != self.attribution:
            raise ValueError(
                "AttributedCompletionPolicy.attribution must match "
                "AttributedCompletionPolicy.abandonment.attribution"
            )
        if not 0.0 <= self.eval_fraction <= 0.5:
            raise ValueError(
                f"AttributedCompletionPolicy.eval_fraction must be between 0.0 and 0.5 "
                f"(got {self.eval_fraction})."
            )


@dataclass
class AttributedCompletionResult:
    """Canonical rows plus attribution, attachment, and abandonment diagnostics."""

    attributed_completions: list[AttributedCompletion] = field(default_factory=list)
    skipped: Counter[str] = field(default_factory=Counter)
    abandonment: AbandonmentResult = field(default_factory=AbandonmentResult)
    abandonment_skipped: Counter[str] = field(default_factory=Counter)
    repository_context: RepositoryContext | None = None


def assemble_attributed_completions(
    store: FactStore,
    mirrors: MirrorManager,
    org_id: str,
    policy: AttributedCompletionPolicy | None = None,
    *,
    policy_digest: str | None = None,
    repository_context: RepositoryContext | None = None,
    as_of: datetime | None = None,
) -> list[AttributedCompletion]:
    """Assemble the org's attributed completions from its facts and mirrors.

    Every Attribution is emitted whether or not it resolves a decision or CI
    outcome. Matching captured observations retain their Session scope.
    Missing observations cannot derive abandonment. Completions, decisions,
    and CI outcomes are read quarantine-excluded (the default path, ADR 0001).
    The decision join runs over every completion the org holds, so a
    ``call_id`` shared across sessions is caught as ambiguous too. Decisions
    carry transcript-derived ``edit_retention_score`` fills
    (``attach_edit_retention``)
    into the join.

    Ordering is a function of the facts alone (ADR 0001), so a re-run — or a
    run over the same facts ingested in a different order — reproduces an
    identical attributed completions list.
    """
    return assemble_attributed_completion_result(
        store,
        mirrors,
        org_id,
        policy,
        policy_digest=policy_digest,
        repository_context=repository_context,
        as_of=as_of,
    ).attributed_completions


def _evidence_sort_key(row: AttributedCompletion) -> tuple:
    """Order attribution rows as before, then abandonment rows by identity."""
    if row.abandonment is None:
        return (
            0,
            _identity_sort_key(row),
            row.repo,
            row.commit_sha,
            row.file_path,
            row.inference_call_id,
        )
    return (1, row.session_id, row.inference_call_id, "", "")


def assemble_attributed_completions_result(
    store: FactStore,
    mirrors: MirrorManager,
    org_id: str,
    policy: AttributedCompletionPolicy | None = None,
    *,
    policy_digest: str | None = None,
    attributions: list[Attribution] | None = None,
    completions: list[InferenceCall] | None = None,
    decisions: list[DeveloperDecision] | None = None,
    decision_identities: list[InferenceCallIdentity] | None = None,
    edit_observations: list[EditObservation] | None = None,
    ci_outcomes: list[CIOutcome] | None = None,
    ci_population: Iterable[CIOutcome | CIOutcomeProjection] | None = None,
    abandonment: AbandonmentResult | None = None,
    session_commit_observations: list[SessionCommitObservation] | None = None,
    as_of: datetime | None = None,
    repository_context: RepositoryContext | None = None,
) -> AttributedCompletionAssemblyResult:
    """Assemble attribution and explicit-abandonment evidence populations.

    Supplied ``decision_identities`` is authoritative, even when empty. It retains
    attachment witnesses outside a caller's completion cohort. Omission uses the
    full supplied completion population, or the organization-wide store read.
    ``ci_population`` declares complete CI evidence through the boundary; omission
    reads it from the same snapshot. Exact ``ci_outcomes`` copies select commits,
    retaining every qualified attempt for each selected commit.
    """

    with store.read_snapshot() as snapshot:
        result = _assemble_attributed_completion_result(
            snapshot,
            mirrors,
            org_id,
            policy,
            policy_digest=policy_digest,
            attributions=attributions,
            completions=completions,
            decisions=decisions,
            decision_identities=decision_identities,
            edit_observations=edit_observations,
            ci_outcomes=ci_outcomes,
            ci_population=ci_population,
            abandonment=abandonment,
            session_commit_observations=session_commit_observations,
            as_of=as_of,
            repository_context=repository_context,
        )
    return AttributedCompletionAssemblyResult(
        rows=result.attributed_completions,
        abandonment=result.abandonment,
        abandonment_skipped=result.abandonment_skipped,
        skipped=result.skipped,
        repository_context=result.repository_context,
    )


def assemble_attributed_completion_result(
    store: FactStore,
    mirrors: MirrorManager,
    org_id: str,
    policy: AttributedCompletionPolicy | None = None,
    *,
    policy_digest: str | None = None,
    session_commit_observations: list[SessionCommitObservation] | None = None,
    as_of: datetime | None = None,
    repository_context: RepositoryContext | None = None,
) -> AttributedCompletionResult:
    """Assemble attributed completions from one consistent fact snapshot."""

    with store.read_snapshot() as snapshot:
        return _assemble_attributed_completion_result(
            snapshot,
            mirrors,
            org_id,
            policy,
            policy_digest=policy_digest,
            session_commit_observations=session_commit_observations,
            as_of=as_of,
            repository_context=repository_context,
        )


def _assemble_attributed_completion_result(
    store: FactStore,
    mirrors: MirrorManager,
    org_id: str,
    policy: AttributedCompletionPolicy | None,
    *,
    policy_digest: str | None,
    attributions: list[Attribution] | None = None,
    completions: list[InferenceCall] | None = None,
    decisions: list[DeveloperDecision] | None = None,
    decision_identities: list[InferenceCallIdentity] | None = None,
    edit_observations: list[EditObservation] | None = None,
    ci_outcomes: list[CIOutcome] | None = None,
    ci_population: Iterable[CIOutcome | CIOutcomeProjection] | None = None,
    abandonment: AbandonmentResult | None = None,
    session_commit_observations: list[SessionCommitObservation] | None = None,
    as_of: datetime | None = None,
    repository_context: RepositoryContext | None = None,
) -> AttributedCompletionResult:
    """Assemble rows and structured skips inside the caller's snapshot."""

    result = AttributedCompletionResult()
    policy = policy or AttributedCompletionPolicy()
    provenance = Provenance(
        policy_version=policy.policy_version,
        quarantine_revision=store.quarantine_revision(org_id),
        policy_digest=policy_digest,
    )

    if as_of is not None and as_of.utcoffset() is None:
        raise ValueError("as_of must be an aware datetime")
    if repository_context is not None:
        if repository_context.org_id != org_id or (
            as_of is not None and repository_context.as_of != as_of.astimezone(UTC)
        ):
            raise ValueError("repository context must match organization and boundary")
        as_of = repository_context.as_of
    ci_population = (
        store.read_ci_outcome_projections(
            org_id,
            captured_through=as_of or datetime.max.replace(tzinfo=UTC),
            limit=REPOSITORY_IDENTITY_LIMIT,
        )
        if ci_population is None
        else tuple(islice(ci_population, REPOSITORY_IDENTITY_LIMIT + 1))
    )
    if len(ci_population) > REPOSITORY_IDENTITY_LIMIT:
        raise OperationalReportLimitExceeded("CI population exceeds limit")
    population_by_id = {}
    for item in ci_population:
        key = (item.org_id, item.outcome_id)
        if key in population_by_id:
            raise ValueError("CI population repeats a source Outcome")
        population_by_id[key] = item
    selected_ci = ci_population if ci_outcomes is None else ci_outcomes
    selected_full_sources = {
        item.outcome_id: item
        for item in store.read_ci_outcomes_by_ids(
            org_id,
            {
                item.outcome_id
                for item in selected_ci
                if isinstance(item, CIOutcome)
                and isinstance(
                    population_by_id.get((item.org_id, item.outcome_id)),
                    CIOutcomeProjection,
                )
            },
        )
    }
    for item in selected_ci:
        source = population_by_id.get((item.org_id, item.outcome_id))
        if source is None or not _same_ci_source(item, source):
            raise ValueError("selected CI evidence contradicts its population")
        if isinstance(item, CIOutcome) and isinstance(source, CIOutcomeProjection):
            exact = selected_full_sources.get(item.outcome_id)
            if exact is None or not _same_ci_source(item, exact):
                raise ValueError("selected CI evidence contradicts its population")

    supplements = tuple(
        repository_identity_evidence_of(item)
        for item in (*(session_commit_observations or ()), *ci_population)
        if repository_context is None and repository_identity_of(item) is None
    )
    observation_facts = (
        store.read_session_commit_observations(org_id, as_of=as_of)
        if session_commit_observations is None
        else session_commit_observations
    )
    # Standalone assembly sees the whole org. Cohort callers supply complete
    # decision_identities so older and cross-Session aliases remain witnesses.
    if completions is None:
        completions = store.read_report_inference_calls(org_id)
    if decisions is None:
        decisions = store.read_decision_projections(org_id)
    if edit_observations is None:
        edit_observations = store.read_edit_observation_projections(org_id)
    preloaded_without_context = attributions is not None and repository_context is None
    context = repository_context or read_repository_context(
        store, org_id, as_of=as_of, supplemental_legacy_evidence=supplements
    )
    boundary = as_of or max(
        context.as_of,
        max(
            (inference_observed_at(item).astimezone(UTC) for item in completions),
            default=datetime.min.replace(tzinfo=UTC),
        ),
        max(
            (
                item.captured_at.astimezone(UTC)
                for item in (
                    *decisions,
                    *edit_observations,
                    *ci_population,
                    *observation_facts,
                )
                if item.org_id == org_id
            ),
            default=datetime.min.replace(tzinfo=UTC),
        ),
    )
    if context.as_of != boundary:
        context = read_repository_context(
            store, org_id, as_of=boundary, supplemental_legacy_evidence=supplements
        )
    result.repository_context = context
    completions = [
        item
        for item in completions
        if inference_observed_at(item).astimezone(UTC) <= boundary
    ]
    decisions = [
        item for item in decisions if item.captured_at.astimezone(UTC) <= boundary
    ]
    edit_observations = [
        item
        for item in edit_observations
        if item.captured_at.astimezone(UTC) <= boundary
    ]
    if decision_identities is not None:
        decision_identities = [
            item
            for item in decision_identities
            if item.observed_at.astimezone(UTC) <= boundary
        ]
    decisions = attach_edit_retention(
        decisions,
        edit_observations,
        four_gram_containment,
    )
    decisions = drop_subsumed_codex_decisions(decisions)
    observation_result = bind_session_commit_keys_result(
        observation_facts, org_id, as_of=boundary, repository_context=context
    )
    result.skipped.update(observation_result.skipped)
    observation_bindings = observation_result.bindings
    if attributions is None:
        observed_sessions = defaultdict(set)
        for commit, session_id in observation_bindings:
            observed_sessions[commit].add(session_id)
        captured_notes = {
            commit: frozenset(sessions)
            for commit, sessions in observed_sessions.items()
        }
        pushes_by_identity = defaultdict(list)
        for push in store.read_pushes(org_id):
            pushes_by_identity[push.repository_id is not None].append(push)
        attributions = []
        # Reuse this assembly's binding and loss counts for identified Pushes.
        # Legacy Pushes keep the Attribution owner's native Git-note path.
        for identified, pushes in sorted(pushes_by_identity.items()):
            attribution_result = derive_attribution_result(
                store,
                mirrors,
                org_id,
                policy.attribution,
                pushes=pushes,
                note_session_ids_by_commit=captured_notes if identified else None,
                policy_digest=policy_digest,
                repository_context=context,
                as_of=boundary,
            )
            result.skipped.update(attribution_result.skipped)
            attributions.extend(attribution_result.attributions)

    attachment_result = join_decisions_by_call_id_result(
        completions if decision_identities is None else decision_identities, decisions
    )
    result.skipped.update(attachment_result.skipped)
    decisions_by_completion = attachment_result.decisions_by_completion
    outcome_result = derive_ci_resolution_result(
        ci_population,
        repository_context=context,
        quarantine_revision=provenance.quarantine_revision,
    )
    result.skipped.update(outcome_result.skipped)
    selected_keys = set()
    for item in selected_ci:
        resolved = context.resolve_fact(item)
        if resolved.key is not None:
            selected_keys.add(CommitKey(resolved.key, item.commit_sha))
    outcomes_by_commit = {
        key: items
        for key, items in outcome_result.outcomes_by_commit.items()
        if key in selected_keys
    }

    if abandonment is None:
        abandonment = derive_abandonment(
            store,
            mirrors,
            org_id,
            policy.abandonment,
            attributions=attributions,
            policy_digest=policy_digest,
            session_commit_observations=observation_facts,
            as_of=boundary,
            repository_context=context,
        )

    # Ties break on the attribution's own identity tuple (repo, commit_sha,
    # file_path, inference_call_id) — fact identity, never ingest or read order.
    ordered = sorted(
        attributions,
        key=lambda c: (
            _identity_sort_key(c),
            c.repo,
            c.commit_sha,
            c.file_path,
            c.inference_call_id,
        ),
    )

    for attribution in ordered:
        commit = context.commit_key(
            attribution.org_id,
            attribution.repo,
            attribution.commit_sha,
            repository_identity=attribution.repository_identity,
        )
        reason = None
        if commit is None or (
            preloaded_without_context and attribution.repository_identity is not None
        ):
            reason = "repository_identity_unresolved"
        elif (
            attribution.repository_identity is not None
            or attribution.source_push_id is not None
        ):
            source = context.resolve_source(
                FactTable.PUSHES, attribution.source_push_id
            )
            if source.key is None:
                reason = source.reason
            elif source.key != commit.repository:
                reason = "repository_identity_conflict"
        if reason is not None:
            result.skipped[reason] += 1
            logger.warning(
                "attributed_completion_repository_declined reason=%s count=1", reason
            )
            continue
        turn_decisions = list(
            decisions_by_completion.get(attribution.inference_call_id, ())
        )
        outcomes = sorted(
            outcomes_by_commit.get(commit, ()),
            key=lambda o: o.outcome_id,
        )
        result.attributed_completions.append(
            AttributedCompletion(
                org_id=org_id,
                session_id=attribution.session_id,
                inference_call_id=attribution.inference_call_id,
                repo=context.repo_for(commit.repository),
                repository_identity=attribution.repository_identity,
                source_push_id=attribution.source_push_id,
                commit_sha=attribution.commit_sha,
                file_path=attribution.file_path,
                similarity_score=attribution.similarity_score,
                attribution_source=attribution.attribution_source,
                decisions=turn_decisions,
                ci_outcomes=outcomes,
                provenance=provenance,
                split=split_of(attribution.session_id, policy.eval_fraction),
                session_commit_observations=observation_bindings.get(
                    (commit, attribution.session_id),
                    (),
                ),
            )
        )

    completions_by_session: dict[str, list[InferenceCall]] = {}
    for completion in completions:
        completions_by_session.setdefault(completion.session_id, []).append(completion)

    abandonment_skipped: Counter[str] = Counter()
    joined_explicit_accept_ids: set[str] = set()
    abandoned_sessions = {item.session_id: item for item in abandonment.sessions}
    for session_id in sorted(abandoned_sessions):
        evidence = abandoned_sessions[session_id]
        if evidence.explicit_accepted_decisions == 0:
            abandonment_skipped[SKIP_IMPLICIT_ONLY_SESSION] += 1
            continue
        for completion in sorted(
            completions_by_session.get(session_id, ()),
            key=inference_fact_id,
        ):
            turn_decisions = list(
                decisions_by_completion.get(inference_fact_id(completion), ())
            )
            explicit_accepts = [
                decision
                for decision in turn_decisions
                if decision.accepted and decision.explicit
            ]
            if not explicit_accepts:
                continue
            joined_explicit_accept_ids.update(
                decision.decision_id for decision in explicit_accepts
            )
            result.attributed_completions.append(
                AttributedCompletion(
                    org_id=org_id,
                    session_id=session_id,
                    inference_call_id=inference_fact_id(completion),
                    repo=None,
                    commit_sha=None,
                    file_path=None,
                    similarity_score=None,
                    attribution_source=None,
                    decisions=turn_decisions,
                    ci_outcomes=[],
                    provenance=provenance,
                    split=split_of(session_id, policy.eval_fraction),
                    abandonment=evidence,
                )
            )

    for decision in decisions:
        if (
            decision.session_id in abandoned_sessions
            and abandoned_sessions[decision.session_id].explicit_accepted_decisions
            and decision.accepted
            and decision.explicit
            and decision.decision_id not in joined_explicit_accept_ids
        ):
            abandonment_skipped[SKIP_EXPLICIT_ACCEPT_UNJOINED] += 1

    result.attributed_completions.sort(key=_evidence_sort_key)
    decision_ids = {
        decision.decision_id
        for row in result.attributed_completions
        for decision in row.decisions
    }
    outcome_ids = {
        outcome.outcome_id
        for row in result.attributed_completions
        for outcome in row.ci_outcomes
    }
    exact_decisions = {
        decision.decision_id: decision
        for decision in store.read_decisions_by_ids(org_id, decision_ids)
    }
    exact_outcomes = {
        outcome.outcome_id: outcome
        for outcome in store.read_ci_outcomes_by_ids(org_id, outcome_ids)
    }
    for items in outcomes_by_commit.values():
        for item in items:
            if item.outcome_id not in outcome_ids:
                continue
            if isinstance(item, CIOutcome):
                exact_outcomes[item.outcome_id] = item
            else:
                exact = exact_outcomes.get(item.outcome_id)
                if exact is None or not _same_ci_source(item, exact):
                    raise ValueError("CI population lacks its exact captured Fact")
    result.attributed_completions = [
        replace(
            row,
            decisions=[
                exact_decisions[decision.decision_id].model_copy(
                    update={"edit_retention_score": decision.edit_retention_score}
                )
                for decision in row.decisions
            ],
            ci_outcomes=[
                exact_outcomes[outcome.outcome_id] for outcome in row.ci_outcomes
            ],
        )
        for row in result.attributed_completions
    ]
    result.abandonment = abandonment
    result.abandonment_skipped.update(abandonment_skipped)

    logger.info(
        "attributed_completions_assembled",
        extra={
            "org_id": org_id,
            "attributions": len(attributions),
            "attributed_completions": len(result.attributed_completions),
            "abandoned_sessions": len(abandonment.sessions),
            "abandonment_skipped": dict(sorted(abandonment_skipped.items())),
        },
    )
    return result


def _same_ci_source(
    left: CIOutcome | CIOutcomeProjection, right: CIOutcome | CIOutcomeProjection
) -> bool:
    """Compare every projected source field by value and UTC instant."""
    for item in fields(CIOutcomeProjection):
        left_value, right_value = getattr(left, item.name), getattr(right, item.name)
        if item.name == "captured_at":
            left_value, right_value = (
                left_value.astimezone(UTC),
                right_value.astimezone(UTC),
            )
        if left_value != right_value:
            return False
    if isinstance(left, CIOutcome) and isinstance(right, CIOutcome):
        # JSON spelling makes non-finite raw values reflexive without treating
        # strings, booleans, or changed provider payloads as the same evidence.
        return json.dumps(
            left.model_dump(mode="json")["raw"], sort_keys=True, ensure_ascii=False
        ) == json.dumps(
            right.model_dump(mode="json")["raw"], sort_keys=True, ensure_ascii=False
        )
    return True


def _identity_sort_key(row) -> tuple[str, ...]:
    identity = row.repository_identity
    return (
        ("", "", "")
        if identity is None
        else (identity.provider, identity.host, identity.repository_id)
    )


def session_commit_observation_ids(
    row: AttributedCompletion, *, repository_context: RepositoryContext | None = None
) -> tuple[str, ...]:
    """Return original observation IDs on this artifact's qualified Session edge.

    Complete context is required to inherit identity from a legacy source Push.
    Without it, trusted canonical rows can match only explicit identical identity.
    """
    if row.abandonment is not None:
        return ()
    commit = (
        None
        if repository_context is None
        else repository_context.commit_key(
            row.org_id,
            row.repo,
            row.commit_sha,
            repository_identity=row.repository_identity,
        )
    )
    ids = set()
    for item in row.session_commit_observations:
        if (item.org_id, item.commit_sha, item.session_id) != (
            row.org_id,
            row.commit_sha,
            row.session_id,
        ):
            continue
        if repository_context is not None:
            resolved = repository_context.resolve_fact(item)
            matches = commit is not None and resolved.key == commit.repository
        else:
            identity = repository_identity_of(item)
            matches = identity == row.repository_identity and (
                identity is not None or item.repo == row.repo
            )
        if matches:
            ids.add(item.observation_id)
    return tuple(sorted(ids))
