# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Attribution as a pure derivation (ADR 0001).

``derive_attributions`` is a pure function of (facts, mirror, policy): it
reads the org's pushes and inference calls from the fact store, walks each push's
commits in the bare mirror, and joins commits to inference calls — notes
attribution first (the ``refs/notes/sediment`` stamp, a recorded fact), the
jaccard guess (token-overlap similarity) as the universal fallback. Nothing
here is persisted: a ``Attribution`` is derived data, recomputable over all
history, versioned by the policy that produced it.

Purity constraints (ADR 0001): every timestamp read here is a fact — the
similarity lookback windows anchor on ``push.captured_at``, never on
wall-clock now, and every tie (equal scores, equal capture times) resolves by
fact identity, never by ingest or read order — so re-running the derivation
months later, or over the same facts ingested in a different order,
reproduces the same results. All fact reads go through the store's default
(quarantine-excluding) read path, and the org's ``quarantine_revision`` folds
into each attribution's provenance alongside the policy version.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sediment_core import (
    CommitSha,
    FactStore,
    NonEmptyId,
    OrgId,
    Push,
    RepoSlug,
    normalize_commit_sha,
)
from sediment_core.store import AttributionPush

from .diff import is_code_file, parse_diff_sections
from .inference_call import (
    InferenceCall,
    inference_fact_id,
    inference_observed_at,
    render_scoring_text,
)
from .mirror import MirrorError, MirrorManager, RepoMirror
from .notes import read_commit_note
from .provenance import Provenance
from .repository_context import read_repository_context
from .repository_identity import (
    CommitKey,
    IdentifiedRepositoryKey,
    RepositoryContext,
    RepositoryKey,
    RepositoryIdentity,
    RepositoryResolution,
    repository_identity_evidence_of,
    repository_identity_of,
    repository_read_key,
)
from .session_commit import bind_session_commit_keys_result
from .scoring import JaccardScorer, Scorer
from .similarity import tokenize

logger = logging.getLogger("sediment.derive.attribution")

ATTRIBUTION_IMPLEMENTATION_VERSION = "3"

# (inference call, its token set) — tokenized once, scored many times.
_Candidate = tuple[InferenceCall, set[str]]


class AttributionSource(StrEnum):
    GIT_NOTES = "git_notes"  # deterministic, from the git-notes session stamp
    JACCARD = "jaccard"  # similarity guess, the fallback


@dataclass(frozen=True)
class SimilarityPolicy:
    """Similarity threshold and fact-anchored lookback window for one method."""

    min_similarity: float
    lookback_window_minutes: int

    def __post_init__(self) -> None:
        if not isinstance(self.min_similarity, int | float) or isinstance(
            self.min_similarity, bool
        ):
            raise ValueError("min_similarity must be a number")
        if not 0.0 <= self.min_similarity <= 1.0:
            raise ValueError(
                f"min_similarity must be between 0.0 and 1.0 "
                f"(got {self.min_similarity})"
            )
        if (
            type(self.lookback_window_minutes) is not int
            or self.lookback_window_minutes <= 0
        ):
            raise ValueError(
                "lookback_window_minutes must be a positive integer "
                f"(got {self.lookback_window_minutes})"
            )


def _git_notes_policy() -> SimilarityPolicy:
    return SimilarityPolicy(min_similarity=0.3, lookback_window_minutes=10080)


def _jaccard_policy() -> SimilarityPolicy:
    return SimilarityPolicy(min_similarity=0.7, lookback_window_minutes=60)


@dataclass(frozen=True)
class AttributionPolicy:
    """The tunable attribution semantics — what counts as a match and how far
    back to look. ``policy_version`` stamps every attribution's provenance;
    bump it when tuning any knob so derived datasets stay distinguishable."""

    # Include inference calls captured shortly after a push webhook.
    post_push_grace_period_minutes: int = 10
    max_commits_per_push: int = 20
    # A git note proves the session-to-commit edge. The longer window finds the
    # named session, and similarity ranks its inference calls.
    git_notes: SimilarityPolicy = field(default_factory=_git_notes_policy)
    # Jaccard searches across the organization, so it uses a narrower window
    # and a higher threshold to limit false positives.
    jaccard: SimilarityPolicy = field(default_factory=_jaccard_policy)
    policy_version: str = ATTRIBUTION_IMPLEMENTATION_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.post_push_grace_period_minutes) is not int
            or self.post_push_grace_period_minutes < 0
        ):
            raise ValueError(
                "post_push_grace_period_minutes must be a non-negative integer "
                f"(got {self.post_push_grace_period_minutes})"
            )
        if type(self.max_commits_per_push) is not int or self.max_commits_per_push <= 0:
            raise ValueError(
                "max_commits_per_push must be a positive integer "
                f"(got {self.max_commits_per_push})"
            )


