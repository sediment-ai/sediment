# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic policy, scope, and bundle primitives for derivation runs."""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import tomllib
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from itertools import chain
from dataclasses import dataclass, field, fields, is_dataclass, replace
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Literal, get_args

from pydantic import BaseModel, Field, TypeAdapter
from sediment_core import (
    SessionCommitObservation,
    RepositoryIdentityEvidence,
    RepositoryRename,
    REPOSITORY_IDENTITY_LIMIT,
    INFERENCE_SESSION_BYTES_LIMIT,
    FactTable,
    CIOutcome,
    DeveloperDecision,
    FactStore,
    InferenceCall,
    InferenceMessage,
    OrgId,
    ToolCallPart,
)
from sediment_derive.repository_identity import (
    CommitKey,
    RepositoryContext,
    RepositoryIdentity,
    LegacyRepositoryKey,
    build_repository_context,
    repository_sort_key,
)
from sediment_core.models import ScalarIdentity
from sediment_core.store import (
    InferenceCallIdentity,
    RolloutInferenceCall,
    DeveloperDecisionProjection,
    CIOutcomeProjection,
)
from sediment_derive import (
    AbandonmentPolicy,
    Provenance,
    AttributionSource,
    AttributionPolicy,
    SimilarityPolicy,
    CommitRef,
    MirrorManager,
    Rollout,
    RolloutPolicy,
    SessionAbandonment,
    Turn,
    derive_rollout_result,
    derive_ci_resolution_result,
    inference_fact_id,
    inference_observed_at,
    split_of,
)

from sediment_derive.rollout import (
    ROLLOUT_IMPLEMENTATION_VERSION,
    RolloutFragmentReason,
    project_session_turns,
)
from sediment_derive.attachment import join_decisions_by_call_id_result
from sediment_derive.inference_call import model_call_ids

from ._record_storage import RecordStore, FileRecords as BundleRecords, _write_private

from .attributed_completions import (
    ATTRIBUTED_COMPLETION_IMPLEMENTATION_VERSION,
    AttributedCompletion,
    AttributedCompletionPolicy,
    assemble_attributed_completion_result,
)

