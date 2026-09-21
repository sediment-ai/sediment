# SPDX-License-Identifier: AGPL-3.0-or-later
"""
HTTP client seam for the remote CLI verbs.

Client verbs speak HTTP through this module; only ``server`` and explicit
local mode open the fact store.  Credentials resolve from environment
overrides (``SEDIMENT_URL``, ``SEDIMENT_SESSION_TOKEN``) else the current
entry in ``~/.sediment/config.json``.  The two failure modes an operator
actually hits map to clean one-line errors: a rejected token (401) and an
unreachable server (connection refused) — never a traceback.
"""

from __future__ import annotations

import ipaddress
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from sediment_core import EVIDENCE_REQUEST_BYTES_LIMIT, EVIDENCE_RESPONSE_BYTES_LIMIT

from . import __version__, ui

CONFIG_PATH = Path.home() / ".sediment" / "config.json"
_TIMEOUT_SECONDS = 10.0

# Tests inject an in-process transport so login/facts/commit exercise the
# real app without opening a socket.
_transport: httpx.BaseTransport | None = None


class ClientError(Exception):
    """A remote verb failed for a reason the operator can act on."""


def _http() -> httpx.Client:
    return httpx.Client(
        transport=_transport,
        timeout=_TIMEOUT_SECONDS,
        follow_redirects=False,
    )


def norm_url(url: str) -> str:
    """One spelling of a server URL: trailing slash stripped, so ``login``
    and a later ``SEDIMENT_URL`` override resolve to the same config key."""
    return url.strip().rstrip("/")


_URL_ERROR = (
    "server URL must use HTTPS, or loopback HTTP with localhost, "
    "127.0.0.0/8, or [::1]; omit credentials, query, fragment, and base path"
)


def is_loopback_host(host: str) -> bool:
    """Whether *host* is localhost or a literal loopback IP address."""
    if host.lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback and (
        isinstance(address, ipaddress.IPv4Address)
        or address == ipaddress.IPv6Address("::1")
    )


def _valid_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        candidate = host[:-1] if host.endswith(".") else host
        try:
            candidate = candidate.encode("idna").decode("ascii")
        except UnicodeError:
            return False
        if "." in candidate and all(
            character.isdigit() or character == "." for character in candidate
        ):
            return False
        labels = candidate.split(".")
        return (
            bool(candidate)
            and len(candidate) <= 253
            and all(
                label
                and len(label) <= 63
                and label[0].isalnum()
                and label[-1].isalnum()
                and all(character.isalnum() or character == "-" for character in label)
                for label in labels
            )
        )
    return True


def validate_server_url(url: str) -> str:
    """Validate and normalize a deployment base URL before token use."""
    if any(ord(character) < 32 or ord(character) == 127 for character in url):
        raise ClientError(_URL_ERROR)
    value = url.strip()
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise ClientError(_URL_ERROR) from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or not _valid_host(host)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or "?" in value
        or "#" in value
        or parsed.netloc.endswith(":")
        or (parsed.scheme == "http" and not is_loopback_host(host))
    ):
        raise ClientError(_URL_ERROR)
    return value[:-1] if parsed.path == "/" else value


def read_config() -> dict[str, Any]:
    """The config file as a dict; ``{}`` when absent or unparseable.  Unknown
    keys are preserved by callers — the attribution stamper keeps its own
    ``auto_install_remotes`` key in the same file."""
    try:
        raw = CONFIG_PATH.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        cfg = json.loads(raw)
    except ValueError:
        return {}
    return cfg if isinstance(cfg, dict) else {}


