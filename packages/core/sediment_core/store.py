# SPDX-License-Identifier: AGPL-3.0-or-later
"""Complete private PostgreSQL fact store and bounded derivation reads."""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import Field, TypeAdapter
from sqlalchemy import (
    and_,
    case,
    column as sql_column,
    false,
    func,
    inspect,
    null,
    or_,
    select,
    tuple_,
    true,
    values,
)
from sqlalchemy.dialects.postgresql import ARRAY, array, insert
from sqlalchemy.engine import Connection, Engine

from . import evidence
from .evidence import (
    EvidenceCallMetadata,
    EvidenceInventory,
    EvidenceManifest,
    EvidenceMessageSource,
    EvidenceRead,
    EvidenceReadError,
    EvidenceReference,
    project_evidence_inventory,
    project_evidence_manifest,
    project_evidence_read,
    validate_evidence_references,
)
from .models import (
    AgentHarness,
    AwareDatetime,
    CIOutcome,
    CIProvider,
    CIResult,
    CommitSha,
    DeveloperDecision,
    EditObservation,
    FactTable,
    ForgeProvider,
    ForgeHost,
    ProviderRepositoryId,
    GatewayProvider,
    InferenceCall,
    InferenceMessage,
    InteractionMode,
    NonEmptyId,
    OrgId,
    PullRequestMerge,
    PullRequestRevision,
    Push,
    RepoSlug,
    RequiredRepoSlug,
    RepositoryRename,
    QuarantineAction,
    QuarantineRecord,
    RejectedEdit,
    RetryLinkage,
    SessionCommitObservation,
    ToolCallPart,
)
from .postgres_engine import DatabaseOperationError
from .postgres_migrations import RevisionState, inspect_engine_revision
from .postgres_schema import (
    ci_outcomes,
    developer_decisions,
    edit_observations,
    fact_quarantine,
    inference_call_aliases,
    inference_calls,
    pull_request_merges,
    pull_request_revisions,
    pushes,
    rejected_edits,
    retry_linkages,
    repository_renames,
    session_commit_observations,
    sessions,
)
from .redaction import RedactionReason, redact_fact

_FACT_TABLES = {
    FactTable.INFERENCE_CALLS: inference_calls,
    FactTable.DEVELOPER_DECISIONS: developer_decisions,
    FactTable.EDIT_OBSERVATIONS: edit_observations,
    FactTable.REJECTED_EDITS: rejected_edits,
    FactTable.RETRY_LINKAGES: retry_linkages,
    FactTable.SESSION_COMMIT_OBSERVATIONS: session_commit_observations,
    FactTable.CI_OUTCOMES: ci_outcomes,
    FactTable.PUSHES: pushes,
    FactTable.PULL_REQUEST_MERGES: pull_request_merges,
    FactTable.PULL_REQUEST_REVISIONS: pull_request_revisions,
    FactTable.REPOSITORY_RENAMES: repository_renames,
}

REPOSITORY_IDENTITY_LIMIT = 50_000

# Encoded TEXT budgets precede transfer and canonical Python decoding. These
# are execution capacity limits, independent of Derivation policy versions.
INFERENCE_REPORT_ROW_LIMIT = 50_000
INFERENCE_CALL_ROW_BYTES_LIMIT = 64 * 1024 * 1024
INFERENCE_SESSION_BYTES_LIMIT = 256 * 1024 * 1024
INFERENCE_PROJECTION_BYTES_LIMIT = 256 * 1024 * 1024


class RepositoryIdentityConflict(ValueError):
    """A retained repository Fact contradicts the submitted identity."""


@dataclass(frozen=True)
class RepositoryFactReceipt:
    fact_id: NonEmptyId
    stored: bool
    fact: (
        Push
        | CIOutcome
        | SessionCommitObservation
        | PullRequestMerge
        | PullRequestRevision
        | RepositoryRename
    )


@dataclass(frozen=True)
class RepositoryIdentityEvidence:
    """A captured repository role, including explicit identity absence."""

    source_table: Literal[
        FactTable.PUSHES,
        FactTable.CI_OUTCOMES,
        FactTable.SESSION_COMMIT_OBSERVATIONS,
        FactTable.PULL_REQUEST_MERGES,
        FactTable.PULL_REQUEST_REVISIONS,
    ]
    source_fact_id: NonEmptyId
    role: Literal["repo", "head_repo"]
    org_id: OrgId
    repo: RepoSlug
    repository_provider: ForgeProvider | None
    repository_host: ForgeHost | None
    repository_id: ProviderRepositoryId | None
    captured_at: AwareDatetime
    source_push_id: NonEmptyId | None = None


_REPOSITORY_COMPONENTS = ("repository_provider", "repository_host", "repository_id")


def _repository_natural_conditions(table, fact):
    conditions = [table.c.org_id == fact.org_id]
    if isinstance(fact, RepositoryRename):
        if fact.source_event_id is None:
            return [table.c.rename_id == fact.rename_id, *conditions]
        return [
            *conditions,
            table.c.repository_provider == fact.repository_provider,
            table.c.repository_host == fact.repository_host,
            table.c.source_event_id == fact.source_event_id,
        ]
    if fact.repository_id is None:
        conditions.extend((table.c.repository_id.is_(None),))
        if not isinstance(fact, CIOutcome):
            conditions.append(table.c.repo == fact.repo)
    else:
        components = (
            _REPOSITORY_COMPONENTS[:2]
            if isinstance(fact, CIOutcome)
            else _REPOSITORY_COMPONENTS
        )
        conditions.extend(table.c[name] == getattr(fact, name) for name in components)
        conditions.append(table.c.repository_id.is_not(None))
    if isinstance(fact, CIOutcome):
        conditions.extend(
            (
                table.c.provider == fact.provider,
                table.c.run_id == fact.run_id,
                func.coalesce(table.c.run_attempt, 0) == (fact.run_attempt or 0),
            )
        )
    elif isinstance(fact, Push):
        conditions.extend(
            table.c[name] == getattr(fact, name)
            for name in ("ref", "before_sha", "after_sha")
        )
    elif isinstance(fact, SessionCommitObservation):
        conditions.extend(
            (
                table.c.commit_sha == fact.commit_sha,
                table.c.session_id == fact.session_id,
            )
        )
    elif isinstance(fact, (PullRequestMerge, PullRequestRevision)):
        conditions.extend(
            (table.c.provider == fact.provider, table.c.pr_number == fact.pr_number)
        )
        if isinstance(fact, PullRequestRevision):
            conditions.extend(
                (table.c.head_sha == fact.head_sha, table.c.base_sha == fact.base_sha)
            )
    return conditions


def _repository_receipt(connection, fact, table, primary_key: str, values):
    stored = (
        connection.execute(
            insert(table)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(table.c[primary_key])
        ).scalar_one_or_none()
        is not None
    )
    if stored:
        return RepositoryFactReceipt(getattr(fact, primary_key), True, fact)
    rows = (
        connection.execute(
            select(table).where(
                or_(
                    table.c[primary_key] == getattr(fact, primary_key),
                    and_(*_repository_natural_conditions(table, fact)),
                )
            )
        )
        .mappings()
        .all()
    )
    if len(rows) != 1:
        raise RepositoryIdentityConflict(
            "repository Fact identity conflicts with retained evidence"
        )
    values = dict(rows[0])
    if "raw" in values:
        values["raw"] = json.loads(values["raw"])
    retained = type(fact).model_validate(values)
    fields = ["org_id", *_REPOSITORY_COMPONENTS]
    if not isinstance(fact, RepositoryRename):
        if fact.repository_id is None:
            fields.append("repo")
    if isinstance(fact, Push):
        fields.extend(("provider", "ref", "before_sha", "after_sha"))
    elif isinstance(fact, SessionCommitObservation):
        fields.extend(("commit_sha", "session_id"))
    elif isinstance(fact, CIOutcome):
        fields.extend(
            (
                "provider",
                "run_id",
                "run_attempt",
                "commit_sha",
                "workflow_id",
                "workflow_path",
            )
        )
    elif isinstance(fact, (PullRequestMerge, PullRequestRevision)):
        fields.extend(
            (
                "provider",
                "pr_number",
                "head_sha",
                "base_sha",
                "head_repository_provider",
                "head_repository_host",
                "head_repository_id",
            )
        )
        if fact.head_repository_id is None:
            fields.append("head_repo")
        if isinstance(fact, PullRequestMerge):
            fields.append("merge_commit_sha")
    elif isinstance(fact, RepositoryRename):
        fields.extend(("old_repo", "new_repo", "source_event_id", "occurred_at"))
    if any(getattr(fact, name) != getattr(retained, name) for name in fields):
        raise RepositoryIdentityConflict(
            "repository Fact identity conflicts with retained evidence"
        )
    return RepositoryFactReceipt(getattr(retained, primary_key), False, retained)


# Two bound values per composite key leave headroom below PostgreSQL's 65,535
# bind-parameter ceiling for tenancy and time-bound predicates.
COMPOSITE_FILTER_KEY_LIMIT = 30_000
# Identified composite filters bind four values per key, with headroom for
# tenant, capture bounds, and optional filters under PostgreSQL's 65,535 cap.
REPOSITORY_FILTER_KEY_LIMIT = 15_000
RepositoryReadKey = (
    RequiredRepoSlug | tuple[ForgeProvider, ForgeHost, ProviderRepositoryId]
)
RepositoryCommitReadKey = tuple[RepositoryReadKey, CommitSha]
RepositoryPRReadKey = tuple[
    RepositoryReadKey, Annotated[int, Field(ge=1, le=2**63 - 1, strict=True)]
]
_COMMIT_SHA = TypeAdapter(CommitSha)
_REPOSITORY_READ_KEY = TypeAdapter(RepositoryReadKey)
_REPOSITORY_COMMIT_READ_KEY = TypeAdapter(RepositoryCommitReadKey)
_REPOSITORY_PR_READ_KEY = TypeAdapter(RepositoryPRReadKey)


class RepositoryReadAmbiguous(ValueError):
    """A read selector identifies more than one retained repository namespace."""


def _repository_selector_condition(table, key):
    try:
        key = _REPOSITORY_READ_KEY.validate_python(key)
    except (ValueError, TypeError):
        raise ValueError("invalid repository selector") from None
    if isinstance(key, str):
        return and_(table.c.repository_id.is_(None), table.c.repo == key)
    provider, host, repository_id = key
    return and_(
        table.c.repository_provider == provider,
        table.c.repository_host == host,
        table.c.repository_id == repository_id,
    )


def _repository_composite_condition(
    table, value_column, *, literal_keys, qualified_keys, pr=False, extra_bindings=0
):
    """One SQL owner for literal compatibility and explicit qualified branches."""
    if literal_keys is not None and qualified_keys is not None:
        raise ValueError("literal and qualified repository filters cannot be combined")
    if qualified_keys is None:
        if literal_keys is None:
            return None
        validate_composite_filter_keys(literal_keys)
        if 2 * len(literal_keys) + extra_bindings > 60_000:
            raise OperationalReportLimitExceeded(
                "Repository filter exceeds parameter bound"
            )
        return _repository_membership(
            (table.c.repo, value_column), sorted(literal_keys)
        )
    if len(qualified_keys) > REPOSITORY_FILTER_KEY_LIMIT:
        raise OperationalReportLimitExceeded("Repository filter exceeds 15000 keys")
    legacy, identified = set(), set()
    adapter = _REPOSITORY_PR_READ_KEY if pr else _REPOSITORY_COMMIT_READ_KEY
    for raw in qualified_keys:
        try:
            key, value = adapter.validate_python(raw)
        except (ValueError, TypeError):
            raise ValueError("invalid qualified repository filter") from None
        if isinstance(key, str):
            legacy.add((key, value))
        else:
            identified.add((*key, value))
    if 2 * len(legacy) + 4 * len(identified) + extra_bindings > 60_000:
        raise OperationalReportLimitExceeded(
            "Repository filter exceeds parameter bound"
        )
    clauses = []
    if legacy:
        clauses.append(
            and_(
                table.c.repository_id.is_(None),
                _repository_membership((table.c.repo, value_column), sorted(legacy)),
            )
        )
    if identified:
        clauses.append(
            _repository_membership(
                (
                    table.c.repository_provider,
                    table.c.repository_host,
                    table.c.repository_id,
                    value_column,
                ),
                sorted(identified),
            )
        )
    return or_(*clauses) if clauses else false()


def _repository_membership(columns, rows):
    if not rows:
        return false()
    # A tuple-IN literal list expands into an OR expression tree in PostgreSQL.
    # VALUES stays a relation, so accepted populations don't exhaust stack depth.
    population = values(
        *(sql_column(f"key_{index}", item.type) for index, item in enumerate(columns)),
        name="repository_filter",
    ).data(rows)
    return tuple_(*columns).in_(population.select())


class OperationalReportLimitExceeded(ValueError):
    """A bounded operational read exceeded its fixed evidence limit."""


def _require_head_revision(engine: Engine, operation: str) -> None:
    """Refuse a descriptive-text write into a database below the schema head.

    ``_SerializedText.process_bind_param`` already ``json.dumps(...,
    ensure_ascii=True)``-encodes descriptive strings on the write path, and
    the ``0009_descriptive_text_encoding`` migration applies the same
    encoding to every existing row. A write that lands in a pre-0009
    database is encoded once at insert and again at migration, corrupting
    the logical value when it is read back. The API lifespan gate
    (``apps/api/sediment_api/main.py``) prevents this on the ingest path;
    this gate closes the same window for the CLI one-shot write verbs that
    share no such check. Read-only verbs keep working against a behind
    database so a staged rollout can still compare counts (ADR 0015).
    """
    if inspect_engine_revision(engine).state is not RevisionState.AT_HEAD:
        raise DatabaseOperationError(
            f"{operation} requires the database schema at head; "
            "run `sediment db upgrade`"
        )


_SESSION_FACT_TABLES = frozenset(
    {
        FactTable.INFERENCE_CALLS,
        FactTable.DEVELOPER_DECISIONS,
        FactTable.EDIT_OBSERVATIONS,
        FactTable.REJECTED_EDITS,
        FactTable.RETRY_LINKAGES,
    }
)
_FACT_PRIMARY_KEYS = {
    FactTable.REPOSITORY_RENAMES: repository_renames.c.rename_id,
    FactTable.INFERENCE_CALLS: inference_calls.c.inference_call_id,
    FactTable.DEVELOPER_DECISIONS: developer_decisions.c.decision_id,
    FactTable.EDIT_OBSERVATIONS: edit_observations.c.observation_id,
    FactTable.REJECTED_EDITS: rejected_edits.c.rejection_id,
    FactTable.RETRY_LINKAGES: retry_linkages.c.retry_linkage_id,
    FactTable.SESSION_COMMIT_OBSERVATIONS: session_commit_observations.c.observation_id,
    FactTable.CI_OUTCOMES: ci_outcomes.c.outcome_id,
    FactTable.PUSHES: pushes.c.push_id,
    FactTable.PULL_REQUEST_MERGES: pull_request_merges.c.merge_id,
    FactTable.PULL_REQUEST_REVISIONS: pull_request_revisions.c.revision_id,
}
_DECISION_RETURN_COLUMNS = tuple(
    column for column in developer_decisions.c if column.name != "raw"
)

logger = logging.getLogger(__name__)


def _log_redaction(
    table: FactTable,
    fact_id: str,
    counts: Mapping[RedactionReason, int],
    stored: bool,
) -> None:
    if not counts:
        return
    summary = ",".join(
        f"{reason.value}:{counts[reason]}"
        for reason in RedactionReason
        if counts.get(reason)
    )
    logger.info(
        "fact_redacted fact_table=%s fact_id=%s stored=%s counts=%s",
        table.value,
        fact_id,
        stored,
        summary,
    )


@dataclass(frozen=True)
class SessionRecord:
    """One immutable Session row read from PostgreSQL."""

    org_id: str
    session_id: str
    user_id: str | None
    user_id_conflict: bool
    first_observed_at: datetime
    last_observed_at: datetime