_ATTRIBUTION_FIELDS = frozenset(
    {
        "post_push_grace_period_minutes",
        "max_commits_per_push",
        "git_notes",
        "jaccard",
    }
)
_SIMILARITY_FIELDS = frozenset({"min_similarity", "lookback_window_minutes"})
_ROOT_FIELDS = frozenset({"schema_version", "attribution", "split"})
_SPLIT_FIELDS = frozenset({"eval_fraction"})
_ARTIFACT_FILES = {
    "attributed_completions": "attributed_completions.jsonl",
    "rollouts": "rollouts.jsonl",
    "inference_calls": "inference_calls.jsonl",
    "inference_call_identities": "inference_call_identities.jsonl",
    "repository_identities": "repository_identities.jsonl",
    "repository_renames": "repository_renames.jsonl",
}
_BUNDLE_SCHEMA_VERSION = 4
_IDENTITY_POPULATION = "organization-through-as-of-v1"
_IDENTITY_LIMIT = 50_000
_RECORD_ENCODING = "sediment-record-json-v1"
_TIMESTAMP_PATTERN = (
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
_ORG_ID = TypeAdapter(OrgId)
_SCALAR_IDENTITY = TypeAdapter(ScalarIdentity)
_IDENTITY_ADAPTER = TypeAdapter(InferenceCallIdentity)
_REPOSITORY_EVIDENCE_ADAPTER = TypeAdapter(RepositoryIdentityEvidence)

NonNegativeInt = Annotated[int, Field(ge=0)]
PositiveInt = Annotated[int, Field(gt=0)]
UnitFloat = Annotated[float, Field(ge=0.0, le=1.0)]
EvalFraction = Annotated[float, Field(ge=0.0, le=0.5)]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ManifestTimestamp = Annotated[
    datetime,
    Field(pattern=_TIMESTAMP_PATTERN),
]
ManifestUserId = Annotated[str, Field(pattern=r"\S")]
ManifestUsers = Annotated[list[ManifestUserId], Field(min_length=1)]


class BundleValidationError(ValueError):
    """A derived bundle failed its local integrity or schema checks."""


@dataclass(frozen=True)
class BundleFileMetadata:
    """Integrity metadata for one canonical JSONL file."""

    path: str
    rows: NonNegativeInt
    bytes: NonNegativeInt
    sha256: Sha256


@dataclass(frozen=True)
class BundleAttributedCompletionsFile:
    """Integrity metadata for ``attributed_completions.jsonl``."""

    path: Literal["attributed_completions.jsonl"]
    rows: NonNegativeInt
    bytes: NonNegativeInt
    sha256: Sha256


@dataclass(frozen=True)
class BundleRolloutsFile:
    """Integrity metadata for ``rollouts.jsonl``."""

    path: Literal["rollouts.jsonl"]
    rows: NonNegativeInt
    bytes: NonNegativeInt
    sha256: Sha256


@dataclass(frozen=True)
class BundleInferenceCallsFile:
    """Integrity metadata for ``inference_calls.jsonl``."""

    path: Literal["inference_calls.jsonl"]
    rows: NonNegativeInt
    bytes: NonNegativeInt
    sha256: Sha256


@dataclass(frozen=True)
class BundleInferenceCallIdentitiesFile:
    """Integrity metadata for the complete declared identity population."""

    path: Literal["inference_call_identities.jsonl"]
    rows: NonNegativeInt
    bytes: NonNegativeInt
    sha256: Sha256


@dataclass(frozen=True)
class BundleRepositoryIdentitiesFile:
    """Integrity metadata for the complete repository projection population."""

    path: Literal["repository_identities.jsonl"]
    rows: NonNegativeInt
    bytes: NonNegativeInt
    sha256: Sha256


@dataclass(frozen=True)
class BundleRepositoryRenamesFile:
    """Integrity metadata for the complete RepositoryRename population."""

    path: Literal["repository_renames.jsonl"]
    rows: NonNegativeInt
    bytes: NonNegativeInt
    sha256: Sha256


@dataclass(frozen=True)
class BundleFiles:
    """The six canonical JSONL files in a derived bundle."""

    attributed_completions: BundleAttributedCompletionsFile
    rollouts: BundleRolloutsFile
    inference_calls: BundleInferenceCallsFile
    inference_call_identities: BundleInferenceCallIdentitiesFile
    repository_identities: BundleRepositoryIdentitiesFile
    repository_renames: BundleRepositoryRenamesFile


@dataclass(frozen=True)
class BundleArtifactCounts:
    """Row counts for the six canonical JSONL files."""

    attributed_completions: NonNegativeInt
    rollouts: NonNegativeInt
    inference_calls: NonNegativeInt
    inference_call_identities: NonNegativeInt
    repository_identities: NonNegativeInt
    repository_renames: NonNegativeInt


@dataclass(frozen=True)
class BundleScope:
    """The closed cohort scope serialized in a bundle manifest."""

    since: ManifestTimestamp | None
    until: ManifestTimestamp | None
    users: ManifestUsers | None


@dataclass(frozen=True)
class BundleSimilarityPolicy:
    """One serialized similarity policy."""

    min_similarity: UnitFloat
    lookback_window_minutes: PositiveInt


@dataclass(frozen=True)
class BundleAttributionPolicy:
    """The closed attribution policy serialized in a bundle manifest."""

    post_push_grace_period_minutes: NonNegativeInt
    max_commits_per_push: PositiveInt
    git_notes: BundleSimilarityPolicy
    jaccard: BundleSimilarityPolicy


@dataclass(frozen=True)
class BundleSplitPolicy:
    """The closed split policy serialized in a bundle manifest."""

    eval_fraction: EvalFraction


@dataclass(frozen=True)
class BundlePolicy:
    """Every resolved derivation policy value serialized in a manifest."""

    schema_version: Literal[1]
    attribution: BundleAttributionPolicy
    split: BundleSplitPolicy


@dataclass(frozen=True)
class BundleImplementationVersions:
    """Implementation versions required by this bundle reader."""

    abandonment: Literal[AbandonmentPolicy().policy_version]
    attribution: Literal[AttributionPolicy().policy_version]
    attributed_completion: Literal[ATTRIBUTED_COMPLETION_IMPLEMENTATION_VERSION]
    rollout: Literal[ROLLOUT_IMPLEMENTATION_VERSION]
    repository_identity: Literal["1"]


@dataclass(frozen=True)
class BundleManifest:
    """The deterministic top-level contract in ``manifest.json``."""

    bundle_schema_version: Literal[4]
    record_encoding: Literal["sediment-record-json-v1"]
    identity_population: Literal["organization-through-as-of-v1"]
    repository_population: Literal["organization-through-as-of-v1"]
    org_id: Annotated[OrgId, Field(min_length=1)]
    scope: BundleScope
    policy: BundlePolicy
    policy_digest: Sha256
    implementation_versions: BundleImplementationVersions
    quarantine_revision: NonNegativeInt
    as_of: ManifestTimestamp | None
    mirror_revisions: Mapping[str, Mapping[str, str]]
    counts: BundleArtifactCounts
    skipped: Mapping[str, NonNegativeInt]
    excluded: Mapping[str, NonNegativeInt]
    files: BundleFiles
    fragmented: Mapping[RolloutFragmentReason, NonNegativeInt]


@dataclass(frozen=True)
class BundleRecord:
    """A strict JSON envelope containing one lossless canonical record."""

    record_json: str


@dataclass(frozen=True)
class DerivationPolicy:
    """The fully resolved policy shared by both canonical artifacts."""

    schema_version: int = 1
    attribution: AttributionPolicy = field(default_factory=AttributionPolicy)
    eval_fraction: float = 0.1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError(
                f"unsupported derivation policy schema_version {self.schema_version}"
            )
        if not isinstance(self.eval_fraction, int | float) or isinstance(
            self.eval_fraction, bool
        ):
            raise ValueError("eval_fraction must be a number")
        if not 0.0 <= self.eval_fraction <= 0.5:
            raise ValueError(
                f"eval_fraction must be between 0.0 and 0.5 (got {self.eval_fraction})"
            )

    @property
    def digest(self) -> str:
        """Return the stable SHA-256 digest of every resolved policy value."""

        encoded = json.dumps(
            self.to_dict(),
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        """Return the manifest-ready resolved policy."""

        attribution = {
            "post_push_grace_period_minutes": (
                self.attribution.post_push_grace_period_minutes
            ),
            "max_commits_per_push": self.attribution.max_commits_per_push,
            "git_notes": {
                "min_similarity": self.attribution.git_notes.min_similarity,
                "lookback_window_minutes": (
                    self.attribution.git_notes.lookback_window_minutes
                ),
            },
            "jaccard": {
                "min_similarity": self.attribution.jaccard.min_similarity,
                "lookback_window_minutes": (
                    self.attribution.jaccard.lookback_window_minutes
                ),
            },
        }
        return {
            "schema_version": self.schema_version,
            "attribution": attribution,
            "split": {"eval_fraction": self.eval_fraction},
        }


@dataclass(frozen=True)
class DerivationScope:
    """A post-derivation cohort selection within one configured organization."""

    since: datetime | None = None
    until: datetime | None = None
    users: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        for name, value in (("since", self.since), ("until", self.until)):
            if value is not None and (
                value.tzinfo is None or value.utcoffset() is None
            ):
                raise ValueError(f"{name} must be timezone-aware")
        if self.since is not None and self.until is not None:
            if self.since >= self.until:
                raise ValueError("since must be earlier than until")
        if self.users is not None:
            normalized = tuple(sorted({user.strip() for user in self.users}))
            if not normalized or "" in normalized:
                raise ValueError("users must contain at least one non-empty user ID")
            object.__setattr__(self, "users", normalized)

    def includes(self, *, user_id: str, occurred_at: datetime) -> bool:
        """Return whether a completion belongs to this cohort."""

        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        if self.users is not None and user_id not in self.users:
            return False
        if self.since is not None and occurred_at < self.since:
            return False
        return self.until is None or occurred_at < self.until


@dataclass(frozen=True)
class DerivedBundle:
    """One canonical run, backed by tuples or a live private record store."""

    org_id: str
    policy: DerivationPolicy
    scope: DerivationScope
    as_of: datetime | None
    quarantine_revision: int
    mirror_revisions: Mapping[str, Mapping[str, str]]
    attributed_completions: Sequence[AttributedCompletion]
    rollouts: Sequence[Rollout]
    inference_calls: Sequence[InferenceCall]
    inference_call_identities: tuple[InferenceCallIdentity, ...]
    skipped: Mapping[str, int]
    excluded: Mapping[str, int]
    fragmented: Mapping[RolloutFragmentReason, int] = field(default_factory=dict)
    repository_identities: tuple[RepositoryIdentityEvidence, ...] = ()
    repository_renames: tuple[RepositoryRename, ...] = ()


class BundleCapacityError(BundleValidationError):
    """A complete bundle cannot fit its declared execution resource budget."""


@dataclass(frozen=True)
class BundleLimits:
    """Byte ceilings for one encoded record, private stage, and materialization.

    The record ceiling admits complete large Rollouts; it is not a promise
    that arbitrary records fit a particular process memory allowance.
    """

    max_record_bytes: int = 512 * 1024 * 1024
    max_staging_bytes: int = 8 * 1024 * 1024 * 1024
    max_materialized_bytes: int = 64 * 1024 * 1024

    def __post_init__(self):
        for item in fields(self):
            value = getattr(self, item.name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{item.name} must be a positive integer")


class BundleRecordStore(RecordStore):
    """Own six canonical populations with private quota-limited record storage."""

    def __init__(
        self,
        *,
        limits: BundleLimits | None = None,
        temporary_parent: Path | None = None,
        _prefix: str = ".sediment-bundle-",
        private: bool = False,
    ):
        self._private = private
        super().__init__(
            limits=limits or BundleLimits(),
            capacity_error=BundleCapacityError,
            validation_error=BundleValidationError,
            temporary_parent=temporary_parent,
            _prefix=_prefix,
        )

    def records(self, name: str) -> BundleRecords:
        if name not in _ARTIFACT_FILES:
            raise ValueError("unknown canonical artifact population")
        return super().records(
            name,
            encoder=_private_record_chunks if self._private else _record_chunks,
            decoder=_decode_private_record if self._private else _decode_record,
            restore=_record_loader(name),
        )


def build_derived_bundle(
    store: FactStore,
    mirrors: MirrorManager,
    org_id: str,
    *,
    policy: DerivationPolicy | None = None,
    scope: DerivationScope | None = None,
    limits: BundleLimits | None = None,
    temporary_parent: Path | None = None,
) -> DerivedBundle:
    """Explicitly materialize a small canonical run within its byte budget."""
    limits = limits or BundleLimits()
    with build_derived_bundle_context(
        store,
        mirrors,
        org_id,
        policy=policy,
        scope=scope,
        limits=limits,
        temporary_parent=temporary_parent,
    ) as bundle:
        return _materialize_bundle(bundle, limits)


@contextmanager
def build_derived_bundle_context(
    store: FactStore,
    mirrors: MirrorManager,
    org_id: str,
    *,
    policy: DerivationPolicy | None = None,
    scope: DerivationScope | None = None,
    limits: BundleLimits | None = None,
    temporary_parent: Path | None = None,
) -> Iterator[DerivedBundle]:
    """Build and validate a canonical run backed by private disposable records.

    Fact and mirror snapshots cover construction and validation. Their locks end
    before yielding; the private records remain readable until context exit.
    """
    with BundleRecordStore(
        limits=limits, temporary_parent=temporary_parent, private=True
    ) as stage:
        yield _build_derived_bundle(store, mirrors, org_id, policy, scope, stage)


def _build_derived_bundle(store, mirrors, org_id, policy, scope, stage):
    policy = policy or DerivationPolicy()
    scope = scope or DerivationScope()
    with store.read_snapshot() as snapshot:
        pushes = snapshot.read_pushes(org_id)
        inference_calls = snapshot.read_inference_call_summaries(org_id)
        decisions = snapshot.read_decision_projections(org_id)
        edit_observations = snapshot.read_edit_observation_projections(org_id)
        ci_outcomes = snapshot.read_ci_outcome_projections(org_id)
        session_observations = snapshot.read_session_commit_observations(org_id)
        through = datetime.max.replace(tzinfo=UTC)
        repository_identities = tuple(
            snapshot.read_repository_identities(
                org_id, captured_through=through, limit=REPOSITORY_IDENTITY_LIMIT
            )
        )
        repository_renames = tuple(
            snapshot.read_repository_renames(
                org_id, captured_through=through, limit=REPOSITORY_IDENTITY_LIMIT
            )
        )
        timestamps = [
            inference_observed_at(fact).astimezone(UTC) for fact in inference_calls
        ]
        timestamps.extend(
            fact.captured_at.astimezone(UTC)
            for fact in (
                *decisions,
                *edit_observations,
                *ci_outcomes,
                *pushes,
                *session_observations,
                *repository_identities,
                *repository_renames,
            )
        )
        as_of = max(timestamps, default=None)
        context = build_repository_context(
            repository_identities,
            repository_renames,
            org_id,
            as_of=as_of or datetime.min.replace(tzinfo=UTC),
        )
        disk_repositories = tuple(
            sorted(mirrors.list_mirrored_repositories(org_id), key=repository_sort_key)
        )
        lock_repositories = set(disk_repositories)
        for push in pushes:
            resolved = context.resolve_fact(push)
            if resolved.key is not None:
                lock_repositories.add(resolved.key)
        identity_evidence = (
            tuple(
                snapshot.read_inference_call_identities(
                    org_id, observed_through=as_of, limit=_IDENTITY_LIMIT
                )
            )
            if as_of is not None
            else ()
        )
        with mirrors.read_repository_snapshot(lock_repositories) as mirror_snapshot:
            mirror_revisions = {
                _mirror_revision_key(key): mirror.refs()
                for key in disk_repositories
                if (mirror := mirror_snapshot.open_repository(key)) is not None
            }
            attributed_result = assemble_attributed_completion_result(
                snapshot,
                mirror_snapshot,
                org_id,
                AttributedCompletionPolicy(
                    attribution=policy.attribution,
                    abandonment=AbandonmentPolicy(
                        attribution=policy.attribution,
                    ),
                    policy_version=ATTRIBUTED_COMPLETION_IMPLEMENTATION_VERSION,
                    eval_fraction=policy.eval_fraction,
                ),
                policy_digest=policy.digest,
                session_commit_observations=session_observations,
                as_of=context.as_of,
                repository_context=context,
            )
            by_id = {inference_fact_id(call): call for call in inference_calls}
            excluded: Counter[str] = Counter()
            skipped: Counter[str] = Counter()
            selected_attributed = stage.records("attributed_completions")
            referenced_ids = _stage_attributed_completions(
                attributed_result, by_id, scope, selected_attributed, skipped, excluded
            )
            del attributed_result
            selected_rollouts = stage.records("rollouts")

            def collect_rollout(rollout):
                if _select_rollout(rollout, by_id, scope, skipped, excluded):
                    selected_rollouts.append(rollout)
                    referenced_ids.update(
                        turn.inference_call_id
                        for segment in rollout.segments
                        for turn in segment
                    )

            rollout_result = derive_rollout_result(
                snapshot,
                mirror_snapshot,
                org_id,
                RolloutPolicy(
                    attribution=policy.attribution,
                    policy_version=ROLLOUT_IMPLEMENTATION_VERSION,
                    eval_fraction=policy.eval_fraction,
                ),
                policy_digest=policy.digest,
                mirror_repositories=disk_repositories,
                session_commit_observations=session_observations,
                as_of=context.as_of,
                repository_context=context,
                rollout_sink=collect_rollout,
            )
            selected_rollouts.seal()
            skipped.update(
                {
                    f"rollout.{key}": value
                    for key, value in rollout_result.skipped.items()
                }
            )
            quarantine_revision = snapshot.quarantine_revision(org_id)
            bundle = _finish_derived_bundle(
                snapshot,
                org_id=org_id,
                policy=policy,
                scope=scope,
                mirror_revisions=mirror_revisions,
                stage=stage,
                referenced_ids=referenced_ids,
                skipped=skipped,
                excluded=excluded,
                fragmented=rollout_result.fragmented,
                identity_evidence=identity_evidence,
                repository_identities=repository_identities,
                repository_renames=repository_renames,
                as_of=as_of,
                quarantine_revision=quarantine_revision,
            )
            validate_derived_bundle(bundle)
            return bundle


def _stage_attributed_completions(result, by_id, scope, rows, skipped, excluded):
    for counts, prefix in (
        (result.skipped, "attributed_completion"),
        (result.abandonment.skipped, "attributed_completion.abandonment"),
        (result.abandonment_skipped, "attributed_completion"),
    ):
        skipped.update({f"{prefix}.{key}": value for key, value in counts.items()})
    referenced_ids = set()
    for row in result.attributed_completions:
        completion = by_id.get(row.inference_call_id)
        if completion is None:
            skipped["attributed_completion.inference_call_not_found"] += 1
            continue
        if scope.users is not None and completion.user_id not in scope.users:
            excluded["attributed_completion_user_scope"] += 1
            continue
        if not _in_time_scope(scope, inference_observed_at(completion)):
            excluded["attributed_completion_time_scope"] += 1
            continue
        rows.append(row)
        referenced_ids.add(row.inference_call_id)
    rows.seal()
    return referenced_ids


def _select_rollout(rollout, by_id, scope, skipped, excluded):
    completions = [
        by_id.get(turn.inference_call_id)
        for segment in rollout.segments
        for turn in segment
    ]
    if any(completion is None for completion in completions):
        skipped["rollout.inference_call_not_found"] += 1
        return False
    if scope.users is not None:
        allowed = [completion.user_id in scope.users for completion in completions]
        if not all(allowed):
            reason = "mixed_user_rollout" if any(allowed) else "rollout_user_scope"
            excluded[reason] += 1
            return False
    if (scope.since is not None or scope.until is not None) and not any(
        _in_time_scope(scope, inference_observed_at(completion))
        for completion in completions
    ):
        excluded["rollout_time_scope"] += 1
        return False
    return True


def _finish_derived_bundle(
    snapshot: FactStore,
    *,
    org_id: str,
    policy: DerivationPolicy,
    scope: DerivationScope,
    mirror_revisions: Mapping[str, Mapping[str, str]],
    stage: BundleRecordStore,
    referenced_ids: set[str],
    skipped: Counter[str],
    excluded: Counter[str],
    fragmented: Mapping[RolloutFragmentReason, int],
    identity_evidence: tuple[InferenceCallIdentity, ...],
    repository_identities: tuple[RepositoryIdentityEvidence, ...],
    repository_renames: tuple[RepositoryRename, ...],
    as_of: datetime | None,
    quarantine_revision: int,
) -> DerivedBundle:
    """Stage referenced Facts in the snapshot's canonical deterministic order."""
    referenced = stage.records("inference_calls")
    with contextlib.closing(
        snapshot.iter_inference_calls_by_ids(org_id, referenced_ids)
    ) as calls:
        referenced.extend(calls)
    referenced.seal()
    return DerivedBundle(
        org_id=org_id,
        policy=policy,
        scope=scope,
        as_of=as_of,
        quarantine_revision=quarantine_revision,
        mirror_revisions=mirror_revisions,
        attributed_completions=stage.records("attributed_completions"),
        rollouts=stage.records("rollouts"),
        inference_calls=referenced,
        inference_call_identities=identity_evidence,
        repository_identities=repository_identities,
        repository_renames=repository_renames,
        skipped=dict(sorted(skipped.items())),
        excluded=dict(sorted(excluded.items())),
        fragmented=dict(sorted(fragmented.items())),
    )


def _in_time_scope(scope: DerivationScope, occurred_at: datetime) -> bool:
    if scope.since is not None and occurred_at < scope.since:
        return False
    return scope.until is None or occurred_at < scope.until


def write_derived_bundle(
    bundle: DerivedBundle, destination: Path, *, limits: BundleLimits | None = None
) -> Path:
    """Write a private bundle atomically without replacing a destination."""

    if destination.exists():
        raise FileExistsError(
            f"derived bundle destination already exists: {destination}"
        )
    parent = destination.parent
    if not parent.is_dir():
        raise FileNotFoundError(f"derived bundle parent does not exist: {parent}")

    validate_derived_bundle(bundle)
    rows = {
        "attributed_completions": bundle.attributed_completions,
        "rollouts": bundle.rollouts,
        "inference_calls": bundle.inference_calls,
        "repository_identities": tuple(
            sorted(bundle.repository_identities, key=_repository_evidence_sort_key)
        ),
        "repository_renames": tuple(
            sorted(
                bundle.repository_renames,
                key=lambda row: (row.captured_at.astimezone(UTC), row.rename_id),
            )
        ),
        "inference_call_identities": tuple(
            sorted(
                bundle.inference_call_identities,
                key=lambda item: (
                    item.observed_at.astimezone(UTC),
                    item.inference_call_id,
                ),
            )
        ),
    }
    with BundleRecordStore(
        limits=limits, temporary_parent=parent, _prefix=f".{destination.name}."
    ) as stage:
        for name, values in rows.items():
            stage.records(name).extend(values)
            stage.records(name).seal()
        return _publish_bundle(bundle, destination, rows, stage)


def _publish_bundle(bundle, destination, rows, stage):
    metadata = {
        name: {
            "path": _ARTIFACT_FILES[name],
            "rows": len(records),
            "bytes": records._bytes,
            "sha256": records._digest.hexdigest(),
        }
        for name, records in stage._records.items()
    }
    file_metadata = BundleFiles(
        attributed_completions=BundleAttributedCompletionsFile(
            **metadata["attributed_completions"]
        ),
        rollouts=BundleRolloutsFile(**metadata["rollouts"]),
        inference_calls=BundleInferenceCallsFile(**metadata["inference_calls"]),
        inference_call_identities=BundleInferenceCallIdentitiesFile(
            **metadata["inference_call_identities"]
        ),
        repository_identities=BundleRepositoryIdentitiesFile(
            **metadata["repository_identities"]
        ),
        repository_renames=BundleRepositoryRenamesFile(
            **metadata["repository_renames"]
        ),
    )
    manifest = BundleManifest(
        bundle_schema_version=_BUNDLE_SCHEMA_VERSION,
        record_encoding=_RECORD_ENCODING,
        identity_population=_IDENTITY_POPULATION,
        repository_population=_IDENTITY_POPULATION,
        org_id=bundle.org_id,
        scope=BundleScope(
            since=bundle.scope.since,
            until=bundle.scope.until,
            users=list(bundle.scope.users) if bundle.scope.users is not None else None,
        ),
        policy=_bundle_policy(bundle.policy),
        policy_digest=bundle.policy.digest,
        implementation_versions=_implementation_versions(),
        quarantine_revision=bundle.quarantine_revision,
        as_of=bundle.as_of,
        mirror_revisions=bundle.mirror_revisions,
        counts=BundleArtifactCounts(
            attributed_completions=len(rows["attributed_completions"]),
            rollouts=len(rows["rollouts"]),
            inference_calls=len(rows["inference_calls"]),
            inference_call_identities=len(rows["inference_call_identities"]),
            repository_identities=len(rows["repository_identities"]),
            repository_renames=len(rows["repository_renames"]),
        ),
        skipped=bundle.skipped,
        excluded=bundle.excluded,
        files=file_metadata,
        fragmented=bundle.fragmented,
    )
    manifest_bytes = _json_bytes(manifest) + b"\n"

    temporary = stage.directory
    stage._reserve(len(manifest_bytes))
    _write_private(temporary / "manifest.json", manifest_bytes)
    _fsync_directory(temporary)
    if destination.exists():
        raise FileExistsError(
            f"derived bundle destination already exists: {destination}"
        )
    temporary.rename(destination)
    _fsync_directory(destination.parent)
    return destination


def read_derived_bundle(
    source: Path, *, limits: BundleLimits | None = None
) -> DerivedBundle:
    """Explicitly materialize a validated small bundle, within its byte budget."""
    limits = limits or BundleLimits()
    with open_derived_bundle(source, limits=limits) as bundle:
        return _materialize_bundle(bundle, limits)


def _materialize_bundle(bundle: DerivedBundle, limits: BundleLimits) -> DerivedBundle:
    size = sum(
        getattr(bundle, name).encoded_bytes
        for name in ("attributed_completions", "rollouts", "inference_calls")
    )
    if size > limits.max_materialized_bytes:
        raise BundleCapacityError(
            "bundle materialization exceeds byte budget; use "
            "open_derived_bundle or build_derived_bundle_context"
        )
    return replace(
        bundle,
        attributed_completions=tuple(bundle.attributed_completions),
        rollouts=tuple(bundle.rollouts),
        inference_calls=tuple(bundle.inference_calls),
    )


@contextmanager
def open_derived_bundle(
    source: Path,
    *,
    limits: BundleLimits | None = None,
    temporary_parent: Path | None = None,
) -> Iterator[DerivedBundle]:
    """Yield validated file-backed records from a private immutable snapshot.

    Validation completes before yielding. Access expires when the context exits.
    Set temporary_parent to a private disk-backed location for large workloads.
    """
    with BundleRecordStore(limits=limits, temporary_parent=temporary_parent) as stage:
        yield _read_derived_bundle(source, stage)


def _read_derived_bundle(source: Path, stage: BundleRecordStore) -> DerivedBundle:
    """Validate and reconstruct a canonical derived bundle from disk."""

    manifest_path = source / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise BundleValidationError("manifest.json is missing or not a regular file")
    try:
        with manifest_path.open("rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            if size > min(stage.limits.max_record_bytes, 8 * 1024 * 1024):
                raise BundleCapacityError("bundle manifest exceeds byte budget")
            data = handle.read(size + 1)
        if len(data) != size:
            raise BundleValidationError("bundle manifest changed while reading")
        stage._reserve(len(data))
        manifest = _load_json(data.decode("utf-8"), "manifest.json")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BundleValidationError(f"manifest.json is invalid: {exc}") from exc
    if not isinstance(manifest, dict):
        raise BundleValidationError("manifest.json must contain an object")
    if (
        type(manifest.get("bundle_schema_version")) is not int
        or manifest.get("bundle_schema_version") != _BUNDLE_SCHEMA_VERSION
    ):
        raise BundleValidationError(
            f"unsupported bundle_schema_version {manifest.get('bundle_schema_version')!r}; recompute the bundle from Facts"
        )

    _require_keys(
        manifest,
        {
            "bundle_schema_version",
            "record_encoding",
            "identity_population",
            "repository_population",
            "fragmented",
            "org_id",
            "scope",
            "policy",
            "policy_digest",
            "implementation_versions",
            "quarantine_revision",
            "as_of",
            "mirror_revisions",
            "counts",
            "skipped",
            "excluded",
            "files",
        },
        "manifest",
    )
    if manifest["record_encoding"] != _RECORD_ENCODING:
        raise BundleValidationError("unsupported record_encoding; recompute the bundle")
    if manifest["identity_population"] != _IDENTITY_POPULATION:
        raise BundleValidationError(
            "unsupported identity_population; recompute the bundle"
        )

    if manifest["repository_population"] != _IDENTITY_POPULATION:
        raise BundleValidationError(
            "unsupported repository_population; recompute the bundle"
        )

    policy = _policy_from_manifest(manifest["policy"])
    if manifest["policy_digest"] != policy.digest:
        raise BundleValidationError("policy_digest does not match the resolved policy")
    if manifest["implementation_versions"] != _json_value(_implementation_versions()):
        raise BundleValidationError(
            "implementation_versions do not match the bundle implementation"
        )
    scope = _scope_from_manifest(manifest["scope"])
    files = manifest["files"]
    if not isinstance(files, dict) or set(files) != set(_ARTIFACT_FILES):
        raise BundleValidationError("manifest files must name the six artifacts")

    decoded = {}
    for name, expected_path in _ARTIFACT_FILES.items():
        metadata = files[name]
        if not isinstance(metadata, dict):
            raise BundleValidationError(f"files.{name} must be an object")
        _require_keys(metadata, {"path", "rows", "bytes", "sha256"}, f"files.{name}")
        if metadata["path"] != expected_path:
            raise BundleValidationError(f"files.{name}.path must be {expected_path!r}")
        _non_negative_integer(metadata["rows"], f"files.{name}.rows")
        if name == "inference_call_identities" and metadata["rows"] > _IDENTITY_LIMIT:
            raise BundleValidationError("identity population exceeds row limit")
        if (
            name in {"repository_identities", "repository_renames"}
            and metadata["rows"] > REPOSITORY_IDENTITY_LIMIT
        ):
            raise BundleValidationError("repository population exceeds row limit")
        _non_negative_integer(metadata["bytes"], f"files.{name}.bytes")
        _sha256(metadata["sha256"], f"files.{name}.sha256")
        records = stage.records(name)
        records._capture(source / expected_path, metadata)
        decoded[name] = records

    attributed = decoded["attributed_completions"]
    rollouts = decoded["rollouts"]
    inference_calls = decoded["inference_calls"]
    identities = tuple(decoded["inference_call_identities"])
    repository_identities = tuple(decoded["repository_identities"])
    repository_renames = tuple(decoded["repository_renames"])
    counts = manifest["counts"]
    expected_counts = {
        "attributed_completions": len(attributed),
        "rollouts": len(rollouts),
        "inference_calls": len(inference_calls),
        "inference_call_identities": len(identities),
        "repository_identities": len(repository_identities),
        "repository_renames": len(repository_renames),
    }
    if counts != expected_counts:
        raise BundleValidationError("manifest counts do not match artifact rows")
    _counter_mapping(counts, "counts")
    bundle = DerivedBundle(
        org_id=manifest["org_id"],
        policy=policy,
        scope=scope,
        as_of=_datetime_or_none(manifest["as_of"], "as_of"),
        quarantine_revision=_non_negative_integer(
            manifest["quarantine_revision"], "quarantine_revision"
        ),
        mirror_revisions=_string_mapping_of_mappings(
            manifest["mirror_revisions"], "mirror_revisions"
        ),
        attributed_completions=attributed,
        rollouts=rollouts,
        inference_calls=inference_calls,
        inference_call_identities=identities,
        repository_identities=repository_identities,
        repository_renames=repository_renames,
        skipped=_counter_mapping(manifest["skipped"], "skipped"),
        excluded=_counter_mapping(manifest["excluded"], "excluded"),
        fragmented=_fragmentation(manifest["fragmented"]),
    )
    validate_derived_bundle(bundle)
    return bundle


def _inference_call_from_dict(value: dict[str, Any]) -> InferenceCall:
    return _fact_from_dict(InferenceCall, value, "inference-call fact")


def _identity_from_dict(value: dict[str, Any]) -> InferenceCallIdentity:
    name = "Inference call identity"
    _require_keys(value, {item.name for item in fields(InferenceCallIdentity)}, name)
    try:
        identity = _IDENTITY_ADAPTER.validate_python(value)
    except ValueError as exc:
        raise BundleValidationError(f"{name} is invalid: {exc}") from exc
    _check_unchanged(
        value,
        {item.name: getattr(identity, item.name) for item in fields(identity)},
        name,
    )
    if tuple(sorted(set(identity.call_ids))) != identity.call_ids:
        raise BundleValidationError(f"{name} aliases must be sorted and distinct")
    return identity


def _private_json_chunks(value):
    # Escaped surrogate pairs and astral characters share public JSON spelling.
    # Private execution files preserve their distinct code points until the
    # trainer's representation checks run. They are never published as bundles.
    for chunk in json.JSONEncoder(
        ensure_ascii=False, allow_nan=True, separators=(",", ":")
    ).iterencode(value):
        yield chunk.encode("utf-8", "surrogatepass")
    yield b"\n"


def _private_record_chunks(row):
    yield from _private_json_chunks(_json_value(row))


def _decode_private_record(data, name):
    return _load_json(data.decode("utf-8", "surrogatepass"), name, extended=True)


def _record_loader(name):
    return {
        "attributed_completions": _attributed_from_dict,
        "rollouts": _rollout_from_dict,
        "inference_calls": _inference_call_from_dict,
        "inference_call_identities": _identity_from_dict,
        "repository_identities": _repository_evidence_from_dict,
        "repository_renames": lambda row: _fact_from_dict(
            RepositoryRename, row, "RepositoryRename"
        ),
    }[name]


def _decode_record(data: bytes, name: str) -> dict:
    try:
        envelope = _load_json(data.decode("utf-8"), name)
    except UnicodeError as exc:
        raise BundleValidationError(f"{name} is invalid UTF-8") from exc
    _require_keys(envelope, {"record_json"}, name)
    if not isinstance(envelope["record_json"], str):
        raise BundleValidationError(f"{name} record_json must be a string")
    record = _load_json(envelope["record_json"], name + " record_json", extended=True)
    if not isinstance(record, dict):
        raise BundleValidationError(f"{name} record_json must contain an object")
    return record


def _json_chunks(value: Any, *, protocol: bool = False) -> Iterator[str]:
    """Encode canonical values without constructing a recursive JSON copy.

    Splitting string escaping at Unicode code-point boundaries preserves the
    exact ASCII JSON spelling, including lone surrogates and exceptional floats.
    """
    if not protocol and isinstance(value, BaseModel):
        yield "{"
        for index, name in enumerate(sorted(type(value).model_fields)):
            if index:
                yield ","
            yield json.dumps(name) + ":"
            annotation = type(value).model_fields[name].annotation
            yield from _json_chunks(
                getattr(value, name),
                protocol=annotation is Any or Any in get_args(annotation),
            )
        yield "}"
    elif not protocol and isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise BundleValidationError("canonical timestamp must be timezone-aware")
        yield json.dumps(value.astimezone(UTC).isoformat())
    elif not protocol and isinstance(value, Enum):
        yield from _json_chunks(value.value)
    elif not protocol and is_dataclass(value):
        yield "{"
        for index, item in enumerate(sorted(fields(value), key=lambda item: item.name)):
            if index:
                yield ","
            yield json.dumps(item.name) + ":"
            yield from _json_chunks(getattr(value, item.name))
        yield "}"
    elif isinstance(value, dict) or (not protocol and isinstance(value, Mapping)):
        if any(not isinstance(key, str) for key in value):
            raise BundleValidationError("canonical record object keys must be strings")
        yield "{"
        for index, key in enumerate(sorted(value)):
            if index:
                yield ","
            yield from _json_chunks(str(key))
            yield ":"
            yield from _json_chunks(value[key], protocol=protocol)
        yield "}"
    elif isinstance(value, list) or (not protocol and isinstance(value, tuple)):
        yield "["
        for index, item in enumerate(value):
            if index:
                yield ","
            yield from _json_chunks(item, protocol=protocol)
        yield "]"
    elif type(value) is str:
        yield '"'
        for offset in range(0, len(value), 16 * 1024):
            yield json.dumps(value[offset : offset + 16 * 1024], ensure_ascii=True)[
                1:-1
            ]
        yield '"'
    elif value is None or type(value) in (int, float, bool):
        yield json.dumps(value, allow_nan=True, separators=(",", ":"))
    else:
        raise BundleValidationError(
            f"unsupported canonical value type {type(value).__name__}"
        )


def _canonical_digest(value: Any) -> bytes:
    digest = hashlib.sha256()
    for chunk in _json_chunks(value):
        digest.update(chunk.encode("ascii"))
    return digest.digest()


def _record_chunks(row: Any) -> Iterator[bytes]:
    yield b'{"record_json":"'
    buffer = bytearray()
    for chunk in _json_chunks(row):
        # The outer envelope escapes the inner canonical JSON string once more.
        buffer.extend(json.dumps(chunk, ensure_ascii=True)[1:-1].encode("ascii"))
        if len(buffer) >= 64 * 1024:
            yield bytes(buffer)
            buffer.clear()
    if buffer:
        yield bytes(buffer)
    yield b'"}\n'


def _validate_artifact_record(row, attributed):
    if attributed:
        _attributed_from_dict(_json_value(row))
        return
    # Validate one Turn at a time through the same wire schema owner. Keeping a
    # second complete reconstructed Rollout provides no additional evidence.
    _rollout_from_dict(_json_value(replace(row, segments=[])))
    if not isinstance(row.segments, list | tuple):
        raise BundleValidationError("rollout segments must be an array")
    for segment in row.segments:
        if not isinstance(segment, list | tuple):
            raise BundleValidationError("every rollout segment must be an array")
        for turn in segment:
            _turn_from_dict(_json_value(turn))


def _jsonl_bytes(rows: tuple[Any, ...]) -> bytes:
    return b"".join(
        _json_bytes(BundleRecord(_json_bytes(row, extended=True).decode("ascii")))
        + b"\n"
        for row in rows
    )


def _bundle_policy(policy: DerivationPolicy) -> BundlePolicy:
    return BundlePolicy(
        schema_version=1,
        attribution=BundleAttributionPolicy(
            post_push_grace_period_minutes=(
                policy.attribution.post_push_grace_period_minutes
            ),
            max_commits_per_push=policy.attribution.max_commits_per_push,
            git_notes=BundleSimilarityPolicy(
                min_similarity=policy.attribution.git_notes.min_similarity,
                lookback_window_minutes=(
                    policy.attribution.git_notes.lookback_window_minutes
                ),
            ),
            jaccard=BundleSimilarityPolicy(
                min_similarity=policy.attribution.jaccard.min_similarity,
                lookback_window_minutes=(
                    policy.attribution.jaccard.lookback_window_minutes
                ),
            ),
        ),
        split=BundleSplitPolicy(eval_fraction=policy.eval_fraction),
    )


def _implementation_versions() -> BundleImplementationVersions:
    return BundleImplementationVersions(
        abandonment=AbandonmentPolicy().policy_version,
        attribution=AttributionPolicy().policy_version,
        attributed_completion=ATTRIBUTED_COMPLETION_IMPLEMENTATION_VERSION,
        rollout=ROLLOUT_IMPLEMENTATION_VERSION,
        repository_identity="1",
    )


def _json_bytes(value: Any, *, extended: bool = False) -> bytes:
    return json.dumps(
        _json_value(value),
        ensure_ascii=True,
        allow_nan=extended,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return {
            name: (
                _protocol_value(getattr(value, name))
                if item.annotation is Any or Any in get_args(item.annotation)
                else _json_value(getattr(value, name))
            )
            for name, item in type(value).model_fields.items()
        }
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise BundleValidationError("canonical timestamp must be timezone-aware")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {
            item.name: _json_value(getattr(value, item.name)) for item in fields(value)
        }
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise BundleValidationError("canonical record object keys must be strings")
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or type(value) in (str, int, float, bool):
        return value
    raise BundleValidationError(
        f"unsupported canonical value type {type(value).__name__}"
    )


def _protocol_value(value: Any) -> Any:
    if value is None or type(value) in (str, int, float, bool):
        return value
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _protocol_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_protocol_value(item) for item in value]
    raise BundleValidationError(
        f"canonical protocol value has unsupported type {type(value).__name__}"
    )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _datetime_or_none(value: Any, name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(_TIMESTAMP_PATTERN, value):
        raise BundleValidationError(f"{name} must be an RFC 3339 string or null")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise BundleValidationError(f"{name} is not a valid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BundleValidationError(f"{name} must be timezone-aware")
    return parsed


def _load_json(text: str, name: str, *, extended: bool = False) -> Any:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in pairs:
            if key in out:
                raise BundleValidationError(
                    f"{name} contains duplicate object key {key!r}"
                )
            out[key] = value
        return out

    def constant(value: str) -> float:
        if not extended:
            raise BundleValidationError(
                f"{name} contains non-finite numeric token {value}"
            )
        return {"NaN": math.nan, "Infinity": math.inf, "-Infinity": -math.inf}[value]

    try:
        return json.loads(text, object_pairs_hook=object_pairs, parse_constant=constant)
    except (ValueError, TypeError) as exc:
        raise BundleValidationError(f"{name} is invalid JSON: {exc}") from exc


def _policy_from_manifest(value: Any) -> DerivationPolicy:
    if not isinstance(value, dict):
        raise BundleValidationError("policy must be an object")
    _require_keys(value, set(_ROOT_FIELDS), "policy")
    attribution = value["attribution"]
    split = value["split"]
    if not isinstance(attribution, dict) or not isinstance(split, dict):
        raise BundleValidationError("policy attribution and split must be objects")
    _require_keys(attribution, set(_ATTRIBUTION_FIELDS), "policy.attribution")
    git_notes = attribution["git_notes"]
    jaccard = attribution["jaccard"]
    if not isinstance(git_notes, dict) or not isinstance(jaccard, dict):
        raise BundleValidationError(
            "policy attribution.git_notes and attribution.jaccard must be objects"
        )
    _require_keys(git_notes, set(_SIMILARITY_FIELDS), "policy.attribution.git_notes")
    _require_keys(jaccard, set(_SIMILARITY_FIELDS), "policy.attribution.jaccard")
    _require_keys(split, set(_SPLIT_FIELDS), "policy.split")
    try:
        return DerivationPolicy(
            schema_version=value["schema_version"],
            attribution=AttributionPolicy(
                post_push_grace_period_minutes=attribution[
                    "post_push_grace_period_minutes"
                ],
                max_commits_per_push=attribution["max_commits_per_push"],
                git_notes=SimilarityPolicy(**git_notes),
                jaccard=SimilarityPolicy(**jaccard),
            ),
            eval_fraction=split["eval_fraction"],
        )
    except (TypeError, ValueError) as exc:
        raise BundleValidationError(f"policy is invalid: {exc}") from exc


def _scope_from_manifest(value: Any) -> DerivationScope:
    if not isinstance(value, dict):
        raise BundleValidationError("scope must be an object")
    _require_keys(value, {"since", "until", "users"}, "scope")
    users = value["users"]
    if users is not None and (
        not isinstance(users, list) or any(not isinstance(user, str) for user in users)
    ):
        raise BundleValidationError("scope.users must be an array of strings or null")
    try:
        scope = DerivationScope(
            since=_datetime_or_none(value["since"], "scope.since"),
            until=_datetime_or_none(value["until"], "scope.until"),
            users=tuple(users) if users is not None else None,
        )
        if users is not None and list(scope.users) != users:
            raise BundleValidationError(
                "scope.users must be normalized, sorted and unique"
            )
        return scope
    except ValueError as exc:
        raise BundleValidationError(f"scope is invalid: {exc}") from exc


def _attributed_from_dict(value: dict[str, Any]) -> AttributedCompletion:
    _require_keys(
        value,
        {
            "org_id",
            "session_id",
            "inference_call_id",
            "repo",
            "commit_sha",
            "file_path",
            "similarity_score",
            "attribution_source",
            "repository_identity",
            "source_push_id",
            "abandonment",
            "decisions",
            "ci_outcomes",
            "provenance",
            "split",
            "session_commit_observations",
        },
        "attributed completion",
    )
    _validate_split(value["split"])
    for name in ("org_id", "session_id", "inference_call_id"):
        _non_empty_string(value[name], f"attributed completion {name}")
    _list(value["decisions"], "attributed completion decisions")
    _list(value["ci_outcomes"], "attributed completion ci_outcomes")
    abandonment = _abandonment_from_dict(value["abandonment"])
    if abandonment is None:
        for name in ("repo", "file_path"):
            _non_empty_string(value[name], f"attributed completion {name}")
        _commit_sha(value["commit_sha"], "attributed completion commit_sha")
        score = value["similarity_score"]
        if (
            not isinstance(score, int | float)
            or isinstance(score, bool)
            or not 0.0 <= score <= 1.0
        ):
            raise BundleValidationError(
                "attributed completion similarity_score must be a number between 0 and 1"
            )
        try:
            attribution_source = AttributionSource(value["attribution_source"])
        except ValueError as exc:
            raise BundleValidationError(
                "attributed completion attribution_source is invalid"
            ) from exc
    else:
        if any(
            value[name] is not None
            for name in (
                "repo",
                "commit_sha",
                "file_path",
                "similarity_score",
                "attribution_source",
                "repository_identity",
                "source_push_id",
            )
        ):
            raise BundleValidationError(
                "abandonment attributed completion fields must be null"
            )
        attribution_source = None
    try:
        return AttributedCompletion(
            org_id=value["org_id"],
            session_id=value["session_id"],
            inference_call_id=value["inference_call_id"],
            repo=value["repo"],
            commit_sha=value["commit_sha"],
            file_path=value["file_path"],
            similarity_score=value["similarity_score"],
            attribution_source=attribution_source,
            repository_identity=_repository_identity_from_dict(
                value["repository_identity"]
            ),
            source_push_id=None
            if value["source_push_id"] is None
            else _non_empty_string(value["source_push_id"], "source_push_id"),
            decisions=[
                _fact_from_dict(DeveloperDecision, row, "Developer decision")
                for row in value["decisions"]
            ],
            ci_outcomes=[_ci_outcome_from_dict(row) for row in value["ci_outcomes"]],
            provenance=_provenance_from_dict(
                value["provenance"], "attributed completion provenance"
            ),
            split=value["split"],
            session_commit_observations=tuple(
                _fact_from_dict(SessionCommitObservation, item, "Session observation")
                for item in _list(
                    value["session_commit_observations"], "session_commit_observations"
                )
            ),
            abandonment=abandonment,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise BundleValidationError(f"attributed completion is invalid: {exc}") from exc


def _abandonment_from_dict(value: Any) -> SessionAbandonment | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise BundleValidationError(
            "attributed completion abandonment must be an object"
        )
    _require_keys(
        value,
        {
            "org_id",
            "session_id",
            "accepted_decisions",
            "explicit_accepted_decisions",
            "last_decision_at",
            "as_of",
            "provenance",
        },
        "attributed completion abandonment",
    )
    for name in ("org_id", "session_id"):
        _non_empty_string(value[name], f"attributed completion abandonment {name}")
    accepted = value["accepted_decisions"]
    explicit = value["explicit_accepted_decisions"]
    if type(accepted) is not int or accepted < 1:
        raise BundleValidationError(
            "attributed completion abandonment accepted_decisions must be positive"
        )
    if type(explicit) is not int or not 0 <= explicit <= accepted:
        raise BundleValidationError(
            "attributed completion abandonment explicit_accepted_decisions must be "
            "between zero and accepted_decisions"
        )
    last_decision_at = _datetime_or_none(
        value["last_decision_at"],
        "attributed completion abandonment last_decision_at",
    )
    as_of = _datetime_or_none(value["as_of"], "attributed completion abandonment as_of")
    if last_decision_at is None or as_of is None:
        raise BundleValidationError(
            "attributed completion abandonment timestamps cannot be null"
        )
    if as_of < last_decision_at:
        raise BundleValidationError(
            "attributed completion abandonment as_of cannot precede last_decision_at"
        )
    return SessionAbandonment(
        org_id=value["org_id"],
        session_id=value["session_id"],
        accepted_decisions=accepted,
        explicit_accepted_decisions=explicit,
        last_decision_at=last_decision_at,
        as_of=as_of,
        provenance=_provenance_from_dict(
            value["provenance"], "attributed completion abandonment provenance"
        ),
    )


def _rollout_from_dict(value: dict[str, Any]) -> Rollout:
    _require_keys(
        value,
        {
            "org_id",
            "session_id",
            "segments",
            "commits",
            "attribution_source",
            "terminal_outcomes",
            "provenance",
            "split",
            "session_commit_observations",
        },
        "rollout",
    )
    _validate_split(value["split"])
    for name in ("org_id", "session_id"):
        _non_empty_string(value[name], f"rollout {name}")
    segments_value = _list(value["segments"], "rollout segments")
    if any(not isinstance(segment, list) for segment in segments_value):
        raise BundleValidationError("every rollout segment must be an array")
    commits = [
        _commit_ref_from_dict(item)
        for item in _list(value["commits"], "rollout commits")
    ]
    _list(value["terminal_outcomes"], "rollout terminal_outcomes")
    try:
        segments = [
            [_turn_from_dict(turn) for turn in segment] for segment in value["segments"]
        ]
        return Rollout(
            org_id=value["org_id"],
            session_id=value["session_id"],
            segments=segments,
            commits=commits,
            attribution_source=AttributionSource(value["attribution_source"]),
            terminal_outcomes=[
                _ci_outcome_from_dict(row) for row in value["terminal_outcomes"]
            ],
            provenance=_provenance_from_dict(value["provenance"], "rollout provenance"),
            split=value["split"],
            session_commit_observations=tuple(
                _fact_from_dict(SessionCommitObservation, item, "Session observation")
                for item in _list(
                    value["session_commit_observations"], "session_commit_observations"
                )
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise BundleValidationError(f"rollout is invalid: {exc}") from exc


def _commit_ref_from_dict(value: Any) -> CommitRef:
    if not isinstance(value, dict):
        raise BundleValidationError("rollout commit must be an object")
    _require_keys(
        value, {"repo", "commit_sha", "repository_identity"}, "rollout commit"
    )
    return CommitRef(
        repo=_non_empty_string(value["repo"], "rollout commit repo"),
        commit_sha=_commit_sha(value["commit_sha"], "rollout commit commit_sha"),
        repository_identity=_repository_identity_from_dict(
            value["repository_identity"]
        ),
    )


def _ci_outcome_from_dict(value: Any) -> CIOutcome:
    if not isinstance(value, dict):
        raise BundleValidationError("CI outcome must be an object")
    _require_keys(value, set(CIOutcome.model_fields), "CI outcome")
    try:
        return _fact_from_dict(CIOutcome, value, "CI outcome")
    except (TypeError, ValueError) as exc:
        raise BundleValidationError(f"CI outcome is invalid: {exc}") from exc


def _turn_from_dict(value: Any) -> Turn:
    if not isinstance(value, dict):
        raise BundleValidationError("rollout turn must be an object")
    _require_keys(
        value,
        {"new_messages", "completion", "decisions", "inference_call_id", "tool_calls"},
        "rollout turn",
    )
    _list(value["new_messages"], "rollout turn new_messages")
    _non_empty_string(value["inference_call_id"], "rollout turn inference_call_id")
    if not isinstance(value["completion"], str):
        raise BundleValidationError("rollout turn completion must be a string")
    _list(value["decisions"], "rollout turn decisions")
    _list(value["tool_calls"], "rollout turn tool_calls")
    return Turn(
        new_messages=[
            _fact_from_dict(InferenceMessage, row, "Inference message")
            for row in value["new_messages"]
        ],
        completion=value["completion"],
        decisions=tuple(
            _fact_from_dict(DeveloperDecision, row, "Developer decision")
            for row in value["decisions"]
        ),
        inference_call_id=value["inference_call_id"],
        tool_calls=tuple(
            _fact_from_dict(ToolCallPart, row, "Tool call")
            for row in value["tool_calls"]
        ),
    )


def _validate_split(value: Any) -> None:
    if not isinstance(value, str) or value not in {"train", "eval"}:
        raise BundleValidationError(f"invalid split {value!r}")


def _non_empty_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BundleValidationError(f"{name} must be a non-empty string")
    try:
        _SCALAR_IDENTITY.validate_python(value, strict=True)
    except ValueError as exc:
        raise BundleValidationError(f"{name} is invalid: {exc}") from exc
    return value


def _non_negative_integer(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise BundleValidationError(f"{name} must be a non-negative integer")
    return value


def _provenance_from_dict(value: Any, name: str) -> Provenance:
    if not isinstance(value, dict):
        raise BundleValidationError(f"{name} must be an object")
    _require_keys(
        value,
        {"policy_version", "quarantine_revision", "policy_digest"},
        name,
    )
    try:
        return Provenance(
            policy_version=value["policy_version"],
            quarantine_revision=value["quarantine_revision"],
            policy_digest=value["policy_digest"],
        )
    except (TypeError, ValueError) as exc:
        raise BundleValidationError(f"{name} is invalid: {exc}") from exc


def _commit_sha(value: Any, name: str) -> str:
    value = _non_empty_string(value, name)
    if len(value) not in {40, 64} or any(
        char not in "0123456789abcdef" for char in value
    ):
        raise BundleValidationError(f"{name} must be a full lowercase commit SHA")
    return value


def _list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise BundleValidationError(f"{name} must be an array")
    return value


def _require_keys(value: dict[str, Any], expected: set[str], name: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise BundleValidationError(f"{name} fields do not match the bundle schema")


def _counter_mapping(value: Any, name: str) -> dict[str, int]:
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or type(count) is not int or count < 0
        for key, count in value.items()
    ):
        raise BundleValidationError(f"{name} must map strings to non-negative integers")
    return dict(sorted(value.items()))


def _string_mapping_of_mappings(value: Any, name: str) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict):
        raise BundleValidationError(f"{name} must be an object")
    out: dict[str, dict[str, str]] = {}
    for key, nested in value.items():
        if (
            not isinstance(key, str)
            or not isinstance(nested, dict)
            or any(
                not isinstance(nested_key, str) or not isinstance(nested_value, str)
                for nested_key, nested_value in nested.items()
            )
        ):
            raise BundleValidationError(f"{name} must map strings to string mappings")
        out[key] = dict(sorted(nested.items()))
    return dict(sorted(out.items()))


def _sha256(value: Any, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise BundleValidationError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _fragmentation(value: Any) -> dict[str, int]:
    counts = _counter_mapping(value, "fragmented")
    if set(counts) - set(get_args(RolloutFragmentReason)):
        raise BundleValidationError("fragmented contains an unknown boundary reason")
    return counts


def _fact_from_dict(model: type[BaseModel], value: Any, name: str) -> Any:
    """Validate canonical fields without repairing or coercing their values."""
    _require_keys(value, set(model.model_fields), name)
    try:
        fact = model.model_validate(value)
        _check_unchanged(value, fact, name)
        return fact
    except (TypeError, ValueError) as exc:
        raise BundleValidationError(f"{name} is invalid: {exc}") from exc


def _check_unchanged(value: Any, canonical: Any, name: str) -> None:
    # Pydantic accepts JSON timestamps and enum spellings, but also coerces
    # strings and booleans to numbers. An imported canonical record is already
    # normalized; the reader must not silently repair it through those rules.
    if isinstance(canonical, BaseModel):
        _require_keys(value, set(type(canonical).model_fields), name)
        for key in type(canonical).model_fields:
            _check_unchanged(value[key], getattr(canonical, key), f"{name}.{key}")
        return
    if isinstance(canonical, datetime):
        if _datetime_or_none(value, name) != canonical:
            raise BundleValidationError(f"{name} timestamp was coerced")
        return
    if isinstance(canonical, Enum):
        canonical = canonical.value
    if isinstance(canonical, dict):
        if not isinstance(value, dict) or value.keys() != canonical.keys():
            raise BundleValidationError(f"{name} object was coerced")
        for key in canonical:
            _check_unchanged(value[key], canonical[key], f"{name}.{key}")
        return
    if isinstance(canonical, list | tuple):
        if not isinstance(value, list) or len(value) != len(canonical):
            raise BundleValidationError(f"{name} array was coerced")
        for index, (original, item) in enumerate(zip(value, canonical, strict=True)):
            _check_unchanged(original, item, f"{name}[{index}]")
        return
    if type(canonical) is float and type(value) in (int, float):
        if value == canonical or (math.isnan(value) and math.isnan(canonical)):
            return
    if type(value) is not type(canonical) or value != canonical:
        raise BundleValidationError(f"{name} value was coerced or normalized")


def _mirror_revision_key(key) -> str:
    # Legacy labels stay readable; JSON tuples encode identified keys losslessly.
    if isinstance(key, LegacyRepositoryKey):
        return key.repo
    return json.dumps(repository_sort_key(key), separators=(",", ":"))


def _repository_evidence_sort_key(row):
    return (
        row.captured_at.astimezone(UTC),
        row.source_table,
        row.source_fact_id,
        row.role,
    )


def _repository_identity_from_dict(value):
    if value is None:
        return None
    _require_keys(value, {"provider", "host", "repository_id"}, "repository identity")
    try:
        identity = RepositoryIdentity(**value)
    except (TypeError, ValueError) as exc:
        raise BundleValidationError("repository identity is invalid") from exc
    _check_unchanged(value, _json_value(identity), "repository identity")
    return identity


def _repository_evidence_from_dict(value):
    _require_keys(
        value,
        {item.name for item in fields(RepositoryIdentityEvidence)},
        "repository evidence",
    )
    try:
        evidence = _REPOSITORY_EVIDENCE_ADAPTER.validate_python(value)
    except (TypeError, ValueError) as exc:
        raise BundleValidationError("repository evidence is invalid") from exc
    _check_unchanged(
        value,
        {item.name: getattr(evidence, item.name) for item in fields(evidence)},
        "repository evidence",
    )
    return evidence


def _bundle_repository_context(bundle: DerivedBundle) -> RepositoryContext:
    seen_sources, seen_renames = set(), set()
    for population, expected_type in (
        (bundle.repository_identities, RepositoryIdentityEvidence),
        (bundle.repository_renames, RepositoryRename),
    ):
        if not isinstance(population, tuple):
            raise BundleValidationError(
                "repository population must be an explicit tuple"
            )
        if len(population) > REPOSITORY_IDENTITY_LIMIT:
            raise BundleValidationError("repository population exceeds row limit")
        for item in population:
            if not isinstance(item, expected_type):
                raise BundleValidationError(
                    "repository population requires canonical records"
                )
            if (
                item.org_id != bundle.org_id
                or bundle.as_of is None
                or item.captured_at.astimezone(UTC) > bundle.as_of.astimezone(UTC)
            ):
                raise BundleValidationError(
                    "repository population organization or capture boundary disagrees"
                )
    for row in bundle.repository_identities:
        if not isinstance(row, RepositoryIdentityEvidence):
            raise BundleValidationError(
                "repository population requires canonical projections"
            )
        _repository_evidence_from_dict(_json_value(row))
        source = (row.source_table, row.source_fact_id, row.role)
        if source in seen_sources:
            raise BundleValidationError("duplicate repository source role")
        seen_sources.add(source)
    for row in bundle.repository_renames:
        if not isinstance(row, RepositoryRename):
            raise BundleValidationError(
                "repository population requires canonical rename Facts"
            )
        _fact_from_dict(RepositoryRename, _json_value(row), "RepositoryRename")
        if row.rename_id in seen_renames:
            raise BundleValidationError("duplicate repository rename")
        seen_renames.add(row.rename_id)
    try:
        return build_repository_context(
            bundle.repository_identities,
            bundle.repository_renames,
            bundle.org_id,
            as_of=bundle.as_of or datetime.min.replace(tzinfo=UTC),
        )
    except (TypeError, ValueError) as exc:
        raise BundleValidationError("repository population is invalid") from exc


def validate_derived_bundle(bundle: DerivedBundle) -> RepositoryContext:
    """Reject inconsistent internal claims against declared complete identities.

    Required before bundle-based training, including in-memory exports. This
    proves neither external producer truth nor the absence of undisclosed Facts.
    """
    _non_empty_string(bundle.org_id, "org_id")
    try:
        normalized_org = _ORG_ID.validate_python(bundle.org_id, strict=True)
    except ValueError as exc:
        raise BundleValidationError(f"org_id is invalid: {exc}") from exc
    if normalized_org != bundle.org_id:
        raise BundleValidationError("org_id must already be canonical")
    revision = _non_negative_integer(bundle.quarantine_revision, "quarantine_revision")
    if bundle.as_of is not None and (
        not isinstance(bundle.as_of, datetime)
        or bundle.as_of.tzinfo is None
        or bundle.as_of.utcoffset() is None
    ):
        raise BundleValidationError("as_of must be an aware datetime or null")
    _policy_from_manifest(bundle.policy.to_dict())
    _scope_from_manifest(_json_value(bundle.scope))
    _counter_mapping(bundle.skipped, "skipped")
    _counter_mapping(bundle.excluded, "excluded")
    _fragmentation(bundle.fragmented)
    _string_mapping_of_mappings(bundle.mirror_revisions, "mirror_revisions")

    context = _bundle_repository_context(bundle)

    if not isinstance(bundle.inference_call_identities, tuple):
        raise BundleValidationError("identity population must be an explicit tuple")
    if len(bundle.inference_call_identities) > _IDENTITY_LIMIT:
        raise BundleValidationError("identity population exceeds row limit")
    identities: dict[str, InferenceCallIdentity] = {}
    identities_by_session: dict[str, list[InferenceCallIdentity]] = {}
    for item in bundle.inference_call_identities:
        if not isinstance(item, InferenceCallIdentity):
            raise BundleValidationError(
                "identity population requires canonical identity records"
            )
        witness = _identity_from_dict(_json_value(item))
        name = f"Inference call identity {witness.inference_call_id!r}"
        if witness.inference_call_id in identities:
            raise BundleValidationError(f"{name}: duplicate identity")
        if witness.org_id != bundle.org_id:
            raise BundleValidationError(f"{name}: org_id does not match manifest")
        if bundle.as_of is None or witness.observed_at.astimezone(
            UTC
        ) > bundle.as_of.astimezone(UTC):
            raise BundleValidationError(f"{name}: observed_at exceeds or lacks as_of")
        identities[witness.inference_call_id] = witness
        identities_by_session.setdefault(witness.session_id, []).append(witness)

    calls: dict[str, int] = {}
    call_content_bytes: dict[str, int] = {}
    facts: dict[tuple[type, str], bytes] = {}
    carried_outcomes: dict[str, CIOutcomeProjection] = {}
    artifacts: dict[tuple[Any, ...], bytes] = {}
    decision_views: dict[tuple[bool, str], DeveloperDecisionProjection] = {}
    decision_digests: dict[tuple[bool, str], tuple[bytes, bytes]] = {}

    def remember_decision(
        decision: DeveloperDecision, attributed: bool, identity: str
    ) -> None:
        key = (attributed, decision.decision_id)
        payload = _canonical_digest(decision)
        captured_fields = _canonical_digest(
            {
                name: getattr(decision, name)
                for name in type(decision).model_fields
                if name != "edit_retention_score"
            }
        )
        other = decision_digests.get(key)
        if other is not None and other[0] != payload:
            raise BundleValidationError(
                f"{identity}: conflicting decision_id {decision.decision_id!r} within artifact view"
            )
        opposite_key = (not attributed, decision.decision_id)
        opposite = decision_views.get(opposite_key)
        if opposite is not None:
            derived, captured = (
                (decision, opposite) if attributed else (opposite, decision)
            )
            # ADR 0015 permits derived retention only when captured score is absent.
            if decision_digests[opposite_key][1] != captured_fields or (
                captured.edit_retention_score is not None
                and derived.edit_retention_score != captured.edit_retention_score
            ):
                raise BundleValidationError(
                    f"{identity}: conflicting captured fields for decision_id {decision.decision_id!r}"
                )
        decision_views[key] = DeveloperDecisionProjection(
            **{
                item.name: getattr(decision, item.name)
                for item in fields(DeveloperDecisionProjection)
            }
        )
        decision_digests[key] = payload, captured_fields

    def remember(fact: BaseModel, field_name: str, identity: str) -> None:
        key = (type(fact), getattr(fact, field_name))
        payload = _canonical_digest(fact)
        if key in facts and facts[key] != payload:
            raise BundleValidationError(
                f"{identity}: conflicting {field_name} {key[1]!r}"
            )
        facts[key] = payload

    for call_index, call in enumerate(bundle.inference_calls):
        identity = f"Inference call {call.inference_call_id!r}"
        _inference_call_from_dict(_json_value(call))
        if call.inference_call_id in calls:
            raise BundleValidationError(f"{identity}: duplicate inference-call ID")
        if call.org_id != bundle.org_id:
            raise BundleValidationError(f"{identity}: org_id does not match manifest")
        projected = InferenceCallIdentity(
            inference_call_id=call.inference_call_id,
            org_id=call.org_id,
            session_id=call.session_id,
            observed_at=call.observed_at.astimezone(UTC),
            call_ids=tuple(sorted(model_call_ids(call))),
        )
        witness = identities.get(call.inference_call_id)
        if (
            witness is None
            or replace(witness, observed_at=witness.observed_at.astimezone(UTC))
            != projected
        ):
            raise BundleValidationError(
                f"{identity}: identity evidence is absent or disagrees"
            )
        calls[call.inference_call_id] = call_index
        # Match FactStore's ASCII JSON content envelope without retaining encoded
        # copies or counting raw audit payloads that Session reconstruction omits.
        call_content_bytes[call.inference_call_id] = sum(
            len(chunk)
            for messages in (call.input_messages, call.output_messages)
            for chunk in _json_chunks(messages)
        )
    call = None
    referenced = set()

    for row in chain(bundle.attributed_completions, bundle.rollouts):
        attributed = isinstance(row, AttributedCompletion)
        identity = (
            f"Attributed completion {row.inference_call_id!r} {row.repo!r}/{row.commit_sha!r}/{row.file_path!r}"
            if attributed
            else f"Rollout Session {row.session_id!r}"
        )
        try:
            _validate_artifact_record(row, attributed)
        except BundleValidationError as exc:
            raise BundleValidationError(f"{identity}: {exc}") from exc
        if row.org_id != bundle.org_id:
            raise BundleValidationError(f"{identity}: org_id does not match manifest")
        if row.split != split_of(row.session_id, bundle.policy.eval_fraction):
            raise BundleValidationError(f"{identity}: split does not match policy")
        expected_version = (
            ATTRIBUTED_COMPLETION_IMPLEMENTATION_VERSION
            if attributed
            else ROLLOUT_IMPLEMENTATION_VERSION
        )
        if row.provenance != Provenance(
            expected_version, revision, bundle.policy.digest
        ):
            raise BundleValidationError(
                f"{identity}: provenance does not match manifest"
            )
        row_key = (
            (
                "attributed",
                row.org_id,
                row.session_id,
                row.inference_call_id,
                row.repository_identity,
                row.repo,
                row.commit_sha,
                row.file_path,
            )
            if attributed
            else ("rollout", row.org_id, row.session_id)
        )
        payload = _canonical_digest(row)
        if row_key in artifacts and artifacts[row_key] != payload:
            raise BundleValidationError(f"{identity}: conflicting artifact identity")
        artifacts[row_key] = payload
        if attributed:
            ids = [row.inference_call_id]
            decisions = row.decisions
            outcomes = row.ci_outcomes
            commits = set()
            if row.abandonment is None:
                commit = context.commit_key(
                    row.org_id,
                    row.repo,
                    row.commit_sha,
                    repository_identity=row.repository_identity,
                )
                if commit is None:
                    raise BundleValidationError(
                        f"{identity}: repository identity is unresolved"
                    )
                if (
                    row.repository_identity is not None
                    or row.source_push_id is not None
                ):
                    source = context.resolve_source(
                        FactTable.PUSHES, row.source_push_id
                    )
                    if source.key is None or source.key != commit.repository:
                        raise BundleValidationError(
                            f"{identity}: source Push identity is absent or disagrees"
                        )
                commits.add(commit)
            if row.abandonment is not None and row.abandonment.provenance != Provenance(
                AbandonmentPolicy().policy_version, revision, bundle.policy.digest
            ):
                raise BundleValidationError(
                    f"{identity}: abandonment provenance does not match manifest"
                )
        else:
            turns = [turn for segment in row.segments for turn in segment]
            ids = [turn.inference_call_id for turn in turns]
            decisions = [decision for turn in turns for decision in turn.decisions]
            outcomes = row.terminal_outcomes
            commits = {
                context.commit_key(
                    row.org_id,
                    commit.repo,
                    commit.commit_sha,
                    repository_identity=commit.repository_identity,
                )
                for commit in row.commits
            }
            if None in commits:
                raise BundleValidationError(
                    f"{identity}: repository identity is unresolved"
                )
        for call_id in ids:
            call = identities.get(call_id) if call_id in calls else None
            if call is None:
                raise BundleValidationError(
                    f"{identity}: inference call {call_id!r} is absent"
                )
            if (call.org_id, call.session_id) != (row.org_id, row.session_id):
                raise BundleValidationError(
                    f"{identity}: Inference call {call_id!r} org_id or session_id does not match artifact"
                )
            referenced.add(call_id)
        for decision in decisions:
            if (decision.org_id, decision.session_id) != (row.org_id, row.session_id):
                raise BundleValidationError(
                    f"{identity}: Developer decision {decision.decision_id!r} org_id or session_id does not match artifact"
                )
            remember_decision(decision, attributed, identity)
        for outcome in outcomes:
            if (
                outcome.org_id != row.org_id
                or context.resolve_fact(outcome).key is None
                or CommitKey(context.resolve_fact(outcome).key, outcome.commit_sha)
                not in commits
            ):
                raise BundleValidationError(
                    f"{identity}: CI outcome {outcome.outcome_id!r} org_id or repository-qualified commit does not match artifact"
                )
            remember(outcome, "outcome_id", identity)
            carried_outcomes[outcome.outcome_id] = CIOutcomeProjection(
                **{
                    item.name: getattr(outcome, item.name)
                    for item in fields(CIOutcomeProjection)
                }
            )
        for observation in row.session_commit_observations:
            if bundle.as_of is None:
                raise BundleValidationError(
                    f"{identity}: Session observation requires non-null as_of"
                )
            if (
                (observation.org_id, observation.session_id)
                != (
                    row.org_id,
                    row.session_id,
                )
                or context.resolve_fact(observation).key is None
                or CommitKey(
                    context.resolve_fact(observation).key, observation.commit_sha
                )
                not in commits
            ):
                raise BundleValidationError(
                    f"{identity}: Session observation {observation.observation_id!r} org_id, session_id or repository-qualified commit does not match artifact"
                )
            if observation.captured_at.astimezone(UTC) > bundle.as_of.astimezone(UTC):
                raise BundleValidationError(
                    f"{identity}: Session observation {observation.observation_id!r} captured_at exceeds as_of"
                )
            remember(observation, "observation_id", identity)
    row = turns = decisions = outcomes = None
    # Exact repeated copies were checked before coalescing them. A contradictory
    # run visible across artifacts cannot become clean in a per-commit export.
    ci_population = derive_ci_resolution_result(
        carried_outcomes.values(), repository_context=context
    )
    if ci_population.skipped.get("conflicting_run_identity"):
        raise BundleValidationError("bundle contains a conflicting CI run")
    if calls.keys() != referenced:
        raise BundleValidationError(
            f"bundle contains an unreferenced inference call: {sorted(calls.keys() - referenced)!r}"
        )

    # One organization-wide index for all carried Attributed completion claims.
    # Repeated artifact copies of one checked Decision are not separate Facts.
    attributed_decisions = [
        decision for (attributed, _), decision in decision_views.items() if attributed
    ]
    attached = join_decisions_by_call_id_result(
        identities.values(), attributed_decisions
    ).decisions_by_completion
    attached_ids = {
        call_id: {decision.decision_id for decision in decisions}
        for call_id, decisions in attached.items()
    }
    for row in bundle.attributed_completions:
        for decision in row.decisions:
            if decision.decision_id not in attached_ids.get(row.inference_call_id, ()):
                raise BundleValidationError(
                    f"Attributed completion {row.inference_call_id!r}: Decision {decision.decision_id!r} attachment does not match"
                )
    for rollout in bundle.rollouts:
        name = f"Rollout Session {rollout.session_id!r}"
        turns = [turn for segment in rollout.segments for turn in segment]
        turn_ids = [turn.inference_call_id for turn in turns]
        session_ids = {
            item.inference_call_id
            for item in identities_by_session.get(rollout.session_id, ())
        }
        if (
            not turn_ids
            or len(set(turn_ids)) != len(turn_ids)
            or set(turn_ids) != session_ids
        ):
            raise BundleValidationError(
                f"{name}: incomplete or duplicate Session Turn coverage"
            )
        # Session scope stays deliberate: a different Session's alias cannot
        # invalidate a Rollout Decision under the existing canonical owner.
        decisions = list(
            {
                decision.decision_id: decision
                for turn in turns
                for decision in turn.decisions
            }.values()
        )
        if sum(call_content_bytes[call_id] for call_id in turn_ids) > (
            INFERENCE_SESSION_BYTES_LIMIT
        ):
            raise BundleCapacityError(
                f"{name}: input/output content exceeds Session byte budget"
            )
        session_calls = []
        for call_id in turn_ids:
            source = bundle.inference_calls[calls[call_id]]
            session_calls.append(
                RolloutInferenceCall(
                    **{
                        item.name: getattr(source, item.name)
                        for item in fields(RolloutInferenceCall)
                    }
                )
            )
            del source
        segments, _, _ = project_session_turns(session_calls, decisions)
        if _canonical_digest(segments) != _canonical_digest(rollout.segments):
            raise BundleValidationError(
                f"{name}: Turn projection or Decision attachment does not match captured calls"
            )
        del session_calls, segments, turns, rollout

    return context


def load_derivation_policy(path: Path) -> DerivationPolicy:
    """Load and strictly validate a derivation policy TOML file."""

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot load derivation policy {path}: {exc}") from exc
    if not isinstance(data, dict):  # pragma: no cover - tomllib guarantees a table
        raise ValueError("derivation policy must be a TOML table")
    _reject_unknown(data, _ROOT_FIELDS, "")

    if "schema_version" not in data:
        raise ValueError("derivation policy requires schema_version = 1")
    schema_version = data["schema_version"]
    if type(schema_version) is not int:
        raise ValueError("schema_version must be an integer")

    attribution_data = _table(data, "attribution")
    git_notes_data = _table(attribution_data, "git_notes")
    jaccard_data = _table(attribution_data, "jaccard")
    split_data = _table(data, "split")
    _reject_unknown(attribution_data, _ATTRIBUTION_FIELDS, "attribution.")
    _reject_unknown(git_notes_data, _SIMILARITY_FIELDS, "attribution.git_notes.")
    _reject_unknown(jaccard_data, _SIMILARITY_FIELDS, "attribution.jaccard.")
    _reject_unknown(split_data, _SPLIT_FIELDS, "split.")

    defaults = AttributionPolicy()
    attribution = AttributionPolicy(
        post_push_grace_period_minutes=attribution_data.get(
            "post_push_grace_period_minutes",
            defaults.post_push_grace_period_minutes,
        ),
        max_commits_per_push=attribution_data.get(
            "max_commits_per_push", defaults.max_commits_per_push
        ),
        git_notes=SimilarityPolicy(
            min_similarity=git_notes_data.get(
                "min_similarity", defaults.git_notes.min_similarity
            ),
            lookback_window_minutes=git_notes_data.get(
                "lookback_window_minutes",
                defaults.git_notes.lookback_window_minutes,
            ),
        ),
        jaccard=SimilarityPolicy(
            min_similarity=jaccard_data.get(
                "min_similarity", defaults.jaccard.min_similarity
            ),
            lookback_window_minutes=jaccard_data.get(
                "lookback_window_minutes",
                defaults.jaccard.lookback_window_minutes,
            ),
        ),
    )
    return DerivationPolicy(
        schema_version=schema_version,
        attribution=attribution,
        eval_fraction=split_data.get("eval_fraction", 0.1),
    )


def _table(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a TOML table")
    return value


def _reject_unknown(data: dict[str, Any], allowed: frozenset[str], prefix: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        names = ", ".join(f"{prefix}{name}" for name in unknown)
        raise ValueError(f"unknown derivation policy field: {names}")
