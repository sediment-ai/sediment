# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Query endpoint tests — real FactStore + real git fixtures, never
mocked (per AGENTS.md).  The seeded scenario builds a real git repository,
mirrors it, stores a push + completion, and derives a attribution — the
same path a real deployment takes.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# gitfixtures is in packages/derive/tests — add it so the test module can
# import make_work_repo / make_remote / commit_all / run_git.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "packages/derive/tests"))

from gitfixtures import FIB, commit_all, make_remote, make_work_repo, run_git  # noqa: E402
from sediment_core import (  # noqa: E402
    CIOutcome,
    CIProvider,
    CIResult,
    FactTable,
    AgentHarness,
    DeveloperDecision,
    EditObservation,
    InteractionMode,
    ForgeProvider,
    GatewayProvider,
    InferenceCall,
    InferenceMessage,
    Push,
    RejectedEdit,
    RetryLinkage,
    TextPart,
)
from sediment_api.main import app  # noqa: E402

ORG = "testorg"
REPO = "testorg/test-repo"
AUTH = {"Authorization": "Bearer test-operator-token-3a7e-2f6c"}


def _note(*session_ids: str) -> str:
    return json.dumps(
        {
            "v": 1,
            "sessions": [
                {
                    "tool": "claude-code",
                    "session_id": s,
                    "stamped_at": "2026-07-13T00:00:00+00:00",
                }
                for s in session_ids
            ],
        }
    )


def _ci_outcome(**overrides) -> CIOutcome:
    values = {
        "outcome_id": "outcome-query",
        "org_id": ORG,
        "provider": CIProvider.GITHUB_ACTIONS,
        "run_id": "run/42",
        "run_attempt": 2,
        "repo": REPO,
        "commit_sha": "c" * 40,
        "branch": "main",
        "result": CIResult.FAILED,
        "workflow_name": "CI",
        "workflow_id": "ci.yml",
        "run_url": "https://ci.example/runs/42",
        "provider_result": "failure",
        "captured_at": datetime(2026, 9, 5, 12, 0, tzinfo=UTC),
        "raw": {"logs": "sensitive"},
    }
    values.update(overrides)
    return CIOutcome(**values)


@pytest.fixture()
def seeded_attribution(tmp_path, client: TestClient, monkeypatch) -> str:
    """Build a real git repo, mirror it, store a push + completion with a
    notes attribution, then run the derivation.  Returns the commit SHA so
    tests can query it."""
    from sediment_api.config import settings
    from sediment_derive import MirrorManager, derive_attributions

    # Patch mirror_path so the endpoint's derivation sees the mirror.
    mirror_base = str(tmp_path / "mirrors")
    monkeypatch.setattr(settings, "mirror_path", mirror_base)

    work = make_work_repo(tmp_path)
    (work / "app").mkdir()
    (work / "app" / "math_utils.py").write_text("")
    base = commit_all(work, "scaffold")
    (work / "app" / "math_utils.py").write_text(FIB)
    head = commit_all(work, "add fibonacci helper")
    run_git(work, "notes", "--ref=sediment", "add", "-m", _note("sess-query"), head)

    remote = make_remote(tmp_path, work)

    push = Push(
        org_id=ORG,
        provider=ForgeProvider.GITHUB,
        repo=REPO,
        clone_url=str(remote),
        ref="refs/heads/main",
        before_sha=base,
        after_sha=head,
    )
    mirrors = MirrorManager(mirror_base)
    mirrors.ensure(push)

    store = app.state.fact_store
    store.store_push(push)
    from sediment_core import SessionCommitObservation

    store.store_session_commit_observation(
        SessionCommitObservation(
            observation_id="seeded-session-observation",
            org_id=ORG,
            repo=REPO,
            commit_sha=head,
            session_id="sess-query",
            source_push_id=push.push_id,
            captured_at=push.captured_at,
        )
    )

    completion = InferenceCall(
        org_id=ORG,
        session_id="sess-query",
        user_id="agent:derive",
        gateway_provider=GatewayProvider.LITELLM,
        model="claude-sonnet-5",
        input_messages=[
            InferenceMessage(
                role="user",
                parts=[TextPart(content="write a fibonacci function")],
            )
        ],
        output_messages=[
            InferenceMessage(role="assistant", parts=[TextPart(content=FIB)])
        ],
        input_tokens=10,
        output_tokens=20,
        duration_ms=50,
        model_call_id="call-query",
        observed_at=datetime.now(UTC),
    )
    store.store_inference_call(completion)

    # Regression seed for the decision count: decisions carry a call_id,
    # not a inference_call_id, and a fixture without one let an AttributeError
    # on the count path survive CI.
    store.store_decision(
        DeveloperDecision(
            org_id=ORG,
            session_id="sess-query",
            user_id="agent:derive",
            agent_harness=AgentHarness.CLAUDE_CODE,
            file_path="app/math_utils.py",
            accepted=True,
            explicit=True,
            interaction_mode=InteractionMode.AGENT,
            call_id="call-query",
            occurred_at=datetime.now(UTC),
        )
    )

    attributions = derive_attributions(store, mirrors, ORG)
    assert len(attributions) >= 1, "seed scenario must produce ≥1 attribution"

    return head


