# SPDX-License-Identifier: AGPL-3.0-or-later
"""Observation-backed accepted Session status; absence cannot prove abandonment.

ADR 0014 retains the public result shapes for historical callers. A captured
SessionCommitObservation proves committed status. Missing observation evidence
remains attribution_unavailable, irrespective of live notes or similarity.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from sediment_core import (
    DeveloperDecision,
    FactStore,
    NonEmptyId,
    OrgId,
    Push,
    SessionCommitObservation,
)

from .attribution import Attribution, AttributionPolicy
from .gc import MirrorGCPolicy
from .inference_call import InferenceCall, inference_observed_at
from .mirror import MirrorManager
from .provenance import Provenance
from .rollout import CommitRef
from .session_commit import SESSION_COMMIT_UNOBSERVED, bind_session_commit_keys_result
from .repository_context import read_repository_context
from .repository_identity import RepositoryContext, repository_identity_evidence_of

logger = logging.getLogger("sediment.derive.abandonment")

# Emitted skips are no_accepted_decision, reached_a_commit, and
# SESSION_COMMIT_UNOBSERVED. The grace/Attribution constants and legacy Session
# shapes remain compatibility exports; absent observations never prove abandonment.
SKIP_NO_ACCEPTED_DECISION = "no_accepted_decision"
SKIP_REACHED_A_COMMIT = "reached_a_commit"
SKIP_WITHIN_GRACE_HORIZON = "within_grace_horizon"
SKIP_ATTRIBUTION_UNAVAILABLE = "attribution_unavailable"

AcceptedSessionStatus = Literal[
    "committed",
    "abandoned",
    "in_flight",
    "attribution_unavailable",
]
ACCEPTED_SESSION_STATUSES: tuple[AcceptedSessionStatus, ...] = (
    "committed",
    "abandoned",
    "in_flight",
    "attribution_unavailable",
)


@dataclass(frozen=True)
class AbandonmentPolicy:
    """Versioned factual Session status with legacy policy parsing compatibility.

    The attribution, mirror, and horizon fields remain accepted for historical
    policy documents. They cannot supply missing observation evidence.
    """

    attribution: AttributionPolicy = field(default_factory=AttributionPolicy)
    mirror_gc: MirrorGCPolicy = field(default_factory=MirrorGCPolicy)
    grace_horizon_days: int = 14
    policy_version: str = "6"

    def __post_init__(self) -> None:
        if self.grace_horizon_days < 1:
            raise ValueError(
                "AbandonmentPolicy.grace_horizon_days must be >= 1 "
                f"(got {self.grace_horizon_days}); a zero horizon calls every "
                "in-flight session abandoned."
            )


@dataclass(frozen=True)
class SessionAbandonment:
    """One session whose accepted work reached no commit — derived, never
    persisted (ADR 0001)."""

    org_id: str
    session_id: str
    accepted_decisions: int  # how many accepts went unshipped
    explicit_accepted_decisions: int  # how many were human gestures
    last_decision_at: datetime  # the anchor the horizon is measured from
    as_of: datetime  # the fact set's newest timestamp — see the module docstring
    provenance: Provenance


@dataclass(frozen=True)
class AcceptedSessionOutcome:
    """One accepted Session's terminal abandonment classification."""

    org_id: OrgId
    session_id: NonEmptyId
    accepted_decisions: int
    explicit_accepted_decisions: int
    last_decision_at: datetime
    as_of: datetime
    status: AcceptedSessionStatus
    provenance: Provenance


@dataclass
class AbandonmentResult:
    """Accepted Session outcomes plus compatible abandonment views."""

    sessions: list[SessionAbandonment] = field(default_factory=list)
    skipped: Counter[str] = field(default_factory=Counter)
    as_of: datetime | None = None
    provenance: Provenance | None = None
    outcomes: list[AcceptedSessionOutcome] = field(default_factory=list)


def _as_of(
    decisions: list[DeveloperDecision],
    pushes: list[Push],
    completions: list[InferenceCall],
) -> datetime | None:
    """The newest timestamp anywhere in the input facts.

    A function of the fact set alone — never ``datetime.now()``, which would
    make the same facts yield different verdicts on different days and break
    recomputability over all history (ADR 0001, non-negotiable rule 2).
    Both ``occurred_at`` and ``captured_at`` count: a fact captured late is
    still evidence that time has passed.
    """
    stamps: list[datetime] = []
    for d in decisions:
        stamps.append(d.occurred_at)
        stamps.append(d.captured_at)
    stamps.extend(p.captured_at for p in pushes)
    stamps.extend(inference_observed_at(call) for call in completions)
    return max(stamp.astimezone(UTC) for stamp in stamps) if stamps else None


def derive_abandonment(
    store: FactStore,
    mirrors: MirrorManager,
    org_id: str,
    policy: AbandonmentPolicy | None = None,
    *,
    attributions: Iterable[Attribution] | None = None,
    decisions: Iterable[DeveloperDecision] | None = None,
    pushes: Iterable[Push] | None = None,
    completions: Iterable[InferenceCall] | None = None,
    as_of: datetime | None = None,
    session_commits: Mapping[str, Iterable[CommitRef]] | None = None,
    policy_digest: str | None = None,
    session_commit_observations: Iterable[SessionCommitObservation] | None = None,
    repository_context: RepositoryContext | None = None,
) -> AbandonmentResult:
    """Classify accepted Sessions using captured observations at the boundary.

    Supplied Fact collections are authoritative, including empty collections.
    Legacy ``attributions`` and ``session_commits`` arguments remain accepted but
    cannot establish factual identity without source observations (ADR 0014).
    """
    with store.read_snapshot() as snapshot:
        return _derive_abandonment(
            snapshot,
            mirrors,
            org_id,
            policy,
            attributions=attributions,
            decisions=decisions,
            pushes=pushes,
            completions=completions,
            as_of=as_of,
            session_commits=session_commits,
            policy_digest=policy_digest,
            session_commit_observations=session_commit_observations,
            repository_context=repository_context,
        )


