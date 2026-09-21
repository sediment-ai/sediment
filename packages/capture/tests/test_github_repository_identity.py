# SPDX-License-Identifier: AGPL-3.0-or-later
"""Signed-payload repository roles never borrow names or URL identity."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import sediment_capture as capture

FIXTURES = Path(__file__).parent / "fixtures"
PARSERS = ("push", "workflow_run", "pull_request_merge", "pull_request_revision")


def payload_for(kind):
    name = "pull_request" if kind.startswith("pull_request") else kind
    payload = json.loads((FIXTURES / f"github_{name}.json").read_text())
    if kind == "pull_request_revision":
        payload["action"] = "opened"
    payload["repository"]["id"] = 186853002
    if kind.startswith("pull_request"):
        payload["pull_request"]["head"]["repo"]["id"] = 186853003
    return payload


def parse(kind, payload, **kwargs):
    result = getattr(capture, f"parse_{kind}")(payload, org_id="identity", **kwargs)
    return result[0] if kind == "pull_request_revision" else result


@pytest.mark.parametrize("kind", PARSERS)
@pytest.mark.parametrize("repository_id", [1, "1", 99999999999999999999])
def test_provider_id_is_captured_in_its_trusted_namespace(kind, repository_id):
    payload = payload_for(kind)
    payload["repository"]["id"] = repository_id
    payload["repository"]["clone_url"] = "https://untrusted.example/x/y.git"
    fact = parse(kind, payload, github_host="GITHUB.EXAMPLE.TEST")
    assert fact is not None
    assert fact.schema_version == 2
    assert fact.repository_provider.value == "github"
    assert fact.repository_host == "github.example.test"
    assert fact.repository_id == str(repository_id)
    if kind.startswith("pull_request"):
        assert fact.head_repository_id == "186853003"
        assert fact.head_repository_host == "github.example.test"
    if kind == "workflow_run":
        assert fact.provider.value == "github_actions"


@pytest.mark.parametrize("kind", PARSERS)
@pytest.mark.parametrize(
    "repository_id",
    [
        None,
        True,
        False,
        0,
        -1,
        1.0,
        "",
        " 1",
        "1 ",
        "01",
        "+1",
        "1e2",
        "١",
        "1" * 21,
        10**20,
        "1\0",
        "\ud800",
        {},
        [],
    ],
)
def test_invalid_optional_identity_keeps_otherwise_valid_fact(
    kind, repository_id, caplog
):
    payload = payload_for(kind)
    payload["repository"]["id"] = repository_id
    fact = parse(kind, payload)
    assert fact is not None
    assert (
        fact.repository_provider is fact.repository_host is fact.repository_id is None
    )
    losses = [r for r in caplog.records if r.message == "repository_identity_declined"]
    assert len(losses) == 1
    assert losses[0].role == "repo"
    assert losses[0].count == 1
    assert losses[0].reason == (
        "repository_identity_absent"
        if repository_id is None
        else "repository_identity_invalid"
    )
    if kind.startswith("pull_request"):
        assert fact.head_repository_id == "186853003"


@pytest.mark.parametrize("kind", ["pull_request_merge", "pull_request_revision"])
def test_invalid_head_identity_does_not_inherit_target(kind, caplog):
    payload = payload_for(kind)
    payload["pull_request"]["head"]["repo"]["id"] = "invalid-head-secret"
    fact = parse(kind, payload)
    assert fact.repository_id == "186853002"
    assert (
        fact.head_repository_provider
        is fact.head_repository_host
        is fact.head_repository_id
        is None
    )
    assert "invalid-head-secret" not in caplog.text
    [loss] = [r for r in caplog.records if r.message == "repository_identity_declined"]
    assert loss.role == "head_repo"


def test_workflow_repository_is_top_level_not_head_or_run_url():
    payload = payload_for("workflow_run")
    payload["workflow_run"]["head_repository"] = {"id": 777}
    payload["workflow_run"]["html_url"] = "https://other.test/repos/999/actions/runs/1"
    fact = parse("workflow_run", payload)
    assert fact.repository_id == "186853002"
    assert fact.repository_host == "github.com"


def rename_payload():
    return {
        "action": "renamed",
        "repository": {
            "id": 186853002,
            "full_name": "Acme/New",
            "updated_at": "2026-09-12T12:00:00Z",
        },
        "changes": {"repository": {"name": {"from": "Old"}}},
    }


def test_rename_preserves_provider_evidence_without_occurrence_guess():
    fact, reason = capture.parse_repository_rename(
        rename_payload(),
        org_id="identity",
        source_event_id="delivery-1",
        github_host="github.example.test",
    )
    assert reason is None
    assert fact.old_repo == "acme/old"
    assert fact.new_repo == "acme/new"
    assert fact.repository_provider.value == "github"
    assert fact.repository_host == "github.example.test"
    assert fact.repository_id == "186853002"
    assert fact.source_event_id == "delivery-1"
    assert fact.occurred_at is None
    assert "raw" not in fact.model_dump()
    assert fact.captured_at.utcoffset() is not None


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("missing_id", "repository_identity_absent"),
        ("invalid_id", "repository_identity_invalid"),
        ("missing_old", "invalid_repository_rename_boundary"),
        ("invalid_old", "invalid_repository_rename_boundary"),
        ("same_name", "repository_name_unchanged"),
        ("malformed_action", "malformed_discriminator"),
        ("transfer", "unsupported_discriminator"),
    ],
)
def test_rename_declines_with_closed_reason(mutation, reason, caplog):
    payload = rename_payload()
    if mutation == "missing_id":
        payload["repository"].pop("id")
    elif mutation == "invalid_id":
        payload["repository"]["id"] = False
    elif mutation == "missing_old":
        payload["changes"] = None
    elif mutation == "invalid_old":
        payload["changes"]["repository"]["name"]["from"] = "../secret"
    elif mutation == "same_name":
        payload["changes"]["repository"]["name"]["from"] = "NEW"
    elif mutation == "malformed_action":
        payload["action"] = {"sensitive": "action-data"}
    else:
        payload["action"] = "transferred"
    fact, loss = capture.parse_repository_rename(payload, org_id="identity")
    assert fact is None
    assert loss.value == reason
    assert "action-data" not in caplog.text


def test_rename_without_delivery_id_does_not_invent_one():
    fact, reason = capture.parse_repository_rename(rename_payload(), org_id="identity")
    assert reason is None
    assert fact.source_event_id is None