@dataclass(frozen=True)
class InferenceCallSummary:
    """Typed inference metadata without message or raw TEXT columns."""

    inference_call_id: str
    org_id: str
    session_id: str
    user_id: str | None
    gateway_provider: GatewayProvider
    model_provider: str | None
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    duration_ms: int | None
    model_call_id: str | None
    observed_at: datetime


@dataclass(frozen=True)
class ReportInferenceCall(InferenceCallSummary):
    """Report evidence without request histories or raw capture payloads."""

    output_messages: list[InferenceMessage]


@dataclass(frozen=True)
class InferenceCallIdentity:
    """Call identity and distinct provider/tool aliases, without message bodies."""

    inference_call_id: NonEmptyId
    org_id: OrgId
    session_id: NonEmptyId
    observed_at: AwareDatetime
    call_ids: tuple[NonEmptyId, ...]


@dataclass(frozen=True)
class AttributionInferenceCall:
    """Inference output needed for attribution scoring and decision joins."""

    inference_call_id: str
    session_id: str
    model_call_id: str | None
    output_messages: list[InferenceMessage]
    observed_at: datetime


@dataclass(frozen=True)
class RolloutInferenceCall:
    """Canonical inference content without the unused raw provider payload."""

    schema_version: int
    inference_call_id: str
    org_id: str
    session_id: str
    user_id: str | None
    gateway_provider: GatewayProvider
    model_provider: str | None
    model: str | None
    input_messages: list[InferenceMessage]
    output_messages: list[InferenceMessage]
    input_tokens: int | None
    output_tokens: int | None
    duration_ms: int | None
    model_call_id: str | None
    observed_at: datetime


@dataclass(frozen=True)
class DeveloperDecisionProjection:
    """Decision evidence without its unused raw capture payload."""

    decision_id: str
    org_id: str
    session_id: str
    user_id: str | None
    agent_harness: AgentHarness
    file_path: str
    accepted: bool
    explicit: bool
    interaction_mode: InteractionMode
    commit_sha: str | None
    call_id: str | None
    edit_retention_score: float | None
    observation_delay_ms: int | None
    occurred_at: datetime
    captured_at: datetime

    def model_copy(self, *, update: dict[str, object]) -> DeveloperDecisionProjection:
        """Return a derived copy for edit-retention attachment."""
        return replace(self, **update)


@dataclass(frozen=True)
class EditObservationProjection:
    """Edit-survival evidence without its unused raw capture payload."""

    observation_id: str
    org_id: str
    session_id: str
    user_id: str | None
    agent_harness: AgentHarness
    file_path: str
    call_id: str
    applied_text: str
    observed_file_text: str
    external_lines_added: int | None
    external_lines_removed: int | None
    occurred_at: datetime
    captured_at: datetime


@dataclass(frozen=True)
class CIOutcomeProjection:
    """Queryable CI evidence without its unused raw provider payload."""

    schema_version: int
    outcome_id: str
    org_id: str
    provider: CIProvider
    run_id: str
    run_attempt: int | None
    repo: str
    commit_sha: str
    branch: str
    result: CIResult
    workflow_name: str
    workflow_id: str | None
    workflow_path: str | None
    run_url: str | None
    provider_result: str | None
    error_type: str | None
    reason: str | None
    source_event_type: str | None
    source_spec_version: str | None
    source_event_id: str | None
    pr_number: int | None
    captured_at: datetime
    repository_provider: ForgeProvider | None = None
    repository_host: ForgeHost | None = None
    repository_id: ProviderRepositoryId | None = None


@dataclass(frozen=True)
class SessionFactCoverage:
    """Visible and quarantined counts for one Session Fact type."""

    total: int
    visible: int
    quarantined: int


@dataclass(frozen=True)
class SessionTimelineProjection:
    """One content-free Session event for an investigation timeline."""

    event_type: str
    fact_id: str
    occurred_at: datetime
    gateway_provider: GatewayProvider | None = None
    model_provider: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    duration_ms: int | None = None
    model_call_id: str | None = None
    agent_harness: AgentHarness | None = None
    accepted: bool | None = None
    explicit: bool | None = None
    interaction_mode: InteractionMode | None = None
    has_file_path: bool | None = None
    call_id: str | None = None
    commit_sha: str | None = None
    external_lines_added_present: bool | None = None
    external_lines_removed_present: bool | None = None
    external_lines_added: int | None = None
    external_lines_removed: int | None = None
    tool_name: str | None = None
    rejected_call_id: str | None = None
    accepted_call_id: str | None = None


@dataclass(frozen=True)
class SessionDossierProjection:
    """Bounded Session metadata read from one database transaction."""

    session_id: str
    first_observed_at: datetime
    last_observed_at: datetime
    coverage: Mapping[str, SessionFactCoverage]
    timeline: tuple[SessionTimelineProjection, ...]


@dataclass(frozen=True)
class PushProjection:
    """Push navigation metadata without clone credentials."""

    push_id: str
    provider: str
    repo: str
    ref: str
    before_sha: str
    after_sha: str
    forced: bool
    captured_at: datetime
    repository_provider: ForgeProvider | None = None
    repository_host: ForgeHost | None = None
    repository_id: ProviderRepositoryId | None = None


@dataclass(frozen=True)
class PushGCRow:
    """The Push fields used to decide mirror retention."""

    repo: str
    captured_at: datetime
    repository_provider: ForgeProvider | None = None
    repository_host: ForgeHost | None = None
    repository_id: ProviderRepositoryId | None = None


@dataclass(frozen=True)
class InferenceCallReconciliation:
    """Narrow fields for reconciling one Session without captured content."""

    inference_call_id: NonEmptyId
    model_call_id: NonEmptyId | None
    gateway_provider: str
    model_provider: str | None
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    duration_ms: int | None


@dataclass(frozen=True)
class CompatibilityInferenceEvidence:
    """Response tool-call join ids without captured Inference call content."""

    tool_call_ids: tuple[NonEmptyId, ...]


@dataclass(frozen=True)
class CompatibilityDecisionEvidence:
    """Decision join fields for one disposable compatibility Session."""

    agent_harness: AgentHarness
    accepted: bool
    explicit: bool
    interaction_mode: InteractionMode
    call_id: NonEmptyId | None


@dataclass(frozen=True)
class CompatibilityEditEvidence:
    """Edit-observation join fields for one disposable compatibility Session."""

    agent_harness: AgentHarness
    call_id: NonEmptyId


@dataclass(frozen=True)
class InferenceCallReceipt:
    """Database insertion disposition and the retained Inference call identity."""

    fact_id: NonEmptyId
    stored: bool


class InferenceCallIdentityConflict(ValueError):
    """An attempted Inference call conflicts with incompatible stored identity."""


