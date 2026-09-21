# SPDX-License-Identifier: AGPL-3.0-or-later
"""Repository lifetime isolation through public investigation routes."""

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from sediment_core import CIOutcome, CIProvider, CIResult, FactTable, RepositoryRename
from sediment_api.main import app

ORG = "testorg"
AUTH = {"Authorization": "Bearer test-operator-token-3a7e-2f6c"}
T0 = datetime(2026, 9, 5, 12, tzinfo=UTC)
SHA = "a" * 40
IDENTITY = {
    "repository_provider": "github",
    "repository_host": "github.com",
    "repository_id": "101",
}


def _outcome(fact_id, *, minute=0, **overrides):
    values = dict(
        outcome_id=fact_id,
        org_id=ORG,
        provider=CIProvider.GITHUB_ACTIONS,
        run_id=fact_id,
        run_attempt=1,
        repo="testorg/old",
        commit_sha=SHA,
        branch="main",
        result=CIResult.FAILED,
        workflow_name="CI",
        captured_at=T0 + timedelta(minutes=minute),
        raw={"secret": "not returned"},
        **IDENTITY,
    )
    values.update(overrides)
    fact = CIOutcome(**values)
    app.state.fact_store.store_ci_outcome(fact)
    return fact


def _window(**overrides):
    return dict(
        captured_after=(T0 - timedelta(minutes=1)).isoformat(),
        captured_before=(T0 + timedelta(hours=1)).isoformat(),
        **overrides,
    )


def _get(client, path="/query/ci/failures", **params):
    return client.get(path, params=params, headers=AUTH)


def test_ci_run_requires_selector_for_distinct_forge_hosts(client):
    _outcome("public-run", run_id="same-run")
    _outcome("private-run", run_id="same-run", repository_host="forge.example.com")
    response = _get(
        client,
        "/query/ci/outcome",
        provider="github_actions",
        run_id="same-run",
        run_attempt=1,
    )
    assert response.status_code == 409
    assert response.json() == {"detail": {"reason": "repository_selector_ambiguous"}}
    selected = _get(
        client,
        "/query/ci/outcome",
        provider="github_actions",
        run_id="same-run",
        run_attempt=1,
        **IDENTITY,
    )
    assert selected.status_code == 200
    assert selected.json()["outcome"]["outcome_id"] == "public-run"
    assert selected.json()["outcome"]["repository_identity"] == {
        "provider": "github",
        "host": "github.com",
        "repository_id": "101",
    }


def test_ci_page_follows_one_identity_across_names_without_rename_receipt(client):
    _outcome("old-ci")
    _outcome("new-ci", minute=1, repo="testorg/new")
    response = _get(client, **_window(repo="testorg/new"))
    assert response.status_code == 200
    assert [row["outcome_id"] for row in response.json()["outcomes"]] == [
        "new-ci",
        "old-ci",
    ]
    # Navigation retains the selected identity rather than a name/SHA alias.
    link = response.json()["outcomes"][0]["commit_query"]
    assert urlsplit(link).path == f"/query/commit/{SHA}"
    query = parse_qs(urlsplit(link).query)
    assert query["repository_id"] == ["101"]
    assert query["repository_host"] == ["github.com"]
    assert "raw" not in response.json()["outcomes"][0]


def test_ci_slug_reuse_refuses_pooling_and_explicit_identity_selects_lifetime(client):
    _outcome("original")
    _outcome("recreated", repository_id="303", minute=1)
    ambiguous = _get(client, **_window(repo="testorg/old"))
    assert ambiguous.status_code == 409
    exact = _get(client, **_window(**IDENTITY))
    assert exact.status_code == 200
    assert [row["outcome_id"] for row in exact.json()["outcomes"]] == ["original"]