def write_config(cfg: dict[str, Any]) -> None:
    """Write the config at 0600 — it holds a bearer token.  Created with
    ``O_CREAT`` + mode so it never exists for a moment at the default umask
    (0644)."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(cfg, indent=2) + "\n"
    fd = os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    # O_CREAT's mode applies only at creation: a pre-existing file (the
    # stamper's hand-authored auto_install_remotes config) keeps its old
    # mode, so re-assert 0600 now that a token is landing in it.
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(data)


def _resolve() -> tuple[str, str]:
    """(base_url, token) from env-else-config.

    ``SEDIMENT_URL`` overrides the current server; ``SEDIMENT_SESSION_TOKEN``
    overrides its stored token.  The config entry for the resolved URL holds
    the token when neither env var does."""
    base_url = os.environ.get("SEDIMENT_URL")
    token = os.environ.get("SEDIMENT_SESSION_TOKEN")
    if base_url is None or token is None:
        cfg = read_config()
        if base_url is None:
            current = cfg.get("current")
            base_url = current if isinstance(current, str) and current else None
        if token is None and base_url:
            base_url = validate_server_url(base_url)
            entry = (cfg.get("servers") or {}).get(base_url)
            token = entry.get("token") if isinstance(entry, dict) else None
    if not base_url or not token:
        raise ClientError("not logged in — run sediment login")
    return validate_server_url(base_url), token


def _url(base_url: str, path: str) -> str:
    return f"{base_url}/{path.lstrip('/')}"


def _get(base_url: str, token: str, path: str) -> httpx.Response:
    base_url = validate_server_url(base_url)
    try:
        return _http().get(
            _url(base_url, path),
            headers={"Authorization": f"Bearer {token}"},
        )
    except httpx.HTTPError as exc:
        raise ClientError(
            "is the server running? sediment server or docker compose up"
        ) from exc


def _post(base_url: str, token: str, path: str, body: Any) -> httpx.Response:
    base_url = validate_server_url(base_url)
    try:
        return _http().post(
            _url(base_url, path),
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )
    except httpx.HTTPError as exc:
        raise ClientError(
            "is the server running? sediment server or docker compose up"
        ) from exc


def _error_detail(resp: httpx.Response) -> str:
    # A non-2xx body is whatever the far end sent: valid JSON that is a list
    # or a scalar has no ``.get``, and the module promises never a traceback.
    try:
        body = resp.json()
    except ValueError:
        body = None
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        reason = detail.get("reason")
        messages = {
            "repository_selector_ambiguous": "repository_selector_ambiguous: provide repository provider, host, and ID",
            "repository_evidence_limit": "repository_evidence_limit: complete repository evidence exceeds the read limit",
        }
        if isinstance(reason, str) and reason in messages:
            return messages[reason]
    return detail if isinstance(detail, str) else f"server error ({resp.status_code})"


def probe_me(base_url: str, token: str) -> dict[str, Any]:
    """GET /v1/me with explicit credentials — the ``sediment login``
    validation call.  A 401 here is a wrong token ("that's not a valid
    token"), distinct from the verb path's rejected-token message."""
    resp = _get(base_url, token, "/v1/me")
    if resp.status_code == 401:
        raise ClientError("that's not a valid token")
    if resp.status_code >= 300:
        raise ClientError(_error_detail(resp))
    try:
        identity = resp.json()
        if (
            not isinstance(identity, dict)
            or identity.get("authority") not in {"operator", "ingest", "retrieval"}
            or not isinstance(identity.get("client_id"), str)
            or not identity["client_id"]
            or not isinstance(identity.get("org_id"), str)
            or not identity["org_id"]
        ):
            raise ValueError
    except (ValueError, TypeError):
        raise ClientError(
            "server did not confirm credential authority; upgrade the API before login"
        ) from None
    return identity


def get_json(path: str) -> Any:
    """GET ``path`` with resolved credentials; returns the parsed JSON body.
    Raises ``ClientError`` with the actionable message on the two operator
    failure modes (401, connection refused) and any other non-2xx."""
    base_url, token = _resolve()
    resp = _get(base_url, token, path)
    if resp.status_code == 401:
        raise ClientError("not logged in / token rejected — run sediment login")
    if resp.status_code == 403:
        raise ClientError(
            "operator authority required — run sediment login with an operator token"
        )
    if resp.status_code >= 300:
        raise ClientError(_error_detail(resp))
    return resp.json()


def post_json(path: str, body: Any) -> Any:
    """POST ``body`` to ``path`` with resolved credentials; returns the parsed
    JSON body.  Same failure mapping as :func:`get_json` — the ingest doors
    answer 200 with a skip reason rather than an error, so a non-2xx here is
    a real problem."""
    base_url, token = _resolve()
    resp = _post(base_url, token, path, body)
    if resp.status_code == 401:
        raise ClientError("not logged in / token rejected — run sediment login")
    if resp.status_code == 403:
        raise ClientError(
            "operator authority required — run sediment login with an operator token"
        )
    if resp.status_code >= 300:
        raise ClientError(_error_detail(resp))
    return resp.json()


def read_evidence(
    path: str, *, params: dict[str, str] | None = None, body: bytes | None = None
) -> bytes:
    """Read one complete bounded evidence response with operator credentials."""
    if body is not None and len(body) > EVIDENCE_REQUEST_BYTES_LIMIT:
        raise ClientError("evidence request exceeds the 64 KiB limit")
    base_url, token = _resolve()
    try:
        with (
            _http() as http,
            http.stream(
                "GET" if body is None else "POST",
                _url(base_url, path),
                params=params,
                content=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept-Encoding": "identity",
                },
                timeout=httpx.Timeout(_TIMEOUT_SECONDS, read=40.0),
            ) as response,
        ):
            if response.status_code == 401:
                raise ClientError("not logged in / token rejected — run sediment login")
            if response.status_code == 403:
                raise ClientError(
                    "operator authority required — run sediment login with an operator token"
                )
            if response.status_code != 200:
                raise ClientError(f"evidence server error ({response.status_code})")
            if (
                response.headers.get("Content-Encoding", "identity").lower()
                != "identity"
            ):
                raise ClientError("evidence response must use identity encoding")
            content = bytearray()
            for chunk in response.iter_bytes():
                if len(content) + len(chunk) > EVIDENCE_RESPONSE_BYTES_LIMIT:
                    raise ClientError("evidence response exceeds the 1 MiB limit")
                content.extend(chunk)
            return bytes(content)
    except httpx.HTTPError:
        raise ClientError(
            "evidence request failed; check the server connection"
        ) from None


def current_url() -> str:
    """The resolved server URL, for a verb that has to reason about *which*
    server it is about to write to."""
    base_url, _ = _resolve()
    return base_url


_version_checked = False


def maybe_warn_version_skew() -> None:
    """Warn once per process when the server's version differs from this
    client's, printing the exact upgrade command.  A failed probe is
    silent — the verb's own request will surface the real error."""
    global _version_checked
    if _version_checked:
        return
    _version_checked = True
    try:
        me = get_json("/v1/me")
    except ClientError:
        return
    server_version = me.get("version")
    if server_version and server_version != __version__:
        print(
            ui.warn_line(
                f"server version {server_version} differs from client "
                f"{__version__}; run `uv tool upgrade sediment-cli`"
            ),
            file=sys.stderr,
        )