class FactStore:
    """The PostgreSQL fact store over a process-owned SQLAlchemy engine."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def store_inference_call(self, call: InferenceCall) -> bool:
        return self.store_inference_call_receipt(call).stored

    def store_inference_call_receipt(self, call: InferenceCall) -> InferenceCallReceipt:
        """Insert a call or return the row retained by database uniqueness.

        The conflict lookup runs after INSERT, so PostgreSQL has waited for
        concurrent insertions and a READ COMMITTED statement sees their rows.
        Exact primary/natural-key matches include quarantine; replay cannot
        release an existing Fact. Conflicting identities expose no retained ID.
        """
        call, redaction_counts = redact_fact(call)
        aliases = {
            part.id
            for message in call.output_messages
            for part in message.parts
            if isinstance(part, ToolCallPart)
        }
        if call.model_call_id is not None:
            aliases.add(call.model_call_id)
        aliases = sorted(aliases)
        values = call.model_dump(
            exclude={"input_messages", "output_messages", "raw"},
            mode="python",
        )
        values.update(
            call_alias_count=len(aliases),
            input_messages=_serialize(
                [message.model_dump(mode="python") for message in call.input_messages]
            ),
            output_messages=_serialize(
                [message.model_dump(mode="python") for message in call.output_messages]
            ),
            raw=_serialize(call.raw),
        )
        statement = (
            insert(inference_calls)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(inference_calls.c.inference_call_id)
        )
        with self._engine.begin() as connection:
            stored = connection.execute(statement).scalar_one_or_none() is not None
            if stored:
                connection.execute(_session_upsert(call, call.observed_at))
                for start in range(0, len(aliases), 1000):
                    connection.execute(
                        insert(inference_call_aliases),
                        [
                            {
                                "inference_call_id": call.inference_call_id,
                                "org_id": call.org_id,
                                "ordinal": ordinal,
                                "call_id": alias,
                            }
                            for ordinal, alias in enumerate(
                                aliases[start : start + 1000], start
                            )
                        ],
                    )
                fact_id = call.inference_call_id
            else:
                identity = inference_calls.c.inference_call_id == call.inference_call_id
                if call.model_call_id is not None:
                    identity = or_(
                        identity,
                        and_(
                            inference_calls.c.org_id == call.org_id,
                            inference_calls.c.gateway_provider == call.gateway_provider,
                            inference_calls.c.model_call_id == call.model_call_id,
                        ),
                    )
                matches = (
                    connection.execute(
                        select(
                            inference_calls.c.inference_call_id,
                            inference_calls.c.org_id,
                            inference_calls.c.gateway_provider,
                            inference_calls.c.model_call_id,
                        ).where(identity)
                    )
                    .mappings()
                    .all()
                )
                if len(matches) != 1 or (
                    matches[0]["org_id"],
                    matches[0]["gateway_provider"],
                    matches[0]["model_call_id"],
                ) != (call.org_id, call.gateway_provider, call.model_call_id):
                    raise InferenceCallIdentityConflict(
                        "Inference call identity conflicts with stored evidence"
                    )
                fact_id = matches[0]["inference_call_id"]
        _log_redaction(
            table=FactTable.INFERENCE_CALLS,
            fact_id=call.inference_call_id,
            counts=redaction_counts,
            stored=stored,
        )
        return InferenceCallReceipt(fact_id=fact_id, stored=stored)

    def read_inference_calls(
        self,
        org_id: str,
        since: timedelta | None = None,
        include_quarantined: bool = False,
    ) -> list[InferenceCall]:
        with self._engine.connect() as connection:
            return _read_inference_calls(
                connection,
                org_id,
                include_quarantined,
                observed_after=(
                    datetime.now(UTC) - since if since is not None else None
                ),
            )

    def read_inference_call_reconciliation(
        self, org_id: str, session_id: str, *, limit: int = 1000
    ) -> list[InferenceCallReconciliation]:
        columns = (
            inference_calls.c.inference_call_id,
            inference_calls.c.model_call_id,
            inference_calls.c.gateway_provider,
            inference_calls.c.model_provider,
            inference_calls.c.model,
            inference_calls.c.input_tokens,
            inference_calls.c.output_tokens,
            inference_calls.c.duration_ms,
        )
        statement = (
            select(*columns)
            .where(
                *_fact_conditions(
                    org_id, FactTable.INFERENCE_CALLS, include_quarantined=False
                ),
                inference_calls.c.session_id == session_id,
            )
            .order_by(
                inference_calls.c.observed_at,
                inference_calls.c.inference_call_id,
            )
            .limit(limit + 1)
        )
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        if len(rows) > limit:
            raise ValueError("Session exceeds the reconciliation limit")
        return [InferenceCallReconciliation(**row) for row in rows]

    def read_compatibility_inference_evidence(
        self, org_id: str, session_id: str, *, limit: int = 50
    ) -> list[CompatibilityInferenceEvidence]:
        statement = (
            select(inference_calls.c.output_messages)
            .where(
                *_fact_conditions(
                    org_id, FactTable.INFERENCE_CALLS, include_quarantined=False
                ),
                inference_calls.c.session_id == session_id,
            )
            .order_by(
                inference_calls.c.observed_at,
                inference_calls.c.inference_call_id,
            )
            .limit(limit + 1)
        )
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        if len(rows) > limit:
            raise ValueError("Session exceeds the compatibility limit")
        return [
            CompatibilityInferenceEvidence(
                tool_call_ids=tuple(
                    sorted(
                        {
                            part.id
                            for message in _messages(row["output_messages"])
                            for part in message.parts
                            if isinstance(part, ToolCallPart)
                        }
                    )
                )
            )
            for row in rows
        ]

    def read_compatibility_evidence(
        self, org_id: str, session_id: str, *, limit: int = 1000
    ) -> tuple[list[CompatibilityDecisionEvidence], list[CompatibilityEditEvidence]]:
        decision_statement = (
            select(
                developer_decisions.c.agent_harness,
                developer_decisions.c.accepted,
                developer_decisions.c.explicit,
                developer_decisions.c.interaction_mode,
                developer_decisions.c.call_id,
            )
            .where(
                *_fact_conditions(
                    org_id, FactTable.DEVELOPER_DECISIONS, include_quarantined=False
                ),
                developer_decisions.c.session_id == session_id,
            )
            .order_by(
                developer_decisions.c.occurred_at,
                developer_decisions.c.decision_id,
            )
            .limit(limit + 1)
        )
        observation_statement = (
            select(
                edit_observations.c.agent_harness,
                edit_observations.c.call_id,
            )
            .where(
                *_fact_conditions(
                    org_id, FactTable.EDIT_OBSERVATIONS, include_quarantined=False
                ),
                edit_observations.c.session_id == session_id,
            )
            .order_by(
                edit_observations.c.occurred_at,
                edit_observations.c.observation_id,
            )
            .limit(limit + 1)
        )
        with self._engine.connect() as connection:
            decisions = connection.execute(decision_statement).mappings().all()
            observations = connection.execute(observation_statement).mappings().all()
        if len(decisions) > limit or len(observations) > limit:
            raise ValueError("Session exceeds the compatibility limit")
        return (
            [CompatibilityDecisionEvidence(**row) for row in decisions],
            [CompatibilityEditEvidence(**row) for row in observations],
        )

    def store_decision(self, decision: DeveloperDecision) -> bool:
        return self.store_decisions([decision])[0]

    def store_decisions(self, decisions: list[DeveloperDecision]) -> list[bool]:
        if not decisions:
            return []
        redacted = [redact_fact(decision) for decision in decisions]
        input_values = [_decision_values(decision) for decision, _counts in redacted]
        # Every prepared field emits one bind; DO NOTHING and RETURNING add none.
        # Leave headroom below PostgreSQL's 65,535-parameter protocol limit.
        rows_per_statement = 60_000 // len(input_values[0])
        stored = []
        with self._engine.begin() as connection:
            for offset in range(0, len(input_values), rows_per_statement):
                chunk = input_values[offset : offset + rows_per_statement]
                statement = (
                    insert(developer_decisions)
                    .values(chunk)
                    .on_conflict_do_nothing()
                    .returning(*_DECISION_RETURN_COLUMNS)
                )
                returned_rows = connection.execute(statement).mappings()
                returned = Counter(_decision_row_key(row) for row in returned_rows)
                for values in chunk:
                    key = _decision_row_key(values)
                    did_store = returned[key] > 0
                    stored.append(did_store)
                    if did_store:
                        returned[key] -= 1
            for metadata in _session_metadata(
                decision
                for (decision, _), did_store in zip(redacted, stored)
                if did_store
            ):
                connection.execute(_session_upsert_values(metadata))
        for (decision, counts), did_store in zip(redacted, stored, strict=True):
            _log_redaction(
                table=FactTable.DEVELOPER_DECISIONS,
                fact_id=decision.decision_id,
                counts=counts,
                stored=did_store,
            )
        return stored

    def read_decisions(
        self,
        org_id: str,
        include_quarantined: bool = False,
        *,
        captured_through: datetime | None = None,
        session_ids: set[str] | None = None,
        call_ids: set[str] | None = None,
        limit: int | None = None,
    ) -> list[DeveloperDecision]:
        with self._engine.connect() as connection:
            return _read_decisions(
                connection,
                org_id,
                include_quarantined,
                captured_through=captured_through,
                session_ids=session_ids,
                call_ids=call_ids,
                limit=limit,
            )

    def store_edit_observation(self, observation: EditObservation) -> bool:
        observation, redaction_counts = redact_fact(observation)
        values = observation.model_dump(
            exclude={"applied_text", "observed_file_text", "raw"}, mode="python"
        )
        values.update(
            applied_text=_serialize(observation.applied_text),
            observed_file_text=_serialize(observation.observed_file_text),
            raw=_serialize(observation.raw),
        )
        statement = (
            insert(edit_observations)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(edit_observations.c.observation_id)
        )
        with self._engine.begin() as connection:
            stored = connection.execute(statement).scalar_one_or_none() is not None
            if stored:
                connection.execute(
                    _session_upsert(observation, observation.captured_at)
                )
        _log_redaction(
            table=FactTable.EDIT_OBSERVATIONS,
            fact_id=observation.observation_id,
            counts=redaction_counts,
            stored=stored,
        )
        return stored

    def read_edit_observations(
        self,
        org_id: str,
        include_quarantined: bool = False,
        *,
        captured_through: datetime | None = None,
        session_ids: set[str] | None = None,
        call_ids: set[str] | None = None,
        limit: int | None = None,
    ) -> list[EditObservation]:
        with self._engine.connect() as connection:
            return _read_edit_observations(
                connection,
                org_id,
                include_quarantined,
                captured_through=captured_through,
                session_ids=session_ids,
                call_ids=call_ids,
                limit=limit,
            )

    def store_rejected_edit(self, rejected: RejectedEdit) -> bool:
        rejected, redaction_counts = redact_fact(rejected)
        values = rejected.model_dump(exclude={"proposed", "raw"}, mode="python")
        values.update(
            proposed=_serialize(rejected.proposed),
            raw=_serialize(rejected.raw),
        )
        statement = (
            insert(rejected_edits)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(rejected_edits.c.rejection_id)
        )
        with self._engine.begin() as connection:
            stored = connection.execute(statement).scalar_one_or_none() is not None
            if stored:
                connection.execute(_session_upsert(rejected, rejected.captured_at))
        _log_redaction(
            table=FactTable.REJECTED_EDITS,
            fact_id=rejected.rejection_id,
            counts=redaction_counts,
            stored=stored,
        )
        return stored

    def read_rejected_edits(
        self, org_id: str, include_quarantined: bool = False
    ) -> list[RejectedEdit]:
        with self._engine.connect() as connection:
            return _read_rejected_edits(connection, org_id, include_quarantined)

    def store_retry_linkage(self, linkage: RetryLinkage) -> bool:
        linkage, redaction_counts = redact_fact(linkage)
        values = linkage.model_dump(exclude={"raw"}, mode="python")
        values["raw"] = _serialize(linkage.raw)
        statement = (
            insert(retry_linkages)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(retry_linkages.c.retry_linkage_id)
        )
        with self._engine.begin() as connection:
            stored = connection.execute(statement).scalar_one_or_none() is not None
            if stored:
                connection.execute(_session_upsert(linkage, linkage.captured_at))
        _log_redaction(
            table=FactTable.RETRY_LINKAGES,
            fact_id=linkage.retry_linkage_id,
            counts=redaction_counts,
            stored=stored,
        )
        return stored

    def read_retry_linkages(
        self,
        org_id: str,
        include_quarantined: bool = False,
        *,
        captured_through: datetime | None = None,
        session_ids: set[str] | None = None,
        limit: int | None = None,
    ) -> list[RetryLinkage]:
        with self._engine.connect() as connection:
            return _read_retry_linkages(
                connection,
                org_id,
                include_quarantined,
                captured_through=captured_through,
                session_ids=session_ids,
                limit=limit,
            )

    def store_ci_outcome(self, outcome: CIOutcome) -> bool:
        return self.store_ci_outcome_receipt(outcome).stored

    def store_ci_outcome_receipt(self, outcome: CIOutcome) -> RepositoryFactReceipt:
        outcome, redaction_counts = redact_fact(outcome)
        values = outcome.model_dump(exclude={"raw"}, mode="python")
        values["raw"] = _serialize(outcome.raw)
        with self._engine.begin() as connection:
            receipt = _repository_receipt(
                connection, outcome, ci_outcomes, "outcome_id", values
            )
        _log_redaction(
            table=FactTable.CI_OUTCOMES,
            fact_id=outcome.outcome_id,
            counts=redaction_counts,
            stored=receipt.stored,
        )
        return receipt

    def read_ci_outcomes(
        self,
        org_id: str,
        include_quarantined: bool = False,
        *,
        captured_through: datetime | None = None,
        commit_sha: CommitSha | None = None,
        repo_commits: set[tuple[str, str]] | None = None,
        repository_commits: set[RepositoryCommitReadKey] | None = None,
        limit: int | None = None,
    ) -> list[CIOutcome]:
        with self._engine.connect() as connection:
            return _read_ci_outcomes(
                connection,
                org_id,
                include_quarantined,
                captured_through=captured_through,
                commit_sha=commit_sha,
                repo_commits=repo_commits,
                repository_commits=repository_commits,
                limit=limit,
            )

    def read_ci_outcome_by_run(
        self,
        org_id: str,
        provider: CIProvider,
        run_id: str,
        *,
        run_attempt: int | None,
        repository_key: RepositoryReadKey | None = None,
        captured_through: datetime | None = None,
    ) -> CIOutcomeProjection | None:
        with self._engine.connect() as connection:
            return _read_ci_outcome_by_run(
                connection,
                org_id,
                provider,
                run_id,
                run_attempt=run_attempt,
                repository_key=repository_key,
                captured_through=captured_through,
            )

    def read_ci_outcome_summaries(
        self,
        org_id: str,
        *,
        repo: str | None = None,
        repository_key: RepositoryReadKey | None = None,
        result: CIResult,
        captured_between: tuple[datetime, datetime],
        limit: int,
        captured_through: datetime | None = None,
        before: tuple[datetime, str] | None = None,
        workflow_name: str | None = None,
        pr_number: int | None = None,
    ) -> list[CIOutcomeProjection]:
        with self._engine.connect() as connection:
            return _read_ci_outcome_summaries(
                connection,
                org_id,
                repo=repo,
                repository_key=repository_key,
                result=result,
                captured_between=captured_between,
                limit=limit,
                captured_through=captured_through,
                before=before,
                workflow_name=workflow_name,
                pr_number=pr_number,
            )

    def store_push(self, push: Push) -> bool:
        return self.store_push_receipt(push).stored

    def store_push_receipt(self, push: Push) -> RepositoryFactReceipt:
        with self._engine.begin() as connection:
            return _repository_receipt(
                connection, push, pushes, "push_id", push.model_dump(mode="python")
            )

    def read_pushes(
        self,
        org_id: str,
        include_quarantined: bool = False,
        *,
        captured_between: tuple[datetime, datetime] | None = None,
        repository_commits: set[RepositoryCommitReadKey] | None = None,
        captured_through: datetime | None = None,
        limit: int | None = None,
    ) -> list[Push]:
        with self._engine.connect() as connection:
            return _read_pushes(
                connection,
                org_id,
                include_quarantined,
                captured_between=captured_between,
                captured_through=captured_through,
                repository_commits=repository_commits,
                limit=limit,
            )

    def read_stored_push_id(self, push: Push) -> str | None:
        """Return the persisted identity for a Push natural key."""
        statement = select(pushes.c.push_id).where(
            *_repository_natural_conditions(pushes, push)
        )
        with self._engine.connect() as connection:
            return connection.execute(statement).scalar_one_or_none()

    def store_session_commit_observation(
        self, observation: SessionCommitObservation
    ) -> bool:
        with self._engine.begin() as connection:
            if observation.repository_id is not None:
                source = (
                    connection.execute(
                        select(pushes).where(
                            pushes.c.org_id == observation.org_id,
                            pushes.c.push_id == observation.source_push_id,
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if source is None or any(
                    source[name] != getattr(observation, name)
                    for name in _REPOSITORY_COMPONENTS
                ):
                    raise RepositoryIdentityConflict(
                        "observation source Push identity is absent or contradictory"
                    )
            receipt = _repository_receipt(
                connection,
                observation,
                session_commit_observations,
                "observation_id",
                observation.model_dump(mode="python"),
            )
            if receipt.stored:
                connection.execute(
                    _session_upsert(observation, observation.captured_at)
                )
        return receipt.stored

    def read_session_commit_observations(
        self,
        org_id: str,
        *,
        as_of: datetime | None = None,
        commit_sha: CommitSha | None = None,
        repo_commits: set[tuple[str, str]] | None = None,
        repository_commits: set[RepositoryCommitReadKey] | None = None,
        session_ids: set[str] | None = None,
        limit: int | None = None,
        include_quarantined: bool = False,
    ) -> list[SessionCommitObservation]:
        with self._engine.connect() as connection:
            return _read_session_commit_observations(
                connection,
                org_id,
                include_quarantined,
                as_of=as_of,
                commit_sha=commit_sha,
                repo_commits=repo_commits,
                repository_commits=repository_commits,
                session_ids=session_ids,
                limit=limit,
            )

    def store_pull_request_merge(self, merge: PullRequestMerge) -> bool:
        return self.store_pull_request_merge_receipt(merge).stored

    def store_pull_request_merge_receipt(
        self, merge: PullRequestMerge
    ) -> RepositoryFactReceipt:
        with self._engine.begin() as connection:
            return _repository_receipt(
                connection,
                merge,
                pull_request_merges,
                "merge_id",
                merge.model_dump(mode="python"),
            )

    def read_pull_request_merges(
        self,
        org_id: str,
        include_quarantined: bool = False,
        *,
        captured_through: datetime | None = None,
        merged_through: datetime | None = None,
        repo_prs: set[tuple[str, int]] | None = None,
        repository_prs: set[RepositoryPRReadKey] | None = None,
        limit: int | None = None,
    ) -> list[PullRequestMerge]:
        with self._engine.connect() as connection:
            return _read_pull_request_merges(
                connection,
                org_id,
                include_quarantined,
                captured_through=captured_through,
                merged_through=merged_through,
                repo_prs=repo_prs,
                repository_prs=repository_prs,
                limit=limit,
            )

    def store_pull_request_revision(self, revision: PullRequestRevision) -> bool:
        return self.store_pull_request_revision_receipt(revision).stored

    def store_pull_request_revision_receipt(
        self, revision: PullRequestRevision
    ) -> RepositoryFactReceipt:
        with self._engine.begin() as connection:
            return _repository_receipt(
                connection,
                revision,
                pull_request_revisions,
                "revision_id",
                revision.model_dump(mode="python"),
            )

    def store_repository_rename(self, rename: RepositoryRename) -> bool:
        return self.store_repository_rename_receipt(rename).stored

    def store_repository_rename_receipt(
        self, rename: RepositoryRename
    ) -> RepositoryFactReceipt:
        with self._engine.begin() as connection:
            return _repository_receipt(
                connection,
                rename,
                repository_renames,
                "rename_id",
                rename.model_dump(mode="python"),
            )

    def read_repository_renames(
        self,
        org_id: str,
        *,
        captured_through=None,
        limit=None,
        include_quarantined=False,
    ):
        with self._engine.connect() as connection:
            return _read_repository_renames(
                connection,
                org_id,
                captured_through=captured_through,
                limit=limit,
                include_quarantined=include_quarantined,
            )

    def read_repository_identities(
        self,
        org_id: str,
        *,
        captured_through: datetime,
        limit: int = REPOSITORY_IDENTITY_LIMIT,
    ):
        with self.read_snapshot() as snapshot:
            return snapshot.read_repository_identities(
                org_id, captured_through=captured_through, limit=limit
            )

    def read_pull_request_revisions(
        self,
        org_id: str,
        include_quarantined: bool = False,
        *,
        captured_through: datetime | None = None,
        repo_prs: set[tuple[str, int]] | None = None,
        repository_prs: set[RepositoryPRReadKey] | None = None,
        limit: int | None = None,
    ) -> list[PullRequestRevision]:
        with self._engine.connect() as connection:
            return _read_pull_request_revisions(
                connection,
                org_id,
                include_quarantined,
                captured_through=captured_through,
                repo_prs=repo_prs,
                repository_prs=repository_prs,
                limit=limit,
            )

    def read_sessions(self, org_id: str) -> list[SessionRecord]:
        statement = (
            select(sessions)
            .where(sessions.c.org_id == org_id)
            .order_by(sessions.c.first_observed_at, sessions.c.session_id)
        )
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return [SessionRecord(**row) for row in rows]

    def read_session_dossier(
        self, org_id: str, session_id: str, *, limit: int = 500
    ) -> SessionDossierProjection | None:
        with self.read_snapshot() as snapshot:
            return snapshot.read_session_dossier(org_id, session_id, limit=limit)

    def read_evidence_inventory(
        self, org_id: OrgId, session_id: NonEmptyId
    ) -> EvidenceInventory:
        """Read complete, bounded call metadata from one visibility snapshot."""
        org_id, session_id = _evidence_scope(org_id, session_id)
        with self.read_snapshot() as snapshot:
            return snapshot.read_evidence_inventory(org_id, session_id)

    def read_evidence_manifest(
        self, org_id: OrgId, session_id: NonEmptyId, inference_call_id: NonEmptyId
    ) -> EvidenceManifest:
        """Describe the selected call's canonical parts without emitting content."""
        org_id, session_id = _evidence_scope(org_id, session_id)
        inference_call_id = _EVIDENCE_ID.validate_python(inference_call_id)
        with self.read_snapshot() as snapshot:
            return snapshot.read_evidence_manifest(
                org_id, session_id, inference_call_id
            )

    def read_evidence_parts(
        self,
        org_id: OrgId,
        session_id: NonEmptyId,
        references: Sequence[EvidenceReference],
    ) -> EvidenceRead:
        """Read exact occurrences without raw payloads or unrelated message sides."""
        org_id, session_id = _evidence_scope(org_id, session_id)
        references = validate_evidence_references(references)
        with self.read_snapshot() as snapshot:
            return snapshot.read_evidence_parts(org_id, session_id, references)

    def read_delivery_summaries(
        self,
        org_id: str,
        repo_commits: set[tuple[str, str]] | None = None,
        *,
        repository_commits: set[RepositoryCommitReadKey] | None = None,
        captured_through: datetime | None = None,
        limit: int = 500,
    ) -> tuple[list[PushProjection], list[CIOutcomeProjection]]:
        with self.read_snapshot() as snapshot:
            return snapshot.read_delivery_summaries(
                org_id,
                repo_commits,
                repository_commits=repository_commits,
                captured_through=captured_through,
                limit=limit,
            )

    def read_inference_call_summaries(
        self,
        org_id: str,
        *,
        observed_between: tuple[datetime, datetime] | None = None,
        inference_call_ids: set[str] | None = None,
        limit: int | None = None,
    ) -> list[InferenceCallSummary]:
        with self.read_snapshot() as snapshot:
            return snapshot.read_inference_call_summaries(
                org_id,
                observed_between=observed_between,
                inference_call_ids=inference_call_ids,
                limit=limit,
            )

    def read_inference_call_identity_witnesses(
        self, org_id: str, *, call_ids: set[str], observed_through: datetime
    ) -> list[InferenceCallIdentity]:
        """Find unique owners or two witnesses of ambiguity for requested aliases."""
        with self.read_snapshot() as snapshot:
            return snapshot.read_inference_call_identity_witnesses(
                org_id, call_ids=call_ids, observed_through=observed_through
            )

    def read_inference_call_identities(
        self, org_id: str, *, observed_through: datetime, limit: int
    ) -> list[InferenceCallIdentity]:
        """Read complete organization-wide attachment witnesses through a boundary."""
        with self.read_snapshot() as snapshot:
            return snapshot.read_inference_call_identities(
                org_id, observed_through=observed_through, limit=limit
            )

    def read_attribution_candidates(
        self,
        org_id: str,
        *,
        observed_between: tuple[datetime, datetime],
        limit: int | None = None,
    ) -> list[AttributionInferenceCall]:
        with self.read_snapshot() as snapshot:
            return snapshot.read_attribution_candidates(
                org_id, observed_between=observed_between, limit=limit
            )

    def read_report_inference_calls(
        self,
        org_id: str,
        *,
        inference_call_ids: set[str] | None = None,
        observed_through: datetime | None = None,
        limit: int | None = None,
    ) -> list[ReportInferenceCall]:
        """Read bounded output and metadata for shared report algorithms."""
        with self.read_snapshot() as snapshot:
            return snapshot.read_report_inference_calls(
                org_id,
                inference_call_ids=inference_call_ids,
                observed_through=observed_through,
                limit=limit,
            )

    def read_session_inference_calls(
        self,
        org_id: str,
        session_id: str,
        *,
        observed_through: datetime | None = None,
    ) -> list[RolloutInferenceCall]:
        """Read one complete Session through an inclusive evidence boundary."""
        with self.read_snapshot() as snapshot:
            return snapshot.read_session_inference_calls(
                org_id, session_id, observed_through=observed_through
            )

    def iter_inference_calls_by_ids(
        self, org_id: str, inference_call_ids: set[str]
    ) -> Iterator[InferenceCall]:
        """Yield selected complete Facts from one snapshot, buffering one row.

        The iterator holds a database connection until exhaustion or closure.
        Wrap it in ``contextlib.closing`` when consumption can stop early,
        including when the loop body can raise.
        """
        with self.read_snapshot() as snapshot:
            yield from snapshot.iter_inference_calls_by_ids(org_id, inference_call_ids)

    def read_rollout_inference_calls(self, org_id: str) -> list[RolloutInferenceCall]:
        with self.read_snapshot() as snapshot:
            return snapshot.read_rollout_inference_calls(org_id)

    def read_decision_projections(
        self, org_id: str
    ) -> list[DeveloperDecisionProjection]:
        with self.read_snapshot() as snapshot:
            return snapshot.read_decision_projections(org_id)

    def read_edit_observation_projections(
        self, org_id: str
    ) -> list[EditObservationProjection]:
        with self.read_snapshot() as snapshot:
            return snapshot.read_edit_observation_projections(org_id)

    def read_ci_outcome_projections(
        self,
        org_id: str,
        *,
        captured_through: datetime | None = None,
        limit: int | None = None,
    ) -> list[CIOutcomeProjection]:
        with self.read_snapshot() as snapshot:
            return snapshot.read_ci_outcome_projections(
                org_id, captured_through=captured_through, limit=limit
            )

    def read_inference_calls_by_ids(
        self, org_id: str, inference_call_ids: set[str]
    ) -> list[InferenceCall]:
        with self.read_snapshot() as snapshot:
            return snapshot.read_inference_calls_by_ids(org_id, inference_call_ids)

    def read_decisions_by_ids(
        self, org_id: str, decision_ids: set[str]
    ) -> list[DeveloperDecision]:
        with self.read_snapshot() as snapshot:
            return snapshot.read_decisions_by_ids(org_id, decision_ids)

    def read_ci_outcomes_by_ids(
        self, org_id: str, outcome_ids: set[str]
    ) -> list[CIOutcome]:
        with self.read_snapshot() as snapshot:
            return snapshot.read_ci_outcomes_by_ids(org_id, outcome_ids)

    def iter_push_gc_rows(
        self, org_id: str, *, batch_size: int = 1000
    ) -> Iterator[PushGCRow]:
        with self.read_snapshot() as snapshot:
            yield from snapshot.iter_push_gc_rows(org_id, batch_size=batch_size)

    def health_check(self) -> bool:
        with self._engine.connect() as connection:
            return connection.execute(select(1)).scalar_one() == 1

    def available_fact_tables(self) -> tuple[FactTable, ...]:
        """Known physical Fact tables, for inspection before a schema upgrade."""
        with self._engine.connect() as connection:
            names = set(inspect(connection).get_table_names())
        return tuple(
            fact_table
            for fact_table, table in _FACT_TABLES.items()
            if table.name in names
        )

    def count_facts(
        self,
        org_id: str,
        fact_table: FactTable,
        *,
        include_quarantined: bool = False,
    ) -> int:
        fact_table = FactTable(fact_table)
        table = _FACT_TABLES[fact_table]
        statement = (
            select(func.count())
            .select_from(table)
            .where(*_fact_conditions(org_id, fact_table, include_quarantined))
        )
        with self._engine.connect() as connection:
            return int(connection.execute(statement).scalar_one())

    def count_session_facts(
        self,
        org_id: str,
        fact_table: FactTable,
        session_id: str,
        *,
        include_quarantined: bool = False,
    ) -> int:
        fact_table = FactTable(fact_table)
        if fact_table not in _SESSION_FACT_TABLES:
            raise ValueError(f"{fact_table.value} is not a session-scoped fact table")
        table = _FACT_TABLES[fact_table]
        statement = (
            select(func.count())
            .select_from(table)
            .where(
                *_fact_conditions(org_id, fact_table, include_quarantined),
                table.c.session_id == session_id,
            )
        )
        with self._engine.connect() as connection:
            return int(connection.execute(statement).scalar_one())

    def count_sessions(self, org_id: str) -> int:
        statement = (
            select(func.count())
            .select_from(sessions)
            .where(sessions.c.org_id == org_id)
        )
        with self._engine.connect() as connection:
            return int(connection.execute(statement).scalar_one())

    def quarantine_fact(
        self,
        org_id: str,
        fact_table: FactTable | str,
        fact_id: str,
        *,
        reason: str,
    ) -> None:
        self._append_quarantine(
            org_id, fact_table, fact_id, QuarantineAction.QUARANTINE, reason
        )

    def release_fact(
        self,
        org_id: str,
        fact_table: FactTable | str,
        fact_id: str,
        *,
        reason: str,
    ) -> None:
        self._append_quarantine(
            org_id, fact_table, fact_id, QuarantineAction.RELEASE, reason
        )

    def _append_quarantine(
        self,
        org_id: str,
        fact_table: FactTable | str,
        fact_id: str,
        action: QuarantineAction,
        reason: str,
    ) -> None:
        _require_head_revision(self._engine, action.value)
        record = QuarantineRecord(
            org_id=org_id,
            fact_table=fact_table,
            fact_id=fact_id,
            action=action,
            reason=reason,
        )
        table = FactTable(record.fact_table)
        target = _FACT_TABLES[table]
        primary_key = _FACT_PRIMARY_KEYS[table]
        with self._engine.begin() as connection:
            exists = connection.execute(
                select(1)
                .where(
                    target.c.org_id == record.org_id,
                    primary_key == record.fact_id,
                )
                .limit(1)
            ).first()
            if exists is None:
                logger.warning(
                    "quarantine_target_not_found table=%s fact_id=%s action=%s",
                    table,
                    record.fact_id[:100],
                    record.action,
                )
            connection.execute(
                insert(fact_quarantine).values(**record.model_dump(mode="python"))
            )

    def read_quarantine_log(self, org_id: str) -> list[QuarantineRecord]:
        statement = (
            select(
                fact_quarantine.c.quarantine_id,
                fact_quarantine.c.org_id,
                fact_quarantine.c.fact_table,
                fact_quarantine.c.fact_id,
                fact_quarantine.c.action,
                fact_quarantine.c.reason,
                fact_quarantine.c.recorded_at,
            )
            .where(fact_quarantine.c.org_id == org_id)
            .order_by(fact_quarantine.c.quarantine_revision)
        )
        with self._engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return [QuarantineRecord.model_validate(row) for row in rows]

    def quarantine_revision(self, org_id: str) -> int:
        with self._engine.connect() as connection:
            return _quarantine_revision(connection, org_id)

    @contextmanager
    def read_snapshot(self) -> Iterator[_FactSnapshot]:
        connection = self._engine.connect()
        transaction = None
        try:
            transaction = connection.begin()
            connection.exec_driver_sql(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
            )
            # Establish the database view at context entry, before a caller can
            # allow a concurrent writer to commit.
            connection.execute(select(1)).scalar_one()
            yield _FactSnapshot(connection)
        finally:
            if transaction is not None and transaction.is_active:
                transaction.rollback()
            connection.close()

    def quarantine_inference_calls_where(
        self,
        org_id: str,
        *,
        captured_between: tuple[datetime, datetime] | None = None,
        session_id: str | None = None,
        provider: str | None = None,
        reason: str,
        dry_run: bool = False,
    ) -> int:
        # Validate every caller-controlled audit field before checking out a
        # connection. The ghost id is legal and is never inserted.
        QuarantineRecord(
            org_id=org_id,
            fact_table=FactTable.INFERENCE_CALLS,
            fact_id="bulk-validation",
            action=QuarantineAction.QUARANTINE,
            reason=reason,
        )
        # The dry-run branch only reads ``inference_calls`` and writes no
        # ``_SerializedText`` value, so it stays usable against a behind
        # database during a staged rollout. The apply branch inserts
        # ``fact_quarantine.reason`` through ``_SerializedText`` and must be
        # gated so the 0009 migration does not re-encode those rows.
        if not dry_run:
            _require_head_revision(self._engine, "quarantine-inference-calls")
        conditions = [inference_calls.c.org_id == org_id]
        if captured_between is not None:
            lower, upper = captured_between
            if lower.tzinfo is None or upper.tzinfo is None:
                raise ValueError("captured_between bounds must be timezone-aware")
            conditions.extend(
                [
                    inference_calls.c.observed_at >= lower,
                    inference_calls.c.observed_at <= upper,
                ]
            )
        if session_id is not None:
            conditions.append(inference_calls.c.session_id == session_id)
        if provider is not None:
            conditions.append(inference_calls.c.gateway_provider == provider)
        matches_statement = (
            select(inference_calls.c.inference_call_id)
            .where(*conditions)
            .order_by(inference_calls.c.inference_call_id)
        )
        with self._engine.begin() as connection:
            fact_ids = list(connection.execute(matches_statement).scalars())
            if dry_run or not fact_ids:
                return len(fact_ids)
            records = [
                QuarantineRecord(
                    org_id=org_id,
                    fact_table=FactTable.INFERENCE_CALLS,
                    fact_id=fact_id,
                    action=QuarantineAction.QUARANTINE,
                    reason=reason,
                ).model_dump(mode="python")
                for fact_id in fact_ids
            ]
            connection.execute(insert(fact_quarantine).values(records))
        return len(fact_ids)