def test_ci_rename_only_claim_qualifies_selector_through_its_boundary(client):
    _outcome("original")
    app.state.fact_store.store_repository_rename(
        RepositoryRename(
            org_id=ORG,
            **IDENTITY,
            old_repo="testorg/old",
            new_repo="testorg/new",
            captured_at=T0 + timedelta(minutes=2),
        )
    )
    before = _get(client, **_window(repo="testorg/new", as_of=T0.isoformat()))
    assert before.status_code == 200
    assert before.json()["outcomes"] == []
    after = _get(
        client,
        **_window(repo="testorg/new", as_of=(T0 + timedelta(minutes=2)).isoformat()),
    )
    assert after.status_code == 200
    assert [row["outcome_id"] for row in after.json()["outcomes"]] == ["original"]


@pytest.mark.parametrize(
    "selector",
    [
        {"repository_provider": "github"},
        {"repository_host": "github.com", "repository_id": "101"},
        {**IDENTITY, "repo": "testorg/unrelated"},
    ],
)
def test_ci_selector_requires_complete_identity_and_supported_name(client, selector):
    _outcome("original")
    response = _get(client, **_window(**selector))
    assert response.status_code == 422


def test_ci_selector_ignores_foreign_and_quarantined_claims(client):
    _outcome("original")
    _outcome("foreign", org_id="otherorg", repository_id="202")
    hidden = _outcome("hidden", repository_id="303")
    app.state.fact_store.quarantine_fact(
        ORG,
        FactTable.CI_OUTCOMES,
        hidden.outcome_id,
        reason="synthetic isolation test",
    )
    response = _get(client, **_window(repo="testorg/old"))
    assert response.status_code == 200
    assert [row["outcome_id"] for row in response.json()["outcomes"]] == ["original"]


def test_ci_cursor_binds_identity_scope_and_utc_equivalent_filters(client):
    _outcome("first")
    _outcome("second", minute=1, repo="testorg/new")
    params = _window(**IDENTITY, limit=1)
    first = _get(client, **params)
    assert first.status_code == 200
    cursor = first.json()["next_cursor"]
    assert cursor is not None
    equivalent = {
        **params,
        "cursor": cursor,
        "captured_after": "2026-09-05T12:59:00+01:00",
    }
    second = _get(client, **equivalent)
    assert second.status_code == 200
    assert [row["outcome_id"] for row in second.json()["outcomes"]] == ["first"]
    _outcome("another-host", repository_host="forge.example.com")
    changed = _get(
        client, **{**params, "cursor": cursor, "repository_host": "forge.example.com"}
    )
    assert changed.status_code == 422
    changed_time = _get(client, **{**params, "cursor": cursor, "as_of": T0.isoformat()})
    assert changed_time.status_code == 422


def test_ci_cursor_cannot_switch_to_recreated_slug(client):
    _outcome("first")
    _outcome("second", minute=1)
    params = _window(repo="testorg/old", limit=1)
    first = _get(client, **params)
    assert first.status_code == 200
    _outcome("recreated", repository_id="303", minute=2)
    response = _get(client, **{**params, "cursor": first.json()["next_cursor"]})
    assert response.status_code == 409
    assert response.json() == {"detail": {"reason": "repository_selector_ambiguous"}}


def test_ci_as_of_bounds_outcomes_in_addition_to_identity_evidence(client):
    _outcome("earlier")
    _outcome("later", minute=1)
    response = _get(client, **_window(**IDENTITY, as_of=T0.isoformat()))
    assert response.status_code == 200
    assert [row["outcome_id"] for row in response.json()["outcomes"]] == ["earlier"]


