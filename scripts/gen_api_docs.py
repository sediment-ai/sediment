# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generate `docs/reference/api.md` — every HTTP route.

Sibling of `gen_cli_docs.py`: the page is produced from the committed
`openapi.yaml`, so a new route cannot ship undocumented, and CI runs
`--check` to fail on a stale page.

Two sources, because the spec cannot see the whole contract:

- `openapi.yaml` — paths, parameters, request schemas, and the auth-derived
  401s. `scripts/dump_openapi.py` regenerates it from the live app and CI
  runs that script's own `--check`, so reading the file is reading the app.
- `CONTRACTS` here — what each route answers, and the status codes routers
  raise as plain ``HTTPException``s (400, 413, 503) that FastAPI's schema
  generator never sees. AGENTS.md §API Conventions is normative on those
  shapes; this map is that section rendered per route. Every path must
  appear in it — an unmapped route fails the run, the same forcing function
  `dump_openapi.py` applies to its auth map.

The auth statement per route comes from `dump_openapi.py`'s `BEARER`/`HMAC`/
`OPEN` sets, imported rather than restated: one door inventory, one place to
be wrong.

Determinism: the page is a pure function of the committed spec. Nothing here
reads the environment — `gen_cli_docs.py`'s `SEDIMENT_ORG_ID` trap does not
apply, because the app is never imported — and a test pins that.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = REPO_ROOT / "openapi.yaml"
OUT_PATH = REPO_ROOT / "docs" / "reference" / "api.md"

# Auth prose per scheme, keyed by the set a path belongs to in
# dump_openapi.py. The short label feeds the endpoint index.
BEARER_AUTH = (
    "ingest or operator",
    "Bearer token from the named `SEDIMENT_INGEST_TOKENS` map or the legacy "
    "`$SEDIMENT_API_BEARER_TOKEN`. The operator token also permits explicit "
    "operator ingest. Missing or invalid credentials return 401.",
)
OPERATOR_AUTH = (
    "operator",
    "Bearer token — `Authorization: Bearer $SEDIMENT_OPERATOR_TOKEN`. "
    "Missing or invalid credentials return 401; an ingest token returns 403.",
)
PROBE_AUTH = (
    "ingest or operator",
    "Either configured bearer authority. The response identifies the configured "
    "authority and client, never the token. Missing or invalid credentials return 401.",
)
HMAC_AUTH = (
    "HMAC signature",
    "GitHub webhook signature — `X-Hub-Signature-256`, HMAC-SHA256 over the "
    "raw body keyed with `SEDIMENT_GITHUB_WEBHOOK_SECRET`. The signature is "
    "verified **before** the event type is examined, so a misconfigured "
    "webhook cannot green-light itself with a setup ping.",
)
OPEN_AUTH = ("none", "None — the liveness probe is unauthenticated.")