class _FactSnapshot:
    """One borrowed connection's immutable PostgreSQL fact view."""

    def read_evidence_inventory(
        self, org_id: OrgId, session_id: NonEmptyId
    ) -> EvidenceInventory:
        org_id, session_id = _evidence_scope(org_id, session_id)
        revision = self.quarantine_revision(org_id)
        found = self._connection.execute(
            select(
                select(1)
                .select_from(sessions)
                .where(sessions.c.org_id == org_id, sessions.c.session_id == session_id)
                .exists()
            )
        ).scalar_one()
        calls = []
        quarantined = 0
        if found:
            conditions = _evidence_conditions(org_id, session_id)
            count, size = _evidence_preflight(
                self._connection, conditions, _EVIDENCE_METADATA_TEXT_COLUMNS
            )
            if count > evidence.EVIDENCE_INVENTORY_LIMIT:
                raise EvidenceReadError(
                    "evidence_inventory_limit",
                    count=count,
                    limit=evidence.EVIDENCE_INVENTORY_LIMIT,
                )
            _check_evidence_bytes(size)
            total = self._connection.execute(
                select(func.count())
                .select_from(inference_calls)
                .where(
                    inference_calls.c.org_id == org_id,
                    inference_calls.c.session_id == session_id,
                )
            ).scalar_one()
            quarantined = total - count
            rows = self._connection.execute(
                select(*_evidence_metadata_columns()).where(*conditions)
            ).mappings()
            calls = [_evidence_metadata(row) for row in rows]
        return project_evidence_inventory(
            session_id,
            revision,
            found=found,
            calls=calls,
            quarantined_inference_calls=quarantined,
        )

    def read_evidence_manifest(
        self, org_id: OrgId, session_id: NonEmptyId, inference_call_id: NonEmptyId
    ) -> EvidenceManifest:
        org_id, session_id = _evidence_scope(org_id, session_id)
        inference_call_id = _EVIDENCE_ID.validate_python(inference_call_id)
        conditions = _evidence_conditions(org_id, session_id, {inference_call_id})
        count, size = _evidence_preflight(
            self._connection,
            conditions,
            (*_EVIDENCE_METADATA_TEXT_COLUMNS, "input_messages", "output_messages"),
        )
        if not count:
            raise EvidenceReadError("evidence_unavailable")
        _check_evidence_bytes(size)
        row = (
            self._connection.execute(
                select(
                    *_evidence_metadata_columns(),
                    inference_calls.c.input_messages,
                    inference_calls.c.output_messages,
                ).where(*conditions)
            )
            .mappings()
            .one()
        )
        return project_evidence_manifest(
            session_id,
            self.quarantine_revision(org_id),
            _evidence_metadata(row),
            _messages(row["input_messages"]),
            _messages(row["output_messages"]),
        )

    def read_evidence_parts(
        self,
        org_id: OrgId,
        session_id: NonEmptyId,
        references: Sequence[EvidenceReference],
    ) -> EvidenceRead:
        org_id, session_id = _evidence_scope(org_id, session_id)
        references = validate_evidence_references(references)
        selected: dict[str, set[str]] = {}
        for reference in references:
            selected.setdefault(reference.inference_call_id, set()).add(reference.side)
        groups: dict[tuple[str, ...], set[str]] = {}
        for identifier, sides in selected.items():
            groups.setdefault(tuple(sorted(sides)), set()).add(identifier)
        statements = []
        total_bytes = 0
        for sides, identifiers in sorted(groups.items()):
            conditions = _evidence_conditions(org_id, session_id, identifiers)
            columns = tuple(f"{side}_messages" for side in sides)
            _, size = _evidence_preflight(
                self._connection, conditions, ("inference_call_id", *columns)
            )
            total_bytes += size
            statements.append(
                (
                    sides,
                    select(
                        inference_calls.c.inference_call_id,
                        inference_calls.c.observed_at,
                        *(inference_calls.c[name] for name in columns),
                    ).where(*conditions),
                )
            )
        # Preflight every selected source before transferring any source column.
        _check_evidence_bytes(total_bytes)
        sources = {}
        for sides, statement in statements:
            for row in self._connection.execute(statement).mappings():
                for side in sides:
                    sources[(row["inference_call_id"], side)] = EvidenceMessageSource(
                        row["observed_at"], tuple(_messages(row[f"{side}_messages"]))
                    )
        return project_evidence_read(
            session_id, self.quarantine_revision(org_id), references, sources
        )

    def read_repository_renames(
        self,
        org_id: str,
        *,
        captured_through=None,
        limit=None,
        include_quarantined=False,
    ):
        return _read_repository_renames(
            self._connection,
            org_id,
            captured_through=captured_through,
            limit=limit,
            include_quarantined=include_quarantined,
        )

    def read_repository_identities(
        self,
        org_id: str,
        *,
        captured_through: datetime,
        limit: int = REPOSITORY_IDENTITY_LIMIT,
    ):
        return _read_repository_identities(
            self._connection, org_id, captured_through=captured_through, limit=limit
        )

    def __init__(self, connection: Connection) -> None:
        self._connection = connection
        self._cache: dict[tuple[object, ...], tuple[object, ...]] = {}

    @contextmanager
    def read_snapshot(self) -> Iterator[_FactSnapshot]:
        yield self

    def read_inference_calls(
        self,
        org_id: str,
        since: timedelta | None = None,
        include_quarantined: bool = False,
    ) -> list[InferenceCall]:
        return _read_inference_calls(
            self._connection,
            org_id,
            include_quarantined,
            observed_after=(datetime.now(UTC) - since if since is not None else None),
        )

    def quarantine_revision(self, org_id: str) -> int:
        return _quarantine_revision(self._connection, org_id)

    def read_decisions(
        self,
        org_id: str,
        *,
        captured_through: datetime | None = None,
        session_ids: set[str] | None = None,
        call_ids: set[str] | None = None,
        limit: int | None = None,
        include_quarantined: bool = False,
    ) -> list[DeveloperDecision]:
        return _read_decisions(
            self._connection,
            org_id,
            include_quarantined,
            captured_through=captured_through,
            session_ids=session_ids,
            call_ids=call_ids,
            limit=limit,
        )

    def read_edit_observations(
        self,
        org_id: str,
        *,
        captured_through: datetime | None = None,
        session_ids: set[str] | None = None,
        call_ids: set[str] | None = None,
        limit: int | None = None,
        include_quarantined: bool = False,
    ) -> list[EditObservation]:
        return _read_edit_observations(
            self._connection,
            org_id,
            include_quarantined,
            captured_through=captured_through,
            session_ids=session_ids,
            call_ids=call_ids,
            limit=limit,
        )

    def read_rejected_edits(
        self, org_id: str, *, include_quarantined: bool = False
    ) -> list[RejectedEdit]:
        return _read_rejected_edits(self._connection, org_id, include_quarantined)

    def read_retry_linkages(
        self,
        org_id: str,
        *,
        captured_through: datetime | None = None,
        session_ids: set[str] | None = None,
        limit: int | None = None,
        include_quarantined: bool = False,
    ) -> list[RetryLinkage]:
        return _read_retry_linkages(
            self._connection,
            org_id,
            include_quarantined,
            captured_through=captured_through,
            session_ids=session_ids,
            limit=limit,
        )

    def read_ci_outcome_by_run(
        self,
        org_id: str,
        provider: CIProvider,
        run_id: str,
        *,
        run_attempt: int | None,
        repository_key: RepositoryReadKey | None = None,
        captured_through: datetime | None = None,
    ) -> CIOutcomeProjection | None:
        return _read_ci_outcome_by_run(
            self._connection,
            org_id,
            provider,
            run_id,
            run_attempt=run_attempt,
            repository_key=repository_key,
            captured_through=captured_through,
        )

    def read_ci_outcome_summaries(
        self,
        org_id: str,
        *,
        repo: str | None = None,
        repository_key: RepositoryReadKey | None = None,
        result: CIResult,
        captured_between: tuple[datetime, datetime],
        limit: int,
        captured_through: datetime | None = None,
        before: tuple[datetime, str] | None = None,
        workflow_name: str | None = None,
        pr_number: int | None = None,
    ) -> list[CIOutcomeProjection]:
        return _read_ci_outcome_summaries(
            self._connection,
            org_id,
            repo=repo,
            repository_key=repository_key,
            result=result,
            captured_between=captured_between,
            limit=limit,
            captured_through=captured_through,
            before=before,
            workflow_name=workflow_name,
            pr_number=pr_number,
        )

    def read_ci_outcomes(
        self,
        org_id: str,
        *,
        captured_through: datetime | None = None,
        commit_sha: CommitSha | None = None,
        repo_commits: set[tuple[str, str]] | None = None,
        repository_commits: set[RepositoryCommitReadKey] | None = None,
        limit: int | None = None,
        include_quarantined: bool = False,
    ) -> list[CIOutcome]:
        return _read_ci_outcomes(
            self._connection,
            org_id,
            include_quarantined,
            captured_through=captured_through,
            commit_sha=commit_sha,
            repo_commits=repo_commits,
            repository_commits=repository_commits,
            limit=limit,
        )

    def read_pushes(
        self,
        org_id: str,
        *,
        captured_between: tuple[datetime, datetime] | None = None,
        repository_commits: set[RepositoryCommitReadKey] | None = None,
        captured_through: datetime | None = None,
        limit: int | None = None,
        include_quarantined: bool = False,
    ) -> list[Push]:
        return _read_pushes(
            self._connection,
            org_id,
            include_quarantined,
            captured_between=captured_between,
            captured_through=captured_through,
            repository_commits=repository_commits,
            limit=limit,
        )

    def read_session_commit_observations(
        self,
        org_id: str,
        *,
        as_of: datetime | None = None,
        commit_sha: CommitSha | None = None,
        repo_commits: set[tuple[str, str]] | None = None,
        repository_commits: set[RepositoryCommitReadKey] | None = None,
        session_ids: set[str] | None = None,
        limit: int | None = None,
        include_quarantined: bool = False,
    ) -> list[SessionCommitObservation]:
        return _read_session_commit_observations(
            self._connection,
            org_id,
            include_quarantined,
            as_of=as_of,
            commit_sha=commit_sha,
            repo_commits=repo_commits,
            repository_commits=repository_commits,
            session_ids=session_ids,
            limit=limit,
        )

    def read_session_dossier(
        self, org_id: str, session_id: str, *, limit: int = 500
    ) -> SessionDossierProjection | None:
        if not 1 <= limit <= 1000:
            raise ValueError("Session dossier limit must be between 1 and 1000")
        session = (
            self._connection.execute(
                select(
                    sessions.c.session_id,
                    sessions.c.first_observed_at,
                    sessions.c.last_observed_at,
                ).where(
                    sessions.c.org_id == org_id,
                    sessions.c.session_id == session_id,
                )
            )
            .mappings()
            .one_or_none()
        )
        if session is None:
            return None
        coverage = _session_dossier_coverage(self._connection, org_id, session_id)
        visible = sum(row.visible for row in coverage.values())
        if visible > limit:
            raise ValueError("Session exceeds the dossier limit")
        timeline = _session_dossier_timeline(self._connection, org_id, session_id)
        return SessionDossierProjection(
            **session,
            coverage=coverage,
            timeline=tuple(
                sorted(
                    timeline,
                    key=lambda row: (row.occurred_at, row.event_type, row.fact_id),
                )
            ),
        )

    def read_delivery_summaries(
        self,
        org_id: str,
        repo_commits: set[tuple[str, str]] | None = None,
        *,
        repository_commits: set[RepositoryCommitReadKey] | None = None,
        captured_through: datetime | None = None,
        limit: int = 500,
    ) -> tuple[list[PushProjection], list[CIOutcomeProjection]]:
        if not 1 <= limit <= 1000:
            raise ValueError("delivery summary limit must be between 1 and 1000")
        _validate_bounded_fact_read(captured_through, None, "delivery summary")
        if repo_commits is None and repository_commits is None:
            raise ValueError("delivery summaries require repository commit keys")
        push_condition = _repository_composite_condition(
            pushes,
            pushes.c.after_sha,
            literal_keys=repo_commits,
            qualified_keys=repository_commits,
        )
        ci_condition = _repository_composite_condition(
            ci_outcomes,
            ci_outcomes.c.commit_sha,
            literal_keys=repo_commits,
            qualified_keys=repository_commits,
        )
        if (
            len(repo_commits if repo_commits is not None else repository_commits)
            > limit
        ):
            raise ValueError("Session exceeds the delivery summary limit")
        push_bounds, ci_bounds = [], []
        if captured_through is not None:
            push_bounds.append(pushes.c.captured_at <= captured_through)
            ci_bounds.append(ci_outcomes.c.captured_at <= captured_through)
        push_statement = (
            select(
                pushes.c.push_id,
                pushes.c.provider,
                pushes.c.repo,
                pushes.c.ref,
                pushes.c.before_sha,
                pushes.c.after_sha,
                pushes.c.forced,
                pushes.c.captured_at,
                pushes.c.repository_provider,
                pushes.c.repository_host,
                pushes.c.repository_id,
            )
            .where(
                *_fact_conditions(org_id, FactTable.PUSHES, False),
                push_condition,
                *push_bounds,
            )
            .order_by(pushes.c.captured_at, pushes.c.push_id)
            .limit(limit + 1)
        )
        ci_statement = (
            select(*_columns_except(ci_outcomes, "raw"))
            .where(
                *_fact_conditions(org_id, FactTable.CI_OUTCOMES, False),
                ci_condition,
                *ci_bounds,
            )
            .order_by(ci_outcomes.c.captured_at, ci_outcomes.c.outcome_id)
            .limit(limit + 1)
        )
        push_rows = self._connection.execute(push_statement).mappings().all()
        ci_rows = self._connection.execute(ci_statement).mappings().all()
        if len(push_rows) > limit or len(ci_rows) > limit:
            raise ValueError("Session exceeds the delivery summary limit")
        return (
            [
                PushProjection(
                    **{
                        **row,
                        "repository_provider": _coerce_repository_provider(
                            row["repository_provider"]
                        ),
                    }
                )
                for row in push_rows
            ],
            [_ci_outcome_projection(row) for row in ci_rows],
        )

    def read_pull_request_merges(
        self,
        org_id: str,
        *,
        captured_through: datetime | None = None,
        merged_through: datetime | None = None,
        repo_prs: set[tuple[str, int]] | None = None,
        repository_prs: set[RepositoryPRReadKey] | None = None,
        limit: int | None = None,
        include_quarantined: bool = False,
    ) -> list[PullRequestMerge]:
        return _read_pull_request_merges(
            self._connection,
            org_id,
            include_quarantined,
            captured_through=captured_through,
            merged_through=merged_through,
            repo_prs=repo_prs,
            repository_prs=repository_prs,
            limit=limit,
        )

    def read_pull_request_revisions(
        self,
        org_id: str,
        *,
        captured_through: datetime | None = None,
        repo_prs: set[tuple[str, int]] | None = None,
        repository_prs: set[RepositoryPRReadKey] | None = None,
        limit: int | None = None,
        include_quarantined: bool = False,
    ) -> list[PullRequestRevision]:
        return _read_pull_request_revisions(
            self._connection,
            org_id,
            include_quarantined,
            captured_through=captured_through,
            repo_prs=repo_prs,
            repository_prs=repository_prs,
            limit=limit,
        )

    def read_inference_call_summaries(
        self,
        org_id: str,
        *,
        observed_between: tuple[datetime, datetime] | None = None,
        inference_call_ids: set[str] | None = None,
        limit: int | None = None,
    ) -> list[InferenceCallSummary]:
        if limit is not None and (type(limit) is not int or limit <= 0):
            raise ValueError("Inference call cohort limit must be positive")
        conditions = list(_fact_conditions(org_id, FactTable.INFERENCE_CALLS, False))
        selected_ids = None
        if inference_call_ids is not None:
            selected_ids = _validated_identity_filter(inference_call_ids)
            conditions.append(inference_calls.c.inference_call_id.in_(selected_ids))
        if observed_between is not None:
            lower, upper = observed_between
            if lower.tzinfo is None or upper.tzinfo is None:
                raise ValueError("observed_between bounds must be timezone-aware")
            if lower >= upper:
                raise ValueError(
                    "observed_between lower bound must precede upper bound"
                )
            conditions.extend(
                [
                    inference_calls.c.observed_at >= lower,
                    inference_calls.c.observed_at < upper,
                ]
            )
        key = (
            "inference_call_summaries",
            org_id,
            observed_between,
            selected_ids,
            limit,
        )
        if key in self._cache:
            return list(self._cache[key])
        statement = (
            select(
                inference_calls.c.inference_call_id,
                inference_calls.c.org_id,
                inference_calls.c.session_id,
                inference_calls.c.user_id,
                inference_calls.c.gateway_provider,
                inference_calls.c.model_provider,
                inference_calls.c.model,
                inference_calls.c.input_tokens,
                inference_calls.c.output_tokens,
                inference_calls.c.duration_ms,
                inference_calls.c.model_call_id,
                inference_calls.c.observed_at,
            )
            .where(*conditions)
            .order_by(
                inference_calls.c.observed_at,
                inference_calls.c.inference_call_id,
            )
        )
        if limit is not None:
            statement = statement.limit(limit + 1)
        rows = self._connection.execute(statement).mappings().all()
        if limit is not None and len(rows) > limit:
            raise OperationalReportLimitExceeded(
                f"Inference call cohort exceeds {limit}"
            )
        result = tuple(
            InferenceCallSummary(
                **{
                    **row,
                    "gateway_provider": GatewayProvider(row["gateway_provider"]),
                }
            )
            for row in rows
        )
        self._cache[key] = result
        return list(result)

    def read_inference_call_identity_witnesses(
        self, org_id: str, *, call_ids: set[str], observed_through: datetime
    ) -> list[InferenceCallIdentity]:
        """Read at most two visible owners per requested alias in this snapshot.

        Two owners prove ambiguity; these partial aliases must never replace the
        complete identity population required by a self-contained bundle.
        """
        org_id = _EVIDENCE_ORG.validate_python(org_id)
        identifiers = _validated_identity_filter(call_ids)
        if observed_through.utcoffset() is None:
            raise ValueError("observed_through must be timezone-aware")
        observed_through = observed_through.astimezone(UTC)
        if not identifiers:
            return []
        key = (
            "inference_call_identity_witnesses",
            org_id,
            observed_through,
            identifiers,
        )
        if key in self._cache:
            return list(self._cache[key])
        requested = values(
            sql_column("call_id", inference_call_aliases.c.call_id.type),
            name="requested_aliases",
        ).data([(identifier,) for identifier in identifiers])
        owners = (
            select(
                inference_calls.c.inference_call_id,
                inference_calls.c.org_id,
                inference_calls.c.session_id,
                inference_calls.c.observed_at,
            )
            .select_from(inference_call_aliases.join(inference_calls))
            .where(
                array(
                    [inference_call_aliases.c.org_id, inference_call_aliases.c.call_id]
                )
                == array([org_id, requested.c.call_id]).cast(
                    ARRAY(inference_call_aliases.c.call_id.type)
                ),
                *_fact_conditions(org_id, FactTable.INFERENCE_CALLS, False),
                inference_calls.c.observed_at <= observed_through,
            )
            .distinct()
            .order_by(inference_calls.c.inference_call_id)
            .limit(2)
            .correlate(requested)
            .lateral("alias_owners")
        )
        statement = select(*owners.c, requested.c.call_id).select_from(
            requested.join(owners, true())
        )
        identities = {}
        aliases = {}
        for row in self._connection.execute(statement).mappings():
            fact_id = row["inference_call_id"]
            identities[fact_id] = {name: row[name] for name in owners.c.keys()}
            aliases.setdefault(fact_id, set()).add(row["call_id"])
        result = tuple(
            sorted(
                (
                    InferenceCallIdentity(
                        **row, call_ids=tuple(sorted(aliases[fact_id]))
                    )
                    for fact_id, row in identities.items()
                ),
                key=lambda item: (
                    item.observed_at.astimezone(UTC),
                    item.inference_call_id,
                ),
            )
        )
        self._cache[key] = result
        return list(result)

    def read_inference_call_identities(
        self, org_id: str, *, observed_through: datetime, limit: int
    ) -> list[InferenceCallIdentity]:
        """Read all visible aliases or refuse an incomplete uniqueness population.

        The row budget does not bound output-message bytes or database scan work.
        Tool aliases require decoding canonical TEXT in Python, without SQL casts.
        """
        if type(limit) is not int or limit <= 0:
            raise ValueError("Inference call identity limit must be positive")
        if observed_through.utcoffset() is None:
            raise ValueError("observed_through must be timezone-aware")
        key = ("inference_call_identities", org_id, observed_through, limit)
        if key in self._cache:
            return list(self._cache[key])
        statement = (
            select(
                inference_calls.c.inference_call_id,
                inference_calls.c.org_id,
                inference_calls.c.session_id,
                inference_calls.c.model_call_id,
                inference_calls.c.output_messages,
                inference_calls.c.observed_at,
            )
            .where(
                *_fact_conditions(org_id, FactTable.INFERENCE_CALLS, False),
                inference_calls.c.observed_at <= observed_through,
            )
            .order_by(
                inference_calls.c.observed_at, inference_calls.c.inference_call_id
            )
            .limit(limit + 1)
        )
        _check_inference_payload_budget(
            self._connection,
            statement,
            ("output_messages",),
            label="Inference call identity population",
            limit=limit,
        )
        identities = []
        with self._connection.execute(statement.execution_options(yield_per=1)) as rows:
            for row in rows.mappings():
                if len(identities) == limit:
                    raise OperationalReportLimitExceeded(
                        f"Inference call identity population exceeds {limit}"
                    )
                aliases = {
                    part.id
                    for message in _messages(row["output_messages"])
                    for part in message.parts
                    if isinstance(part, ToolCallPart)
                }
                if row["model_call_id"] is not None:
                    aliases.add(row["model_call_id"])
                identities.append(
                    InferenceCallIdentity(
                        inference_call_id=row["inference_call_id"],
                        org_id=row["org_id"],
                        session_id=row["session_id"],
                        observed_at=row["observed_at"],
                        call_ids=tuple(sorted(aliases)),
                    )
                )
        self._cache[key] = tuple(identities)
        return identities

    def read_attribution_candidates(
        self,
        org_id: str,
        *,
        observed_between: tuple[datetime, datetime],
        limit: int | None = None,
    ) -> list[AttributionInferenceCall]:
        lower, upper = observed_between
        if lower.tzinfo is None or upper.tzinfo is None:
            raise ValueError("observed_between bounds must be timezone-aware")
        if limit is not None and (type(limit) is not int or limit <= 0):
            raise ValueError("Attribution candidate limit must be positive")
        statement = (
            select(
                inference_calls.c.inference_call_id,
                inference_calls.c.session_id,
                inference_calls.c.model_call_id,
                inference_calls.c.output_messages,
                inference_calls.c.observed_at,
            )
            .where(
                *_fact_conditions(org_id, FactTable.INFERENCE_CALLS, False),
                inference_calls.c.observed_at >= lower,
                inference_calls.c.observed_at <= upper,
            )
            .order_by(
                inference_calls.c.observed_at,
                inference_calls.c.inference_call_id,
            )
        )
        if limit is not None:
            statement = statement.limit(limit + 1)
        _check_inference_payload_budget(
            self._connection,
            statement,
            ("output_messages",),
            label="Attribution candidate cohort",
            limit=limit,
            total_bytes_limit=INFERENCE_PROJECTION_BYTES_LIMIT,
        )
        with self._connection.execute(statement.execution_options(yield_per=1)) as rows:
            return [
                AttributionInferenceCall(
                    **{**row, "output_messages": _messages(row["output_messages"])}
                )
                for row in rows.mappings()
            ]

    def read_report_inference_calls(
        self,
        org_id: str,
        *,
        inference_call_ids: set[str] | None = None,
        observed_through: datetime | None = None,
        limit: int | None = None,
    ) -> list[ReportInferenceCall]:
        """Read report evidence without retaining decoded output in the snapshot."""
        if limit is None:
            limit = INFERENCE_REPORT_ROW_LIMIT
        _validate_bounded_fact_read(observed_through, limit, "Inference call report")
        if inference_call_ids is not None and not inference_call_ids:
            return []
        conditions = _inference_content_conditions(
            org_id,
            inference_call_ids=inference_call_ids,
            observed_through=observed_through,
        )
        statement = (
            select(
                *_columns_except(
                    inference_calls,
                    "schema_version",
                    "input_messages",
                    "raw",
                    "call_alias_count",
                )
            )
            .where(*conditions)
            .order_by(
                inference_calls.c.observed_at, inference_calls.c.inference_call_id
            )
        )
        if limit is not None:
            statement = statement.limit(limit + 1)
        _check_inference_payload_budget(
            self._connection,
            statement,
            ("output_messages",),
            label="Inference call report cohort",
            limit=limit,
            total_bytes_limit=INFERENCE_PROJECTION_BYTES_LIMIT,
        )
        with self._connection.execute(statement.execution_options(yield_per=1)) as rows:
            return [
                ReportInferenceCall(
                    **{
                        **row,
                        "gateway_provider": GatewayProvider(row["gateway_provider"]),
                        "output_messages": _messages(row["output_messages"]),
                    }
                )
                for row in rows.mappings()
            ]

    def read_session_inference_calls(
        self,
        org_id: str,
        session_id: str,
        *,
        observed_through: datetime | None = None,
    ) -> list[RolloutInferenceCall]:
        """Read every visible call in one Session, without a cohort lower bound.

        Encoded input/output size is checked before any content is transferred.
        The snapshot retains no decoded content after the caller releases it.
        """
        _validate_bounded_fact_read(observed_through, None, "Inference call Session")
        statement = (
            select(*_columns_except(inference_calls, "raw", "call_alias_count"))
            .where(
                *_inference_content_conditions(
                    org_id, observed_through=observed_through
                ),
                inference_calls.c.session_id == session_id,
            )
            .order_by(
                inference_calls.c.observed_at, inference_calls.c.inference_call_id
            )
        )
        _check_inference_payload_budget(
            self._connection,
            statement,
            ("input_messages", "output_messages"),
            label="Inference call Session",
            total_bytes_limit=INFERENCE_SESSION_BYTES_LIMIT,
        )
        with self._connection.execute(statement.execution_options(yield_per=1)) as rows:
            return [_rollout_inference_call(row) for row in rows.mappings()]

    def read_rollout_inference_calls(self, org_id: str) -> list[RolloutInferenceCall]:
        """Explicitly materialize all Rollout content without a snapshot cache."""
        statement = (
            select(*_columns_except(inference_calls, "raw", "call_alias_count"))
            .where(*_fact_conditions(org_id, FactTable.INFERENCE_CALLS, False))
            .order_by(
                inference_calls.c.observed_at, inference_calls.c.inference_call_id
            )
        )
        _check_inference_payload_budget(
            self._connection,
            statement,
            ("input_messages", "output_messages"),
            label="Rollout Inference call",
        )
        with self._connection.execute(statement.execution_options(yield_per=1)) as rows:
            return [_rollout_inference_call(row) for row in rows.mappings()]

    def read_decision_projections(
        self, org_id: str
    ) -> list[DeveloperDecisionProjection]:
        key = ("decision_projections", org_id)
        if key in self._cache:
            return list(self._cache[key])
        rows = self._connection.execute(
            select(*_columns_except(developer_decisions, "raw"))
            .where(*_fact_conditions(org_id, FactTable.DEVELOPER_DECISIONS, False))
            .order_by(
                developer_decisions.c.occurred_at,
                developer_decisions.c.decision_id,
            )
        ).mappings()
        result = tuple(
            DeveloperDecisionProjection(
                **{
                    **row,
                    "agent_harness": AgentHarness(row["agent_harness"]),
                    "interaction_mode": InteractionMode(row["interaction_mode"]),
                }
            )
            for row in rows
        )
        self._cache[key] = result
        return list(result)

    def read_edit_observation_projections(
        self, org_id: str
    ) -> list[EditObservationProjection]:
        key = ("edit_observation_projections", org_id)
        if key in self._cache:
            return list(self._cache[key])
        rows = self._connection.execute(
            select(*_columns_except(edit_observations, "raw"))
            .where(*_fact_conditions(org_id, FactTable.EDIT_OBSERVATIONS, False))
            .order_by(
                edit_observations.c.occurred_at,
                edit_observations.c.observation_id,
            )
        ).mappings()
        result = tuple(
            EditObservationProjection(
                **{
                    **row,
                    "agent_harness": AgentHarness(row["agent_harness"]),
                    "applied_text": json.loads(row["applied_text"]),
                    "observed_file_text": json.loads(row["observed_file_text"]),
                }
            )
            for row in rows
        )
        self._cache[key] = result
        return list(result)

    def read_ci_outcome_projections(
        self,
        org_id: str,
        *,
        captured_through: datetime | None = None,
        limit: int | None = None,
    ) -> list[CIOutcomeProjection]:
        _validate_bounded_fact_read(captured_through, limit, "CI outcome")
        if captured_through is not None:
            captured_through = captured_through.astimezone(UTC)
        key = ("ci_outcome_projections", org_id, captured_through, limit)
        if key in self._cache:
            return list(self._cache[key])
        statement = (
            select(*_columns_except(ci_outcomes, "raw"))
            .where(*_fact_conditions(org_id, FactTable.CI_OUTCOMES, False))
            .order_by(ci_outcomes.c.captured_at, ci_outcomes.c.outcome_id)
        )
        if captured_through is not None:
            statement = statement.where(ci_outcomes.c.captured_at <= captured_through)
        rows = _bounded_fact_rows(self._connection, statement, limit, "CI outcome")
        result = tuple(_ci_outcome_projection(row) for row in rows)
        self._cache[key] = result
        return list(result)

    def iter_push_gc_rows(
        self, org_id: str, *, batch_size: int = 1000
    ) -> Iterator[PushGCRow]:
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        cursor: tuple[datetime, str] | None = None
        while True:
            conditions = list(_fact_conditions(org_id, FactTable.PUSHES, False))
            if cursor is not None:
                conditions.append(
                    tuple_(pushes.c.captured_at, pushes.c.push_id) > cursor
                )
            rows = (
                self._connection.execute(
                    select(
                        pushes.c.push_id,
                        pushes.c.repo,
                        pushes.c.captured_at,
                        pushes.c.repository_provider,
                        pushes.c.repository_host,
                        pushes.c.repository_id,
                    )
                    .where(*conditions)
                    .order_by(pushes.c.captured_at, pushes.c.push_id)
                    .limit(batch_size)
                )
                .mappings()
                .all()
            )
            if not rows:
                return
            for row in rows:
                yield PushGCRow(
                    **{
                        name: (
                            _coerce_repository_provider(value)
                            if name == "repository_provider"
                            else value
                        )
                        for name, value in row.items()
                        if name != "push_id"
                    }
                )
            last = rows[-1]
            cursor = (last["captured_at"], last["push_id"])

    def iter_inference_calls_by_ids(
        self, org_id: str, inference_call_ids: set[str]
    ) -> Iterator[InferenceCall]:
        """Yield full Facts in observation/identity order with one row buffered.

        Close a partially consumed iterator before leaving the snapshot. The
        snapshot owns consistency across preflight, iteration, and other reads.
        """
        if not inference_call_ids:
            return
        statement = (
            select(inference_calls)
            .where(
                *_inference_content_conditions(
                    org_id, inference_call_ids=inference_call_ids
                )
            )
            .order_by(
                inference_calls.c.observed_at, inference_calls.c.inference_call_id
            )
        )
        _check_inference_payload_budget(
            self._connection,
            statement,
            ("input_messages", "output_messages", "raw"),
            label="Inference call",
        )
        with self._connection.execute(statement.execution_options(yield_per=1)) as rows:
            for row in rows.mappings():
                yield _inference_call_from_row(row)

    def read_inference_calls_by_ids(
        self, org_id: str, inference_call_ids: set[str]
    ) -> list[InferenceCall]:
        if not inference_call_ids:
            return []
        return _read_inference_calls(
            self._connection,
            org_id,
            False,
            inference_call_ids=inference_call_ids,
        )

    def read_decisions_by_ids(
        self, org_id: str, decision_ids: set[str]
    ) -> list[DeveloperDecision]:
        if not decision_ids:
            return []
        return _read_decisions(
            self._connection, org_id, False, decision_ids=decision_ids
        )

    def read_ci_outcomes_by_ids(
        self, org_id: str, outcome_ids: set[str]
    ) -> list[CIOutcome]:
        if not outcome_ids:
            return []
        return _read_ci_outcomes(
            self._connection, org_id, False, outcome_ids=outcome_ids
        )


