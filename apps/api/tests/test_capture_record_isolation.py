# SPDX-License-Identifier: AGPL-3.0-or-later
"""Authenticated capture isolates source errors before the database write."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sediment_capture import sign_payload

from sediment_api.config import settings
from sediment_api.main import app

AUTH = {"Authorization": "Bearer test-operator-token-3a7e-2f6c"}


def _record(call_id, *, tool_name="Edit"):
    attributes = {
        "tool_name": tool_name,
        "decision": "accept",
        "source": "user_temporary",
        "session.id": f"session-{call_id}",
        "tool_use_id": call_id,
    }
    return {
        "timeUnixNano": "1783542925478460000",
        "body": {"stringValue": "claude_code.tool_decision"},
        "attributes": [
            {"key": key, "value": {"stringValue": value}}
            for key, value in attributes.items()
        ],
    }


def _payload(*records):
    return {"resourceLogs": [{"scopeLogs": [{"logRecords": list(records)}]}]}


def test_otlp_5000_decisions_commit_and_replay_with_exact_receipts(client, caplog):
    from sediment_api.deps import MAX_BODY_BYTES

    caplog.set_level("INFO", logger="sediment.api.otlp")
    records = [_record(f"large-{index}") for index in range(5_000)]
    body = json.dumps(_payload(*records), separators=(",", ":")).encode()
    assert len(body) < MAX_BODY_BYTES
    store = app.state.fact_store
    response = client.post(
        "/v1/logs",
        content=body,
        headers={**AUTH, "Content-Type": "application/json"},
    )
    assert response.status_code == 200
    assert response.json() == {}
    retained = store.read_decisions(settings.org_id)
    assert len(retained) == 5_000
    assert {decision.call_id for decision in retained} == {
        f"large-{index}" for index in range(5_000)
    }
    assert all(decision.accepted and decision.explicit for decision in retained)
    [receipt] = [
        record
        for record in caplog.records
        if record.message.startswith("otlp_logs_received")
    ]
    assert receipt.record_counts == {
        "received": 5_000,
        "translated": 5_000,
        "untranslated": 0,
        "malformed": 0,
    }
    assert receipt.fact_counts["developer_decisions"] == {
        "candidates": 5_000,
        "stored": 5_000,
        "duplicates": 0,
    }
    sessions = store.read_sessions(settings.org_id)
    assert len(sessions) == 5_000

    caplog.clear()
    replay = client.post(
        "/v1/logs",
        content=body,
        headers={**AUTH, "Content-Type": "application/json"},
    )
    assert replay.status_code == 200
    assert replay.json() == {}
    [duplicate] = [
        record
        for record in caplog.records
        if record.message.startswith("otlp_logs_received")
    ]
    assert duplicate.record_counts == receipt.record_counts
    assert duplicate.fact_counts["developer_decisions"] == {
        "candidates": 5_000,
        "stored": 0,
        "duplicates": 5_000,
    }
    assert store.read_decisions(settings.org_id) == retained
    assert store.read_sessions(settings.org_id) == sessions


def test_cursor_redelivery_preserves_first_fact_and_accounts_for_duplicate(
    client, caplog
):
    from copy import deepcopy

    caplog.set_level("INFO", logger="sediment.api.otlp")
    record = _record("cursor-write", tool_name="Write")
    record["body"] = {"stringValue": "sediment.tool_decision"}
    record["attributes"] += [
        {"key": "agent", "value": {"stringValue": "cursor"}},
        {"key": "explicit", "value": {"boolValue": False}},
        {"key": "file_path", "value": {"stringValue": "a.py"}},
    ]
    store = app.state.fact_store
    response = client.post("/v1/logs", json=_payload(record), headers=AUTH)
    assert response.status_code == 200
    [first] = store.read_decisions(settings.org_id)
    [receipt] = [
        r for r in caplog.records if r.message.startswith("otlp_logs_received")
    ]
    assert receipt.record_counts == {
        "received": 1,
        "translated": 1,
        "untranslated": 0,
        "malformed": 0,
    }
    assert receipt.fact_counts["developer_decisions"] == {
        "candidates": 1,
        "stored": 1,
        "duplicates": 0,
    }

    replay = deepcopy(record)
    replay["timeUnixNano"] = "1783542926478460000"
    caplog.clear()
    response = client.post("/v1/logs", json=_payload(replay), headers=AUTH)
    assert response.status_code == 200
    assert store.read_decisions(settings.org_id) == [first]
    [duplicate] = [
        r for r in caplog.records if r.message.startswith("otlp_logs_received")
    ]
    assert duplicate.record_counts == receipt.record_counts
    assert duplicate.fact_counts["developer_decisions"] == {
        "candidates": 1,
        "stored": 0,
        "duplicates": 1,
    }


def _accounting_payload():
    def record(body, **attributes):
        return {
            "timeUnixNano": "1783542925478460000",
            "body": {"stringValue": body},
            "attributes": [
                {"key": key, "value": {"stringValue": value}}
                for key, value in attributes.items()
            ],
        }

    identity = {
        "session.id": "accounting-session",
        "agent": "claude-code",
        "tool_use_id": "edit",
        "file_path": "a.py",
        "tool_name": "Edit",
    }
    codex = {
        "conversation.id": "codex-session",
        "call_id": "patch",
        "tool_name": "apply_patch",
    }
    return _payload(
        _record("accepted"),
        record(
            "",
            **codex,
            **{"event.name": "codex.tool_decision"},
            decision="approved",
            source="user",
        ),
        record(
            "",
            **codex,
            **{"event.name": "codex.tool_result"},
            arguments="*** Begin Patch\n*** Add File: a.py\n+one\n*** Add File: b.py\n+two\n*** End Patch",
        ),
        record(
            "sediment.edit_observation",
            **identity,
            applied_text="one",
            observed_file_text="one",
        ),
        record("sediment.rejected_edit", **identity, proposed="two"),
        record(
            "sediment.retry_linkage",
            **identity,
            rejected_call_id="rejected",
            accepted_call_id="edit",
        ),
        record("unsupported"),
        _record("declined", tool_name=[]),
        None,
    )


@pytest.mark.parametrize("extra_decisions", [0, 5_000])
def test_otlp_completed_receipt_partitions_records_and_all_fact_types(
    client, caplog, extra_decisions
):
    caplog.set_level("INFO", logger="sediment.api.otlp")
    payload = _accounting_payload()
    payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"].extend(
        _record(f"mixed-{index}") for index in range(extra_decisions)
    )
    response = client.post("/v1/logs", json=payload, headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {}
    [receipt] = [
        r for r in caplog.records if r.message.startswith("otlp_logs_received")
    ]
    assert getattr(receipt, "org_id", None) == settings.org_id
    assert receipt.record_counts == {
        "received": 9 + extra_decisions,
        "translated": 5 + extra_decisions,
        "untranslated": 3,
        "malformed": 1,
    }
    assert receipt.malformed_containers == 0
    assert receipt.fact_counts == {
        "developer_decisions": {
            "candidates": 3 + extra_decisions,
            "stored": 3 + extra_decisions,
            "duplicates": 0,
        },
        "edit_observations": {"candidates": 1, "stored": 1, "duplicates": 0},
        "rejected_edits": {"candidates": 1, "stored": 1, "duplicates": 0},
        "retry_linkages": {"candidates": 1, "stored": 1, "duplicates": 0},
    }
    store = app.state.fact_store
    assert len(store.read_decisions(settings.org_id)) == 3 + extra_decisions
    assert len(store.read_edit_observations(settings.org_id)) == 1
    assert len(store.read_rejected_edits(settings.org_id)) == 1
    assert len(store.read_retry_linkages(settings.org_id)) == 1
    before = (
        store.read_decisions(settings.org_id),
        store.read_edit_observations(settings.org_id),
        store.read_rejected_edits(settings.org_id),
        store.read_retry_linkages(settings.org_id),
    )
    # Operational fields must also survive the default message-only formatter.
    assert f"org_id={settings.org_id}" in receipt.message
    assert (
        f"record_counts={json.dumps(receipt.record_counts, sort_keys=True)}"
        in receipt.message
    )
    assert (
        f"fact_counts={json.dumps(receipt.fact_counts, sort_keys=True)}"
        in receipt.message
    )

    caplog.clear()
    replay = client.post("/v1/logs", json=payload, headers=AUTH)
    assert replay.status_code == 200
    assert replay.json() == {}
    [duplicate] = [
        r for r in caplog.records if r.message.startswith("otlp_logs_received")
    ]
    assert duplicate.record_counts == receipt.record_counts
    assert duplicate.fact_counts == {
        name: {
            "candidates": counts["candidates"],
            "stored": 0,
            "duplicates": counts["candidates"],
        }
        for name, counts in receipt.fact_counts.items()
    }
    assert (
        store.read_decisions(settings.org_id),
        store.read_edit_observations(settings.org_id),
        store.read_rejected_edits(settings.org_id),
        store.read_retry_linkages(settings.org_id),
    ) == before


@pytest.mark.parametrize(
    "payload,containers,records",
    [
        ({}, 0, {"received": 0, "translated": 0, "untranslated": 0, "malformed": 0}),
        (
            {"resourceLogs": None},
            1,
            {"received": 0, "translated": 0, "untranslated": 0, "malformed": 0},
        ),
        (
            _payload(None, {}, {"body": {"stringValue": "unsupported"}}),
            0,
            {"received": 3, "translated": 0, "untranslated": 2, "malformed": 1},
        ),
    ],
)
def test_otlp_empty_receipt_keeps_record_and_container_units(
    client, caplog, payload, containers, records
):
    caplog.set_level("INFO", logger="sediment.api.otlp")
    response = client.post("/v1/logs", json=payload, headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {}
    [receipt] = [
        r for r in caplog.records if r.message.startswith("otlp_logs_received")
    ]
    assert receipt.org_id == settings.org_id
    assert receipt.record_counts == records
    assert receipt.malformed_containers == containers
    assert receipt.fact_counts == {
        name: {"candidates": 0, "stored": 0, "duplicates": 0}
        for name in (
            "developer_decisions",
            "edit_observations",
            "rejected_edits",
            "retry_linkages",
        )
    }


def test_otlp_partial_storage_failure_has_no_completed_receipt_and_replay_converges(
    client, postgres_engine, caplog
):
    from copy import deepcopy

    caplog.set_level("INFO", logger="sediment.api.otlp")
    # Decisions commit in a batch; each observation commits separately. Force
    # the second observation to fail after both preceding transactions commit.
    good = _accounting_payload()["resourceLogs"][0]["scopeLogs"][0]["logRecords"][3]
    bad = deepcopy(good)
    for attribute in bad["attributes"]:
        if attribute["key"] == "tool_use_id":
            attribute["value"]["stringValue"] = "blocked-observation"
    payload = _payload(_record("first"), good, bad)
    with postgres_engine.begin() as connection:
        connection.exec_driver_sql(
            "ALTER TABLE edit_observations ADD CONSTRAINT reject_later_observation "
            "CHECK (call_id <> 'blocked-observation')"
        )
    response = client.post("/v1/logs", json=payload, headers=AUTH)
    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "database_operation_failed"
    assert not any(r.message.startswith("otlp_logs_received") for r in caplog.records)
    store = app.state.fact_store
    [decision] = store.read_decisions(settings.org_id)
    [observation] = store.read_edit_observations(settings.org_id)
    assert decision.call_id == "first"
    assert observation.call_id == "edit"
    with postgres_engine.begin() as connection:
        connection.exec_driver_sql(
            "ALTER TABLE edit_observations DROP CONSTRAINT reject_later_observation"
        )
    caplog.clear()
    replay = client.post("/v1/logs", json=payload, headers=AUTH)
    assert replay.status_code == 200
    assert replay.json() == {}
    [receipt] = [
        r for r in caplog.records if r.message.startswith("otlp_logs_received")
    ]
    assert receipt.record_counts == {
        "received": 3,
        "translated": 3,
        "untranslated": 0,
        "malformed": 0,
    }
    assert receipt.fact_counts == {
        "developer_decisions": {"candidates": 1, "stored": 0, "duplicates": 1},
        "edit_observations": {"candidates": 2, "stored": 1, "duplicates": 1},
        "rejected_edits": {"candidates": 0, "stored": 0, "duplicates": 0},
        "retry_linkages": {"candidates": 0, "stored": 0, "duplicates": 0},
    }
    assert store.read_decisions(settings.org_id) == [decision]
    observations = store.read_edit_observations(settings.org_id)
    assert len(observations) == 2
    assert observation in observations


@pytest.mark.parametrize("invalid", [None, 42, True, [], {}, "unsupported"])
def test_otlp_malformed_record_keeps_valid_sibling_and_receipt(client, invalid, caplog):
    payload = _payload(_record("bad", tool_name=invalid), _record("good"))
    caplog.set_level("INFO")

    response = client.post("/v1/logs", json=payload, headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {}
    store = app.state.fact_store
    decisions = store.read_decisions(settings.org_id)
    assert [decision.call_id for decision in decisions] == ["good"]
    assert [session.session_id for session in store.read_sessions(settings.org_id)] == [
        "session-good"
    ]
    assert any(
        getattr(record, "record_position", None) == 0
        and getattr(record, "reason", None)
        in {"malformed_discriminator", "unsupported_discriminator"}
        for record in caplog.records
    )
    assert client.post("/v1/logs", json=payload, headers=AUTH).status_code == 200
    assert store.read_decisions(settings.org_id) == decisions


def test_otlp_database_failure_rolls_back_valid_decisions_and_sessions(
    client, postgres_engine
):
    # A real constraint failure must reject the write and roll back the batch.
    with postgres_engine.begin() as connection:
        connection.exec_driver_sql(
            "ALTER TABLE developer_decisions ADD CONSTRAINT reject_second_call "
            "CHECK (call_id <> 'second')"
        )

    response = client.post(
        "/v1/logs", json=_payload(_record("first"), _record("second")), headers=AUTH
    )

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "database_operation_failed"
    store = app.state.fact_store
    assert store.read_decisions(settings.org_id) == []
    assert store.read_sessions(settings.org_id) == []

    with postgres_engine.begin() as connection:
        connection.exec_driver_sql(
            "ALTER TABLE developer_decisions DROP CONSTRAINT reject_second_call"
        )
    retry = client.post(
        "/v1/logs", json=_payload(_record("first"), _record("second")), headers=AUTH
    )
    assert retry.status_code == 200
    assert {decision.call_id for decision in store.read_decisions(settings.org_id)} == {
        "first",
        "second",
    }


@pytest.mark.parametrize("field", ["survival_rate_four_gram", "time_delay_ms"])
def test_otlp_oversized_retention_integer_preserves_facts_and_raw(client, field):
    attrs = {
        "event.name": "copilot_chat.edit.survival",
        "edit_source": "apply_patch",
        "survival_rate_no_revert": 1,
        "survival_rate_four_gram": 1,
        "time_delay_ms": 0,
        "copilot_chat.file.relative_path": "a.py",
        "request_id": "copilot-call",
    }
    attrs[field] = 10**400
    record = {
        "timeUnixNano": "1783542925478460000",
        "attributes": [
            {
                "key": key,
                "value": {
                    "intValue" if isinstance(value, int) else "stringValue": value
                },
            }
            for key, value in attrs.items()
        ],
    }
    payload = _payload(record, _record("good"))
    payload["resourceLogs"][0]["resource"] = {
        "attributes": [
            {"key": "session.id", "value": {"stringValue": "copilot-session"}}
        ]
    }

    response = client.post("/v1/logs", json=payload, headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {}
    decisions = app.state.fact_store.read_decisions(settings.org_id)
    by_call = {decision.call_id: decision for decision in decisions}
    assert set(by_call) == {"copilot-call", "good"}
    copilot = by_call["copilot-call"]
    assert copilot.accepted is True
    assert copilot.edit_retention_score == (
        None if field == "survival_rate_four_gram" else 1.0
    )
    assert copilot.observation_delay_ms is None
    assert copilot.raw == record
    assert client.post("/v1/logs", json=payload, headers=AUTH).status_code == 200
    assert app.state.fact_store.read_decisions(settings.org_id) == decisions


@pytest.mark.parametrize("invalid", [[], {}])
def test_gateway_malformed_part_keeps_typed_content_and_source(client, invalid):
    payload = {
        "litellm_call_id": "call",
        "messages": [],
        "response": {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": invalid},
                            {"type": "text", "text": "kept"},
                        ],
                    }
                }
            ]
        },
    }
    response = client.post(
        "/ingest/gateway",
        headers=AUTH,
        json={
            "provider": "litellm",
            "session_id": "session",
            "payload": payload,
        },
    )
    assert response.status_code == 200
    fact = app.state.fact_store.read_inference_calls(settings.org_id)[0]
    assert fact.output_messages[0].parts[0].content == "kept"
    assert len(fact.output_messages[0].parts) == 1
    assert fact.raw == payload


@pytest.mark.parametrize("invalid", [None, 42, True, [], {}, "unsupported"])
def test_github_malformed_action_returns_documented_skip(client, invalid):
    body = json.dumps({"action": invalid}).encode()
    response = client.post(
        "/ingest/github/pull-request",
        content=body,
        headers={
            "X-Hub-Signature-256": sign_payload(body, settings.github_webhook_secret),
            "X-GitHub-Event": "pull_request",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 200
    assert response.json() == {
        "skipped": True,
        "reason": "unsupported_pull_request_action",
    }


@pytest.mark.parametrize(
    "event,route,fixture",
    [
        ("push", "push", "github_push.json"),
        ("workflow_run", "ci", "github_workflow_run.json"),
    ],
)
@pytest.mark.parametrize("invalid", ["\x00", "\ud800"])
def test_github_invalid_identity_declines_whole_fact(
    client, event, route, fixture, invalid
):
    fixtures = Path(__file__).resolve().parents[3] / "packages/capture/tests/fixtures"
    payload = json.loads((fixtures / fixture).read_text())
    if event == "push":
        payload["ref"] = "refs/heads/" + invalid
    else:
        payload["workflow_run"]["id"] = invalid
    body = json.dumps(payload).encode()
    response = client.post(
        f"/ingest/github/{route}",
        content=body,
        headers={
            "X-Hub-Signature-256": sign_payload(body, settings.github_webhook_secret),
            "X-GitHub-Event": event,
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 200
    assert response.json()["skipped"] is True
    store = app.state.fact_store
    assert store.read_pushes(settings.org_id) == []
    assert store.read_ci_outcomes(settings.org_id) == []
