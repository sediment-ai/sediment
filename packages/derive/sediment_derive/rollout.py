# SPDX-License-Identifier: AGPL-3.0-or-later
"""Derive Session Rollouts from immutable Facts and read-only mirrors.

A continued Segment proves that the next input replays the prior typed input
and captured output. Roles, ordered parts, and semantic values compare exactly;
finish_reason is response metadata, not echoed input. Missing output, rewritten
history, and missing or contradictory output echoes open another Segment. Every
Turn remains, and the closed fragmented counter records each retained boundary.

TextPart content remains literal. Capture owns provider-envelope translation;
Derivation never parses text as provider JSON or removes semantic cache_control
keys. Structured object order is immaterial and matching non-finite categories
compare consistently. A continued Turn preserves the input suffix after the
prior input, including the output echo; a boundary Turn preserves its full input.

Session-to-commit binding and terminal CI evidence remain separate from Turn
continuity. All Fact reads exclude Quarantine. Ordering uses Fact time and
identity, never ingest order, and owner version 4 identifies these semantics.
"""

from __future__ import annotations

import logging
import math
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from sediment_core import (
    SessionCommitObservation,
    CIOutcome,
    DeveloperDecision,
    FactStore,
    InferenceMessage,
    TextPart,
    ReasoningPart,
    ToolCallPart,
    CommitSha,
    RepoSlug,
    REPOSITORY_IDENTITY_LIMIT,
    INFERENCE_REPORT_ROW_LIMIT,
)

from .attachment import (
    join_decisions_by_call_id_result,
)
from .ci_resolution import derive_ci_resolution_result
from .attribution import (
    AttributionSource,
    AttributionPolicy,
    derive_attribution_result,
)
from .inference_call import (
    InferenceCall,
    inference_fact_id,
    inference_input_messages,
    inference_observed_at,
    inference_tool_calls,
    render_scoring_text,
)
from .mirror import MirrorError, MirrorManager, RepoMirror, _git
from .notes import read_commit_note
from .provenance import Provenance
from .repository_context import read_repository_context
from .repository_identity import (
    CommitKey,
    IdentifiedRepositoryKey,
    LegacyRepositoryKey,
    RepositoryContext,
    RepositoryIdentity,
    RepositoryKey,
    commit_sort_key,
    repository_identity_evidence_of,
)
from .split import Split, split_of
from .session_commit import bind_session_commit_keys_result
from datetime import UTC, datetime

logger = logging.getLogger("sediment.derive.rollout")

ROLLOUT_IMPLEMENTATION_VERSION = "4"
RolloutFragmentReason = Literal[
    "prior_output_absent", "input_history_changed", "prior_output_not_replayed"
]
ROLLOUT_FRAGMENT_REASONS: tuple[RolloutFragmentReason, ...] = (
    "prior_output_absent",
    "input_history_changed",
    "prior_output_not_replayed",
)


@dataclass(frozen=True)
class Turn:
    """One agent turn: the messages this call added, its assistant output, and
    the human decisions bound to it. Derived, never persisted (ADR 0001)."""

    new_messages: list[InferenceMessage]
    completion: str  # assistant output, including any tool-call text
    decisions: tuple[DeveloperDecision, ...]  # joined by call_id, unique-or-drop
    inference_call_id: str
    # The inference-call fact's response-side tool calls, verbatim.
    # Empty when the capture saw none — no calls made, or no body logged.
    tool_calls: tuple[ToolCallPart, ...] = ()


@dataclass(frozen=True)
class CommitRef:
    """A repository-qualified commit identity."""

    repo: RepoSlug
    commit_sha: CommitSha
    repository_identity: RepositoryIdentity | None = None


