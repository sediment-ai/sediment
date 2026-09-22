# SPDX-License-Identifier: AGPL-3.0-or-later
"""SQLAlchemy Core metadata for the PostgreSQL fact store."""

from __future__ import annotations

import json
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    and_,
    func,
    or_,
)
from sqlalchemy.dialects.postgresql import array

from sqlalchemy.types import TypeDecorator
from sqlalchemy.sql.expression import Grouping

from .models import (
    CIProvider,
    CIResult,
    AgentHarness,
    FactTable,
    ForgeProvider,
    GatewayProvider,
    InteractionMode,
    QuarantineAction,
)


class _SerializedText(TypeDecorator[str]):
    """Lossless descriptive strings at the FactStore SQL boundary (ADR 0015).

    SQL NULL stays NULL. Equality predicates use the same encoding as writes;
    complete Facts and projections both receive the original logical string.
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect) -> str | None:
        return None if value is None else json.dumps(value, ensure_ascii=True)

    def process_result_value(self, value: str | None, dialect) -> str | None:
        return None if value is None else json.loads(value)


metadata = MetaData()

_INT64_MAX = 2**63 - 1
_TRIM_CHARS = "E' \\t\\n\\r'"


def _enum_sql(column: str, enum: type[StrEnum]) -> str:
    values = ", ".join(f"'{member.value}'" for member in enum)
    return f"{column} IN ({values})"


def _non_empty_sql(column: str) -> str:
    return f"{column} <> '' AND {column} = btrim({column}, {_TRIM_CHARS})"


def _nullable_non_empty_sql(column: str) -> str:
    return f"{column} IS NULL OR ({_non_empty_sql(column)})"


def _org_sql() -> str:
    return "org_id ~ '^[a-z0-9][a-z0-9._-]{0,63}$'"


def _repo_sql(column: str = "repo") -> str:
    return (
        f"{column} = '' OR ({column} = lower({column}) AND {column} ~ '^[^/]+/[^/]+$')"
    )


def _required_repo_sql(column: str = "repo") -> str:
    return f"{column} <> '' AND {_repo_sql(column)}"


def _sha_sql(column: str) -> str:
    return f"{column} ~ '^(?:[0-9a-f]{{40}}|[0-9a-f]{{64}})$'"


def _repository_columns(table: str, prefix: str = "repository", *, required=False):
    provider, host, identifier = (
        f"{prefix}_{part}" for part in ("provider", "host", "id")
    )
    host_pattern = (
        "^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$"
    )
    complete = (
        f"{provider} IS NOT NULL AND {host} IS NOT NULL AND {identifier} IS NOT NULL"
    )
    absent = f"{provider} IS NULL AND {host} IS NULL AND {identifier} IS NULL"
    return [
        Column(provider, Text, nullable=not required),
        Column(host, Text, nullable=not required),
        Column(identifier, Text, nullable=not required),
        CheckConstraint(
            f"({absent}) OR ({complete})", name=f"ck_{table}_{prefix}_complete"
        ),
        CheckConstraint(
            _enum_sql(provider, ForgeProvider), name=f"ck_{table}_{prefix}_provider"
        ),
        CheckConstraint(
            f"length({host}) <= 253 AND {host} ~ '{host_pattern}'",
            name=f"ck_{table}_{prefix}_host",
        ),
        CheckConstraint(
            f"{identifier} ~ '^[1-9][0-9]{{0,19}}$'", name=f"ck_{table}_{prefix}_id"
        ),
    ]


sessions = Table(
    "sessions",
    metadata,
    Column("org_id", Text, nullable=False),
    Column("session_id", Text, nullable=False),
    Column("user_id", Text),
    Column("user_id_conflict", Boolean, nullable=False, server_default="false"),
    Column("first_observed_at", DateTime(timezone=True), nullable=False),
    Column("last_observed_at", DateTime(timezone=True), nullable=False),
    PrimaryKeyConstraint("org_id", "session_id", name="pk_sessions"),
    CheckConstraint(_org_sql(), name="ck_sessions_org_id"),
    CheckConstraint(_non_empty_sql("session_id"), name="ck_sessions_session_id"),
    CheckConstraint(_nullable_non_empty_sql("user_id"), name="ck_sessions_user_id"),
    CheckConstraint(
        "first_observed_at <= last_observed_at",
        name="ck_sessions_observation_bounds",
    ),
)

inference_calls = Table(
    "inference_calls",
    metadata,
    Column("schema_version", BigInteger, nullable=False),
    Column("inference_call_id", Text, primary_key=True),
    Column("org_id", Text, nullable=False),
    Column("session_id", Text, nullable=False),
    Column("user_id", Text),
    Column("gateway_provider", Text, nullable=False),
    Column("model_provider", Text),
    Column("model", Text),
    Column("input_messages", Text, nullable=False),
    Column("output_messages", Text, nullable=False),
    Column("input_tokens", BigInteger),
    Column("output_tokens", BigInteger),
    Column("duration_ms", BigInteger),
    Column("model_call_id", Text),
    Column("call_alias_count", BigInteger, nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    Column("raw", Text, nullable=False),
    CheckConstraint("schema_version = 1", name="ck_inference_calls_schema_version"),
    CheckConstraint(
        "call_alias_count >= 0", name="ck_inference_calls_call_alias_count"
    ),
    CheckConstraint(
        _non_empty_sql("inference_call_id"),
        name="ck_inference_calls_inference_call_id",
    ),
    CheckConstraint(_org_sql(), name="ck_inference_calls_org_id"),
    CheckConstraint(_non_empty_sql("session_id"), name="ck_inference_calls_session_id"),
    CheckConstraint(
        _nullable_non_empty_sql("user_id"), name="ck_inference_calls_user_id"
    ),
    CheckConstraint(
        _enum_sql("gateway_provider", GatewayProvider),
        name="ck_inference_calls_gateway_provider",
    ),
    CheckConstraint(
        "(input_tokens IS NULL OR input_tokens >= 0) AND "
        "(output_tokens IS NULL OR output_tokens >= 0) AND "
        "(duration_ms IS NULL OR duration_ms >= 0)",
        name="ck_inference_calls_usage",
    ),
    CheckConstraint(
        _nullable_non_empty_sql("model_call_id"),
        name="ck_inference_calls_model_call_id",
    ),
)
Index(
    "uq_inference_calls_model_call",
    inference_calls.c.org_id,
    inference_calls.c.gateway_provider,
    inference_calls.c.model_call_id,
    unique=True,
    postgresql_where=inference_calls.c.model_call_id.is_not(None),
)
Index(
    "ix_inference_calls_org_time",
    inference_calls.c.org_id,
    inference_calls.c.observed_at,
)
Index(
    "ix_inference_calls_session_time",
    inference_calls.c.org_id,
    inference_calls.c.session_id,
    inference_calls.c.observed_at,
    inference_calls.c.inference_call_id,
)

# Physical copies of canonical identifiers, not independently captured Facts.
inference_call_aliases = Table(
    "inference_call_aliases",
    metadata,
    Column(
        "inference_call_id",
        Text,
        ForeignKey("inference_calls.inference_call_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("ordinal", BigInteger, nullable=False),
    Column("org_id", Text, nullable=False),
    Column("call_id", Text, nullable=False),
    PrimaryKeyConstraint(
        "inference_call_id", "ordinal", name="pk_inference_call_aliases"
    ),
    CheckConstraint("ordinal >= 0", name="ck_inference_call_aliases_ordinal"),
    CheckConstraint(_org_sql(), name="ck_inference_call_aliases_org_id"),
    CheckConstraint(
        _non_empty_sql("call_id"), name="ck_inference_call_aliases_call_id"
    ),
)
Index(
    "ix_inference_call_aliases_lookup",
    Grouping(
        array([inference_call_aliases.c.org_id, inference_call_aliases.c.call_id])
    ),
    postgresql_using="hash",
)

developer_decisions = Table(
    "developer_decisions",
    metadata,
    Column("decision_id", Text, primary_key=True),
    Column("org_id", Text, nullable=False),
    Column("session_id", Text, nullable=False),
    Column("user_id", Text),
    Column("agent_harness", Text, nullable=False),
    Column("file_path", Text, nullable=False),
    Column("accepted", Boolean, nullable=False),
    Column("explicit", Boolean, nullable=False),
    Column("interaction_mode", Text, nullable=False),
    Column("commit_sha", Text),
    Column("call_id", Text),
    Column("edit_retention_score", Float),
    Column("observation_delay_ms", BigInteger),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("captured_at", DateTime(timezone=True), nullable=False),
    Column("raw", Text, nullable=False),
    CheckConstraint(_non_empty_sql("decision_id"), name="ck_decisions_decision_id"),
    CheckConstraint(_org_sql(), name="ck_decisions_org_id"),
    CheckConstraint(_non_empty_sql("session_id"), name="ck_decisions_session_id"),
    CheckConstraint(_nullable_non_empty_sql("user_id"), name="ck_decisions_user_id"),
    CheckConstraint(
        _enum_sql("agent_harness", AgentHarness), name="ck_decisions_agent_harness"
    ),
    CheckConstraint(
        _enum_sql("interaction_mode", InteractionMode),
        name="ck_decisions_interaction_mode",
    ),
    CheckConstraint(
        f"commit_sha IS NULL OR ({_sha_sql('commit_sha')})",
        name="ck_decisions_commit_sha",
    ),
    CheckConstraint(_nullable_non_empty_sql("call_id"), name="ck_decisions_call_id"),
    CheckConstraint(
        "edit_retention_score IS NULL OR edit_retention_score BETWEEN 0.0 AND 1.0",
        name="ck_decisions_edit_retention_score",
    ),
    CheckConstraint(
        "observation_delay_ms IS NULL OR observation_delay_ms >= 0",
        name="ck_decisions_observation_delay_ms",
    ),
)
Index(
    "uq_decisions_keyed",
    developer_decisions.c.org_id,
    developer_decisions.c.agent_harness,
    developer_decisions.c.session_id,
    developer_decisions.c.accepted,
    developer_decisions.c.explicit,
    developer_decisions.c.interaction_mode,
    developer_decisions.c.occurred_at,
    developer_decisions.c.call_id,
    unique=True,
    postgresql_where=and_(
        developer_decisions.c.call_id.is_not(None),
        developer_decisions.c.agent_harness == AgentHarness.CLAUDE_CODE.value,
    ),
)
Index(
    "uq_decisions_natural",
    developer_decisions.c.org_id,
    developer_decisions.c.agent_harness,
    developer_decisions.c.session_id,
    developer_decisions.c.file_path,
    developer_decisions.c.accepted,
    developer_decisions.c.explicit,
    developer_decisions.c.interaction_mode,
    developer_decisions.c.occurred_at,
    func.coalesce(developer_decisions.c.call_id, ""),
    func.coalesce(developer_decisions.c.observation_delay_ms, -1),
    unique=True,
    postgresql_where=or_(
        developer_decisions.c.call_id.is_(None),
        developer_decisions.c.agent_harness != AgentHarness.CLAUDE_CODE.value,
    ),
)
Index(
    "ix_decisions_session_time",
    developer_decisions.c.org_id,
    developer_decisions.c.session_id,
    developer_decisions.c.occurred_at,
    developer_decisions.c.decision_id,
)

edit_observations = Table(
    "edit_observations",
    metadata,
    Column("observation_id", Text, primary_key=True),
    Column("org_id", Text, nullable=False),
    Column("session_id", Text, nullable=False),
    Column("user_id", Text),
    Column("agent_harness", Text, nullable=False),
    Column("file_path", Text, nullable=False),
    Column("call_id", Text, nullable=False),
    Column("applied_text", Text, nullable=False),
    Column("observed_file_text", Text, nullable=False),
    Column("external_lines_added", BigInteger),
    Column("external_lines_removed", BigInteger),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("captured_at", DateTime(timezone=True), nullable=False),
    Column("raw", Text, nullable=False),
    CheckConstraint(
        _non_empty_sql("observation_id"),
        name="ck_edit_observations_observation_id",
    ),
    CheckConstraint(_org_sql(), name="ck_edit_observations_org_id"),
    CheckConstraint(
        _non_empty_sql("session_id"), name="ck_edit_observations_session_id"
    ),
    CheckConstraint(
        _nullable_non_empty_sql("user_id"), name="ck_edit_observations_user_id"
    ),
    CheckConstraint(
        _enum_sql("agent_harness", AgentHarness),
        name="ck_edit_observations_agent_harness",
    ),
    CheckConstraint(_non_empty_sql("call_id"), name="ck_edit_observations_call_id"),
    CheckConstraint(
        "(external_lines_added IS NULL OR external_lines_added >= 0) AND "
        "(external_lines_removed IS NULL OR external_lines_removed >= 0)",
        name="ck_edit_observations_external_counts",
    ),
)
Index(
    "uq_edit_observations_call",
    edit_observations.c.org_id,
    edit_observations.c.agent_harness,
    edit_observations.c.session_id,
    edit_observations.c.call_id,
    unique=True,
)
Index(
    "ix_edit_observations_session_time",
    edit_observations.c.org_id,
    edit_observations.c.session_id,
    edit_observations.c.occurred_at,
    edit_observations.c.observation_id,
)

rejected_edits = Table(
    "rejected_edits",
    metadata,
    Column("rejection_id", Text, primary_key=True),
    Column("org_id", Text, nullable=False),
    Column("session_id", Text, nullable=False),
    Column("user_id", Text),
    Column("agent_harness", Text, nullable=False),
    Column("file_path", Text, nullable=False),
    Column("call_id", Text, nullable=False),
    Column("proposed", Text, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("captured_at", DateTime(timezone=True), nullable=False),
    Column("raw", Text, nullable=False),
    CheckConstraint(
        _non_empty_sql("rejection_id"), name="ck_rejected_edits_rejection_id"
    ),
    CheckConstraint(_org_sql(), name="ck_rejected_edits_org_id"),
    CheckConstraint(_non_empty_sql("session_id"), name="ck_rejected_edits_session_id"),
    CheckConstraint(
        _nullable_non_empty_sql("user_id"), name="ck_rejected_edits_user_id"
    ),
    CheckConstraint(
        _enum_sql("agent_harness", AgentHarness),
        name="ck_rejected_edits_agent_harness",
    ),
    CheckConstraint(_non_empty_sql("call_id"), name="ck_rejected_edits_call_id"),
)
Index(
    "uq_rejected_edits_call",
    rejected_edits.c.org_id,
    rejected_edits.c.agent_harness,
    rejected_edits.c.session_id,
    rejected_edits.c.call_id,
    unique=True,
)
Index(
    "ix_rejected_edits_session_time",
    rejected_edits.c.org_id,
    rejected_edits.c.session_id,
    rejected_edits.c.occurred_at,
    rejected_edits.c.rejection_id,
)

retry_linkages = Table(
    "retry_linkages",
    metadata,
    Column("retry_linkage_id", Text, primary_key=True),
    Column("org_id", Text, nullable=False),
    Column("session_id", Text, nullable=False),
    Column("user_id", Text),
    Column("agent_harness", Text, nullable=False),
    Column("file_path", Text, nullable=False),
    Column("tool_name", Text, nullable=False),
    Column("rejected_call_id", Text, nullable=False),
    Column("accepted_call_id", Text, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("captured_at", DateTime(timezone=True), nullable=False),
    Column("raw", Text, nullable=False),
    CheckConstraint(
        _non_empty_sql("retry_linkage_id"),
        name="ck_retry_linkages_retry_linkage_id",
    ),
    CheckConstraint(_org_sql(), name="ck_retry_linkages_org_id"),
    CheckConstraint(_non_empty_sql("session_id"), name="ck_retry_linkages_session_id"),
    CheckConstraint(
        _nullable_non_empty_sql("user_id"), name="ck_retry_linkages_user_id"
    ),
    CheckConstraint(
        _enum_sql("agent_harness", AgentHarness),
        name="ck_retry_linkages_agent_harness",
    ),
    CheckConstraint(
        _non_empty_sql("file_path"),
        name="ck_retry_linkages_file_path",
    ),
    CheckConstraint(
        "tool_name IN ('Edit', 'Write')",
        name="ck_retry_linkages_tool_name",
    ),
    CheckConstraint(
        _non_empty_sql("rejected_call_id"),
        name="ck_retry_linkages_rejected_call_id",
    ),
    CheckConstraint(
        _non_empty_sql("accepted_call_id"),
        name="ck_retry_linkages_accepted_call_id",
    ),
    CheckConstraint(
        "rejected_call_id <> accepted_call_id",
        name="ck_retry_linkages_distinct_calls",
    ),
)
Index(
    "uq_retry_linkages_calls",
    retry_linkages.c.org_id,
    retry_linkages.c.agent_harness,
    retry_linkages.c.session_id,
    retry_linkages.c.tool_name,
    retry_linkages.c.file_path,
    retry_linkages.c.rejected_call_id,
    retry_linkages.c.accepted_call_id,
    unique=True,
)
Index(
    "ix_retry_linkages_session_time",
    retry_linkages.c.org_id,
    retry_linkages.c.session_id,
    retry_linkages.c.occurred_at,
    retry_linkages.c.retry_linkage_id,
)

ci_outcomes = Table(
    "ci_outcomes",
    metadata,
    *_repository_columns("ci_outcomes"),
    Column("schema_version", BigInteger, nullable=False),
    Column("outcome_id", Text, primary_key=True),
    Column("org_id", Text, nullable=False),
    Column("provider", Text, nullable=False),
    Column("run_id", Text, nullable=False),
    Column("run_attempt", BigInteger),
    Column("repo", Text, nullable=False),
    Column("commit_sha", Text, nullable=False),
    Column("branch", Text, nullable=False),
    Column("result", Text, nullable=False),
    Column("workflow_name", _SerializedText, nullable=False),
    Column("workflow_id", Text),
    Column("workflow_path", Text),
    Column("run_url", _SerializedText),
    Column("provider_result", _SerializedText),
    Column("error_type", _SerializedText),
    Column("reason", _SerializedText),
    Column("source_event_type", _SerializedText),
    Column("source_spec_version", _SerializedText),
    Column("source_event_id", Text),
    Column("pr_number", BigInteger),
    Column("captured_at", DateTime(timezone=True), nullable=False),
    Column("raw", Text, nullable=False),
    CheckConstraint("schema_version IN (1, 2)", name="ck_ci_outcomes_schema_version"),
    CheckConstraint(_non_empty_sql("outcome_id"), name="ck_ci_outcomes_outcome_id"),
    CheckConstraint(_org_sql(), name="ck_ci_outcomes_org_id"),
    CheckConstraint(_enum_sql("provider", CIProvider), name="ck_ci_outcomes_provider"),
    CheckConstraint(_non_empty_sql("run_id"), name="ck_ci_outcomes_run_id"),
    CheckConstraint(
        f"run_attempt IS NULL OR run_attempt BETWEEN 1 AND {_INT64_MAX}",
        name="ck_ci_outcomes_run_attempt",
    ),
    CheckConstraint(_repo_sql(), name="ck_ci_outcomes_repo"),
    CheckConstraint(_sha_sql("commit_sha"), name="ck_ci_outcomes_commit_sha"),
    CheckConstraint(
        f"branch NOT LIKE 'refs/heads/%' AND branch = btrim(branch, {_TRIM_CHARS})",
        name="ck_ci_outcomes_branch",
    ),
    CheckConstraint(_enum_sql("result", CIResult), name="ck_ci_outcomes_result"),
    CheckConstraint(
        _nullable_non_empty_sql("workflow_id"), name="ck_ci_outcomes_workflow_id"
    ),
    CheckConstraint(
        _nullable_non_empty_sql("source_event_id"),
        name="ck_ci_outcomes_source_event_id",
    ),
    CheckConstraint(
        f"pr_number IS NULL OR pr_number BETWEEN 1 AND {_INT64_MAX}",
        name="ck_ci_outcomes_pr_number",
    ),
    CheckConstraint(
        "schema_version = 2 OR repository_id IS NULL",
        name="ck_ci_outcomes_repository_legacy",
    ),
)
Index(
    "uq_ci_run",
    ci_outcomes.c.org_id,
    ci_outcomes.c.provider,
    ci_outcomes.c.run_id,
    func.coalesce(ci_outcomes.c.run_attempt, 0),
    unique=True,
    postgresql_where=ci_outcomes.c.repository_id.is_(None),
)
Index(
    "uq_ci_run_identified",
    ci_outcomes.c.org_id,
    ci_outcomes.c.repository_provider,
    ci_outcomes.c.repository_host,
    ci_outcomes.c.provider,
    ci_outcomes.c.run_id,
    func.coalesce(ci_outcomes.c.run_attempt, 0),
    unique=True,
    postgresql_where=ci_outcomes.c.repository_id.is_not(None),
)
Index("ix_ci_org_commit", ci_outcomes.c.org_id, ci_outcomes.c.commit_sha)
Index(
    "ix_ci_failure_lookup",
    ci_outcomes.c.org_id,
    ci_outcomes.c.repo,
    ci_outcomes.c.result,
    ci_outcomes.c.captured_at,
    ci_outcomes.c.outcome_id,
)
Index(
    "ix_ci_repo_commit",
    ci_outcomes.c.org_id,
    ci_outcomes.c.repo,
    ci_outcomes.c.commit_sha,
    ci_outcomes.c.captured_at,
    ci_outcomes.c.outcome_id,
)

pushes = Table(
    "pushes",
    metadata,
    Column("schema_version", BigInteger, nullable=False),
    *_repository_columns("pushes"),
    Column("push_id", Text, primary_key=True),
    Column("org_id", Text, nullable=False),
    Column("provider", Text, nullable=False),
    Column("repo", Text, nullable=False),
    Column("clone_url", _SerializedText, nullable=False),
    Column("ref", Text, nullable=False),
    Column("before_sha", Text, nullable=False),
    Column("after_sha", Text, nullable=False),
    Column("forced", Boolean, nullable=False),
    Column("captured_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(_non_empty_sql("push_id"), name="ck_pushes_push_id"),
    CheckConstraint(_org_sql(), name="ck_pushes_org_id"),
    CheckConstraint(_enum_sql("provider", ForgeProvider), name="ck_pushes_provider"),
    CheckConstraint(_repo_sql(), name="ck_pushes_repo"),
    CheckConstraint(_non_empty_sql("ref"), name="ck_pushes_ref"),
    CheckConstraint(_sha_sql("before_sha"), name="ck_pushes_before_sha"),
    CheckConstraint(_sha_sql("after_sha"), name="ck_pushes_after_sha"),
    CheckConstraint("schema_version IN (1, 2)", name="ck_pushes_schema_version"),
    CheckConstraint(
        "schema_version = 2 OR repository_id IS NULL",
        name="ck_pushes_repository_legacy",
    ),
    CheckConstraint("repository_provider = provider", name="ck_pushes_forge_agreement"),
)
Index(
    "uq_pushes_natural",
    pushes.c.org_id,
    pushes.c.repo,
    pushes.c.ref,
    pushes.c.before_sha,
    pushes.c.after_sha,
    unique=True,
    postgresql_where=pushes.c.repository_id.is_(None),
)
Index(
    "uq_pushes_natural_identified",
    pushes.c.org_id,
    pushes.c.repository_provider,
    pushes.c.repository_host,
    pushes.c.repository_id,
    pushes.c.ref,
    pushes.c.before_sha,
    pushes.c.after_sha,
    unique=True,
    postgresql_where=pushes.c.repository_id.is_not(None),
)
Index(
    "ix_pushes_repo_after",
    pushes.c.org_id,
    pushes.c.repo,
    pushes.c.after_sha,
    pushes.c.captured_at,
    pushes.c.push_id,
)

session_commit_observations = Table(
    "session_commit_observations",
    metadata,
    *_repository_columns("session_commit_observations"),
    Column("schema_version", BigInteger, nullable=False),
    Column("observation_id", Text, primary_key=True),
    Column("org_id", Text, nullable=False),
    Column("repo", Text, nullable=False),
    Column("commit_sha", Text, nullable=False),
    Column("session_id", Text, nullable=False),
    Column("source_push_id", Text, nullable=False),
    Column("captured_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "schema_version IN (1, 2)", name="ck_session_commit_observations_schema_version"
    ),
    CheckConstraint(
        _non_empty_sql("observation_id"),
        name="ck_session_commit_observations_observation_id",
    ),
    CheckConstraint(_org_sql(), name="ck_session_commit_observations_org_id"),
    CheckConstraint(_required_repo_sql(), name="ck_session_commit_observations_repo"),
    CheckConstraint(
        _sha_sql("commit_sha"), name="ck_session_commit_observations_commit_sha"
    ),
    CheckConstraint(
        _non_empty_sql("session_id"),
        name="ck_session_commit_observations_session_id",
    ),
    CheckConstraint(
        _non_empty_sql("source_push_id"),
        name="ck_session_commit_observations_source_push_id",
    ),
    CheckConstraint(
        "schema_version = 2 OR repository_id IS NULL",
        name="ck_session_commit_observations_repository_legacy",
    ),
)
Index(
    "uq_session_commit_observations_edge",
    session_commit_observations.c.org_id,
    session_commit_observations.c.repo,
    session_commit_observations.c.commit_sha,
    session_commit_observations.c.session_id,
    unique=True,
    postgresql_where=session_commit_observations.c.repository_id.is_(None),
)
Index(
    "uq_session_commit_observations_edge_identified",
    session_commit_observations.c.org_id,
    session_commit_observations.c.repository_provider,
    session_commit_observations.c.repository_host,
    session_commit_observations.c.repository_id,
    session_commit_observations.c.commit_sha,
    session_commit_observations.c.session_id,
    unique=True,
    postgresql_where=session_commit_observations.c.repository_id.is_not(None),
)
Index(
    "ix_session_commit_observations_as_of",
    session_commit_observations.c.org_id,
    session_commit_observations.c.captured_at,
    session_commit_observations.c.observation_id,
)

pull_request_merges = Table(
    "pull_request_merges",
    metadata,
    Column("schema_version", BigInteger, nullable=False),
    *_repository_columns("pull_request_merges"),
    *_repository_columns("pull_request_merges", "head_repository"),
    Column("merge_id", Text, primary_key=True),
    Column("org_id", Text, nullable=False),
    Column("provider", Text, nullable=False),
    Column("repo", Text, nullable=False),
    Column("pr_number", BigInteger, nullable=False),
    Column("head_repo", Text, nullable=False),
    Column("head_ref", Text, nullable=False),
    Column("head_sha", Text, nullable=False),
    Column("base_ref", Text, nullable=False),
    Column("base_sha", Text, nullable=False),
    Column("merge_commit_sha", Text, nullable=False),
    Column("merged_at", DateTime(timezone=True), nullable=False),
    Column("source_event_id", Text),
    Column("captured_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(_non_empty_sql("merge_id"), name="ck_pr_merges_merge_id"),
    CheckConstraint(_org_sql(), name="ck_pr_merges_org_id"),
    CheckConstraint(_enum_sql("provider", ForgeProvider), name="ck_pr_merges_provider"),
    CheckConstraint(_required_repo_sql(), name="ck_pr_merges_repo"),
    CheckConstraint(
        f"pr_number BETWEEN 1 AND {_INT64_MAX}", name="ck_pr_merges_pr_number"
    ),
    CheckConstraint(_required_repo_sql("head_repo"), name="ck_pr_merges_head_repo"),
    CheckConstraint(
        f"head_ref <> '' AND head_ref NOT LIKE 'refs/heads/%' "
        f"AND head_ref = btrim(head_ref, {_TRIM_CHARS})",
        name="ck_pr_merges_head_ref",
    ),
    CheckConstraint(_sha_sql("head_sha"), name="ck_pr_merges_head_sha"),
    CheckConstraint(
        f"base_ref <> '' AND base_ref NOT LIKE 'refs/heads/%' "
        f"AND base_ref = btrim(base_ref, {_TRIM_CHARS})",
        name="ck_pr_merges_base_ref",
    ),
    CheckConstraint(_sha_sql("base_sha"), name="ck_pr_merges_base_sha"),
    CheckConstraint(_sha_sql("merge_commit_sha"), name="ck_pr_merges_merge_commit_sha"),
    CheckConstraint(
        _nullable_non_empty_sql("source_event_id"),
        name="ck_pr_merges_source_event_id",
    ),
    CheckConstraint(
        "schema_version IN (1, 2)", name="ck_pull_request_merges_schema_version"
    ),
    CheckConstraint(
        "schema_version = 2 OR head_repository_id IS NULL",
        name="ck_pull_request_merges_head_legacy",
    ),
    CheckConstraint(
        "head_repository_provider = provider",
        name="ck_pull_request_merges_head_provider",
    ),
    CheckConstraint(
        "schema_version = 2 OR repository_id IS NULL",
        name="ck_pull_request_merges_repository_legacy",
    ),
    CheckConstraint(
        "repository_provider = provider", name="ck_pull_request_merges_forge_agreement"
    ),
)
Index(
    "uq_pull_request_merges_natural",
    pull_request_merges.c.org_id,
    pull_request_merges.c.provider,
    pull_request_merges.c.repo,
    pull_request_merges.c.pr_number,
    unique=True,
    postgresql_where=pull_request_merges.c.repository_id.is_(None),
)
Index(
    "uq_pull_request_merges_natural_identified",
    pull_request_merges.c.org_id,
    pull_request_merges.c.repository_provider,
    pull_request_merges.c.repository_host,
    pull_request_merges.c.repository_id,
    pull_request_merges.c.pr_number,
    unique=True,
    postgresql_where=pull_request_merges.c.repository_id.is_not(None),
)
Index(
    "ix_pull_request_merges_org_repo",
    pull_request_merges.c.org_id,
    pull_request_merges.c.repo,
    pull_request_merges.c.merged_at,
)

pull_request_revisions = Table(
    "pull_request_revisions",
    metadata,
    Column("schema_version", BigInteger, nullable=False),
    *_repository_columns("pull_request_revisions"),
    *_repository_columns("pull_request_revisions", "head_repository"),
    Column("revision_id", Text, primary_key=True),
    Column("org_id", Text, nullable=False),
    Column("provider", Text, nullable=False),
    Column("repo", Text, nullable=False),
    Column("pr_number", BigInteger, nullable=False),
    Column("head_repo", Text, nullable=False),
    Column("head_ref", Text, nullable=False),
    Column("head_sha", Text, nullable=False),
    Column("base_ref", Text, nullable=False),
    Column("base_sha", Text, nullable=False),
    Column("previous_head_sha", Text),
    Column("source_event_id", Text),
    Column("captured_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(_non_empty_sql("revision_id"), name="ck_pr_revisions_revision_id"),
    CheckConstraint(_org_sql(), name="ck_pr_revisions_org_id"),
    CheckConstraint(
        _enum_sql("provider", ForgeProvider), name="ck_pr_revisions_provider"
    ),
    CheckConstraint(_required_repo_sql(), name="ck_pr_revisions_repo"),
    CheckConstraint(
        f"pr_number BETWEEN 1 AND {_INT64_MAX}", name="ck_pr_revisions_pr_number"
    ),
    CheckConstraint(_required_repo_sql("head_repo"), name="ck_pr_revisions_head_repo"),
    CheckConstraint(
        f"head_ref <> '' AND head_ref NOT LIKE 'refs/heads/%' "
        f"AND head_ref = btrim(head_ref, {_TRIM_CHARS})",
        name="ck_pr_revisions_head_ref",
    ),
    CheckConstraint(_sha_sql("head_sha"), name="ck_pr_revisions_head_sha"),
    CheckConstraint(
        f"base_ref <> '' AND base_ref NOT LIKE 'refs/heads/%' "
        f"AND base_ref = btrim(base_ref, {_TRIM_CHARS})",
        name="ck_pr_revisions_base_ref",
    ),
    CheckConstraint(_sha_sql("base_sha"), name="ck_pr_revisions_base_sha"),
    CheckConstraint(
        f"previous_head_sha IS NULL OR ({_sha_sql('previous_head_sha')})",
        name="ck_pr_revisions_previous_head_sha",
    ),
    CheckConstraint(
        _nullable_non_empty_sql("source_event_id"),
        name="ck_pr_revisions_source_event_id",
    ),
    CheckConstraint(
        "schema_version IN (1, 2)", name="ck_pull_request_revisions_schema_version"
    ),
    CheckConstraint(
        "schema_version = 2 OR head_repository_id IS NULL",
        name="ck_pull_request_revisions_head_legacy",
    ),
    CheckConstraint(
        "head_repository_provider = provider",
        name="ck_pull_request_revisions_head_provider",
    ),
    CheckConstraint(
        "schema_version = 2 OR repository_id IS NULL",
        name="ck_pull_request_revisions_repository_legacy",
    ),
    CheckConstraint(
        "repository_provider = provider",
        name="ck_pull_request_revisions_forge_agreement",
    ),
)
Index(
    "uq_pull_request_revisions_natural",
    pull_request_revisions.c.org_id,
    pull_request_revisions.c.provider,
    pull_request_revisions.c.repo,
    pull_request_revisions.c.pr_number,
    pull_request_revisions.c.head_sha,
    pull_request_revisions.c.base_sha,
    unique=True,
    postgresql_where=pull_request_revisions.c.repository_id.is_(None),
)
Index(
    "uq_pull_request_revisions_natural_identified",
    pull_request_revisions.c.org_id,
    pull_request_revisions.c.repository_provider,
    pull_request_revisions.c.repository_host,
    pull_request_revisions.c.repository_id,
    pull_request_revisions.c.pr_number,
    pull_request_revisions.c.head_sha,
    pull_request_revisions.c.base_sha,
    unique=True,
    postgresql_where=pull_request_revisions.c.repository_id.is_not(None),
)
Index(
    "ix_pull_request_revisions_org_repo_pr",
    pull_request_revisions.c.org_id,
    pull_request_revisions.c.repo,
    pull_request_revisions.c.pr_number,
)

repository_renames = Table(
    "repository_renames",
    metadata,
    Column("schema_version", BigInteger, nullable=False),
    Column("rename_id", Text, primary_key=True),
    Column("org_id", Text, nullable=False),
    *_repository_columns("repository_renames", required=True),
    Column("old_repo", Text, nullable=False),
    Column("new_repo", Text, nullable=False),
    Column("source_event_id", Text),
    Column("occurred_at", DateTime(timezone=True)),
    Column("captured_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("schema_version = 1", name="ck_repository_renames_schema_version"),
    CheckConstraint(
        _non_empty_sql("rename_id"), name="ck_repository_renames_rename_id"
    ),
    CheckConstraint(_org_sql(), name="ck_repository_renames_org_id"),
    CheckConstraint(
        _required_repo_sql("old_repo"), name="ck_repository_renames_old_repo"
    ),
    CheckConstraint(
        _required_repo_sql("new_repo"), name="ck_repository_renames_new_repo"
    ),
    CheckConstraint(
        "old_repo <> new_repo", name="ck_repository_renames_distinct_names"
    ),
    CheckConstraint(
        _nullable_non_empty_sql("source_event_id"),
        name="ck_repository_renames_source_event_id",
    ),
)
Index(
    "uq_repository_renames_delivery",
    repository_renames.c.org_id,
    repository_renames.c.repository_provider,
    repository_renames.c.repository_host,
    repository_renames.c.source_event_id,
    unique=True,
    postgresql_where=repository_renames.c.source_event_id.is_not(None),
)
Index(
    "ix_repository_renames_as_of",
    repository_renames.c.org_id,
    repository_renames.c.captured_at,
    repository_renames.c.rename_id,
)

fact_quarantine = Table(
    "fact_quarantine",
    metadata,
    Column(
        "quarantine_revision",
        BigInteger,
        Identity(always=True),
        primary_key=True,
    ),
    Column("quarantine_id", Text, nullable=False),
    Column("org_id", Text, nullable=False),
    Column("fact_table", Text, nullable=False),
    Column("fact_id", Text, nullable=False),
    Column("action", Text, nullable=False),
    Column("reason", _SerializedText, nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("quarantine_id", name="uq_quarantine_id"),
    CheckConstraint(
        _non_empty_sql("quarantine_id"), name="ck_quarantine_quarantine_id"
    ),
    CheckConstraint(_org_sql(), name="ck_quarantine_org_id"),
    CheckConstraint(_enum_sql("fact_table", FactTable), name="ck_quarantine_table"),
    CheckConstraint(_non_empty_sql("fact_id"), name="ck_quarantine_fact_id"),
    CheckConstraint(_enum_sql("action", QuarantineAction), name="ck_quarantine_action"),
)
Index(
    "ix_quarantine_fact",
    fact_quarantine.c.org_id,
    fact_quarantine.c.fact_table,
    fact_quarantine.c.fact_id,
    fact_quarantine.c.quarantine_revision,
)