_EVIDENCE_ID = TypeAdapter(NonEmptyId)
_EVIDENCE_ORG = TypeAdapter(OrgId)
_EVIDENCE_METADATA = TypeAdapter(EvidenceCallMetadata)
_EVIDENCE_METADATA_TEXT_COLUMNS = ("inference_call_id", "model_provider", "model")


def _evidence_scope(org_id: OrgId, session_id: NonEmptyId) -> tuple[OrgId, NonEmptyId]:
    return _EVIDENCE_ORG.validate_python(org_id), _EVIDENCE_ID.validate_python(
        session_id
    )


def _evidence_conditions(
    org_id: OrgId, session_id: NonEmptyId, identifiers: set[str] | None = None
) -> list:
    return [
        *_inference_content_conditions(org_id, inference_call_ids=identifiers),
        inference_calls.c.session_id == session_id,
    ]


def _evidence_metadata_columns() -> list:
    return [
        inference_calls.c[name]
        for name in (*_EVIDENCE_METADATA_TEXT_COLUMNS, "observed_at")
    ]


def _evidence_metadata(row: Mapping) -> EvidenceCallMetadata:
    return _EVIDENCE_METADATA.validate_python(
        {name: row[name] for name in (*_EVIDENCE_METADATA_TEXT_COLUMNS, "observed_at")}
    )