@dataclass(frozen=True)
class Attribution:
    """The derived join between a completion and a commit — NOT a fact, never
    persisted (ADR 0001). Identity is (repo, commit_sha, file_path): the
    derivation emits at most one attribution per changed file, and a git-notes
    attribution always supersedes a similarity guess for the same key."""

    org_id: OrgId
    repo: RepoSlug
    commit_sha: CommitSha
    file_path: str
    inference_call_id: NonEmptyId
    session_id: NonEmptyId  # the matched completion's REAL session (ADR 0002)
    similarity_score: float
    attribution_source: AttributionSource
    provenance: Provenance
    repository_identity: RepositoryIdentity | None = None
    source_push_id: NonEmptyId | None = None


@dataclass
class AttributionResult:
    """Attributions plus the closed tally of inputs that couldn't derive."""

    attributions: list[Attribution]
    skipped: Counter[str]

    def __init__(self) -> None:
        self.attributions = []
        self.skipped = Counter()


def derive_commit_attributions(
    store: FactStore,
    mirrors: MirrorManager,
    org_id: OrgId,
    commit_sha: CommitSha,
    policy: AttributionPolicy | None = None,
    *,
    repository_context: RepositoryContext,
    note_session_ids_by_commit: Mapping[CommitKey, frozenset[str]],
    repository_key: RepositoryKey | None = None,
    scorer: Scorer | None = None,
    policy_digest: str | None = None,
) -> list[Attribution]:
    """Score one commit after finding its earliest eligible stored Push owners.

    The caller supplies complete repository metadata and captured note bindings
    from the same snapshot. An empty note map is authoritative. Stored Push
    projections qualify through their captured identity and name; arbitrary
    preloaded Facts must continue using the stricter full Derivation API.
    """
    commit_sha = normalize_commit_sha(commit_sha)
    if repository_context.org_id != org_id:
        raise ValueError("repository context must match organization")
    if repository_key is not None:
        repository_read_key(repository_key)
        if repository_key.org_id != org_id:
            raise ValueError("repository selector must match organization")
    policy = policy or AttributionPolicy()
    scorer = scorer or JaccardScorer()
    keys = (
        (repository_key,)
        if repository_key is not None
        else repository_context.repository_keys()
    )
    with store.read_snapshot() as snapshot:
        owners = []
        result = AttributionResult()
        for key in keys:
            try:
                repo = repository_context.repo_for(key)
            except KeyError:
                result.skipped["repository_identity_unresolved"] += 1
                continue
            mirror = mirrors.open_repository(key)
            if mirror is None:
                result.skipped["mirror_absent"] += 1
                logger.debug("mirror_absent", extra={"repo": repo})
                continue
            if not mirror.commit_exists(commit_sha):
                continue
            with closing(
                snapshot.iter_attribution_pushes(
                    org_id,
                    captured_through=repository_context.as_of,
                    repository_key=repository_read_key(key),
                )
            ) as pushes:
                for push in pushes:
                    resolved = repository_context.resolve_reference(
                        push.org_id,
                        push.repo,
                        repository_identity=repository_identity_of(push),
                    )
                    if resolved.key != key:
                        result.skipped[resolved.reason] += 1
                        logger.warning(
                            "Attribution repository declined reason=%s count=1",
                            resolved.reason,
                        )
                        continue
                    if push.after_sha != commit_sha:
                        if (
                            push.forced
                            or not push.before_sha.strip("0")
                            or push.before_sha in (commit_sha, push.after_sha)
                        ):
                            continue
                        if commit_sha not in mirror.list_push_commits(
                            push, policy.max_commits_per_push
                        ):
                            continue
                    # Ownership precedes candidate availability and matching.
                    owners.append((push, resolved, mirror))
                    break
        provenance = Provenance(
            policy_version=policy.policy_version,
            quarantine_revision=snapshot.quarantine_revision(org_id),
            policy_digest=policy_digest,
        )
        for push, resolved, mirror in sorted(
            owners, key=lambda item: (item[0].captured_at, item[0].push_id)
        ):
            result.attributions.extend(
                _attribute_owned_commit(
                    snapshot,
                    mirror,
                    push,
                    commit_sha,
                    resolved,
                    policy,
                    scorer,
                    provenance,
                    repository_context.as_of,
                    note_session_ids_by_commit.get(
                        CommitKey(resolved.key, commit_sha), frozenset()
                    ),
                    result.skipped,
                )
            )
        for reason, count in sorted(result.skipped.items()):
            logger.debug("Commit Attribution skipped reason=%s count=%d", reason, count)
        return result.attributions


