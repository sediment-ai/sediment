# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Mirror garbage collection: reclaim bare mirrors for repos with no
recent Push activity.

This is deliberately **not** an ADR-0001 derivation: it performs a
filesystem side effect (deleting a mirror directory), not a recomputation
over facts, so it has no persisted-output purity claim to keep. ``now`` is
still caller-supplied rather than read internally, keeping the function
itself deterministic and testable the same way a derivation would be.

GC keys retention off ``store.read_pushes`` — the quarantine-excluding
default read path (same as every derivation). A mirrored repo with zero
pushes under that read is ``quarantine_ambiguous`` and always **kept**: that
read cannot distinguish a true orphan (a mirror pre-seeded or created
out-of-band, no Push fact ever existed) from a repo whose entire Push
history is quarantined. Reaching for ``include_quarantined=True`` here would
resolve the ambiguity but misuses an escape hatch reserved for audit tooling
(``docs/agents/derivations.md`` gotchas) — a scheduled deletion job is
exactly the kind of caller that must not touch it. There is no code path
that produces a "confirmed orphan" verdict as a result.

**Known gaps, not fixed here:**

- **Partial quarantine resolves toward deletion, not ambiguity.**
  The zero-push case above is the only one this module treats as ambiguous.
  A repo whose *recent* pushes are all quarantined but whose older pushes
  predate the retention cutoff reads, under the same quarantine-excluding
  path, as "last push was N days ago" — eligible for removal — while a
  *fully* quarantined repo correctly reads as zero-push and is kept. The
  same ambiguity the zero-push case exists to catch resolves the other way
  here: incident-response quarantine of a repo's recent history can get its
  mirror reclaimed while the quarantine is still in effect. Telling the two
  cases apart would need to know *which repo* each ``QuarantineRecord``
  covers, but the log only carries ``fact_id`` — recovering the repo means
  reading the quarantined push's own content, i.e. exactly the
  ``include_quarantined=True`` escape hatch the paragraph above rules out
  for this caller. Resolving this within that doctrine is an open question,
  not a bug fix.
- **Snapshot-vs-remove TOCTOU.** Pushes are read once up front, then
  removals happen in a loop; a push that lands mid-run (a webhook stores the
  ``Push`` fact and ``MirrorManager.ensure`` fetches it, both under the
  per-mirror lock) is invisible to the snapshot already taken, so a
  freshly-refreshed mirror can still be removed. Recoverable — the repo is
  alive, the next push re-clones, and every derivation reader is verified
  fail-soft on ``mirror_absent`` — and the window is seconds against a
  90-day threshold, but it is a real race, not just a theoretical one.

Repository lifetimes use separate mirror keys. A rename does not move the
identified mirror or reset its retention history.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from sediment_core import OrgId, RepoSlug

from .mirror import MirrorManager
from .repository_identity import (
    IdentifiedRepositoryKey,
    LegacyRepositoryKey,
    RepositoryIdentity,
    RepositoryKey,
    build_repository_context,
    repository_identity_of,
)

if TYPE_CHECKING:
    from sediment_core import FactStore

logger = logging.getLogger("sediment.derive.gc")


class GCAction(StrEnum):
    KEPT = "kept"
    REMOVED = "removed"


@dataclass(frozen=True)
class MirrorGCPolicy:
    """Tunable GC semantics. ``policy_version`` stamps provenance the same
    way every other derive policy does; bump it when tuning
    ``retention_days``."""

    retention_days: int = 90
    policy_version: str = "1"

    def __post_init__(self) -> None:
        # A zero or negative retention puts the cutoff at or beyond "now",
        # which marks every mirror with push history for removal — the
        # destructive version of the config typo list_push_commits clamps.
        if self.retention_days < 1:
            raise ValueError(f"retention_days must be >= 1, got {self.retention_days}")


@dataclass(frozen=True)
class MirrorGCResult:
    """One mirrored repo's GC verdict for one run. Not a fact, not
    persisted — recomputed fresh on every invocation."""

    org_id: OrgId
    repo: RepoSlug | None
    action: GCAction
    last_push_captured_at: datetime | None
    skip_reason: str | None  # closed vocabulary: within_retention,
    # quarantine_ambiguous, remove_failed
    repository_identity: RepositoryIdentity | None = None


