# SPDX-License-Identifier: AGPL-3.0-or-later
"""POST /ingest/github/push → mirror refresh + attribution-derivation wiring
and derived artifacts.

Real bare remotes, real fetches (per AGENTS.md, no mocks). The contract
under test: the Push fact is stored exactly as before, the mirror refresh
runs only when SEDIMENT_MIRROR_PATH is set, the derivation trigger fires
after the refresh (a log line, not a cache — ADR 0001), and no mirror
failure — bad remote, policy rejection — ever surfaces as a 4xx/5xx
(ADR 0001: facts are the contract, the mirror is best-effort substrate).

Fixture repos set a local git identity before committing: CI runners have
none configured globally.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sediment_capture import sign_payload
from sediment_core import (
    ForgeProvider,
    GatewayProvider,
    InferenceCall,
    InferenceMessage,
    Push,
    TextPart,
)
from sediment_derive import MirrorManager
from sqlalchemy.exc import OperationalError

from sediment_api.config import settings
from sediment_api.main import app
from sediment_api.routers import forge

REPO = "acme-corp/backend-service"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def _make_remote(tmp_path: Path, *, note_body: str | None = None) -> tuple[Path, str]:
    """A work repo with one commit, pushed to a bare remote; returns the
    remote path and the head sha."""
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "dev@example.com")
    _git(work, "config", "user.name", "Dev")
    (work / "a.py").write_text("x = 1\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    head = _git(work, "rev-parse", "HEAD").strip()
    if note_body is not None:
        _git(work, "notes", "--ref=sediment", "add", "-m", note_body, head)
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    _git(work, "push", "-q", str(remote), "refs/heads/*:refs/heads/*")
    if note_body is not None:
        _git(work, "push", "-q", str(remote), "refs/notes/*:refs/notes/*")
    return remote, head


def _note(*session_ids: str) -> str:
    return json.dumps(
        {
            "v": 1,
            "sessions": [
                {
                    "tool": "cursor",
                    "session_id": session_id,
                    "stamped_at": "2026-09-06T10:00:00Z",
                }
                for session_id in session_ids
            ],
        }
    )


def _make_three_commit_remote(tmp_path: Path) -> tuple[Path, list[str]]:
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "dev@example.com")
    _git(work, "config", "user.name", "Dev")
    shas: list[str] = []
    for number in range(3):
        (work / "a.py").write_text(f"x = {number}\n")
        _git(work, "add", "-A")
        _git(work, "commit", "-q", "-m", f"commit {number}")
        sha = _git(work, "rev-parse", "HEAD").strip()
        shas.append(sha)
        _git(work, "notes", "--ref=sediment", "add", "-m", _note(f"s-{number}"), sha)
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    _git(work, "push", "-q", str(remote), "refs/heads/*:refs/heads/*")
    _git(work, "push", "-q", str(remote), "refs/notes/*:refs/notes/*")
    return remote, shas


def _push_payload(
    clone_url: str, after: str, repo: str = REPO, *, repository_id: int | None = None
) -> dict[str, Any]:
    payload = {
        "ref": "refs/heads/main",
        "before": "0" * 40,
        "after": after,
        "deleted": False,
        "forced": False,
        "repository": {"full_name": repo, "clone_url": clone_url},
    }
    if repository_id is not None:
        payload["repository"]["id"] = repository_id
    return payload


def _post_push(client: TestClient, payload: dict[str, Any]) -> httpx.Response:
    body = json.dumps(payload).encode()
    return client.post(
        "/ingest/github/push",
        content=body,
        headers={
            "X-Hub-Signature-256": sign_payload(body, settings.github_webhook_secret),
            "X-GitHub-Event": "push",
            "Content-Type": "application/json",
        },
    )


def _mirror(mirrors: Path):
    """The refreshed mirror via the package's public read-only accessor —
    not a re-derivation of MirrorManager's on-disk layout."""
    return MirrorManager(str(mirrors)).open(settings.org_id, REPO)


def _identified_mirror(mirrors: Path, push: Push):
    from sediment_core import REPOSITORY_IDENTITY_LIMIT
    from sediment_derive import build_repository_context

    boundary = datetime.now(UTC)
    with app.state.fact_store.read_snapshot() as snapshot:
        context = build_repository_context(
            snapshot.read_repository_identities(
                settings.org_id,
                captured_through=boundary,
                limit=REPOSITORY_IDENTITY_LIMIT,
            ),
            snapshot.read_repository_renames(
                settings.org_id,
                captured_through=boundary,
                limit=REPOSITORY_IDENTITY_LIMIT,
            ),
            settings.org_id,
            as_of=boundary,
        )
    return MirrorManager(str(mirrors)).open_repository(context.resolve_fact(push).key)


def test_identified_refresh_copies_retained_source_and_keeps_first_time(
    client, tmp_path, monkeypatch
):
    remote, head = _make_remote(tmp_path, note_body=_note("identity-session"))
    mirrors = tmp_path / "mirrors"
    monkeypatch.setattr(settings, "mirror_path", str(mirrors))
    monkeypatch.setattr(settings, "dev_mode", True)
    payload = _push_payload(str(remote), head, repository_id=186853002)
    first = _post_push(client, payload)
    assert first.status_code == 200
    [source] = app.state.fact_store.read_pushes(settings.org_id)
    [observation] = app.state.fact_store.read_session_commit_observations(
        settings.org_id
    )
    assert observation.source_push_id == first.json()["fact_id"] == source.push_id
    assert observation.repository_provider == source.repository_provider
    assert observation.repository_host == source.repository_host
    assert observation.repository_id == source.repository_id == "186853002"
    assert (observation.session_id, observation.commit_sha) == (
        "identity-session",
        head,
    )
    mirror = _identified_mirror(mirrors, source)
    assert mirror is not None
    assert _git(mirror.path, "rev-parse", "refs/heads/main").strip() == head
    assert _mirror(mirrors) is None
    second = _post_push(client, payload)
    assert second.json() == {"fact_id": source.push_id, "stored": False}
    assert app.state.fact_store.read_pushes(settings.org_id) == [source]
    assert app.state.fact_store.read_session_commit_observations(settings.org_id) == [
        observation
    ]