# Per path: what it is for, what it answers, and the status codes the route
# raises directly (AGENTS.md §API Conventions). Shared codes live in
# _SHARED_STATUS; only route-specific ones belong here, and they win.
#
# "The route" includes its dependencies, not just the handler body: the
# webhook doors inherit a 400 from `deps.py::read_verified_webhook`. Grep
# `status_code=` across BOTH routers/ and deps.py when adding a route —
# the path check below cannot catch a missing code.
CONTRACTS: dict[str, tuple[str, str, dict[str, str]]] = {
    "/ingest/gateway": (
        "Inference-call facts from an LLM gateway.",
        'Stores one inference-call fact: `{"fact_id": "<uuid>", "stored": '
        "<bool>}`. `stored: false` means a redelivery collapsed on a UNIQUE "
        "index (ADR 0003) — success, not an error. A call whose session "
        'cannot be resolved is declined with 200 `{"skipped": true, "reason": '
        '"no_session"}`.',
        {
            "400": "`provider` names a gateway with no adapter.",
            "409": "Primary and natural Inference call identities conflict; `detail.code` is `inference_call_identity_conflict`. No foreign Fact ID is returned.",
        },
    ),
    "/ingest/github/push": (
        "Push facts from a GitHub `push` webhook.",
        '`{"fact_id": "<uuid>", "stored": <bool>}`. A non-`push` '
        "`X-GitHub-Event`, or a push carrying nothing storable, returns 200 "
        '`{"skipped": true, "reason": "<why>"}` so GitHub stops retrying. A '
        "stored push may schedule a background mirror refresh and "
        "re-derivation; that result is one log line, never stored (ADR 0001).",
        {
            "409": "Repository identity conflicts with a retained receipt; `detail.code` is `repository_identity_conflict`. No foreign Fact ID is returned.",
            "503": "PostgreSQL Fact store unavailable; the request does not acknowledge successful storage.",
            "400": (
                "Signature valid, but the body is not a JSON object — a "
                "malformed request from an authenticated sender, so 400, "
                "not 401 (`deps.py::read_verified_webhook`)."
            ),
        },
    ),
    "/ingest/github/repository": (
        "Immutable Repository rename receipts.",
        '`{"fact_id": "<uuid>", "stored": <bool>}` after storage, including '
        'when mirrors are disabled. A declined event returns 200 `{"skipped": true, '
        '"reason": "<why>"}`. Renames never move an identified mirror.',
        {
            "409": "Repository identity conflicts with a retained receipt; `detail.code` is `repository_identity_conflict`. No foreign Fact ID is returned.",
            "503": "PostgreSQL Fact store unavailable; the request does not acknowledge successful storage.",
            "400": (
                "Signature valid, but the body is not a JSON object — a "
                "malformed request from an authenticated sender, so 400, "
                "not 401 (`deps.py::read_verified_webhook`)."
            ),
        },
    ),
    "/ingest/github/ci": (
        "CI outcome facts from a GitHub `workflow_run` webhook.",
        '`{"fact_id": "<uuid>", "stored": <bool>}` for a completed '
        "`workflow_run`. Any other event, or a run still in flight, returns "
        '200 `{"skipped": true, "reason": "<why>"}`.',
        {
            "409": "Repository identity conflicts with a retained receipt; `detail.code` is `repository_identity_conflict`. No foreign Fact ID is returned.",
            "503": "PostgreSQL Fact store unavailable; the request does not acknowledge successful storage.",
            "400": (
                "Signature valid, but the body is not a JSON object — a "
                "malformed request from an authenticated sender, so 400, "
                "not 401 (`deps.py::read_verified_webhook`)."
            ),
        },
    ),
    "/ingest/github/pull-request": (
        "Revision and merge-boundary facts from a GitHub `pull_request` webhook.",
        '`{"fact_id": "<uuid>", "stored": <bool>}` for an opened, synchronized, '
        "or merged pull request. Another event type, an unmerged closure, or a "
        'malformed boundary returns 200 `{"skipped": true, "reason": "<why>"}`.',
        {
            "409": "Repository identity conflicts with a retained receipt; `detail.code` is `repository_identity_conflict`. No foreign Fact ID is returned.",
            "503": "PostgreSQL Fact store unavailable; the request does not acknowledge successful storage.",
            "400": (
                "Signature valid, but the body is not a JSON object — a "
                "malformed request from an authenticated sender, so 400, "
                "not 401 (`deps.py::read_verified_webhook`)."
            ),
        },
    ),
    "/ingest/ci": (
        "CI outcome facts from any non-GitHub pipeline.",
        '`{"fact_id": "<uuid>", "stored": <bool>}`. The envelope forbids '
        "unknown fields: a caller naming an org or inventing a field is "
        "rejected at the door, not ignored.",
        {
            "409": "Repository identity conflicts with a retained receipt; `detail.code` is `repository_identity_conflict`. No foreign Fact ID is returned.",
            "503": "PostgreSQL Fact store unavailable; the request does not acknowledge successful storage.",
        },
    ),
    "/v1/logs": (
        "OTLP log records: developer decisions, edit observations, rejected "
        "edits, and retry linkages.",
        "Returns `{}` — the empty OTLP/HTTP JSON `ExportLogsServiceResponse`, "
        "which means full success. Per-record dedup is invisible to the "
        "exporter: a redelivered record is counted in the server log, never "
        "reported back.",
        {
            "400": (
                "The body is not JSON, or not a JSON object — 400 so the "
                "exporter drops it instead of retrying."
            )
        },
    ),
    "/query/commit/{sha}": (
        "Observed Sessions and inferred call associations for one commit.",
        "Observed Session edges, inferred calls and decisions, and exact CI Facts, grouped by "
        "repository lifetime with identity and observed names. Unresolved CI remains separate; "
        "declined source counts stay visible. Select with a complete provider/host/ID triple "
        "or an unambiguous name. An inclusive `as_of` bounds all supporting evidence. "
        'No evidence returns `{"commit_sha": "<sha>", "attributed": false}` at '
        "200, not an error. The derivation runs per request and is never "
        "persisted (ADR 0001).",
        {
            "409": "The closed `detail.reason` is `repository_selector_ambiguous`, `repository_evidence_limit`, or `non_finite_number`. No partial result is emitted; stored Facts remain intact.",
            "422": "Invalid SHA, incomplete identity, or a supplied name that contradicts the selected identity.",
            "503": (
                "The bounded read worker is busy, exceeds its 30-second deadline or result limit, or cannot access PostgreSQL. It runs off the "
                "event loop, so the ingest doors stay responsive meanwhile."
            ),
        },
    ),
    "/query/ci/outcome": (
        "One CI outcome identified by provider run and attempt.",
        "Returns one metadata-only CI outcome and a commit-query path bound to "
        "its identity and evidence boundary. Multiple forge namespaces require a "
        "complete repository provider/host/ID selector. An "
        'unknown identity returns 200 `{"found": false}` without disclosing '
        "another organization's rows.",
        {
            "409": "The closed `detail.reason` is `repository_selector_ambiguous`, `repository_evidence_limit`, or `non_finite_number`. No partial result is emitted; stored Facts remain intact."
        },
    ),
    "/query/ci/failures": (
        "Bounded failure-first CI outcome search.",
        "Returns one keyset-paginated page for a repository and capture-time "
        "window. The normalized result defaults to `failed`; callers can "
        "request another result for comparison. Each row links to its commit "
        "query with the same identity and boundary. The default `as_of` is "
        "`captured_before`; an explicit boundary is inclusive. Cursors bind the "
        "organization, qualified repository, boundary, filters, and quarantine "
        "revision. They don't retain a transaction between requests.",
        {
            "409": "The closed `detail.reason` is `repository_selector_ambiguous`, `repository_evidence_limit`, or `non_finite_number`. No partial result is emitted; stored Facts remain intact.",
            "422": (
                "Required bounds are absent or invalid, the start doesn't "
                "precede the end, identity is incomplete, a supplied name contradicts "
                "the selected identity, or the cursor no longer matches its scope."
            ),
        },
    ),
    "/query/session/{session_id}": (
        "Metadata evidence dossier for one Session.",
        "Returns a bounded timeline, visible and quarantined counts, capture "
        "gaps, captured Session commit observations, exact-head Push receipts, and matching CI "
        "outcomes under one complete repository context. Repository identity "
        "accompanies each delivery summary; `repository_skipped` counts declined "
        "observation sources. Captured content, user identity, file paths, and the "
        "free-form CI reason remain absent. An unknown Session returns 200 "
        '`{"found": false}`.',
        {
            "409": (
                "Emitted content contains a non-finite number (`detail.reason=non_finite_number`), or "
                "the Session or its linked delivery evidence exceeds the fixed cap."
            ),
            "503": "The bounded read worker is busy, exceeds its 30-second deadline or result limit, or cannot access PostgreSQL.",
        },
    ),
    "/v1/me": (
        "Auth probe: tenant, version, authority, and configured client.",
        '`{"org_id": "<tenant>", "version": "<api version>", "authority": "<ingest or operator>", "client_id": "<configured client>"}`. These identifiers never select tenancy.',
        {},
    ),
    "/v1/facts": (
        "Fact counts per table, total, and visible.",
        '`{"sessions": <int>, "tables": {"<table>": {"total": <int>, '
        '"visible": <int>}}, "quarantine_revision": <int>}`. `quarantine_revision` '
        "is the org's provenance token: the numeric quarantine-log high-water "
        "mark, `0` when nothing is quarantined.",
        {},
    ),
    "/v1/facts/session/{session_id}": (
        "Session-scoped fact counts for write verification.",
        '`{"session_id": "<session id>", "tables": {"<session table>": '
        '{"total": <int>, "visible": <int>}}}`. The tables are inference '
        "calls, developer decisions, edit observations, rejected edits, and "
        "retry linkages. The demo "
        "command uses these counts so unrelated organization-wide facts "
        "cannot mask a dropped demo record.",
        {},
    ),
    "/v1/facts/session/{session_id}/inference-calls": (
        "Session-scoped Inference call fields for usage reconciliation.",
        '`{"session_id": "<session id>", "inference_calls": [{'
        '"inference_call_id": "<id>", "model_call_id": "<id or null>", '
        '"gateway_provider": "<provider>", "model_provider": '
        '"<provider or null>", "model": "<model>", "input_tokens": '
        '<int or null>, "output_tokens": <int or null>, "duration_ms": '
        "<int or null>}]}`. Prompts, responses, raw provider payloads, and "
        "user identity are absent.",
        {"409": "The Session exceeds the bounded reconciliation read."},
    ),
    "/v1/facts/session/{session_id}/compatibility-evidence": (
        "Session-scoped decision, Edit observation, and inference join fields.",
        '`{"session_id": "<session id>", "inference_calls": [{'
        '"tool_call_ids": ["<response tool-call id>"]}], '
        '"developer_decisions": [{"agent_harness": "<harness>", '
        '"accepted": <bool>, "explicit": <bool>, "interaction_mode": '
        '"<mode>", "call_id": "<id or null>"}], "edit_observations": [{'
        '"agent_harness": "<harness>", "call_id": "<id>"}]}`. Captured '
        "content, raw payloads, paths, user identity, and Fact identifiers are absent.",
        {"409": "The Session exceeds a bounded evidence read."},
    ),
    "/v1/reports/model-outcomes": (
        "Bounded model-outcome evidence for operational decisions.",
        "A version 1 envelope containing the explicit report scope and the "
        "canonical base model-outcome report. The route uses the deployment "
        "org and doesn't persist Derivation output.",
        {
            "409": "The cohort, supporting Fact population, or serialized response exceeds its fixed cap.",
            "503": "The bounded read worker is busy, exceeds its deadline or result limit, or cannot access PostgreSQL.",
            "422": "A bound is absent or invalid, or the half-open cohort exceeds 31 days.",
        },
    ),
    "/v1/reports/accepted-work-lifecycle": (
        "Bounded accepted-work lifecycle evidence for operational decisions.",
        "A version 1 envelope containing the explicit report scope and the "
        "canonical accepted-work lifecycle report. The route uses the deployment "
        "org and doesn't persist Derivation output.",
        {
            "409": "The cohort, supporting Fact population, or serialized response exceeds its fixed cap.",
            "503": "The bounded read worker is busy, exceeds its deadline or result limit, or cannot access PostgreSQL.",
            "422": "A bound is absent or invalid, or the half-open cohort exceeds 31 days.",
        },
    ),
    "/health": (
        "Liveness and version.",
        '`{"status": "ok", "version": "<api version>"}`.',
        {},
    ),
}

