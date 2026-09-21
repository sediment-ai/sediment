# SPDX-License-Identifier: AGPL-3.0-or-later
"""ADR 0015: every physical column and Fact field has a representation contract."""

from datetime import UTC, datetime, timedelta
import inspect
import json
import math

import pytest
from pydantic import BaseModel, ValidationError
import sediment_core.models as models
from sediment_core import FactTable
from sediment_core.postgres_schema import metadata
from sqlalchemy import event, text

# Frozen review inventory. A field addition must declare its treatment here.
INVENTORY = {
    "sessions": {
        "Identity": ["org_id", "session_id", "user_id?"],
        "Boolean": ["user_id_conflict"],
        "Aware timestamp": ["first_observed_at", "last_observed_at"],
    },
    "inference_calls": {
        "Integer": [
            "schema_version",
            "input_tokens?",
            "output_tokens?",
            "duration_ms?",
        ],
        "Identity": [
            "inference_call_id",
            "org_id",
            "session_id",
            "user_id?",
            "model_provider?",
            "model?",
            "model_call_id?",
        ],
        "Closed vocabulary": ["gateway_provider"],
        "Opaque content": ["input_messages", "output_messages", "raw"],
        "Aware timestamp": ["observed_at"],
    },
    "developer_decisions": {
        "Identity": [
            "decision_id",
            "org_id",
            "session_id",
            "user_id?",
            "file_path",
            "commit_sha?",
            "call_id?",
        ],
        "Closed vocabulary": ["agent_harness", "interaction_mode"],
        "Boolean": ["accepted", "explicit"],
        "Bounded float": ["edit_retention_score?"],
        "Integer": ["observation_delay_ms?"],
        "Aware timestamp": ["occurred_at", "captured_at"],
        "Opaque content": ["raw"],
    },
    "edit_observations": {
        "Identity": [
            "observation_id",
            "org_id",
            "session_id",
            "user_id?",
            "file_path",
            "call_id",
        ],
        "Closed vocabulary": ["agent_harness"],
        "Opaque content": ["applied_text", "observed_file_text", "raw"],
        "Integer": ["external_lines_added?", "external_lines_removed?"],
        "Aware timestamp": ["occurred_at", "captured_at"],
    },
    "rejected_edits": {
        "Identity": [
            "rejection_id",
            "org_id",
            "session_id",
            "user_id?",
            "file_path",
            "call_id",
        ],
        "Closed vocabulary": ["agent_harness"],
        "Opaque content": ["proposed", "raw"],
        "Aware timestamp": ["occurred_at", "captured_at"],
    },
    "retry_linkages": {
        "Identity": [
            "retry_linkage_id",
            "org_id",
            "session_id",
            "user_id?",
            "file_path",
            "rejected_call_id",
            "accepted_call_id",
        ],
        "Closed vocabulary": ["agent_harness", "tool_name"],
        "Aware timestamp": ["occurred_at", "captured_at"],
        "Opaque content": ["raw"],
    },
    "ci_outcomes": {
        "Integer": ["schema_version", "run_attempt?", "pr_number?"],
        "Identity": [
            "repository_host?",
            "repository_id?",
            "outcome_id",
            "org_id",
            "run_id",
            "repo",
            "commit_sha",
            "branch",
            "workflow_id?",
            "workflow_path?",
            "source_event_id?",
        ],
        "Closed vocabulary": ["repository_provider?", "provider", "result"],
        "Descriptive content": [
            "workflow_name",
            "run_url?",
            "provider_result?",
            "error_type?",
            "reason?",
            "source_event_type?",
            "source_spec_version?",
        ],
        "Aware timestamp": ["captured_at"],
        "Opaque content": ["raw"],
    },
    "pushes": {
        "Integer": ["schema_version"],
        "Identity": [
            "repository_host?",
            "repository_id?",
            "push_id",
            "org_id",
            "repo",
            "ref",
            "before_sha",
            "after_sha",
        ],
        "Closed vocabulary": ["repository_provider?", "provider"],
        "Descriptive content": ["clone_url"],
        "Boolean": ["forced"],
        "Aware timestamp": ["captured_at"],
    },
    "session_commit_observations": {
        "Integer": ["schema_version"],
        "Identity": [
            "repository_host?",
            "repository_id?",
            "observation_id",
            "org_id",
            "repo",
            "commit_sha",
            "session_id",
            "source_push_id",
        ],
        "Closed vocabulary": ["repository_provider?"],
        "Aware timestamp": ["captured_at"],
    },
    "pull_request_merges": {
        "Identity": [
            "head_repository_host?",
            "head_repository_id?",
            "repository_host?",
            "repository_id?",
            "merge_id",
            "org_id",
            "repo",
            "head_repo",
            "head_ref",
            "head_sha",
            "base_ref",
            "base_sha",
            "merge_commit_sha",
            "source_event_id?",
        ],
        "Closed vocabulary": [
            "head_repository_provider?",
            "repository_provider?",
            "provider",
        ],
        "Integer": ["schema_version", "pr_number"],
        "Aware timestamp": ["merged_at", "captured_at"],
    },
    "pull_request_revisions": {
        "Identity": [
            "head_repository_host?",
            "head_repository_id?",
            "repository_host?",
            "repository_id?",
            "revision_id",
            "org_id",
            "repo",
            "head_repo",
            "head_ref",
            "head_sha",
            "base_ref",
            "base_sha",
            "previous_head_sha?",
            "source_event_id?",
        ],
        "Closed vocabulary": [
            "head_repository_provider?",
            "repository_provider?",
            "provider",
        ],
        "Integer": ["schema_version", "pr_number"],
        "Aware timestamp": ["captured_at"],
    },
    "repository_renames": {
        "Integer": ["schema_version"],
        "Identity": [
            "rename_id",
            "org_id",
            "repository_host",
            "repository_id",
            "old_repo",
            "new_repo",
            "source_event_id?",
        ],
        "Closed vocabulary": ["repository_provider"],
        "Aware timestamp": ["occurred_at?", "captured_at"],
    },
    "fact_quarantine": {
        "Integer": ["quarantine_revision"],
        "Identity": ["quarantine_id", "org_id", "fact_id"],
        "Closed vocabulary": ["fact_table", "action"],
        "Descriptive content": ["reason"],
        "Aware timestamp": ["recorded_at"],
    },
}