def _attribute_owned_commit(
    store,
    mirror: RepoMirror,
    push: AttributionPush,
    commit_sha: CommitSha,
    resolved: RepositoryResolution,
    policy: AttributionPolicy,
    scorer: Scorer,
    provenance: Provenance,
    as_of: datetime,
    note_session_ids: frozenset[str],
    skipped: Counter[str],
) -> list[Attribution]:
    """Materialize one owner's exact population, releasing it before the next."""
    upper = min(
        as_of,
        push.captured_at + timedelta(minutes=policy.post_push_grace_period_minutes),
    )
    calls = store.read_attribution_candidates(
        push.org_id,
        observed_between=(
            push.captured_at
            - timedelta(minutes=policy.jaccard.lookback_window_minutes),
            upper,
        ),
        note_session_ids=set(note_session_ids),
        notes_observed_between=(
            push.captured_at
            - timedelta(minutes=policy.git_notes.lookback_window_minutes),
            upper,
        ),
    )
    candidates = [(call, tokenize(render_scoring_text(call))) for call in calls]
    if not candidates:
        skipped["no_inference_call_candidates"] += 1
        return []
    return _attribute_commit(
        mirror,
        push,
        commit_sha,
        _window(candidates, push, policy.jaccard.lookback_window_minutes, policy),
        _window(candidates, push, policy.git_notes.lookback_window_minutes, policy),
        policy,
        provenance,
        scorer,
        skipped,
        repository_resolution=resolved,
        note_session_ids=note_session_ids,
    )


def derive_attributions(
    store: FactStore,
    mirrors: MirrorManager,
    org_id: str,
    policy: AttributionPolicy | None = None,
    *,
    pushes: list[Push] | None = None,
    scorer: Scorer | None = None,
    policy_digest: str | None = None,
    candidates: list[_Candidate] | None = None,
    note_sessions_by_commit: dict[tuple[str, str], frozenset[str]] | None = None,
    note_session_ids_by_commit: Mapping[CommitKey, frozenset[str]] | None = None,
    repository_context: RepositoryContext | None = None,
    as_of: datetime | None = None,
) -> list[Attribution]:
    """Derive the org's attributions from its facts and mirrors.

    ``pushes`` narrows the scope to the given push facts — the push trigger's
    shape (one webhook derives its own push, not the repo's whole history).
    Default: every push the store holds for the org. Inference calls are read
    once (quarantine-excluded) and only tokenized inside the scope's window
    union; each pushed repo's mirror is opened read-only (never fetches —
    derivations must not touch the network). A repo that was never mirrored,
    or a commit whose objects are gone (force-pushed away, pruned), is
    skipped with a log line, never an error: the facts remain and a later
    re-derivation picks up whatever the mirror then holds.

    ``candidates`` supplies a pre-built, pre-tokenized completion set (the
    shape ``_scope_candidates`` returns), skipping the internal full-table
    read and tokenization. Callers that already built an org-wide set —
    ``derive_attribution_share`` iterating repos — pass it here so the read
    cost stops scaling with repo count. Default ``None``: the
    derivation scopes and tokenizes the org's completions itself.

    A commit reachable from several pushes attributes once, against the
    earliest push's window (closest to when the commit was made). A scoped
    run therefore matches the full derivation except for commits an earlier,
    out-of-scope push already carried — acceptable for the trigger's log
    line; the full-scope run is the authority.
    """
    return derive_attribution_result(
        store,
        mirrors,
        org_id,
        policy,
        pushes=pushes,
        scorer=scorer,
        policy_digest=policy_digest,
        candidates=candidates,
        note_sessions_by_commit=note_sessions_by_commit,
        note_session_ids_by_commit=note_session_ids_by_commit,
        repository_context=repository_context,
        as_of=as_of,
    ).attributions


