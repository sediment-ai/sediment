# SPDX-License-Identifier: AGPL-3.0-or-later
"""The four-command quickstart as a regression test.

Subprocess ``sediment server`` on an ephemeral port → ``login`` → ``install``
into a scratch repo → replay a frozen Claude Code tool_decision fixture into
``/v1/logs`` → ``sediment facts`` (remote) shows the decision. This is the
test that would have caught both 2026-08-13 getting-started bugs (the
README's missing OTEL_LOGS_EXPORTER and the hook path rot)."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

import sediment_cli.client as api_client
from sediment_cli import cli

FIXTURE = (
    Path(__file__).parents[2]
    / "packages"
    / "capture"
    / "tests"
    / "fixtures"
    / "otlp"
    / "claude_code"
    / "tool_decision_accept_user.json"
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def quickstart_server(tmp_path, postgres_database_url):
    """A real ``sediment server`` subprocess, storing where the quickstart
    says it does: the default ``~/.sediment/server`` under a scratch HOME.
    No ``--root``, so the default the doc relies on is the one exercised."""
    port = _free_port()
    home = tmp_path / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("SEDIMENT_")}
    env["HOME"] = str(home)
    env["SEDIMENT_BOOTSTRAP_DATABASE_URL"] = postgres_database_url
    proc = subprocess.Popen(
        [sys.executable, "-m", "sediment_cli.cli", "server", "--port", str(port)],
        cwd=tmp_path,  # a stray checkout .env must not leak into Settings
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if proc.poll() is not None or time.monotonic() > deadline:
                out = proc.stdout.read() if proc.stdout else ""
                pytest.fail(f"server did not come up:\n{out}")
            time.sleep(0.2)
        server_env = home / ".sediment" / "server" / "server.env"
        token = dict(
            line.split("=", 1) for line in server_env.read_text().splitlines()
        )["SEDIMENT_API_BEARER_TOKEN"]
        yield url, token, home, postgres_database_url
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_quickstart_end_to_end(quickstart_server, tmp_path, monkeypatch, capsys):
    url, token, home, bootstrap_url = quickstart_server

    # sediment login — against the real subprocess server, exactly as
    # docs/quickstart.md §3 spells it: the bare loopback URL, nothing
    # piped. The prompt is trapped; reaching it means the documented step
    # still asks a question.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("SEDIMENT_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("SEDIMENT_URL", raising=False)
    monkeypatch.delenv("SEDIMENT_DATABASE_URL", raising=False)
    monkeypatch.setattr(api_client, "CONFIG_PATH", home / ".sediment" / "config.json")
    monkeypatch.setattr(api_client, "_transport", None)
    monkeypatch.setattr(cli, "_prompt_token", lambda: pytest.fail("prompted"))
    assert cli.main(["login", url]) == 0
    assert "logged in" in capsys.readouterr().out

    # sediment install — a scratch repo; agent hooks + env land in fake HOME.
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (home / ".claude").mkdir()  # "agent present" — install wires it
    assert cli.main(["install", str(repo)]) == 0
    hooks = repo / ".git" / "hooks"
    assert (hooks / "post-commit").exists()
    env_sh = (home / ".sediment" / "env.sh").read_text()
    assert "OTEL_LOGS_EXPORTER=otlp" in env_sh
    assert f"OTEL_EXPORTER_OTLP_ENDPOINT={url}" in env_sh
    identities = cli._load_server_env(home / ".sediment/server/server.env")
    assert token in env_sh
    assert identities["SEDIMENT_OPERATOR_TOKEN"] not in env_sh
    login = api_client.read_config()["servers"][url]
    assert login["authority"] == "operator"
    assert login["capture_authority"] == "ingest"
    assert (
        httpx.get(
            f"{url}/v1/facts", headers={"Authorization": f"Bearer {token}"}
        ).status_code
        == 403
    )

    # A wired agent's decision lands: replay the frozen wire fixture.
    payload = json.loads(FIXTURE.read_text())
    resp = httpx.post(
        f"{url}/v1/logs",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    assert resp.status_code == 200

    # sediment facts — remote, via the stored login.
    assert cli.main(["facts"]) == 0
    out = capsys.readouterr().out
    row = next(line for line in out.splitlines() if "developer_decisions" in line)
    assert row.split() == ["developer_decisions", "1", "1"]

    # The Compose operator command needs database/org settings only. The same
    # role created by the local provisioning path has quarantine read authority.
    from sqlalchemy.engine import make_url

    credentials = cli._load_server_env(home / ".sediment/server/server.env")
    operator_url = (
        make_url(bootstrap_url)
        .set(
            username="sediment_operator",
            password=credentials["SEDIMENT_OPERATOR_PASSWORD"],
        )
        .render_as_string(hide_password=False)
    )
    operator_env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("SEDIMENT_")
    }
    operator_env.update(SEDIMENT_DATABASE_URL=operator_url, SEDIMENT_ORG_ID="default")
    result = subprocess.run(
        [sys.executable, "-m", "sediment_cli.cli", "quarantine-log"],
        cwd=tmp_path,
        env=operator_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "0 rows" in result.stdout