def test_identified_duplicate_uses_separate_checked_fetch_location(
    client, tmp_path, monkeypatch
):
    remote, head = _make_remote(tmp_path, note_body=_note("identity-session"))
    mirrors = tmp_path / "mirrors"
    monkeypatch.setattr(settings, "mirror_path", str(mirrors))
    monkeypatch.setattr(settings, "dev_mode", True)
    payload = _push_payload(
        str(tmp_path / "unavailable.git"), head, repository_id=186853002
    )
    first = _post_push(client, payload)
    [source] = app.state.fact_store.read_pushes(settings.org_id)
    assert app.state.fact_store.read_session_commit_observations(settings.org_id) == []
    payload["repository"]["clone_url"] = str(remote)
    second = _post_push(client, payload)
    assert second.json() == {"fact_id": first.json()["fact_id"], "stored": False}
    assert app.state.fact_store.read_pushes(settings.org_id) == [source]
    [observation] = app.state.fact_store.read_session_commit_observations(
        settings.org_id
    )
    assert observation.source_push_id == source.push_id
    assert observation.repository_id == source.repository_id
    assert _identified_mirror(mirrors, source) is not None


def test_duplicate_name_needs_retained_evidence_before_refresh(
    client, tmp_path, monkeypatch, caplog
):
    remote, head = _make_remote(tmp_path, note_body=_note("identity-session"))
    mirrors = tmp_path / "mirrors"
    monkeypatch.setattr(settings, "mirror_path", str(mirrors))
    monkeypatch.setattr(settings, "dev_mode", True)
    payload = _push_payload(
        str(tmp_path / "unavailable.git"), head, OLD_REPO, repository_id=186853002
    )
    first = _post_push(client, payload)
    [source] = app.state.fact_store.read_pushes(settings.org_id)
    payload["repository"].update(full_name=NEW_REPO, clone_url=str(remote))
    declined = _post_push(client, payload)
    assert declined.json() == {"fact_id": first.json()["fact_id"], "stored": False}
    assert app.state.fact_store.read_session_commit_observations(settings.org_id) == []
    assert "repository_mirror_identity_unresolved" in caplog.text
    rename = _post_repository(client, _repo_rename_payload("old-service", NEW_REPO))
    assert rename.json()["stored"] is True
    accepted = _post_push(client, payload)
    assert accepted.json() == declined.json()
    assert app.state.fact_store.read_pushes(settings.org_id) == [source]
    [observation] = app.state.fact_store.read_session_commit_observations(
        settings.org_id
    )
    assert observation.repo == OLD_REPO
    assert observation.source_push_id == source.push_id
    assert observation.repository_id == source.repository_id


def test_known_competing_lifetime_keeps_existing_refs_and_observations(
    client, tmp_path, monkeypatch, caplog
):
    remote, head = _make_remote(tmp_path, note_body=_note("identity-session"))
    mirrors = tmp_path / "mirrors"
    monkeypatch.setattr(settings, "mirror_path", str(mirrors))
    monkeypatch.setattr(settings, "dev_mode", True)
    _post_push(client, _push_payload(str(remote), head, repository_id=186853002))
    [source] = app.state.fact_store.read_pushes(settings.org_id)
    prior_observations = app.state.fact_store.read_session_commit_observations(
        settings.org_id
    )
    mirror = _identified_mirror(mirrors, source)
    assert mirror is not None
    work = tmp_path / "work"
    (work / "a.py").write_text("x = 2\n")
    _git(work, "add", "a.py")
    _git(work, "commit", "-q", "-m", "replacement")
    replacement = _git(work, "rev-parse", "HEAD").strip()
    _git(
        work,
        "notes",
        "--ref=sediment",
        "add",
        "-m",
        _note("replacement-session"),
        replacement,
    )
    _git(
        work,
        "push",
        "-q",
        str(remote),
        "refs/heads/*:refs/heads/*",
        "refs/notes/*:refs/notes/*",
    )
    competing = _post_push(
        client, _push_payload(str(remote), replacement, repository_id=186853003)
    )
    assert competing.json()["stored"] is True
    refresh = _post_push(
        client, _push_payload(str(remote), replacement, repository_id=186853002)
    )
    assert refresh.json()["stored"] is True
    assert _git(mirror.path, "rev-parse", "refs/heads/main").strip() == head
    assert (
        app.state.fact_store.read_session_commit_observations(settings.org_id)
        == prior_observations
    )
    assert "repository_mirror_identity_unresolved" in caplog.text
    assert _mirror(mirrors) is None


def test_missing_identity_cannot_create_legacy_mirror_at_identified_name(
    client, tmp_path, monkeypatch, caplog
):
    remote, head = _make_remote(tmp_path, note_body=_note("identity-session"))
    mirrors = tmp_path / "mirrors"
    monkeypatch.setattr(settings, "mirror_path", str(mirrors))
    monkeypatch.setattr(settings, "dev_mode", True)
    _post_push(client, _push_payload(str(remote), head, repository_id=186853002))
    prior = app.state.fact_store.read_session_commit_observations(settings.org_id)
    response = _post_push(client, _push_payload(str(remote), head))
    assert response.json()["stored"] is True
    assert _mirror(mirrors) is None
    assert (
        app.state.fact_store.read_session_commit_observations(settings.org_id) == prior
    )
    assert "repository_identity_unresolved" in caplog.text


def test_forks_with_equal_git_objects_capture_separate_session_edges(
    client, tmp_path, monkeypatch
):
    remote, head = _make_remote(tmp_path, note_body=_note("shared-session"))
    fork = tmp_path / "fork.git"
    _git(tmp_path, "clone", "--mirror", str(remote), str(fork))
    mirrors = tmp_path / "mirrors"
    monkeypatch.setattr(settings, "mirror_path", str(mirrors))
    monkeypatch.setattr(settings, "dev_mode", True)
    for name, location, identity in [
        (REPO, remote, 186853002),
        ("fork/service", fork, 186853003),
    ]:
        assert (
            _post_push(
                client, _push_payload(str(location), head, name, repository_id=identity)
            ).json()["stored"]
            is True
        )
    sources = app.state.fact_store.read_pushes(settings.org_id)
    observations = app.state.fact_store.read_session_commit_observations(
        settings.org_id
    )
    assert len(observations) == len(sources) == 2
    assert {item.repository_id for item in observations} == {"186853002", "186853003"}
    assert len({_identified_mirror(mirrors, source).path for source in sources}) == 2
    for observation in observations:
        [source] = [
            item for item in sources if item.push_id == observation.source_push_id
        ]
        assert observation.repository_id == source.repository_id
        assert observation.session_id == "shared-session"