def derive_attribution_result(
    store: FactStore,
    mirrors: MirrorManager,
    org_id: str,
    policy: AttributionPolicy | None = None,
    *,
    pushes: list[Push] | None = None,
    scorer: Scorer | None = None,
    policy_digest: str | None = None,
    candidates: list[_Candidate] | None = None,
    note_sessions_by_commit: dict[tuple[str, str], frozenset[str]] | None = None,
    note_session_ids_by_commit: Mapping[CommitKey, frozenset[str]] | None = None,
    repository_context: RepositoryContext | None = None,
    as_of: datetime | None = None,
) -> AttributionResult:
    """Derive attributions and count every fail-soft attribution skip."""

    with store.read_snapshot() as snapshot:
        return _derive_attribution_result(
            snapshot,
            mirrors,
            org_id,
            policy,
            pushes=pushes,
            scorer=scorer,
            policy_digest=policy_digest,
            candidates=candidates,
            note_sessions_by_commit=note_sessions_by_commit,
            note_session_ids_by_commit=note_session_ids_by_commit,
            repository_context=repository_context,
            as_of=as_of,
        )


def _derive_attribution_result(
    store: FactStore,
    mirrors: MirrorManager,
    org_id: str,
    policy: AttributionPolicy | None = None,
    *,
    pushes: list[Push] | None = None,
    scorer: Scorer | None = None,
    policy_digest: str | None = None,
    candidates: list[_Candidate] | None = None,
    note_sessions_by_commit: dict[tuple[str, str], frozenset[str]] | None = None,
    note_session_ids_by_commit: Mapping[CommitKey, frozenset[str]] | None = None,
    repository_context: RepositoryContext | None = None,
    as_of: datetime | None = None,
) -> AttributionResult:
    """Derive attributions inside the caller's fact snapshot."""

    result = AttributionResult()
    if note_sessions_by_commit is not None and note_session_ids_by_commit is not None:
        raise ValueError("choose one repository-qualified note map")
    if as_of is not None and as_of.utcoffset() is None:
        raise ValueError("as_of must be an aware datetime")
    if as_of is None and repository_context is not None:
        as_of = repository_context.as_of
    preloaded_without_context = pushes is not None and repository_context is None
    repository_context = repository_context or read_repository_context(
        store,
        org_id,
        as_of=as_of,
        supplemental_legacy_evidence=(
            repository_identity_evidence_of(push)
            for push in pushes or ()
            if push.repository_id is None
        ),
    )
    if repository_context.org_id != org_id or (
        as_of is not None and repository_context.as_of != as_of.astimezone(UTC)
    ):
        raise ValueError("repository context must match organization and boundary")
    policy = policy or AttributionPolicy()
    scorer = scorer or JaccardScorer()
    provenance = Provenance(
        policy_version=policy.policy_version,
        quarantine_revision=store.quarantine_revision(org_id),
        policy_digest=policy_digest,
    )
    if pushes is None:
        pushes = store.read_pushes(org_id)
    # Order by fact identity, not read/arrival order (ADR 0001): captured_at
    # ties break on push_id, so which push's window claims a shared commit is
    # a function of the facts alone.
    pushes = sorted(
        (
            push
            for push in pushes
            if push.org_id == org_id
            and (
                as_of is None
                or push.captured_at.astimezone(UTC) <= as_of.astimezone(UTC)
            )
        ),
        key=lambda p: (p.captured_at.astimezone(UTC), p.push_id),
    )
    resolved_pushes = []
    for push in pushes:
        if preloaded_without_context and push.repository_id is not None:
            resolved = RepositoryResolution(reason="repository_identity_unresolved")
        else:
            resolved = repository_context.resolve_fact(push)
        if resolved.key is None:
            result.skipped[resolved.reason] += 1
            logger.warning(
                "Attribution repository declined reason=%s count=1", resolved.reason
            )
        else:
            resolved_pushes.append((push, resolved))
    pushes = [push for push, _ in resolved_pushes]
    if not pushes:
        return result

    # An empty candidate set skips the whole git walk. A caller-supplied
    # org-wide set covers the org's widest window; an individually empty
    # scope then falls through to the per-commit no_match skip. Output stays
    # identical to deriving with a scope-local candidate set.
    if candidates is None:
        candidates = _scope_candidates(store, org_id, pushes, policy)
    candidates = [
        (call, tokens)
        for call, tokens in candidates
        if getattr(call, "org_id", org_id) == org_id
        and (
            as_of is None
            or inference_observed_at(call).astimezone(UTC) <= as_of.astimezone(UTC)
        )
    ]
    if not candidates:
        result.skipped["no_inference_call_candidates"] += len(pushes)
        return result

    qualified_notes = note_session_ids_by_commit
    if note_sessions_by_commit is not None:
        qualified_notes, skipped = _qualify_legacy_note_sessions(
            note_sessions_by_commit, repository_context, org_id
        )
        result.skipped.update(skipped)
    captured_notes = {}
    if qualified_notes is None and any(
        isinstance(row.key, IdentifiedRepositoryKey) for _, row in resolved_pushes
    ):
        bindings = bind_session_commit_keys_result(
            store.read_session_commit_observations(
                org_id, as_of=repository_context.as_of
            ),
            org_id,
            as_of=repository_context.as_of,
            repository_context=repository_context,
        )
        result.skipped.update(bindings.skipped)
        for commit, session_id in bindings.bindings:
            captured_notes.setdefault(commit, set()).add(session_id)
    seen: set[CommitKey] = set()
    for push, resolved in resolved_pushes:
        mirror = mirrors.open_repository(resolved.key)
        if mirror is None:
            result.skipped["mirror_absent"] += 1
            logger.debug("mirror_absent", extra={"repo": push.repo})
            continue
        # Both windows depend only on (push, policy) — computed once per
        # push, not once per commit.
        org_candidates = _window(
            candidates, push, policy.jaccard.lookback_window_minutes, policy
        )
        git_notes_candidates = _window(
            candidates, push, policy.git_notes.lookback_window_minutes, policy
        )
        for sha in mirror.list_push_commits(push, policy.max_commits_per_push):
            commit_key = CommitKey(resolved.key, sha)
            if commit_key in seen:
                continue
            seen.add(commit_key)
            result.attributions += _attribute_commit(
                mirror,
                push,
                sha,
                org_candidates,
                git_notes_candidates,
                policy,
                provenance,
                scorer,
                result.skipped,
                repository_resolution=resolved,
                note_session_ids=(
                    qualified_notes.get(commit_key, frozenset())
                    if qualified_notes is not None
                    else frozenset(captured_notes.get(commit_key, ()))
                    if isinstance(resolved.key, IdentifiedRepositoryKey)
                    else None
                ),
            )
    return result


