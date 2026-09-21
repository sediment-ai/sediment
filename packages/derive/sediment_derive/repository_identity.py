# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure repository resolution from a declared captured population (ADR 0019)."""

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from itertools import islice
from types import MappingProxyType
from typing import Literal, get_args

from pydantic import TypeAdapter

from sediment_core import (
    CommitSha,
    CIOutcome,
    FactTable,
    ForgeHost,
    ForgeProvider,
    NonEmptyId,
    OperationalReportLimitExceeded,
    OrgId,
    ProviderRepositoryId,
    PullRequestMerge,
    PullRequestRevision,
    Push,
    RepoSlug,
    RepositoryIdentityEvidence,
    REPOSITORY_IDENTITY_LIMIT,
    RepositoryRename,
    RepositoryReadAmbiguous,
    RepositoryReadKey,
    RequiredRepoSlug,
    SessionCommitObservation,
)
from sediment_core.models import AwareDatetime
from sediment_core.store import CIOutcomeProjection

RepositoryIdentitySkipReason = Literal[
    "repository_identity_absent",
    "repository_identity_unresolved",
    "repository_identity_conflict",
    "repository_source_absent",
    "repository_mirror_identity_unresolved",
]
REPOSITORY_IDENTITY_SKIP_REASONS = frozenset(get_args(RepositoryIdentitySkipReason))
REPOSITORY_IDENTITY_POLICY_VERSION = "1"
RepositoryRole = Literal["repo", "head_repo"]
SourceKey = tuple[FactTable, NonEmptyId, RepositoryRole]
_PROVIDER = TypeAdapter(ForgeProvider)
_HOST = TypeAdapter(ForgeHost)
_REPOSITORY_ID = TypeAdapter(ProviderRepositoryId)
_ORG = TypeAdapter(OrgId)
_REPO = TypeAdapter(RequiredRepoSlug)
_OPTIONAL_REPO = TypeAdapter(RepoSlug)
_COMMIT = TypeAdapter(CommitSha)
_EVIDENCE = TypeAdapter(RepositoryIdentityEvidence)


@dataclass(frozen=True)
class RepositoryIdentity:
    provider: ForgeProvider
    host: ForgeHost
    repository_id: ProviderRepositoryId

    def __post_init__(self):
        object.__setattr__(self, "provider", _PROVIDER.validate_python(self.provider))
        object.__setattr__(self, "host", _HOST.validate_python(self.host))
        object.__setattr__(
            self, "repository_id", _REPOSITORY_ID.validate_python(self.repository_id)
        )


@dataclass(frozen=True)
class IdentifiedRepositoryKey:
    org_id: OrgId
    identity: RepositoryIdentity

    def __post_init__(self):
        object.__setattr__(self, "org_id", _ORG.validate_python(self.org_id))
        if not isinstance(self.identity, RepositoryIdentity):
            raise ValueError("repository key requires a validated identity")


@dataclass(frozen=True)
class LegacyRepositoryKey:
    org_id: OrgId
    repo: RequiredRepoSlug

    def __post_init__(self):
        object.__setattr__(self, "org_id", _ORG.validate_python(self.org_id))
        object.__setattr__(self, "repo", _REPO.validate_python(self.repo))


RepositoryKey = IdentifiedRepositoryKey | LegacyRepositoryKey


@dataclass(frozen=True)
class CommitKey:
    repository: RepositoryKey
    commit_sha: CommitSha

    def __post_init__(self):
        if not isinstance(
            self.repository, (IdentifiedRepositoryKey, LegacyRepositoryKey)
        ):
            raise ValueError("commit key requires a repository key")
        object.__setattr__(self, "commit_sha", _COMMIT.validate_python(self.commit_sha))


@dataclass(frozen=True)
class RepositoryResolution:
    key: RepositoryKey | None = None
    repo: RepoSlug | None = None
    reason: RepositoryIdentitySkipReason | None = None
    evidence: tuple[RepositoryIdentityEvidence, ...] = ()


def repository_identity_of(
    fact, *, role: RepositoryRole = "repo"
) -> RepositoryIdentity | None:
    if role not in ("repo", "head_repo"):
        raise ValueError("invalid repository role")
    prefix = "head_repository" if role == "head_repo" else "repository"
    values = tuple(
        getattr(fact, f"{prefix}_{part}") for part in ("provider", "host", "id")
    )
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise ValueError("repository identity requires all three components")
    return RepositoryIdentity(*values)


