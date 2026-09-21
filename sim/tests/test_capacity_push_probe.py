# SPDX-License-Identifier: AGPL-3.0-or-later
"""The capacity Push probe proves observation, not merely HTTP receipt."""

from __future__ import annotations

import importlib
import json
import os
import random
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from sediment_capture.github import parse_push
from sediment_core import FactStore
from sediment_derive.notes import read_commit_note
from sqlalchemy import create_engine

from sim import scenarios


def _probe_module():
    name = "sim.capacity_push_probe"
    assert importlib.util.find_spec(name) is not None, "Push probe is not implemented"
    return importlib.import_module(name)


def test_prepare_push_reuses_seed_repository_and_writes_readable_note(tmp_path):
    module = _probe_module()
    scenarios.gen_repo.generate(tmp_path)
    repo = scenarios.gen_repo._Repo(
        tmp_path / scenarios.gen_repo.REPO_NAME, random.Random(0)
    )
    before = repo.git("rev-parse", "HEAD")

    probe = module.prepare_push(tmp_path, "capacity-session")

    assert repo.git("rev-parse", "HEAD^") == before
    assert probe.commit_sha == repo.git("rev-parse", "HEAD")
    note = read_commit_note(repo.path, probe.commit_sha)
    assert [session.session_id for session in note.sessions] == ["capacity-session"]
    payload = json.loads(probe.body)
    push = parse_push(payload, org_id=scenarios.ORG, github_host="git.simcorp.example")
    assert push.before_sha == before
    assert push.after_sha == probe.commit_sha
    assert push.repository_id == str(scenarios.REPOSITORY_ID)
    assert push.clone_url == repo.path.as_uri()


def test_prepare_push_refuses_dirty_seed_without_committing_it(tmp_path):
    module = _probe_module()
    scenarios.gen_repo.generate(tmp_path)
    repo = scenarios.gen_repo._Repo(
        tmp_path / scenarios.gen_repo.REPO_NAME, random.Random(0)
    )
    before = repo.git("rev-parse", "HEAD")
    repo.write("unrelated.txt", "uncommitted change\n")

    with pytest.raises(ValueError, match="dirty_seed"):
        module.prepare_push(tmp_path, "capacity-session")

    assert repo.git("rev-parse", "HEAD") == before
    assert (repo.path / "unrelated.txt").exists()


@pytest.fixture(scope="module", params=[True, False], ids=["mirror", "no-mirror"])
def live_api(request, tmp_path_factory, postgres_database_factory):
    workdir = tmp_path_factory.mktemp("capacity-push")
    scenarios.gen_repo.generate(workdir)
    database_url = postgres_database_factory()
    env = os.environ.copy()
    env.update(
        SEDIMENT_DATABASE_URL=database_url,
        SEDIMENT_ORG_ID=scenarios.ORG,
        SEDIMENT_DEV_MODE="true",
        SEDIMENT_API_BEARER_TOKEN="sim-token-9c41-ingest-2b7f",
        SEDIMENT_OPERATOR_TOKEN="sim-operator-2ab6-token-8c1d",
        SEDIMENT_GITHUB_WEBHOOK_SECRET="sim-webhook-secret-d5f2-91a",
        SEDIMENT_GITHUB_HOST="git.simcorp.example",
        SEDIMENT_INGEST_TOKENS="{}",
    )
    env.pop("SEDIMENT_MIRROR_PATH", None)
    if request.param:
        env["SEDIMENT_MIRROR_PATH"] = str(workdir / "mirrors")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    engine = create_engine(database_url)
    with (workdir / "api.log").open("wb") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "sediment_api.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            cwd=Path(__file__).resolve().parents[2],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            with httpx.Client(timeout=1, trust_env=False) as client:
                deadline = time.monotonic() + 15
                while True:
                    assert process.poll() is None, "test API stopped during startup"
                    try:
                        if client.get(url + "/health").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    assert time.monotonic() < deadline, "test API startup timeout"
                    time.sleep(0.05)
                body = {
                    "provider": "litellm",
                    "session_id": "capacity-session",
                    "payload": scenarios._fixture("litellm_payload.json"),
                }
                response = client.post(
                    url + "/ingest/gateway",
                    json=body,
                    headers={
                        "Authorization": "Bearer " + env["SEDIMENT_API_BEARER_TOKEN"]
                    },
                )
                assert response.status_code == 200
                assert response.json()["stored"] is True
            yield workdir, url, FactStore(engine), request.param
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            engine.dispose()


def test_post_requires_exact_background_observation(live_api):
    module = _probe_module()
    assert callable(getattr(module, "post_and_wait", None)), "receipt probe is missing"
    workdir, url, store, has_mirror = live_api
    probe = module.prepare_push(workdir, "capacity-session")
    kwargs = dict(secret="sim-webhook-secret-d5f2-91a", store=store, probe=probe)
    started = time.monotonic()
    if not has_mirror:
        with pytest.raises(module.PushProbeFailure, match="observation_timeout") as exc:
            module.post_and_wait(url, timeout=0.75, **kwargs)
        assert exc.value.result["stored"] is True
        assert exc.value.result["acknowledged"] >= started
        assert "observed" not in exc.value.result
        assert len(store.read_pushes(scenarios.ORG)) == 1
        return

    result = module.post_and_wait(url, timeout=15, **kwargs)

    assert started <= result["started"] <= result["acknowledged"] <= result["observed"]
    assert result["stored"] is True
    assert result["commit_sha"] == probe.commit_sha
    [observation] = store.read_session_commit_observations(
        scenarios.ORG,
        session_ids={probe.session_id},
        repo_commits={(scenarios.REPO_FULL, probe.commit_sha)},
    )
    assert observation.source_push_id == result["fact_id"]
    assert observation.observation_id == result["observation_id"]
    assert result["redelivery_stored"] is False
    assert result["redelivery_fact_id"] == result["fact_id"]

    with pytest.raises(module.PushProbeFailure, match="preexisting_observation"):
        module.post_and_wait(url, timeout=2, **kwargs)