def _evidence_preflight(
    connection: Connection, conditions: list, columns: tuple[str, ...]
) -> tuple[int, int]:
    """Count opaque stored bytes without transferring any variable-width field."""
    size = sum(
        func.coalesce(func.octet_length(inference_calls.c[name]), 0) for name in columns
    )
    count, total = connection.execute(
        select(func.count(), func.coalesce(func.sum(size), 0))
        .select_from(inference_calls)
        .where(*conditions)
    ).one()
    return int(count), int(total)


def _check_evidence_bytes(size: int) -> None:
    if size > evidence.EVIDENCE_SOURCE_BYTES_LIMIT:
        raise EvidenceReadError(
            "evidence_source_limit",
            bytes=size,
            limit=evidence.EVIDENCE_SOURCE_BYTES_LIMIT,
        )


def _read_inference_calls(
    connection: Connection,
    org_id: str,
    include_quarantined: bool,
    *,
    inference_call_ids: set[str] | None = None,
    observed_after: datetime | None = None,
) -> list[InferenceCall]:
    conditions = list(
        _fact_conditions(org_id, FactTable.INFERENCE_CALLS, include_quarantined)
    )
    if inference_call_ids is not None:
        conditions.append(
            inference_calls.c.inference_call_id.in_(sorted(inference_call_ids))
        )
    if observed_after is not None:
        conditions.append(inference_calls.c.observed_at >= observed_after)
    statement = (
        select(inference_calls)
        .where(*conditions)
        .order_by(inference_calls.c.observed_at, inference_calls.c.inference_call_id)
    )
    rows = connection.execute(statement).mappings().all()
    return [_inference_call_from_row(row) for row in rows]


def _inference_call_from_row(row: Mapping) -> InferenceCall:
    values = dict(row)
    values.pop("call_alias_count", None)
    for name in ("input_messages", "output_messages", "raw"):
        values[name] = json.loads(values[name])
    return InferenceCall.model_validate(values)


def _validated_identity_filter(identifiers: set[str]) -> tuple[str, ...]:
    if len(identifiers) > COMPOSITE_FILTER_KEY_LIMIT:
        raise OperationalReportLimitExceeded(
            f"Inference call identity filter exceeds {COMPOSITE_FILTER_KEY_LIMIT} keys"
        )
    return tuple(sorted({_EVIDENCE_ID.validate_python(value) for value in identifiers}))


def _rollout_inference_call(row: Mapping) -> RolloutInferenceCall:
    return RolloutInferenceCall(
        **{
            **row,
            "gateway_provider": GatewayProvider(row["gateway_provider"]),
            "input_messages": _messages(row["input_messages"]),
            "output_messages": _messages(row["output_messages"]),
        }
    )


def _inference_content_conditions(
    org_id: str,
    *,
    inference_call_ids: set[str] | None = None,
    observed_through: datetime | None = None,
) -> list:
    conditions = list(_fact_conditions(org_id, FactTable.INFERENCE_CALLS, False))
    if inference_call_ids is not None:
        # Stay below PostgreSQL's parameter ceiling, including tenancy filters.
        if len(inference_call_ids) > 50_000:
            raise OperationalReportLimitExceeded(
                "Inference call selection exceeds 50000 IDs"
            )
        conditions.append(
            inference_calls.c.inference_call_id.in_(sorted(inference_call_ids))
        )
    if observed_through is not None:
        conditions.append(inference_calls.c.observed_at <= observed_through)
    return conditions


def _check_inference_payload_budget(
    connection: Connection,
    statement,
    content_columns: tuple[str, ...],
    *,
    label: str,
    limit: int | None = None,
    total_bytes_limit: int | None = None,
) -> None:
    """Check scalar byte lengths without transferring or interpreting JSON TEXT."""
    encoded_bytes = sum(
        func.octet_length(inference_calls.c[name]) for name in content_columns
    )
    sizes = (
        statement.with_only_columns(encoded_bytes.label("encoded_bytes"))
        .order_by(None)
        .subquery()
    )
    count, largest, total = connection.execute(
        select(
            func.count(),
            func.max(sizes.c.encoded_bytes),
            func.sum(sizes.c.encoded_bytes),
        )
    ).one()
    if limit is not None and count > limit:
        raise OperationalReportLimitExceeded(f"{label} exceeds {limit}")
    if largest is not None and largest > INFERENCE_CALL_ROW_BYTES_LIMIT:
        raise OperationalReportLimitExceeded(
            f"{label} row exceeds {INFERENCE_CALL_ROW_BYTES_LIMIT} encoded content bytes"
        )
    if (
        total_bytes_limit is not None
        and total is not None
        and total > total_bytes_limit
    ):
        raise OperationalReportLimitExceeded(
            f"{label} exceeds {total_bytes_limit} encoded content bytes"
        )