FACT_MODELS = {
    "inference_calls": models.InferenceCall,
    "developer_decisions": models.DeveloperDecision,
    "edit_observations": models.EditObservation,
    "rejected_edits": models.RejectedEdit,
    "retry_linkages": models.RetryLinkage,
    "ci_outcomes": models.CIOutcome,
    "pushes": models.Push,
    "session_commit_observations": models.SessionCommitObservation,
    "pull_request_merges": models.PullRequestMerge,
    "pull_request_revisions": models.PullRequestRevision,
    "fact_quarantine": models.QuarantineRecord,
    "repository_renames": models.RepositoryRename,
}
T0 = datetime(2026, 9, 6, tzinfo=UTC)


def fact(table, **changes):
    values = dict(
        org_id="acme",
        session_id="session-1",
        user_id="user-1",
        gateway_provider="litellm",
        model="model-1",
        model_provider="provider-1",
        input_messages=[],
        output_messages=[],
        agent_harness="claude-code",
        file_path="app.py",
        call_id="call-1",
        accepted=True,
        explicit=True,
        interaction_mode="agent",
        occurred_at=T0,
        captured_at=T0,
        observed_at=T0,
        recorded_at=T0,
        applied_text="applied",
        observed_file_text="observed",
        proposed="proposed",
        tool_name="Edit",
        rejected_call_id="rejected-1",
        accepted_call_id="accepted-1",
        provider="github_actions" if table == "ci_outcomes" else "github",
        run_id="run-1",
        repo="acme/service",
        commit_sha="a" * 40,
        branch="main",
        result="passed",
        clone_url="",
        ref="refs/heads/main",
        before_sha="b" * 40,
        after_sha="a" * 40,
        source_push_id="push-1",
        pr_number=1,
        head_repo="acme/service",
        head_ref="feature",
        head_sha="a" * 40,
        base_ref="main",
        base_sha="b" * 40,
        merge_commit_sha="c" * 40,
        merged_at=T0,
        fact_table="inference_calls",
        fact_id="call-1",
        action="quarantine",
        reason="review",
    )
    if table == "repository_renames":
        values.update(
            repository_provider="github",
            repository_host="github.com",
            repository_id="123",
            old_repo="acme/old",
            new_repo="acme/new",
        )
    values.update(changes)
    model = FACT_MODELS[table]
    return model.model_validate(
        {k: v for k, v in values.items() if k in model.model_fields}
    )