@dataclass(frozen=True)
class Rollout:
    """A session-level trajectory with a terminal verifiable reward. Derived,
    never persisted (ADR 0001); a second canonical derived artifact beside the
    attributed completion (ADR 0004)."""

    org_id: str
    session_id: str
    segments: list[list[Turn]]  # one list of turns per unbroken prefix chain
    commits: list[CommitRef]  # session-attributed commits, in trajectory order
    attribution_source: AttributionSource  # GIT_NOTES | JACCARD
    terminal_outcomes: list[CIOutcome]  # every CI outcome for the commits above
    provenance: Provenance
    # Deterministic train/eval holdout, hashed on session_id with split_of.
    # Attributed completions use the same primitive.
    split: Split
    session_commit_observations: tuple[SessionCommitObservation, ...] = ()


@dataclass(frozen=True)
class RolloutPolicy:
    """The tunable rollout semantics. ``policy_version`` stamps every rollout's
    provenance; bump it when tuning any knob so derived datasets stay
    distinguishable. ``attribution`` drives the jaccard commit-binding fallback
    (the same derivation, filtered to the session)."""

    attribution: AttributionPolicy = field(default_factory=AttributionPolicy)
    policy_version: str = ROLLOUT_IMPLEMENTATION_VERSION
    # Eval-holdout share, hashed on session_id. The canonical version-1 policy
    # defaults to 0.1. A caller can set 0.0 to disable the split.
    eval_fraction: float = 0.1

    def __post_init__(self) -> None:
        if not 0.0 <= self.eval_fraction <= 0.5:
            raise ValueError(
                f"RolloutPolicy.eval_fraction must be between 0.0 and 0.5 "
                f"(got {self.eval_fraction})."
            )


@dataclass
class RolloutResult:
    """Canonical Rollouts, declined inputs, and retained-Turn fragmentation.

    With a Rollout sink, ``rollouts`` stays empty and the sink owns the rows.
    Diagnostics describe the complete successful run in either delivery mode.
    """

    rollouts: list[Rollout] = field(default_factory=list)
    skipped: Counter[str] = field(default_factory=Counter)
    fragmented: Counter[RolloutFragmentReason] = field(default_factory=Counter)


@dataclass
class NotesBindingResult:
    """Session-to-commit notes bindings plus fail-soft read losses."""

    session_commits: dict[str, set[CommitRef]] = field(default_factory=dict)
    skipped: Counter[str] = field(default_factory=Counter)


def derive_rollouts(
    store: FactStore,
    mirrors: MirrorManager,
    org_id: str,
    policy: RolloutPolicy | None = None,
    *,
    policy_digest: str | None = None,
    repository_context: RepositoryContext | None = None,
    as_of: datetime | None = None,
) -> list[Rollout]:
    """Derive the org's session rollouts from its facts and mirrors.

    One rollout per session that has at least one inference call. Inference calls,
    decisions, CI outcomes, and pushes are all read quarantine-excluded (the
    default path, ADR 0001). A session with no completions produces no rollout;
    a session with completions but no attributable commit is still emitted with
    an empty ``terminal_outcomes`` — it is SFT-able, just not reward-labeled.

    Ordering is a function of the facts alone (ADR 0001), so a re-run — or a
    run over the same facts ingested in a different order — reproduces identical
    rollouts.
    """
    return derive_rollout_result(
        store,
        mirrors,
        org_id,
        policy,
        policy_digest=policy_digest,
        repository_context=repository_context,
        as_of=as_of,
    ).rollouts