def test_query_commit_attributed(client: TestClient, seeded_attribution: str) -> None:
    resp = client.get(
        f"/query/commit/{seeded_attribution}",
        headers=AUTH,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["commit_sha"] == seeded_attribution
    assert body["attributed"] is True
    repos = body["repos"]
    assert isinstance(repos, list)
    assert len(repos) >= 1
    entry = repos[0]
    assert entry["repo"] == REPO
    assert len(entry["inference_calls"]) >= 1
    comp = entry["inference_calls"][0]
    assert comp["inference_call_id"] is not None
    assert comp["session_id"] == "sess-query"
    assert comp["gateway_provider"] == "litellm"
    assert comp["model_provider"] is None
    assert comp["model"] == "claude-sonnet-5"
    assert comp["user_id"] == "agent:derive"
    assert comp["attribution_source"] == "git_notes"
    assert entry["decisions"] == 1
    assert isinstance(entry["ci_outcomes"], list)


def test_query_commit_unattributed(client: TestClient, tmp_path) -> None:
    """An unknown sha returns attributed: false, 200 — not an error."""
    unknown = "a" * 40
    resp = client.get(
        f"/query/commit/{unknown}",
        headers=AUTH,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["commit_sha"] == unknown
    assert body["attributed"] is False


def test_query_ci_outcome_by_exact_run_identity(client: TestClient) -> None:
    app.state.fact_store.store_ci_outcome(_ci_outcome())

    response = client.get(
        "/query/ci/outcome",
        params={
            "provider": "github_actions",
            "run_id": "run/42",
            "run_attempt": 2,
        },
        headers=AUTH,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["found"] is True
    assert body["outcome"]["run_attempt"] == 2
    assert body["outcome"]["provider_result"] == "failure"
    from urllib.parse import parse_qs, urlsplit

    navigation = urlsplit(body["outcome"]["commit_query"])
    assert navigation.path == f"/query/commit/{'c' * 40}"
    assert parse_qs(navigation.query)["as_of"] == ["2026-09-05T12:00:00+00:00"]
    assert "raw" not in body["outcome"]


def test_query_ci_outcome_supports_null_attempt_identity(client: TestClient) -> None:
    app.state.fact_store.store_ci_outcome(
        _ci_outcome(outcome_id="null-attempt", run_id="run-null", run_attempt=None)
    )

    response = client.get(
        "/query/ci/outcome",
        params={"provider": "github_actions", "run_id": "run-null"},
        headers=AUTH,
    )

    assert response.status_code == 200
    assert response.json()["outcome"]["run_attempt"] is None


def test_query_ci_outcome_hides_other_org_and_quarantined_facts(
    client: TestClient,
) -> None:
    store = app.state.fact_store
    store.store_ci_outcome(
        _ci_outcome(outcome_id="other-org", org_id="other", run_id="hidden-run")
    )
    other_org = client.get(
        "/query/ci/outcome",
        params={
            "provider": "github_actions",
            "run_id": "hidden-run",
            "run_attempt": 2,
        },
        headers=AUTH,
    )
    own = _ci_outcome(outcome_id="quarantined", run_id="hidden-run")
    store.store_ci_outcome(own)
    store.quarantine_fact(
        ORG,
        FactTable.CI_OUTCOMES,
        own.outcome_id,
        reason="invalid CI integration",
    )
    quarantined = client.get(
        "/query/ci/outcome",
        params={
            "provider": "github_actions",
            "run_id": "hidden-run",
            "run_attempt": 2,
        },
        headers=AUTH,
    )

    assert other_org.json() == {"found": False}
    assert quarantined.json() == {"found": False}


def test_query_ci_failures_defaults_to_failed_and_paginates(
    client: TestClient,
) -> None:
    store = app.state.fact_store
    base = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    store.store_ci_outcome(_ci_outcome(outcome_id="failed-1", captured_at=base))
    store.store_ci_outcome(
        _ci_outcome(
            outcome_id="passed",
            run_id="run-passed",
            result=CIResult.PASSED,
            captured_at=base.replace(minute=1),
        )
    )
    store.store_ci_outcome(
        _ci_outcome(
            outcome_id="failed-2",
            run_id="run-failed-2",
            captured_at=base.replace(minute=2),
        )
    )
    params = {
        "repo": REPO,
        "captured_after": "2026-09-05T11:00:00Z",
        "captured_before": "2026-09-05T13:00:00Z",
        "limit": 1,
    }

    first = client.get("/query/ci/failures", params=params, headers=AUTH)
    assert first.status_code == 200
    assert [row["outcome_id"] for row in first.json()["outcomes"]] == ["failed-2"]
    assert first.json()["result"] == "failed"
    assert first.json()["next_cursor"] is not None

    second = client.get(
        "/query/ci/failures",
        params={**params, "cursor": first.json()["next_cursor"]},
        headers=AUTH,
    )
    assert second.status_code == 200
    assert [row["outcome_id"] for row in second.json()["outcomes"]] == ["failed-1"]
    assert second.json()["next_cursor"] is None


def test_query_ci_failures_requires_time_bounds_and_auth(client: TestClient) -> None:
    missing_bounds = client.get(
        "/query/ci/failures", params={"repo": REPO}, headers=AUTH
    )
    unauthenticated = client.get(
        "/query/ci/failures",
        params={
            "repo": REPO,
            "captured_after": "2026-09-05T11:00:00Z",
            "captured_before": "2026-09-05T13:00:00Z",
        },
    )
    invalid_token = client.get(
        "/query/ci/failures",
        params={
            "repo": REPO,
            "captured_after": "2026-09-05T11:00:00Z",
            "captured_before": "2026-09-05T13:00:00Z",
        },
        headers={"Authorization": "Bearer wrong"},
    )
    reversed_bounds = client.get(
        "/query/ci/failures",
        params={
            "repo": REPO,
            "captured_after": "2026-09-05T13:00:00Z",
            "captured_before": "2026-09-05T11:00:00Z",
        },
        headers=AUTH,
    )

    assert missing_bounds.status_code == 422
    assert unauthenticated.status_code == 401
    assert invalid_token.status_code == 401
    assert reversed_bounds.status_code == 422


def test_query_ci_filters_non_verdicts_and_excludes_sensitive_fields(
    client: TestClient,
) -> None:
    store = app.state.fact_store
    store.store_ci_outcome(
        _ci_outcome(
            outcome_id="error-match",
            run_id="error-match",
            result=CIResult.ERROR,
            workflow_name="Build",
            pr_number=12,
        )
    )
    store.store_ci_outcome(
        _ci_outcome(
            outcome_id="error-other-pr",
            run_id="error-other-pr",
            result=CIResult.ERROR,
            workflow_name="Build",
            pr_number=13,
        )
    )

    response = client.get(
        "/query/ci/failures",
        params={
            "repo": REPO,
            "captured_after": "2026-09-05T11:00:00Z",
            "captured_before": "2026-09-05T13:00:00Z",
            "result": "error",
            "workflow_name": "Build",
            "pr_number": 12,
        },
        headers=AUTH,
    )

    assert response.status_code == 200
    assert [row["outcome_id"] for row in response.json()["outcomes"]] == ["error-match"]
    forbidden = {
        "raw",
        "prompt",
        "response",
        "reasoning",
        "tool_arguments",
        "diff",
        "applied_text",
        "observed_file_text",
    }

    def keys(value):
        if isinstance(value, dict):
            return set(value) | set().union(*(keys(item) for item in value.values()))
        if isinstance(value, list):
            return set().union(*(keys(item) for item in value))
        return set()

    assert keys(response.json()).isdisjoint(forbidden)


def test_query_ci_failures_rejects_malformed_cursor(client: TestClient) -> None:
    response = client.get(
        "/query/ci/failures",
        params={
            "repo": REPO,
            "captured_after": "2026-09-05T11:00:00Z",
            "captured_before": "2026-09-05T13:00:00Z",
            "cursor": "%%%",
        },
        headers=AUTH,
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "invalid CI outcome cursor"}


def test_query_ci_cursor_is_bound_to_original_filters(client: TestClient) -> None:
    store = app.state.fact_store
    store.store_ci_outcome(_ci_outcome(outcome_id="cursor-1"))
    store.store_ci_outcome(
        _ci_outcome(
            outcome_id="cursor-2",
            run_id="cursor-run-2",
            captured_at=datetime(2026, 9, 5, 12, 1, tzinfo=UTC),
        )
    )
    params = {
        "repo": REPO,
        "captured_after": "2026-09-05T11:00:00Z",
        "captured_before": "2026-09-05T13:00:00Z",
        "limit": 1,
    }
    first = client.get("/query/ci/failures", params=params, headers=AUTH)

    changed = client.get(
        "/query/ci/failures",
        params={
            **params,
            "result": "passed",
            "cursor": first.json()["next_cursor"],
        },
        headers=AUTH,
    )

    assert changed.status_code == 422
    assert changed.json() == {"detail": "CI outcome cursor doesn't match filters"}


def test_query_ci_openapi_defines_response_envelopes() -> None:
    schema = app.openapi()
    exact = schema["paths"]["/query/ci/outcome"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    failures = schema["paths"]["/query/ci/failures"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]

    assert "anyOf" in exact
    assert "$ref" in failures


def test_query_session_dossier_assembles_metadata_evidence(
    client: TestClient, seeded_attribution: str
) -> None:
    store = app.state.fact_store
    occurred_at = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    store.store_edit_observation(
        EditObservation(
            observation_id="observation-dossier",
            org_id=ORG,
            session_id="sess-query",
            user_id="developer-sensitive",
            agent_harness=AgentHarness.CLAUDE_CODE,
            file_path="/Users/developer/private.py",
            call_id="edit-dossier",
            applied_text="secret applied text",
            observed_file_text="secret observed text",
            external_lines_added=3,
            external_lines_removed=None,
            occurred_at=occurred_at,
            raw={"secret": "raw observation"},
        )
    )
    store.store_rejected_edit(
        RejectedEdit(
            rejection_id="rejection-dossier",
            org_id=ORG,
            session_id="sess-query",
            user_id="developer-sensitive",
            agent_harness=AgentHarness.CLAUDE_CODE,
            file_path="/Users/developer/private.py",
            call_id="reject-dossier",
            proposed="secret proposed text",
            occurred_at=occurred_at,
            raw={"secret": "raw rejection"},
        )
    )
    store.store_retry_linkage(
        RetryLinkage(
            retry_linkage_id="retry-dossier",
            org_id=ORG,
            session_id="sess-query",
            user_id="developer-sensitive",
            agent_harness=AgentHarness.CLAUDE_CODE,
            file_path="/Users/developer/private.py",
            tool_name="Edit",
            rejected_call_id="reject-dossier",
            accepted_call_id="edit-dossier",
            occurred_at=occurred_at,
            raw={"secret": "raw linkage"},
        )
    )
    store.store_ci_outcome(
        _ci_outcome(
            outcome_id="dossier-ci",
            run_id="dossier-run",
            commit_sha=seeded_attribution,
            reason="failure in /Users/developer/private.py: secret source",
        )
    )

    response = client.get("/query/session/sess-query", headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    assert body["found"] is True
    assert body["session_id"] == "sess-query"
    assert {event["event_type"] for event in body["timeline"]} == {
        "inference_call",
        "developer_decision",
        "edit_observation",
        "rejected_edit",
        "retry_linkage",
    }
    assert [
        (event["occurred_at"], event["event_type"], event["fact_id"])
        for event in body["timeline"]
    ] == sorted(
        (event["occurred_at"], event["event_type"], event["fact_id"])
        for event in body["timeline"]
    )
    assert body["attributed_commits"][0]["commit_sha"] == seeded_attribution
    assert body["pushes"][0]["after_sha"] == seeded_attribution
    assert body["ci_outcomes"][0]["outcome_id"] == "dossier-ci"
    observation = next(
        event for event in body["timeline"] if event["event_type"] == "edit_observation"
    )
    assert observation["has_file_path"] is True
    assert observation["external_lines_added_present"] is True
    assert observation["external_lines_removed_present"] is False
    assert observation["external_lines_added"] == 3
    assert "external_lines_removed" not in observation

    serialized = json.dumps(body)
    for forbidden in (
        "developer-sensitive",
        "/Users/developer/private.py",
        "secret applied text",
        "secret observed text",
        "secret proposed text",
        "raw observation",
        "raw rejection",
        "raw linkage",
        "secret source",
    ):
        assert forbidden not in serialized


def test_query_session_dossier_unknown_and_other_org_are_absent(
    client: TestClient,
) -> None:
    app.state.fact_store.store_inference_call(
        InferenceCall(
            org_id="other",
            session_id="other-session",
            gateway_provider=GatewayProvider.LITELLM,
            input_messages=[],
            output_messages=[],
        )
    )

    unknown = client.get("/query/session/missing", headers=AUTH)
    other_org = client.get("/query/session/other-session", headers=AUTH)

    assert unknown.json() == {"found": False}
    assert other_org.json() == {"found": False}


def test_query_session_dossier_reports_quarantined_omissions(
    client: TestClient,
) -> None:
    store = app.state.fact_store
    call = InferenceCall(
        inference_call_id="visible-dossier-call",
        org_id=ORG,
        session_id="quarantine-session",
        gateway_provider=GatewayProvider.LITELLM,
        input_messages=[],
        output_messages=[],
    )
    hidden = call.model_copy(update={"inference_call_id": "hidden-dossier-call"})
    store.store_inference_call(call)
    store.store_inference_call(hidden)
    store.quarantine_fact(
        ORG,
        FactTable.INFERENCE_CALLS,
        hidden.inference_call_id,
        reason="invalid capture",
    )

    response = client.get("/query/session/quarantine-session", headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    assert body["coverage"]["inference_calls"] == {
        "total": 2,
        "visible": 1,
        "quarantined": 1,
    }
    assert body["omitted_events"] == 1
    assert [event["fact_id"] for event in body["timeline"]] == ["visible-dossier-call"]


def test_query_session_dossier_rejects_large_sessions(
    client: TestClient, monkeypatch
) -> None:
    from sediment_api import workers

    monkeypatch.setattr(
        workers,
        "_WORKER_COMMAND",
        (
            sys.executable,
            "-c",
            "from sediment_api import worker; worker.query._SESSION_DOSSIER_LIMIT = 1; raise SystemExit(worker.main())",
        ),
    )
    store = app.state.fact_store
    for call_id in ("large-1", "large-2"):
        store.store_inference_call(
            InferenceCall(
                inference_call_id=call_id,
                org_id=ORG,
                session_id="large-session",
                gateway_provider=GatewayProvider.LITELLM,
                input_messages=[],
                output_messages=[],
            )
        )

    response = client.get("/query/session/large-session", headers=AUTH)

    assert response.status_code == 409
    assert response.json() == {"detail": "Session exceeds the dossier limit"}


def test_query_session_dossier_observation_is_independent_of_push_attribution_window(
    client: TestClient, seeded_attribution: str
) -> None:
    store = app.state.fact_store
    later = store.read_pushes(ORG)[0]
    store.store_push(
        Push(
            push_id="earlier-dossier-push",
            org_id=ORG,
            provider=ForgeProvider.GITHUB,
            repo=later.repo,
            clone_url=later.clone_url,
            ref="refs/heads/earlier",
            before_sha=later.before_sha,
            after_sha=seeded_attribution,
            captured_at=later.captured_at - timedelta(days=30),
        )
    )

    response = client.get("/query/session/sess-query", headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    assert [item["commit_sha"] for item in body["attributed_commits"]] == [
        seeded_attribution
    ]
    assert len(body["pushes"]) == 2
    assert body["ci_outcomes"] == []
    assert "session_commit_unobserved" not in body["gaps"]


def test_query_session_dossier_requires_auth_and_has_typed_openapi(
    client: TestClient,
) -> None:
    assert client.get("/query/session/session-1").status_code == 401
    assert (
        client.get(
            "/query/session/session-1",
            headers={"Authorization": "Bearer wrong"},
        ).status_code
        == 401
    )
    response_schema = app.openapi()["paths"]["/query/session/{session_id}"]["get"][
        "responses"
    ]["200"]["content"]["application/json"]["schema"]
    assert "anyOf" in response_schema


def test_query_commit_unknown_sha_with_mirror(
    client: TestClient, seeded_attribution: str
) -> None:
    """Mirror configured, derivation runs, sha matches nothing →
    attributed: false, 200 — the post-derivation empty path."""
    unknown = "b" * 40
    resp = client.get(
        f"/query/commit/{unknown}",
        headers=AUTH,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["commit_sha"] == unknown
    assert body["attributed"] is False


def test_query_commit_auth_reject(client: TestClient, seeded_attribution) -> None:
    """Missing / invalid bearer → 401."""
    resp = client.get(f"/query/commit/{seeded_attribution}")
    assert resp.status_code == 401


def test_query_commit_bad_sha_rejected(client: TestClient) -> None:
    """Malformed sha → 422, not 500."""
    resp = client.get(
        "/query/commit/not-a-sha",
        headers=AUTH,
    )
    assert resp.status_code == 422


def test_query_commit_multi_repo(
    tmp_path, client: TestClient, seeded_attribution: str, monkeypatch
) -> None:
    """Same commit sha in two repos → one per-repo entry for each."""
    import shutil
    from urllib.parse import quote

    from sediment_api.config import settings

    REPO2 = "testorg/other-repo"

    # Copy the existing mirror to a second repo name so the derivation
    # finds the same commit in both.
    mirror_base = Path(settings.mirror_path)
    src = mirror_base / quote(f"{ORG}/{REPO}", safe="")
    dst = mirror_base / quote(f"{ORG}/{REPO2}", safe="")
    shutil.copytree(src, dst)

    # Store a push for the second repo carrying the same head sha.
    push2 = Push(
        org_id=ORG,
        provider=ForgeProvider.GITHUB,
        repo=REPO2,
        clone_url="file:///unused",
        ref="refs/heads/main",
        before_sha="0" * 40,
        after_sha=seeded_attribution,
    )
    store = app.state.fact_store
    store.store_push(push2)
    # A CI outcome for REPO only: the per-repo grouping must keep it out
    # of REPO2's entry (the contamination case).
    store.store_ci_outcome(
        CIOutcome(
            org_id=ORG,
            provider=CIProvider.GITHUB_ACTIONS,
            run_id="run/1",
            run_attempt=2,
            repo=REPO,
            commit_sha=seeded_attribution,
            branch="main",
            result=CIResult.PASSED,
            workflow_name="CI",
            workflow_id="workflow/1",
            run_url="https://ci.example/run/1",
            provider_result="success",
            source_event_type="github.workflow_run.completed",
            source_event_id="delivery/1",
        )
    )

    resp = client.get(
        f"/query/commit/{seeded_attribution}",
        headers=AUTH,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["attributed"] is True
    repos = body["repos"]
    # Both repos appear, sorted, each with its own attributions.
    by_name = {r["repo"]: r for r in repos}
    assert set(by_name) == {REPO, REPO2}
    assert [r["repo"] for r in repos] == sorted(by_name)
    for entry in repos:
        assert len(entry["inference_calls"]) >= 1
        assert isinstance(entry["decisions"], int)
    # CI outcomes never cross repos: the outcome stored for REPO must not
    # appear under REPO2.
    [outcome] = by_name[REPO]["ci_outcomes"]
    assert outcome["result"] == "passed"
    assert outcome["run_id"] == "run/1"
    assert outcome["run_attempt"] == 2
    assert outcome["workflow_id"] == "workflow/1"
    assert outcome["provider_result"] == "success"
    assert outcome["source_event_type"] == "github.workflow_run.completed"
    assert outcome["source_event_id"] == "delivery/1"
    assert by_name[REPO2]["ci_outcomes"] == []


def test_query_commit_short_sha_rejected(client: TestClient) -> None:
    resp = client.get(
        "/query/commit/abc123",
        headers=AUTH,
    )
    assert resp.status_code == 422


def test_query_accepts_sha256_and_normalizes_case(client: TestClient) -> None:
    """The read door shares the CommitSha definition: a 64-char
    SHA-256 name answers cleanly, and an uppercase spelling of a stored
    (lowercase-normalized) sha still matches instead of missing."""
    resp = client.get(f"/query/commit/{'f' * 64}", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["attributed"] is False
    resp = client.get(
        f"/query/commit/{'AB12' * 10}",
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert resp.json()["commit_sha"] == "ab12" * 10


def test_query_timeout_returns_503(
    client: TestClient, seeded_attribution: str, monkeypatch
) -> None:
    """A query that exceeds the time budget returns 503 with a clear
    detail — never blocks indefinitely, never 500s."""
    from sediment_api import workers

    # Pin the budget to a tiny window so the test doesn't sleep long.
    monkeypatch.setattr(workers, "QUERY_BUDGET_SECONDS", 0.1)
    monkeypatch.setattr(
        workers,
        "_WORKER_COMMAND",
        (sys.executable, "-c", "import time; time.sleep(60)"),
    )

    resp = client.get(
        f"/query/commit/{seeded_attribution}",
        headers=AUTH,
    )
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert "budget" in detail


def test_query_concurrent_health_succeeds(
    client: TestClient, seeded_attribution: str, monkeypatch, tmp_path
) -> None:
    """While a heavy query runs, /health and ingest doors stay responsive
    — the derivation is off the event loop.  Fires both concurrently and
    asserts the health check completes before the query finishes."""
    import threading
    import time

    from sediment_api import workers

    # A latch so the stub blocks until we let it go — the health check
    # must prove responsiveness *during* the derivation, not after.
    entered = tmp_path / "entered"
    latch = tmp_path / "released"
    monkeypatch.setattr(
        workers,
        "_WORKER_COMMAND",
        (
            sys.executable,
            "-c",
            "from pathlib import Path\nimport time\n"
            f"Path({str(entered)!r}).touch()\n"
            f"while not Path({str(latch)!r}).exists(): time.sleep(0.01)\n"
            "print('200'); print('{}')",
        ),
    )

    results: dict[str, int | None] = {"query": None, "health": None}

    def _do_query() -> None:
        resp = client.get(
            f"/query/commit/{seeded_attribution}",
            headers=AUTH,
        )
        results["query"] = resp.status_code

    def _do_health() -> None:
        # Wait for the query thread to enter _slow, proving the derivation
        # is in-flight.
        deadline = time.monotonic() + 5
        while not entered.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert entered.exists()
        resp = client.get("/health")
        results["health"] = resp.status_code

    query_thread = threading.Thread(target=_do_query)
    health_thread = threading.Thread(target=_do_health)

    query_thread.start()
    health_thread.start()

    try:
        health_thread.join(timeout=5)
        assert results["health"] == 200, "health check must succeed during query"
        assert results["query"] is None
        assert client.get("/v1/facts", headers=AUTH).status_code == 200
    finally:
        latch.touch()
        query_thread.join(timeout=5)
    assert results["query"] == 200


def test_query_process_survives_slow_query(
    client: TestClient,
    seeded_attribution: str,
    monkeypatch,
) -> None:
    """An over-budget query returns 503 and the process keeps serving."""
    from sediment_api import workers

    # monkeypatch.context(), not manual save/restore: a failing assert
    # inside the block must not leak the sleeping stub into later tests.
    with monkeypatch.context() as m:
        m.setattr(workers, "QUERY_BUDGET_SECONDS", 0.05)
        m.setattr(
            workers,
            "_WORKER_COMMAND",
            (sys.executable, "-c", "import time; time.sleep(60)"),
        )

        # First query: times out.
        resp = client.get(
            f"/query/commit/{seeded_attribution}",
            headers=AUTH,
        )
        assert resp.status_code == 503

        # Process must still be alive: health and a real query must work.
        assert client.get("/health").status_code == 200

    # The context restored the real derivation and budget; a real query
    # must succeed.
    resp = client.get(
        f"/query/commit/{seeded_attribution}",
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert resp.json()["attributed"] is True


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_commit_query_declines_non_finite_fact_content_without_coercion(
    client: TestClient, seeded_attribution: str, value: float
) -> None:
    import math

    store = app.state.fact_store
    outcome = _ci_outcome(
        commit_sha=seeded_attribution, raw={"nested": [{"value": value}]}
    )
    assert store.store_ci_outcome(outcome)
    response = client.get(f"/query/commit/{seeded_attribution}", headers=AUTH)
    assert response.status_code == 409
    assert response.json() == {"detail": {"reason": "non_finite_number"}}
    stored = store.read_ci_outcomes(ORG)[0].raw["nested"][0]["value"]
    assert math.isnan(stored) if math.isnan(value) else stored == value
    metadata = client.get(
        "/query/ci/outcome",
        params={
            "provider": outcome.provider.value,
            "run_id": outcome.run_id,
            "run_attempt": outcome.run_attempt,
        },
        headers=AUTH,
    )
    assert metadata.status_code == 200
    assert "raw" not in metadata.json()["outcome"]


def test_commit_query_preserves_finite_and_descriptive_fact_content(
    client: TestClient, seeded_attribution: str
) -> None:
    value = "text\x00\ud800"
    outcome = _ci_outcome(
        commit_sha=seeded_attribution,
        reason=value,
        raw={"nested": [{"value": 1.5}], "text": value},
    )
    assert app.state.fact_store.store_ci_outcome(outcome)
    response = client.get(f"/query/commit/{seeded_attribution}", headers=AUTH)
    assert response.status_code == 200
    emitted = response.json()["repos"][0]["ci_outcomes"][0]
    assert emitted["reason"] == value
    assert emitted["raw"] == outcome.raw


@pytest.mark.parametrize("ambiguous", [False, True])
def test_commit_query_decision_count_preserves_org_population_and_session_identity(
    client: TestClient, seeded_attribution: str, ambiguous: bool
) -> None:
    store = app.state.fact_store
    original = store.read_inference_calls(ORG)[0]
    store.store_decision(
        DeveloperDecision(
            org_id=ORG,
            session_id="other-session",
            agent_harness=AgentHarness.CLAUDE_CODE,
            file_path="app/math_utils.py",
            accepted=True,
            explicit=True,
            interaction_mode=InteractionMode.AGENT,
            call_id=original.model_call_id,
            occurred_at=datetime.now(UTC),
        )
    )
    # Query population remains org-wide: foreign Facts don't introduce ambiguity.
    store.store_inference_call(
        InferenceCall(
            org_id="foreign-org",
            session_id=original.session_id,
            gateway_provider=GatewayProvider.LITELLM,
            input_messages=[],
            output_messages=[],
            model_call_id=original.model_call_id,
        )
    )
    store.store_decision(
        DeveloperDecision(
            org_id=ORG,
            session_id=original.session_id,
            agent_harness=AgentHarness.CLAUDE_CODE,
            file_path="keyless.py",
            accepted=True,
            explicit=True,
            interaction_mode=InteractionMode.AGENT,
            call_id=None,
            occurred_at=datetime.now(UTC),
        )
    )
    if ambiguous:
        store.store_inference_call(
            InferenceCall(
                org_id=ORG,
                session_id="other-session",
                gateway_provider=GatewayProvider.PORTKEY,
                input_messages=[],
                output_messages=[],
                model_call_id=original.model_call_id,
            )
        )
    for _ in range(2):
        response = client.get(f"/query/commit/{seeded_attribution}", headers=AUTH)
        assert response.status_code == 200
        entry = next(item for item in response.json()["repos"] if item["repo"] == REPO)
        assert entry["decisions"] == (0 if ambiguous else 1)


def test_session_dossier_uses_stored_observation_without_a_mirror(client, monkeypatch):
    from sediment_core import SessionCommitObservation
    from sediment_api.config import settings

    monkeypatch.setattr(settings, "mirror_path", None)
    observation = SessionCommitObservation(
        observation_id="dossier-edge",
        org_id=ORG,
        repo=REPO,
        commit_sha="a" * 40,
        session_id="observed-without-mirror",
        source_push_id="push",
        captured_at=datetime(2026, 9, 5, tzinfo=UTC),
    )
    app.state.fact_store.store_session_commit_observation(observation)
    response = client.get("/query/session/observed-without-mirror", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["found"] is True
    assert len(body["attributed_commits"]) == 1
    assert body["attributed_commits"][0]["attribution_sources"] == []
    assert {"minimum_similarity", "maximum_similarity", "attributed_files"}.isdisjoint(
        body["attributed_commits"][0]
    )
    assert body["attributed_commits"][0]["session_commit_observation_ids"] == [
        "dossier-edge"
    ]


def test_commit_query_retains_exact_ci_when_session_observation_is_missing(
    client, monkeypatch
):
    from sediment_api.config import settings

    monkeypatch.setattr(settings, "mirror_path", None)
    outcome = _ci_outcome()
    app.state.fact_store.store_ci_outcome(outcome)
    response = client.get(f"/query/commit/{outcome.commit_sha}", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["attributed"] is False
    assert body["repos"][0]["ci_outcomes"][0]["outcome_id"] == outcome.outcome_id
    assert body["repos"][0]["observed_sessions"] == []


@pytest.mark.parametrize(
    "case",
    [
        "matching",
        "absent",
        "wrong_org",
        "wrong_repo",
        "wrong_commit",
        "wrong_session",
        "quarantined",
    ],
)
def test_public_investigations_keep_observed_and_inferred_relationships_separate(
    client, seeded_attribution, case
):
    from sediment_core import SessionCommitObservation

    store = app.state.fact_store
    [original] = store.read_session_commit_observations(ORG)
    if case != "matching":
        store.quarantine_fact(
            ORG,
            FactTable.SESSION_COMMIT_OBSERVATIONS,
            original.observation_id,
            reason="synthetic quarantine",
        )
    changes = {
        "wrong_org": {"org_id": "other"},
        "wrong_repo": {"repo": "other/repo"},
        "wrong_commit": {"commit_sha": "b" * 40},
        "wrong_session": {"session_id": "other"},
    }
    if case in changes:
        observation = SessionCommitObservation.model_validate(
            {
                **original.model_dump(),
                "observation_id": "candidate-edge",
                **changes.get(case, {}),
                "captured_at": original.captured_at + timedelta(microseconds=1),
            }
        )
        store.store_session_commit_observation(observation)
    ci = _ci_outcome(commit_sha=seeded_attribution)
    store.store_ci_outcome(ci)
    for _ in range(2):
        response = client.get(f"/query/commit/{seeded_attribution}", headers=AUTH)
        assert response.status_code == 200
        entry = next(row for row in response.json()["repos"] if row["repo"] == REPO)
        assert len(entry["inference_calls"]) == 1
        assert entry["inference_calls"][0]["relationship"] == "inferred_call_to_file"
        assert entry["ci_outcomes"][0]["outcome_id"] == ci.outcome_id
        assert entry["session_commit_unobserved"] == int(case != "matching")
        sessions = {row["session_id"] for row in entry["observed_sessions"]}
        assert ("sess-query" in sessions) == (case == "matching")
        dossier = client.get("/query/session/sess-query", headers=AUTH)
        assert dossier.status_code == 200
        exact = [
            row
            for row in dossier.json()["attributed_commits"]
            if (row["repo"], row["commit_sha"]) == (REPO, seeded_attribution)
        ]
        assert bool(exact) == (case == "matching")
        ci_read = client.get(
            "/query/ci/outcome",
            params={
                "provider": ci.provider.value,
                "run_id": ci.run_id,
                "run_attempt": ci.run_attempt,
            },
            headers=AUTH,
        )
        assert ci_read.status_code == 200
        assert ci_read.json()["outcome"]["outcome_id"] == ci.outcome_id
        failures = client.get(
            "/query/ci/failures",
            params={
                "repo": REPO,
                "captured_after": (ci.captured_at - timedelta(days=1)).isoformat(),
                "captured_before": (ci.captured_at + timedelta(days=1)).isoformat(),
            },
            headers=AUTH,
        )
        assert failures.status_code == 200
        assert ci.outcome_id in json.dumps(failures.json())


def test_commit_query_omits_inference_histories_and_unrelated_decisions(
    client: TestClient, seeded_attribution: str
) -> None:
    from sqlalchemy import event
    from sediment_api.routers.query import _run_query

    store = app.state.fact_store
    store.store_inference_call(
        InferenceCall(
            org_id=ORG,
            session_id="unrelated-session",
            gateway_provider=GatewayProvider.LITELLM,
            input_messages=[
                InferenceMessage(role="user", parts=[TextPart(content="x" * 100_000)])
            ],
            output_messages=[],
            raw={"payload": "y" * 100_000},
        )
    )
    store.store_decision(
        DeveloperDecision(
            org_id=ORG,
            session_id="unrelated-session",
            agent_harness=AgentHarness.CLAUDE_CODE,
            file_path="unrelated.py",
            accepted=True,
            explicit=True,
            interaction_mode=InteractionMode.AGENT,
            call_id="unrelated-call",
            occurred_at=datetime.now(UTC),
        )
    )
    statements = []

    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(store._engine, "before_cursor_execute", capture)
    try:
        result = _run_query(seeded_attribution, store)
    finally:
        event.remove(store._engine, "before_cursor_execute", capture)
    assert result["repos"][0]["decisions"] == 1
    assert all(
        "inference_calls.input_messages" not in statement
        and "inference_calls.raw" not in statement
        for statement in statements
    )
    decision_reads = [
        statement
        for statement in statements
        if "SELECT developer_decisions." in statement
    ]
    assert decision_reads
    assert all("developer_decisions.session_id IN" in sql for sql in decision_reads)
    summary_reads = [
        statement
        for statement in statements
        if "inference_calls.input_tokens" in statement
    ]
    assert summary_reads
    assert all("inference_calls.inference_call_id IN" in sql for sql in summary_reads)
    witness_reads = [
        statement for statement in statements if "inference_call_aliases" in statement
    ]
    assert witness_reads
    assert all("inference_calls.output_messages" not in sql for sql in witness_reads)


def test_commit_query_without_inferred_calls_avoids_call_and_decision_reads(
    client: TestClient, seeded_attribution: str, monkeypatch
) -> None:
    from sqlalchemy import event
    from sediment_api.config import settings
    from sediment_api.routers.query import _run_query

    monkeypatch.setattr(settings, "mirror_path", None)
    store = app.state.fact_store
    statements = []

    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(store._engine, "before_cursor_execute", capture)
    try:
        result = _run_query(seeded_attribution, store)
    finally:
        event.remove(store._engine, "before_cursor_execute", capture)
    assert result["attributed"] is True
    assert result["repos"][0]["inference_calls"] == []
    assert all("FROM inference_calls" not in statement for statement in statements)
    assert all("FROM developer_decisions" not in statement for statement in statements)


@pytest.mark.parametrize(
    "witness",
    ["old", "boundary", "future", "quarantined", "released", "foreign", "three_owners"],
)
def test_commit_query_matches_all_history_joins_and_shuffled_arrival(
    client: TestClient, seeded_attribution: str, postgres_database_factory, witness
) -> None:
    from contextlib import contextmanager

    from sqlalchemy import create_engine
    from sediment_core import FactStore, ToolCallPart
    from sediment_api.routers.query import _run_query

    store = app.state.fact_store
    boundary = datetime.now(UTC)
    original = store.read_inference_calls(ORG)[0]
    collision = InferenceCall(
        inference_call_id="alias-witness",
        org_id="foreign" if witness == "foreign" else ORG,
        session_id="unrelated-session",
        gateway_provider=GatewayProvider.PORTKEY,
        input_messages=[],
        output_messages=[
            InferenceMessage(
                role="assistant",
                parts=[
                    ToolCallPart(id=original.model_call_id, name="Edit", arguments={}),
                    ToolCallPart(id=original.model_call_id, name="Edit", arguments={}),
                ],
            )
        ],
        observed_at=(
            boundary - timedelta(days=30)
            if witness == "old"
            else boundary + timedelta(microseconds=1)
            if witness == "future"
            else boundary
        ),
    )
    store.store_inference_call(collision)
    if witness == "three_owners":
        store.store_inference_call(
            InferenceCall(
                inference_call_id="third-alias-owner",
                org_id=ORG,
                session_id="third-session",
                gateway_provider=GatewayProvider.PORTKEY,
                input_messages=[],
                output_messages=[],
                model_call_id=original.model_call_id,
                observed_at=boundary,
            )
        )
    store.store_ci_outcome(_ci_outcome(commit_sha=seeded_attribution))
    store.store_ci_outcome(_ci_outcome(outcome_id="unrelated-ci", run_id="unrelated"))
    if witness in {"quarantined", "released"}:
        store.quarantine_fact(
            ORG,
            FactTable.INFERENCE_CALLS,
            collision.inference_call_id,
            reason="excluded",
        )
        if witness == "released":
            store.release_fact(
                ORG,
                FactTable.INFERENCE_CALLS,
                collision.inference_call_id,
                reason="restored",
            )

    class AllHistorySnapshot:
        """Retain the pre-projection read populations as a response oracle."""

        def __init__(self, snapshot):
            self.snapshot = snapshot

        def __getattr__(self, name):
            return getattr(self.snapshot, name)

        def read_inference_call_summaries(self, org_id, *, inference_call_ids):
            return self.snapshot.read_inference_calls(org_id)

        def read_inference_call_identity_witnesses(
            self, org_id, *, call_ids, observed_through
        ):
            return [
                call
                for call in self.snapshot.read_inference_calls(org_id)
                if call.observed_at <= observed_through
            ]

        def read_decisions(self, org_id, *, session_ids, **kwargs):
            return self.snapshot.read_decisions(org_id, **kwargs)

        def read_session_commit_observations(
            self, org_id, *, commit_sha=None, **kwargs
        ):
            rows = self.snapshot.read_session_commit_observations(org_id, **kwargs)
            return [
                row
                for row in rows
                if commit_sha is None or row.commit_sha == commit_sha
            ]

        def read_ci_outcomes(self, org_id, *, commit_sha=None, **kwargs):
            rows = self.snapshot.read_ci_outcomes(org_id, **kwargs)
            return [
                row
                for row in rows
                if commit_sha is None or row.commit_sha == commit_sha
            ]

    class AllHistoryStore:
        @contextmanager
        def read_snapshot(self):
            with store.read_snapshot() as snapshot:
                yield AllHistorySnapshot(snapshot)

    expected = _run_query(seeded_attribution, AllHistoryStore(), as_of=boundary)
    assert expected["repos"][0]["decisions"] == (
        0 if witness in {"old", "boundary", "released", "three_owners"} else 1
    )
    assert _run_query(seeded_attribution, store, as_of=boundary) == expected

    engine = create_engine(postgres_database_factory())
    try:
        shuffled = FactStore(engine)
        # Reverse each population's arrival order, retaining exact Fact identities.
        for read_name, write_name in (
            ("read_pushes", "store_push"),
            ("read_inference_calls", "store_inference_call"),
            ("read_decisions", "store_decision"),
            ("read_ci_outcomes", "store_ci_outcome"),
            ("read_session_commit_observations", "store_session_commit_observation"),
        ):
            for fact in reversed(
                getattr(store, read_name)(ORG, include_quarantined=True)
            ):
                getattr(shuffled, write_name)(fact)
        if witness == "foreign":
            shuffled.store_inference_call(collision)
        if witness == "quarantined":
            shuffled.quarantine_fact(
                ORG,
                FactTable.INFERENCE_CALLS,
                collision.inference_call_id,
                reason="excluded",
            )
        assert _run_query(seeded_attribution, shuffled, as_of=boundary) == expected
    finally:
        engine.dispose()


@pytest.mark.parametrize("in_candidate_window", [False, True])
def test_commit_query_only_bounds_output_needed_for_attribution(
    client: TestClient, seeded_attribution: str, monkeypatch, in_candidate_window
) -> None:
    from sediment_api import workers

    store = app.state.fact_store
    [original] = store.read_inference_calls(ORG)
    unrelated = InferenceCall(
        inference_call_id="oversized-alias-source",
        org_id=ORG,
        session_id="old-unrelated-session",
        gateway_provider=GatewayProvider.LITELLM,
        model_call_id="unrelated-call",
        input_messages=[],
        output_messages=[
            InferenceMessage(role="assistant", parts=[TextPart(content="x" * 20_000)])
        ],
        observed_at=(
            original.observed_at
            if in_candidate_window
            else original.observed_at - timedelta(days=30)
        ),
    )
    store.store_inference_call(unrelated)
    monkeypatch.setattr(
        workers,
        "_WORKER_COMMAND",
        (
            sys.executable,
            "-c",
            "from sediment_core import store; "
            "store.INFERENCE_CALL_ROW_BYTES_LIMIT = 10000; "
            "from sediment_api.worker import main; raise SystemExit(main())",
        ),
    )
    response = client.get(f"/query/commit/{seeded_attribution}", headers=AUTH)
    if in_candidate_window:
        assert response.status_code == 409
        assert response.json() == {"detail": {"reason": "repository_evidence_limit"}}
    else:
        assert response.status_code == 200
        assert response.json()["repos"][0]["decisions"] == 1


def test_commit_query_refuses_excess_requested_aliases_without_partial_attachment(
    client: TestClient, seeded_attribution: str, monkeypatch
) -> None:
    from sediment_api import workers

    app.state.fact_store.store_decision(
        DeveloperDecision(
            org_id=ORG,
            session_id="sess-query",
            agent_harness=AgentHarness.CLAUDE_CODE,
            file_path="missing-call.py",
            accepted=True,
            explicit=True,
            interaction_mode=InteractionMode.AGENT,
            call_id="absent-call",
            occurred_at=datetime.now(UTC),
        )
    )
    monkeypatch.setattr(
        workers,
        "_WORKER_COMMAND",
        (
            sys.executable,
            "-c",
            "from sediment_core import store; "
            "store.COMPOSITE_FILTER_KEY_LIMIT = 1; "
            "from sediment_api.worker import main; raise SystemExit(main())",
        ),
    )
    response = client.get(f"/query/commit/{seeded_attribution}", headers=AUTH)
    assert response.status_code == 409
    assert response.json() == {"detail": {"reason": "repository_evidence_limit"}}


def test_commit_query_historical_notes_come_from_captured_observations(
    client: TestClient, seeded_attribution: str
) -> None:
    from sediment_api.config import settings
    from sediment_derive import MirrorManager
    from sediment_derive.repository_identity import LegacyRepositoryKey

    store = app.state.fact_store
    store.quarantine_fact(
        ORG,
        FactTable.SESSION_COMMIT_OBSERVATIONS,
        "seeded-session-observation",
        reason="exclude captured note evidence",
    )
    boundary = datetime.now(UTC)
    response = client.get(
        f"/query/commit/{seeded_attribution}",
        params={"as_of": boundary.isoformat()},
        headers=AUTH,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["attributed"] is False
    assert body["repos"][0]["inference_calls"][0]["attribution_source"] == "jaccard"

    mirror = MirrorManager(settings.mirror_path).open_repository(
        LegacyRepositoryKey(ORG, REPO)
    )
    assert mirror is not None
    run_git(
        mirror.path,
        "-c",
        "user.name=Dev",
        "-c",
        "user.email=dev@example.com",
        "notes",
        "--ref=sediment",
        "add",
        "-f",
        "-m",
        _note("a-later-mutable-note"),
        seeded_attribution,
    )
    repeated = client.get(
        f"/query/commit/{seeded_attribution}",
        params={"as_of": boundary.isoformat()},
        headers=AUTH,
    )
    assert repeated.status_code == 200
    assert repeated.json() == body


@pytest.mark.parametrize("observed_session", [False, True])
def test_commit_query_long_note_window_only_loads_observed_sessions(
    client: TestClient, seeded_attribution: str, monkeypatch, observed_session
) -> None:
    from sediment_api import workers

    store = app.state.fact_store
    [original] = store.read_inference_calls(ORG)
    store.store_inference_call(
        InferenceCall(
            inference_call_id="long-note-window-call",
            org_id=ORG,
            session_id=original.session_id if observed_session else "other-session",
            gateway_provider=GatewayProvider.LITELLM,
            observed_at=original.observed_at - timedelta(days=2),
            input_messages=[],
            output_messages=[
                InferenceMessage(
                    role="assistant", parts=[TextPart(content="x" * 20_000)]
                )
            ],
        )
    )
    monkeypatch.setattr(
        workers,
        "_WORKER_COMMAND",
        (
            sys.executable,
            "-c",
            "from sediment_core import store; "
            "store.INFERENCE_CALL_ROW_BYTES_LIMIT = 10000; "
            "from sediment_api.worker import main; raise SystemExit(main())",
        ),
    )
    response = client.get(f"/query/commit/{seeded_attribution}", headers=AUTH)
    if observed_session:
        assert response.status_code == 409
        assert response.json() == {"detail": {"reason": "repository_evidence_limit"}}
    else:
        assert response.status_code == 200
        assert response.json()["repos"][0]["decisions"] == 1


def test_commit_query_compacts_repeated_repository_evidence_before_cap(
    client: TestClient, seeded_attribution: str, monkeypatch
) -> None:
    from sediment_api import workers

    for index in range(20):
        app.state.fact_store.store_ci_outcome(
            _ci_outcome(outcome_id=f"history-{index}", run_id=f"history-{index}")
        )
    monkeypatch.setattr(
        workers,
        "_WORKER_COMMAND",
        (
            sys.executable,
            "-c",
            "from sediment_derive import repository_context; "
            "repository_context.REPOSITORY_IDENTITY_LIMIT = 4; "
            "from sediment_api.worker import main; raise SystemExit(main())",
        ),
    )
    response = client.get(f"/query/commit/{seeded_attribution}", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["repos"][0]["decisions"] == 1


def test_commit_query_avoids_complete_history_readers(
    client: TestClient, seeded_attribution: str, monkeypatch
) -> None:
    from sediment_api.routers.query import _run_query
    from sediment_core.store import _FactSnapshot

    def reject_complete_read(*args, **kwargs):
        pytest.fail("commit investigation selected a complete-history reader")

    for name in (
        "read_repository_identities",
        "read_repository_renames",
        "read_pushes",
        "read_inference_calls",
        "read_inference_call_identities",
    ):
        monkeypatch.setattr(_FactSnapshot, name, reject_complete_read)
    result = _run_query(seeded_attribution, app.state.fact_store)
    assert result["attributed"] is True
    assert result["repos"][0]["decisions"] == 1
