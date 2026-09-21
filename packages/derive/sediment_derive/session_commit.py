# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure historical Session-to-commit observation binding (ADR 0014)."""

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
import logging
from types import MappingProxyType

from sediment_core import NonEmptyId, OrgId, SessionCommitObservation

from .repository_identity import (
    CommitKey,
    LegacyRepositoryKey,
    RepositoryContext,
    RepositoryIdentitySkipReason,
    build_repository_context,
    commit_sort_key,
    repository_identity_evidence_of,
)

SESSION_COMMIT_UNOBSERVED = "session_commit_unobserved"
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SessionCommitBindingResult:
    """Qualified edges and declined observation counts, retaining exact Facts."""

    bindings: Mapping[
        tuple[CommitKey, NonEmptyId], tuple[SessionCommitObservation, ...]
    ]
    skipped: Mapping[RepositoryIdentitySkipReason, int]


def bind_session_commit_keys_result(
    observations: Iterable[SessionCommitObservation],
    org_id: OrgId,
    *,
    as_of: datetime,
    repository_context: RepositoryContext,
) -> SessionCommitBindingResult:
    """Resolve each eligible observation once through its captured source proof."""
    if as_of.utcoffset() is None:
        raise ValueError("as_of must be an aware datetime")
    boundary = as_of.astimezone(UTC)
    if repository_context.org_id != org_id or repository_context.as_of != boundary:
        raise ValueError("repository context must match organization and boundary")
    grouped = {}
    skipped = Counter()
    for item in observations:
        if item.org_id != org_id or item.captured_at.astimezone(UTC) > boundary:
            continue
        resolved = repository_context.resolve_fact(item)
        if resolved.key is None:
            skipped[resolved.reason] += 1
            continue
        key = (CommitKey(resolved.key, item.commit_sha), item.session_id)
        grouped.setdefault(key, []).append(item)
    bindings = {
        key: tuple(
            sorted(
                grouped[key],
                key=lambda item: (
                    item.captured_at.astimezone(UTC),
                    item.observation_id,
                ),
            )
        )
        for key in sorted(grouped, key=lambda key: (commit_sort_key(key[0]), key[1]))
    }
    for reason, count in sorted(skipped.items()):
        logger.warning(
            "Session commit binding declined reason=%s count=%d", reason, count
        )
    return SessionCommitBindingResult(
        MappingProxyType(bindings), MappingProxyType(dict(sorted(skipped.items())))
    )


def bind_session_commits(
    observations: Iterable[SessionCommitObservation],
    org_id: str,
    *,
    as_of: datetime,
) -> dict[tuple[str, str, str], tuple[SessionCommitObservation, ...]]:
    """Legacy-only adapter keyed by (repository name, commit, Session).

    The supplied population declares legacy scope, not complete external history.
    Identified callers must use the qualified result with a complete context.
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be an aware datetime")
    boundary = as_of.astimezone(UTC)
    eligible = tuple(
        item
        for item in observations
        if item.org_id == org_id and item.captured_at.astimezone(UTC) <= boundary
    )
    context = build_repository_context(
        (repository_identity_evidence_of(item) for item in eligible),
        (),
        org_id,
        as_of=boundary,
    )
    result = bind_session_commit_keys_result(
        eligible,
        org_id,
        as_of=boundary,
        repository_context=context,
    )
    bindings = {}
    declined = 0
    for (commit, session_id), facts in result.bindings.items():
        if isinstance(commit.repository, LegacyRepositoryKey):
            bindings[(commit.repository.repo, commit.commit_sha, session_id)] = facts
        else:
            declined += len(facts)
    if declined:
        logger.warning(
            "Session commit binding declined reason=repository_identity_unresolved count=%d",
            declined,
        )
    return bindings