def _messages(value: str) -> list[InferenceMessage]:
    return [InferenceMessage.model_validate(message) for message in json.loads(value)]


def _columns_except(table, *excluded: str) -> list[object]:
    return [column for column in table.c if column.name not in excluded]


def _validate_bounded_fact_read(
    captured_through: datetime | None, limit: int | None, label: str
) -> None:
    if captured_through is not None and captured_through.tzinfo is None:
        raise ValueError("captured_through must be timezone-aware")
    if limit is not None and (type(limit) is not int or limit <= 0):
        raise ValueError(f"{label} limit must be positive")


def validate_composite_filter_keys(keys: set[tuple[object, object]]) -> None:
    """Reject a composite filter that cannot fit safely in one PostgreSQL query."""

    if len(keys) > COMPOSITE_FILTER_KEY_LIMIT:
        raise OperationalReportLimitExceeded(
            f"composite filter exceeds {COMPOSITE_FILTER_KEY_LIMIT} keys"
        )


def _bounded_fact_rows(connection, statement, limit: int | None, label: str):
    if limit is not None:
        statement = statement.limit(limit + 1)
    rows = connection.execute(statement).mappings().all()
    if limit is not None and len(rows) > limit:
        raise OperationalReportLimitExceeded(f"{label} cohort exceeds {limit}")
    return rows


def _read_decisions(
    connection: Connection,
    org_id: str,
    include_quarantined: bool,
    *,
    decision_ids: set[str] | None = None,
    captured_through: datetime | None = None,
    session_ids: set[str] | None = None,
    call_ids: set[str] | None = None,
    limit: int | None = None,
) -> list[DeveloperDecision]:
    _validate_bounded_fact_read(captured_through, limit, "Developer decision")
    if any(values is not None and not values for values in (session_ids, call_ids)):
        return []
    conditions = list(
        _fact_conditions(org_id, FactTable.DEVELOPER_DECISIONS, include_quarantined)
    )
    if decision_ids is not None:
        conditions.append(developer_decisions.c.decision_id.in_(sorted(decision_ids)))
    if captured_through is not None:
        conditions.append(developer_decisions.c.captured_at <= captured_through)
    if session_ids is not None:
        conditions.append(developer_decisions.c.session_id.in_(sorted(session_ids)))
    if call_ids is not None:
        conditions.append(developer_decisions.c.call_id.in_(sorted(call_ids)))
    statement = (
        select(developer_decisions)
        .where(*conditions)
        .order_by(
            developer_decisions.c.occurred_at,
            developer_decisions.c.decision_id,
        )
    )
    rows = _bounded_fact_rows(connection, statement, limit, "Developer decision")
    return [
        DeveloperDecision.model_validate({**row, "raw": json.loads(row["raw"])})
        for row in rows
    ]


def _read_edit_observations(
    connection: Connection,
    org_id: str,
    include_quarantined: bool,
    *,
    captured_through: datetime | None = None,
    session_ids: set[str] | None = None,
    call_ids: set[str] | None = None,
    limit: int | None = None,
) -> list[EditObservation]:
    _validate_bounded_fact_read(captured_through, limit, "Edit observation")
    if any(values is not None and not values for values in (session_ids, call_ids)):
        return []
    conditions = list(
        _fact_conditions(org_id, FactTable.EDIT_OBSERVATIONS, include_quarantined)
    )
    if captured_through is not None:
        conditions.append(edit_observations.c.captured_at <= captured_through)
    if session_ids is not None:
        conditions.append(edit_observations.c.session_id.in_(sorted(session_ids)))
    if call_ids is not None:
        conditions.append(edit_observations.c.call_id.in_(sorted(call_ids)))
    statement = (
        select(edit_observations)
        .where(*conditions)
        .order_by(
            edit_observations.c.occurred_at,
            edit_observations.c.observation_id,
        )
    )
    rows = _bounded_fact_rows(connection, statement, limit, "Edit observation")
    return [
        EditObservation.model_validate(
            {
                **row,
                "applied_text": json.loads(row["applied_text"]),
                "observed_file_text": json.loads(row["observed_file_text"]),
                "raw": json.loads(row["raw"]),
            }
        )
        for row in rows
    ]


def _read_rejected_edits(
    connection: Connection, org_id: str, include_quarantined: bool
) -> list[RejectedEdit]:
    rows = connection.execute(
        select(rejected_edits)
        .where(*_fact_conditions(org_id, FactTable.REJECTED_EDITS, include_quarantined))
        .order_by(rejected_edits.c.occurred_at, rejected_edits.c.rejection_id)
    ).mappings()
    return [
        RejectedEdit.model_validate(
            {
                **row,
                "proposed": json.loads(row["proposed"]),
                "raw": json.loads(row["raw"]),
            }
        )
        for row in rows
    ]


def _read_retry_linkages(
    connection: Connection,
    org_id: str,
    include_quarantined: bool,
    *,
    captured_through: datetime | None = None,
    session_ids: set[str] | None = None,
    limit: int | None = None,
) -> list[RetryLinkage]:
    _validate_bounded_fact_read(captured_through, limit, "Retry linkage")
    if session_ids is not None and not session_ids:
        return []
    conditions = list(
        _fact_conditions(org_id, FactTable.RETRY_LINKAGES, include_quarantined)
    )
    if captured_through is not None:
        conditions.append(retry_linkages.c.captured_at <= captured_through)
    if session_ids is not None:
        conditions.append(retry_linkages.c.session_id.in_(sorted(session_ids)))
    statement = (
        select(retry_linkages)
        .where(*conditions)
        .order_by(retry_linkages.c.occurred_at, retry_linkages.c.retry_linkage_id)
    )
    rows = _bounded_fact_rows(connection, statement, limit, "Retry linkage")
    return [
        RetryLinkage.model_validate({**row, "raw": json.loads(row["raw"])})
        for row in rows
    ]


def _read_ci_outcome_by_run(
    connection,
    org_id,
    provider,
    run_id,
    *,
    run_attempt,
    repository_key=None,
    captured_through=None,
):
    _validate_bounded_fact_read(captured_through, None, "CI outcome")
    conditions = [
        *_fact_conditions(org_id, FactTable.CI_OUTCOMES, False),
        ci_outcomes.c.provider == provider.value,
        ci_outcomes.c.run_id == run_id,
        func.coalesce(ci_outcomes.c.run_attempt, 0)
        == (run_attempt if run_attempt is not None else 0),
    ]
    if repository_key is not None:
        conditions.append(_repository_selector_condition(ci_outcomes, repository_key))
    if captured_through is not None:
        conditions.append(ci_outcomes.c.captured_at <= captured_through)
    rows = (
        connection.execute(
            select(*_columns_except(ci_outcomes, "raw")).where(*conditions).limit(2)
        )
        .mappings()
        .all()
    )
    if len(rows) > 1:
        raise RepositoryReadAmbiguous("repository_selector_ambiguous")
    return _ci_outcome_projection(rows[0]) if rows else None


def _read_ci_outcome_summaries(
    connection,
    org_id,
    *,
    repo,
    repository_key,
    result,
    captured_between,
    limit,
    captured_through=None,
    before=None,
    workflow_name=None,
    pr_number=None,
):
    if not 1 <= limit <= 101:
        raise ValueError("limit must be between 1 and 101")
    if repo is not None and repository_key is not None:
        raise ValueError("literal and qualified repository filters cannot be combined")
    if repo is None and repository_key is None:
        raise ValueError("CI summaries require a repository selector")
    captured_after, captured_before = captured_between
    _validate_bounded_fact_read(captured_after, None, "CI outcome")
    _validate_bounded_fact_read(captured_before, None, "CI outcome")
    if captured_after >= captured_before:
        raise ValueError("captured_after must precede captured_before")
    conditions = [
        *_fact_conditions(org_id, FactTable.CI_OUTCOMES, False),
        _repository_selector_condition(ci_outcomes, repository_key)
        if repository_key is not None
        else ci_outcomes.c.repo == repo,
        ci_outcomes.c.result == result.value,
        ci_outcomes.c.captured_at >= captured_after,
        ci_outcomes.c.captured_at < captured_before,
    ]
    _validate_bounded_fact_read(captured_through, None, "CI outcome")
    if captured_through is not None:
        conditions.append(ci_outcomes.c.captured_at <= captured_through)
    if before is not None:
        _validate_bounded_fact_read(before[0], None, "CI outcome")
        conditions.append(
            tuple_(ci_outcomes.c.captured_at, ci_outcomes.c.outcome_id) < before
        )
    if workflow_name is not None:
        conditions.append(ci_outcomes.c.workflow_name == workflow_name)
    if pr_number is not None:
        conditions.append(ci_outcomes.c.pr_number == pr_number)
    rows = (
        connection.execute(
            select(*_columns_except(ci_outcomes, "raw"))
            .where(*conditions)
            .order_by(ci_outcomes.c.captured_at.desc(), ci_outcomes.c.outcome_id.desc())
            .limit(limit)
        )
        .mappings()
        .all()
    )
    return [_ci_outcome_projection(row) for row in rows]


def _read_ci_outcomes(
    connection: Connection,
    org_id: str,
    include_quarantined: bool,
    *,
    outcome_ids: set[str] | None = None,
    captured_through: datetime | None = None,
    commit_sha: CommitSha | None = None,
    repo_commits: set[tuple[str, str]] | None = None,
    repository_commits: set[RepositoryCommitReadKey] | None = None,
    limit: int | None = None,
) -> list[CIOutcome]:
    _validate_bounded_fact_read(captured_through, limit, "CI outcome")
    repository_condition = _repository_composite_condition(
        ci_outcomes,
        ci_outcomes.c.commit_sha,
        literal_keys=repo_commits,
        qualified_keys=repository_commits,
        pr=False,
        extra_bindings=len(outcome_ids or ()) + (commit_sha is not None),
    )
    conditions = list(
        _fact_conditions(org_id, FactTable.CI_OUTCOMES, include_quarantined)
    )
    if outcome_ids is not None:
        conditions.append(ci_outcomes.c.outcome_id.in_(sorted(outcome_ids)))
    if captured_through is not None:
        conditions.append(ci_outcomes.c.captured_at <= captured_through)
    if commit_sha is not None:
        conditions.append(
            ci_outcomes.c.commit_sha == _COMMIT_SHA.validate_python(commit_sha)
        )
    if repository_condition is not None:
        conditions.append(repository_condition)
    statement = (
        select(ci_outcomes)
        .where(*conditions)
        .order_by(ci_outcomes.c.captured_at, ci_outcomes.c.outcome_id)
    )
    rows = _bounded_fact_rows(connection, statement, limit, "CI outcome")
    return [
        CIOutcome.model_validate({**row, "raw": json.loads(row["raw"])}) for row in rows
    ]


def _repository_read_bounds(captured_through, limit):
    if captured_through is not None and captured_through.utcoffset() is None:
        raise ValueError("captured_through must be timezone-aware")
    if type(limit) is not int or not 1 <= limit <= REPOSITORY_IDENTITY_LIMIT:
        raise ValueError(
            f"repository evidence limit must be 1..{REPOSITORY_IDENTITY_LIMIT}"
        )


def _read_repository_renames(
    connection, org_id, *, captured_through, limit, include_quarantined
):
    limit = REPOSITORY_IDENTITY_LIMIT if limit is None else limit
    _repository_read_bounds(captured_through, limit)
    conditions = list(
        _fact_conditions(org_id, FactTable.REPOSITORY_RENAMES, include_quarantined)
    )
    if captured_through is not None:
        conditions.append(repository_renames.c.captured_at <= captured_through)
    rows = _bounded_fact_rows(
        connection,
        select(repository_renames)
        .where(*conditions)
        .order_by(
            repository_renames.c.captured_at,
            repository_renames.c.rename_id,
        ),
        limit,
        "Repository rename",
    )
    return [RepositoryRename.model_validate(row) for row in rows]


def _read_repository_identities(connection, org_id, *, captured_through, limit):
    if captured_through is None:
        raise ValueError("repository identities require a capture boundary")
    _repository_read_bounds(captured_through, limit)
    result = []
    for fact_table in (
        FactTable.PUSHES,
        FactTable.CI_OUTCOMES,
        FactTable.SESSION_COMMIT_OBSERVATIONS,
        FactTable.PULL_REQUEST_MERGES,
        FactTable.PULL_REQUEST_REVISIONS,
    ):
        table = _FACT_TABLES[fact_table]
        roles = (
            ("repo", "head_repo")
            if fact_table
            in (FactTable.PULL_REQUEST_MERGES, FactTable.PULL_REQUEST_REVISIONS)
            else ("repo",)
        )
        for role in roles:
            prefix = "head_repository" if role == "head_repo" else "repository"
            columns = [
                table.c.org_id,
                _FACT_PRIMARY_KEYS[fact_table].label("source_fact_id"),
                table.c[role].label("repo"),
                table.c.captured_at,
            ]
            columns.extend(
                table.c[f"{prefix}_{field}"].label(f"repository_{field}")
                for field in ("provider", "host", "id")
            )
            if fact_table is FactTable.SESSION_COMMIT_OBSERVATIONS:
                columns.append(table.c.source_push_id)
            rows = connection.execute(
                select(*columns)
                .where(
                    *_fact_conditions(org_id, fact_table, False),
                    table.c.captured_at <= captured_through,
                )
                .order_by(table.c.captured_at, _FACT_PRIMARY_KEYS[fact_table])
                .limit(limit + 1 - len(result))
            ).mappings()
            for row in rows:
                values = dict(row)
                values["repository_provider"] = _coerce_repository_provider(
                    values["repository_provider"]
                )
                result.append(
                    RepositoryIdentityEvidence(
                        source_table=fact_table, role=role, **values
                    )
                )
                if len(result) > limit:
                    raise OperationalReportLimitExceeded(
                        f"Repository identity population exceeds {limit}"
                    )
    return sorted(
        result,
        key=lambda item: (
            item.captured_at.astimezone(UTC),
            item.source_table,
            item.source_fact_id,
            item.role,
        ),
    )


def _read_session_commit_observations(
    connection: Connection,
    org_id: str,
    include_quarantined: bool,
    *,
    as_of: datetime | None = None,
    commit_sha: CommitSha | None = None,
    repo_commits: set[tuple[str, str]] | None = None,
    repository_commits: set[RepositoryCommitReadKey] | None = None,
    session_ids: set[str] | None = None,
    limit: int | None = None,
) -> list[SessionCommitObservation]:
    if as_of is not None and as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    if limit is not None and (type(limit) is not int or limit <= 0):
        raise ValueError("Session commit observation limit must be positive")
    repository_condition = _repository_composite_condition(
        session_commit_observations,
        session_commit_observations.c.commit_sha,
        literal_keys=repo_commits,
        qualified_keys=repository_commits,
        pr=False,
        extra_bindings=len(session_ids or ()) + (commit_sha is not None),
    )
    conditions = list(
        _fact_conditions(
            org_id, FactTable.SESSION_COMMIT_OBSERVATIONS, include_quarantined
        )
    )
    if as_of is not None:
        conditions.append(session_commit_observations.c.captured_at <= as_of)
    if commit_sha is not None:
        conditions.append(
            session_commit_observations.c.commit_sha
            == _COMMIT_SHA.validate_python(commit_sha)
        )
    if repository_condition is not None:
        conditions.append(repository_condition)
    if session_ids is not None:
        conditions.append(
            session_commit_observations.c.session_id.in_(sorted(session_ids))
        )
    statement = (
        select(session_commit_observations)
        .where(*conditions)
        .order_by(
            session_commit_observations.c.captured_at,
            session_commit_observations.c.repo,
            session_commit_observations.c.commit_sha,
            session_commit_observations.c.session_id,
            session_commit_observations.c.observation_id,
        )
    )
    if limit is not None:
        statement = statement.limit(limit + 1)
    rows = connection.execute(statement).mappings().all()
    if limit is not None and len(rows) > limit:
        raise OperationalReportLimitExceeded(
            f"Session commit observation cohort exceeds {limit}"
        )
    return [SessionCommitObservation.model_validate(row) for row in rows]