def test_representation_inventory_covers_every_column_and_fact_field():
    assert set(INVENTORY) == set(metadata.tables)
    part_models = {
        models.InferenceMessage,
        models.TextPart,
        models.ReasoningPart,
        models.ToolCallPart,
        models.ToolCallResponsePart,
    }
    part_fields = {
        models.InferenceMessage: {
            "role": "content",
            "parts": "nested",
            "finish_reason": "nullable content",
        },
        models.TextPart: {"type": "closed", "content": "content"},
        models.ReasoningPart: {"type": "closed", "content": "content"},
        models.ToolCallPart: {
            "type": "closed",
            "id": "identity",
            "name": "content",
            "arguments": "opaque",
        },
        models.ToolCallResponsePart: {
            "type": "closed",
            "id": "identity",
            "result": "opaque",
        },
    }
    assert part_models == set(part_fields)
    for model, fields in part_fields.items():
        assert set(fields) == set(model.model_fields)
    canonical_models = {
        value
        for name, value in inspect.getmembers(models, inspect.isclass)
        if issubclass(value, BaseModel)
        and value is not BaseModel
        and not name.startswith("_")
    }
    assert canonical_models == set(FACT_MODELS.values()) | part_models
    for table_name, treatments in INVENTORY.items():
        columns = [column for names in treatments.values() for column in names]
        assert len(columns) == len(set(columns))
        declared = {column.rstrip("?"): column.endswith("?") for column in columns}
        table = metadata.tables[table_name]
        assert declared == {column.name: column.nullable for column in table.columns}
        if table_name in FACT_MODELS:
            assert set(declared) - {"quarantine_revision"} == set(
                FACT_MODELS[table_name].model_fields
            )


IDENTITIES = [
    (table, name.rstrip("?"))
    for table, groups in INVENTORY.items()
    if table in FACT_MODELS
    for name in groups.get("Identity", [])
]


@pytest.mark.parametrize(("table", "field"), IDENTITIES)
@pytest.mark.parametrize("bad", ["\x00", "\ud800", "\udfff"])
def test_native_identities_reject_unrepresentable_characters(table, field, bad):
    original = fact(table).model_dump(mode="python")
    original[field] = (original.get(field) or "identity") + bad
    with pytest.raises(ValidationError):
        FACT_MODELS[table].model_validate(original)


@pytest.mark.parametrize("part", [models.ToolCallPart, models.ToolCallResponsePart])
@pytest.mark.parametrize("bad", ["\x00", "\ud800", "\udfff"])
def test_tool_call_identity_inside_opaque_content_is_validated(part, bad):
    with pytest.raises(ValidationError):
        part(id="tool" + bad, name="Edit", result=None)


INTEGER_FIELDS = [
    (table, field.rstrip("?"))
    for table, groups in INVENTORY.items()
    if table in FACT_MODELS
    for field in groups.get("Integer", [])
    if field not in {"schema_version", "quarantine_revision"}
]


@pytest.mark.parametrize(("table", "field"), INTEGER_FIELDS)
def test_bigint_bounds_match_canonical_validation(table, field):
    assert getattr(fact(table, **{field: 2**63 - 1}), field) == 2**63 - 1
    with pytest.raises(ValidationError):
        fact(table, **{field: 2**63})


