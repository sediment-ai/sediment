# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Shared signal-attachment core (ADR 0004): the two joins the rollout and
attributed_completion derivations both need — a developer decision joined to its completion
by ``call_id`` (unique-or-drop, the zero-poisoning rule), and CI outcomes
indexed by ``(repo, commit_sha)`` so a fork or shared-history repo carrying
the same commit_sha can never contaminate the wrong derivation's reward.

Lives once, in ``sediment_derive``, so the join semantics cannot fork between
``rollout.py`` (session scope: called once per session, over that session's
own completions) and ``sediment_export.attributed_completions`` (completion
scope: called once over the org's whole completion population). Both callers
pick the candidate population. After checking uniqueness over that population,
the join requires the decision and selected call to share organization and Session.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC
import logging
from typing import Literal
from types import MappingProxyType

from sediment_core import CIOutcome, AgentHarness, DeveloperDecision, NonEmptyId, OrgId
from sediment_core.store import CIOutcomeProjection, InferenceCallIdentity

from .inference_call import InferenceCall, inference_fact_id, model_call_ids
from .repository_identity import (
    CommitKey,
    IdentifiedRepositoryKey,
    RepositoryContext,
    RepositoryIdentitySkipReason,
    build_repository_context,
    commit_sort_key,
    repository_identity_evidence_of,
)


logger = logging.getLogger(__name__)

DecisionAttachmentSkipReason = Literal[
    "missing_decision_call_id",
    "unmatched_decision_call_id",
    "ambiguous_decision_call_id",
    "decision_org_mismatch",
    "decision_session_mismatch",
]
DECISION_ATTACHMENT_SKIP_REASONS: tuple[DecisionAttachmentSkipReason, ...] = (
    "missing_decision_call_id",
    "unmatched_decision_call_id",
    "ambiguous_decision_call_id",
    "decision_org_mismatch",
    "decision_session_mismatch",
)


@dataclass
class DecisionAttachmentResult:
    """Decision attachments plus counted unique-or-drop losses."""

    decisions_by_completion: dict[str, list[DeveloperDecision]] = field(
        default_factory=dict
    )
    skipped: Counter[DecisionAttachmentSkipReason] = field(default_factory=Counter)


def drop_subsumed_codex_decisions(
    decisions: Iterable[DeveloperDecision],
) -> list[DeveloperDecision]:
    """Collapse a Codex decision-only redelivery's synthetic ``file_path=""``
    row when keyed per-file siblings already carry the same decision.

    A Codex redelivery whose ``tool_result`` straddled an export batch can
    store one ``file_path=""`` row next to N per-file fan-out rows from the
    same decision record. The collapse is asymmetric: ``""`` is the same
    decision as *any* one of the keyed rows, but the keyed rows stay distinct
    from each other — no UNIQUE index can express that, so the fact store
    legitimately keeps both shapes (ADR 0001 — never mutate facts). This is a
    read-time derivation rule, not a fact-store fix.

    "The same decision" is matched on the decision's full natural key minus
    ``file_path`` — ``(org_id, session_id, accepted, explicit, interaction_mode,
    occurred_at, call_id)`` — not on ``call_id`` alone. A redelivered record
    necessarily agrees on all of these (same record bytes, same verdict, same
    event time), so the redelivery shape still collapses; a *genuinely distinct*
    decision on the same ``call_id`` — Codex's reject convention emits
    ``file_path=""`` (see ``otlp.py``), and a deny can precede an approved
    per-file fan-out when the call is re-prompted via the hook/policy path —
    differs in ``accepted``/``occurred_at`` and survives. Keying on
    ``call_id`` alone would silently drop that real reject: label-flipping
    poisoning, the exact failure zero-poisoning exists to prevent.

    Scoped to ``AgentHarness.CODEX`` only — other agent harnesses don't produce
    this redelivery shape, and an empty ``file_path`` there is a genuine
    no-path decision (e.g. a reject with no file attached) that must not be
    dropped just because it shares a ``call_id`` with keyed rows.

    Groups with only a ``file_path=""`` row (no keyed sibling) are untouched
    — that is a genuine no-path decision, not a subsumed redelivery."""

    def _key(d: DeveloperDecision) -> tuple:
        return (
            d.org_id,
            d.session_id,
            d.accepted,
            d.explicit,
            d.interaction_mode,
            d.occurred_at.astimezone(UTC),
            d.call_id,
        )

    decisions = list(decisions)
    keyed_groups = {
        _key(d)
        for d in decisions
        if d.agent_harness == AgentHarness.CODEX
        and d.call_id is not None
        and d.file_path != ""
    }
    return [
        d
        for d in decisions
        if not (
            d.agent_harness == AgentHarness.CODEX
            and d.file_path == ""
            and d.call_id is not None
            and _key(d) in keyed_groups
        )
    ]


def join_decisions_by_call_id(
    completions: Iterable[InferenceCall | InferenceCallIdentity],
    decisions: Iterable[DeveloperDecision],
) -> dict[str, list[DeveloperDecision]]:
    """inference_call_id -> its decisions, joined by ``call_id`` under
    unique-or-drop: a decision attaches only when its ``call_id`` resolves to
    exactly one completion in ``completions`` and both Facts share ``org_id``
    and ``session_id``. A completion is reachable
    under its own gateway ``call_id`` and under each of its response
    ``tool_calls[].id`` — the agent-side tool-use id a decision
    actually carries. The gateway and tool-use ids occupy different
    namespaces. An id shared by several
    completions is ambiguous — the decision is dropped rather than
    mis-attributed (zero-poisoning, ADR 0004's namesake rule). Keyless
    decisions (no ``call_id``) never join. Several decisions may share one
    completion (e.g. an accept and a per-file reject) — that is not ambiguity.

    Before joining, a Codex decision-only redelivery's subsumed
    ``file_path=""`` row is collapsed away in favor of its keyed per-file
    siblings — see ``drop_subsumed_codex_decisions``.

    ``completions`` defines the scope of the uniqueness check: a caller
    scoped to one session (rollout.py) sees ambiguity only within that
    session's completions; a caller scoped to the whole org (attributed-completion assembly)
    sees it org-wide. Neither is "more correct" — each derivation passes the
    population its own zero-poisoning guarantee is defined over. Organization
    mismatch takes precedence over Session mismatch after identity resolution."""
    return join_decisions_by_call_id_result(
        completions, decisions
    ).decisions_by_completion


def join_decisions_by_call_id_result(
    completions: Iterable[InferenceCall | InferenceCallIdentity],
    decisions: Iterable[DeveloperDecision],
) -> DecisionAttachmentResult:
    """Join decisions and count every unique-or-drop attachment loss."""

    completions_by_call_id: dict[str, list[InferenceCall | InferenceCallIdentity]] = (
        defaultdict(list)
    )
    for c in completions:
        # One entry per distinct id: a tool-call id repeating within the same
        # completion (or equalling its gateway call_id) must not make the
        # completion its own "ambiguous" duplicate.
        for call_id in model_call_ids(c):
            completions_by_call_id[call_id].append(c)

    result = DecisionAttachmentResult()
    for d in drop_subsumed_codex_decisions(decisions):
        matches = completions_by_call_id.get(d.call_id, ())
        reason: DecisionAttachmentSkipReason
        if d.call_id is None:
            reason = "missing_decision_call_id"
        elif not matches:
            reason = "unmatched_decision_call_id"
        elif len(matches) > 1:
            reason = "ambiguous_decision_call_id"
        elif matches[0].org_id != d.org_id:
            reason = "decision_org_mismatch"
        elif matches[0].session_id != d.session_id:
            reason = "decision_session_mismatch"
        else:
            result.decisions_by_completion.setdefault(
                inference_fact_id(matches[0]), []
            ).append(d)
            continue
        result.skipped[reason] += 1
        logger.warning(
            "decision attachment declined reason=%s decision_id=%s call_id=%s "
            "org_id=%s session_id=%s",
            reason,
            d.decision_id,
            d.call_id,
            d.org_id,
            d.session_id,
        )

    for attached in result.decisions_by_completion.values():
        attached.sort(
            key=lambda decision: (
                decision.occurred_at.astimezone(UTC),
                decision.decision_id,
            )
        )
    return result


@dataclass(frozen=True)
class CIOutcomeIndexResult:
    """Qualified CI attachments and declined source-outcome counts."""

    outcomes_by_commit: Mapping[CommitKey, tuple[CIOutcome | CIOutcomeProjection, ...]]
    skipped: Mapping[RepositoryIdentitySkipReason, int]
    declined_sources: frozenset[tuple[OrgId, NonEmptyId]]


def index_ci_outcomes_by_commit_key_result(
    ci_outcomes: Iterable[CIOutcome | CIOutcomeProjection],
    *,
    repository_context: RepositoryContext | None = None,
) -> CIOutcomeIndexResult:
    """Retain exact eligible sources; absent context permits only legacy islands.

    A context declares organization and capture scope. Without one, each supplied
    organization's Facts declare its local population, never external completeness.
    """
    outcomes = tuple(ci_outcomes)
    if repository_context is not None:
        outcomes = tuple(
            item
            for item in outcomes
            if item.org_id == repository_context.org_id
            and item.captured_at.astimezone(UTC) <= repository_context.as_of
        )
        contexts = {repository_context.org_id: repository_context}
    else:
        by_org = defaultdict(list)
        for item in outcomes:
            by_org[item.org_id].append(item)
        contexts = {
            org_id: build_repository_context(
                (repository_identity_evidence_of(item) for item in population),
                (),
                org_id,
                as_of=max(item.captured_at.astimezone(UTC) for item in population),
            )
            for org_id, population in by_org.items()
        }
    grouped = defaultdict(list)
    skipped = Counter()
    declined_sources = set()
    for item in outcomes:
        resolved = contexts[item.org_id].resolve_fact(item)
        reason = resolved.reason
        if repository_context is None and isinstance(
            resolved.key, IdentifiedRepositoryKey
        ):
            reason = "repository_identity_unresolved"
        if reason is not None:
            skipped[reason] += 1
            declined_sources.add((item.org_id, item.outcome_id))
            continue
        grouped[CommitKey(resolved.key, item.commit_sha)].append(item)
    result = {
        key: tuple(
            sorted(
                grouped[key],
                key=lambda item: (item.captured_at.astimezone(UTC), item.outcome_id),
            )
        )
        for key in sorted(grouped, key=commit_sort_key)
    }
    for reason, count in sorted(skipped.items()):
        logger.warning(
            "CI repository attachment declined reason=%s count=%d", reason, count
        )
    return CIOutcomeIndexResult(
        MappingProxyType(result),
        MappingProxyType(dict(sorted(skipped.items()))),
        frozenset(declined_sources),
    )


def index_ci_outcomes_by_repo_commit(
    ci_outcomes: Iterable[CIOutcome | CIOutcomeProjection],
) -> dict[tuple[str, str], list[CIOutcome | CIOutcomeProjection]]:
    """Legacy-only tuple adapter; refuse to flatten identified or tenant keys."""
    indexed = index_ci_outcomes_by_commit_key_result(ci_outcomes)
    by_repo_commit = {}
    owners = {}
    conflicts = set()
    for commit, items in indexed.outcomes_by_commit.items():
        key = (commit.repository.repo, commit.commit_sha)
        if key in owners and owners[key] != commit.repository.org_id:
            conflicts.add(key)
        owners[key] = commit.repository.org_id
        by_repo_commit.setdefault(key, []).extend(items)
    declined = sum(len(by_repo_commit.pop(key)) for key in conflicts)
    if declined:
        logger.warning(
            "CI repository attachment declined reason=repository_identity_unresolved count=%d",
            declined,
        )
    return by_repo_commit