def derive_rollout_result(
    store: FactStore,
    mirrors: MirrorManager,
    org_id: str,
    policy: RolloutPolicy | None = None,
    *,
    policy_digest: str | None = None,
    mirror_repos: Iterable[str] | None = None,
    session_commit_observations: list[SessionCommitObservation] | None = None,
    as_of: datetime | None = None,
    repository_context: RepositoryContext | None = None,
    mirror_repositories: Iterable[RepositoryKey] | None = None,
    rollout_sink: Callable[[Rollout], None] | None = None,
) -> RolloutResult:
    """Derive complete Sessions and return their structured diagnostics.

    Without ``rollout_sink``, the result materializes all Rollouts. With a sink,
    each finalized Rollout is delivered in Session order and ``result.rollouts``
    stays empty. The sink owns row retention and must stage rows until this
    function succeeds. Sink and capacity errors propagate; callers must discard
    partial staging when a run fails instead of publishing an incomplete result.
    """

    with store.read_snapshot() as snapshot:
        return _derive_rollout_result(
            snapshot,
            mirrors,
            org_id,
            policy,
            policy_digest=policy_digest,
            mirror_repos=mirror_repos,
            session_commit_observations=session_commit_observations,
            as_of=as_of,
            repository_context=repository_context,
            mirror_repositories=mirror_repositories,
            rollout_sink=rollout_sink,
        )