@pytest.mark.parametrize("failure", ["database", "limit", "invalid"])
def test_context_read_failure_preserves_push_receipt_without_git(
    client, tmp_path, monkeypatch, caplog, failure
):
    from sediment_core.store import OperationalReportLimitExceeded, _FactSnapshot

    remote, head = _make_remote(tmp_path, note_body=_note("identity-session"))
    mirrors = tmp_path / "mirrors"
    monkeypatch.setattr(settings, "mirror_path", str(mirrors))
    monkeypatch.setattr(settings, "dev_mode", True)

    def fail(*args, **kwargs):
        if failure == "limit":
            raise OperationalReportLimitExceeded("private identity diagnostic")
        if failure == "invalid":
            raise ValueError("private identity diagnostic")
        raise OperationalError(
            "private statement", {}, Exception("private identity diagnostic")
        )

    monkeypatch.setattr(_FactSnapshot, "read_repository_identities", fail)
    push = forge.parse_push(
        _push_payload(str(remote), head, repository_id=186853002),
        org_id=settings.org_id,
        github_host=settings.github_host,
    )
    receipt = app.state.fact_store.store_push_receipt(push)
    forge._refresh_mirror(receipt.fact, app.state.fact_store)
    assert receipt.stored is True
    assert len(app.state.fact_store.read_pushes(settings.org_id)) == 1
    assert app.state.fact_store.read_session_commit_observations(settings.org_id) == []
    assert not mirrors.exists()
    expected = {
        "database": "repository_context_database_unavailable",
        "limit": "repository_context_declined reason=population_limit count=1",
        "invalid": "repository_context_declined reason=invalid_population count=1",
    }[failure]
    assert expected in caplog.text
    assert "private identity diagnostic" not in caplog.text


def test_identity_snapshot_closes_before_git_observation(client, tmp_path, monkeypatch):
    from sediment_core.store import _FactSnapshot

    remote, head = _make_remote(tmp_path, note_body=_note("identity-session"))
    monkeypatch.setattr(settings, "mirror_path", str(tmp_path / "mirrors"))
    monkeypatch.setattr(settings, "dev_mode", True)
    connections = []
    original_read = _FactSnapshot.read_repository_identities
    original_capture = forge._capture_session_commit_observations

    def read(snapshot, *args, **kwargs):
        connections.append(snapshot._connection)
        return original_read(snapshot, *args, **kwargs)

    def capture(*args, **kwargs):
        assert connections and all(connection.closed for connection in connections)
        return original_capture(*args, **kwargs)

    monkeypatch.setattr(_FactSnapshot, "read_repository_identities", read)
    monkeypatch.setattr(forge, "_capture_session_commit_observations", capture)
    push = forge.parse_push(
        _push_payload(str(remote), head, repository_id=186853002),
        org_id=settings.org_id,
        github_host=settings.github_host,
    )
    receipt = app.state.fact_store.store_push_receipt(push)
    forge._refresh_mirror(receipt.fact, app.state.fact_store)
    assert receipt.stored is True
    assert connections
    assert (
        len(app.state.fact_store.read_session_commit_observations(settings.org_id)) == 1
    )


def test_push_refreshes_mirror_when_configured(
    client: TestClient, tmp_path: Path, monkeypatch
) -> None:
    remote, head = _make_remote(tmp_path)
    mirrors = tmp_path / "mirrors"
    monkeypatch.setattr(settings, "mirror_path", str(mirrors))
    # dev posture: the fixture remote is a local path, which production
    # enforcement rightly refuses (covered separately below).
    monkeypatch.setattr(settings, "dev_mode", True)

    resp = _post_push(client, _push_payload(str(remote), head))
    assert resp.status_code == 200
    assert resp.json()["stored"] is True  # fact behavior unchanged

    # The mirror refresh runs as a background task; TestClient drives the full
    # ASGI cycle (background tasks included) before returning, so it is done.
    mirror = _mirror(mirrors)
    assert mirror is not None
    assert _git(mirror.path, "rev-parse", "refs/heads/main").strip() == head


def test_successful_refresh_captures_git_note_session_edges_once(
    client: TestClient, tmp_path: Path, monkeypatch
) -> None:
    remote, head = _make_remote(
        tmp_path, note_body=_note("session-2", "session-1", "session-1")
    )
    mirrors = tmp_path / "mirrors"
    monkeypatch.setattr(settings, "mirror_path", str(mirrors))
    monkeypatch.setattr(settings, "dev_mode", True)

    first = _post_push(client, _push_payload(str(remote), head))
    second = _post_push(client, _push_payload(str(remote), head))

    assert first.status_code == second.status_code == 200
    observations = app.state.fact_store.read_session_commit_observations(
        settings.org_id
    )
    assert [(row.commit_sha, row.session_id) for row in observations] == [
        (head, "session-1"),
        (head, "session-2"),
    ]
    assert {row.source_push_id for row in observations} == {first.json()["fact_id"]}


def test_redelivery_after_failed_refresh_uses_the_stored_push_identity(
    client: TestClient, tmp_path: Path, monkeypatch
) -> None:
    remote, head = _make_remote(tmp_path, note_body=_note("session-1"))
    unavailable = tmp_path / "unavailable.git"
    remote.rename(unavailable)
    mirrors = tmp_path / "mirrors"
    monkeypatch.setattr(settings, "mirror_path", str(mirrors))
    monkeypatch.setattr(settings, "dev_mode", True)
    payload = _push_payload(str(remote), head)

    first = _post_push(client, payload)
    unavailable.rename(remote)
    second = _post_push(client, payload)

    assert first.json()["stored"] is True
    assert second.json()["stored"] is False
    [observation] = app.state.fact_store.read_session_commit_observations(
        settings.org_id
    )
    assert observation.source_push_id == first.json()["fact_id"]