def gc_mirrors(
    store: "FactStore",
    mirrors: MirrorManager,
    org_id: str,
    policy: MirrorGCPolicy | None = None,
    *,
    now: datetime,
    dry_run: bool = True,
) -> list[MirrorGCResult]:
    """GC every mirror ``mirrors.list_mirrored_repositories(org_id)``
    holds, ordered by repository key — not filesystem iteration
    order, so results are reproducible.

    A repo's most recent (non-quarantined) ``Push.captured_at`` older than
    ``policy.retention_days`` before ``now`` is removed (``dry_run=False``
    actually deletes; ``dry_run=True``, the default, reports what would
    happen without touching disk). A repo with zero pushes under that read
    is ``quarantine_ambiguous`` and kept — see the module docstring.

    A removal attempt that doesn't land — a concurrent rename/remove already
    cleared the directory, or ``shutil.rmtree`` raises ``OSError`` — is
    reported ``kept``/``remove_failed`` rather than a misreported ``removed``,
    and does not abort the run: the remaining repos are still processed.
    """
    policy = policy or MirrorGCPolicy()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("mirror retention requires an aware boundary")
    cutoff = now.astimezone(UTC) - timedelta(days=policy.retention_days)

    last_push_by_repo: dict[RepositoryKey, datetime] = {}
    with store.read_snapshot() as snapshot:
        context = build_repository_context(
            snapshot.read_repository_identities(org_id, captured_through=now),
            snapshot.read_repository_renames(org_id, captured_through=now),
            org_id,
            as_of=now,
        )
        for push in snapshot.iter_push_gc_rows(org_id):
            identity = repository_identity_of(push)
            # Retention uses each stored Push's exact namespace, including the
            # separate legacy directory. It never promotes legacy history into
            # an identified repository or relies on a display name to remove it.
            if not push.repo and identity is None:
                logger.warning("repository_identity_absent", extra={"org_id": org_id})
                continue
            key = (
                IdentifiedRepositoryKey(org_id, identity)
                if identity is not None
                else LegacyRepositoryKey(org_id, push.repo)
            )
            captured_at = push.captured_at.astimezone(UTC)
            current = last_push_by_repo.get(key)
            if current is None or captured_at > current:
                last_push_by_repo[key] = captured_at

    results: list[MirrorGCResult] = []
    for key in mirrors.list_mirrored_repositories(org_id):
        identity = key.identity if isinstance(key, IdentifiedRepositoryKey) else None
        names = context.observed_repo_slugs(key)
        repo = (
            names[0]
            if names
            else key.repo
            if isinstance(key, LegacyRepositoryKey)
            else None
        )
        last_push = last_push_by_repo.get(key)
        if last_push is None:
            action, skip_reason = GCAction.KEPT, "quarantine_ambiguous"
        elif last_push >= cutoff:
            action, skip_reason = GCAction.KEPT, "within_retention"
        elif dry_run:
            action, skip_reason = GCAction.REMOVED, None
            logger.info(
                "mirror_gc_would_remove",
                extra={
                    "org_id": org_id,
                    "repo": repo,
                    "last_push_captured_at": last_push.isoformat(),
                },
            )
        else:
            # Fail-soft (CONTEXT.md): a concurrent rename/remove or an
            # OSError from shutil.rmtree (permissions, dir swapped for a
            # file) must not abort the loop with mirrors already deleted
            # and no report of what happened, nor claim REMOVED for a
            # mirror still on disk.
            try:
                removed = mirrors.remove_repository(key)
            except OSError:
                logger.exception(
                    "mirror_gc_remove_failed",
                    extra={
                        "org_id": org_id,
                        "repo": repo,
                        "last_push_captured_at": last_push.isoformat(),
                    },
                )
                action, skip_reason = GCAction.KEPT, "remove_failed"
            else:
                if removed:
                    action, skip_reason = GCAction.REMOVED, None
                    logger.info(
                        "mirror_gc_removed",
                        extra={
                            "org_id": org_id,
                            "repo": repo,
                            "last_push_captured_at": last_push.isoformat(),
                        },
                    )
                else:
                    action, skip_reason = GCAction.KEPT, "remove_failed"
                    logger.warning(
                        "mirror_gc_remove_failed",
                        extra={
                            "org_id": org_id,
                            "repo": repo,
                            "last_push_captured_at": last_push.isoformat(),
                        },
                    )
        results.append(
            MirrorGCResult(
                org_id=org_id,
                repo=repo,
                action=action,
                last_push_captured_at=last_push,
                skip_reason=skip_reason,
                repository_identity=identity,
            )
        )
    return results