def _derive_rollout_result(
    store: FactStore,
    mirrors: MirrorManager,
    org_id: str,
    policy: RolloutPolicy | None = None,
    *,
    policy_digest: str | None = None,
    mirror_repos: Iterable[str] | None = None,
    session_commit_observations: list[SessionCommitObservation] | None = None,
    as_of: datetime | None = None,
    repository_context: RepositoryContext | None = None,
    mirror_repositories: Iterable[RepositoryKey] | None = None,
    rollout_sink: Callable[[Rollout], None] | None = None,
) -> RolloutResult:
    """Derive rollouts inside the caller's fact snapshot."""

    result = RolloutResult()
    implicit_boundary = as_of is None and repository_context is None
    if mirror_repos is not None and mirror_repositories is not None:
        raise ValueError("choose one mirror repository inventory")
    if as_of is None and repository_context is not None:
        as_of = repository_context.as_of
    preloaded_without_context = (
        session_commit_observations is not None and repository_context is None
    )
    repository_context = repository_context or read_repository_context(
        store,
        org_id,
        as_of=as_of,
        supplemental_legacy_evidence=(
            repository_identity_evidence_of(item)
            for item in session_commit_observations or ()
            if item.repository_id is None
        ),
    )
    if as_of is not None and as_of.utcoffset() is None:
        raise ValueError("as_of must be an aware datetime")
    if repository_context.org_id != org_id or (
        as_of is not None and repository_context.as_of != as_of.astimezone(UTC)
    ):
        raise ValueError("repository context must match organization and boundary")
    call_summaries = store.read_inference_call_summaries(
        org_id, limit=INFERENCE_REPORT_ROW_LIMIT
    )
    decisions = store.read_decision_projections(org_id)
    if implicit_boundary:
        # A repository-only cutoff would hide later Calls or Decisions already
        # read for this Rollout, including the Attribution post-Push grace window.
        as_of = max(
            repository_context.as_of,
            max(
                (
                    inference_observed_at(call).astimezone(UTC)
                    for call in call_summaries
                ),
                default=repository_context.as_of,
            ),
            max(
                (row.captured_at.astimezone(UTC) for row in decisions),
                default=repository_context.as_of,
            ),
        )
        if as_of > repository_context.as_of:
            repository_context = read_repository_context(
                store,
                org_id,
                as_of=as_of,
                supplemental_legacy_evidence=(
                    repository_identity_evidence_of(item)
                    for item in session_commit_observations or ()
                    if item.repository_id is None
                ),
            )
    policy = policy or RolloutPolicy()
    provenance = Provenance(
        policy_version=policy.policy_version,
        quarantine_revision=store.quarantine_revision(org_id),
        policy_digest=policy_digest,
    )

    observation_facts = (
        store.read_session_commit_observations(org_id, as_of=as_of)
        if session_commit_observations is None
        else session_commit_observations
    )
    if preloaded_without_context:
        observation_facts = [
            item
            for item in observation_facts
            if item.org_id == org_id
            and item.captured_at.astimezone(UTC) <= repository_context.as_of
        ]
        declined = sum(item.repository_id is not None for item in observation_facts)
        if declined:
            result.skipped["repository_identity_unresolved"] += declined
            logger.warning(
                "Rollout observations declined reason=repository_identity_unresolved count=%d",
                declined,
            )
        observation_facts = [
            item for item in observation_facts if item.repository_id is None
        ]
    binding_result = bind_session_commit_keys_result(
        observation_facts,
        org_id,
        as_of=repository_context.as_of,
        repository_context=repository_context,
    )
    bindings = binding_result.bindings
    result.skipped.update(binding_result.skipped)
    if as_of is not None:
        call_summaries = [
            call
            for call in call_summaries
            if inference_observed_at(call).astimezone(UTC) <= as_of.astimezone(UTC)
        ]
    if not call_summaries:
        return result
    if as_of is not None:
        decisions = [
            row
            for row in decisions
            if row.captured_at.astimezone(UTC) <= as_of.astimezone(UTC)
        ]
    ci_outcomes = store.read_ci_outcome_projections(
        org_id, captured_through=as_of, limit=REPOSITORY_IDENTITY_LIMIT
    )
    keys = (
        tuple(mirror_repositories)
        if mirror_repositories is not None
        else tuple(LegacyRepositoryKey(org_id, repo) for repo in mirror_repos)
        if mirror_repos is not None
        else tuple(mirrors.list_mirrored_repositories(org_id))
    )
    legacy_repos = sorted(
        key.repo
        for key in keys
        if isinstance(key, LegacyRepositoryKey) and key.org_id == org_id
    )

    session_ids = {call.session_id for call in call_summaries}
    decs_by_session: dict[str, list[DeveloperDecision]] = defaultdict(list)
    for d in decisions:
        decs_by_session[d.session_id].append(d)

    # Notes: session -> {commit_sha: repo}, from the refs/notes/sediment stamp.
    notes_result = session_commits_from_notes_result(
        mirrors, org_id, legacy_repos, repository_context=repository_context
    )
    result.skipped.update(notes_result.skipped)
    notes_commits = notes_result.session_commits
    qualified_note_sessions = defaultdict(set)
    for session_id, commits in notes_commits.items():
        for commit in commits:
            key = _commit_key(commit, org_id, repository_context)
            if key is not None:
                qualified_note_sessions[key].add(session_id)
    for commit, session_id in bindings:
        if isinstance(commit.repository, IdentifiedRepositoryKey):
            qualified_note_sessions[commit].add(session_id)
            notes_commits.setdefault(session_id, set()).add(
                _commit_ref(commit, repository_context)
            )

    # Jaccard: session -> {commit_sha: repo}, from attributions for the
    # session's completions. Computed only when some session lacks a notes
    # binding — the attribution walk diffs commits, so skip it entirely when
    # notes cover everything (notes supersede jaccard per session).
    jaccard_commits: dict[str, set[CommitRef]] = defaultdict(set)
    if any(not notes_commits.get(sid) for sid in session_ids):
        attribution_result = derive_attribution_result(
            store,
            mirrors,
            org_id,
            policy.attribution,
            policy_digest=policy_digest,
            repository_context=repository_context,
            as_of=as_of,
            note_session_ids_by_commit={
                key: frozenset(value) for key, value in qualified_note_sessions.items()
            },
        )
        result.skipped.update(attribution_result.skipped)
        for attribution in attribution_result.attributions:
            jaccard_commits[attribution.session_id].add(
                CommitRef(
                    repo=attribution.repo,
                    commit_sha=attribution.commit_sha,
                    repository_identity=attribution.repository_identity,
                )
            )

    # A conflicting retry can refer to another repository or commit. Qualify
    # complete runs before selecting this Session's terminal evidence.
    ci_index = derive_ci_resolution_result(
        ci_outcomes, repository_context=repository_context
    )
    result.skipped.update(ci_index.skipped)
    ci_by_commit = ci_index.outcomes_by_commit

    for session_id in sorted(session_ids):
        segments, attachment_skips, fragmented = project_session_turns(
            store.read_session_inference_calls(
                org_id, session_id, observed_through=as_of
            ),
            decs_by_session.get(session_id, []),
        )
        result.skipped.update(attachment_skips)
        result.fragmented.update(fragmented)
        notes_map = notes_commits.get(session_id) or set()
        if notes_map:
            source = AttributionSource.GIT_NOTES
            commit_refs = notes_map
        else:
            source = AttributionSource.JACCARD  # the fallback (may be empty)
            commit_refs = jaccard_commits.get(session_id, set())
        commits = _order_commits(
            mirrors, org_id, commit_refs, repository_context=repository_context
        )
        commit_keys = {
            _commit_key(commit, org_id, repository_context) for commit in commits
        }
        rollout = Rollout(
            org_id=org_id,
            session_id=session_id,
            segments=segments,
            commits=commits,
            attribution_source=source,
            terminal_outcomes=_terminal_outcomes(
                commits,
                ci_by_commit,
                org_id=org_id,
                repository_context=repository_context,
            ),
            provenance=provenance,
            split=split_of(session_id, policy.eval_fraction),
            session_commit_observations=tuple(
                item
                for key, observations in bindings.items()
                if key[1] == session_id and key[0] in commit_keys
                for item in observations
            ),
        )
        decision_ids = {
            decision.decision_id
            for segment in rollout.segments
            for turn in segment
            for decision in turn.decisions
        }
        outcome_ids = {outcome.outcome_id for outcome in rollout.terminal_outcomes}
        exact_decisions = {
            decision.decision_id: decision
            for decision in store.read_decisions_by_ids(org_id, decision_ids)
        }
        exact_outcomes = {
            outcome.outcome_id: outcome
            for outcome in store.read_ci_outcomes_by_ids(org_id, outcome_ids)
        }
        rollout = replace(
            rollout,
            segments=[
                [
                    replace(
                        turn,
                        decisions=tuple(
                            exact_decisions[decision.decision_id]
                            for decision in turn.decisions
                        ),
                    )
                    for turn in segment
                ]
                for segment in rollout.segments
            ],
            terminal_outcomes=[
                exact_outcomes[outcome.outcome_id]
                for outcome in rollout.terminal_outcomes
            ],
        )
        if rollout_sink is None:
            result.rollouts.append(rollout)
        else:
            rollout_sink(rollout)
        # Release the prior Session before decoding the next group's histories.
        del rollout, segments, exact_decisions, exact_outcomes
    return result


