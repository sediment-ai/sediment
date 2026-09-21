# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
import os
import subprocess
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from sediment_cli import __version__

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def isolated_capture_environment(monkeypatch):
    """Installed consumers must not borrow capture consent or credentials."""
    for name in (
        "SEDIMENT_OTLP_ENDPOINT",
        "SEDIMENT_INGEST_TOKEN",
        "SEDIMENT_API_BEARER_TOKEN",
        "OTEL_EXPORTER_OTLP_HEADERS",
        "SEDIMENT_DELIVERY_DIR",
    ):
        monkeypatch.delenv(name, raising=False)


class _TranscriptCapture(BaseHTTPRequestHandler):
    requests: list[tuple[str, dict]] = []

    def do_POST(self) -> None:  # noqa: N802 — stdlib callback name
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.requests.append((self.path, json.loads(body)))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture(scope="module")
def installed_wheels(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("installed-wheels")
    wheels = root / "wheels"
    subprocess.run(
        [
            "uv",
            "build",
            "--all-packages",
            "--wheel",
            "--out-dir",
            str(wheels),
        ],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    venv = root / "venv"
    subprocess.run(["uv", "venv", str(venv)], check=True, capture_output=True)
    python = venv / "bin" / "python"
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            *[str(path) for path in sorted(wheels.glob("*.whl"))],
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return wheels, venv


def test_built_wheel_cli_uses_postgres_and_ships_migrations(
    tmp_path: Path,
    postgres_database_url: str,
    installed_wheels,
) -> None:
    wheels, venv = installed_wheels
    sediment = venv / "bin" / "sediment"
    storage_free_env = {
        key: value
        for key, value in os.environ.items()
        if key != "SEDIMENT_DATABASE_URL"
    }
    help_result = subprocess.run(
        [str(sediment), "--help"],
        env=storage_free_env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert help_result.returncode == 0
    retired_path_flag = "--db" + "-path"
    assert retired_path_flag not in help_result.stdout

    version_result = subprocess.run(
        [str(sediment), "--version"],
        env=storage_free_env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert version_result.returncode == 0
    assert version_result.stdout.strip() == f"sediment {__version__}"

    database_env = {
        **storage_free_env,
        "SEDIMENT_ORG_ID": "wheel-test",
        "SEDIMENT_DEV_MODE": "true",
    }
    status = subprocess.run(
        [
            str(sediment),
            "db",
            "status",
            "--database-url",
            postgres_database_url,
        ],
        env=database_env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert status.returncode == 0, status.stderr
    assert "at_head" in status.stdout

    direct_facts = subprocess.run(
        [
            str(sediment),
            "facts",
            "--database-url",
            postgres_database_url,
        ],
        env=database_env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert direct_facts.returncode == 0, direct_facts.stderr
    assert "inference_calls" in direct_facts.stdout

    report = subprocess.run(
        [
            str(sediment),
            "report",
            "abandonment",
            "--database-url",
            postgres_database_url,
            "--org",
            "wheel-test",
            "--mirror-path",
            str(tmp_path / "mirrors"),
            "--json",
        ],
        env=database_env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert report.returncode == 0, report.stderr
    assert json.loads(report.stdout)["org_id"] == "wheel-test"

    [core_wheel] = list(wheels.glob("sediment_core-*.whl"))
    with zipfile.ZipFile(core_wheel) as archive:
        names = set(archive.namelist())
    assert "sediment_core/alembic/env.py" in names
    assert "sediment_core/alembic/versions/0001_postgresql_baseline.py" in names


def test_installed_wheel_configures_transcript_hooks(
    tmp_path: Path,
    installed_wheels,
) -> None:
    _, venv = installed_wheels
    sediment = venv / "bin" / "sediment"
    home = tmp_path / "installed-home"
    (home / ".claude").mkdir(parents=True)
    repo = tmp_path / "installed-repo"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    installed_env = {
        **{
            key: value
            for key, value in os.environ.items()
            if key not in {"PYTHONPATH", "SEDIMENT_DATABASE_URL"}
        },
        "HOME": str(home),
        "CODEX_HOME": str(home / ".codex"),
        "PATH": f"{venv / 'bin'}:/usr/bin:/bin",
        "SEDIMENT_INGEST_TOKEN": "wheel-test-token",
    }
    install = subprocess.run(
        [
            str(sediment),
            "install",
            str(repo),
            "--transcripts",
            "--no-agents",
        ],
        env=installed_env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert install.returncode == 0, install.stderr
    assert "transcript hook: skipped" not in install.stderr
    assert "snapshot hook: skipped" not in install.stderr
    settings = json.loads((home / ".claude" / "settings.json").read_text())
    [session_end] = settings["hooks"]["SessionEnd"]
    [session_end_hook] = session_end["hooks"]
    assert session_end_hook["command"] == (
        f'"{sediment}" transcript --agent claude-code || true'
    )
    [pre_tool_use] = settings["hooks"]["PreToolUse"]
    [pre_tool_use_hook] = pre_tool_use["hooks"]
    assert pre_tool_use["matcher"] == "Edit|Write"
    assert pre_tool_use_hook["command"] == (
        f'"{sediment}" transcript snapshot --agent claude-code || true'
    )

    doctor = subprocess.run(
        [str(sediment), "doctor", str(repo)],
        env=installed_env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert "claude-code transcript hook: present" in doctor.stdout
    assert "claude-code snapshot hook: present" in doctor.stdout

    target = repo / "app.py"
    target.write_text("retained = True\n", encoding="utf-8")
    transcript = tmp_path / "transcript.jsonl"
    entries = [
        {
            "type": "assistant",
            "sessionId": "sess-wheel",
            "timestamp": "2026-08-24T12:00:00.000Z",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu-wheel",
                        "name": "Write",
                        "input": {
                            "file_path": str(target),
                            "content": "retained = True\n",
                        },
                    }
                ]
            },
        },
        {
            "type": "user",
            "sessionId": "sess-wheel",
            "timestamp": "2026-08-24T12:00:01.000Z",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu-wheel",
                        "is_error": False,
                    }
                ]
            },
        },
    ]
    transcript.write_text(
        "\n".join(json.dumps(entry) for entry in entries) + "\n",
        encoding="utf-8",
    )
    oversized_transcript = tmp_path / "oversized-transcript.jsonl"
    oversized_entries = json.loads(json.dumps(entries))
    oversized_entries[0]["message"]["content"][0]["input"]["content"] = "x" * (
        256 * 1024 + 1
    )
    oversized_transcript.write_text(
        "\n".join(json.dumps(entry) for entry in oversized_entries) + "\n",
        encoding="utf-8",
    )

    _TranscriptCapture.requests.clear()
    server = HTTPServer(("127.0.0.1", 0), _TranscriptCapture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run(
            [str(sediment), "transcript", "--agent", "claude-code"],
            input=json.dumps(
                {
                    "session_id": "sess-wheel",
                    "transcript_path": str(transcript),
                }
            ),
            env={
                **installed_env,
                "SEDIMENT_OTLP_ENDPOINT": f"http://127.0.0.1:{server.server_port}",
            },
            check=False,
            capture_output=True,
            text=True,
        )
        oversized = subprocess.run(
            [str(sediment), "transcript", "--agent", "claude-code"],
            input=json.dumps(
                {
                    "session_id": "sess-wheel",
                    "transcript_path": str(oversized_transcript),
                }
            ),
            env={
                **installed_env,
                "SEDIMENT_OTLP_ENDPOINT": (f"http://127.0.0.1:{server.server_port}"),
            },
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert result.returncode == 0, result.stderr
    assert oversized.returncode == 0, oversized.stderr
    [(path, payload)] = _TranscriptCapture.requests
    assert path == "/v1/logs"
    [record] = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
    attrs = {
        attribute["key"]: attribute["value"]["stringValue"]
        for attribute in record["attributes"]
    }
    assert attrs["session.id"] == "sess-wheel"
    assert attrs["tool_use_id"] == "toolu-wheel"
    assert attrs["applied_text"] == "retained = True\n"
    assert attrs["observed_file_text"] == "retained = True\n"

    malformed = subprocess.run(
        [str(sediment), "transcript", "--agent", "claude-code"],
        input="{not json",
        env={
            **installed_env,
            "SEDIMENT_OTLP_ENDPOINT": "http://127.0.0.1:9",
        },
        check=False,
        capture_output=True,
        text=True,
    )
    assert malformed.returncode == 0, malformed.stderr

    unavailable = subprocess.run(
        [str(sediment), "transcript", "--agent", "claude-code"],
        input=json.dumps(
            {
                "session_id": "sess-wheel",
                "transcript_path": str(transcript),
            }
        ),
        env={
            **installed_env,
            "SEDIMENT_OTLP_ENDPOINT": "http://127.0.0.1:9",
        },
        check=False,
        capture_output=True,
        text=True,
    )
    assert unavailable.returncode == 0, unavailable.stderr


def test_installed_wheel_dispatches_cursor_hook(
    tmp_path: Path,
    installed_wheels,
) -> None:
    _, venv = installed_wheels
    sediment = venv / "bin" / "sediment"
    repo = tmp_path / "cursor-repo"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    payload = {
        "conversation_id": "cursor-wheel-session",
        "generation_id": "cursor-wheel-generation",
        "hook_event_name": "postToolUse",
        "tool_name": "Write",
        "tool_use_id": "cursor-wheel-call",
        "cwd": str(repo),
    }
    _TranscriptCapture.requests.clear()
    server = HTTPServer(("127.0.0.1", 0), _TranscriptCapture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run(
            [str(sediment), "cursor-hook"],
            input=json.dumps(payload),
            env={
                **{
                    key: value
                    for key, value in os.environ.items()
                    if key not in {"PYTHONPATH", "SEDIMENT_DATABASE_URL"}
                },
                "HOME": str(tmp_path / "home"),
                "SEDIMENT_OTLP_ENDPOINT": (f"http://127.0.0.1:{server.server_port}"),
                "SEDIMENT_INGEST_TOKEN": "wheel-test-token",
            },
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert result.returncode == 0, result.stderr
    [(path, posted)] = _TranscriptCapture.requests
    assert path == "/v1/logs"
    [record] = posted["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
    attrs = {
        attribute["key"]: next(iter(attribute["value"].values()))
        for attribute in record["attributes"]
    }
    assert attrs == {
        "agent": "cursor",
        "session.id": "cursor-wheel-session",
        "tool_use_id": "cursor-wheel-call",
        "tool_name": "Write",
        "decision": "accept",
        "explicit": False,
    }
    marker = repo / ".git" / "sediment-sessions"
    [entry] = [json.loads(line) for line in marker.read_text().splitlines()]
    assert (entry["tool"], entry["session_id"]) == (
        "cursor",
        "cursor-wheel-session",
    )


def test_installed_server_manages_database_and_preserves_facts(
    tmp_path, installed_wheels
):
    """First boot, concurrent boot, two shutdown signals, and a persisted restart."""
    import signal
    import socket
    import time

    import httpx

    _, venv = installed_wheels
    sediment = venv / "bin" / "sediment"
    home = tmp_path / "managed-home"
    home.mkdir()
    root = home / ".sediment" / "server"
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("SEDIMENT_", "PG"))
    }
    environment.update(HOME=str(home), PYTHONUNBUFFERED="1")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    output_file = tmp_path / "managed-server.log"

    def command(*args):
        return subprocess.run(
            [str(sediment), *args],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )

    original_credentials = None
    for stop_signal in (signal.SIGINT, signal.SIGTERM):
        with output_file.open("a") as output:
            server = subprocess.Popen(
                [str(sediment), "server", "--port", str(port)],
                cwd=tmp_path,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
            try:
                deadline = time.monotonic() + 120
                while True:
                    try:
                        if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    assert server.poll() is None, output_file.read_text()
                    assert time.monotonic() < deadline, output_file.read_text()
                    time.sleep(0.1)
                credentials = (root / "server.env").read_bytes()
                assert (root / "postgres" / "PG_VERSION").read_text().strip() == "17"
                assert (root / "server.env").stat().st_mode & 0o777 == 0o600
                if original_credentials is None:
                    original_credentials = credentials
                    conflict = command("server", "--port", "0")
                    assert conflict.returncode == 1
                    assert "already running" in conflict.stderr
                    login = command("login", url)
                    assert login.returncode == 0, login.stderr
                    demo = command("demo")
                    assert demo.returncode == 0, demo.stderr
                else:
                    assert credentials == original_credentials
                facts = command("facts")
                assert facts.returncode == 0, facts.stderr
                row = next(
                    line
                    for line in facts.stdout.splitlines()
                    if "inference_calls" in line
                )
                assert row.split() == ["inference_calls", "1", "1"]
                database_pid = int(
                    (root / "postgres" / "postmaster.pid").read_text().splitlines()[0]
                )
                server.send_signal(stop_signal)
                assert server.wait(timeout=30) == 0, output_file.read_text()
                assert not (root / "postgres" / "postmaster.pid").exists()
                with pytest.raises(ProcessLookupError):
                    os.kill(database_pid, 0)
            finally:
                if server.poll() is None:
                    server.terminate()
                    server.wait(timeout=30)
    logs = output_file.read_text()
    for line in original_credentials.decode().splitlines():
        assert line.split("=", 1)[1] not in logs
    assert logs.count("Downloading PostgreSQL") == 1
    # A failed API bind must also release its newly started database.
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", port))
        occupied.listen()
        failed = command("server", "--port", str(port))
        assert failed.returncode != 0
    assert not (root / "postgres" / "postmaster.pid").exists()
    assert (root / "server.env").read_bytes() == original_credentials