def _session_dossier_coverage(
    connection: Connection, org_id: str, session_id: str
) -> dict[str, SessionFactCoverage]:
    coverage: dict[str, SessionFactCoverage] = {}
    for fact_table in _SESSION_FACT_TABLES:
        table = _FACT_TABLES[fact_table]
        total = int(
            connection.execute(
                select(func.count())
                .select_from(table)
                .where(
                    table.c.org_id == org_id,
                    table.c.session_id == session_id,
                )
            ).scalar_one()
        )
        visible = int(
            connection.execute(
                select(func.count())
                .select_from(table)
                .where(
                    *_fact_conditions(org_id, fact_table, False),
                    table.c.session_id == session_id,
                )
            ).scalar_one()
        )
        coverage[fact_table.value] = SessionFactCoverage(
            total=total,
            visible=visible,
            quarantined=total - visible,
        )
    return coverage


def _session_dossier_timeline(
    connection: Connection, org_id: str, session_id: str
) -> list[SessionTimelineProjection]:
    calls = connection.execute(
        select(
            inference_calls.c.inference_call_id,
            inference_calls.c.observed_at,
            inference_calls.c.gateway_provider,
            inference_calls.c.model_provider,
            inference_calls.c.model,
            inference_calls.c.input_tokens,
            inference_calls.c.output_tokens,
            inference_calls.c.duration_ms,
            inference_calls.c.model_call_id,
        ).where(
            *_fact_conditions(org_id, FactTable.INFERENCE_CALLS, False),
            inference_calls.c.session_id == session_id,
        )
    ).mappings()
    decisions = connection.execute(
        select(
            developer_decisions.c.decision_id,
            developer_decisions.c.occurred_at,
            developer_decisions.c.agent_harness,
            developer_decisions.c.accepted,
            developer_decisions.c.explicit,
            developer_decisions.c.interaction_mode,
            (developer_decisions.c.file_path != "").label("has_file_path"),
            developer_decisions.c.call_id,
            developer_decisions.c.commit_sha,
        ).where(
            *_fact_conditions(org_id, FactTable.DEVELOPER_DECISIONS, False),
            developer_decisions.c.session_id == session_id,
        )
    ).mappings()
    observations = connection.execute(
        select(
            edit_observations.c.observation_id,
            edit_observations.c.occurred_at,
            edit_observations.c.agent_harness,
            (edit_observations.c.file_path != "").label("has_file_path"),
            edit_observations.c.call_id,
            edit_observations.c.external_lines_added.is_not(None).label(
                "external_lines_added_present"
            ),
            edit_observations.c.external_lines_removed.is_not(None).label(
                "external_lines_removed_present"
            ),
            edit_observations.c.external_lines_added,
            edit_observations.c.external_lines_removed,
        ).where(
            *_fact_conditions(org_id, FactTable.EDIT_OBSERVATIONS, False),
            edit_observations.c.session_id == session_id,
        )
    ).mappings()
    rejections = connection.execute(
        select(
            rejected_edits.c.rejection_id,
            rejected_edits.c.occurred_at,
            rejected_edits.c.agent_harness,
            (rejected_edits.c.file_path != "").label("has_file_path"),
            rejected_edits.c.call_id,
        ).where(
            *_fact_conditions(org_id, FactTable.REJECTED_EDITS, False),
            rejected_edits.c.session_id == session_id,
        )
    ).mappings()
    retries = connection.execute(
        select(
            retry_linkages.c.retry_linkage_id,
            retry_linkages.c.occurred_at,
            retry_linkages.c.agent_harness,
            (retry_linkages.c.file_path != "").label("has_file_path"),
            retry_linkages.c.tool_name,
            retry_linkages.c.rejected_call_id,
            retry_linkages.c.accepted_call_id,
        ).where(
            *_fact_conditions(org_id, FactTable.RETRY_LINKAGES, False),
            retry_linkages.c.session_id == session_id,
        )
    ).mappings()
    timeline = [
        SessionTimelineProjection(
            event_type="inference_call",
            fact_id=row["inference_call_id"],
            occurred_at=row["observed_at"],
            gateway_provider=GatewayProvider(row["gateway_provider"]),
            model_provider=row["model_provider"],
            model=row["model"],
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            duration_ms=row["duration_ms"],
            model_call_id=row["model_call_id"],
        )
        for row in calls
    ]
    timeline.extend(
        SessionTimelineProjection(
            event_type="developer_decision",
            fact_id=row["decision_id"],
            occurred_at=row["occurred_at"],
            agent_harness=AgentHarness(row["agent_harness"]),
            accepted=row["accepted"],
            explicit=row["explicit"],
            interaction_mode=InteractionMode(row["interaction_mode"]),
            has_file_path=row["has_file_path"],
            call_id=row["call_id"],
            commit_sha=row["commit_sha"],
        )
        for row in decisions
    )
    timeline.extend(
        SessionTimelineProjection(
            event_type="edit_observation",
            fact_id=row["observation_id"],
            occurred_at=row["occurred_at"],
            agent_harness=AgentHarness(row["agent_harness"]),
            has_file_path=row["has_file_path"],
            call_id=row["call_id"],
            external_lines_added_present=row["external_lines_added_present"],
            external_lines_removed_present=row["external_lines_removed_present"],
            external_lines_added=row["external_lines_added"],
            external_lines_removed=row["external_lines_removed"],
        )
        for row in observations
    )
    timeline.extend(
        SessionTimelineProjection(
            event_type="rejected_edit",
            fact_id=row["rejection_id"],
            occurred_at=row["occurred_at"],
            agent_harness=AgentHarness(row["agent_harness"]),
            has_file_path=row["has_file_path"],
            call_id=row["call_id"],
        )
        for row in rejections
    )
    timeline.extend(
        SessionTimelineProjection(
            event_type="retry_linkage",
            fact_id=row["retry_linkage_id"],
            occurred_at=row["occurred_at"],
            agent_harness=AgentHarness(row["agent_harness"]),
            has_file_path=row["has_file_path"],
            tool_name=row["tool_name"],
            rejected_call_id=row["rejected_call_id"],
            accepted_call_id=row["accepted_call_id"],
        )
        for row in retries
    )
    return timeline


def _coerce_repository_provider(value: str | None) -> ForgeProvider | None:
    return ForgeProvider(value) if value is not None else None


def _ci_outcome_projection(row: Mapping[str, Any]) -> CIOutcomeProjection:
    return CIOutcomeProjection(
        **{
            **row,
            "provider": CIProvider(row["provider"]),
            "result": CIResult(row["result"]),
            "repository_provider": _coerce_repository_provider(
                row["repository_provider"]
            ),
        }
    )


def _read_pushes(
    connection: Connection,
    org_id: str,
    include_quarantined: bool,
    *,
    captured_between: tuple[datetime, datetime] | None = None,
    repository_commits: set[RepositoryCommitReadKey] | None = None,
    captured_through: datetime | None = None,
    limit: int | None = None,
) -> list[Push]:
    if limit is not None and (type(limit) is not int or limit <= 0):
        raise ValueError("Push cohort limit must be positive")
    _validate_bounded_fact_read(captured_through, limit, "Push")
    repository_condition = _repository_composite_condition(
        pushes,
        pushes.c.after_sha,
        literal_keys=None,
        qualified_keys=repository_commits,
    )
    conditions = list(_fact_conditions(org_id, FactTable.PUSHES, include_quarantined))
    if repository_condition is not None:
        conditions.append(repository_condition)
    if captured_through is not None:
        conditions.append(pushes.c.captured_at <= captured_through)
    if captured_between is not None:
        lower, upper = captured_between
        if lower.tzinfo is None or upper.tzinfo is None:
            raise ValueError("captured_between bounds must be timezone-aware")
        if lower >= upper:
            raise ValueError("captured_between lower bound must precede upper bound")
        conditions.extend([pushes.c.captured_at >= lower, pushes.c.captured_at < upper])
    statement = (
        select(pushes)
        .where(*conditions)
        .order_by(pushes.c.captured_at, pushes.c.push_id)
    )
    if limit is not None:
        statement = statement.limit(limit + 1)
    rows = connection.execute(statement).mappings().all()
    if limit is not None and len(rows) > limit:
        raise OperationalReportLimitExceeded(f"Push cohort exceeds {limit}")
    return [Push.model_validate(row) for row in rows]


def _read_pull_request_merges(
    connection: Connection,
    org_id: str,
    include_quarantined: bool,
    *,
    captured_through: datetime | None = None,
    merged_through: datetime | None = None,
    repo_prs: set[tuple[str, int]] | None = None,
    repository_prs: set[RepositoryPRReadKey] | None = None,
    limit: int | None = None,
) -> list[PullRequestMerge]:
    _validate_bounded_fact_read(captured_through, limit, "Pull request merge")
    if merged_through is not None and merged_through.tzinfo is None:
        raise ValueError("merged_through must be timezone-aware")
    repository_condition = _repository_composite_condition(
        pull_request_merges,
        pull_request_merges.c.pr_number,
        literal_keys=repo_prs,
        qualified_keys=repository_prs,
        pr=True,
        extra_bindings=0,
    )
    conditions = list(
        _fact_conditions(org_id, FactTable.PULL_REQUEST_MERGES, include_quarantined)
    )
    if captured_through is not None:
        conditions.append(pull_request_merges.c.captured_at <= captured_through)
    if merged_through is not None:
        conditions.append(pull_request_merges.c.merged_at <= merged_through)
    if repository_condition is not None:
        conditions.append(repository_condition)
    statement = (
        select(pull_request_merges)
        .where(*conditions)
        .order_by(
            pull_request_merges.c.merged_at,
            pull_request_merges.c.repo,
            pull_request_merges.c.pr_number,
            pull_request_merges.c.merge_id,
        )
    )
    rows = _bounded_fact_rows(connection, statement, limit, "Pull request merge")
    return [PullRequestMerge.model_validate(row) for row in rows]


def _read_pull_request_revisions(
    connection: Connection,
    org_id: str,
    include_quarantined: bool,
    *,
    captured_through: datetime | None = None,
    repo_prs: set[tuple[str, int]] | None = None,
    repository_prs: set[RepositoryPRReadKey] | None = None,
    limit: int | None = None,
) -> list[PullRequestRevision]:
    _validate_bounded_fact_read(captured_through, limit, "Pull request revision")
    repository_condition = _repository_composite_condition(
        pull_request_revisions,
        pull_request_revisions.c.pr_number,
        literal_keys=repo_prs,
        qualified_keys=repository_prs,
        pr=True,
        extra_bindings=0,
    )
    conditions = list(
        _fact_conditions(org_id, FactTable.PULL_REQUEST_REVISIONS, include_quarantined)
    )
    if captured_through is not None:
        conditions.append(pull_request_revisions.c.captured_at <= captured_through)
    if repository_condition is not None:
        conditions.append(repository_condition)
    statement = (
        select(pull_request_revisions)
        .where(*conditions)
        .order_by(
            pull_request_revisions.c.repo,
            pull_request_revisions.c.pr_number,
            pull_request_revisions.c.head_sha,
            pull_request_revisions.c.base_sha,
            pull_request_revisions.c.revision_id,
        )
    )
    rows = _bounded_fact_rows(connection, statement, limit, "Pull request revision")
    return [PullRequestRevision.model_validate(row) for row in rows]


def _quarantine_revision(connection: Connection, org_id: str) -> int:
    statement = select(
        func.coalesce(func.max(fact_quarantine.c.quarantine_revision), 0)
    ).where(fact_quarantine.c.org_id == org_id)
    return int(connection.execute(statement).scalar_one())


def _serialize(value: object) -> str:
    """Serialize opaque canonical content without PostgreSQL interpretation."""
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=True,
        separators=(",", ":"),
    )


def _fact_conditions(
    org_id: str, fact_table: FactTable, include_quarantined: bool
) -> tuple[object, ...]:
    table = _FACT_TABLES[fact_table]
    if include_quarantined:
        return (table.c.org_id == org_id,)
    latest_action = (
        select(fact_quarantine.c.action)
        .where(
            fact_quarantine.c.org_id == table.c.org_id,
            fact_quarantine.c.fact_table == fact_table.value,
            fact_quarantine.c.fact_id == _FACT_PRIMARY_KEYS[fact_table],
        )
        .order_by(fact_quarantine.c.quarantine_revision.desc())
        .limit(1)
        .correlate(table)
        .scalar_subquery()
    )
    return (
        table.c.org_id == org_id,
        latest_action.is_distinct_from(QuarantineAction.QUARANTINE.value),
    )


def _decision_values(decision: DeveloperDecision) -> dict[str, object]:
    values = decision.model_dump(exclude={"raw"}, mode="python")
    values["raw"] = _serialize(decision.raw)
    return values


def _decision_row_key(values: Mapping[str, object]) -> tuple[object, ...]:
    """Match a PostgreSQL RETURNING row without materializing raw audit data."""
    key = []
    for column in _DECISION_RETURN_COLUMNS:
        value = values[column.name]
        # An ambiguous local datetime can compare unequal to its UTC instant.
        key.append(value.astimezone(UTC) if isinstance(value, datetime) else value)
    return tuple(key)


def _session_metadata(
    decisions,
) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str], dict[str, object]] = {}
    users: dict[tuple[str, str], set[str]] = {}
    for decision in decisions:
        key = (decision.org_id, decision.session_id)
        captured_at = decision.captured_at.astimezone(UTC)
        if key not in grouped:
            grouped[key] = {
                "org_id": decision.org_id,
                "session_id": decision.session_id,
                "first_observed_at": captured_at,
                "last_observed_at": captured_at,
            }
            users[key] = set()
        grouped[key]["first_observed_at"] = min(
            grouped[key]["first_observed_at"], captured_at
        )
        grouped[key]["last_observed_at"] = max(
            grouped[key]["last_observed_at"], captured_at
        )
        if decision.user_id is not None:
            users[key].add(decision.user_id)
    out = []
    for key in sorted(grouped):
        identities = users[key]
        out.append(
            {
                **grouped[key],
                "user_id": next(iter(identities)) if len(identities) == 1 else None,
                "user_id_conflict": len(identities) > 1,
            }
        )
    return out


def _session_upsert(
    fact: (
        InferenceCall
        | DeveloperDecision
        | EditObservation
        | RejectedEdit
        | RetryLinkage
        | SessionCommitObservation
    ),
    observed_at: datetime,
):
    return _session_upsert_values(
        {
            "org_id": fact.org_id,
            "session_id": fact.session_id,
            "user_id": getattr(fact, "user_id", None),
            "user_id_conflict": False,
            "first_observed_at": observed_at,
            "last_observed_at": observed_at,
        }
    )


def _session_upsert_values(values: dict[str, object]):
    statement = insert(sessions).values(**values)
    excluded = statement.excluded
    conflict = and_(
        sessions.c.user_id.is_not(None),
        excluded.user_id.is_not(None),
        sessions.c.user_id != excluded.user_id,
    )
    return statement.on_conflict_do_update(
        index_elements=[sessions.c.org_id, sessions.c.session_id],
        set_={
            "first_observed_at": func.least(
                sessions.c.first_observed_at, excluded.first_observed_at
            ),
            "last_observed_at": func.greatest(
                sessions.c.last_observed_at, excluded.last_observed_at
            ),
            "user_id": case(
                (sessions.c.user_id_conflict.is_(True), null()),
                (excluded.user_id_conflict.is_(True), null()),
                (conflict, null()),
                else_=func.coalesce(sessions.c.user_id, excluded.user_id),
            ),
            "user_id_conflict": case(
                (sessions.c.user_id_conflict.is_(True), True),
                (excluded.user_id_conflict.is_(True), True),
                (conflict, True),
                else_=False,
            ),
        },
    )