# Codes every route shares: 401 from the auth dependency, 422 from the
# envelope validator, 413 from the body-size middleware (bodies only).
_SHARED_STATUS = {
    "401": "Missing or invalid credentials.",
    "403": "The credential lacks operator authority.",
    "503": "Worker capacity, deadline, result limit, or database availability prevents this operation; retry retained request bytes after recovery.",
    "413": (
        "Body over 25 MiB. The cap is enforced pre-auth, on every door that "
        "reads a body."
    ),
    "422": (
        "A required header, path parameter, or body field failed validation. "
        "The response carries `type`/`loc`/`msg` and never echoes the input."
    ),
}
_OK = "Success — the response shape above."

_HEADER = """# HTTP API reference

Every route the Sediment server exposes, generated from the committed
`openapi.yaml` by `scripts/gen_api_docs.py`. Do not edit this file — change
the route, then run `uv run python scripts/dump_openapi.py` followed by
`uv run python scripts/gen_api_docs.py` (CI fails on a stale spec or page).

A deployment can also serve the spec itself — `/docs`, `/redoc`,
`/openapi.json` — but all three are closed unless docs are enabled.

New here? Start with the [quickstart](../quickstart.md); the client side of
these routes is the [CLI reference](cli.md).

## Conventions

Routers write facts only: no derived state is computed into storage on the
request path (ADR 0001). Tenancy binds to the deployment
(`SEDIMENT_ORG_ID`), never to the request — no route accepts an org.

- **Single-fact ingest** returns `{"fact_id": "<uuid>", "stored": <bool>}`.
  `stored: false` is a redelivery that collapsed on a UNIQUE index — success.
- **A declined payload** returns 200 `{"skipped": true, "reason": "<why>"}`,
  so a webhook sender does not retry what will never be stored.
- **Auth failures are 401**; a malformed body from an authenticated caller is
  400 or 422. A 500 is a bug, never a documented outcome.
- **Every body read is bounded** at 25 MiB, before authentication.
"""


