# SPDX-License-Identifier: AGPL-3.0-or-later
"""Complete repository evidence reads within a caller-owned Fact snapshot."""

from datetime import UTC, datetime
from dataclasses import replace
from collections.abc import Iterable
from itertools import islice

from sediment_core import (
    OrgId,
    OperationalReportLimitExceeded,
    REPOSITORY_IDENTITY_LIMIT,
    RepositoryIdentityEvidence,
)

from .repository_identity import (
    RepositoryContext,
    build_repository_context,
    repository_identity_evidence_of,
)


def read_repository_context(
    snapshot,
    org_id: OrgId,
    *,
    as_of: datetime | None = None,
    supplemental_legacy_evidence: Iterable[RepositoryIdentityEvidence] = (),
) -> RepositoryContext:
    """Read one bounded population without starting a transaction or reading time.

    An explicit boundary remains authoritative. Without one, the last captured
    repository Fact supplies the boundary; this does not select other Fact families.
    Preloaded legacy APIs may supplement explicit identity absence. Supplements
    join the complete population before resolution, so source conflicts and later
    identified claims remain authoritative. Identified supplements are forbidden.
    """
    if as_of is not None and as_of.utcoffset() is None:
        raise ValueError("as_of must be an aware datetime")
    through = as_of or datetime.max.replace(tzinfo=UTC)
    evidence = snapshot.read_repository_identities(
        org_id, captured_through=through, limit=REPOSITORY_IDENTITY_LIMIT
    )
    renames = snapshot.read_repository_renames(
        org_id, captured_through=through, limit=REPOSITORY_IDENTITY_LIMIT
    )
    supplements = tuple(
        islice(supplemental_legacy_evidence, REPOSITORY_IDENTITY_LIMIT + 1)
    )
    if len(supplements) > REPOSITORY_IDENTITY_LIMIT:
        raise OperationalReportLimitExceeded(
            "repository identity supplement population exceeds limit"
        )
    supplements = tuple(
        repository_identity_evidence_of(row, role=row.role) for row in supplements
    )
    if any(
        item.repository_provider is not None
        or item.repository_host is not None
        or item.repository_id is not None
        for item in supplements
    ):
        raise ValueError("repository supplements must be legacy evidence")
    # A preload can repeat the same stored projection. Count that source once,
    # retaining contradictory copies for the resolver to reject. UTC equality
    # keeps distinct daylight-saving folds from concealing a changed timestamp.
    stored = {
        replace(row, captured_at=row.captured_at.astimezone(UTC)) for row in evidence
    }
    evidence = (
        *evidence,
        *(
            row
            for row in supplements
            if replace(row, captured_at=row.captured_at.astimezone(UTC)) not in stored
        ),
    )
    boundary = as_of or max(
        (
            row.captured_at.astimezone(UTC)
            for row in (*evidence, *renames)
            if row.org_id == org_id
        ),
        default=datetime.min.replace(tzinfo=UTC),
    )
    return build_repository_context(evidence, renames, org_id, as_of=boundary)