def project_session_turns(
    completions: list[InferenceCall], decisions: list[DeveloperDecision]
) -> tuple[list[list[Turn]], Counter[str], Counter[RolloutFragmentReason]]:
    """Continue only when typed prior input and output are replayed exactly.

    A retained Turn at a Segment boundary carries its full input. A continued
    Turn carries the suffix after the prior input, including the output echo.
    Fragmentation is separate from skipped inputs because every Turn remains.
    """
    ordered = sorted(
        completions,
        key=lambda call: (
            inference_observed_at(call).astimezone(UTC),
            inference_fact_id(call),
        ),
    )

    # (completion, new_messages, segment_index)
    entries: list[tuple[InferenceCall, list[InferenceMessage], int]] = []
    segment = 0
    prev: InferenceCall | None = None
    fragmented: Counter[RolloutFragmentReason] = Counter()
    for c in ordered:
        messages = inference_input_messages(c)
        reason = _fragment_reason(prev, c) if prev is not None else None
        if prev is None or reason is not None:
            new_messages = messages
            if reason is not None:
                segment += 1
                fragmented[reason] += 1
        else:
            new_messages = messages[len(inference_input_messages(prev)) :]
        entries.append((c, new_messages, segment))
        prev = c

    # Session scope: ambiguity is checked only within this session's own
    # completions (shared with attributed-completion assembly, which runs the same
    # primitive at completion/org scope — see attachment.py).
    attachment_result = join_decisions_by_call_id_result(
        (c for c, _, _ in entries), decisions
    )
    decisions_for = attachment_result.decisions_by_completion

    by_segment: dict[int, list[Turn]] = defaultdict(list)
    for c, new_messages, segment in entries:
        turn_decisions = tuple(decisions_for.get(inference_fact_id(c), ()))
        by_segment[segment].append(
            Turn(
                new_messages=new_messages,
                completion=render_scoring_text(c),
                decisions=turn_decisions,
                inference_call_id=inference_fact_id(c),
                tool_calls=tuple(inference_tool_calls(c)),
            )
        )
    return (
        [by_segment[s] for s in sorted(by_segment)],
        attachment_result.skipped,
        fragmented,
    )


