# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, DateTime, Text
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from sediment_core.models import (
    CIOutcome,
    DeveloperDecision,
    EditObservation,
    InferenceCall,
    PullRequestMerge,
    PullRequestRevision,
    Push,
    RepositoryRename,
    QuarantineRecord,
    RejectedEdit,
    RetryLinkage,
    SessionCommitObservation,
)
from sediment_core.postgres_schema import metadata


_FACT_MODELS = {
    "inference_calls": InferenceCall,
    "developer_decisions": DeveloperDecision,
    "edit_observations": EditObservation,
    "rejected_edits": RejectedEdit,
    "retry_linkages": RetryLinkage,
    "session_commit_observations": SessionCommitObservation,
    "ci_outcomes": CIOutcome,
    "pushes": Push,
    "pull_request_merges": PullRequestMerge,
    "pull_request_revisions": PullRequestRevision,
    "fact_quarantine": QuarantineRecord,
    "repository_renames": RepositoryRename,
}


def _postgres_sql(ddl: object) -> str:
    return str(ddl.compile(dialect=postgresql.dialect())).lower()


def test_postgres_metadata_defines_complete_fact_and_session_schema() -> None:
    assert set(metadata.tables) == {"sessions", *_FACT_MODELS}
    for table_name, model in _FACT_MODELS.items():
        columns = set(metadata.tables[table_name].columns.keys())
        if table_name == "fact_quarantine":
            columns.remove("quarantine_revision")
        assert columns == set(model.model_fields)

    assert set(metadata.tables["sessions"].columns.keys()) == {
        "org_id",
        "session_id",
        "user_id",
        "user_id_conflict",
        "first_observed_at",
        "last_observed_at",
    }


def test_postgres_metadata_uses_native_timestamps_booleans_and_identity() -> None:
    for table in metadata.tables.values():
        for column in table.columns:
            if column.name.endswith("_at"):
                assert isinstance(column.type, DateTime)
                assert column.type.timezone is True

    sessions = metadata.tables["sessions"]
    assert isinstance(sessions.c.user_id_conflict.type, Boolean)
    assert isinstance(metadata.tables["pushes"].c.forced.type, Boolean)

    revision = metadata.tables["fact_quarantine"].c.quarantine_revision
    assert isinstance(revision.type, BigInteger)
    assert revision.identity is not None
    assert revision.identity.always is True
    assert "generated always as identity" in _postgres_sql(
        CreateTable(metadata.tables["fact_quarantine"])
    )


def test_postgres_metadata_keeps_content_bearing_values_as_text() -> None:
    content_columns = {
        "inference_calls": {"input_messages", "output_messages", "raw"},
        "developer_decisions": {"raw"},
        "edit_observations": {
            "applied_text",
            "observed_file_text",
            "raw",
        },
        "rejected_edits": {"proposed", "raw"},
        "retry_linkages": {"raw"},
        "ci_outcomes": {"raw"},
    }
    for table_name, column_names in content_columns.items():
        table = metadata.tables[table_name]
        for column_name in column_names:
            assert isinstance(table.c[column_name].type, Text)


def test_postgres_metadata_preserves_natural_key_unique_indexes() -> None:
    expected = {
        "uq_inference_calls_model_call",
        "uq_decisions_keyed",
        "uq_decisions_natural",
        "uq_edit_observations_call",
        "uq_rejected_edits_call",
        "uq_retry_linkages_calls",
        "uq_ci_run",
        "uq_pushes_natural",
        "uq_pull_request_merges_natural",
        "uq_pull_request_revisions_natural",
        "uq_session_commit_observations_edge",
    }
    expected |= {
        "uq_ci_run_identified",
        "uq_pushes_natural_identified",
        "uq_pull_request_merges_natural_identified",
        "uq_pull_request_revisions_natural_identified",
        "uq_session_commit_observations_edge_identified",
        "uq_repository_renames_delivery",
    }
    indexes = {
        index.name: index
        for table in metadata.tables.values()
        for index in table.indexes
        if index.unique
    }
    assert set(indexes) == expected

    inference_sql = _postgres_sql(CreateIndex(indexes["uq_inference_calls_model_call"]))
    assert "where model_call_id is not null" in inference_sql

    keyed_sql = _postgres_sql(CreateIndex(indexes["uq_decisions_keyed"]))
    assert "where call_id is not null" in keyed_sql
    assert "agent_harness = 'claude-code'" in keyed_sql

    natural_sql = _postgres_sql(CreateIndex(indexes["uq_decisions_natural"]))
    assert "coalesce(call_id, '')" in natural_sql
    assert "coalesce(observation_delay_ms, -1)" in natural_sql

    ci_sql = _postgres_sql(CreateIndex(indexes["uq_ci_run"]))
    assert "coalesce(run_attempt, 0)" in ci_sql


def test_postgres_metadata_names_domain_check_constraints() -> None:
    constraint_names = {
        constraint.name
        for table in metadata.tables.values()
        for constraint in table.constraints
        if constraint.name is not None
    }
    assert {
        "ck_sessions_observation_bounds",
        "ck_inference_calls_schema_version",
        "ck_inference_calls_usage",
        "ck_decisions_edit_retention_score",
        "ck_edit_observations_external_counts",
        "ck_ci_outcomes_schema_version",
        "ck_ci_outcomes_run_attempt",
        "ck_ci_outcomes_pr_number",
    } <= constraint_names


def test_postgres_metadata_accepts_the_absent_branch_sentinel() -> None:
    constraint = next(
        constraint
        for constraint in metadata.tables["ci_outcomes"].constraints
        if constraint.name == "ck_ci_outcomes_branch"
    )
    sql = str(constraint.sqltext)
    assert "branch <> ''" not in sql
    assert "branch = btrim" in sql
