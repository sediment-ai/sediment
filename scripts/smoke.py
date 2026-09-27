# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deployment smoke test: fire the wire-verified e2e fixtures at a RUNNING
Sediment deployment and check every ingest door end-to-end (HTTP, auth,
storage-visible responses).

Same fixtures as ``apps/api/tests/test_e2e_smoke.py`` — referenced from
``packages/capture/tests/fixtures``, not copied — so the deployed stack and
CI exercise identical bytes. Stdlib only: runs on a bare host with nothing
but Python 3 and a checkout.

    python3 scripts/smoke.py [base_url]     # default http://127.0.0.1:8000

Credentials come from SEDIMENT_API_BEARER_TOKEN or SEDIMENT_OPERATOR_TOKEN,
plus SEDIMENT_GITHUB_WEBHOOK_SECRET. Explicit environment credentials take
precedence over the .env at the repo root.

NOTE: this plants synthetic facts in ALL FOUR fact tables (session
``sess-smoke``, the fixtures' sessions, repo acme-corp/backend-service).
Run it against a fresh stack before real capture starts (reset:
``docker compose --profile gateway --profile operator down --volumes``);
on a stack holding real facts, quarantine
instead — the completion by session id, the rest per fact id from the PASS
lines below (docs/operate/deploy.md §8.3). Re-runs are expected to report
``stored: false``: the redelivery collapsed on a UNIQUE index, which is the
dedup contract working, not a failure. The OTLP door's ``{}`` success shape
hides per-record results by design — confirm decisions landed with the
fact counts in docs/operate/deploy.md §5.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("sediment.smoke")

HERE = Path(__file__).resolve().parent
FIXTURES = HERE.parent / "packages/capture/tests/fixtures"
# Checkout tools share a stdlib-only policy; no installed package is required.
sys.path.insert(0, str(HERE.parent))
from scripts.operator_http import open_request, validate_url  # noqa: E402

# Identify ourselves instead of urllib's default: Cloudflare's Browser
# Integrity Check bans the "Python-urllib" signature outright (error 1010),
# so behind a tunnel-fronted deployment every check would 403 before
# reaching the API. Any honest non-blocklisted UA passes.
USER_AGENT = "sediment-smoke/1"


def _env(name: str) -> str:
    """Environment first, then the .env at the repo root."""
    if os.environ.get(name):
        return os.environ[name]
    env_file = HERE.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip().strip("\"'")
    return ""


def _json(raw: bytes) -> dict:
    """Fail-soft body decode: a tunnel/proxy in front of the API can answer
    with an HTML error page (502 from cloudflared, say) — that must surface
    as a FAIL with the body's head, never a traceback."""
    try:
        parsed = json.loads(raw or b"{}")
        return parsed if isinstance(parsed, dict) else {"body": parsed}
    except ValueError:
        return {"body": raw[:120].decode(errors="replace")}


def _post(url: str, body: bytes, headers: dict[str, str]) -> tuple[int, dict]:
    url = validate_url(url)
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            **headers,
        },
    )
    try:
        with open_request(req, timeout=10) as resp:
            return resp.status, _json(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, _json(e.read())
    except OSError as e:
        # Connection refused / reset / timeout mid-run: a clean FAIL beats a
        # traceback when the stack dies halfway through the checks.
        return 0, {"body": str(e)}


def main() -> int:
    try:
        base = validate_url(
            sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000", base=True
        )
    except ValueError as error:
        logger.error("invalid deployment URL: %s", error)
        return 2
    token = (
        os.environ.get("SEDIMENT_API_BEARER_TOKEN")
        or os.environ.get("SEDIMENT_OPERATOR_TOKEN")
        or _env("SEDIMENT_API_BEARER_TOKEN")
        or _env("SEDIMENT_OPERATOR_TOKEN")
    )
    secret = _env("SEDIMENT_GITHUB_WEBHOOK_SECRET")
    if not token or not secret:
        logger.error(
            "SEDIMENT_OPERATOR_TOKEN (or SEDIMENT_API_BEARER_TOKEN) and "
            "SEDIMENT_GITHUB_WEBHOOK_SECRET are required "
            "in the environment or %s",
            HERE.parent / ".env",
        )
        return 2
    bearer = {"Authorization": f"Bearer {token}"}
    failures = 0

    def check(name: str, ok: bool, detail: str) -> None:
        nonlocal failures
        failures += 0 if ok else 1
        logger.log(
            logging.INFO if ok else logging.ERROR,
            "%s %s: %s",
            "PASS" if ok else "FAIL",
            name,
            detail,
        )

    try:
        health_req = urllib.request.Request(
            f"{base}/health", headers={"User-Agent": USER_AGENT}
        )
        with open_request(health_req, timeout=10) as resp:
            check("health", resp.status == 200, f"status={resp.status}")
    except OSError as e:
        logger.error("FAIL health: %s unreachable (%s)", base, e)
        return 1

    # gateway door → inference calls
    envelope = {
        "provider": "litellm",
        "session_id": "sess-smoke",
        "user_id": "smoke@example.test",
        "payload": json.loads(
            (FIXTURES / "litellm_standard_logging_object.json").read_text()
        ),
    }
    status, body = _post(
        f"{base}/ingest/gateway", json.dumps(envelope).encode(), bearer
    )
    check(
        "gateway inference call",
        status == 200 and "fact_id" in body,
        f"status={status} stored={body.get('stored')} "
        f"fact_id={body.get('fact_id')} (stored=False = dedup, fine on re-run)",
    )

    # OTLP door → developer_decisions (every wire-verified fixture)
    for path in sorted(FIXTURES.glob("otlp/*/*.json")):
        status, body = _post(f"{base}/v1/logs", path.read_bytes(), bearer)
        # Empty ExportLogsServiceResponse ({}) = full success (OTLP/HTTP JSON).
        check(
            f"otlp {path.parent.name}/{path.name}",
            status == 200 and body == {},
            f"status={status}",
        )

    # forge door → pushes / ci_outcomes (HMAC-signed, same scheme as GitHub)
    for name, route, event in (
        ("github_push.json", "/ingest/github/push", "push"),
        ("github_workflow_run.json", "/ingest/github/ci", "workflow_run"),
    ):
        raw = (FIXTURES / name).read_bytes()
        # ponytail: 2-line HMAC duplicated from sediment_capture.sign_payload
        # (the canonical scheme) to stay stdlib-only on a bare host.
        sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        status, body = _post(
            f"{base}{route}",
            raw,
            {"X-Hub-Signature-256": sig, "X-GitHub-Event": event},
        )
        check(
            f"webhook {event}",
            status == 200 and "fact_id" in body,
            f"status={status} stored={body.get('stored')} "
            f"fact_id={body.get('fact_id')} (stored=False = dedup, fine on re-run)",
        )

    status, _ = _post(f"{base}/v1/logs", b"{}", {"Authorization": "Bearer wrong"})
    check("auth rejection", status == 401, f"status={status}")

    if failures:
        logger.error("%d check(s) failed", failures)
        return 1
    logger.info(
        "all doors green — next: verify fact counts with sediment facts "
        "in docs/operate/deploy.md §5"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