@pytest.mark.parametrize(
    "value", ["", "nul\x00value", "surrogate\ud800value", "é" * 4096]
)
def test_descriptive_content_roundtrips_full_and_projected_reads(
    postgres_store, postgres_engine, value
):
    nonempty = value or "nonempty"
    outcome = fact(
        "ci_outcomes",
        workflow_name=value,
        reason=value,
        run_url=nonempty,
        provider_result=nonempty,
        error_type=nonempty,
        source_event_type=nonempty,
        source_spec_version=nonempty,
    )
    assert postgres_store.store_ci_outcome(outcome)
    assert postgres_store.read_ci_outcomes("acme") == [outcome]
    with postgres_store.read_snapshot() as snapshot:
        assert snapshot.read_ci_outcomes("acme") == [outcome]
    rows = postgres_store.read_ci_outcome_summaries(
        "acme",
        repo="acme/service",
        result=models.CIResult.PASSED,
        captured_between=(T0, T0 + timedelta(seconds=1)),
        limit=10,
        workflow_name=value,
    )
    assert len(rows) == 1
    assert rows[0].workflow_name == value
    assert rows[0].reason == value
    assert rows[0].run_url == nonempty
    assert (
        postgres_store.read_ci_outcome_by_run(
            "acme", models.CIProvider.GITHUB_ACTIONS, "run-1", run_attempt=None
        )
        == rows[0]
    )
    push = fact("pushes", clone_url=value)
    assert postgres_store.store_push(push)
    assert postgres_store.read_pushes("acme") == [push]
    postgres_store.quarantine_fact(
        "acme", FactTable.PUSHES, push.push_id, reason=nonempty
    )
    assert postgres_store.read_quarantine_log("acme")[0].reason == nonempty
    assert postgres_store.read_pushes("acme") == []
    with postgres_engine.connect() as connection:
        encoded = connection.execute(
            text("SELECT workflow_name, reason FROM ci_outcomes")
        ).one()
        assert all(item.isascii() for item in encoded)
        assert [json.loads(item) for item in encoded] == [value, value]


def test_compatibility_projection_decodes_opaque_content_without_raw(
    postgres_store, postgres_engine
):
    output = [
        models.InferenceMessage(
            role="assistant",
            parts=[
                models.TextPart(content="nul\x00and\ud800"),
                models.ToolCallPart(
                    id="tool-2", name="Edit", arguments={"value": float("nan")}
                ),
                models.ToolCallPart(
                    id="tool-1", name="Edit", arguments={"value": 10**500}
                ),
                models.ToolCallPart(id="tool-2", name="Edit"),
            ],
        )
    ]
    call = fact("inference_calls", output_messages=output, raw={"private": "unused"})
    assert postgres_store.store_inference_call(call)
    loaded = postgres_store.read_inference_calls("acme")[0]
    assert loaded.output_messages[0].parts[0].content == "nul\x00and\ud800"
    assert math.isnan(loaded.output_messages[0].parts[1].arguments["value"])
    statements = []

    def track(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement)

    event.listen(postgres_engine, "before_cursor_execute", track)
    try:
        rows = postgres_store.read_compatibility_inference_evidence("acme", "session-1")
    finally:
        event.remove(postgres_engine, "before_cursor_execute", track)
    assert [row.tool_call_ids for row in rows] == [("tool-1", "tool-2")]
    assert "inference_calls.raw" not in statements[0]
    assert "jsonb" not in statements[0].lower()
    with pytest.raises(ValueError, match="compatibility limit"):
        postgres_store.read_compatibility_inference_evidence(
            "acme", "session-1", limit=0
        )
    postgres_store.quarantine_fact(
        "acme", FactTable.INFERENCE_CALLS, call.inference_call_id, reason="test"
    )
    assert (
        postgres_store.read_compatibility_inference_evidence("acme", "session-1") == []
    )