def test_malformed_note_records_no_session_commit_observation(
    client: TestClient, tmp_path: Path, monkeypatch
) -> None:
    remote, head = _make_remote(tmp_path, note_body="not-json")
    monkeypatch.setattr(settings, "mirror_path", str(tmp_path / "mirrors"))
    monkeypatch.setattr(settings, "dev_mode", True)

    response = _post_push(client, _push_payload(str(remote), head))

    assert response.status_code == 200
    assert app.state.fact_store.read_session_commit_observations(settings.org_id) == []


def test_observation_capture_uses_the_push_commit_bound(
    client: TestClient, tmp_path: Path, monkeypatch
) -> None:
    remote, shas = _make_three_commit_remote(tmp_path)
    mirrors = MirrorManager(str(tmp_path / "mirrors"))
    push = Push(
        push_id="bounded-push",
        org_id=settings.org_id,
        provider=ForgeProvider.GITHUB,
        repo=REPO,
        clone_url=str(remote),
        ref="refs/heads/main",
        before_sha=shas[0],
        after_sha=shas[2],
    )
    app.state.fact_store.store_push(push)
    mirror = mirrors.ensure(push)

    observations = forge._capture_session_commit_observations(
        push, mirror, push.push_id, max_commits=1
    )
    forge._store_session_commit_observations(push, observations, app.state.fact_store)

    observations = app.state.fact_store.read_session_commit_observations(
        settings.org_id
    )
    assert [(row.commit_sha, row.session_id) for row in observations] == [
        (shas[2], "s-2")
    ]


def test_refresh_keeps_notes_stable_until_its_observation_pass_finishes(
    tmp_path: Path, monkeypatch, postgres_store
) -> None:
    remote, head = _make_remote(tmp_path, note_body=_note("session-1"))
    monkeypatch.setattr(settings, "mirror_path", str(tmp_path / "mirrors"))
    monkeypatch.setattr(settings, "dev_mode", True)
    first = Push(
        push_id="first-push",
        org_id=settings.org_id,
        provider=ForgeProvider.GITHUB,
        repo=REPO,
        clone_url=str(remote),
        ref="refs/heads/main",
        before_sha="0" * 40,
        after_sha=head,
    )
    second = first.model_copy(
        update={"push_id": "second-push", "ref": "refs/heads/other"}
    )
    postgres_store.store_push(first)
    postgres_store.store_push(second)
    original_capture = forge._capture_session_commit_observations
    second_started = threading.Event()
    second_capture_started = threading.Event()
    threads: list[threading.Thread] = []

    def capture(push, mirror, source_push_id, *, max_commits):
        if push.push_id == first.push_id:
            work = tmp_path / "work"
            _git(
                work,
                "notes",
                "--ref=sediment",
                "add",
                "-f",
                "-m",
                _note("session-2"),
                head,
            )
            _git(work, "push", "-q", "-f", str(remote), "refs/notes/*:refs/notes/*")

            def refresh_second() -> None:
                second_started.set()
                forge._refresh_mirror(second, postgres_store)

            thread = threading.Thread(target=refresh_second)
            threads.append(thread)
            thread.start()
            assert second_started.wait(timeout=1)
            assert not second_capture_started.wait(timeout=0.1)
        else:
            second_capture_started.set()
        return original_capture(push, mirror, source_push_id, max_commits=max_commits)

    monkeypatch.setattr(forge, "_capture_session_commit_observations", capture)

    forge._refresh_mirror(first, postgres_store)

    assert second_capture_started.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=1)
    observations = postgres_store.read_session_commit_observations(settings.org_id)
    assert {row.session_id: row.source_push_id for row in observations} == {
        "session-1": first.push_id,
        "session-2": second.push_id,
    }


def test_blocked_observation_store_does_not_hold_the_mirror_lock(
    tmp_path: Path, monkeypatch, postgres_store
) -> None:
    remote, head = _make_remote(tmp_path, note_body=_note("session-1"))
    mirror_path = tmp_path / "mirrors"
    monkeypatch.setattr(settings, "mirror_path", str(mirror_path))
    monkeypatch.setattr(settings, "dev_mode", True)
    push = Push(
        push_id="blocked-store-push",
        org_id=settings.org_id,
        provider=ForgeProvider.GITHUB,
        repo=REPO,
        clone_url=str(remote),
        ref="refs/heads/main",
        before_sha="0" * 40,
        after_sha=head,
    )
    postgres_store.store_push(push)
    store_entered = threading.Event()
    allow_store = threading.Event()
    snapshot_acquired = threading.Event()
    original_store = postgres_store.store_session_commit_observation

    def store_observation(observation):
        store_entered.set()
        assert allow_store.wait(timeout=5)
        return original_store(observation)

    monkeypatch.setattr(
        postgres_store, "store_session_commit_observation", store_observation
    )
    refresh_thread = threading.Thread(
        target=forge._refresh_mirror, args=(push, postgres_store)
    )
    refresh_thread.start()
    assert store_entered.wait(timeout=5)

    def read_snapshot() -> None:
        with MirrorManager(str(mirror_path)).read_snapshot(settings.org_id, [REPO]):
            snapshot_acquired.set()

    snapshot_thread = threading.Thread(target=read_snapshot)
    snapshot_thread.start()
    try:
        assert snapshot_acquired.wait(timeout=0.2)
    finally:
        allow_store.set()
        refresh_thread.join(timeout=5)
        snapshot_thread.join(timeout=5)