def _qualify_legacy_note_sessions(
    notes: Mapping[tuple[str, str], frozenset[str]],
    repository_context: RepositoryContext,
    org_id: OrgId,
) -> tuple[dict[CommitKey, frozenset[str]], Counter[str]]:
    """Resolve legacy map entries once, declining every conflicting alias entry."""
    grouped: dict[CommitKey, list[frozenset[str]]] = {}
    skipped = Counter()
    for (repo, sha), sessions in notes.items():
        resolution = repository_context.resolve_reference(org_id, repo)
        if resolution.key is None:
            skipped[resolution.reason] += 1
            continue
        try:
            key = CommitKey(resolution.key, sha)
        except ValueError:
            skipped["repository_identity_unresolved"] += 1
            continue
        grouped.setdefault(key, []).append(sessions)
    qualified = {}
    for key, entries in grouped.items():
        if any(sessions != entries[0] for sessions in entries[1:]):
            skipped["repository_identity_conflict"] += len(entries)
        else:
            qualified[key] = entries[0]
    for reason, count in sorted(skipped.items()):
        logger.warning(
            "Attribution note map declined reason=%s count=%d", reason, count
        )
    return qualified, skipped


def _attribute_commit(
    mirror: RepoMirror,
    push: Push | AttributionPush,
    commit_sha: str,
    org_candidates: list[_Candidate],
    git_notes_candidates: list[_Candidate],
    policy: AttributionPolicy,
    provenance: Provenance,
    scorer: Scorer,
    skipped: Counter[str],
    *,
    repository_resolution: RepositoryResolution,
    note_session_ids: frozenset[str] | None = None,
) -> list[Attribution]:
    """Attribute one commit's changed code files. Per file, git-notes attribution
    first: the note proves the session→commit edge, so a match within the
    noted sessions supersedes any org-wide guess — even a stronger-scoring
    one. Only a file the note leaves unmatched falls to the jaccard guess."""
    try:
        raw_diff = mirror.fetch_commit_diff(push.repo, commit_sha)
    except MirrorError as exc:
        skipped["commit_diff_unavailable"] += 1
        logger.warning(
            "commit_diff_unavailable",
            extra={"repo": push.repo, "commit": commit_sha, "error": str(exc)},
        )
        return []
    parsed = parse_diff_sections(raw_diff)
    skipped.update(parsed.skipped)
    scored_files = [
        (fd, tokenize(fd.added_lines))
        for fd in parsed.sections
        if is_code_file(fd.file_path) and fd.added_lines.strip()
    ]
    if not scored_files:
        if not parsed.skipped:
            skipped["no_code_diff"] += 1
        return []

    session_ids: set[str] | frozenset[str] = frozenset()
    if note_session_ids is None:
        note = read_commit_note(mirror.path, commit_sha)  # None → jaccard
        if note is not None:
            session_ids = {session.session_id for session in note.sessions}
    else:
        session_ids = note_session_ids
    session_candidates: list[_Candidate] = []
    if session_ids:
        session_candidates = [
            (c, t) for c, t in git_notes_candidates if c.session_id in session_ids
        ]

    out: list[Attribution] = []
    for file_diff, diff_tokens in scored_files:
        source = AttributionSource.GIT_NOTES
        best = _best_match(
            session_candidates, diff_tokens, policy.git_notes.min_similarity, scorer
        )
        if best is None:
            source = AttributionSource.JACCARD
            best = _best_match(
                org_candidates, diff_tokens, policy.jaccard.min_similarity, scorer
            )
        if best is None:
            skipped["no_match"] += 1
            continue
        completion, score = best
        out.append(
            Attribution(
                org_id=push.org_id,
                repo=repository_resolution.repo,
                commit_sha=commit_sha,
                file_path=file_diff.file_path,
                inference_call_id=inference_fact_id(completion),
                session_id=completion.session_id,
                similarity_score=score,
                attribution_source=source,
                provenance=provenance,
                repository_identity=(
                    repository_resolution.key.identity
                    if isinstance(repository_resolution.key, IdentifiedRepositoryKey)
                    else None
                ),
                source_push_id=push.push_id,
            )
        )
    return out