def test_descriptive_encoding_migration_preserves_logical_facts(
    postgres_database_factory,
):
    from alembic import command
    from sqlalchemy import create_engine, MetaData, Table, select
    from sediment_core import FactStore
    from sediment_core.postgres_migrations import (
        _alembic_config,
        inspect_revision,
        RevisionState,
        upgrade_database,
    )

    url = postgres_database_factory(migrated=False)
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            config = _alembic_config()
            config.attributes["connection"] = connection
            command.upgrade(config, "0008_session_commit_observations")
        assert inspect_revision(url).state is RevisionState.BEHIND
        old_tables = MetaData()
        originals = []
        # No codec is present in reflected pre-migration metadata. Empty, NULL,
        # Unicode, escaped-looking content and maximum logical length stay distinct.
        for index, value in enumerate([None, "", 'é\\u0000"\n', "é" * 4096]):
            outcome = fact(
                "ci_outcomes",
                schema_version=1,
                outcome_id=f"old-ci-{index}",
                run_id=f"old-run-{index}",
                workflow_name=value or "",
                reason=value,
                run_url=value or None,
                provider_result=value or None,
                error_type=value or None,
                source_event_type=value or None,
                source_spec_version=value or None,
            )
            push = fact(
                "pushes",
                schema_version=1,
                push_id=f"old-push-{index}",
                clone_url=value or "",
                after_sha=str(index) * 40,
            )
            quarantine = fact(
                "fact_quarantine",
                quarantine_id=f"old-quarantine-{index}",
                fact_id=push.push_id,
                reason="  " + (value or "review") + "  ",
            )
            for table_name, record in [
                ("ci_outcomes", outcome),
                ("pushes", push),
                ("fact_quarantine", quarantine),
            ]:
                table = old_tables.tables.get(table_name)
                if table is None:
                    table = Table(table_name, old_tables, autoload_with=engine)
                # Freeze the pre-identity physical columns; no uncaptured identity enters migration.
                values = {
                    name: value
                    for name, value in record.model_dump(mode="python").items()
                    if name in table.c
                }
                if "raw" in values:
                    values["raw"] = json.dumps(values["raw"], ensure_ascii=True)
                with engine.begin() as connection:
                    connection.execute(table.insert().values(**values))
                originals.append((table_name, record))
        before = {}
        with engine.connect() as connection:
            for table in old_tables.tables.values():
                before[table.name] = [
                    dict(row) for row in connection.execute(select(table)).mappings()
                ]
        upgrade_database(url)
        upgrade_database(url)
        assert inspect_revision(url).state is RevisionState.AT_HEAD
        # Compare every physical column after decoding only the migrated fields.
        with engine.connect() as connection:
            for table in old_tables.tables.values():
                decoded = []
                content_columns = INVENTORY[table.name]["Descriptive content"]
                for row in connection.execute(select(table)).mappings():
                    values = dict(row)
                    for column in content_columns:
                        name = column.rstrip("?")
                        if values[name] is not None:
                            assert values[name].isascii()
                            values[name] = json.loads(values[name])
                    decoded.append(values)
                assert decoded == before[table.name]
        store = FactStore(engine)
        readers = {
            "ci_outcomes": store.read_ci_outcomes,
            "pushes": store.read_pushes,
            "fact_quarantine": store.read_quarantine_log,
        }
        for table_name, record in originals:
            assert record in readers[table_name]("acme")
        with engine.connect() as connection:
            config = _alembic_config()
            config.attributes["connection"] = connection
            with pytest.raises(RuntimeError, match="forward-only"):
                command.downgrade(config, "0008_session_commit_observations")
        assert inspect_revision(url).state is RevisionState.AT_HEAD
    finally:
        engine.dispose()


def test_descriptive_codec_preserves_redaction_before_serialization(postgres_store):
    from sediment_core import REDACTION_MARKER

    outcome = fact("ci_outcomes", reason="prefix\x00\ud800 Bearer abcdefghijklmnop")
    assert postgres_store.store_ci_outcome(outcome)
    assert postgres_store.read_ci_outcomes("acme")[0].reason == (
        "prefix\x00\ud800 Bearer " + REDACTION_MARKER
    )


def test_descriptive_validators_preserve_empty_null_and_trimming_contracts(
    postgres_store,
):
    outcome = fact(
        "ci_outcomes", workflow_name="  ", run_url=None, reason=None, workflow_path="  "
    )
    assert outcome.workflow_name == ""
    assert outcome.workflow_path == ""
    assert postgres_store.store_ci_outcome(outcome)
    assert postgres_store.read_ci_outcomes("acme") == [outcome]
    for name in [
        "run_url",
        "provider_result",
        "error_type",
        "source_event_type",
        "source_spec_version",
    ]:
        assert (
            getattr(fact("ci_outcomes", **{name: " padded\x00\ud800 "}), name)
            == "padded\x00\ud800"
        )
        with pytest.raises(ValidationError):
            fact("ci_outcomes", **{name: "  "})
    with pytest.raises(ValidationError):
        fact("ci_outcomes", reason="\ud800" * 4097)
    for table in ["developer_decisions", "edit_observations", "rejected_edits"]:
        assert fact(table, file_path="").file_path == ""
        assert fact(table, file_path=" padded ").file_path == " padded "
    assert fact("inference_calls", model_provider="").model_provider == ""
    assert (
        fact("inference_calls", model_provider=" padded ").model_provider == " padded "
    )