def repository_sort_key(key: RepositoryKey) -> tuple[str, ...]:
    if isinstance(key, IdentifiedRepositoryKey):
        return (
            key.org_id,
            "identified",
            key.identity.provider,
            key.identity.host,
            key.identity.repository_id,
        )
    return (key.org_id, "legacy", key.repo)


def repository_read_key(key: RepositoryKey) -> RepositoryReadKey:
    """Convert a validated qualified key to the core SQL filter contract."""
    if isinstance(key, IdentifiedRepositoryKey):
        return key.identity.provider, key.identity.host, key.identity.repository_id
    if isinstance(key, LegacyRepositoryKey):
        return key.repo
    raise ValueError("invalid repository read key")


def commit_sort_key(key: CommitKey) -> tuple[str, ...]:
    return (*repository_sort_key(key.repository), key.commit_sha)


def _source_key(row: RepositoryIdentityEvidence) -> SourceKey:
    return row.source_table, row.source_fact_id, row.role


def _evidence_signature(row: RepositoryIdentityEvidence) -> tuple:
    # Python datetime equality collapses distinct folds with the same tzinfo.
    return (
        *_source_key(row),
        row.org_id,
        row.repo,
        row.repository_provider,
        row.repository_host,
        row.repository_id,
        row.captured_at.astimezone(UTC),
        row.source_push_id,
    )


def _evidence_sort_key(row: RepositoryIdentityEvidence) -> tuple:
    return row.captured_at.astimezone(UTC), *_source_key(row)


def _evidence_representation_key(row: RepositoryIdentityEvidence) -> tuple:
    # Equivalent copies may spell the same instant with different UTC offsets.
    # Preserve an actual supplied projection, selected independently of order.
    return (
        row.captured_at.isoformat(),
        repr(row.captured_at.tzinfo),
        row.captured_at.fold,
    )


def _validate_evidence(row: RepositoryIdentityEvidence) -> RepositoryIdentityEvidence:
    try:
        # Validate the core projection without introducing another Fact model.
        validated = _EVIDENCE.validate_python(vars(row))
        repository_identity_of(validated)
        if validated.role == "head_repo" and validated.source_table not in (
            FactTable.PULL_REQUEST_MERGES,
            FactTable.PULL_REQUEST_REVISIONS,
        ):
            raise ValueError
        is_observation = validated.source_table == FactTable.SESSION_COMMIT_OBSERVATIONS
        if is_observation != (validated.source_push_id is not None):
            raise ValueError
        if _evidence_signature(row) != _evidence_signature(validated):
            raise ValueError
        return validated
    except (ValueError, TypeError, AttributeError):
        raise ValueError("invalid repository identity evidence") from None


def _project_fact(fact, role: RepositoryRole) -> RepositoryIdentityEvidence:
    if role not in ("repo", "head_repo"):
        raise ValueError("invalid repository role")
    if isinstance(fact, RepositoryIdentityEvidence):
        if role != fact.role:
            raise ValueError("repository role differs from projection")
        return fact
    if isinstance(fact, Push):
        table, fact_id = FactTable.PUSHES, fact.push_id
    elif isinstance(fact, (CIOutcome, CIOutcomeProjection)):
        table, fact_id = FactTable.CI_OUTCOMES, fact.outcome_id
    elif isinstance(fact, SessionCommitObservation):
        table, fact_id = FactTable.SESSION_COMMIT_OBSERVATIONS, fact.observation_id
    elif isinstance(fact, PullRequestMerge):
        table, fact_id = FactTable.PULL_REQUEST_MERGES, fact.merge_id
    elif isinstance(fact, PullRequestRevision):
        table, fact_id = FactTable.PULL_REQUEST_REVISIONS, fact.revision_id
    else:
        raise TypeError("unsupported repository Fact")
    if role == "head_repo" and not isinstance(
        fact, (PullRequestMerge, PullRequestRevision)
    ):
        raise ValueError("repository Fact has no head role")
    prefix = "head_repository" if role == "head_repo" else "repository"
    return RepositoryIdentityEvidence(
        source_table=table,
        source_fact_id=fact_id,
        role=role,
        org_id=fact.org_id,
        repo=getattr(fact, role),
        repository_provider=getattr(fact, f"{prefix}_provider"),
        repository_host=getattr(fact, f"{prefix}_host"),
        repository_id=getattr(fact, f"{prefix}_id"),
        captured_at=fact.captured_at,
        source_push_id=fact.source_push_id
        if isinstance(fact, SessionCommitObservation)
        else None,
    )


def repository_identity_evidence_of(
    fact, *, role: RepositoryRole = "repo"
) -> RepositoryIdentityEvidence:
    """Project one captured role; this declares no external population completeness."""
    return _validate_evidence(_project_fact(fact, role))