@pytest.mark.parametrize("identified", [False, True])
def test_push_pokes_attribution_derivation_and_logs_counts(
    client: TestClient, tmp_path: Path, monkeypatch, caplog, identified
) -> None:
    # After the mirror refresh, the background
    # task runs the attribution derivation for the pushed repo and logs
    # structured counts. Trigger + log line only — nothing is persisted.
    remote, head = _make_remote(
        tmp_path, note_body=_note("sess-1") if identified else None
    )
    mirrors = tmp_path / "mirrors"
    monkeypatch.setattr(settings, "mirror_path", str(mirrors))
    monkeypatch.setattr(settings, "dev_mode", True)
    # A completion whose text matches the pushed a.py exactly: one jaccard hit.
    store = app.state.fact_store
    store.store_inference_call(
        InferenceCall(
            org_id=settings.org_id,
            session_id="sess-1",
            user_id="dev",
            gateway_provider=GatewayProvider.LITELLM,
            model="claude-sonnet-5",
            input_messages=[],
            output_messages=[
                InferenceMessage(role="assistant", parts=[TextPart(content="x = 1\n")])
            ],
            input_tokens=1,
            output_tokens=1,
            duration_ms=1,
        )
    )

    with caplog.at_level(logging.INFO, logger="sediment.api.forge"):
        resp = _post_push(
            client,
            _push_payload(str(remote), head, repository_id=101 if identified else None),
        )
    assert resp.status_code == 200

    derived = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("attributions_derived")
    ]
    assert derived == [
        f"attributions_derived repo={REPO} attributed=1 notes={int(identified)} jaccard={int(not identified)}"
    ]
    # Trigger only, no cache: the store still holds exactly the two facts.
    store = app.state.fact_store
    assert len(store.read_pushes(settings.org_id)) == 1
    assert len(store.read_inference_calls(settings.org_id)) == 1


def test_background_database_failure_does_not_log_driver_diagnostics(
    tmp_path: Path, monkeypatch, postgres_store, caplog
) -> None:
    sentinel = "sentinel-driver-diagnostic"

    def fail(*args, **kwargs):
        raise OperationalError("SELECT secret", {}, Exception(sentinel))

    monkeypatch.setattr(forge, "derive_attribution_result", fail)
    push = Push(
        org_id=settings.org_id,
        provider=ForgeProvider.GITHUB,
        repo=REPO,
        ref="refs/heads/main",
        before_sha="0" * 40,
        after_sha="a" * 40,
        clone_url="https://example.com/repo.git",
    )

    with caplog.at_level(logging.ERROR, logger="sediment.api.forge"):
        forge._derive_push_attributions(
            push,
            MirrorManager(str(tmp_path / "mirrors")),
            postgres_store,
        )

    assert "attribution_derivation_database_unavailable" in caplog.text
    assert sentinel not in caplog.text
    assert "SELECT secret" not in caplog.text


def test_observation_bug_does_not_escape_the_background_chain(
    tmp_path: Path, monkeypatch, postgres_store, caplog
) -> None:
    remote, head = _make_remote(tmp_path)
    monkeypatch.setattr(settings, "mirror_path", str(tmp_path / "mirrors"))
    monkeypatch.setattr(settings, "dev_mode", True)
    push = Push(
        org_id=settings.org_id,
        provider=ForgeProvider.GITHUB,
        repo=REPO,
        ref="refs/heads/main",
        before_sha="0" * 40,
        after_sha=head,
        clone_url=str(remote),
    )
    postgres_store.store_push(push)

    def fail(*args, **kwargs):
        raise RuntimeError("capture bug")

    monkeypatch.setattr(forge, "_capture_session_commit_observations", fail)

    with caplog.at_level(logging.ERROR, logger="sediment.api.forge"):
        forge._refresh_mirror(push, postgres_store)

    assert "session_commit_observation_error" in caplog.text


def test_push_mirror_failure_is_never_an_error_response(
    client: TestClient, tmp_path: Path, monkeypatch
) -> None:
    # A remote git cannot fetch: the fact must still store and the route
    # must still 200 — refresh failure is logged, never surfaced.
    mirrors = tmp_path / "mirrors"
    monkeypatch.setattr(settings, "mirror_path", str(mirrors))
    monkeypatch.setattr(settings, "dev_mode", True)

    payload = _push_payload(str(tmp_path / "no-such-remote.git"), "a" * 40)
    resp = _post_push(client, payload)
    assert resp.status_code == 200
    assert resp.json()["stored"] is True
    assert len(app.state.fact_store.read_pushes(settings.org_id)) == 1
    assert app.state.fact_store.read_session_commit_observations(settings.org_id) == []


def test_push_production_policy_rejects_local_clone_url_quietly(
    client: TestClient, tmp_path: Path, monkeypatch
) -> None:
    # Production posture: a local-path clone_url is refused by MirrorPolicy
    # before any git runs — nothing on disk, and still a clean 200 with the
    # fact stored. Pin dev_mode off rather than assume the ambient env (a
    # developer running with SEDIMENT_DEV_MODE=true must not flip this test).
    monkeypatch.setattr(settings, "dev_mode", False)
    mirrors = tmp_path / "mirrors"
    monkeypatch.setattr(settings, "mirror_path", str(mirrors))

    resp = _post_push(client, _push_payload("/local/repo.git", "a" * 40))
    assert resp.status_code == 200
    assert resp.json()["stored"] is True
    assert not mirrors.exists()  # rejected before init — no trace


def test_push_without_mirror_path_stores_fact_only(
    client: TestClient, tmp_path: Path, monkeypatch
) -> None:
    # Default deployment: SEDIMENT_MIRROR_PATH unset → no mirror side effects
    # at all, identical fact behavior. Pin mirror_path None (don't assume the
    # ambient env leaves it unset).
    monkeypatch.setattr(settings, "mirror_path", None)

    resp = _post_push(client, _push_payload("/local/repo.git", "a" * 40))
    assert resp.status_code == 200
    assert resp.json()["stored"] is True
    assert not (tmp_path / "mirrors").exists()
    assert len(app.state.fact_store.read_pushes(settings.org_id)) == 1