def _fragment_reason(
    previous: InferenceCall, current: InferenceCall
) -> RolloutFragmentReason | None:
    """Classify one unproven adjacent pair in the closed precedence order."""
    if not any(
        not isinstance(part, TextPart | ReasoningPart) or bool(part.content)
        for message in previous.output_messages
        for part in message.parts
    ):
        return "prior_output_absent"
    before = _canonical_history(previous.input_messages)
    after = _canonical_history(current.input_messages)
    if after[: len(before)] != before:
        return "input_history_changed"
    expected = before + _canonical_history(previous.output_messages)
    if after[: len(expected)] != expected:
        return "prior_output_not_replayed"
    return None


def _canonical_history(messages: list[InferenceMessage]) -> list[tuple[str, Any]]:
    """Compare roles and ordered semantic parts, excluding finish_reason.

    TextPart content stays literal. Provider cache metadata is translated at
    capture; semantic cache_control keys in structured values remain intact.
    """
    return [
        (
            message.role,
            [_semantic_value(part.model_dump(mode="python")) for part in message.parts],
        )
        for message in messages
    ]


def _semantic_value(value: Any) -> Any:
    """Keep structured equality while making non-finite categories reflexive."""
    if isinstance(value, dict):
        return {key: _semantic_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_semantic_value(item) for item in value]
    if isinstance(value, bool):
        # JSON booleans are distinct from the numeric values 0 and 1.
        return (bool, value)
    if isinstance(value, float) and not math.isfinite(value):
        # A tagged tuple can't collide with a JSON string, list, or object.
        return (float, "nan" if math.isnan(value) else str(value))
    return value


def session_commits_from_notes(
    mirrors: MirrorManager, org_id: str, repos: list[str]
) -> dict[str, set[CommitRef]]:
    """Notes binding: session ID to repository-qualified commits.
    ``refs/notes/sediment`` stamp names the session. A note is proof of the
    session->commit edge regardless of push timing, so this reads the whole
    notes ref, not a push window. Fail-soft throughout: a repo never mirrored,
    a mirror with no notes ref, or an unreadable note simply contributes
    nothing.

    ponytail: one ``read_commit_note`` subprocess per noted commit per repo. A
    ``git notes ... show`` batched over the listed objects is the upgrade when
    a repo's note volume makes the per-note fork cost show up."""
    return session_commits_from_notes_result(mirrors, org_id, repos).session_commits