def _observation(
    *,
    identity=IDENTITY,
    observation_identity=None,
    suffix="",
    repo="testorg/old",
    session_id="session",
):
    from sediment_core import Push, SessionCommitObservation

    push = Push(
        push_id=f"push{suffix}",
        org_id=ORG,
        provider="github",
        repo=repo,
        clone_url="https://github.com/testorg/old.git",
        ref="refs/heads/main",
        before_sha="b" * 40,
        after_sha=SHA,
        captured_at=T0,
        **identity,
    )
    app.state.fact_store.store_push(push)
    observation = SessionCommitObservation(
        observation_id=f"observation{suffix}",
        org_id=ORG,
        repo=repo,
        commit_sha=SHA,
        session_id=session_id,
        source_push_id=push.push_id,
        captured_at=T0,
        **(identity if observation_identity is None else observation_identity),
    )
    app.state.fact_store.store_session_commit_observation(observation)
    return observation


def test_session_dossier_joins_renamed_ci_and_preserves_source_observation(client):
    observation = _observation()
    _outcome("renamed-ci", repo="testorg/new", minute=1)
    _outcome("fork-ci", repository_id="202", minute=1)
    response = _get(client, "/query/session/session")
    assert response.status_code == 200
    body = response.json()
    assert body["attributed_commits"][0]["session_commit_observation_ids"] == [
        observation.observation_id
    ]
    assert (
        body["attributed_commits"][0]["repository_identity"]["repository_id"] == "101"
    )
    assert [row["outcome_id"] for row in body["ci_outcomes"]] == ["renamed-ci"]
    assert body["pushes"][0]["repository_identity"]["repository_id"] == "101"


def test_commit_investigation_separates_equal_name_and_sha_lifetimes(client):
    _observation()
    _observation(identity={**IDENTITY, "repository_id": "202"}, suffix="-fork")
    _outcome("original-ci")
    _outcome("fork-ci", repository_id="202")
    response = _get(client, f"/query/commit/{SHA}")
    assert response.status_code == 200
    body = response.json()
    assert body["attributed"] is True
    groups = {row["repository_identity"]["repository_id"]: row for row in body["repos"]}
    assert set(groups) == {"101", "202"}
    assert [row["outcome_id"] for row in groups["101"]["ci_outcomes"]] == [
        "original-ci"
    ]
    assert [row["outcome_id"] for row in groups["202"]["ci_outcomes"]] == ["fork-ci"]
    assert groups["101"]["observed_sessions"][0]["session_commit_observation_ids"] == [
        "observation"
    ]
    selected = _get(client, f"/query/commit/{SHA}", **IDENTITY)
    assert selected.status_code == 200
    assert len(selected.json()["repos"]) == 1
    ambiguous = _get(client, f"/query/commit/{SHA}", repo="testorg/old")
    assert ambiguous.status_code == 409


def test_commit_investigation_follows_observation_source_push_inheritance(client):
    _observation(
        observation_identity={
            "repository_provider": None,
            "repository_host": None,
            "repository_id": None,
        }
    )
    _outcome("renamed-ci", repo="testorg/new", minute=1)
    response = _get(client, f"/query/commit/{SHA}")
    assert response.status_code == 200
    repo = response.json()["repos"][0]
    assert repo["repository_identity"]["repository_id"] == "101"
    assert repo["observed_repo_slugs"] == ["testorg/new", "testorg/old"]
    assert repo["observed_sessions"][0]["session_commit_observation_ids"] == [
        "observation"
    ]
    assert [row["outcome_id"] for row in repo["ci_outcomes"]] == ["renamed-ci"]


def test_session_unresolved_legacy_edge_is_counted_without_losing_direct_metadata(
    client,
):
    legacy = {
        "repository_provider": None,
        "repository_host": None,
        "repository_id": None,
    }
    _observation(identity=legacy)
    _outcome("later-claim", repo="testorg/old", minute=1)
    response = _get(client, "/query/session/session")
    assert response.status_code == 200
    body = response.json()
    assert body["found"] is True
    assert body["coverage"]["inference_calls"]["visible"] == 0
    assert body["attributed_commits"] == []
    assert body["ci_outcomes"] == []
    assert body["repository_skipped"] == {"repository_identity_unresolved": 1}
    assert body["session_commit_unobserved"] == 1


