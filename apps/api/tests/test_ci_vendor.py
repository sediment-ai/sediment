# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Vendor-neutral CI ingest tests — real FactStore on a tmp DB, never
mocked (per AGENTS.md). Cover store, run-attempt dedup,
auth-reject, and malformed-payload rejection.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sediment_api.config import settings
from sediment_api.main import app
from sediment_core import REDACTION_MARKER


def _valid_payload(**over) -> dict:
    base: dict = {
        "provider": "jenkins",
        "run_id": "backend-42",
        "repo": "acme-corp/backend-service",
        "commit_sha": "a" * 40,
        "branch": "main",
        "workflow_name": "Build & Test",
        "result": "passed",
        "run_url": "https://jenkins.example.com/job/backend/42",
    }
    base.update(over)
    return base


AUTH = {"Authorization": "Bearer test-operator-token-3a7e-2f6c"}


def test_ci_vendor_store_and_facts_count(client: TestClient) -> None:
    """A valid POST stores a CIOutcome fact that sediment facts shows."""
    resp = client.post("/ingest/ci", json=_valid_payload(), headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["stored"] is True
    assert body["fact_id"] is not None


def test_ci_vendor_accepts_run_identity_without_run_url(client: TestClient) -> None:
    payload = _valid_payload()
    payload["run_id"] = "backend-42"
    del payload["run_url"]

    response = client.post("/ingest/ci", json=payload, headers=AUTH)

    assert response.status_code == 200
    stored = app.state.fact_store.read_ci_outcomes(settings.org_id)
    assert stored[-1].run_id == "backend-42"
    assert stored[-1].run_url is None


def test_ci_vendor_preserves_structured_provenance(client: TestClient) -> None:
    response = client.post(
        "/ingest/ci",
        json=_valid_payload(
            run_id="backend-43",
            run_attempt=2,
            workflow_id="pipeline-main",
            provider_result="SYSTEM_ERROR",
            result="error",
            error_type="runner_lost",
            reason="runner stopped responding",
            source_event_type="dev.cdevents.pipelinerun.finished.0.2.0",
            source_spec_version="0.5.0",
            source_event_id="event-43",
        ),
        headers=AUTH,
    )

    assert response.status_code == 200
    stored = app.state.fact_store.read_ci_outcomes(settings.org_id)[-1]
    assert stored.run_attempt == 2
    assert stored.workflow_id == "pipeline-main"
    assert stored.provider_result == "SYSTEM_ERROR"
    assert stored.result.value == "error"
    assert stored.error_type == "runner_lost"
    assert stored.reason == "runner stopped responding"
    assert stored.source_event_type == "dev.cdevents.pipelinerun.finished.0.2.0"
    assert stored.source_spec_version == "0.5.0"
    assert stored.source_event_id == "event-43"


def test_ci_vendor_dedup_by_run_attempt(client: TestClient) -> None:
    """The same provider run id and attempt collapses as a redelivery."""
    payload = _valid_payload()
    r1 = client.post("/ingest/ci", json=payload, headers=AUTH)
    assert r1.json()["stored"] is True

    r2 = client.post("/ingest/ci", json=payload, headers=AUTH)
    assert r2.status_code == 200
    assert r2.json()["stored"] is False


def test_ci_vendor_reason_redaction_overflow_does_not_500(client: TestClient) -> None:
    """A max-length reason that redaction lengthens past 4096 must store
    (HTTP 200), not surface a CHECK-violation 500, and redeliveries dedup."""
    reason = "a" * 4082 + '"api_key": "x"'
    assert len(reason) == 4096
    payload = _valid_payload(
        run_id="run-overflow",
        run_attempt=1,
        result="failed",
        reason=reason,
    )
    first = client.post("/ingest/ci", json=payload, headers=AUTH)
    assert first.status_code == 200
    assert first.json()["stored"] is True

    retry = client.post("/ingest/ci", json=payload, headers=AUTH)
    assert retry.status_code == 200
    assert retry.json()["stored"] is False

    stored = app.state.fact_store.read_ci_outcomes(settings.org_id)
    assert stored[-1].reason.endswith(REDACTION_MARKER)
    assert len(stored[-1].reason) <= 4096


def test_ci_vendor_auth_reject(client: TestClient) -> None:
    """Missing / invalid bearer → 401."""
    resp = client.post("/ingest/ci", json=_valid_payload())
    assert resp.status_code == 401


def test_ci_vendor_malformed_sha_rejected(client: TestClient) -> None:
    """Malformed commit_sha → 422, not 500."""
    resp = client.post(
        "/ingest/ci",
        json=_valid_payload(commit_sha="not-a-sha"),
        headers=AUTH,
    )
    assert resp.status_code == 422


def test_ci_vendor_short_sha_rejected(client: TestClient) -> None:
    resp = client.post(
        "/ingest/ci",
        json=_valid_payload(commit_sha="abc123"),
        headers=AUTH,
    )
    assert resp.status_code == 422


def test_ci_vendor_invalid_result_rejected(client: TestClient) -> None:
    """An unknown result value → 422 (pydantic enum validation)."""
    resp = client.post(
        "/ingest/ci",
        json=_valid_payload(result="aborted"),
        headers=AUTH,
    )
    assert resp.status_code == 422


def test_ci_vendor_rejects_invalid_provider(client: TestClient) -> None:
    """An unknown provider value → 422 (pydantic enum validation)."""
    resp = client.post(
        "/ingest/ci",
        json=_valid_payload(provider="travis_ci"),
        headers=AUTH,
    )
    assert resp.status_code == 422


def test_ci_vendor_other_provider_accepted(client: TestClient) -> None:
    """The 'other' catch-all provider is a valid enum value."""
    resp = client.post(
        "/ingest/ci",
        json=_valid_payload(provider="other"),
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert resp.json()["stored"] is True


def test_ci_vendor_extra_field_rejected(client: TestClient) -> None:
    """extra=\"forbid\" on the model: a client naming an org is rejected."""
    payload = _valid_payload()
    payload["org_id"] = "evil-corp"
    resp = client.post("/ingest/ci", json=payload, headers=AUTH)
    assert resp.status_code == 422


def test_ci_vendor_all_providers_accepted(client: TestClient) -> None:
    """Every CIProvider value is accepted."""
    providers = ["github_actions", "jenkins", "gitlab_ci", "circleci", "buildkite"]
    for provider in providers:
        resp = client.post(
            "/ingest/ci",
            json=_valid_payload(
                provider=provider, run_url=f"https://example.com/{provider}/1"
            ),
            headers=AUTH,
        )
        assert resp.status_code == 200, f"provider={provider} rejected"
        assert resp.json()["stored"] is True, f"provider={provider} not stored"


def test_ci_vendor_accepts_cancelled_result(client: TestClient) -> None:
    """CANCELLED is a valid CIResult."""
    resp = client.post(
        "/ingest/ci",
        json=_valid_payload(
            result="cancelled", run_url="https://example.com/cancelled/1"
        ),
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert resp.json()["stored"] is True


def test_ci_vendor_missing_or_empty_run_id_rejected(client: TestClient) -> None:
    """Provider run identity is required and can't be whitespace."""
    payload = _valid_payload()
    del payload["run_id"]
    assert client.post("/ingest/ci", json=payload, headers=AUTH).status_code == 422
    resp = client.post("/ingest/ci", json=_valid_payload(run_id="   "), headers=AUTH)
    assert resp.status_code == 422


@pytest.mark.parametrize("run_attempt", [0, -1, True, 2**63])
def test_ci_vendor_rejects_invalid_run_attempt(
    client: TestClient, run_attempt: object
) -> None:
    response = client.post(
        "/ingest/ci",
        json=_valid_payload(run_attempt=run_attempt),
        headers=AUTH,
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "result",
    ["error", "timed_out", "skipped", "neutral", "unknown"],
)
def test_ci_vendor_accepts_non_verdict_results(client: TestClient, result: str) -> None:
    response = client.post(
        "/ingest/ci",
        json=_valid_payload(run_id=f"run-{result}", result=result),
        headers=AUTH,
    )
    assert response.status_code == 200


def test_ci_vendor_accepts_sha256_object_name(client: TestClient) -> None:
    """A 64-char SHA-256 object name stores: the old route-local check
    required exactly 40 chars, locking SHA-256 repos out of this door
    entirely."""
    resp = client.post(
        "/ingest/ci",
        json=_valid_payload(commit_sha="f" * 64),
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert resp.json()["stored"] is True


def test_ci_vendor_normalizes_sha_case(client: TestClient) -> None:
    """Uppercase hex is accepted and collapses with the lowercase spelling
    of the same commit — one commit, one uq_ci_run index entry."""
    r1 = client.post(
        "/ingest/ci", json=_valid_payload(commit_sha="AB12" * 10), headers=AUTH
    )
    assert r1.status_code == 200
    assert r1.json()["stored"] is True
    r2 = client.post(
        "/ingest/ci", json=_valid_payload(commit_sha="ab12" * 10), headers=AUTH
    )
    assert r2.status_code == 200
    assert r2.json()["stored"] is False


def test_ci_vendor_branch_normalized_exactly_once(client: TestClient) -> None:
    """BranchName is idempotent (the store re-validates on read, so the
    validator must be a fixed point) — nested refs/heads/ spellings
    collapse deterministically and the read-back equals the stored
    value."""
    resp = client.post(
        "/ingest/ci",
        json=_valid_payload(
            branch="refs/heads/refs/heads/main",
            run_url="https://example.com/normalize-once/1",
        ),
        headers=AUTH,
    )
    assert resp.status_code == 200
    stored = app.state.fact_store.read_ci_outcomes(settings.org_id)
    assert stored[0].branch == "main"


def test_ci_vendor_workflow_fields_normalize(client: TestClient) -> None:
    """Padded workflow fields strip (lineage key), and a stripped-empty
    workflow_path degrades to None — '' would fragment the lineage key
    against a genuine absent."""
    resp = client.post(
        "/ingest/ci",
        json=_valid_payload(
            workflow_name=" Build & Test ",
            workflow_path="   ",
            run_url="https://example.com/wf-norm/1",
        ),
        headers=AUTH,
    )
    assert resp.status_code == 200
    stored = app.state.fact_store.read_ci_outcomes(settings.org_id)[-1]
    assert stored.workflow_name == "Build & Test"
    assert stored.workflow_path is None


@pytest.mark.parametrize(
    "field", ["run_id", "branch", "workflow_path", "source_event_id"]
)
@pytest.mark.parametrize("bad", ["\x00", "\ud800"])
def test_ci_vendor_declines_unrepresentable_identity_before_storage(client, field, bad):
    import json

    response = client.post(
        "/ingest/ci",
        content=json.dumps(_valid_payload(**{field: "identity" + bad})),
        headers={**AUTH, "Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert app.state.fact_store.read_ci_outcomes(settings.org_id) == []
    assert (
        client.post("/ingest/ci", json=_valid_payload(), headers=AUTH).status_code
        == 200
    )


def test_ci_vendor_preserves_exceptional_descriptive_content(client):
    import json

    payload = _valid_payload(
        workflow_name="display\x00\ud800",
        run_url="url\x00\ud800",
        reason="reason\x00\ud800",
    )
    response = client.post(
        "/ingest/ci",
        content=json.dumps(payload),
        headers={**AUTH, "Content-Type": "application/json"},
    )
    assert response.status_code == 200
    stored = app.state.fact_store.read_ci_outcomes(settings.org_id)[0]
    assert stored.reason == payload["reason"]
    assert stored.workflow_name == payload["workflow_name"]
    assert stored.run_url == payload["run_url"]


def test_ci_vendor_descriptive_content_survives_authenticated_outcome_read(client):
    import json

    value = "CI descriptive\x00\ud800"
    payload = _valid_payload(workflow_name=value, run_url=value, reason=value)
    ingest = client.post(
        "/ingest/ci",
        content=json.dumps(payload),
        headers={**AUTH, "Content-Type": "application/json"},
    )
    assert ingest.status_code == 200
    response = client.get(
        "/query/ci/outcome",
        params={"provider": payload["provider"], "run_id": payload["run_id"]},
        headers=AUTH,
    )
    assert response.status_code == 200
    for field in ["workflow_name", "run_url", "reason"]:
        assert response.json()["outcome"][field] == value


def test_ci_failure_page_preserves_descriptive_content(client):
    import json
    from datetime import UTC, datetime, timedelta

    value = "provider text\x00\ud800"
    payload = _valid_payload(result="failed", reason=value, workflow_name=value)
    assert (
        client.post(
            "/ingest/ci",
            content=json.dumps(payload),
            headers={**AUTH, "Content-Type": "application/json"},
        ).status_code
        == 200
    )
    instant = datetime.now(UTC)
    response = client.get(
        "/query/ci/failures",
        params={
            "repo": payload["repo"],
            "captured_after": (instant - timedelta(days=1)).isoformat(),
            "captured_before": (instant + timedelta(days=1)).isoformat(),
        },
        headers=AUTH,
    )
    assert response.status_code == 200
    assert response.json()["outcomes"][0]["reason"] == value


def test_query_response_preserves_response_contract_validation():
    from pydantic import ValidationError
    from sediment_api.routers.query import (
        CIOutcomeFound,
        CIOutcomeNotFound,
        _query_response,
    )

    with pytest.raises(ValidationError):
        _query_response({"found": True}, CIOutcomeFound | CIOutcomeNotFound)


def test_query_representation_guard_does_not_relabel_programming_errors():
    from sediment_api.routers.query import _query_response

    with pytest.raises(TypeError, match="unsupported query response value"):
        _query_response({"unexpected": object()}, dict[str, object])
