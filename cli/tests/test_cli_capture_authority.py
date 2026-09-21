# SPDX-License-Identifier: AGPL-3.0-or-later
"""Capture enrollment never distributes a credential with operator authority."""

import io
import json
import os
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from sqlalchemy.engine import make_url

from sediment_cli import attribution, cli
from sediment_cli import client as api_client

OPERATOR = "test-operator-token-3a7e-2f6c"
INGEST = "test-ingest-token-3a7e-9d21"


@pytest.mark.parametrize("capture,token", [(False, INGEST), (True, OPERATOR)])
def test_login_rejects_wrong_authority_without_overwriting_config(
    app_transport, monkeypatch, capsys, capture, token
):
    original = {
        "current": "https://testserver",
        "servers": {"https://testserver": {"token": OPERATOR}},
    }
    api_client.write_config(original)
    monkeypatch.setattr(sys, "stdin", io.StringIO(token + "\n"))
    args = ["login", "https://testserver", "--with-token"]
    if capture:
        args.append("--capture")
    assert cli.main(args) == 1
    assert "authority" in capsys.readouterr().err
    assert api_client.read_config() == original


def test_capture_login_preserves_operator_and_records_api_proof(
    app_transport, monkeypatch
):
    api_client.write_config(
        {"servers": {"https://testserver": {"token": OPERATOR, "note": "keep"}}}
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(INGEST + "\n"))
    assert cli.main(["login", "https://testserver", "--capture", "--with-token"]) == 0
    entry = api_client.read_config()["servers"]["https://testserver"]
    assert entry["token"] == OPERATOR
    assert entry["note"] == "keep"
    assert entry["capture_token"] == INGEST
    assert entry["capture_authority"] == "ingest"
    assert entry["capture_client_id"] == "legacy"
    monkeypatch.setattr(sys, "stdin", io.StringIO(OPERATOR + "\n"))
    assert cli.main(["login", "https://testserver", "--with-token"]) == 0
    assert (
        api_client.read_config()["servers"]["https://testserver"]["capture_token"]
        == INGEST
    )


def test_operator_error_explains_required_login(app_transport, monkeypatch):
    monkeypatch.setenv("SEDIMENT_URL", "https://testserver")
    monkeypatch.setenv("SEDIMENT_SESSION_TOKEN", INGEST)
    with pytest.raises(api_client.ClientError, match="operator.*sediment login"):
        api_client.get_json("/v1/facts")


def configured_home(tmp_path, monkeypatch, entry):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SEDIMENT_URL", raising=False)
    monkeypatch.delenv("SEDIMENT_INGEST_TOKEN", raising=False)
    config = tmp_path / ".sediment/config.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps(
            {
                "current": "https://sediment.example",
                "servers": {"https://sediment.example": entry},
            }
        )
    )
    return config


@pytest.mark.parametrize(
    "entry",
    [
        {"token": OPERATOR},
        {"token": OPERATOR, "capture_token": INGEST},
        {
            "capture_token": OPERATOR,
            "capture_authority": "operator",
            "capture_client_id": "operator",
        },
    ],
)
def test_install_refuses_unproven_or_operator_capture_credentials(
    tmp_path, monkeypatch, entry
):
    configured_home(tmp_path, monkeypatch, entry)
    monkeypatch.setenv("SEDIMENT_SESSION_TOKEN", OPERATOR)
    with pytest.raises(ValueError, match="login.*--capture"):
        attribution.cmd_install_env(None, None, None)
    with pytest.raises(ValueError, match="login.*--capture"):
        attribution._install_codex_profile("capture")
    assert not (tmp_path / ".sediment/env.sh").exists()


def test_install_uses_only_proven_ingest_in_shell_fish_and_codex(tmp_path, monkeypatch):
    configured_home(
        tmp_path,
        monkeypatch,
        {
            "token": OPERATOR,
            "capture_token": INGEST,
            "capture_authority": "ingest",
            "capture_client_id": "legacy",
        },
    )
    monkeypatch.setenv("SEDIMENT_SESSION_TOKEN", OPERATOR)
    attribution.cmd_install_env(None, None, None)
    codex = attribution._install_codex_profile("capture")
    for path in (
        tmp_path / ".sediment/env.sh",
        tmp_path / ".config/fish/conf.d/sediment.fish",
        codex,
    ):
        content = path.read_text()
        assert INGEST in content
        assert OPERATOR not in content
        assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("authority", ["ingest", "operator", None])