@dataclass(frozen=True)
class RepositoryContext:
    """Immutable lookups; counts describe source roles and rename Facts separately."""

    org_id: OrgId
    as_of: AwareDatetime
    skipped: Mapping[RepositoryIdentitySkipReason, int]
    rename_skipped: Mapping[RepositoryIdentitySkipReason, int]
    _sources: Mapping[SourceKey, RepositoryIdentityEvidence] = field(repr=False)
    _resolutions: Mapping[SourceKey, RepositoryResolution] = field(repr=False)
    _names: Mapping[RepositoryKey, tuple[RepoSlug, ...]] = field(repr=False)
    _claims: Mapping[RepoSlug, frozenset[RepositoryIdentity]] = field(repr=False)
    _unsafe_slugs: frozenset[RepoSlug] = field(repr=False)

    def resolve_source(
        self, source_table: FactTable, source_fact_id: NonEmptyId, *, role="repo"
    ) -> RepositoryResolution:
        return self._resolutions.get(
            (source_table, source_fact_id, role),
            RepositoryResolution(reason="repository_source_absent"),
        )

    def resolve_fact(
        self, fact, *, role: RepositoryRole = "repo"
    ) -> RepositoryResolution:
        """Require the supplied Fact's complete projection to match its source."""
        try:
            projection = repository_identity_evidence_of(fact, role=role)
        except (ValueError, TypeError, AttributeError):
            return RepositoryResolution(reason="repository_identity_conflict")
        key = _source_key(projection)
        result = self.resolve_source(key[0], key[1], role=key[2])
        if result.reason == "repository_identity_conflict":
            return result
        row = self._sources.get(key)
        if row is None:
            return RepositoryResolution(reason="repository_source_absent")
        if _evidence_signature(projection) != _evidence_signature(row):
            return RepositoryResolution(reason="repository_identity_conflict")
        return result

    def resolve_reference(
        self, org_id: OrgId, repo: RepoSlug, *, repository_identity=None
    ) -> RepositoryResolution:
        try:
            org_id = _ORG.validate_python(org_id)
            repo = _OPTIONAL_REPO.validate_python(repo)
        except (ValueError, TypeError):
            return RepositoryResolution(reason="repository_identity_unresolved")
        if repository_identity is not None and not isinstance(
            repository_identity, RepositoryIdentity
        ):
            return RepositoryResolution(reason="repository_identity_unresolved")
        if org_id != self.org_id:
            return RepositoryResolution(reason="repository_identity_unresolved")
        if not repo:
            return RepositoryResolution(reason="repository_identity_absent")
        if repository_identity is None:
            if repo in self._claims or repo in self._unsafe_slugs:
                return RepositoryResolution(reason="repository_identity_unresolved")
            return RepositoryResolution(LegacyRepositoryKey(org_id, repo), repo)
        key = IdentifiedRepositoryKey(org_id, repository_identity)
        names = self._names.get(key, ())
        if repo not in names:
            return RepositoryResolution(reason="repository_identity_unresolved")
        return RepositoryResolution(key, names[0])

    def select_repository(
        self,
        org_id: OrgId,
        *,
        repo: RequiredRepoSlug | None = None,
        repository_identity: RepositoryIdentity | None = None,
    ) -> RepositoryKey | None:
        """Resolve an operator selector without assigning identity to legacy Facts.

        A name can select its sole captured lifetime. It cannot combine multiple
        lifetimes or repair conflicting evidence. Exact IDs require a declared
        name population; a supplied label must belong to that same identity.
        """
        org_id = _ORG.validate_python(org_id)
        if repo is not None:
            repo = _REPO.validate_python(repo)
        if repository_identity is not None and not isinstance(
            repository_identity, RepositoryIdentity
        ):
            raise ValueError("invalid repository selector identity")
        if org_id != self.org_id:
            return None
        if repository_identity is not None:
            key = IdentifiedRepositoryKey(org_id, repository_identity)
            names = self.observed_repo_slugs(key)
            if not names:
                return None
            if repo is not None and repo not in names:
                raise ValueError("repository selector label is not declared")
            return key
        if repo is None:
            raise ValueError("repository selector is absent")
        claims = self._claims.get(repo, frozenset())
        if repo in self._unsafe_slugs or len(claims) > 1:
            raise RepositoryReadAmbiguous()
        if claims:
            key = IdentifiedRepositoryKey(org_id, next(iter(claims)))
            if repo not in self.observed_repo_slugs(key):
                raise RepositoryReadAmbiguous()
            return key
        return LegacyRepositoryKey(org_id, repo)

    def commit_key(
        self,
        org_id: OrgId,
        repo: RepoSlug,
        commit_sha: CommitSha,
        *,
        repository_identity=None,
    ) -> CommitKey | None:
        result = self.resolve_reference(
            org_id, repo, repository_identity=repository_identity
        )
        if result.key is None:
            return None
        try:
            return CommitKey(result.key, commit_sha)
        except (ValueError, TypeError):
            return None

    def repo_for(self, repository_key: RepositoryKey) -> RepoSlug:
        if isinstance(repository_key, LegacyRepositoryKey):
            resolved = self.resolve_reference(
                repository_key.org_id, repository_key.repo
            )
            if resolved.key == repository_key:
                return resolved.repo
        else:
            names = self.observed_repo_slugs(repository_key)
            if names:
                return names[0]
        raise KeyError("repository key has no eligible label")

    def repository_keys(self) -> tuple[RepositoryKey, ...]:
        """List the complete context's repository keys in deterministic order."""
        return tuple(sorted(self._names, key=repository_sort_key))

    def observed_repo_slugs(
        self, repository_key: RepositoryKey
    ) -> tuple[RepoSlug, ...]:
        return self._names.get(repository_key, ())

    def mirror_refresh_resolution(
        self, source_table: FactTable, source_fact_id: NonEmptyId, *, repo: RepoSlug
    ) -> RepositoryResolution:
        """Decline known ambiguous locations; Git itself cannot attest a remote ID."""
        try:
            repo = _OPTIONAL_REPO.validate_python(repo)
        except (ValueError, TypeError):
            return RepositoryResolution(reason="repository_mirror_identity_unresolved")
        result = self.resolve_source(source_table, source_fact_id)
        source = self._sources.get((source_table, source_fact_id, "repo"))
        if result.key is None or source is None or repo in self._unsafe_slugs:
            return RepositoryResolution(reason="repository_mirror_identity_unresolved")
        if isinstance(result.key, IdentifiedRepositoryKey):
            if repo not in self.observed_repo_slugs(result.key) or self._claims.get(
                repo
            ) != frozenset((result.key.identity,)):
                return RepositoryResolution(
                    reason="repository_mirror_identity_unresolved"
                )
        elif repo != source.repo:
            return RepositoryResolution(reason="repository_mirror_identity_unresolved")
        return result


