# SPDX-License-Identifier: AGPL-3.0-or-later
"""Build and validate the six distributions without publishing them."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from collections.abc import Iterable
from contextlib import contextmanager
from email.parser import Parser
from pathlib import Path, PurePosixPath
from uuid import uuid4

PROJECT_FILES = (
    Path("pyproject.toml"),
    Path("packages/core/pyproject.toml"),
    Path("packages/capture/pyproject.toml"),
    Path("packages/derive/pyproject.toml"),
    Path("packages/export/pyproject.toml"),
    Path("apps/api/pyproject.toml"),
    Path("cli/pyproject.toml"),
)
FIRST_PARTY = {
    "sediment-core",
    "sediment-capture",
    "sediment-derive",
    "sediment-export",
    "sediment-api",
    "sediment-cli",
}
PROJECT_URLS = {"Source", "Documentation", "Issues", "Changelog"}
REPOSITORY_LICENSE_CONTENT = (
    Path(__file__).resolve().parents[1] / "LICENSE"
).read_bytes()
REQUIRED_CONTENT = {
    "sediment-core": (
        "sediment_core/__init__.py",
        "sediment_core/alembic/env.py",
        "sediment_core/alembic/versions/0001_postgresql_baseline.py",
    ),
    "sediment-capture": ("sediment_capture/__init__.py",),
    "sediment-derive": ("sediment_derive/__init__.py",),
    "sediment-export": ("sediment_export/__init__.py",),
    "sediment-api": ("sediment_api/__init__.py",),
    "sediment-cli": (
        "sediment_cli/__init__.py",
        "sediment_cli/transcript.py",
        "sediment_cli/delivery.py",
    ),
}
# Matches list-item, two-line (`- name:` then `uses:`), and job-level
# reusable-workflow `uses:` lines — an unpinned action must not hide in any
# of the three styles.
_ACTION_RE = re.compile(r"^\s*(?:-\s+)?uses:\s+([^\s#]+)(?:\s+#\s+(.+))?$")

PIPELINE_FIXTURE_VERSION = "sediment-release-synthetic-v1"
PIPELINE_EXPECTATIONS = {
    "authority": {
        "operator_identity": "operator",
        "capture_identity": "legacy",
        "ingest_reads_denied": True,
        "operator_reads_allowed": True,
        "capture_files_ingest_only": True,
        "capture_files_private": True,
        "api_database_role": "sediment_runtime",
        "production_validation": True,
    },
    "capture": {"inference_calls": 4, "developer_decisions": 3},
    "transcript": {"edit_observations": 1, "rejected_edits": 1, "retry_linkages": 1},
    "transcript_batch": {
        "requests": 2,
        "edit_observations": 32,
        "pending": 0,
        "source_changed": True,
        "transcript_removed": True,
        "prepared_bytes_replayed": True,
        "fact_ids_retained": True,
    },
    "bundle": {"version": 4, "identities": 4, "segments": 3},
    "repository_identity": {
        "source_roles": 4,
        "rename_receipts": 1,
        "renamed_ci_linked": True,
        "training_identity_preserved": True,
        "identity_tampering_rejected": True,
    },
    "training": {
        "sft_rows": 1,
        "task_rows": 1,
        "rollout_rows": 2,
        "unicode_declines": 1,
    },
    "quarantine": {"revisions": [0, 1, 2], "restored": True},
    "cohort": {"attributed_completion_user_scope": 1, "rollout_user_scope": 1},
    "model_report": {
        "completions": 4,
        "explicit_accepts": 3,
        "attributed_inference_calls": 2,
        "ci_linked": 1,
        "ci_passed": 1,
        "authenticated": True,
        "bytes_equal": True,
    },
    "lifecycle_report": {
        "accepted_calls": 2,
        "observed_accepts": 2,
        "attributed": 1,
        "edit_observations": 1,
        "pull_request_membership": 0,
        "missing_pull_request_membership": 1,
        "ci_linked": 0,
        "authenticated": True,
        "bytes_equal": True,
    },
    "delivery": {
        "gateway_queued": 4,
        "otlp_queued": 2,
        "terminal_receipts": 6,
        "pending": 0,
        "sender_restarted": True,
        "callback_worker_stopped": True,
        "source_bytes_preserved": True,
        "capture_instants_preserved": True,
        "transcript_file_changed": True,
    },
    "lost_acknowledgment": {
        "committed_before_retry": True,
        "retained_fact_id": True,
        "gateway_duplicates": 1,
        "inference_calls": 4,
    },
    "reproduction": {"bundle_bytes_equal": True, "training_bytes_equal": True},
    "tamper": {"rehashed_bundle_refused": True, "training_refused": True},
}


@contextmanager
def scratch_database(database_url: str):
    """Own exactly one random database; never migrate the administrative target."""
    from sediment_core.postgres_engine import configure_libpq
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    from sqlalchemy.pool import NullPool

    name = f"sediment_rehearsal_{uuid4().hex}"
    try:
        url = make_url(database_url)
        if url.get_backend_name() != "postgresql":
            raise ValueError("PostgreSQL required")
        configure_libpq()
        engine = create_engine(url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    except Exception:
        raise RuntimeError(
            "runtime rehearsal: scratch database creation failed"
        ) from None
    created = False
    try:
        try:
            with engine.connect() as connection:
                connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
            created = True
        except Exception:
            raise RuntimeError(
                "runtime rehearsal: scratch database creation failed"
            ) from None
        # psycopg query options override the URL path's database selector.
        runtime_url = url.difference_update_query(["dbname"]).set(database=name)
        yield runtime_url.render_as_string(hide_password=False)
    finally:
        try:
            if created:
                try:
                    with engine.connect() as connection:
                        connection.exec_driver_sql(
                            f'DROP DATABASE "{name}" WITH (FORCE)'
                        )
                except Exception:
                    raise RuntimeError(
                        f"runtime rehearsal: scratch database cleanup failed: {name}"
                    ) from None
        finally:
            engine.dispose()


def validate_pipeline_report(report: dict) -> list[str]:
    """Require the fixed corpus's observed stage results before claiming success."""
    errors = []
    if report.get("fixture_version") != PIPELINE_FIXTURE_VERSION:
        errors.append("runtime rehearsal: fixture version absent or contradictory")
    stages = report.get("stages", {})
    for stage, expected in PIPELINE_EXPECTATIONS.items():
        actual = stages.get(stage, {}) if isinstance(stages, dict) else {}
        if not isinstance(actual, dict) or any(
            type(actual.get(key)) is not type(value) or actual.get(key) != value
            for key, value in expected.items()
        ):
            errors.append(
                f"runtime rehearsal: {stage} evidence absent or contradictory"
            )
    return errors


def validate_pipeline_receipts(receipts: list[dict]) -> bool:
    """Require each corpus receipt exactly once, independent of retry order."""
    expected = []
    for stored in (True, False):
        for records, candidates in (
            (
                {"received": 5, "translated": 2, "untranslated": 2, "malformed": 1},
                {
                    "developer_decisions": 3,
                    "edit_observations": 0,
                    "rejected_edits": 0,
                    "retry_linkages": 0,
                },
            ),
            (
                {"received": 3, "translated": 3, "untranslated": 0, "malformed": 0},
                {
                    "developer_decisions": 0,
                    "edit_observations": 1,
                    "rejected_edits": 1,
                    "retry_linkages": 1,
                },
            ),
        ):
            expected.append(
                {
                    "records": records,
                    "facts": {
                        name: {
                            "candidates": count,
                            "stored": count if stored else 0,
                            "duplicates": 0 if stored else count,
                        }
                        for name, count in candidates.items()
                    },
                }
            )
    return sorted(json.dumps(item, sort_keys=True) for item in receipts) == sorted(
        json.dumps(item, sort_keys=True) for item in expected
    )