def test_commit_historical_boundary_excludes_future_ci_and_name_claims(client):
    _observation()
    _outcome("earlier-ci")
    _outcome("future-fork", minute=1, repository_id="202")
    response = _get(
        client, f"/query/commit/{SHA}", repo="testorg/old", as_of=T0.isoformat()
    )
    assert response.status_code == 200
    assert len(response.json()["repos"]) == 1
    assert [
        row["outcome_id"] for row in response.json()["repos"][0]["ci_outcomes"]
    ] == ["earlier-ci"]


@pytest.mark.parametrize(
    "path, params",
    [
        (
            "/query/ci/outcome",
            {"provider": "github_actions", "run_id": "run", "run_attempt": 1},
        ),
        ("/query/ci/failures", _window(repo="testorg/old")),
        (f"/query/commit/{SHA}", {}),
        ("/query/session/session", {}),
    ],
)
def test_query_requires_complete_identity_population_before_selection(
    client, monkeypatch, path, params
):
    from sediment_derive import repository_context

    _observation()
    monkeypatch.setattr(repository_context, "REPOSITORY_IDENTITY_LIMIT", 1)
    if path.startswith(("/query/commit/", "/query/session/")):
        import sys
        from sediment_api import workers

        monkeypatch.setattr(
            workers,
            "_WORKER_COMMAND",
            (
                sys.executable,
                "-c",
                "from sediment_derive import repository_context; "
                "repository_context.REPOSITORY_IDENTITY_LIMIT = 1; "
                "from sediment_api.worker import main; raise SystemExit(main())",
            ),
        )
    response = _get(client, path, **params)
    assert response.status_code == 409
    assert response.json() == {"detail": {"reason": "repository_evidence_limit"}}


def test_ci_cursor_rejects_cross_tenant_copy_and_quarantine_revision_change(
    client, monkeypatch
):
    from sediment_api.config import settings

    _outcome("first")
    _outcome("second", minute=1)
    _outcome("foreign-first", org_id="otherorg")
    _outcome("foreign-second", org_id="otherorg", minute=1)
    params = _window(**IDENTITY, limit=1)
    first = _get(client, **params)
    assert first.status_code == 200
    cursor = first.json()["next_cursor"]
    with monkeypatch.context() as m:
        m.setattr(settings, "org_id", "otherorg")
        assert _get(client, **{**params, "cursor": cursor}).status_code == 422
    app.state.fact_store.quarantine_fact(
        ORG, FactTable.CI_OUTCOMES, "first", reason="synthetic visibility change"
    )
    assert _get(client, **{**params, "cursor": cursor}).status_code == 422


def test_unresolved_exact_ci_remains_visible_without_a_guessed_repository_group(client):
    _outcome(
        "legacy", repository_provider=None, repository_host=None, repository_id=None
    )
    _outcome("identified", minute=1)
    response = _get(client, f"/query/commit/{SHA}")
    assert response.status_code == 200
    assert response.json()["unresolved_ci_outcomes"][0]["outcome_id"] == "legacy"
    assert response.json()["repository_skipped"] == {
        "repository_identity_unresolved": 1
    }
    assert response.json()["repos"][0]["ci_outcomes"][0]["outcome_id"] == "identified"


@pytest.mark.parametrize("outcome_id", [" ", "id\x00", "id\ud800", True, None])
def test_ci_cursor_validates_embedded_fact_identity_before_outcome_read(
    client, outcome_id
):
    import base64
    import json

    _outcome("first")
    _outcome("second", minute=1)
    params = _window(**IDENTITY, limit=1)
    first = _get(client, **params)
    cursor = first.json()["next_cursor"]
    values = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    values[1] = outcome_id
    modified = (
        base64.urlsafe_b64encode(json.dumps(values).encode()).decode().rstrip("=")
    )
    response = _get(client, **{**params, "cursor": modified})
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid CI outcome cursor"}