def _source_identity(row, sources, conflicts):
    """Return effective identity and exact proof, or one source failure."""
    identity = repository_identity_of(row)
    proof = (row,)
    if row.source_table != FactTable.SESSION_COMMIT_OBSERVATIONS:
        return identity, proof, None
    source_key = (FactTable.PUSHES, row.source_push_id, "repo")
    if source_key in conflicts:
        return None, proof, "repository_identity_conflict"
    push = sources.get(source_key)
    if push is None:
        return identity, proof, "repository_source_absent" if identity else None
    proof = tuple(sorted((row, push), key=_evidence_sort_key))
    source_identity = repository_identity_of(push)
    if source_identity is None:
        if identity is not None:
            return None, proof, "repository_identity_unresolved"
        if row.repo != push.repo:
            return None, proof, "repository_identity_conflict"
        return None, proof, None
    if identity is not None and source_identity != identity:
        return None, proof, "repository_identity_conflict"
    return source_identity, proof, None


def _rename_claims(renames, org_id, boundary):
    """Reject contradictory primary/delivery identities without choosing an edge."""
    rows = {}
    groups = {}
    signatures = {}
    conflicts = set()
    for row in renames:
        try:
            row = RepositoryRename.model_validate(row.model_dump())
        except (ValueError, TypeError, AttributeError):
            raise ValueError("invalid repository rename evidence") from None
        if row.org_id != org_id or row.captured_at.astimezone(UTC) > boundary:
            continue
        signature = (
            repository_identity_of(row),
            row.old_repo,
            row.new_repo,
            row.source_event_id,
            row.occurred_at.astimezone(UTC) if row.occurred_at else None,
            row.captured_at.astimezone(UTC),
        )
        if row.rename_id in signatures and signatures[row.rename_id] != signature:
            conflicts.add(row.rename_id)
        signatures[row.rename_id] = signature
        rows.setdefault(row.rename_id, []).append(row)
        if row.source_event_id is not None:
            delivery = (
                row.repository_provider,
                row.repository_host,
                row.source_event_id,
            )
            groups.setdefault(delivery, []).append(row)
    for group in groups.values():
        # Capture time and retained primary ID can differ on a redelivery.
        if (
            len(
                {
                    (
                        repository_identity_of(row),
                        row.old_repo,
                        row.new_repo,
                        row.occurred_at.astimezone(UTC) if row.occurred_at else None,
                    )
                    for row in group
                }
            )
            > 1
        ):
            conflicts.update(row.rename_id for row in group)
    valid, claims, unsafe = [], {}, set()
    for row_id, copies in rows.items():
        for row in copies:
            identity = repository_identity_of(row)
            for slug in (row.old_repo, row.new_repo):
                claims.setdefault(slug, set()).add(identity)
                if row_id in conflicts:
                    unsafe.add(slug)
            if row_id not in conflicts:
                valid.append((identity, row.old_repo, row.new_repo))
    return valid, claims, unsafe, conflicts