def exercise_installed_pipeline(
    workspace: Path, sediment: Path, release_version: str
) -> dict:
    """Execute the fixed synthetic corpus using only the installed runtime.

    Invoked by the wheel venv's isolated Python. The script supplies inputs and
    assertions; installed capture, storage, derivation and export own semantics.
    """
    import importlib
    import importlib.metadata
    import shutil
    import socket
    import threading
    import time
    from datetime import UTC, datetime, timedelta
    from dataclasses import replace
    from http.server import (
        BaseHTTPRequestHandler,
        SimpleHTTPRequestHandler,
        ThreadingHTTPServer,
    )

    import httpx
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    from sediment_core import FactStore
    from sediment_core.postgres_engine import configure_libpq
    from sediment_cli import delivery
    from sediment_capture import sign_payload
    from sediment_export import (
        BundleValidationError,
        read_derived_bundle,
        validate_derived_bundle,
    )

    def require(condition, label):
        if not condition:
            raise AssertionError(f"runtime rehearsal: {label}")

    versions = {name: importlib.metadata.version(name) for name in sorted(FIRST_PARTY)}
    require(set(versions.values()) == {release_version}, "installed version mismatch")
    for name in FIRST_PARTY:
        module = importlib.import_module(name.replace("-", "_"))
        require(
            Path(module.__file__)
            .resolve()
            .is_relative_to(sediment.parent.parent.resolve()),
            "package imported outside installed environment",
        )

    workspace.mkdir()
    configure_libpq()
    env = dict(os.environ)
    org, session, outside_session = (
        "release-rehearsal",
        "rehearsal-session",
        "excluded-session",
    )
    repo_slug = "synthetic/rehearsal"
    renamed_repo = "synthetic/renamed-rehearsal"
    repository_identity = {
        "provider": "github",
        "host": "github.com",
        "repository_id": "770001",
    }
    token, operator_token, secret = (
        "synthetic-rehearsal-ingest-3f471ad1",
        "synthetic-rehearsal-operator-480b1229",
        "synthetic-rehearsal-webhook-19e5debf",
    )
    server_root = workspace / "server"
    env.update(
        {
            "SEDIMENT_ORG_ID": org,
            "SEDIMENT_API_BEARER_TOKEN": token,
            "SEDIMENT_OPERATOR_TOKEN": operator_token,
            "SEDIMENT_GITHUB_WEBHOOK_SECRET": secret,
            "SEDIMENT_MIRROR_PATH": str(server_root / "mirror"),
            "SEDIMENT_DEV_MODE": "false",
            "SEDIMENT_ALLOWED_CLONE_HOSTS": '["127.0.0.1"]',
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "NO_COLOR": "1",
        }
    )

    def command(*args, input=None, success=True, command_env=None):
        result = subprocess.run(
            [str(sediment), *args],
            cwd=workspace,
            env=env if command_env is None else command_env,
            input=input,
            capture_output=True,
            text=True,
            timeout=60,
        )
        require(
            (result.returncode == 0) == success,
            f"command {' '.join(args[:2])} returned {result.returncode}",
        )
        return result.stdout

    def git(cwd, *args):
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *args],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        require(result.returncode == 0, f"scratch Git {args[0]} failed")
        return result.stdout.strip()

    fixture_path = Path(__file__).with_name("fixtures") / "release_synthetic_v1.json"
    fixture_bytes = fixture_path.read_bytes()
    corpus = json.loads(fixture_bytes)
    require(
        corpus["version"] == PIPELINE_FIXTURE_VERSION,
        "synthetic fixture version mismatch",
    )
    prompt, completion, outside_completion = (
        corpus[key] for key in ("prompt", "completion", "outside_completion")
    )
    work = workspace / "work"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    git(work, "config", "user.email", "synthetic@example.test")
    git(work, "config", "user.name", "Synthetic rehearsal")
    (work / "a.py").write_text("counter = 0\n")
    git(work, "add", "a.py")
    git(work, "commit", "-q", "-m", "synthetic base")
    base = git(work, "rev-parse", "HEAD")
    (work / "a.py").write_text(completion, encoding="utf-8")
    (work / "b.py").write_text(outside_completion, encoding="utf-8")
    git(work, "add", "a.py", "b.py")
    git(work, "commit", "-q", "-m", "synthetic patch")
    head = git(work, "rev-parse", "HEAD")
    note = {
        "v": 1,
        "sessions": [
            {
                "tool": "claude-code",
                "session_id": sid,
                "stamped_at": datetime.now(UTC).isoformat(),
            }
            for sid in (session, outside_session)
        ],
    }
    git(work, "notes", "--ref=sediment", "add", "-m", json.dumps(note), head)
    remote = workspace / "remote.git"
    git(workspace, "init", "-q", "--bare", str(remote))
    git(
        work,
        "push",
        "-q",
        str(remote),
        "refs/heads/*:refs/heads/*",
        "refs/notes/*:refs/notes/*",
    )
    # Serve only the owned bare fixture; production clone policy remains enabled.
    git(remote, "update-server-info")

    class GitFixture(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(remote), **kwargs)

        def log_message(self, *args):
            pass

    git_server = ThreadingHTTPServer(("127.0.0.1", 0), GitFixture)
    git_thread = threading.Thread(target=git_server.serve_forever, daemon=True)
    clone_url = f"http://127.0.0.1:{git_server.server_port}/"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    env["SEDIMENT_OTLP_ENDPOINT"] = url
    env["SEDIMENT_INGEST_TOKEN"] = token
    engine = create_engine(env["SEDIMENT_DATABASE_URL"])
    store = FactStore(engine)
    server_env = {
        **{
            key: value
            for key, value in env.items()
            if key != "SEDIMENT_TEST_DATABASE_URL"
        },
        "SEDIMENT_BOOTSTRAP_DATABASE_URL": env["SEDIMENT_DATABASE_URL"],
    }
    log_path = workspace / "server.log"
    with log_path.open("w") as log:
        server = None

        def start_api():
            nonlocal server
            server = subprocess.Popen(
                [
                    str(sediment),
                    "server",
                    "--port",
                    str(port),
                    "--root",
                    str(server_root),
                ],
                cwd=workspace,
                env=server_env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            deadline = time.monotonic() + 30
            while True:
                try:
                    if httpx.get(url + "/health", timeout=1).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                require(
                    server.poll() is None and time.monotonic() < deadline,
                    "installed server did not become ready",
                )
                time.sleep(0.05)

        def stop_api():
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=10)

        received, upstream_receipts, outage_attempts, replay_runs = [], [], [], []
        lost_ack = {}

        class Intermediary(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                received.append(body)
                try:
                    response = httpx.post(
                        url + self.path,
                        content=body,
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": self.headers["Authorization"],
                        },
                        timeout=5,
                    )
                except httpx.HTTPError:
                    outage_attempts.append(self.path)
                    self.send_response(503)
                    self.end_headers()
                    return
                if self.path == "/ingest/gateway" and response.status_code == 200:
                    receipt = response.json()
                    upstream_receipts.append(receipt)
                    if not lost_ack:
                        lost_ack.update(
                            delivery_id=json.loads(body)["capture"]["id"],
                            receipt=receipt,
                        )
                        # The authenticated API response establishes a committed
                        # Fact before this owned intermediary drops the connection.
                        self.close_connection = True
                        self.connection.shutdown(socket.SHUT_RDWR)
                        return
                self.send_response(response.status_code)
                self.send_header("Content-Length", str(len(response.content)))
                self.end_headers()
                self.wfile.write(response.content)

            def log_message(self, *_):
                pass

        intermediary = ThreadingHTTPServer(("127.0.0.1", 0), Intermediary)
        intermediary_thread = threading.Thread(
            target=intermediary.serve_forever, daemon=True
        )
        intermediary_thread.start()
        delivery_url = f"http://127.0.0.1:{intermediary.server_port}"
        queue = workspace / "delivery"
        env.update(
            SEDIMENT_DELIVERY_DIR=str(queue),
            SEDIMENT_INGEST_URL=delivery_url,
            SEDIMENT_OTLP_ENDPOINT=delivery_url,
        )

        def queued_requests():
            requests = []
            for path in queue.glob("*.entry"):
                header, _, body = path.read_bytes().partition(b"\n")
                metadata = json.loads(header)
                require(
                    metadata["format_version"] == delivery.FORMAT_VERSION
                    and metadata["sha256"] == hashlib.sha256(body).hexdigest()
                    and metadata["bytes"] == len(body),
                    "prepared delivery record changed bytes",
                )
                requests.append(
                    delivery.prepare_request(
                        metadata["channel"],
                        metadata["destination"],
                        body,
                        captured_at=metadata["captured_at"],
                        delivery_id=metadata["delivery_id"],
                    )
                )
            return requests

        def replay_batch():
            result = json.loads(command("delivery", "replay"))
            replay_runs.append(result)
            require(not result["worker_busy"], "callback worker survived its process")
            return result

        def drain():
            deadline = time.monotonic() + 20
            while json.loads(command("delivery", "status"))["pending"]:
                require(time.monotonic() < deadline, "installed delivery did not drain")
                replay_batch()
                time.sleep(0.05)

        try:
            git_thread.start()
            start_api()
            enrollment_env = {
                key: value
                for key, value in env.items()
                if key
                not in {
                    "SEDIMENT_INGEST_TOKEN",
                    "SEDIMENT_SESSION_TOKEN",
                    "SEDIMENT_DELIVERY_DIR",
                }
            }
            command(
                "login",
                url,
                "--with-token",
                input=operator_token + "\n",
                command_env=enrollment_env,
            )
            command(
                "login",
                url,
                "--capture",
                "--with-token",
                input=token + "\n",
                command_env=enrollment_env,
            )
            command(
                "install",
                str(work),
                "--codex-profile",
                "rehearsal",
                command_env=enrollment_env,
            )
            capture_files = (
                Path(env["HOME"]) / ".sediment/env.sh",
                Path(env["HOME"]) / ".config/fish/conf.d/sediment.fish",
                Path(env["CODEX_HOME"]) / "rehearsal.config.toml",
            )
            require(
                all(
                    token in path.read_text() and operator_token not in path.read_text()
                    for path in capture_files
                ),
                "capture files include operator authority",
            )
            require(
                all(path.stat().st_mode & 0o777 == 0o600 for path in capture_files),
                "capture credentials are not private",
            )
            identities = {}
            for name, bearer in (("operator", operator_token), ("capture", token)):
                identity = httpx.get(
                    url + "/v1/me",
                    headers={"Authorization": "Bearer " + bearer},
                    timeout=5,
                )
                require(identity.status_code == 200, "credential identity unavailable")
                identities[name] = identity.json()
            require(
                identities["operator"]["authority"] == "operator"
                and identities["capture"]["authority"] == "ingest",
                "credential authorities overlap",
            )
            # Direct operator commands use their own database role. The API server
            # retains its separately provisioned runtime connection on each restart.
            server_credentials = dict(
                line.split("=", 1)
                for line in (server_root / "server.env").read_text().splitlines()
                if line and not line.startswith("#")
            )
            env["SEDIMENT_DATABASE_URL"] = (
                make_url(env["SEDIMENT_DATABASE_URL"])
                .set(
                    username="sediment_operator",
                    password=server_credentials["SEDIMENT_OPERATOR_PASSWORD"],
                )
                .render_as_string(hide_password=False)
            )
            with engine.connect() as connection:
                require(
                    connection.exec_driver_sql(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname=current_database() AND usename='sediment_runtime'"
                    ).scalar_one()
                    > 0,
                    "API runtime database role absent",
                )

            def post(route, body, *, forge=False, event="push"):
                headers = {
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + token,
                }
                if forge:
                    headers.update(
                        {
                            "X-GitHub-Event": event,
                            "X-Hub-Signature-256": sign_payload(body, secret),
                        }
                    )
                response = httpx.post(
                    url + route, content=body, headers=headers, timeout=30
                )
                require(
                    response.status_code == 200,
                    f"HTTP {route} returned {response.status_code}",
                )
                return response.json()

            stop_api()
            callback_path = (
                Path(__file__).resolve().parents[1] / "litellm" / "sediment_callback.py"
            )
            callback_source = callback_path.read_bytes()
            callback_inputs = []
            for item in corpus["gateway"]:
                payload = dict(item["payload"])
                payload["metadata"] = {
                    "requester_metadata": {
                        "session_id": item["session_id"],
                        "user_id": item["user_id"],
                    }
                }
                callback_inputs.append(payload)
            callback_code = """
import asyncio, importlib.util, json, sys, types
from sediment_cli import delivery
for name in ('litellm', 'litellm.integrations', 'litellm.integrations.custom_logger'):
    sys.modules[name] = types.ModuleType(name)
sys.modules['litellm.integrations.custom_logger'].CustomLogger = type('CustomLogger', (), {})
spec = importlib.util.spec_from_file_location('rehearsal_callback', sys.argv[1])
callback = importlib.util.module_from_spec(spec)
spec.loader.exec_module(callback)
async def capture():
    for payload in json.load(sys.stdin):
        await callback.handler.async_log_success_event(
            {'standard_logging_object': payload}, {}, 0.0, 0.0)
try:
    asyncio.run(capture())
finally:
    callback.handler.close_delivery()
print(json.dumps(delivery.status(callback.os.environ['SEDIMENT_DELIVERY_DIR'])))
"""
            sender = subprocess.run(
                [
                    str(sediment.parent / "python"),
                    "-I",
                    "-c",
                    callback_code,
                    str(callback_path),
                ],
                env=env,
                cwd=workspace,
                input=json.dumps(callback_inputs),
                capture_output=True,
                text=True,
                timeout=30,
            )
            require(sender.returncode == 0, "installed callback sender failed")
            callback_status = json.loads(sender.stdout)
            require(
                callback_status["pending"] == 4
                and callback_status["worker_running"] is False,
                "callback did not leave four durable requests after shutdown",
            )
            gateway_requests = sorted(
                queued_requests(),
                key=lambda request: json.loads(request.body)["payload"][
                    "litellm_call_id"
                ],
            )
            gateway_payloads = [request.body for request in gateway_requests]
            replay_batch()
            require(
                not store.read_inference_calls(org)
                and json.loads(command("delivery", "status"))["pending"] == 4
                and outage_attempts,
                "API outage failed to retain callback requests",
            )
            start_api()
            deadline = time.monotonic() + 20
            while not lost_ack:
                require(time.monotonic() < deadline, "lost-acknowledgment stage absent")
                replay_batch()
                time.sleep(0.05)
            committed_before_retry = (
                lost_ack["receipt"]["stored"] is True
                and (queue / (lost_ack["delivery_id"] + ".entry")).exists()
                and any(
                    call.inference_call_id == lost_ack["receipt"]["fact_id"]
                    for call in store.read_inference_calls(org)
                )
            )
            require(
                committed_before_retry, "lost acknowledgment erased pending content"
            )
            drain()
            calls = store.read_inference_calls(org)
            call_ids = [call.inference_call_id for call in calls]
            require(
                len(calls) == len(gateway_requests) == 4,
                "gateway replay changed the Fact population",
            )
            for index, call in enumerate(calls):
                require(
                    call.org_id == org
                    and call.session_id == (session if index < 3 else outside_session),
                    "gateway Session mismatch",
                )
                require(
                    call.model_call_id == f"rehearsal-call-{index}"
                    and call.input_messages[0].parts[0].content == prompt,
                    "gateway canonical input mismatch",
                )
                require(
                    call.raw == json.loads(gateway_payloads[index])["payload"]
                    and call.gateway_provider == "litellm"
                    and call.model == "synthetic-model"
                    and call.user_id
                    == ("selected-user" if index < 3 else "excluded-user"),
                    "gateway canonical metadata/raw mismatch",
                )
                require(
                    call.output_messages[0].parts[0].content
                    == (
                        completion + "\ud800"
                        if index == 2
                        else outside_completion
                        if index == 3
                        else completion
                    ),
                    "gateway canonical output mismatch",
                )
                capture = json.loads(gateway_payloads[index])["capture"]
                require(
                    call.observed_at == datetime.fromisoformat(capture["observed_at"])
                    and capture["id"] == gateway_requests[index].delivery_id,
                    "callback capture identity/instant changed during replay",
                )
            when = calls[0].observed_at

            decision_payload = (
                json.dumps(corpus["otlp"])
                .replace(
                    '"$EVENT_TIME"',
                    json.dumps(str(int(when.timestamp() * 1_000_000_000))),
                )
                .encode()
            )
            stop_api()
            decision_request = delivery.prepare_request(
                "otlp", delivery_url, decision_payload
            )
            decision_enqueue = json.loads(
                command(
                    "delivery", "enqueue", input=json.dumps(decision_request.as_dict())
                )
            )
            require(
                decision_enqueue["status"] == "queued"
                and decision_enqueue["delivery_id"] == decision_request.delivery_id,
                "OTLP delivery request was not durably enqueued",
            )

            # Synthetic Claude Code JSONL grammar v1: an explicit refusal, then
            # a successful Write to the same absolute path in the same Session.
            target = str(work / "a.py")
            transcript_entries = json.loads(
                json.dumps(corpus["transcript"]).replace('"$FILE"', json.dumps(target))
            )
            for entry in transcript_entries:
                entry["timestamp"] = (
                    when + timedelta(milliseconds=entry.pop("offset_ms"))
                ).isoformat()
            transcript = workspace / "claude-synthetic-v1.jsonl"
            transcript.write_text(
                "".join(json.dumps(entry) + "\n" for entry in transcript_entries)
                + "malformed sibling\n"
            )
            hook = json.dumps(
                {"session_id": session, "transcript_path": str(transcript)}
            )
            command("transcript", "--agent", "claude-code", input=hook)
            otlp_requests = queued_requests()
            require(len(otlp_requests) == 2, "installed transcript did not enqueue")
            transcript_request = next(
                request
                for request in otlp_requests
                if request.delivery_id != decision_request.delivery_id
            )
            replay_batch()
            require(
                not store.read_decisions(org)
                and not store.read_edit_observations(org)
                and json.loads(command("delivery", "status"))["pending"] == 2,
                "OTLP outage failed to retain prepared observations",
            )
            (work / "a.py").write_text("changed after durable capture\n")
            transcript_file_changed = (work / "a.py").read_text() != completion
            require(
                set(queued_requests()) == set(otlp_requests),
                "changing the source file rewrote a prepared observation",
            )
            start_api()
            drain()
            decisions = store.read_decisions(org)
            require(
                len(decisions) == 3
                and {d.call_id for d in decisions}
                == {"rehearsal-call-0", "rehearsal-call-1"},
                "Decision fanout/attachment mismatch",
            )
            require(
                {d.file_path for d in decisions if d.call_id == "rehearsal-call-1"}
                == {"a.py", "b.py"},
                "Codex fanout paths changed",
            )
            require(
                all(
                    d.session_id == session
                    and d.accepted
                    and d.explicit
                    and d.occurred_at == when
                    for d in decisions
                ),
                "Decision label/Session/source instant mismatch",
            )
            observations = store.read_edit_observations(org)
            rejected_edits = store.read_rejected_edits(org)
            retries = store.read_retry_linkages(org)
            require(
                len(observations) == len(rejected_edits) == len(retries) == 1,
                "installed transcript omitted expected Facts",
            )
            observation, rejected, retry = (
                observations[0],
                rejected_edits[0],
                retries[0],
            )
            require(
                (
                    observation.call_id,
                    observation.applied_text,
                    observation.observed_file_text,
                    observation.file_path,
                    observation.session_id,
                )
                == ("rehearsal-call-0", completion, completion, target, session),
                "transcript observation payload mismatch",
            )
            require(
                (
                    rejected.call_id,
                    rejected.proposed,
                    rejected.file_path,
                    rejected.session_id,
                )
                == ("rehearsal-rejected", "wrong", target, session),
                "transcript rejected Edit mismatch",
            )
            require(
                (
                    retry.rejected_call_id,
                    retry.accepted_call_id,
                    retry.file_path,
                    retry.session_id,
                )
                == ("rehearsal-rejected", "rehearsal-call-0", target, session),
                "transcript Retry linkage mismatch",
            )

            push_body = json.dumps(
                {
                    "ref": "refs/heads/main",
                    "before": base,
                    "after": head,
                    "repository": {
                        "id": 770001,
                        "full_name": repo_slug,
                        "clone_url": clone_url,
                    },
                }
            ).encode()
            push_receipt = post("/ingest/github/push", push_body, forge=True)
            rename_receipt = post(
                "/ingest/github/repository",
                json.dumps(
                    {
                        "action": "renamed",
                        "repository": {"id": 770001, "full_name": renamed_repo},
                        "changes": {"repository": {"name": {"from": "rehearsal"}}},
                    }
                ).encode(),
                forge=True,
                event="repository",
            )
            ci_receipt = post(
                "/ingest/ci",
                json.dumps(
                    {
                        "provider": "jenkins",
                        "run_id": "rehearsal-run",
                        "repo": renamed_repo,
                        "repository_provider": "github",
                        "repository_host": "github.com",
                        "repository_id": "770001",
                        "commit_sha": head,
                        "branch": "main",
                        "result": "passed",
                        "workflow_id": "rehearsal-workflow",
                        "workflow_name": "Synthetic tests",
                    }
                ).encode(),
            )
            deadline = time.monotonic() + 30
            while len(store.read_session_commit_observations(org)) != 2:
                require(
                    time.monotonic() < deadline,
                    "Push background observation stage absent",
                )
                time.sleep(0.05)
            session_observations = store.read_session_commit_observations(org)
            require(
                {
                    (o.session_id, o.repo, o.commit_sha, o.source_push_id)
                    for o in session_observations
                }
                == {
                    (sid, repo_slug, head, push_receipt["fact_id"])
                    for sid in (session, outside_session)
                },
                "Git-note observation relationship mismatch",
            )
            report_end = max(call.observed_at for call in calls) + timedelta(
                microseconds=1
            )
            report_scope = {
                "cohort_start": min(call.observed_at for call in calls),
                "cohort_end": report_end,
                "as_of": max(datetime.now(UTC), report_end, observation.occurred_at),
            }
            report_params = {
                key: value.isoformat().replace("+00:00", "Z")
                for key, value in report_scope.items()
            }
            report_routes = {
                "model_report": "/v1/reports/model-outcomes",
                "lifecycle_report": "/v1/reports/accepted-work-lifecycle",
            }
            report_auth = {
                name: httpx.get(
                    url + route, params=report_params, timeout=30
                ).status_code
                == 401
                for name, route in report_routes.items()
            }

            read_requests = [
                ("/v1/facts", {}),
                (f"/v1/facts/session/{session}", {}),
                (f"/v1/facts/session/{session}/inference-calls", {}),
                (f"/v1/facts/session/{session}/compatibility-evidence", {}),
                (f"/query/session/{session}", {}),
                (f"/query/commit/{head}", {}),
                (
                    "/query/ci/outcome",
                    {"provider": "jenkins", "run_id": "rehearsal-run"},
                ),
                (
                    "/query/ci/failures",
                    {
                        "repo": repo_slug,
                        "captured_after": report_params["cohort_start"],
                        "captured_before": report_params["as_of"],
                    },
                ),
                *((route, report_params) for route in report_routes.values()),
            ]
            for route, params in read_requests:
                for bearer, expected in ((token, 403), (operator_token, 200)):
                    response = httpx.get(
                        url + route,
                        params=params,
                        headers={"Authorization": "Bearer " + bearer},
                        timeout=30,
                    )
                    require(
                        response.status_code == expected,
                        f"HTTP {route} authority check expected {expected}, got {response.status_code}",
                    )
            authority_stage = {
                "operator_identity": identities["operator"]["client_id"],
                "capture_identity": identities["capture"]["client_id"],
                "ingest_reads_denied": True,
                "operator_reads_allowed": True,
                "capture_files_ingest_only": True,
                "capture_files_private": True,
                "api_database_role": "sediment_runtime",
                "production_validation": server_env["SEDIMENT_DEV_MODE"] == "false",
            }

            def operational_reports():
                responses = {}
                for name, route in report_routes.items():
                    response = httpx.get(
                        url + route,
                        params=report_params,
                        headers={"Authorization": "Bearer " + operator_token},
                        timeout=30,
                    )
                    require(response.status_code == 200, f"HTTP {route} failed")
                    envelope = response.json()
                    require(
                        envelope["schema_version"] == 1
                        and envelope["scope"]
                        == {**report_params, "max_inference_calls": 50_000},
                        f"{name} changed the frozen cohort or observation boundary",
                    )
                    responses[name] = response.content
                return responses

            reports_before = operational_reports()
            for index, body in enumerate(gateway_payloads):
                require(
                    post("/ingest/gateway", body)
                    == {"fact_id": call_ids[index], "stored": False},
                    "gateway replay changed its retained Fact receipt",
                )
            post("/v1/logs", decision_payload)
            post("/v1/logs", transcript_request.body)
            for method, expected in (
                ("read_inference_calls", calls),
                ("read_decisions", decisions),
                ("read_edit_observations", observations),
                ("read_rejected_edits", rejected_edits),
                ("read_retry_linkages", retries),
                ("read_session_commit_observations", session_observations),
            ):
                require(
                    getattr(store, method)(org) == expected,
                    f"{method} replay changed canonical Facts",
                )
            require(
                {s.session_id for s in store.read_sessions(org)}
                == {session, outside_session},
                "unexpected aggregate Session",
            )
            reports_after = operational_reports()
            model_rows = json.loads(reports_before["model_report"])["report"]["rows"]
            require(
                len(model_rows) == 1 and model_rows[0]["model"] == "synthetic-model",
                "model report lost the captured model population",
            )
            model_stage = {
                key: model_rows[0][key]
                for key in (
                    "completions",
                    "explicit_accepts",
                    "attributed_inference_calls",
                    "ci_linked",
                    "ci_passed",
                )
            }
            lifecycle = json.loads(reports_before["lifecycle_report"])["report"]
            require(
                lifecycle["org_id"] == org, "lifecycle report organization mismatch"
            )
            accepted = lifecycle["accepted_work"]
            lifecycle_stage = {
                "accepted_calls": accepted["accepted_calls"],
                "observed_accepts": accepted["coverage"]["observed"],
                "attributed": accepted["attributed"]["count"],
                "edit_observations": lifecycle["edit_retention"]["coverage"][
                    "observed"
                ],
                "pull_request_membership": accepted["pull_request_membership"]["count"],
                "missing_pull_request_membership": accepted["skips"].get(
                    "pull_request_membership_unavailable", 0
                ),
                "ci_linked": accepted["ci_linked"]["count"],
            }
            for name, stage in (
                ("model_report", model_stage),
                ("lifecycle_report", lifecycle_stage),
            ):
                stage.update(
                    authenticated=report_auth[name],
                    bytes_equal=reports_before[name] == reports_after[name],
                    sha256_before=hashlib.sha256(reports_before[name]).hexdigest(),
                    sha256_after=hashlib.sha256(reports_after[name]).hexdigest(),
                    scope=report_params,
                )
            terminal_receipts = [
                json.loads(path.read_bytes())
                for path in sorted(queue.glob("*.receipt"))
            ]
            lost_receipt = next(
                item
                for item in terminal_receipts
                if item["delivery_id"] == lost_ack["delivery_id"]
            )
            prepared_requests = [*gateway_requests, *otlp_requests]
            prepared_bodies = {request.body for request in prepared_requests}
            final_delivery_status = json.loads(command("delivery", "status"))
            delivery_stage = {
                "gateway_queued": callback_status["pending"],
                "otlp_queued": len(otlp_requests),
                "terminal_receipts": len(terminal_receipts),
                "pending": final_delivery_status["pending"],
                "sender_restarted": len(replay_runs) >= 3,
                "callback_worker_stopped": callback_status["worker_running"] is False,
                "source_bytes_preserved": set(received) == prepared_bodies,
                "capture_instants_preserved": all(
                    call.observed_at
                    == datetime.fromisoformat(
                        json.loads(body)["capture"]["observed_at"]
                    )
                    for call, body in zip(calls, gateway_payloads, strict=True)
                ),
                "transcript_file_changed": transcript_file_changed,
                "helper_version": versions["sediment-cli"],
                "callback_runtime": "synthetic CustomLogger import stand-in",
                "configuration": {
                    "mode": "explicit buffered capture",
                    "destinations": "owned loopback API and acknowledgment intermediary",
                    "format_version": delivery.FORMAT_VERSION,
                    "max_active_bytes": delivery.MAX_ACTIVE_BYTES,
                    "max_active_entries": delivery.MAX_ACTIVE_ENTRIES,
                    "max_entry_bytes": delivery.MAX_ENTRY_BYTES,
                    "replay_window_seconds": delivery.REPLAY_WINDOW_SECONDS,
                    "receipt_retention_seconds": delivery.RECEIPT_RETENTION_SECONDS,
                    "max_receipts": delivery.MAX_RECEIPTS,
                    "http_timeout_seconds": delivery.HTTP_TIMEOUT_SECONDS,
                    "max_batch_attempts": delivery.MAX_BATCH_ATTEMPTS,
                },
                "receipts": terminal_receipts,
                "replay_batches": replay_runs,
                "prepared_requests": [
                    {
                        "delivery_id": request.delivery_id,
                        "channel": request.channel,
                        "captured_at": request.captured_at,
                        "sha256": hashlib.sha256(request.body).hexdigest(),
                    }
                    for request in prepared_requests
                ],
            }
            lost_ack_stage = {
                "committed_before_retry": committed_before_retry,
                "retained_fact_id": lost_receipt.get("fact_id")
                == lost_ack["receipt"]["fact_id"]
                and lost_receipt.get("stored") is False,
                "gateway_duplicates": sum(
                    item["stored"] is False for item in upstream_receipts
                ),
                "inference_calls": len(calls),
            }
            policy_file = workspace / "policy.toml"
            policy_file.write_text("schema_version = 1\n[split]\neval_fraction = 0.0\n")

            def derive(name, scoped=True):
                destination = workspace / name
                command(
                    "derive",
                    "--out",
                    str(destination),
                    "--policy",
                    str(policy_file),
                    *(["--users", "selected-user"] if scoped else []),
                )
                result = read_derived_bundle(destination)
                validate_derived_bundle(result)
                return destination, result

            all_path, all_bundle = derive("all", scoped=False)
            first_path, first = derive("first")
            second_path, second = derive("second")
            require(
                len(all_bundle.attributed_completions) == len(all_bundle.rollouts) == 2,
                "cohort control artifact stage absent",
            )
            require(
                [row.inference_call_id for row in first.attributed_completions]
                == call_ids[:1],
                "selected Attributed completion mismatch",
            )
            require(
                [c.inference_call_id for c in first.inference_calls] == call_ids[:3],
                "selected full-call population mismatch",
            )
            require(
                {c.inference_call_id for c in first.inference_call_identities}
                == set(call_ids),
                "bundle lost outside-cohort identity evidence",
            )
            require(
                len(first.repository_identities) == 4
                and len(first.repository_renames) == 1
                and first.repository_renames[0].rename_id == rename_receipt["fact_id"]
                and all(
                    row.repository_id == "770001" for row in first.repository_identities
                ),
                "bundle lost complete repository source evidence",
            )
            require(
                len(first.rollouts) == 1
                and [len(segment) for segment in first.rollouts[0].segments]
                == [1, 1, 1],
                "Rollout fragmentation lost Turns",
            )
            require(
                first.fragmented == {"prior_output_not_replayed": 2},
                "fragmentation accounting mismatch",
            )
            require(
                first.excluded == PIPELINE_EXPECTATIONS["cohort"],
                "cohort exclusion accounting mismatch",
            )
            require(first == second, "same-input bundle identity mismatch")

            def bytes_of(directory):
                return {
                    str(p.relative_to(directory)): p.read_bytes()
                    for p in sorted(directory.rglob("*"))
                    if p.is_file()
                }

            def training(bundle_path, name):
                output = workspace / name
                sft_out = command(
                    "export",
                    "sft",
                    "--recipe",
                    "sft_verified",
                    "--from",
                    str(bundle_path),
                    "--out",
                    str(output),
                )
                rlvr_out = command(
                    "export",
                    "rlvr",
                    "--target",
                    "sediment",
                    "--from",
                    str(bundle_path),
                    "--out",
                    str(output),
                )

                def rows(filename):
                    path = output / filename
                    require(path.is_file(), f"training file {filename} absent")
                    return [
                        json.loads(
                            line,
                            parse_constant=lambda value: (_ for _ in ()).throw(
                                ValueError(value)
                            ),
                        )
                        for line in path.read_text().splitlines()
                    ]

                sft, tasks, rollouts = (
                    rows("sft.jsonl"),
                    rows("tasks.jsonl"),
                    rows("rollouts.jsonl"),
                )
                require(
                    len(sft) == len(tasks) == 1 and len(rollouts) == 2,
                    "training file row counts mismatch",
                )
                require(
                    "rollout rows: 2  skipped: {'unrepresentable_unicode': 1}"
                    in rlvr_out
                    and "samples projected: 1  skipped: {}" in sft_out,
                    "strict projection decline accounting mismatch",
                )
                for filename, emitted in (
                    ("sft.jsonl", sft),
                    ("tasks.jsonl", tasks),
                    ("rollouts.jsonl", rollouts),
                ):
                    require(
                        f"wrote {output / filename} ({len(emitted)} rows)"
                        in sft_out + rlvr_out,
                        "published row receipt disagrees with file",
                    )
                require(
                    sft[0]["metadata"]["completion_id"] == call_ids[0],
                    "SFT row identity mismatch",
                )
                require(
                    all(
                        row["repository_identity"] == repository_identity
                        for row in [sft[0]["metadata"], tasks[0], *rollouts]
                    ),
                    "training row lost repository identity across rename",
                )
                require(
                    sft[0]["metadata"]["eligibility_source"] == "resolved_ci_pass"
                    and sft[0]["metadata"]["recipe_version"] == 1
                    and sft[0]["metadata"]["recipe_id"] == "sft_verified",
                    "SFT evidence recipe mismatch",
                )
                require(
                    sft[0]["prompt"][0]["content"] == prompt
                    and sft[0]["completion"][0]["content"] == completion,
                    "SFT source content mismatch",
                )
                observation_ids = [
                    o.observation_id
                    for o in session_observations
                    if o.session_id == session
                ]
                require(
                    sft[0]["metadata"]["attribution_source"] == "git_notes"
                    and sft[0]["metadata"]["session_commit_observation_ids"]
                    == observation_ids,
                    "SFT source relationship mismatch",
                )
                for row in [tasks[0], *rollouts]:
                    require(
                        row["recipe_id"] == "rlvr_ci"
                        and row["recipe_version"] == 1
                        and row["attribution_source"] == "git_notes"
                        and row["session_commit_observation_ids"] == observation_ids,
                        "RLVR source metadata mismatch",
                    )
                require(
                    tasks[0]["ci_resolution"]["source_outcome_ids"]
                    == [ci_receipt["fact_id"]]
                    and tasks[0]["ci_resolution"]["verdict"] == "passed",
                    "task Reward source mismatch",
                )
                require(
                    [r["turns"][0]["inference_call_id"] for r in rollouts]
                    == call_ids[:2],
                    "RLVR Segment identities mismatch",
                )
                require(
                    [r["segment_index"] for r in rollouts] == [0, 1],
                    "RLVR Segment indexes mismatch",
                )
                for row in rollouts:
                    require(
                        len(row["turns"]) == 1
                        and row["turns"][0]["new_messages"]
                        == [
                            {
                                "role": "user",
                                "parts": [{"type": "text", "content": prompt}],
                            }
                        ]
                        and row["turns"][0]["completion"] == completion
                        and row["turns"][0]["tool_calls"] == [],
                        "RLVR Turn source content mismatch",
                    )
                    require(
                        row["reward_source"] == "resolved_ci_pass"
                        and row["ci_resolution"]["verdict"] == "passed"
                        and row["ci_resolution"]["source_outcome_ids"]
                        == [ci_receipt["fact_id"]],
                        "RLVR Reward source mismatch",
                    )
                return output, {
                    "sft_rows": len(sft),
                    "task_rows": len(tasks),
                    "rollout_rows": len(rollouts),
                    "unicode_declines": 1,
                    "row_ids": {
                        "sft": [sft[0]["metadata"]["completion_id"]],
                        "tasks": [tasks[0]["instance_id"]],
                        "rollouts": [
                            [r["instance_id"], r["segment_index"]] for r in rollouts
                        ],
                    },
                }

            first_output, training_counts = training(first_path, "training-first")
            second_output, _ = training(second_path, "training-second")
            require(
                bytes_of(first_path) == bytes_of(second_path),
                "same-input bundle bytes differ",
            )
            require(
                bytes_of(first_output) == bytes_of(second_output),
                "same-input training bytes differ",
            )

            command(
                "quarantine",
                "inference_calls",
                call_ids[0],
                "--reason",
                "synthetic acceptance",
            )
            _, quarantined = derive("quarantined")
            require(
                call_ids[0]
                not in {
                    c.inference_call_id
                    for c in quarantined.inference_calls
                    + quarantined.inference_call_identities
                },
                "quarantine leaked source identity",
            )
            require(
                call_ids[0]
                not in {c.inference_call_id for c in store.read_inference_calls(org)},
                "quarantine remained visible",
            )
            command(
                "release",
                "inference_calls",
                call_ids[0],
                "--reason",
                "synthetic acceptance",
            )
            restored_path, restored = derive("restored")
            require(
                store.read_inference_calls(org) == calls,
                "release mutated captured Facts",
            )
            restored_output, restored_training = training(
                restored_path, "training-restored"
            )
            require(
                restored_training == training_counts,
                "release did not restore training identities",
            )
            require(
                (
                    first.quarantine_revision,
                    quarantined.quarantine_revision,
                    restored.quarantine_revision,
                )
                == (0, 1, 2),
                "quarantine Provenance revision mismatch",
            )
            comparable = replace(
                restored,
                quarantine_revision=0,
                attributed_completions=tuple(
                    replace(
                        row, provenance=replace(row.provenance, quarantine_revision=0)
                    )
                    for row in restored.attributed_completions
                ),
                rollouts=tuple(
                    replace(
                        row, provenance=replace(row.provenance, quarantine_revision=0)
                    )
                    for row in restored.rollouts
                ),
            )
            require(
                comparable == first,
                "release changed content, labels, exclusions or policy beyond its revision",
            )
            require(
                bytes_of(first_path) != bytes_of(restored_path),
                "quarantine revision absent from bundle bytes",
            )

            tampered_path = workspace / "tampered"
            shutil.copytree(first_path, tampered_path)
            artifact_path = tampered_path / "rollouts.jsonl"
            envelopes = [
                json.loads(line) for line in artifact_path.read_text().splitlines()
            ]
            inner = json.loads(envelopes[0]["record_json"])
            inner["segments"][0][0]["completion"] = "substituted training text"
            envelopes[0]["record_json"] = json.dumps(inner)
            artifact_path.write_text(
                "".join(json.dumps(row) + "\n" for row in envelopes)
            )
            manifest_path = tampered_path / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["files"]["rollouts"]["bytes"] = len(artifact_path.read_bytes())
            manifest["files"]["rollouts"]["sha256"] = hashlib.sha256(
                artifact_path.read_bytes()
            ).hexdigest()
            manifest_path.write_text(json.dumps(manifest))
            try:
                read_derived_bundle(tampered_path)
            except BundleValidationError:
                pass
            else:
                require(False, "rehashed contradictory bundle admitted")
            rejected_output = workspace / "training-tampered"
            command(
                "export",
                "sft",
                "--from",
                str(tampered_path),
                "--out",
                str(rejected_output),
                success=False,
            )
            require(
                not rejected_output.exists(), "invalid bundle published training rows"
            )

            identity_tampered_path = workspace / "identity-tampered"
            shutil.copytree(first_path, identity_tampered_path)
            evidence_path = identity_tampered_path / "repository_identities.jsonl"
            envelopes = [
                json.loads(line) for line in evidence_path.read_text().splitlines()
            ]
            for envelope in envelopes:
                row = json.loads(envelope["record_json"])
                if row["source_fact_id"] == push_receipt["fact_id"]:
                    row["repository_id"] = "770002"
                    envelope["record_json"] = json.dumps(row)
            evidence_path.write_text(
                "".join(json.dumps(row) + "\n" for row in envelopes)
            )
            evidence_manifest_path = identity_tampered_path / "manifest.json"
            evidence_manifest = json.loads(evidence_manifest_path.read_text())
            evidence_metadata = evidence_manifest["files"]["repository_identities"]
            evidence_metadata["bytes"] = len(evidence_path.read_bytes())
            evidence_metadata["sha256"] = hashlib.sha256(
                evidence_path.read_bytes()
            ).hexdigest()
            evidence_manifest_path.write_text(json.dumps(evidence_manifest))
            identity_rejected_output = workspace / "identity-tampered-training"
            command(
                "export",
                "sft",
                "--from",
                str(identity_tampered_path),
                "--out",
                str(identity_rejected_output),
                success=False,
            )
            require(
                not identity_rejected_output.exists(),
                "changed repository evidence published training rows",
            )

            commit_response = httpx.get(
                url + f"/query/commit/{head}",
                params={
                    "repo": renamed_repo,
                    "repository_provider": "github",
                    "repository_host": "github.com",
                    "repository_id": "770001",
                    "as_of": report_params["as_of"],
                },
                headers={"Authorization": "Bearer " + operator_token},
                timeout=30,
            )
            require(
                commit_response.status_code == 200,
                "identified commit investigation failed after rename",
            )
            commit_result = commit_response.json()
            require(
                commit_result["attributed"] and len(commit_result["repos"]) == 1,
                "renamed repository lost its commit evidence",
            )
            commit_repository = commit_result["repos"][0]
            require(
                commit_repository["repository_identity"] == repository_identity
                and commit_repository["ci_outcomes"][0]["outcome_id"]
                == ci_receipt["fact_id"],
                "commit investigation changed the repository or CI source",
            )

            # Default logging is message-only. Validate actual server receipts,
            # not the HTTP {} or a second local call to the translator.
            receipt_lines = [
                line.split("otlp_logs_received ", 1)[1]
                for line in log_path.read_text().splitlines()
                if "otlp_logs_received " in line
            ]
            require(len(receipt_lines) == 4, "completed OTLP receipt stage absent")
            receipts = []
            for line in receipt_lines:
                match = re.fullmatch(
                    r"org_id=(\S+) record_counts=(.+) malformed_containers=(\d+) fact_counts=(.+) retry_linkage_skips=(.+)",
                    line,
                )
                require(
                    match is not None
                    and match[1] == org
                    and match[3] == "0"
                    and json.loads(match[5]) == {},
                    "OTLP receipt unit/organization absent",
                )
                records, facts = json.loads(match[2]), json.loads(match[4])
                receipts.append({"records": records, "facts": facts})
                require(
                    records["received"]
                    == sum(
                        records[key]
                        for key in ("translated", "untranslated", "malformed")
                    ),
                    "record partition mismatch",
                )
                require(
                    all(
                        value["candidates"] == value["stored"] + value["duplicates"]
                        for value in facts.values()
                    ),
                    "Fact receipt conservation mismatch",
                )
            require(
                validate_pipeline_receipts(receipts),
                "record and candidate/stored/duplicate Fact counts mismatch",
            )

            # A separate acceptance population follows the fixed corpus and its
            # reports. Each observation fits the capture limit; their combined
            # envelope exceeds one transport entry. Use the installed producer,
            # replay worker, HTTP receiver, and database for this boundary.
            batch_session = "rehearsal-batch-session"
            batch_target = work / "batch.py"
            batch_observed = "x" * (256 * 1024)
            batch_target.write_text(batch_observed)
            batch_when = when + timedelta(hours=1)
            batch_call_ids = [f"rehearsal-batch-{index:02d}" for index in range(32)]
            batch_entries = []
            for call_id in batch_call_ids:
                batch_entries.extend(
                    [
                        {
                            "type": "assistant",
                            "sessionId": batch_session,
                            "timestamp": batch_when.isoformat(),
                            "message": {
                                "content": [
                                    {
                                        "type": "tool_use",
                                        "id": call_id,
                                        "name": "Edit",
                                        "input": {
                                            "file_path": str(batch_target),
                                            "old_string": "before",
                                            "new_string": "x",
                                        },
                                    }
                                ]
                            },
                        },
                        {
                            "type": "user",
                            "sessionId": batch_session,
                            "timestamp": batch_when.isoformat(),
                            "message": {
                                "content": [
                                    {
                                        "type": "tool_result",
                                        "tool_use_id": call_id,
                                        "is_error": False,
                                        "content": "edited",
                                    }
                                ]
                            },
                        },
                    ]
                )
            batch_transcript = workspace / "claude-large-v1.jsonl"
            batch_source = "".join(json.dumps(entry) + "\n" for entry in batch_entries)
            batch_transcript.write_text(batch_source)
            before_batch = store.read_edit_observations(org)
            stop_api()
            received_before_batch = len(received)
            command(
                "transcript",
                "--agent",
                "claude-code",
                input=json.dumps(
                    {
                        "session_id": batch_session,
                        "transcript_path": str(batch_transcript),
                    }
                ),
            )
            batch_requests = queued_requests()
            require(
                len(batch_requests) == 2
                and all(
                    len(request.body) <= delivery.MAX_ENTRY_BYTES
                    for request in batch_requests
                )
                and sum(len(request.body) for request in batch_requests)
                > delivery.MAX_ENTRY_BYTES,
                "large transcript did not enqueue two bounded requests",
            )
            replay_batch()
            require(
                store.read_edit_observations(org) == before_batch
                and json.loads(command("delivery", "status"))["pending"] == 2,
                "large transcript outage did not preserve the pending population",
            )
            batch_target.write_text("changed after capture\n")
            batch_transcript.unlink()
            require(
                set(queued_requests()) == set(batch_requests),
                "large transcript source changes rewrote prepared requests",
            )
            start_api()
            drain()
            batch_facts = [
                fact
                for fact in store.read_edit_observations(org)
                if fact.session_id == batch_session
            ]
            expected_batch_fields = {
                (
                    call_id,
                    batch_session,
                    str(batch_target),
                    "x",
                    batch_observed,
                    batch_when,
                )
                for call_id in batch_call_ids
            }
            require(
                len(batch_facts) == 32
                and {
                    (
                        fact.call_id,
                        fact.session_id,
                        fact.file_path,
                        fact.applied_text,
                        fact.observed_file_text,
                        fact.occurred_at,
                    )
                    for fact in batch_facts
                }
                == expected_batch_fields,
                "large transcript replay changed source identities, text, or times",
            )
            require(
                {request.body for request in batch_requests}
                == set(received[received_before_batch:]),
                "large transcript replay did not send its original HTTP bytes",
            )
            retained_observations = store.read_edit_observations(org)
            require(
                len(retained_observations) == len(before_batch) + 32
                and all(fact in retained_observations for fact in before_batch),
                "large transcript changed the earlier observation population",
            )
            for request in batch_requests:
                post("/v1/logs", request.body)
            require(
                store.read_edit_observations(org) == retained_observations,
                "large transcript duplicate replay changed retained Fact identities",
            )
            batch_stage = {
                "requests": len(batch_requests),
                "edit_observations": len(batch_facts),
                "pending": json.loads(command("delivery", "status"))["pending"],
                "source_changed": batch_target.read_text() != batch_observed,
                "transcript_removed": not batch_transcript.exists(),
                "prepared_bytes_replayed": True,
                "fact_ids_retained": True,
                "call_ids": sorted(fact.call_id for fact in batch_facts),
                "fact_ids": sorted(fact.observation_id for fact in batch_facts),
                "request_sha256": sorted(
                    hashlib.sha256(request.body).hexdigest()
                    for request in batch_requests
                ),
                "transcript_sha256": hashlib.sha256(batch_source.encode()).hexdigest(),
            }
            manifest = json.loads((first_path / "manifest.json").read_text())
            with engine.connect() as connection:
                postgresql_version = connection.exec_driver_sql(
                    "SHOW server_version"
                ).scalar_one()
            installed_modules = {
                name: str(Path(module.__file__).resolve())
                for name, module in sorted(sys.modules.items())
                if name.startswith("sediment_") and getattr(module, "__file__", None)
            }
            venv = sediment.parent.parent.resolve()
            require(
                all(
                    Path(path).is_relative_to(venv)
                    for path in installed_modules.values()
                ),
                "loaded Sediment module outside installed environment",
            )
            return {
                "fixture_version": PIPELINE_FIXTURE_VERSION,
                "runtime_versions": versions,
                "installed_modules": installed_modules,
                "venv": str(venv),
                "python_version": sys.version.split()[0],
                "postgresql_version": postgresql_version,
                "transcript_wire": "sediment-transcript/1; synthetic Claude Code JSONL",
                "entry_points": [
                    "sediment server",
                    "sediment login --with-token",
                    "sediment login --capture --with-token",
                    "sediment install --codex-profile",
                    "POST /ingest/gateway",
                    "POST /v1/logs",
                    "sediment transcript --agent claude-code",
                    "SedimentCallback.async_log_success_event",
                    "sediment delivery enqueue",
                    "sediment delivery replay",
                    "sediment delivery status",
                    "POST /ingest/github/push",
                    "POST /ingest/github/repository",
                    "POST /ingest/ci",
                    "GET /query/commit/{sha}",
                    "GET /v1/reports/model-outcomes",
                    "GET /v1/reports/accepted-work-lifecycle",
                    "sediment derive",
                    "sediment quarantine",
                    "sediment release",
                    "sediment export sft --recipe sft_verified --from",
                    "sediment export rlvr --target sediment --from",
                ],
                "versions": {
                    "implementation": manifest["implementation_versions"],
                    "policy_digest": manifest["policy_digest"],
                    "policy": manifest["policy"],
                    "as_of": manifest["as_of"],
                    "mirror_revisions": manifest["mirror_revisions"],
                },
                "source_sha256": {
                    "fixture": hashlib.sha256(fixture_bytes).hexdigest(),
                    "transcript": hashlib.sha256(transcript.read_bytes()).hexdigest(),
                    "gateway": [
                        hashlib.sha256(body).hexdigest() for body in gateway_payloads
                    ],
                    "otlp": hashlib.sha256(decision_payload).hexdigest(),
                    "callback": hashlib.sha256(callback_source).hexdigest(),
                    "delivery_helper": hashlib.sha256(
                        Path(delivery.__file__).read_bytes()
                    ).hexdigest(),
                },
                "stages": {
                    "authority": authority_stage,
                    "transcript_batch": batch_stage,
                    "capture": {
                        "inference_calls": len(calls),
                        "developer_decisions": len(decisions),
                        "gateway_ids": call_ids,
                        "otlp_receipts": receipts,
                    },
                    "transcript": {
                        "edit_observations": len(observations),
                        "rejected_edits": len(rejected_edits),
                        "retry_linkages": len(retries),
                        "fact_ids": [
                            observation.observation_id,
                            rejected.rejection_id,
                            retry.retry_linkage_id,
                        ],
                    },
                    "bundle": {
                        "version": manifest["bundle_schema_version"],
                        "identities": len(first.inference_call_identities),
                        "segments": len(first.rollouts[0].segments),
                        "counts": manifest["counts"],
                        "fragmented": first.fragmented,
                    },
                    "repository_identity": {
                        "source_roles": len(first.repository_identities),
                        "rename_receipts": len(first.repository_renames),
                        "renamed_ci_linked": True,
                        "training_identity_preserved": True,
                        "identity_tampering_rejected": True,
                        "identity": repository_identity,
                        "observed_repo_slugs": commit_repository["observed_repo_slugs"],
                    },
                    "training": training_counts,
                    "cohort": first.excluded,
                    "model_report": model_stage,
                    "lifecycle_report": lifecycle_stage,
                    "delivery": delivery_stage,
                    "lost_acknowledgment": lost_ack_stage,
                    "quarantine": {
                        "revisions": [
                            first.quarantine_revision,
                            quarantined.quarantine_revision,
                            restored.quarantine_revision,
                        ],
                        "restored": True,
                    },
                    "reproduction": {
                        "bundle_bytes_equal": True,
                        "training_bytes_equal": True,
                    },
                    "tamper": {
                        "rehashed_bundle_refused": True,
                        "training_refused": True,
                    },
                },
            }
        finally:
            if server is not None:
                stop_api()
            intermediary.shutdown()
            intermediary.server_close()
            intermediary_thread.join(timeout=5)
            git_server.shutdown()
            git_server.server_close()
            git_thread.join(timeout=5)
            engine.dispose()


