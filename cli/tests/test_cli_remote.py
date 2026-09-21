# SPDX-License-Identifier: AGPL-3.0-or-later
"""Remote CLI verbs and their final HTTP request boundary."""

from __future__ import annotations

import io
import json
import os
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

import sediment_cli.client as api_client
from sediment_cli import cli
from sediment_core import (
    AgentHarness,
    DeveloperDecision,
    InteractionMode,
    FactStore,
    ForgeProvider,
    GatewayProvider,
    InferenceCall,
    InferenceMessage,
    Push,
    TextPart,
)

ORG = "testorg"
REPO = "testorg/test-repo"


@pytest.mark.parametrize(
    "url",
    [
        "http://sediment.example.com",
        "ftp://sediment.example.com",
        "sediment.example.com",
        "https://",
        "https://user@sediment.example.com",
        "https://sediment.example.com/api",
        "https://sediment.example.com?token=value",
        "https://sediment.example.com#fragment",
        "https://sediment.example.com#",
        "https://sediment.example.com?",
        "https://sediment.example.com/?#",
        "https://bad_host.example.com",
        "https://999.999.999.999",
        "https://sediment.example.com:99999",
        "http://127.0.0.1\t:8000",
        "http://127.0.0.1\r:8000",
        "http://127.0.0.1\n:8000",
        "\x01https://sediment.example.com",
        "http://127.1:8000",
        "http://0.0.0.0:8000",
        "http://[::ffff:127.0.0.1]:8000",
        "http://localhost.evil.com:8000",
    ],
    ids=[
        "remote-http",
        "wrong-scheme",
        "missing-scheme",
        "missing-host",
        "userinfo",
        "base-path",
        "query",
        "fragment",
        "bare-fragment-delimiter",
        "bare-query-delimiter",
        "bare-query-fragment-delimiters",
        "malformed-host",
        "malformed-ipv4",
        "invalid-port",
        "tab",
        "carriage-return",
        "newline",
        "leading-c0-control",
        "abbreviated-ipv4",
        "unspecified-ipv4",
        "ipv4-mapped-ipv6",
        "localhost-suffix",
    ],
)
def test_login_rejects_unsafe_urls_before_reading_or_sending_a_token(
    url, tmp_path, capsys, monkeypatch
) -> None:
    class _UnreadableStdin:
        def readline(self):
            pytest.fail("read a bearer token before rejecting the URL")

    monkeypatch.setattr("sys.stdin", _UnreadableStdin())
    monkeypatch.setattr(api_client, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(
        api_client,
        "_http",
        lambda: pytest.fail("opened the HTTP client for an unsafe URL"),
    )

    assert cli.main(["login", url, "--with-token"]) == 1
    error = capsys.readouterr().err
    assert "HTTPS" in error
    assert "loopback HTTP" in error


@pytest.mark.parametrize(
    "url",
    [
        "https://sediment.example.com",
        "https://localhost:8443/",
        "http://localhost:8000",
        "http://127.42.0.9:8000",
        "http://[::1]:8000",
        "HTTP://127.0.0.1:8000",
    ],
)
def test_login_accepts_https_and_literal_loopback_http(
    url, tmp_path, monkeypatch
) -> None:
    class _Accepted:
        status_code = 200

        @staticmethod
        def json():
            return {"org_id": ORG, "authority": "operator", "client_id": "operator"}

    class _Http:
        @staticmethod
        def get(*_args, **_kwargs):
            return _Accepted()

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(api_client, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(api_client, "_http", lambda: _Http())
    monkeypatch.setattr(cli, "_prompt_token", lambda: "test-operator-token-3a7e-2f6c")

    assert cli.main(["login", url]) == 0


def test_environment_url_is_validated_before_the_http_client_opens(
    monkeypatch,
) -> None:
    monkeypatch.setenv("SEDIMENT_URL", "http://sediment.example.com")
    monkeypatch.setenv("SEDIMENT_SESSION_TOKEN", "never-send-this-token")
    monkeypatch.setattr(
        api_client,
        "_http",
        lambda: pytest.fail("opened the HTTP client for an unsafe environment URL"),
    )

    with pytest.raises(api_client.ClientError, match="HTTPS"):
        api_client.get_json("/v1/me")


def test_post_url_is_validated_before_the_http_client_opens(monkeypatch) -> None:
    monkeypatch.setenv("SEDIMENT_URL", "http://sediment.example.com")
    monkeypatch.setenv("SEDIMENT_SESSION_TOKEN", "never-send-this-token")
    monkeypatch.setattr(
        api_client,
        "_http",
        lambda: pytest.fail("opened the HTTP client for an unsafe POST URL"),
    )

    with pytest.raises(api_client.ClientError, match="HTTPS"):
        api_client.post_json("/v1/logs", {})


def test_persisted_url_is_validated_before_the_http_client_opens(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("SEDIMENT_URL", raising=False)
    monkeypatch.delenv("SEDIMENT_SESSION_TOKEN", raising=False)
    monkeypatch.setattr(api_client, "CONFIG_PATH", tmp_path / "config.json")
    api_client.write_config(
        {
            "current": "http://sediment.example.com",
            "servers": {
                "http://sediment.example.com": {
                    "token": "never-send-this-token",
                    "org_id": ORG,
                }
            },
        }
    )
    monkeypatch.setattr(
        api_client,
        "_http",
        lambda: pytest.fail("opened the HTTP client for an unsafe persisted URL"),
    )

    with pytest.raises(api_client.ClientError, match="HTTPS"):
        api_client.get_json("/v1/me")


def test_explicit_request_url_is_validated_at_the_network_boundary(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        api_client,
        "_http",
        lambda: pytest.fail("opened the HTTP client for an unsafe explicit URL"),
    )

    with pytest.raises(api_client.ClientError, match="HTTPS"):
        api_client.probe_me("http://sediment.example.com", "never-send-this-token")


def test_http_client_disables_redirect_following_explicitly(monkeypatch) -> None:
    arguments = {}

    class _Http:
        def __init__(self, **kwargs):
            arguments.update(kwargs)

    monkeypatch.setattr(api_client.httpx, "Client", _Http)

    api_client._http()

    assert arguments["follow_redirects"] is False


def test_login_redirect_is_a_clean_error_without_a_second_request(
    tmp_path, capsys, monkeypatch
) -> None:
    import httpx

    requests = []

    class _Redirect(httpx.BaseTransport):
        def handle_request(self, request):
            requests.append(request)
            if len(requests) > 1:
                pytest.fail("followed a redirect with the bearer token")
            return httpx.Response(
                307,
                headers={"Location": "https://redirect.example.com/v1/me"},
                request=request,
            )

    monkeypatch.setattr(api_client, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(api_client, "_transport", _Redirect())
    monkeypatch.setattr(cli, "_prompt_token", lambda: "never-send-this-token")

    assert cli.main(["login", "https://sediment.example.com"]) == 1
    assert len(requests) == 1
    assert "server error (307)" in capsys.readouterr().err


def _seed_completion(store: FactStore, *, session_id: str = "sess-remote-1") -> None:
    store.store_inference_call(
        InferenceCall(
            org_id=ORG,
            session_id=session_id,
            user_id="agent:api",
            gateway_provider=GatewayProvider.LITELLM,
            model="claude-sonnet-5",
            input_messages=[
                InferenceMessage(role="user", parts=[TextPart(content="hi")])
            ],
            output_messages=[
                InferenceMessage(role="assistant", parts=[TextPart(content="hello")])
            ],
            input_tokens=1,
            output_tokens=1,
            duration_ms=1,
            observed_at=datetime.now(UTC),
        )
    )


def test_login_stores_validated_credentials(app_transport, capsys, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_prompt_token", lambda: "test-operator-token-3a7e-2f6c")
    assert cli.main(["login", "https://testserver/"]) == 0
    cfg = json.loads((app_transport / "config.json").read_text())
    assert cfg["current"] == "https://testserver"
    assert cfg["servers"] == {
        "https://testserver": {
            "token": "test-operator-token-3a7e-2f6c",
            "org_id": ORG,
            "authority": "operator",
            "client_id": "operator",
        }
    }
    assert "logged in to https://testserver" in capsys.readouterr().out


def test_login_config_is_0600(app_transport, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_prompt_token", lambda: "test-operator-token-3a7e-2f6c")
    assert cli.main(["login", "https://testserver"]) == 0
    st = os.stat(app_transport / "config.json")
    assert stat.S_IMODE(st.st_mode) == 0o600


def test_login_tightens_preexisting_config_mode(app_transport, monkeypatch) -> None:
    # The stamper's hand-authored auto_install_remotes file may
    # pre-exist at the default umask; once a token lands in it, login must
    # re-assert 0600 (O_CREAT's mode applies only at creation) — and keep
    # the stamper's key.
    config = app_transport / "config.json"
    config.write_text('{"auto_install_remotes": ["github.com/acme/"]}\n')
    os.chmod(config, 0o644)
    monkeypatch.setattr(cli, "_prompt_token", lambda: "test-operator-token-3a7e-2f6c")
    assert cli.main(["login", "https://testserver"]) == 0
    assert stat.S_IMODE(os.stat(config).st_mode) == 0o600
    cfg = json.loads(config.read_text())
    assert cfg["auto_install_remotes"] == ["github.com/acme/"]


def test_login_eof_prompt_is_clean_error(app_transport, capsys, monkeypatch) -> None:
    # Piped/closed stdin (e.g. a 401 re-prompt with nothing left to read)
    # must exit with the standard error line, never an EOFError traceback.
    def _eof() -> str:
        raise EOFError

    monkeypatch.setattr(cli, "_prompt_token", _eof)
    assert cli.main(["login", "https://testserver"]) == 1
    assert "no token provided" in capsys.readouterr().err


def test_login_reprompts_on_401(app_transport, capsys, monkeypatch) -> None:
    tokens = iter(["wrong-token", "test-operator-token-3a7e-2f6c"])
    monkeypatch.setattr(cli, "_prompt_token", lambda: next(tokens))
    assert cli.main(["login", "https://testserver"]) == 0
    cfg = json.loads((app_transport / "config.json").read_text())
    assert (
        cfg["servers"]["https://testserver"]["token"] == "test-operator-token-3a7e-2f6c"
    )
    assert "that's not a valid token" in capsys.readouterr().err


def _write_server_env(home: Path, token: str) -> None:
    """A ``sediment server`` data root with a generated bearer token in it."""
    root = home / ".sediment" / "server"
    root.mkdir(parents=True)
    (root / "server.env").write_text(f"SEDIMENT_OPERATOR_TOKEN={token}\n")


def test_login_reuses_the_eval_server_token_on_loopback(
    tmp_path, app_transport, capsys, monkeypatch
) -> None:
    # `sediment login http://127.0.0.1:8000` is the whole login step —
    # the token this machine's own server generated needs no paste and no
    # pipe. _prompt_token is trapped; reaching it means the step still asks.
    home = tmp_path / "home"
    _write_server_env(home, "test-operator-token-3a7e-2f6c")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(cli, "_prompt_token", lambda: pytest.fail("prompted"))
    assert cli.main(["login", "http://127.0.0.1:8000"]) == 0
    cfg = json.loads((app_transport / "config.json").read_text())
    assert (
        cfg["servers"]["http://127.0.0.1:8000"]["token"]
        == "test-operator-token-3a7e-2f6c"
    )
    out = capsys.readouterr().out
    assert "using the token from ~/.sediment/server/server.env" in out


def test_login_never_offers_the_eval_token_to_a_non_loopback_host(
    tmp_path, app_transport, monkeypatch
) -> None:
    # The generated token is a local secret. A host the operator merely typed
    # must never receive it — that host gets the prompt, like any other.
    home = tmp_path / "home"
    _write_server_env(home, "test-operator-token-3a7e-2f6c")
    monkeypatch.setenv("HOME", str(home))
    prompted = []

    def _prompt() -> str:
        prompted.append(True)
        return "test-operator-token-3a7e-2f6c"

    monkeypatch.setattr(cli, "_prompt_token", _prompt)
    assert cli.main(["login", "https://testserver"]) == 0
    assert prompted, "a remote host must be asked for its own token"


def test_login_prompts_when_the_eval_server_token_is_stale(
    tmp_path, app_transport, capsys, monkeypatch
) -> None:
    # Server restarted under a different token: nobody typed the stale one,
    # so asking is the fix — not an error the operator cannot act on.
    home = tmp_path / "home"
    _write_server_env(home, "stale-token")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(cli, "_prompt_token", lambda: "test-operator-token-3a7e-2f6c")
    assert cli.main(["login", "http://127.0.0.1:8000"]) == 0
    cfg = json.loads((app_transport / "config.json").read_text())
    assert (
        cfg["servers"]["http://127.0.0.1:8000"]["token"]
        == "test-operator-token-3a7e-2f6c"
    )


def test_login_with_token_reads_stdin(app_transport, capsys, monkeypatch) -> None:
    # The unattended path for a server login cannot resolve on its own —
    # remote, or local under a custom --root. _prompt_token is
    # trapped: reaching it would mean the step still asks.
    monkeypatch.setattr(cli, "_prompt_token", lambda: pytest.fail("prompted"))
    monkeypatch.setattr("sys.stdin", io.StringIO("test-operator-token-3a7e-2f6c\n"))
    assert cli.main(["login", "https://testserver", "--with-token"]) == 0
    cfg = json.loads((app_transport / "config.json").read_text())
    assert (
        cfg["servers"]["https://testserver"]["token"] == "test-operator-token-3a7e-2f6c"
    )


def test_login_with_token_does_not_reprompt_on_401(
    app_transport, capsys, monkeypatch
) -> None:
    # A wrong token must exit, not loop reading an exhausted stdin.
    monkeypatch.setattr("sys.stdin", io.StringIO("wrong-token\n"))
    assert cli.main(["login", "https://testserver", "--with-token"]) == 1
    assert "that's not a valid token" in capsys.readouterr().err
    assert not (app_transport / "config.json").exists()


def test_login_with_token_rejects_empty_stdin(
    app_transport, capsys, monkeypatch
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert cli.main(["login", "https://testserver", "--with-token"]) == 1
    assert "no token on stdin" in capsys.readouterr().err


def test_login_with_token_overrides_the_loopback_shortcut(
    tmp_path, app_transport, monkeypatch
) -> None:
    # An explicit --with-token means "use what I pipe", even on loopback
    # where server.env holds a working token.
    home = tmp_path / "home"
    _write_server_env(home, "test-operator-token-3a7e-2f6c")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("sys.stdin", io.StringIO("wrong-token\n"))
    assert cli.main(["login", "http://127.0.0.1:8000", "--with-token"]) == 1


def test_login_refuses_when_env_token_set(app_transport, capsys, monkeypatch) -> None:
    monkeypatch.setenv("SEDIMENT_SESSION_TOKEN", "x")
    assert cli.main(["login", "https://testserver"]) == 1
    assert not (app_transport / "config.json").exists()
    assert "SEDIMENT_SESSION_TOKEN is set" in capsys.readouterr().err


def test_login_refuses_unreachable_server(tmp_path, capsys, monkeypatch) -> None:
    import httpx

    class _Down(httpx.BaseTransport):
        def handle_request(self, request):
            raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(api_client, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(api_client, "_transport", _Down())
    monkeypatch.setattr(cli, "_prompt_token", lambda: "test-operator-token-3a7e-2f6c")
    assert cli.main(["login", "https://testserver"]) == 1
    assert "is the server running" in capsys.readouterr().err


def test_logout_removes_current(app_transport, capsys) -> None:
    api_client.write_config(
        {
            "current": "http://a",
            "servers": {"http://a": {"token": "t", "org_id": "o"}},
        }
    )
    assert cli.main(["logout"]) == 0
    saved = json.loads((app_transport / "config.json").read_text())
    assert saved["servers"] == {}
    assert saved["current"] is None
    assert "logged out of http://a" in capsys.readouterr().out


def test_logout_not_logged_in(app_transport, capsys) -> None:
    assert cli.main(["logout"]) == 1
    assert "not logged in" in capsys.readouterr().err


def test_logout_server_flag_keeps_current(app_transport, capsys) -> None:
    api_client.write_config(
        {
            "current": "http://a",
            "servers": {
                "http://a": {"token": "ta", "org_id": "oa"},
                "http://b": {"token": "tb", "org_id": "ob"},
            },
        }
    )
    assert cli.main(["logout", "--server", "http://b"]) == 0
    saved = json.loads((app_transport / "config.json").read_text())
    assert set(saved["servers"]) == {"http://a"}
    assert saved["current"] == "http://a"


def test_facts_remote(remote, client, capsys) -> None:
    from sediment_api.main import app

    _seed_completion(app.state.fact_store)
    assert cli.main(["facts"]) == 0
    out = capsys.readouterr().out
    assert "sessions" in out
    assert "inference_calls" in out
    assert "quarantine_revision: 0" in out


def test_facts_remote_empty(remote, capsys) -> None:
    assert cli.main(["facts"]) == 0
    out = capsys.readouterr().out
    assert "quarantine_revision: 0" in out
    assert "inference_calls" in out


@pytest.mark.parametrize("rename_available", [False, True])
def test_remote_facts_distinguish_omitted_table_from_measured_zero(
    monkeypatch, capsys, rename_available
) -> None:
    import httpx

    monkeypatch.delenv("SEDIMENT_DATABASE_URL", raising=False)
    monkeypatch.setenv("SEDIMENT_URL", "https://sediment.example.com")
    monkeypatch.setenv("SEDIMENT_SESSION_TOKEN", "synthetic-count-token")
    tables = {"inference_calls": {"total": 2, "visible": 1}}
    if rename_available:
        tables["repository_renames"] = {"total": 0, "visible": 0}

    def respond(request):
        assert request.headers["Authorization"] == "Bearer synthetic-count-token"
        if request.url.path == "/v1/me":
            return httpx.Response(200, json={"org_id": ORG})
        assert request.url.path == "/v1/facts"
        return httpx.Response(
            200,
            json={
                "sessions": 1,
                "tables": tables,
                "quarantine_revision": 3,
            },
        )

    monkeypatch.setattr(
        api_client,
        "_http",
        lambda: httpx.Client(
            transport=httpx.MockTransport(respond),
        ),
    )
    assert cli.main(["facts"]) == 0
    output = capsys.readouterr().out
    rows = {row[0]: row[1:] for line in output.splitlines() if (row := line.split())}
    assert rows["inference_calls"] == ["2", "1"]
    assert rows["quarantine_revision:"] == ["3"]
    expected = ["0", "0"] if rename_available else ["unavailable", "unavailable"]
    assert rows["repository_renames"] == expected
    assert rows["pushes"] == ["unavailable", "unavailable"]


def test_facts_direct_database_url(client, capsys) -> None:
    from sediment_api.config import settings
    from sediment_api.main import app

    _seed_completion(app.state.fact_store)
    assert (
        cli.main(["facts", "--database-url", settings.database_url.get_secret_value()])
        == 0
    )
    out = capsys.readouterr().out
    assert "inference_calls" in out
    assert "quarantine_revision: 0" in out


def test_facts_direct_database_failure_is_safe(monkeypatch, capsys) -> None:
    monkeypatch.setenv("SEDIMENT_ORG_ID", "testorg")
    monkeypatch.delenv("SEDIMENT_DATABASE_URL", raising=False)
    sentinel = "sentinel-facts-password"

    assert (
        cli.main(
            [
                "facts",
                "--database-url",
                f"postgresql+psycopg://user:{sentinel}@127.0.0.1:1/sediment",
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error: count facts failed for ")
    assert sentinel not in captured.err
    assert "psycopg" not in captured.err


def test_facts_not_logged_in(remote, capsys, monkeypatch) -> None:
    monkeypatch.delenv("SEDIMENT_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("SEDIMENT_URL", raising=False)
    assert cli.main(["facts"]) == 1
    assert "not logged in" in capsys.readouterr().err


def test_commit_unattributed(remote, capsys) -> None:
    sha = "a" * 40
    assert cli.main(["commit", sha]) == 0
    assert f"{sha}: no attributions" in capsys.readouterr().out


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


@pytest.fixture()
def seeded_attribution(tmp_path, client, monkeypatch) -> str:
    """A real git repo + mirror + push + completion + decision, derived into
    a attribution — the same path a deployment takes."""
    sys.path.insert(
        0, str(Path(__file__).resolve().parents[3] / "packages/derive/tests")
    )
    from gitfixtures import FIB, commit_all, make_remote, make_work_repo, run_git

    from sediment_api.config import settings
    from sediment_derive import MirrorManager, derive_attributions

    mirror_base = str(tmp_path / "mirrors")
    monkeypatch.setattr(settings, "mirror_path", mirror_base)

    work = make_work_repo(tmp_path)
    (work / "app").mkdir()
    (work / "app" / "math_utils.py").write_text("")
    base = commit_all(work, "scaffold")
    (work / "app" / "math_utils.py").write_text(FIB)
    head = commit_all(work, "add fibonacci helper")
    run_git(work, "notes", "--ref=sediment", "add", "-m", _note("sess-query"), head)

    remote_repo = make_remote(tmp_path, work)

    push = Push(
        org_id=ORG,
        provider=ForgeProvider.GITHUB,
        repo=REPO,
        clone_url=str(remote_repo),
        ref="refs/heads/main",
        before_sha=base,
        after_sha=head,
    )
    MirrorManager(mirror_base).ensure(push)

    from sediment_api.main import app

    store = app.state.fact_store
    store.store_push(push)
    store.store_inference_call(
        InferenceCall(
            org_id=ORG,
            session_id="sess-query",
            user_id="agent:api",
            gateway_provider=GatewayProvider.LITELLM,
            model="claude-sonnet-5",
            input_messages=[
                InferenceMessage(
                    role="user", parts=[TextPart(content="write a fibonacci function")]
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
    )
    store.store_decision(
        DeveloperDecision(
            org_id=ORG,
            session_id="sess-query",
            user_id="agent:api",
            agent_harness=AgentHarness.CLAUDE_CODE,
            file_path="app/math_utils.py",
            accepted=True,
            explicit=True,
            interaction_mode=InteractionMode.AGENT,
            call_id="call-query",
            occurred_at=datetime.now(UTC),
        )
    )
    attributions = derive_attributions(store, MirrorManager(mirror_base), ORG)
    assert attributions, "seed scenario must produce ≥1 attribution"
    return head


def test_commit_attributed(remote, seeded_attribution, capsys) -> None:
    assert cli.main(["commit", seeded_attribution]) == 0
    out = capsys.readouterr().out
    assert seeded_attribution in out
    assert REPO in out
    assert "claude-sonnet-5" in out
    assert "session=sess-query" in out
    assert "decisions: 1" in out


def test_version_skew_warns_once(remote, capsys, monkeypatch) -> None:
    monkeypatch.setattr(api_client, "_version_checked", False)
    monkeypatch.setattr(api_client, "__version__", "0.0.0")
    assert cli.main(["facts"]) == 0
    assert cli.main(["facts"]) == 0
    err = capsys.readouterr().err
    assert err.count("uv tool upgrade sediment-cli") == 1


@pytest.mark.parametrize(
    "body",
    [b'["not", "an", "object"]', b'"scalar"', b"null", b"{not json at all"],
    ids=["list", "string", "null", "malformed"],
)
def test_error_detail_degrades_when_the_body_is_not_an_object(body: bytes) -> None:
    """The far end can answer a non-2xx with any body; the seam promises a
    one-line message, never a traceback."""
    import httpx

    assert api_client._error_detail(httpx.Response(500, content=body)) == (
        "server error (500)"
    )


def test_error_detail_uses_the_servers_detail_string() -> None:
    import httpx

    resp = httpx.Response(400, json={"detail": "org_id is required"})
    assert api_client._error_detail(resp) == "org_id is required"


def test_commit_shows_exact_ci_without_session_observation(remote, capsys, monkeypatch):
    from sediment_api.config import settings
    from sediment_api.main import app
    from sediment_core import CIOutcome, CIProvider, CIResult

    monkeypatch.setattr(settings, "mirror_path", None)
    app.state.fact_store.store_ci_outcome(
        CIOutcome(
            org_id=ORG,
            provider=CIProvider.GITHUB_ACTIONS,
            run_id="raw-ci",
            repo=REPO,
            commit_sha="a" * 40,
            branch="main",
            result=CIResult.FAILED,
            workflow_name="tests",
        )
    )
    assert cli.main(["commit", "a" * 40]) == 0
    output = capsys.readouterr().out
    assert REPO in output
    assert "ci failed" in output
    assert "Session observations: unavailable" in output


def test_commit_selects_and_displays_repository_lifetime(remote, capsys):
    from sediment_api.main import app
    from sediment_core import CIOutcome, CIProvider, CIResult

    sha = "a" * 40
    for repository_id in ("101", "202"):
        app.state.fact_store.store_ci_outcome(
            CIOutcome(
                org_id=ORG,
                provider=CIProvider.GITHUB_ACTIONS,
                run_id=f"run-{repository_id}",
                repo=REPO,
                commit_sha=sha,
                branch="main",
                result=CIResult.PASSED,
                workflow_name=f"workflow-{repository_id}",
                captured_at=datetime(2026, 9, 5, tzinfo=UTC),
                repository_provider="github",
                repository_host="github.com",
                repository_id=repository_id,
            )
        )
    assert cli.main(["commit", sha]) == 0
    output = capsys.readouterr().out
    assert "github.com" in output and "101" in output and "202" in output
    assert (
        cli.main(
            [
                "commit",
                sha,
                "--repository-provider",
                "github",
                "--repository-host",
                "github.com",
                "--repository-id",
                "101",
                "--as-of",
                "2026-09-06T00:00:00Z",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "workflow-101" in output and "workflow-202" not in output
    assert cli.main(["commit", sha, "--repo", REPO]) == 1
    assert "repository_selector_ambiguous" in capsys.readouterr().err


def test_commit_partial_repository_selector_fails_before_request(
    remote, monkeypatch, capsys
):
    def unexpected_request(_path):
        pytest.fail("partial identity must fail before the request")

    monkeypatch.setattr(cli, "get_json", unexpected_request)
    monkeypatch.setattr(
        cli,
        "maybe_warn_version_skew",
        lambda: pytest.fail("partial identity must fail before the version probe"),
    )
    assert cli.main(["commit", "a" * 40, "--repository-id", "101"]) == 1
    assert "all three" in capsys.readouterr().err


@pytest.mark.parametrize(
    "detail", [{"reason": ["bad"]}, {"reason": "unknown"}, {"code": "unknown"}]
)
def test_unknown_structured_error_detail_remains_bounded(detail):
    import httpx

    assert (
        api_client._error_detail(httpx.Response(409, json={"detail": detail}))
        == "server error (409)"
    )