def build_repository_context(
    evidence: Iterable[RepositoryIdentityEvidence],
    renames: Iterable[RepositoryRename],
    org_id: OrgId,
    *,
    as_of: datetime,
) -> RepositoryContext:
    if as_of.utcoffset() is None:
        raise ValueError("as_of must be an aware datetime")
    org_id = _ORG.validate_python(org_id)
    evidence = tuple(islice(evidence, REPOSITORY_IDENTITY_LIMIT + 1))
    renames = tuple(islice(renames, REPOSITORY_IDENTITY_LIMIT + 1))
    if (
        len(evidence) > REPOSITORY_IDENTITY_LIMIT
        or len(renames) > REPOSITORY_IDENTITY_LIMIT
    ):
        raise OperationalReportLimitExceeded(
            "repository identity population exceeds limit"
        )
    boundary = as_of.astimezone(UTC)
    sources, conflicts, source_copies = {}, set(), {}
    for row in evidence:
        row = _validate_evidence(row)
        if row.org_id != org_id or row.captured_at.astimezone(UTC) > boundary:
            continue
        key = _source_key(row)
        if key in sources and _evidence_signature(sources[key]) != _evidence_signature(
            row
        ):
            conflicts.add(key)
        if key not in sources or _evidence_representation_key(
            row
        ) < _evidence_representation_key(sources[key]):
            sources[key] = row
        source_copies.setdefault(key, []).append(row)
    valid_renames, claims, unsafe, rename_conflicts = _rename_claims(
        renames, org_id, boundary
    )
    names = {}
    for identity, old_repo, new_repo in valid_renames:
        names.setdefault(IdentifiedRepositoryKey(org_id, identity), set()).update(
            (old_repo, new_repo)
        )
    for key, copies in source_copies.items():
        for row in copies:
            identity = repository_identity_of(row)
            if identity is not None and row.repo:
                claims.setdefault(row.repo, set()).add(identity)
            if key in conflicts:
                unsafe.add(row.repo)
    sources = {key: row for key, row in sources.items() if key not in conflicts}
    resolved = {}
    for source_key, row in sources.items():
        identity, proof, reason = _source_identity(row, sources, conflicts)
        resolved[source_key] = (identity, proof, reason)
        if reason is not None or not row.repo:
            continue
        key = (
            IdentifiedRepositoryKey(org_id, identity)
            if identity
            else LegacyRepositoryKey(org_id, row.repo)
        )
        names.setdefault(key, set()).add(row.repo)
        if identity:
            claims.setdefault(row.repo, set()).add(identity)
    context = RepositoryContext(
        org_id,
        boundary,
        MappingProxyType({}),
        MappingProxyType(
            {"repository_identity_conflict": len(rename_conflicts)}
            if rename_conflicts
            else {}
        ),
        MappingProxyType(sources),
        MappingProxyType({}),
        MappingProxyType({key: tuple(sorted(value)) for key, value in names.items()}),
        MappingProxyType({key: frozenset(value) for key, value in claims.items()}),
        frozenset(unsafe),
    )
    results = {
        key: RepositoryResolution(reason="repository_identity_conflict")
        for key in conflicts
    }
    for source_key, (identity, proof, reason) in resolved.items():
        if reason is not None:
            results[source_key] = RepositoryResolution(reason=reason, evidence=proof)
            continue
        row = sources[source_key]
        result = context.resolve_reference(
            org_id, row.repo, repository_identity=identity
        )
        results[source_key] = replace(result, evidence=proof)
    skipped = Counter(
        result.reason for result in results.values() if result.reason is not None
    )
    return replace(
        context,
        skipped=MappingProxyType(dict(sorted(skipped.items()))),
        _resolutions=MappingProxyType(results),
    )