def _derive_abandonment(
    store: FactStore,
    mirrors: MirrorManager,
    org_id: str,
    policy: AbandonmentPolicy | None = None,
    *,
    attributions: Iterable[Attribution] | None = None,
    decisions: Iterable[DeveloperDecision] | None = None,
    pushes: Iterable[Push] | None = None,
    completions: Iterable[InferenceCall] | None = None,
    as_of: datetime | None = None,
    session_commits: Mapping[str, Iterable[CommitRef]] | None = None,
    policy_digest: str | None = None,
    session_commit_observations: Iterable[SessionCommitObservation] | None = None,
    repository_context: RepositoryContext | None = None,
) -> AbandonmentResult:
    """Derive abandonment inside the caller's fact snapshot."""

    if as_of is not None and as_of.utcoffset() is None:
        raise ValueError("as_of must be an aware datetime")
    if repository_context is not None:
        if repository_context.org_id != org_id or (
            as_of is not None and repository_context.as_of != as_of.astimezone(UTC)
        ):
            raise ValueError("repository context must match organization and boundary")
        as_of = repository_context.as_of
    policy = policy or AbandonmentPolicy()
    provenance = Provenance(
        policy_version=policy.policy_version,
        quarantine_revision=store.quarantine_revision(org_id),
        policy_digest=policy_digest,
    )
    result = AbandonmentResult(provenance=provenance)

    decision_facts = (
        store.read_decision_projections(org_id)
        if decisions is None
        else list(decisions)
    )
    result.as_of = as_of
    if not decision_facts:
        return result
    push_facts = store.read_pushes(org_id) if pushes is None else list(pushes)
    completion_facts = (
        store.read_inference_call_summaries(org_id)
        if completions is None
        else list(completions)
    )
    observation_facts = list(
        store.read_session_commit_observations(org_id, as_of=as_of)
        if session_commit_observations is None
        else session_commit_observations
    )
    preloaded_without_context = (
        session_commit_observations is not None and repository_context is None
    )
    context = repository_context or read_repository_context(
        store,
        org_id,
        as_of=as_of,
        supplemental_legacy_evidence=(
            repository_identity_evidence_of(item)
            for item in observation_facts
            if preloaded_without_context and item.repository_id is None
        ),
    )
    effective_as_of = as_of or max(
        context.as_of, _as_of(decision_facts, push_facts, completion_facts)
    )
    if context.as_of != effective_as_of:
        context = read_repository_context(
            store,
            org_id,
            as_of=effective_as_of,
            supplemental_legacy_evidence=(
                repository_identity_evidence_of(item)
                for item in observation_facts
                if preloaded_without_context and item.repository_id is None
            ),
        )
    result.as_of = effective_as_of
    if preloaded_without_context:
        declined = [
            item
            for item in observation_facts
            if item.repository_id is not None
            and item.org_id == org_id
            and item.captured_at.astimezone(UTC) <= effective_as_of
        ]
        if declined:
            result.skipped["repository_identity_unresolved"] += len(declined)
            logger.warning(
                "abandonment_repository_declined reason=repository_identity_unresolved count=%d",
                len(declined),
            )
        observation_facts = [
            item for item in observation_facts if item.repository_id is None
        ]
    binding_result = bind_session_commit_keys_result(
        observation_facts, org_id, as_of=effective_as_of, repository_context=context
    )
    result.skipped.update(binding_result.skipped)
    observed_sessions = {session_id for _, session_id in binding_result.bindings}
    decs_by_session: dict[str, list[DeveloperDecision]] = defaultdict(list)
    for decision in decision_facts:
        if decision.org_id == org_id and decision.captured_at.astimezone(
            UTC
        ) <= effective_as_of.astimezone(UTC):
            decs_by_session[decision.session_id].append(decision)
    for session_id, decs in sorted(decs_by_session.items()):
        accepted = [item for item in decs if item.accepted]
        if not accepted:
            result.skipped[SKIP_NO_ACCEPTED_DECISION] += 1
            continue
        observed = session_id in observed_sessions
        reason = SKIP_REACHED_A_COMMIT if observed else SESSION_COMMIT_UNOBSERVED
        result.skipped[reason] += 1
        result.outcomes.append(
            AcceptedSessionOutcome(
                org_id=org_id,
                session_id=session_id,
                accepted_decisions=len(accepted),
                explicit_accepted_decisions=sum(item.explicit for item in accepted),
                last_decision_at=max(item.occurred_at.astimezone(UTC) for item in decs),
                as_of=effective_as_of,
                status="committed" if observed else "attribution_unavailable",
                provenance=provenance,
            )
        )
    logger.info(
        "abandonment_derived org_id=%s abandoned=%d skipped=%s as_of=%s",
        org_id,
        len(result.sessions),
        dict(sorted(result.skipped.items())),
        effective_as_of.isoformat(),
    )
    return result