def test_repo_less_push_stores_fact_but_skips_mirror(
    client: TestClient, tmp_path: Path, monkeypatch
) -> None:
    # parse_push stores a push whose payload omits repository.full_name (facts
    # first), with repo="". The route must NOT refresh: repo="" collapses
    # distinct repos into one shared mirror dir. Fact stored, no mirror dir.
    mirrors = tmp_path / "mirrors"
    monkeypatch.setattr(settings, "mirror_path", str(mirrors))
    monkeypatch.setattr(settings, "dev_mode", True)

    payload = {
        "ref": "refs/heads/main",
        "before": "0" * 40,
        "after": "a" * 40,
        "deleted": False,
        "forced": False,
        # no "repository" key → parse_push yields repo="" / clone_url=""
    }
    resp = _post_push(client, payload)
    assert resp.status_code == 200
    assert resp.json()["stored"] is True
    assert not mirrors.exists()
    pushes = app.state.fact_store.read_pushes(settings.org_id)
    assert len(pushes) == 1
    assert pushes[0].repo == ""


OLD_REPO = "acme-corp/old-service"
NEW_REPO = "acme-corp/new-service"


def _repo_rename_payload(old_name: str, new_full_name: str) -> dict[str, Any]:
    return {
        "action": "renamed",
        "changes": {"repository": {"name": {"from": old_name}}},
        "repository": {"full_name": new_full_name, "id": 186853002},
    }


def _post_repository(
    client: TestClient, payload: dict[str, Any], event: str = "repository"
) -> httpx.Response:
    body = json.dumps(payload).encode()
    return client.post(
        "/ingest/github/repository",
        content=body,
        headers={
            "X-Hub-Signature-256": sign_payload(body, settings.github_webhook_secret),
            "X-GitHub-Event": event,
            "Content-Type": "application/json",
        },
    )


def test_repository_rename_preserves_legacy_mirror_without_aliasing(
    client: TestClient, tmp_path: Path, monkeypatch
) -> None:
    """A captured rename never assigns provider identity to legacy Git objects."""
    remote, head = _make_remote(tmp_path)
    mirrors = tmp_path / "mirrors"
    monkeypatch.setattr(settings, "mirror_path", str(mirrors))
    monkeypatch.setattr(settings, "dev_mode", True)
    response = _post_push(client, _push_payload(str(remote), head, OLD_REPO))
    assert response.json()["stored"] is True
    original = MirrorManager(str(mirrors)).open(settings.org_id, OLD_REPO)
    assert original is not None
    response = _post_repository(
        client, _repo_rename_payload("Old-Service", "Acme-Corp/New-Service")
    )
    assert response.json()["stored"] is True
    [rename] = app.state.fact_store.read_repository_renames(settings.org_id)
    assert (rename.old_repo, rename.new_repo) == (OLD_REPO, NEW_REPO)
    assert original.path.exists()
    assert _git(original.path, "rev-parse", "refs/heads/main").strip() == head
    assert MirrorManager(str(mirrors)).open(settings.org_id, NEW_REPO) is None
    # A later legacy-shaped Push cannot inherit the rename's identity.
    later = _post_push(client, _push_payload(str(remote), head, NEW_REPO))
    assert later.json()["stored"] is True
    assert MirrorManager(str(mirrors)).open(settings.org_id, NEW_REPO) is None


def test_repository_rename_stores_without_existing_mirror(
    client: TestClient, tmp_path: Path, monkeypatch
) -> None:
    mirrors = tmp_path / "mirrors"
    monkeypatch.setattr(settings, "mirror_path", str(mirrors))
    response = _post_repository(client, _repo_rename_payload("old-service", NEW_REPO))
    assert response.json()["stored"] is True
    [rename] = app.state.fact_store.read_repository_renames(settings.org_id)
    assert rename.rename_id == response.json()["fact_id"]
    assert not mirrors.exists()


def test_repository_non_renamed_action_is_skipped(client: TestClient) -> None:
    response = _post_repository(
        client, {"action": "created", "repository": {"full_name": NEW_REPO}}
    )
    assert response.status_code == 200
    assert response.json() == {"skipped": True, "reason": "unsupported_discriminator"}


def test_repository_wrong_event_type_is_skipped(client: TestClient) -> None:
    response = _post_repository(client, {"action": "renamed"}, event="push")
    assert response.status_code == 200
    assert response.json()["skipped"] is True


def test_repository_rename_stores_without_mirror_configuration(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "mirror_path", None)
    response = _post_repository(client, _repo_rename_payload("old-service", NEW_REPO))
    assert response.status_code == 200
    assert response.json()["stored"] is True
    [rename] = app.state.fact_store.read_repository_renames(settings.org_id)
    assert rename.rename_id == response.json()["fact_id"]