def test_explicit_capture_override_requires_live_ingest_authority(
    tmp_path, monkeypatch, authority
):
    configured_home(tmp_path, monkeypatch, {"token": OPERATOR})

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == "/v1/me"
            assert self.headers["Authorization"] == "Bearer explicit-ingest"
            self.send_response(200)
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {"authority": authority, "client_id": "developer", "org_id": "org"}
                ).encode()
            )

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("SEDIMENT_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("SEDIMENT_INGEST_TOKEN", "explicit-ingest")
    try:
        if authority == "ingest":
            attribution.cmd_install_env(None, None, None)
            assert "explicit-ingest" in (tmp_path / ".sediment/env.sh").read_text()
        else:
            with pytest.raises(ValueError, match="ingest authority"):
                attribution.cmd_install_env(None, None, None)
            assert not (tmp_path / ".sediment/env.sh").exists()
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_server_provisions_once_then_runs_only_runtime_database_authority(
    tmp_path, monkeypatch, capsys
):
    import sediment_core.postgres_roles as roles

    monkeypatch.setattr(
        os,
        "environ",
        {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("SEDIMENT_")
        },
    )
    bootstrap = (
        "postgresql+psycopg://bootstrap:bootstrap-secret@127.0.0.1:5432/evaluation"
    )
    monkeypatch.setenv("SEDIMENT_BOOTSTRAP_DATABASE_URL", bootstrap)
    monkeypatch.setenv("SEDIMENT_BOOTSTRAP_PASSWORD", "unused-local-bootstrap-secret")
    calls = []
    monkeypatch.setattr(
        roles,
        "provision_database",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    environments = []
    monkeypatch.setitem(
        sys.modules,
        "uvicorn",
        types.SimpleNamespace(
            run=lambda *a, **k: environments.append(dict(os.environ))
        ),
    )
    root = tmp_path / "server"
    assert cli.main(["server", "--root", str(root)]) == 0
    values = cli._load_server_env(root / "server.env")
    names = (
        "SEDIMENT_OPERATOR_TOKEN",
        "SEDIMENT_API_BEARER_TOKEN",
        "SEDIMENT_GITHUB_WEBHOOK_SECRET",
        "SEDIMENT_MIGRATOR_PASSWORD",
        "SEDIMENT_RUNTIME_PASSWORD",
        "SEDIMENT_OPERATOR_PASSWORD",
    )
    assert len({values[name] for name in names}) == len(names)
    assert calls == [
        (
            (bootstrap,),
            {
                "migrator_password": values[names[3]],
                "runtime_password": values[names[4]],
                "operator_password": values[names[5]],
            },
        )
    ]
    running = environments[0]
    database = make_url(running["SEDIMENT_DATABASE_URL"])
    assert database.username == "sediment_runtime"
    assert database.password == values["SEDIMENT_RUNTIME_PASSWORD"]
    for name in (
        "SEDIMENT_BOOTSTRAP_DATABASE_URL",
        "SEDIMENT_BOOTSTRAP_PASSWORD",
        "SEDIMENT_MIGRATOR_PASSWORD",
        "SEDIMENT_OPERATOR_PASSWORD",
    ):
        assert name not in running
    output = capsys.readouterr()
    assert all(values[name] not in output.out + output.err for name in names)
    assert root.stat().st_mode & 0o777 == 0o700
    assert (root / "server.env").stat().st_mode & 0o777 == 0o600


def test_doctor_requires_operator_authority_for_saved_read_credentials(
    tmp_path, monkeypatch
):
    configured_home(tmp_path, monkeypatch, {"token": INGEST})
    response = io.BytesIO(
        json.dumps(
            {"org_id": "org", "authority": "ingest", "client_id": "legacy"}
        ).encode()
    )
    monkeypatch.setattr(
        attribution.urllib.request,
        "build_opener",
        lambda *args: types.SimpleNamespace(open=lambda *a, **k: response),
    )
    findings = []
    attribution._doctor_server(findings)
    assert findings[0][0] == attribution.DOCTOR_FAIL
    assert "operator" in findings[0][2]


@pytest.mark.parametrize(
    "body", [b"invalid private-response", b"x" * 8193], ids=["malformed", "oversized"]
)
def test_doctor_rejects_unreadable_identity_without_reproducing_response(
    tmp_path, monkeypatch, body
):
    configured_home(tmp_path, monkeypatch, {"token": OPERATOR})
    response = io.BytesIO(body)
    monkeypatch.setattr(
        attribution.urllib.request,
        "build_opener",
        lambda *args: types.SimpleNamespace(open=lambda *a, **k: response),
    )
    findings = []
    attribution._doctor_server(findings)
    assert findings[0][0] == attribution.DOCTOR_FAIL
    assert "private-response" not in str(findings)