def _auth_map() -> dict[str, tuple[str, str]]:
    """Path → (short label, prose), from `dump_openapi.py`'s own sets.

    Loaded by path because `scripts/` is not an importable package. Cheap:
    that module imports the app only inside ``build_spec``.
    """
    spec = importlib.util.spec_from_file_location(
        "dump_openapi", REPO_ROOT / "scripts" / "dump_openapi.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return (
        {path: BEARER_AUTH for path in module.INGEST}
        | {path: OPERATOR_AUTH for path in module.OPERATOR}
        | {"/v1/me": PROBE_AUTH}
        | {path: HMAC_AUTH for path in module.HMAC}
        | {path: OPEN_AUTH for path in module.OPEN}
    )


def _resolve(node: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """A schema node with its `$ref` followed, if it has one."""
    ref = node.get("$ref")
    if not ref:
        return node
    target: Any = spec
    for part in ref.lstrip("#/").split("/"):
        target = target[part]
    return target


def _type_of(prop: dict[str, Any], spec: dict[str, Any]) -> str:
    if "$ref" in prop:
        return _resolve(prop, spec).get("type") or "object"
    if "anyOf" in prop:
        return " or ".join(_type_of(option, spec) for option in prop["anyOf"])
    if prop.get("type") == "array":
        return f"array of {_type_of(prop.get('items', {}), spec)}"
    return prop.get("type", "any")


def _describe(prop: dict[str, Any], spec: dict[str, Any]) -> str:
    target = _resolve(prop, spec)
    parts = []
    description = prop.get("description") or target.get("description")
    if description:
        parts.append(_paragraphs(description)[0])
    if target.get("enum"):
        parts.append("one of " + ", ".join(f"`{v}`" for v in target["enum"]))
    if "default" in prop:
        parts.append(f"default `{json.dumps(prop['default'])}`")
    return "; ".join(parts) or "—"


def _paragraphs(text: str) -> list[str]:
    """Docstring prose as markdown paragraphs.

    FastAPI copies the handler docstring verbatim, so it arrives wrapped at
    the source's line width and in RST-ish double backticks.
    """
    return [
        " ".join(block.split()).replace("``", "`")
        for block in text.strip().split("\n\n")
        if block.strip()
    ]


def _anchor(method: str, path: str) -> str:
    """The GitHub heading anchor for ``## <METHOD> <path>``."""
    slug = f"{method} {path}".lower()
    return "#" + "".join(
        c if c.isalnum() or c in "_-" else "-" if c == " " else "" for c in slug
    )


def _table(title: str, columns: list[str], rows: list[tuple[str, ...]]) -> list[str]:
    if not rows:
        return []
    lines = [
        f"{title}:",
        "",
        "| " + " | ".join(columns) + " |",
        "|" + "---|" * len(columns),
    ]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    lines.append("")
    return lines


def _parameter_rows(op: dict[str, Any], spec: dict[str, Any]) -> list[tuple[str, ...]]:
    return [
        (
            f"`{p['name']}`",
            p["in"],
            _type_of(p["schema"], spec),
            "yes" if p.get("required") else "no",
        )
        for p in op.get("parameters", [])
    ]


def _body(
    op: dict[str, Any], spec: dict[str, Any]
) -> tuple[str, list[tuple[str, ...]]]:
    """The request schema's name and one row per field, `$ref`s inlined."""
    body = op.get("requestBody")
    if not body:
        return "", []
    node = body["content"]["application/json"]["schema"]
    schema = _resolve(node, spec)
    name = node.get("$ref", "").rsplit("/", 1)[-1] or schema.get("title", "body")
    required = set(schema.get("required", []))
    rows = [
        (
            f"`{field}`",
            _type_of(prop, spec),
            "yes" if field in required else "no",
            _describe(prop, spec),
        )
        for field, prop in schema.get("properties", {}).items()
    ]
    return name, rows


def _status_rows(path: str, method: str, op: dict[str, Any]) -> list[tuple[str, ...]]:
    """The spec's documented codes plus the ones routers raise directly.

    FastAPI stamps a 422 on every operation, including the ones that take no
    input at all — documenting a validation error on `/v1/me` would be a
    lie, so an input-free route drops it unless the contract map names one.
    """
    extra = CONTRACTS[path][2]
    codes = set(op["responses"]) | set(extra)
    if method == "POST":
        codes.add("413")
    if not op.get("parameters") and not op.get("requestBody") and "422" not in extra:
        codes.discard("422")
    meanings = {"200": _OK} | _SHARED_STATUS | extra
    return [(f"`{code}`", meanings[code]) for code in sorted(codes)]


def render() -> str:
    spec = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
    paths: dict[str, Any] = spec["paths"]
    auth = _auth_map()

    unmapped = sorted(set(paths) - set(CONTRACTS))
    if unmapped:
        raise SystemExit(
            "error: routes missing from CONTRACTS in "
            f"scripts/gen_api_docs.py: {unmapped}"
        )
    dead = sorted(set(CONTRACTS) - set(paths))
    if dead:
        raise SystemExit(
            "error: CONTRACTS entries with no live route in "
            f"scripts/gen_api_docs.py: {dead}"
        )

    endpoints = [
        (method.upper(), path, op)
        for path, operations in paths.items()
        for method, op in operations.items()
    ]

    lines = [_HEADER, "## Endpoints", ""]
    lines += _table(
        "Every route, in ingest → read order",
        ["Endpoint", "Auth", "Purpose"],
        [
            (
                f"[`{method} {path}`]({_anchor(method, path)})",
                auth[path][0],
                CONTRACTS[path][0],
            )
            for method, path, _ in endpoints
        ],
    )

    for method, path, op in endpoints:
        lines += [f"## {method} {path}", ""]
        for paragraph in _paragraphs(op.get("description", "")):
            lines += [paragraph, ""]
        lines += [f"**Auth:** {auth[path][1]}", ""]
        lines += [f"**Response:** {CONTRACTS[path][1]}", ""]
        lines += _table(
            "Parameters",
            ["Name", "In", "Type", "Required"],
            _parameter_rows(op, spec),
        )
        name, rows = _body(op, spec)
        lines += _table(
            f"Request body — `{name}` (`application/json`)",
            ["Field", "Type", "Required", "Description"],
            rows,
        )
        lines += _table(
            "Status codes", ["Status", "Meaning"], _status_rows(path, method, op)
        )
    return "\n".join(lines).rstrip() + "\n"


def _display(path: Path) -> str:
    """Repo-relative when it is inside the repo, absolute otherwise — the
    output path is patchable, so never assume it lives under REPO_ROOT."""
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if the committed page differs from the generated one",
    )
    args = parser.parse_args(argv)
    generated = render()
    if args.check:
        current = OUT_PATH.read_text(encoding="utf-8") if OUT_PATH.exists() else ""
        if current != generated:
            print(
                f"error: {_display(OUT_PATH)} is stale — run "
                "`uv run python scripts/gen_api_docs.py`",
                file=sys.stderr,
            )
            return 1
        print(f"{_display(OUT_PATH)} is current")
        return 0
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(generated, encoding="utf-8")
    print(f"wrote {_display(OUT_PATH)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