def test_captured_pipeline_conformance_preserves_sources_and_training(
    client: TestClient, tmp_path: Path, monkeypatch, postgres_database_factory
) -> None:
    """Synthetic capture survives storage and both artifacts.

    Git creates the patch and note; authenticated routes create every Fact.
    One unsupported training string remains lossless in the canonical bundle.
    Reversing stored Fact arrival preserves the exact artifact and row bytes.
    """
    from datetime import timedelta
    from sqlalchemy import create_engine
    from sediment_core import FactStore, FactTable
    from sediment_derive import split_of
    from sediment_export import (
        SFTPolicy,
        OperationalReportScope,
        generate_accepted_work_lifecycle_report,
        VerifierCommands,
        build_derived_bundle,
        project_sediment_rollouts,
        project_sediment_tasks,
        project_sft,
        read_derived_bundle,
        write_derived_bundle,
    )
    from sediment_export.jsonl import write_jsonl
    from sediment_export.sft import to_export_rows

    prompt = '[{"type":"text","text":"literal, not an encoded message"}]\nλ\x00'
    completion = 'counter = 1\n++counter\nlabel = "λ"\n'
    session_id = "foundation-corpus"
    remote, base = _make_remote(tmp_path)
    work = tmp_path / "work"
    (work / "a.py").write_text(completion, encoding="utf-8")
    _git(work, "add", "a.py")
    _git(work, "commit", "-q", "-m", "synthetic foundation change")
    head = _git(work, "rev-parse", "HEAD").strip()
    _git(work, "notes", "--ref=sediment", "add", "-m", _note(session_id), head)
    _git(work, "push", "-q", str(remote), "refs/heads/*:refs/heads/*")
    _git(work, "push", "-q", str(remote), "refs/notes/*:refs/notes/*")
    mirrors = MirrorManager(tmp_path / "mirrors")
    monkeypatch.setattr(settings, "mirror_path", str(tmp_path / "mirrors"))
    monkeypatch.setattr(settings, "dev_mode", True)
    auth = {"Authorization": f"Bearer {settings.api_bearer_token}"}
    call_ids = []
    for index, output in enumerate((completion, completion, completion + "\ud800")):
        body = {
            "provider": "litellm",
            "session_id": session_id,
            "user_id": "corpus-developer",
            "payload": {
                "litellm_call_id": f"corpus-call-{index}",
                "model": "corpus-model",
                "messages": [{"role": "user", "content": prompt}],
                "response": {
                    "choices": [{"message": {"role": "assistant", "content": output}}]
                },
            },
        }
        response = client.post(
            "/ingest/gateway",
            content=json.dumps(body),
            headers={**auth, "Content-Type": "application/json"},
        )
        assert response.status_code == 200
        assert response.json()["stored"] is True
        call_ids.append(response.json()["fact_id"])
    first_call = app.state.fact_store.read_inference_calls(settings.org_id)[0]
    decision_payload = {
        "resourceLogs": [
            {
                "scopeLogs": [
                    {
                        "logRecords": [
                            {
                                "body": {"stringValue": "sediment.tool_decision"},
                                "timeUnixNano": str(
                                    int(
                                        first_call.observed_at.timestamp()
                                        * 1_000_000_000
                                    )
                                ),
                                "attributes": [
                                    {"key": key, "value": {"stringValue": value}}
                                    for key, value in {
                                        "agent": "pi",
                                        "session.id": session_id,
                                        "tool_use_id": "corpus-call-0",
                                        "tool_name": "write",
                                        "decision": "accept",
                                        "file_path": "a.py",
                                    }.items()
                                ]
                                + [{"key": "explicit", "value": {"boolValue": True}}],
                            }
                        ]
                    }
                ]
            }
        ]
    }
    assert (
        client.post("/v1/logs", json=decision_payload, headers=auth).status_code == 200
    )
    [decision] = app.state.fact_store.read_decisions(settings.org_id)
    assert decision.call_id == "corpus-call-0"
    push = _post_push(client, _push_payload(str(remote), head))
    assert push.status_code == 200
    merge_body = json.dumps(
        {
            "action": "closed",
            "repository": {"full_name": REPO},
            "pull_request": {
                "number": 1,
                "merged": True,
                "merged_at": first_call.observed_at.isoformat(),
                "merge_commit_sha": head,
                "head": {"ref": "feature", "sha": head, "repo": {"full_name": REPO}},
                "base": {"ref": "main", "sha": base},
            },
        }
    ).encode()
    merge = client.post(
        "/ingest/github/pull-request",
        content=merge_body,
        headers={
            "X-Hub-Signature-256": sign_payload(
                merge_body, settings.github_webhook_secret
            ),
            "X-GitHub-Event": "pull_request",
            "Content-Type": "application/json",
        },
    )
    assert merge.status_code == 200
    assert merge.json()["stored"] is True
    ci = client.post(
        "/ingest/ci",
        json={
            "provider": "jenkins",
            "run_id": "corpus-run",
            "repo": REPO,
            "commit_sha": head,
            "branch": "main",
            "result": "passed",
            "workflow_id": "corpus-workflow",
            "workflow_name": "Corpus tests",
        },
        headers=auth,
    )
    assert ci.status_code == 200
    store = app.state.fact_store
    [observation] = store.read_session_commit_observations(settings.org_id)
    assert (
        observation.org_id,
        observation.repo,
        observation.commit_sha,
        observation.session_id,
        observation.source_push_id,
    ) == (settings.org_id, REPO, head, session_id, push.json()["fact_id"])
    captured = build_derived_bundle(store, mirrors, settings.org_id)
    assert [call.inference_call_id for call in captured.inference_calls] == call_ids
    assert [
        row.inference_call_id for row in captured.attributed_completions
    ] == call_ids[:1]
    assert len(captured.rollouts) == 1
    assert captured.fragmented == {"prior_output_not_replayed": 2}
    assert captured.skipped == {"attributed_completion.abandonment.reached_a_commit": 1}
    assert captured.excluded == {}
    assert [len(segment) for segment in captured.rollouts[0].segments] == [1, 1, 1]
    for artifact in (*captured.attributed_completions, *captured.rollouts):
        assert artifact.session_commit_observations == (observation,)
        assert artifact.split == split_of(session_id, captured.policy.eval_fraction)
        assert artifact.provenance.policy_digest == captured.policy.digest
        assert artifact.provenance.quarantine_revision == 0
    assert {
        row.provenance.policy_version for row in captured.attributed_completions
    } == {"5"}
    assert captured.rollouts[0].provenance.policy_version == "4"

    populations = [captured]
    engine = create_engine(postgres_database_factory())
    try:
        reversed_store = FactStore(engine)
        reversed_store.store_decision(decision)
        for fact in reversed(store.read_ci_outcomes(settings.org_id)):
            reversed_store.store_ci_outcome(fact)
        reversed_store.store_session_commit_observation(observation)
        for fact in reversed(store.read_pushes(settings.org_id)):
            reversed_store.store_push(fact)
        # Bundle identity evidence includes PR roles outside the artifact cohort.
        for fact in reversed(store.read_pull_request_merges(settings.org_id)):
            reversed_store.store_pull_request_merge(fact)
        for fact in reversed(store.read_inference_calls(settings.org_id)):
            reversed_store.store_inference_call(fact)
        populations.append(
            build_derived_bundle(reversed_store, mirrors, settings.org_id)
        )
    finally:
        engine.dispose()
    bundle_bytes, training_bytes = [], []
    for index, bundle in enumerate(populations):
        assert bundle == captured
        destination = tmp_path / f"bundle-{index}"
        write_derived_bundle(bundle, destination)
        restored = read_derived_bundle(destination)
        assert restored == captured
        assert restored.inference_calls[-1].output_messages[0].parts[0].content == (
            completion + "\ud800"
        )
        bundle_bytes.append({p.name: p.read_bytes() for p in destination.iterdir()})
        calls = {call.inference_call_id: call for call in restored.inference_calls}
        sft = project_sft(
            restored.attributed_completions, calls, SFTPolicy(recipe_id="sft_verified")
        )
        assert sft.skipped == {}
        assert {row.metadata.completion_id for row in sft.rows} == set(call_ids[:1])
        for row in sft.rows:
            assert row.prompt[0]["content"] == prompt
            assert row.completion[0]["content"] == completion
            assert row.metadata.recipe_version == 1
            assert row.metadata.eligibility_source == "resolved_ci_pass"
            assert row.metadata.session_commit_observation_ids == (
                observation.observation_id,
            )
        rollouts = project_sediment_rollouts(restored.rollouts)
        assert len(rollouts.rows) == 2
        assert rollouts.skipped == {"unrepresentable_unicode": 1}
        tasks = project_sediment_tasks(
            restored.rollouts, mirrors, VerifierCommands.empty()
        )
        assert len(tasks.rows) == 1
        assert tasks.skipped == {}
        assert tasks.rows[0].body["problem_statement"] == prompt
        assert tasks.rows[0].body["recipe_version"] == 1
        assert tasks.rows[0].body["session_commit_observation_ids"] == (
            observation.observation_id,
        )
        assert tasks.rows[0].body["ci_resolution"]["source_outcome_ids"] == (
            ci.json()["fact_id"],
        )
        rows = to_export_rows(sft.rows)
        output = tmp_path / f"sft-{index}.jsonl"
        result = write_jsonl(rows, output, split_enabled=False)
        assert result.written == {str(output): 1}
        training_bytes.append(output.read_bytes())
    assert bundle_bytes[0] == bundle_bytes[1]
    assert training_bytes[0] == training_bytes[1]

    # The same captured corpus checks every qualified edge field before the
    # factual consumers interpret CI or merge membership. All-history readers
    # have no historical cutoff; a late observation belongs only to the scoped
    # negative control. A different commit can still prove that the Session reached
    # some commit. A repository contradicting its source Push proves no binding.
    from sediment_api.services.operational_reports import (
        generate_operational_model_report,
    )

    scope = OperationalReportScope.trailing_days(1, as_of=captured.as_of)
    changes = {
        "matching": {"captured_at": scope.as_of},
        "absent": None,
        "late": {"captured_at": scope.as_of + timedelta(microseconds=1)},
        "wrong_org": {"org_id": "other"},
        "wrong_repo": {"repo": "other/repo"},
        "wrong_commit": {"commit_sha": "b" * 40},
        "wrong_session": {"session_id": "other"},
        "quarantined": {},
    }
    source_facts = [
        ("store_inference_call", fact)
        for fact in store.read_inference_calls(settings.org_id)
    ] + [
        ("store_decision", decision),
        ("store_push", store.read_pushes(settings.org_id)[0]),
        ("store_ci_outcome", store.read_ci_outcomes(settings.org_id)[0]),
        (
            "store_pull_request_merge",
            store.read_pull_request_merges(settings.org_id)[0],
        ),
    ]
    for case, change in changes.items():
        engine = create_engine(postgres_database_factory())
        try:
            population = FactStore(engine)
            for method, fact in reversed(source_facts):
                getattr(population, method)(fact)
            if change is not None:
                population.store_session_commit_observation(
                    observation.model_copy(update=change)
                )
            if case == "quarantined":
                population.quarantine_fact(
                    settings.org_id,
                    FactTable.SESSION_COMMIT_OBSERVATIONS,
                    observation.observation_id,
                    reason="synthetic corpus",
                )
            observed = case == "matching"
            session_observed = case in {"matching", "wrong_commit"}
            for report_scope in (scope,) if case == "late" else (None, scope):
                lifecycle = generate_accepted_work_lifecycle_report(
                    population, mirrors, settings.org_id, scope=report_scope
                )
                assert lifecycle.accepted_work.accepted_calls == 1, case
                assert lifecycle.accepted_work.attributed.count == int(observed), case
                assert lifecycle.accepted_work.pull_request_membership.count == int(
                    observed
                ), case
                assert lifecycle.merge_durability.scored_rows == int(observed), case
                assert lifecycle.accepted_work.ci_linked.count == int(observed), case
                assert lifecycle.accepted_work.ci_passed == int(observed), case
                assert lifecycle.accepted_work.skips.get(
                    "session_commit_unobserved", 0
                ) == int(not observed), case
                assert lifecycle.session_attrition.committed == int(session_observed), (
                    case
                )
                assert lifecycle.session_attrition.attribution_unavailable == int(
                    not session_observed
                ), case
                assert (
                    lifecycle.session_attrition.abandoned
                    == lifecycle.session_attrition.in_flight
                    == 0
                ), case
                assert lifecycle.provenance.lifecycle.policy_version == "3"
                if case == "wrong_repo":
                    assert lifecycle.repository_skipped["session_observations"] == {
                        "repository_identity_conflict": 1
                    }
            report = generate_operational_model_report(
                population, mirrors, settings.org_id, scope
            )
            [model] = report.result.rows
            assert model.ci_linked == model.ci_passed == int(observed), case
            assert model.session_commit_unobserved == int(not observed), case
            assert model.explicit_accepts == 1
            assert model.provenance.policy_version == "4"
            # Version 1 training still accepts inferred recipe evidence. A
            # bundle built over all history legitimately includes late Facts.
            evidence = build_derived_bundle(population, mirrors, settings.org_id)
            training_observed = case in {"matching", "late"}
            assert evidence.attributed_completions[0].attribution_source is not None
            assert (
                bool(evidence.attributed_completions[0].session_commit_observations)
                == training_observed
            )
            for recipe in ("sft_curated", "sft_verified"):
                projected = project_sft(
                    evidence.attributed_completions, calls, SFTPolicy(recipe_id=recipe)
                )
                assert len(projected.rows) == 1, case
                assert projected.skipped == {}
                assert projected.rows[0].metadata.recipe_version == 1
                assert (
                    bool(projected.rows[0].metadata.session_commit_observation_ids)
                    == training_observed
                )
        finally:
            engine.dispose()
