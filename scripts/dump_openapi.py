# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regenerate (or ``--check``) the committed ``openapi.yaml`` from the live app.

FastAPI generates the spec, but auth lives in plain dependencies
(``deps.verify_token``, HMAC inside ``read_verified_webhook``) where the
schema generator cannot see it — the raw dump advertises the auth headers as
optional parameters and documents no 401. This script injects the two
security schemes, stamps a 401 response on every authed route, and drops the
duplicate header parameters. Every route must appear in exactly one of the
path sets below — an unmapped route fails the run, so adding a door forces
an auth-documentation decision here. CI runs ``--check`` and fails when the
committed spec is stale.
"""

import argparse
import os
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = ROOT / "openapi.yaml"


class _NoAliasDumper(yaml.SafeDumper):
    """Never emit YAML anchors/aliases (``&id001``/``*id001``): injecting one
    shared dict (e.g. the 401 response) into several routes must serialize as
    plain repeated YAML — aliases are a known interop wart in OpenAPI tooling.
    """

    def ignore_aliases(self, data: object) -> bool:
        return True


BEARER = {
    "/ingest/gateway",
    "/ingest/ci",
    "/v1/logs",
    "/query/commit/{sha}",
    "/query/evidence",
    "/query/evidence/manifest",
    "/query/evidence/read",
    "/query/context",
    "/query/context/discover",
    "/query/context/selected",
    "/query/context/evidence",
    "/query/context/evidence/manifest",
    "/query/context/evidence/read",
    "/query/ci/outcome",
    "/query/ci/failures",
    "/query/session/{session_id}",
    "/v1/me",
    "/v1/facts",
    "/v1/facts/session/{session_id}",
    "/v1/facts/session/{session_id}/inference-calls",
    "/v1/facts/session/{session_id}/compatibility-evidence",
    "/v1/reports/model-outcomes",
    "/v1/reports/accepted-work-lifecycle",
}
INGEST = {"/ingest/gateway", "/ingest/ci", "/v1/logs"}
RETRIEVAL = {
    "/query/context",
    "/query/context/discover",
    "/query/context/selected",
    "/query/context/evidence",
    "/query/context/evidence/manifest",
    "/query/context/evidence/read",
}
OPERATOR = BEARER - INGEST - RETRIEVAL - {"/v1/me"}
HMAC = {
    "/ingest/github/push",
    "/ingest/github/repository",
    "/ingest/github/ci",
    "/ingest/github/pull-request",
}
OPEN = {"/health"}
# The dependency signatures leak the auth headers as optional parameters;
# the security scheme is the truthful representation, so drop the dups.
AUTH_HEADERS = {"authorization", "x-hub-signature-256"}

SECURITY_SCHEMES = {
    "bearerAuth": {
        "type": "http",
        "scheme": "bearer",
        "description": "Named SEDIMENT_INGEST_TOKENS credential or legacy SEDIMENT_API_BEARER_TOKEN. Capture authority only; missing/invalid returns401.",
    },
    "operatorBearerAuth": {
        "type": "http",
        "scheme": "bearer",
        "description": "SEDIMENT_OPERATOR_TOKEN. Operator access and explicit operator ingest; missing/invalid returns401, ingest authority on an operator route returns403.",
    },
    "retrievalBearerAuth": {
        "type": "http",
        "scheme": "bearer",
        "description": "SEDIMENT_RETRIEVAL_TOKEN. Read-only context access within the configured Session set through /query/context, /query/context/discover, /query/context/selected, /query/context/evidence, /query/context/evidence/manifest, /query/context/evidence/read, and /v1/me. The legacy /query/context route requires singleton configuration. Existing ingest and operator reads return403.",
    },
    "githubWebhookSignature": {
        "type": "apiKey",
        "in": "header",
        "name": "X-Hub-Signature-256",
        "description": (
            "GitHub webhook HMAC-SHA256 of the raw body, keyed with "
            "SEDIMENT_GITHUB_WEBHOOK_SECRET. Missing/invalid -> 401."
        ),
    },
}

UNAUTHORIZED = {
    "description": "Missing or invalid credentials",
    "content": {
        "application/json": {
            "schema": {"type": "object", "properties": {"detail": {"type": "string"}}}
        }
    },
}


def build_spec() -> str:
    # The app refuses to construct without config, but the spec depends on
    # neither value — same pre-import pattern as apps/api tests/conftest.py.
    os.environ.setdefault("SEDIMENT_ORG_ID", "openapi")
    os.environ.setdefault("SEDIMENT_DEV_MODE", "true")
    os.environ.setdefault(
        "SEDIMENT_DATABASE_URL", "postgresql+psycopg://openapi@localhost/openapi"
    )
    from sediment_api.main import app

    spec = app.openapi()

    unmapped = set(spec["paths"]) - BEARER - HMAC - OPEN
    if unmapped:
        sys.exit(
            "error: routes missing from the auth map in "
            f"scripts/dump_openapi.py: {sorted(unmapped)}"
        )
    dead = (BEARER | HMAC | OPEN) - set(spec["paths"])
    if dead:
        sys.exit(
            "error: auth-map entries with no live route in "
            f"scripts/dump_openapi.py: {sorted(dead)}"
        )

    spec["components"]["securitySchemes"] = SECURITY_SCHEMES
    for path, ops in spec["paths"].items():
        if path in OPERATOR:
            sec = [{"operatorBearerAuth": []}]
        elif path in RETRIEVAL:
            sec = [{"retrievalBearerAuth": []}, {"operatorBearerAuth": []}]
        elif path == "/v1/me":
            sec = [
                {"bearerAuth": []},
                {"operatorBearerAuth": []},
                {"retrievalBearerAuth": []},
            ]
        elif path in BEARER:
            sec = [{"bearerAuth": []}, {"operatorBearerAuth": []}]
        elif path in HMAC:
            sec = [{"githubWebhookSignature": []}]
        else:
            continue
        for op in ops.values():
            op["security"] = sec
            op["responses"]["401"] = UNAUTHORIZED
            if path in OPERATOR:
                op["responses"]["403"] = {"description": "Operator authority required."}
            elif path in INGEST:
                op["responses"]["403"] = {
                    "description": "Ingest or operator authority required."
                }
            params = [
                p
                for p in op.get("parameters", [])
                if p["name"].lower() not in AUTH_HEADERS
            ]
            if params:
                op["parameters"] = params
            else:
                op.pop("parameters", None)

    return yaml.dump(spec, Dumper=_NoAliasDumper, sort_keys=False, allow_unicode=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if openapi.yaml is stale instead of rewriting it",
    )
    args = parser.parse_args()

    fresh = build_spec()
    if args.check:
        on_disk = SPEC_PATH.read_text() if SPEC_PATH.exists() else ""
        if on_disk != fresh:
            print(
                "openapi.yaml is stale — regenerate with: "
                "uv run python scripts/dump_openapi.py",
                file=sys.stderr,
            )
            return 1
        print("openapi.yaml is fresh")
        return 0

    SPEC_PATH.write_text(fresh)
    print(f"wrote {SPEC_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
