# SPDX-License-Identifier: AGPL-3.0-or-later
"""
``sediment demo`` plants a synthetic session so the quickstart ends on a
captured fact.  Its payloads are built inline rather than read from a
fixture, so the risk this file exists to cover is silent drift from the
wire contract: a payload that still POSTs cleanly but no longer produces
the facts the demo claims it does.

Every assertion below therefore runs the demo's own payloads through the
*real* ingest machinery — the route's request model, capture's adapter,
and capture's OTLP translator — never a restatement of their shapes.
"""

from __future__ import annotations

import pytest
from sediment_api.routers.gateway import GatewayIngestRequest
from sediment_capture import ADAPTERS, parse_otlp_decisions
from sediment_core import AgentHarness

from sediment_cli import cli


def _count(out: str, table: str) -> int:
    """The row's ``total`` from a rendered facts table, read by field rather
    than by column offset — the assertion is about the count, not the
    formatting."""
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == table:
            return int(parts[1])
    raise AssertionError(f"no {table} row in:\n{out}")


def test_inference_call_payload_satisfies_the_gateway_route_and_adapter() -> None:
    """The envelope validates under the route's own model (extra="forbid",
    so a stray key fails here), and the adapter turns it into an InferenceCall."""
    body = GatewayIngestRequest.model_validate(cli._demo_completion())

    inference_call = ADAPTERS[body.provider].normalize(
        body.payload,
        session_id=body.session_id,
        user_id=body.user_id,
        org_id="testorg",
    )

    assert inference_call.session_id == cli.DEMO_SESSION_ID
    assert inference_call.model_call_id == cli.DEMO_CALL_ID
    assert inference_call.output_messages[0].parts
    assert inference_call.input_messages


def test_decision_payload_translates_to_exactly_one_decision() -> None:
    decisions = parse_otlp_decisions(cli._demo_decision(), org_id="testorg")

    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.session_id == cli.DEMO_SESSION_ID
    assert decision.accepted is True
    assert decision.explicit is True
    # An unregistered agent would be skipped by the server with a log trail
    # and no fact — the demo would print zeros and claim success.
    assert decision.agent_harness == AgentHarness.CLAUDE_CODE


def test_the_two_facts_share_a_join_key() -> None:
    """The demo advertises one session that hangs together.  A completion
    and a decision that don't share ``call_id`` still store, still make the
    counts non-zero, and still fail to demonstrate the join."""
    body = GatewayIngestRequest.model_validate(cli._demo_completion())
    inference_call = ADAPTERS[body.provider].normalize(
        body.payload,
        session_id=body.session_id,
        user_id=body.user_id,
        org_id="testorg",
    )
    decision = parse_otlp_decisions(cli._demo_decision(), org_id="testorg")[0]

    assert inference_call.model_call_id == decision.call_id
    assert inference_call.session_id == decision.session_id


@pytest.mark.parametrize(
    "url,loopback",
    [
        ("http://127.0.0.1:8000", True),
        ("http://localhost:8000", True),
        ("http://[::1]:8000", True),
        ("https://sediment.example.com", False),
        ("http://10.0.0.4:8000", False),
        # The guard reads the host, not the string: a hostname that merely
        # contains "localhost" is somebody else's server.
        ("https://localhost.example.com", False),
    ],
)
def test_loopback_guard_classifies_urls(url: str, loopback: bool) -> None:
    assert cli._is_loopback(url) is loopback


def test_demo_refuses_a_remote_server_without_force(remote, capsys) -> None:
    """``remote`` points the seam at https://testserver — not loopback — so
    this is the real refusal path, not a mocked one."""
    assert cli.main(["demo"]) == 1
    err = capsys.readouterr().err
    assert "--force" in err
    # It must refuse *before* writing: nothing landed.
    assert cli.main(["facts"]) == 0
    assert _count(capsys.readouterr().out, "inference_calls") == 0


def test_demo_stores_both_facts_and_reports_them(remote, capsys) -> None:
    assert cli.main(["demo", "--force"]) == 0
    out = capsys.readouterr().out

    assert "synthetic" in out
    assert _count(out, "inference_calls") == 1
    assert _count(out, "developer_decisions") == 1
    assert _count(out, "sessions") == 1


def test_demo_is_idempotent(remote, capsys) -> None:
    """Both doors dedup on their natural keys, so a second run is safe —
    the reader who runs it twice must not see two of everything."""
    assert cli.main(["demo", "--force"]) == 0
    capsys.readouterr()
    assert cli.main(["demo", "--force"]) == 0
    out = capsys.readouterr().out

    assert _count(out, "inference_calls") == 1
    assert _count(out, "developer_decisions") == 1


def test_demo_fails_loudly_when_nothing_landed(remote, capsys, monkeypatch) -> None:
    """The OTLP door answers {} whether it stored the record or dropped it, so
    a silent drop would otherwise print a table of zeros under a success
    message and exit 0 — the one outcome a proof verb must not have."""
    from sediment_cli import client as api_client

    real = api_client.get_json

    def zeroed(path: str):
        data = real(path)
        if path == f"/v1/facts/session/{cli.DEMO_SESSION_ID}":
            data["tables"]["developer_decisions"] = {"total": 0, "visible": 0}
        return data

    monkeypatch.setattr(cli, "get_json", zeroed)

    assert cli.main(["demo", "--force"]) == 1
    captured = capsys.readouterr()
    assert "developer_decisions" in captured.err
    assert "stored nothing" in captured.err


def test_demo_does_not_mistake_an_unrelated_decision_for_its_own(
    remote, client, capsys, monkeypatch
) -> None:
    """A populated deployment must not mask a dropped demo decision."""
    from sediment_capture import otlp

    unrelated = cli._demo_decision()
    attributes = unrelated["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0][
        "attributes"
    ]
    for attribute in attributes:
        if attribute["key"] == "session.id":
            attribute["value"]["stringValue"] = "unrelated-session"
        elif attribute["key"] == "tool_use_id":
            attribute["value"]["stringValue"] = "unrelated-call"
    response = client.post(
        "/v1/logs",
        json=unrelated,
        headers={"Authorization": "Bearer test-operator-token-3a7e-2f6c"},
    )
    assert response.status_code == 200

    monkeypatch.setattr(
        otlp,
        "parse_otlp_decisions",
        lambda payload, *, org_id, _translated_records=None: [],
    )

    assert cli.main(["demo", "--force"]) == 1
    captured = capsys.readouterr()
    assert "developer_decisions" in captured.err
    assert "stored nothing" in captured.err