def _project(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))["project"]


def _requirement_name(requirement: str) -> str:
    return re.split(r"[<>=!~\[ ;]", requirement, maxsplit=1)[0].lower()


def validate_source(root: Path, tag: str | None = None) -> list[str]:
    errors: list[str] = []
    release_version = _project(root / PROJECT_FILES[0])["version"]
    if re.fullmatch(r"\d+\.\d+\.\d+(?:rc\d+)?", release_version) is None:
        errors.append(
            f"release version must be X.Y.Z or X.Y.ZrcN; got {release_version!r}"
        )
    root_license = (root / "LICENSE").read_bytes()
    for relative in PROJECT_FILES:
        project = _project(root / relative)
        if project["version"] != release_version:
            errors.append(
                f"{relative}: version {project['version']} != {release_version}"
            )
        for requirement in project.get("dependencies", []):
            name = _requirement_name(requirement)
            if name in FIRST_PARTY and requirement != f"{name}=={release_version}":
                errors.append(
                    f"{relative}: {name} must use exact version {release_version}"
                )
        if relative != PROJECT_FILES[0]:
            if not str(project.get("description", "")).strip():
                errors.append(f"{relative}: summary is missing")
            if project.get("requires-python") != ">=3.12":
                errors.append(f"{relative}: requires-python must be >=3.12")
            if project.get("license") != "AGPL-3.0-or-later":
                errors.append(f"{relative}: license must be AGPL-3.0-or-later")
            if project.get("license-files") != ["LICENSE"]:
                errors.append(f"{relative}: license file must be LICENSE")
            license_path = (root / relative).with_name("LICENSE")
            if not license_path.is_file() or license_path.read_bytes() != root_license:
                errors.append(f"{relative}: license file must match repository LICENSE")
            if set(project.get("urls", {})) != PROJECT_URLS:
                errors.append(
                    f"{relative}: project URLs must be {sorted(PROJECT_URLS)}"
                )

    runtime_path = root / "apps/api/sediment_api/__init__.py"
    runtime_match = re.search(
        r'^__version__\s*=\s*["\']([^"\']+)["\']',
        runtime_path.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    runtime_version = runtime_match.group(1) if runtime_match else None
    if runtime_version != release_version:
        errors.append(
            f"runtime version {runtime_version!r} != release version {release_version}"
        )
    if tag is not None and tag != f"v{release_version}":
        errors.append(f"tag {tag!r} != v{release_version}")
    return errors


def validate_actions(root: Path) -> list[str]:
    errors: list[str] = []
    workflows = root / ".github" / "workflows"
    for path in sorted((*workflows.glob("*.yml"), *workflows.glob("*.yaml"))):
        for line_number, line in enumerate(path.read_text().splitlines(), start=1):
            match = _ACTION_RE.match(line)
            if match is None or match.group(1).startswith("./"):
                continue
            reference, comment = match.groups()
            location = f"{path.relative_to(root)}:{line_number}"
            if re.search(r"@[0-9a-f]{40}$", reference) is None:
                errors.append(f"{location}: action must use an immutable commit SHA")
            if comment is None or re.fullmatch(r"v\d+(?:\.\d+){0,2}", comment) is None:
                errors.append(f"{location}: action needs a readable version comment")
    return errors


def validate_wheels(wheels: Iterable[Path], release_version: str) -> list[str]:
    errors: list[str] = []
    by_name: dict[str, list[Path]] = {name: [] for name in FIRST_PARTY}
    unexpected: list[str] = []
    for path in wheels:
        matches = [
            name
            for name in FIRST_PARTY
            if path.name.startswith(f"{name.replace('-', '_')}-")
        ]
        if len(matches) != 1:
            unexpected.append(path.name)
        else:
            by_name[matches[0]].append(path)
    missing = sorted(name for name, paths in by_name.items() if len(paths) != 1)
    if missing or unexpected:
        errors.append(
            f"wheel set mismatch: expected {sorted(FIRST_PARTY)}; "
            f"missing/duplicate {missing}; unexpected {sorted(unexpected)}"
        )

    for name, paths in sorted(by_name.items()):
        if len(paths) != 1:
            continue
        path = paths[0]
        expected_prefix = f"{name.replace('-', '_')}-{release_version}-"
        if not path.name.startswith(expected_prefix):
            errors.append(f"{path.name}: filename version != {release_version}")
        try:
            with zipfile.ZipFile(path) as archive:
                members = set(archive.namelist())
                metadata_files = [
                    member
                    for member in members
                    if member.endswith(".dist-info/METADATA")
                ]
                if len(metadata_files) != 1:
                    errors.append(f"{path.name}: expected one METADATA file")
                    continue
                metadata = Parser().parsestr(
                    archive.read(metadata_files[0]).decode("utf-8")
                )
                dist_info = metadata_files[0].rsplit("/", 1)[0]
                license_member = f"{dist_info}/licenses/LICENSE"
                license_content = (
                    archive.read(license_member) if license_member in members else None
                )
        except (OSError, UnicodeError, zipfile.BadZipFile) as exc:
            errors.append(f"{path.name}: unreadable wheel ({exc})")
            continue

        if metadata.get("Name", "").lower().replace("_", "-") != name:
            errors.append(f"{path.name}: metadata name != {name}")
        if metadata.get("Version") != release_version:
            errors.append(f"{path.name}: metadata version != {release_version}")
        if not metadata.get("Summary", "").strip():
            errors.append(f"{path.name}: summary is missing")
        if metadata.get("Requires-Python") != ">=3.12":
            errors.append(f"{path.name}: Requires-Python must be >=3.12")
        if metadata.get("License-Expression") != "AGPL-3.0-or-later":
            errors.append(f"{path.name}: license expression is missing")
        if metadata.get_all("License-File", []) != ["LICENSE"]:
            errors.append(f"{path.name}: license file metadata must name LICENSE")
        if license_content is None:
            errors.append(f"{path.name}: license file is missing")
        elif license_content != REPOSITORY_LICENSE_CONTENT:
            errors.append(
                f"{path.name}: license file content does not match repository LICENSE"
            )
        url_labels = {
            value.split(",", 1)[0].strip()
            for value in metadata.get_all("Project-URL", [])
            if "," in value
        }
        if url_labels != PROJECT_URLS:
            errors.append(f"{path.name}: project URLs must be {sorted(PROJECT_URLS)}")
        for requirement in metadata.get_all("Requires-Dist", []):
            dependency = _requirement_name(requirement)
            if dependency in FIRST_PARTY and requirement != (
                f"{dependency}=={release_version}"
            ):
                errors.append(
                    f"{path.name}: {dependency} must use exact version "
                    f"{release_version}"
                )
        for required in REQUIRED_CONTENT[name]:
            if required not in members:
                errors.append(f"{path.name}: missing package content {required}")
        if any("enterprise" in PurePosixPath(member).parts for member in members):
            errors.append(f"{path.name}: forbidden package content enterprise/")
    return errors


def validate_sdists(sdists: Iterable[Path], release_version: str) -> list[str]:
    errors: list[str] = []
    by_name: dict[str, list[Path]] = {name: [] for name in FIRST_PARTY}
    unexpected: list[str] = []
    for path in sdists:
        matches = [
            name
            for name in FIRST_PARTY
            if path.name.startswith(f"{name.replace('-', '_')}-")
        ]
        if len(matches) != 1:
            unexpected.append(path.name)
        else:
            by_name[matches[0]].append(path)
    missing = sorted(name for name, paths in by_name.items() if len(paths) != 1)
    if missing or unexpected:
        errors.append(
            f"source distribution set mismatch: expected {sorted(FIRST_PARTY)}; "
            f"missing/duplicate {missing}; unexpected {sorted(unexpected)}"
        )

    for name, paths in sorted(by_name.items()):
        if len(paths) != 1:
            continue
        path = paths[0]
        normalized = name.replace("-", "_")
        expected_root = f"{normalized}-{release_version}"
        if path.name != f"{expected_root}.tar.gz":
            errors.append(f"{path.name}: filename version != {release_version}")
        try:
            with tarfile.open(path, "r:gz") as archive:
                members = set(archive.getnames())
                metadata_member = f"{expected_root}/PKG-INFO"
                metadata_file = archive.extractfile(metadata_member)
                if metadata_file is None:
                    errors.append(f"{path.name}: expected one PKG-INFO file")
                    continue
                metadata = Parser().parsestr(metadata_file.read().decode("utf-8"))
                license_member = f"{expected_root}/LICENSE"
                license_file = (
                    archive.extractfile(license_member)
                    if license_member in members
                    else None
                )
                license_content = license_file.read() if license_file else None
        except (KeyError, OSError, UnicodeError, tarfile.TarError) as exc:
            errors.append(f"{path.name}: unreadable source distribution ({exc})")
            continue

        if metadata.get("Name", "").lower().replace("_", "-") != name:
            errors.append(f"{path.name}: metadata name != {name}")
        if metadata.get("Version") != release_version:
            errors.append(f"{path.name}: metadata version != {release_version}")
        if not metadata.get("Summary", "").strip():
            errors.append(f"{path.name}: summary is missing")
        if metadata.get("Requires-Python") != ">=3.12":
            errors.append(f"{path.name}: Requires-Python must be >=3.12")
        if metadata.get("License-Expression") != "AGPL-3.0-or-later":
            errors.append(f"{path.name}: license expression is missing")
        if metadata.get_all("License-File", []) != ["LICENSE"]:
            errors.append(f"{path.name}: license file metadata must name LICENSE")
        url_labels = {
            value.split(",", 1)[0].strip()
            for value in metadata.get_all("Project-URL", [])
            if "," in value
        }
        if url_labels != PROJECT_URLS:
            errors.append(f"{path.name}: project URLs must be {sorted(PROJECT_URLS)}")
        for requirement in metadata.get_all("Requires-Dist", []):
            dependency = _requirement_name(requirement)
            if dependency in FIRST_PARTY and requirement != (
                f"{dependency}=={release_version}"
            ):
                errors.append(
                    f"{path.name}: {dependency} must use exact version "
                    f"{release_version}"
                )
        for required in ("pyproject.toml", "LICENSE", *REQUIRED_CONTENT[name]):
            member = f"{expected_root}/{required}"
            if member not in members:
                errors.append(f"{path.name}: missing package content {required}")
        if (
            license_content is not None
            and license_content != REPOSITORY_LICENSE_CONTENT
        ):
            errors.append(
                f"{path.name}: license file content does not match repository LICENSE"
            )
        if any("enterprise" in PurePosixPath(member).parts for member in members):
            errors.append(f"{path.name}: forbidden package content enterprise/")
    return errors


def rebuild_wheels_from_sdists(
    sdists: Iterable[Path], out_dir: Path, cwd: Path
) -> tuple[list[Path], list[str]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    for sdist in sdists:
        build = subprocess.run(
            [
                "uv",
                "build",
                str(sdist),
                "--wheel",
                "--out-dir",
                str(out_dir),
            ],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
        )
        if build.returncode != 0:
            errors.append(
                f"{sdist.name}: source distribution wheel build failed with "
                f"exit code {build.returncode}"
            )
    return sorted(out_dir.glob("*.whl")), errors


def validate_installed_hooks(settings_path: Path, sediment: Path) -> list[str]:
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"installed transcript settings are unreadable ({exc})"]
    hooks = settings.get("hooks", {}) if isinstance(settings, dict) else {}
    expected = {
        "SessionEnd": f'"{sediment}" transcript --agent claude-code || true',
        "PreToolUse": (f'"{sediment}" transcript snapshot --agent claude-code || true'),
    }
    errors: list[str] = []
    for event, expected_command in expected.items():
        blocks = hooks.get(event, []) if isinstance(hooks, dict) else []
        commands = [
            hook.get("command")
            for block in blocks
            if isinstance(block, dict)
            for hook in block.get("hooks", [])
            if isinstance(hook, dict)
        ]
        if commands != [expected_command]:
            errors.append(
                f"{event} must invoke the installed sediment command; got {commands}"
            )
    pre_tool = hooks.get("PreToolUse", []) if isinstance(hooks, dict) else []
    if not (
        len(pre_tool) == 1
        and isinstance(pre_tool[0], dict)
        and pre_tool[0].get("matcher") == "Edit|Write"
    ):
        errors.append("PreToolUse must use the Edit|Write matcher")
    return errors


def _run_installed_worker(
    command: list[str], *, cwd: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Bound the installed worker and stop its owned descendants before return."""
    worker = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        try:
            stdout, stderr = worker.communicate(timeout=180)
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                "runtime rehearsal: installed pipeline timed out"
            ) from None
        except KeyboardInterrupt:
            raise RuntimeError(
                "runtime rehearsal: installed pipeline interrupted"
            ) from None
        return subprocess.CompletedProcess(command, worker.returncode, stdout, stderr)
    finally:
        try:
            # A stopped leader can leave descendants alive. Address the group
            # even after communicate() reaps the leader, without process scans.
            for stop_signal in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(worker.pid, stop_signal)
                except ProcessLookupError:
                    pass
                try:
                    worker.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    if stop_signal == signal.SIGKILL:
                        raise RuntimeError(
                            "runtime rehearsal: installed pipeline cleanup timed out"
                        ) from None
        finally:
            worker.stdout.close()
            worker.stderr.close()


def rehearse(
    root: Path,
    database_url: str,
    tag: str | None = None,
    out_dir: Path | None = None,
) -> list[str]:
    errors = validate_source(root, tag)
    errors.extend(validate_actions(root))
    if errors:
        return errors
    release_version = _project(root / "pyproject.toml")["version"]
    preserved_artifacts_dir: Path | None = None
    if out_dir is not None:
        preserved_artifacts_dir = out_dir if out_dir.is_absolute() else root / out_dir
        if preserved_artifacts_dir.exists() and any(preserved_artifacts_dir.iterdir()):
            return [
                f"artifact output directory must be empty: {preserved_artifacts_dir}"
            ]
        preserved_artifacts_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sediment-release-") as raw_temp:
        temp = Path(raw_temp)
        artifacts_dir = preserved_artifacts_dir or temp / "dist"
        wheel_build = subprocess.run(
            [
                "uv",
                "build",
                "--all-packages",
                "--wheel",
                "--out-dir",
                str(artifacts_dir),
            ],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
        if wheel_build.returncode != 0:
            return [f"wheel build failed with exit code {wheel_build.returncode}"]
        sdist_build = subprocess.run(
            [
                "uv",
                "build",
                "--all-packages",
                "--sdist",
                "--out-dir",
                str(artifacts_dir),
            ],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
        if sdist_build.returncode != 0:
            return [
                "source distribution build failed with exit code "
                f"{sdist_build.returncode}"
            ]
        wheels = sorted(artifacts_dir.glob("*.whl"))
        sdists = sorted(artifacts_dir.glob("*.tar.gz"))
        errors = validate_wheels(wheels, release_version)
        errors.extend(validate_sdists(sdists, release_version))
        if errors:
            return errors
        rebuilt_wheels, errors = rebuild_wheels_from_sdists(
            sdists, temp / "rebuilt", temp
        )
        if errors:
            return errors
        errors = validate_wheels(rebuilt_wheels, release_version)
        if errors:
            return [f"source distribution rebuild: {error}" for error in errors]

        venv = temp / "venv"
        create_venv = subprocess.run(
            ["uv", "venv", "--python", "3.12", str(venv)],
            cwd=temp,
            check=False,
            capture_output=True,
            text=True,
        )
        if create_venv.returncode != 0:
            return [
                f"isolated environment creation failed with exit code "
                f"{create_venv.returncode}"
            ]
        python = venv / "bin" / "python"
        install = subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(python),
                *[str(wheel) for wheel in wheels],
            ],
            cwd=temp,
            check=False,
            capture_output=True,
            text=True,
        )
        if install.returncode != 0:
            return [f"wheel installation failed with exit code {install.returncode}"]

        sediment = venv / "bin" / "sediment"
        home = temp / "home"
        (home / ".claude").mkdir(parents=True)
        repo = temp / "repo"
        git_init = subprocess.run(
            ["git", "init", str(repo)],
            cwd=temp,
            check=False,
            capture_output=True,
            text=True,
        )
        if git_init.returncode != 0:
            return [f"runtime rehearsal: git init exited {git_init.returncode}"]
        inherited_path = os.environ.get("PATH", "/usr/bin:/bin")
        isolated_env = {
            **{
                key: value
                for key, value in os.environ.items()
                if key not in {"PYTHONPATH", "VIRTUAL_ENV"}
                and not key.startswith(("SEDIMENT_", "OTEL_"))
            },
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "PATH": f"{venv / 'bin'}{os.pathsep}{inherited_path}",
            "SEDIMENT_DEV_MODE": "false",
            "SEDIMENT_ORG_ID": "release-rehearsal",
        }

        try:
            with scratch_database(database_url) as runtime_url:
                isolated_env["SEDIMENT_DATABASE_URL"] = runtime_url
                isolated_env["SEDIMENT_TEST_DATABASE_URL"] = runtime_url
                commands = (
                    ("help", [str(sediment), "--help"]),
                    ("version", [str(sediment), "--version"]),
                    (
                        "db upgrade",
                        [
                            str(sediment),
                            "db",
                            "upgrade",
                        ],
                    ),
                    (
                        "db status",
                        [
                            str(sediment),
                            "db",
                            "status",
                        ],
                    ),
                    (
                        "facts",
                        [str(sediment), "facts"],
                    ),
                    (
                        "transcript install",
                        [
                            str(sediment),
                            "install",
                            str(repo),
                            "--transcripts",
                            "--no-agents",
                            "--no-env",
                        ],
                    ),
                )
                results: dict[str, subprocess.CompletedProcess[str]] = {}
                for label, command in commands:
                    result = subprocess.run(
                        command,
                        cwd=temp,
                        env=isolated_env,
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    results[label] = result
                    if result.returncode != 0:
                        return [
                            f"runtime rehearsal: {label} exited {result.returncode}"
                        ]
                if results["version"].stdout.strip() != f"sediment {release_version}":
                    return [
                        "runtime rehearsal: version output "
                        f"{results['version'].stdout.strip()!r} != sediment {release_version}"
                    ]
                if "at_head" not in results["db status"].stdout:
                    return ["runtime rehearsal: database status didn't report at_head"]
                if "inference_calls" not in results["facts"].stdout:
                    return ["runtime rehearsal: facts output omitted inference_calls"]
                errors = validate_installed_hooks(
                    home / ".claude" / "settings.json", sediment
                )
                if errors:
                    return errors
                child_code = """import json, runpy, sys
from pathlib import Path
owner = runpy.run_path(sys.argv[1])
try:
    report = owner["exercise_installed_pipeline"](Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4])
except AssertionError as exc:
    print(json.dumps({"error": str(exc)}))
    sys.exit(1)
print(json.dumps(report, sort_keys=True))
"""
                result = _run_installed_worker(
                    [
                        str(python),
                        "-I",
                        "-c",
                        child_code,
                        str(Path(__file__).resolve()),
                        str(temp / "pipeline"),
                        str(sediment),
                        release_version,
                    ],
                    cwd=temp,
                    env=isolated_env,
                )
                try:
                    report = json.loads(result.stdout)
                except (ValueError, TypeError):
                    return ["runtime rehearsal: installed pipeline produced no report"]
                if result.returncode != 0:
                    return [
                        report.get(
                            "error", "runtime rehearsal: installed pipeline failed"
                        )
                    ]
                errors = validate_pipeline_report(report)
                if errors:
                    return errors
                report["wheel_sha256"] = {
                    wheel.name: hashlib.sha256(wheel.read_bytes()).hexdigest()
                    for wheel in wheels
                }
                print("pipeline acceptance: " + json.dumps(report, sort_keys=True))
        except RuntimeError as exc:
            return [str(exc)]
    return []


def build_parser() -> argparse.ArgumentParser:
    database_url = os.environ.get("SEDIMENT_TEST_DATABASE_URL") or os.environ.get(
        "SEDIMENT_DATABASE_URL"
    )
    parser = argparse.ArgumentParser(
        description=(
            "Build and validate all six wheels and source distributions without "
            "publishing."
        )
    )
    parser.add_argument(
        "--database-url",
        default=database_url,
        required=database_url is None,
        help="PostgreSQL administrative URL with permission to create/drop scratch databases",
    )
    parser.add_argument("--tag", help="optional release tag, for example v0.1.0")
    parser.add_argument(
        "--out-dir",
        type=Path,
        help="preserve validated distribution artifacts in this empty directory",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    errors = rehearse(
        Path(__file__).resolve().parents[1],
        args.database_url,
        args.tag,
        args.out_dir,
    )
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    print(
        "release rehearsal passed: six wheels and six source distributions "
        "validated; installed synthetic pipeline validated; nothing published"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