def _scope_candidates(
    store: FactStore,
    org_id: str,
    pushes: list[Push],
    policy: AttributionPolicy,
    *,
    limit: int | None = None,
) -> list[_Candidate]:
    """The org's inference calls inside the scope's widest possible window,
    tokenized once. Shared with ``attribution_share.py``, which needs the same
    population to decide which commits were agent-plausible.

    PostgreSQL applies the absolute inclusive window in SQL and selects only
    response content plus join fields.
    """
    widest = max(
        policy.jaccard.lookback_window_minutes,
        policy.git_notes.lookback_window_minutes,
    )
    lo = min(p.captured_at for p in pushes) - timedelta(minutes=widest)
    hi = max(p.captured_at for p in pushes) + timedelta(
        minutes=policy.post_push_grace_period_minutes
    )
    read_kwargs: dict[str, object] = {"observed_between": (lo, hi)}
    if limit is not None:
        read_kwargs["limit"] = limit
    calls = store.read_attribution_candidates(org_id, **read_kwargs)
    return [(call, tokenize(render_scoring_text(call))) for call in calls]


def _window(
    candidates: list[_Candidate],
    push: Push | AttributionPush,
    minutes: int,
    policy: AttributionPolicy,
) -> list[_Candidate]:
    """Inference calls captured in the ``minutes`` before the push (plus the
    capture slack after it). Anchored on the push fact's captured_at — not
    wall-clock now — so the window is identical on every re-derivation
    (ADR 0001)."""
    lo = push.captured_at - timedelta(minutes=minutes)
    hi = push.captured_at + timedelta(minutes=policy.post_push_grace_period_minutes)
    return [
        (call, tokens)
        for call, tokens in candidates
        if lo <= inference_observed_at(call) <= hi
    ]


def _best_match(
    candidates: list[_Candidate],
    diff_tokens: set[str],
    floor: float,
    scorer: Scorer,
) -> tuple[InferenceCall, float] | None:
    """The completion whose text best matches the file's added lines, or None
    when nothing reaches the floor. Ties (equal score) resolve by earliest
    captured_at, then smallest inference_call_id — fact identity, never ingest or
    read order (ADR 0001)."""
    best: InferenceCall | None = None
    best_score = 0.0
    for completion, completion_tokens in candidates:
        score = scorer.score(completion_tokens, diff_tokens)
        # <= 0.0: zero overlap is never a match, even under a zero floor.
        if score <= 0.0 or score < floor:
            continue
        if (
            best is None
            or score > best_score
            or (
                score == best_score
                and (
                    inference_observed_at(completion),
                    inference_fact_id(completion),
                )
                < (inference_observed_at(best), inference_fact_id(best))
            )
        ):
            best, best_score = completion, score
    if best is None:
        return None
    return best, best_score