def session_commits_from_notes_result(
    mirrors: MirrorManager,
    org_id: str,
    repos: list[str],
    *,
    repository_context: RepositoryContext | None = None,
) -> NotesBindingResult:
    """Read notes bindings and count listed notes that can't be decoded."""

    result = NotesBindingResult()
    out: dict[str, set[CommitRef]] = defaultdict(set)
    for repo in repos:
        if repository_context is not None:
            resolved = repository_context.resolve_reference(org_id, repo)
            if resolved.key is None:
                result.skipped[resolved.reason] += 1
                logger.warning(
                    "Rollout legacy notes declined reason=%s count=1", resolved.reason
                )
                continue
            key = resolved.key
        else:
            key = LegacyRepositoryKey(org_id, repo)
        mirror = mirrors.open_repository(key)
        if mirror is None:
            logger.debug("mirror_absent", extra={"repo": repo})
            continue
        try:
            listing = _git(mirror.path, "notes", "--ref=sediment", "list")
        except MirrorError:
            # No notes ref (unstamped repo) or a transient git failure — notes
            # attribution has nothing here; the session falls through to the
            # jaccard fallback.
            continue
        for line in listing.splitlines():
            parts = line.split()
            if len(parts) != 2:
                continue
            commit_sha = parts[1]  # "<note_object> <annotated_commit>"
            note = read_commit_note(mirror.path, commit_sha)
            if note is None:
                result.skipped["attribution_note_unreadable"] += 1
                continue
            for session in note.sessions:
                out[session.session_id].add(CommitRef(repo=repo, commit_sha=commit_sha))
    result.session_commits = dict(out)
    return result


def _commit_ref(commit: CommitKey, context: RepositoryContext) -> CommitRef:
    return CommitRef(
        repo=context.repo_for(commit.repository),
        commit_sha=commit.commit_sha,
        repository_identity=commit.repository.identity
        if isinstance(commit.repository, IdentifiedRepositoryKey)
        else None,
    )


def _commit_key(
    commit: CommitRef, org_id: str, context: RepositoryContext
) -> CommitKey | None:
    return context.commit_key(
        org_id,
        commit.repo,
        commit.commit_sha,
        repository_identity=commit.repository_identity,
    )


def _order_commits(
    mirrors: MirrorManager,
    org_id: str,
    commit_refs: set[CommitRef],
    *,
    repository_context: RepositoryContext,
) -> list[CommitRef]:
    """Order attributed commits into trajectory order — ascending committer
    time, ties broken by repository and sha (fact identity, ADR 0001)."""
    # Opened once per repo, not once per commit: a session's commits usually
    # share one repo.
    commits = {}
    for commit in commit_refs:
        key = _commit_key(commit, org_id, repository_context)
        if key is not None:
            commits[key] = _commit_ref(key, repository_context)
    by_repo = {
        key.repository: mirrors.open_repository(key.repository) for key in commits
    }
    return [
        commits[key]
        for key in sorted(
            commits,
            key=lambda key: (
                _commit_time(by_repo[key.repository], key.commit_sha)
                if by_repo[key.repository]
                else 0,
                commit_sort_key(key),
            ),
        )
    ]


def _commit_time(mirror: RepoMirror, sha: str) -> int:
    """The commit's committer timestamp (unix seconds), or 0 if unreadable.
    ``--end-of-options``: ``sha`` originates in webhook payloads, so it must be
    read as a revision, never a git option."""
    try:
        raw = _git(mirror.path, "show", "-s", "--format=%ct", "--end-of-options", sha)
    except MirrorError:
        logger.debug("commit_time_unavailable", extra={"commit": sha})
        return 0
    fields = raw.split()
    try:
        return int(fields[0]) if fields else 0
    except ValueError:
        return 0


def _terminal_outcomes(
    commits: list[CommitRef],
    ci_by_commit: dict[CommitKey, Iterable[CIOutcome]],
    *,
    org_id: str,
    repository_context: RepositoryContext,
) -> list[CIOutcome]:
    """Every CI outcome for the attributed commits — all of them, since a
    rollout may span several commits/runs. Ordered for stable evidence bytes by
    the commit's trajectory position, then outcome_id. Consumers must use CI
    resolution for semantic attempt order; this ordering is not a verdict.

    Selection keys on ``(repo, sha)``, never sha alone. A fork or shared-history
    repo carrying the same commit sha can't contaminate the reward with its own
    CI verdict."""
    return [
        outcome
        for commit in commits
        for outcome in sorted(
            ci_by_commit.get(_commit_key(commit, org_id, repository_context), ()),
            key=lambda item: item.outcome_id,
        )
    ]
