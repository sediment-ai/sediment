# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Sediment CLI: login/logout, remote facts + commit, quarantine, exports, reports, mirror GC.

Remote verbs (login/logout/facts/commit/demo) speak HTTP through
``sediment_cli.client`` and never open the fact store; only explicit
local mode (``facts --database-url`` or ``SEDIMENT_DATABASE_URL``) reads the store
directly, so ``docker compose exec api sediment facts`` is unchanged.

Store verbs are presentation-only (ADR 0001): each is a ``FactStore`` method,
an export pipeline call, or a forwarded report module — the CLI adds no
semantics of its own. Store-writing verbs read the same settings as the API
(database URL, mirror path, org id), so there is exactly one configuration
surface; ``report`` and ``mirror-gc`` never construct ``Settings`` — they
take ``--org``/``--database-url``/``--mirror-path`` with the ``SEDIMENT_*`` env
vars as defaults, so a report can cover any org a deployment holds without
API auth config.

Safety posture: the bulk ``quarantine-inference-calls`` form dry-runs by
default and writes only with ``--apply``, and refuses a filterless
invocation without ``--all`` (no filters means every inference call in the org).
Per-fact ``quarantine``/``release`` act immediately — one id, reversible,
``--reason`` always required. Output vocabulary is the CONTEXT.md glossary:
facts are quarantined and released, never deleted.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import os
import sys
from contextlib import ExitStack
from datetime import datetime
from itertools import islice
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

from pydantic import TypeAdapter

from . import __version__, ui
from .client import (
    ClientError,
    current_url,
    get_json,
    is_loopback_host,
    maybe_warn_version_skew,
    norm_url,
    post_json,
    probe_me,
    read_config,
    validate_server_url,
    write_config,
)

from sediment_core import (
    FactStore,
    FactTable,
    GatewayProvider,
    ForgeProvider,
    NonEmptyId,
    QuarantineRecord,
    normalize_org_id,
)
from sediment_core.postgres_engine import DatabaseOperationError
from sediment_derive import (
    MirrorManager,
    derive_recovery_result,
    inference_fact_id,
)
from sediment_export import (
    DPOPolicy,
    DerivationPolicy,
    DerivationScope,
    build_derived_bundle_context,
    open_derived_bundle,
    SFTPolicy,
    export_rlvr,
    export_rlvr_from_bundle,
    project_recovery,
    load_derivation_policy,
    recovery_to_export_rows,
    write_jsonl,
    write_derived_bundle,
)

# The one-line answer to "what is this?", coder-style, shown beside the
# version on the root --help title. With the title prefix it fills exactly
# the pinned 80 columns. Keep in step with the root pyproject description.
_TAGLINE = "Turn AI developer workflow traces into RL-ready training data."

_TABLES = [t.value for t in FactTable]

# Read-only reports: name -> (module, one-line help). Dispatched
# before argparse and before Settings exists — a report never needs API auth
# config (SEDIMENT_ORG_ID + secrets) to read a store, so argv is forwarded
# verbatim to the module's own parser (`--org` falls back to
# $SEDIMENT_ORG_ID, `--database-url` to $SEDIMENT_DATABASE_URL). Modules import lazily:
# `sediment --help` stays fast and settings-free.
_REPORTS = {
    "model": (
        "sediment_api.reports.model_report",
        "per-model attribution/CI/acceptance; --compare significance",
    ),
    "label-confidence-inspection": (
        "sediment_api.reports.label_confidence_inspection",
        "sample resolved confidence for human validation",
    ),
    "dataset-diagnostics": (
        "sediment_api.reports.dataset_diagnostics",
        "export-side dataset health checks",
    ),
    "recovery-yield": (
        "sediment_api.reports.recovery_yield_report",
        "recovery-pair yield by skip reason",
    ),
    "precision": (
        "sediment_api.reports.precision_report",
        "attribution precision/recall against a ground-truth manifest",
    ),
    "abandonment": (
        "sediment_api.reports.abandonment_report",
        "sessions whose accepted edits never reached a commit",
    ),
    "attribution-share": (
        "sediment_api.reports.attribution_share_report",
        "notes-attribution share and decline alert",
    ),
    "merge-retention": (
        "sediment_api.reports.merge_retention_report",
        "attributed change retention through pull request merge",
    ),
    "lifecycle": (
        "sediment_api.reports.lifecycle_report",
        "accepted-work progression, retention, attrition, and rework evidence",
    ),
}


def _fail(msg: str) -> int:
    print(ui.error_line(msg), file=sys.stderr)
    return 1


def _database_url(args: argparse.Namespace) -> str:
    database_url = args.database_url or os.environ.get("SEDIMENT_DATABASE_URL")
    if not database_url:
        raise ValueError("set SEDIMENT_DATABASE_URL or pass --database-url")
    return database_url


def cmd_db_upgrade(args: argparse.Namespace) -> int:
    """Upgrade the PostgreSQL physical schema to the supported head."""
    from sediment_core.postgres_migrations import HEAD_REVISION, upgrade_database

    upgrade_database(_database_url(args))
    print(f"database schema upgraded to {HEAD_REVISION}")
    return 0


def cmd_db_provision(args: argparse.Namespace) -> int:
    """Provision fixed deployment roles using secrets from the environment."""
    from sediment_core.postgres_roles import provision_database

    values = {}
    for name in (
        "BOOTSTRAP_DATABASE_URL",
        "MIGRATOR_PASSWORD",
        "RUNTIME_PASSWORD",
        "OPERATOR_PASSWORD",
    ):
        value = os.environ.get(f"SEDIMENT_{name}")
        if not value:
            raise ValueError(f"set SEDIMENT_{name} for database provisioning")
        values[name.lower()] = value
    provision_database(**values)
    print("database roles provisioned and schema upgraded")
    return 0


def cmd_db_status(args: argparse.Namespace) -> int:
    """Inspect the PostgreSQL physical schema without changing it."""
    from sediment_core.postgres_migrations import RevisionState, inspect_revision

    inspection = inspect_revision(_database_url(args))
    if inspection.state is RevisionState.AT_HEAD:
        print(f"database schema: at_head ({inspection.head_revision})")
    else:
        print(
            f"database schema: {inspection.state.value} "
            f"(supported head {inspection.head_revision})"
        )
    return 0


def _public_status(status: int) -> int:
    """Map a dispatched command's semantic failure onto the CLI contract."""
    return 0 if status == 0 else 1


def _run_report(argv: list[str]) -> int:
    is_help = bool(argv) and argv[0] in ("-h", "--help")
    if not argv or is_help:
        # Usage on stdout only when asked for (--help); the bare-invocation
        # error goes to stderr like argparse's own, keeping stdout pipeable.
        stream = sys.stdout if is_help else sys.stderr
        lines = [
            "USAGE:",
            "  sediment report <name> [options]",
            "",
            "  read-only reports; each takes --help, an empty result exits 0",
            "",
            "REPORTS:",
        ]
        lines += [
            f"  {name:<22} {help_text}" for name, (_, help_text) in _REPORTS.items()
        ]
        print(ui.style_help("\n".join(lines), stream=stream), file=stream)
        return 0 if is_help else 2
    name, *rest = argv
    if name not in _REPORTS:
        return _fail(f"unknown report {name!r} (choices: {', '.join(_REPORTS)})")
    module = importlib.import_module(_REPORTS[name][0])
    if rest in (["-h"], ["--help"]):
        return _print_dispatched_help(
            module.build_parser(), prog=f"sediment report {name}"
        )
    try:
        return _public_status(module.main(rest))
    except (DatabaseOperationError, OSError, ValueError) as exc:
        return _fail(str(exc))


def _styled_revision(quarantine_revision: object) -> str:
    """Render revision 0 quietly and later quarantine revisions prominently."""
    revision = str(quarantine_revision)
    if revision == "0":
        return ui.style(revision, "dim")
    return ui.style(revision, "sandstone", "bold")


def _print_facts(
    sessions: int, tables: dict[str, dict[str, int]], quarantine_revision: object
) -> None:
    """The §6.2 counts table — shared by the local and remote ``facts`` paths
    so both render identically. ``quarantine_revision`` is an int on both
    paths; the parameter stays ``object`` so either caller formats."""
    print(ui.style(f"{'table':<20} {'total':>7} {'visible':>8}", "dim"))
    print(f"{'sessions':<20} {sessions:>7} {'-':>8}")
    for table in FactTable:
        if table.value not in tables:
            print(f"{table.value:<20} {'unavailable':>7} {'unavailable':>8}")
            continue
        counts = tables[table.value]
        head = f"{table.value:<20} {counts['total']:>7}"
        visible = f"{counts['visible']:>8}"
        if counts["total"] == 0:
            print(ui.style(f"{head} {visible}", "dim"))
        elif counts["visible"] < counts["total"]:
            # Facts hidden from derivations — the number an operator scans for.
            print(f"{head} {ui.style(visible, 'sandstone')}")
        else:
            print(f"{head} {visible}")
    print(f"quarantine_revision: {_styled_revision(quarantine_revision)}")


def _facts_local(store: FactStore, org: str) -> None:
    """Read today's counts straight from the store (local mode)."""
    tables = {
        table.value: {
            "total": store.count_facts(org, table, include_quarantined=True),
            "visible": store.count_facts(org, table),
        }
        for table in store.available_fact_tables()
    }
    _print_facts(store.count_sessions(org), tables, store.quarantine_revision(org))


def cmd_facts(args: argparse.Namespace) -> int:
    """Fact counts per table, total and derivation-facing ("visible" = not
    quarantined). Remote by default (GET /v1/facts); ``--database-url`` or a
    present ``SEDIMENT_DATABASE_URL`` opens the store directly, so
    ``docker compose exec api sediment facts`` is unchanged."""
    if args.database_url or os.environ.get("SEDIMENT_DATABASE_URL"):
        from sediment_api.database import one_shot_fact_store

        database_url = args.database_url or os.environ["SEDIMENT_DATABASE_URL"]
        org = os.environ.get("SEDIMENT_ORG_ID")
        if not org:
            raise ValueError("set SEDIMENT_ORG_ID for direct fact counts")
        with one_shot_fact_store(database_url, operation="count facts") as store:
            _facts_local(store, normalize_org_id(org))
        return 0
    maybe_warn_version_skew()
    data = get_json("/v1/facts")
    _print_facts(data["sessions"], data["tables"], data["quarantine_revision"])
    return 0


# The demo session's identity. Everything the verb writes carries these, so
# one glance at a fact row says "synthetic" and one quarantine call by
# session id removes the lot.
DEMO_SESSION_ID = "sediment-demo"
DEMO_CALL_ID = "sediment-demo-call-1"
# Fixed, not "now": ``occurred_at`` is part of both decision dedup indexes,
# so a wall-clock event time would store a second decision on every run
# while the completion (keyed on call_id) collapsed — the reader who runs
# the verb twice would watch one count move and the other stay put. A
# constant makes both facts collapse, and a synthetic fact claiming a
# synthetic time is the honest version anyway. 2026-01-01T00:00:00Z.
DEMO_EVENT_NANOS = 1767225600000000000


def _demo_completion() -> dict[str, Any]:
    """The gateway envelope a real LiteLLM callback POSTs, with synthetic
    content.  ``litellm_call_id`` matches the decision's ``tool_use_id``
    below so the two facts join exactly as a real session's would."""
    return {
        "provider": "litellm",
        "session_id": DEMO_SESSION_ID,
        "user_id": DEMO_SESSION_ID,
        "payload": {
            "model": "sediment-demo-model",
            "litellm_call_id": DEMO_CALL_ID,
            "messages": [{"role": "user", "content": "Add a docstring to greet()."}],
            "response": {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": (
                                "def greet(name):\n"
                                '    """Return a greeting for name."""\n'
                                "    return f'hello {name}'\n"
                            ),
                        }
                    }
                ]
            },
            "usage": {"prompt_tokens": 12, "completion_tokens": 24},
            "response_time_ms": 350,
        },
    }


def _demo_decision() -> dict[str, Any]:
    """The OTLP/JSON logs batch a harness shim POSTs on an edit-tool call,
    shaped like packages/capture/tests/fixtures/otlp/sediment/tool_decision.json."""
    return {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": [
                        {
                            "key": "user.id",
                            "value": {"stringValue": DEMO_SESSION_ID},
                        }
                    ]
                },
                "scopeLogs": [
                    {
                        "logRecords": [
                            {
                                "body": {"stringValue": "sediment.tool_decision"},
                                "timeUnixNano": str(DEMO_EVENT_NANOS),
                                "attributes": [
                                    {
                                        "key": "agent",
                                        "value": {"stringValue": "claude-code"},
                                    },
                                    {
                                        "key": "session.id",
                                        "value": {"stringValue": DEMO_SESSION_ID},
                                    },
                                    {
                                        "key": "tool_use_id",
                                        "value": {"stringValue": DEMO_CALL_ID},
                                    },
                                    {
                                        "key": "decision",
                                        "value": {"stringValue": "accept"},
                                    },
                                    {"key": "explicit", "value": {"boolValue": True}},
                                    {
                                        "key": "tool_name",
                                        "value": {"stringValue": "Edit"},
                                    },
                                    {
                                        "key": "file_path",
                                        "value": {
                                            "stringValue": "/sediment-demo/greet.py"
                                        },
                                    },
                                ],
                            }
                        ]
                    }
                ],
            }
        ]
    }


def _is_loopback(url: str) -> bool:
    # urlsplit() strips brackets from an IPv6 authority before the helper.
    host = urlsplit(url).hostname
    return host is not None and is_loopback_host(host)


def cmd_demo(args: argparse.Namespace) -> int:
    """Plant one synthetic session through the real ingest doors, then print
    the fact counts.  This exists so the quickstart ends on a captured fact
    instead of a table of zeros: it proves the ingest path works end to end,
    and proves nothing about the reader's own agent, which is still unwired.

    Refuses a non-loopback server without ``--force``.  Seeding a shared
    deployment with synthetic facts is the failure this guard exists to
    prevent — the facts are real once stored, and removing them is a
    quarantine call, not an undo."""
    url = current_url()
    if not _is_loopback(url) and not args.force:
        print(
            ui.error_line(
                f"{url} is not a local server. `demo` writes synthetic facts, "
                "so it refuses a shared deployment. Pass --force if you meant it."
            ),
            file=sys.stderr,
        )
        return 1

    maybe_warn_version_skew()
    print(f"posting demo session (synthetic) to {url}")
    inference_call = post_json("/ingest/gateway", _demo_completion())
    if inference_call.get("skipped"):
        raise ClientError(
            "the server skipped the demo inference call: "
            f"{inference_call.get('reason')}"
        )
    # The OTLP door answers {} whether it stored the record or skipped it —
    # per-record results are invisible to an exporter by design. So this
    # says what was *posted*; the counts below are the only authority on
    # what landed, and the check after them is what makes the difference
    # actionable instead of a table the reader has to audit.
    post_json("/v1/logs", _demo_decision())
    print(f"  posted 1 inference call and 1 decision as session {DEMO_SESSION_ID}")
    print()

    data = get_json("/v1/facts")
    _print_facts(data["sessions"], data["tables"], data["quarantine_revision"])
    print()

    session_data = get_json(f"/v1/facts/session/{DEMO_SESSION_ID}")
    missing = [
        table
        for table in ("inference_calls", "developer_decisions")
        if session_data["tables"].get(table, {}).get("total", 0) == 0
    ]
    if missing:
        print(
            ui.error_line(
                f"posted, but {' and '.join(missing)} stayed empty — the server "
                "took the payload and stored nothing. Check the api logs."
            ),
            file=sys.stderr,
        )
        return 1

    print(
        ui.style(
            "These are synthetic facts, not your agent's. They prove the "
            "ingest path works end to end.",
            "dim",
        )
    )
    return 0


def _load_server_env(env_file: Path) -> dict[str, str]:
    """KEY=VALUE lines from a previous run's server.env; {} when absent."""
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    pairs = {}
    for line in lines:
        key, sep, value = line.partition("=")
        if sep and key and not key.startswith("#"):
            pairs[key] = value
    return pairs


def cmd_server(args: argparse.Namespace) -> int:
    """Run the API and its optional managed PostgreSQL until interrupted."""
    try:
        with ExitStack() as stack:
            return _run_server(args, stack)
    except KeyboardInterrupt:
        return 0


def _run_server(args: argparse.Namespace, stack: ExitStack) -> int:
    """Provision an evaluation database, then serve with runtime database authority.

    An explicit bootstrap URL selects an external database.
    Private server.env stores separate capture/operator API tokens and role
    passwords. An exported secret takes precedence over its saved value.
    """
    import secrets as secrets_module

    from sqlalchemy.engine import make_url

    from sediment_core.postgres_roles import RUNTIME_ROLE, provision_database

    from .local_postgres import managed_postgres, server_root

    bootstrap = os.environ.get("SEDIMENT_BOOTSTRAP_DATABASE_URL")
    try:
        target = make_url(bootstrap) if bootstrap else None
        if target is not None and (
            target.get_backend_name() != "postgresql"
            or not target.host
            or not target.database
        ):
            raise ValueError
    except Exception:
        raise ValueError(
            "bootstrap URL must name an explicit PostgreSQL host and database"
        ) from None

    root = Path(args.root).expanduser().absolute()
    stack.enter_context(server_root(root))
    (root / "mirror").mkdir(exist_ok=True, mode=0o700)
    env_file = root / "server.env"
    if env_file.is_symlink() or (env_file.exists() and env_file.stat().st_nlink != 1):
        raise ValueError("server.env must be a regular private file")
    stored = _load_server_env(env_file)
    additions = {}
    api_keys = ["SEDIMENT_OPERATOR_TOKEN", "SEDIMENT_GITHUB_WEBHOOK_SECRET"]
    if not (
        os.environ.get("SEDIMENT_INGEST_TOKENS") or stored.get("SEDIMENT_INGEST_TOKENS")
    ):
        api_keys.append("SEDIMENT_API_BEARER_TOKEN")
    database_keys = [
        "SEDIMENT_MIGRATOR_PASSWORD",
        "SEDIMENT_RUNTIME_PASSWORD",
        "SEDIMENT_OPERATOR_PASSWORD",
    ]
    if not bootstrap:
        database_keys.append("SEDIMENT_BOOTSTRAP_PASSWORD")
    for key in (*api_keys, *database_keys):
        if not (os.environ.get(key) or stored.get(key)):
            stored[key] = secrets_module.token_hex(32)
            additions[key] = stored[key]
    existing_content = env_file.read_bytes() if env_file.exists() else b""
    separator = (
        "\n"
        if existing_content and not existing_content.endswith(b"\n") and additions
        else ""
    )
    content = separator + "".join(
        f"{key}={value}\n" for key, value in additions.items()
    )
    fd = os.open(
        env_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        if os.fstat(stream.fileno()).st_nlink != 1:
            raise ValueError("server.env must be a regular private file")
        os.fchmod(stream.fileno(), 0o600)
        stream.write(content)

    effective = {
        key: os.environ.get(key) or stored.get(key)
        for key in (
            *api_keys,
            *database_keys,
            "SEDIMENT_API_BEARER_TOKEN",
            "SEDIMENT_INGEST_TOKENS",
        )
    }
    if not bootstrap:
        bootstrap = stack.enter_context(
            managed_postgres(root, effective["SEDIMENT_BOOTSTRAP_PASSWORD"])
        )
        target = make_url(bootstrap)
    provision_database(
        bootstrap,
        migrator_password=effective["SEDIMENT_MIGRATOR_PASSWORD"],
        runtime_password=effective["SEDIMENT_RUNTIME_PASSWORD"],
        operator_password=effective["SEDIMENT_OPERATOR_PASSWORD"],
    )
    os.environ["SEDIMENT_DATABASE_URL"] = target.set(
        drivername="postgresql+psycopg",
        username=RUNTIME_ROLE,
        password=effective["SEDIMENT_RUNTIME_PASSWORD"],
    ).render_as_string(hide_password=False)
    # Only runtime connection authority reaches the API and its worker children.
    for key in (
        *database_keys,
        "SEDIMENT_BOOTSTRAP_PASSWORD",
        "SEDIMENT_BOOTSTRAP_DATABASE_URL",
        "SEDIMENT_MIGRATOR_DATABASE_URL",
        "SEDIMENT_OPERATOR_DATABASE_URL",
    ):
        os.environ.pop(key, None)
    os.environ.setdefault("SEDIMENT_ORG_ID", stored.get("SEDIMENT_ORG_ID", "default"))
    os.environ.setdefault("SEDIMENT_MIRROR_PATH", str(root / "mirror"))
    for key in (*api_keys, "SEDIMENT_API_BEARER_TOKEN", "SEDIMENT_INGEST_TOKENS"):
        if effective.get(key):
            os.environ[key] = effective[key]

    url = f"http://{args.host}:{args.port}"
    ui.banner("sediment", f"v{__version__}")
    if additions:
        print(f"Generated server credentials in {env_file}")
    else:
        print(f"Using credentials from {env_file} and explicit environment overrides")
    print(f"Serving on {ui.style(url, 'sandstone', 'bold')}")
    print(f"Next:  {ui.style(f'sediment login {url}', 'sandstone')}")

    del bootstrap, target, effective, stored, additions
    import uvicorn

    uvicorn.run("sediment_api.main:app", host=args.host, port=args.port)
    return 0


def _prompt_token() -> str:
    import getpass

    return getpass.getpass("Bearer token: ")


def _eval_server_token(url: str, *, capture: bool = False) -> str | None:
    """The token ``sediment server`` generated for *this machine's own* eval
    server, so ``sediment login http://127.0.0.1:8000`` needs nothing pasted
    or piped — the quickstart's whole login step is that one line.

    Loopback only.  The token is a local secret; offering it to whatever
    host the operator typed would hand it to that host.  A non-default
    ``server --root`` is not searched either.
    # ponytail: default root only — `--with-token` covers every other
    # posture, and a --root flag on login would be config for a value that
    # does not vary in the quickstart.
    """
    host = urlsplit(url).hostname
    if host is None or not is_loopback_host(host):
        return None
    env = _load_server_env(Path.home() / ".sediment" / "server" / "server.env")
    return (
        env.get("SEDIMENT_API_BEARER_TOKEN" if capture else "SEDIMENT_OPERATOR_TOKEN")
        or None
    )


def _store_login(
    url: str,
    token: str,
    me: dict,
    *,
    capture: bool = False,
    capture_identity: tuple[str, dict] | None = None,
) -> int:
    """Persist validated credentials and say which org they resolved to."""
    cfg = read_config()
    expected = "ingest" if capture else "operator"
    if me.get("authority") != expected:
        return _fail(
            f"{expected} authority required for this login; use --capture for an ingest credential"
        )
    cfg["current"] = url
    servers = dict(cfg.get("servers") or {})
    existing = servers.get(url)
    entry = dict(existing) if isinstance(existing, dict) else {}
    prefix = "capture_" if capture else ""
    entry.update(
        {
            f"{prefix}token": token,
            f"{prefix}authority": expected,
            f"{prefix}client_id": me["client_id"],
            "org_id": me["org_id"],
        }
    )
    if capture_identity is not None:
        capture_token, capture_me = capture_identity
        if (
            capture_me.get("authority") != "ingest"
            or capture_me.get("org_id") != me["org_id"]
        ):
            return _fail(
                "ingest authority for the same org is required for capture enrollment"
            )
        entry.update(
            capture_token=capture_token,
            capture_authority="ingest",
            capture_client_id=capture_me["client_id"],
        )
    servers[url] = entry
    cfg["servers"] = servers
    write_config(cfg)
    action = "capture credential enrolled for" if capture else "logged in to"
    print(f"{ui.glyph('✓', 'phosphor')}{action} {url} (org {me['org_id']})")
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    """Store credentials for a server after live-validating the token via
    GET /v1/me. A 401 re-prompts; a connection failure stops. Refuses an
    environment override for the selected authority because it would silently
    win over the stored credential.

    Nothing needs answering in the quickstart: a loopback URL reuses
    the eval server's own generated token.  Every other server — remote, or
    local under a custom ``--root`` — takes ``--with-token``: one line on
    stdin, no prompt and no re-prompt.  getpass is not that path, since it
    reads /dev/tty when one exists, so piping into the prompt hangs."""
    capture = bool(getattr(args, "capture", False))
    override = "SEDIMENT_INGEST_TOKEN" if capture else "SEDIMENT_SESSION_TOKEN"
    if os.environ.get(override):
        return _fail(
            f"{override} is set; unset it first — it would "
            "silently override the stored token"
        )
    url = validate_server_url(args.url)
    generated = None if args.with_token else _eval_server_token(url, capture=capture)
    if generated:
        try:
            me = probe_me(url, generated)
        except ClientError as exc:
            # A stale server.env (the server was restarted under a different
            # token) is the one case worth falling through on: nobody typed
            # this token, so asking for one is the fix.  Anything else — an
            # unreachable server above all — is the operator's real error and
            # must not be buried under a prompt.
            if str(exc) != "that's not a valid token":
                return _fail(str(exc))
        else:
            print(ui.style("using the token from ~/.sediment/server/server.env", "dim"))
            capture_identity = None
            if not capture and (capture_token := _eval_server_token(url, capture=True)):
                try:
                    capture_identity = (capture_token, probe_me(url, capture_token))
                except ClientError as exc:
                    return _fail(f"capture enrollment failed: {exc}")
            return _store_login(
                url, generated, me, capture=capture, capture_identity=capture_identity
            )
    while True:
        if args.with_token:
            token = sys.stdin.readline().strip()
            if not token:
                return _fail("no token on stdin")
        else:
            try:
                token = _prompt_token()
            except EOFError:
                # Piped/closed stdin (a 401 re-prompt with no second line to
                # read): clean error, never a traceback — the module contract.
                return _fail("no token provided")
        try:
            me = probe_me(url, token)
        except ClientError as exc:
            msg = str(exc)
            if msg == "that's not a valid token" and not args.with_token:
                print(ui.error_line(msg), file=sys.stderr)
                continue
            return _fail(msg)
        return _store_login(url, token, me, capture=capture)


def cmd_logout(args: argparse.Namespace) -> int:
    """Remove one server's stored credentials (``--server``, default the
    current entry), then say what was removed."""
    cfg = read_config()
    servers = dict(cfg.get("servers") or {})
    if args.server:
        url = norm_url(args.server)
        if url not in servers:
            return _fail(f"no stored credentials for {url}")
    else:
        current = cfg.get("current")
        if not isinstance(current, str) or current not in servers:
            return _fail("not logged in")
        url = current
    removed = servers.pop(url)
    cfg["servers"] = servers
    if cfg.get("current") == url:
        cfg["current"] = None
    write_config(cfg)
    print(
        f"{ui.glyph('✓', 'phosphor')}logged out of {url} (org {removed.get('org_id')})"
    )
    return 0


def cmd_commit(args: argparse.Namespace) -> int:
    """GET /query/commit/{sha} and pretty-print the per-repo attributions,
    decisions, and CI outcomes."""
    selectors = {
        name: getattr(args, name, None)
        for name in (
            "repo",
            "repository_provider",
            "repository_host",
            "repository_id",
            "as_of",
        )
    }
    identity = [
        selectors[name]
        for name in ("repository_provider", "repository_host", "repository_id")
    ]
    if any(value is not None for value in identity) and not all(
        value is not None for value in identity
    ):
        raise ClientError("repository identity requires all three components")
    if selectors["as_of"] is not None:
        selectors["as_of"] = _parse_scope_time(selectors["as_of"], "as_of").isoformat()
    query = urlencode(
        {name: value for name, value in selectors.items() if value is not None}
    )
    path = f"/query/commit/{args.sha}"
    maybe_warn_version_skew()
    data = get_json(f"{path}?{query}" if query else path)
    sha = data.get("commit_sha", args.sha)
    for reason, count in sorted(data.get("repository_skipped", {}).items()):
        print(f"{reason}: {count}")
    for outcome in data.get("unresolved_ci_outcomes", []):
        print(
            f"unresolved repository: ci {outcome['result']} {outcome['workflow_name']}"
        )
    if not data.get("repos"):
        print(f"{sha}: no attributions")
        return 0
    print(ui.style(sha, "bleached", "bold"))
    for repo in data.get("repos", []):
        identity = repo.get("repository_identity")
        label = repo["repo"]
        if identity is not None:
            label += f" ({identity['provider']} {identity['host']} repository {identity['repository_id']})"
        print(f"  {ui.style(label, 'sandstone')}")
        observations = repo.get("observed_sessions", [])
        for session in observations:
            print(f"    observed Session: {session['session_id']}")
        if not observations:
            print("    Session observations: unavailable")
        if repo.get("session_commit_unobserved"):
            print(f"    session_commit_unobserved: {repo['session_commit_unobserved']}")
        for inference_call in repo.get("inference_calls", []):
            print(
                f"    {inference_call['inference_call_id']}  "
                + ui.style(
                    f"{inference_call['gateway_provider']}/"
                    f"{inference_call['model_provider'] or '-'}/"
                    f"{inference_call['model']}",
                    "dim",
                )
                + f"  session={inference_call['session_id']}  "
                f"attribution={inference_call['attribution_source']} (inferred call/file)"
            )
        print(f"    decisions: {repo['decisions']}")
        for outcome in repo.get("ci_outcomes", []):
            color = {"passed": "phosphor", "failed": "iron-oxide"}.get(
                outcome["result"], "dim"
            )
            print(
                f"    ci {ui.style(outcome['result'], color)}  {outcome['workflow_name']}"
            )
    return 0


def cmd_quarantine_or_release(
    store: FactStore, org: str, args: argparse.Namespace
) -> int:
    if args.command == "quarantine":
        store.quarantine_fact(org, args.table, args.fact_id, reason=args.reason)
        print(
            f"{ui.glyph('✓', 'phosphor')}quarantined {args.table}/{args.fact_id} "
            "(reversible: sediment release)"
        )
    else:
        store.release_fact(org, args.table, args.fact_id, reason=args.reason)
        print(f"{ui.glyph('✓', 'phosphor')}released {args.table}/{args.fact_id}")
    return 0


def cmd_quarantine_log(store: FactStore, org: str, args: argparse.Namespace) -> int:
    # ponytail: full-history read, tail applied in the CLI — log rows are
    # small; a store-side LIMIT variant if an org's log ever gets huge.
    log = store.read_quarantine_log(org)
    shown = log if args.all else log[-args.tail :]
    for rec in shown:
        color = "sandstone" if rec.action == "quarantine" else "phosphor"
        print(
            ui.style(rec.recorded_at.isoformat(), "dim")
            + f"  {ui.style(f'{rec.action:<10}', color)} "
            f"{rec.fact_table}/{rec.fact_id}  {rec.reason}"
        )
    if len(shown) < len(log):
        print(
            ui.style(
                f"... showing last {len(shown)} of {len(log)} rows (--all for all)",
                "dim",
            )
        )
    revision = _styled_revision(store.quarantine_revision(org))
    print(f"{len(log)} rows; quarantine_revision: {revision}")
    return 0


def _resolve_quarantine_inference_filters(
    args: argparse.Namespace,
) -> tuple[str | None, tuple[datetime, datetime] | None]:
    provider = None
    if args.provider is not None:
        try:
            provider = GatewayProvider(args.provider).value
        except ValueError:
            choices = ", ".join(item.value for item in GatewayProvider)
            raise ValueError(f"--provider must be one of: {choices}") from None
    between = None
    if args.between:
        try:
            lo, hi = (datetime.fromisoformat(t) for t in args.between)
        except ValueError as exc:
            raise ValueError(f"--between wants two ISO-8601 datetimes: {exc}") from None
        if lo.tzinfo is None or hi.tzinfo is None:
            raise ValueError("--between bounds must be timezone-aware")
        if lo > hi:
            raise ValueError(f"--between bounds are reversed ({lo} > {hi})")
        between = (lo, hi)
    return provider, between


def cmd_quarantine_inference_calls(
    store: FactStore, org: str, args: argparse.Namespace
) -> int:
    provider, between = args._quarantine_inference_filters
    n = store.quarantine_inference_calls_where(
        org,
        captured_between=between,
        session_id=args.session_id,
        provider=provider,
        reason=args.reason,
        dry_run=not args.apply,
    )
    if args.apply:
        print(f"{ui.glyph('✓', 'phosphor')}quarantined {n} model-call facts")
    else:
        print(
            f"{n} model-call facts would be quarantined "
            + ui.style("(dry run; add --apply)", "sandstone")
        )
    return 0


def _mirrors(mirror_path: str | None = None) -> MirrorManager | None:
    """The mirror store every export reads, or None once a missing
    SEDIMENT_MIRROR_PATH has been reported."""
    if not mirror_path:
        _fail("SEDIMENT_MIRROR_PATH is not set; the export needs mirrors")
        return None
    return MirrorManager(mirror_path)


def _print_written(written: dict) -> None:
    """The export payoff lines, shared by every export verb so ✓/dim reads
    the same everywhere."""
    if written:
        for path, count in written.items():
            print(f"{ui.glyph('✓', 'phosphor')}wrote {path} ({count} rows)")
    else:
        print(
            ui.style(
                "nothing written (empty projections leave existing files untouched)",
                "dim",
            )
        )


def _default_derivation_policy() -> DerivationPolicy:
    return DerivationPolicy()


def _parse_scope_time(value: str | None, name: str) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return parsed


def _prepare_store_command(args: argparse.Namespace) -> None:
    """Validate store-command semantics before settings or connectivity."""
    if args.command in {"quarantine", "release", "quarantine-inference-calls"}:
        # Use the fact model's canonical audit-reason validator at the CLI
        # trust boundary without constructing or persisting a placeholder fact.
        QuarantineRecord._reason_required(args.reason)
    if args.command in {"quarantine", "release"}:
        TypeAdapter(NonEmptyId).validate_python(args.fact_id)
    if args.command == "quarantine-log" and not args.all and args.tail < 1:
        raise ValueError("--tail must be >= 1")
    if args.command == "quarantine-inference-calls":
        if args.session_id is not None:
            TypeAdapter(NonEmptyId).validate_python(args.session_id)
        filters = _resolve_quarantine_inference_filters(args)
        args._quarantine_inference_filters = filters
        provider, between = filters
        if not (between or args.session_id or provider) and not args.all:
            raise ValueError(
                "no filters given — this would quarantine the org's every "
                "model-call fact; pass --all if that is what you mean"
            )
    if args.command == "derive":
        if args.sample < 0:
            raise ValueError("--sample must be zero or greater")
        policy = (
            load_derivation_policy(Path(args.policy))
            if args.policy
            else _default_derivation_policy()
        )
        scope = DerivationScope(
            since=_parse_scope_time(args.since, "--since"),
            until=_parse_scope_time(args.until, "--until"),
            users=tuple(args.users) if args.users is not None else None,
        )
        args._derivation = (policy, scope)


def cmd_derive(store: FactStore, org: str, args: argparse.Namespace) -> int:
    mirrors = _mirrors(getattr(args, "_mirror_path", None))
    if mirrors is None:
        return 1
    policy, scope = args._derivation
    destination = Path(args.out)
    with build_derived_bundle_context(
        store, mirrors, org, policy=policy, scope=scope
    ) as bundle:
        try:
            write_derived_bundle(bundle, destination)
        except OSError as exc:
            return _fail(str(exc))
        print(f"{ui.glyph('✓', 'phosphor')}wrote {destination}")
        print(f"attributed completions: {len(bundle.attributed_completions)}")
        print(f"rollouts: {len(bundle.rollouts)}")
        print(f"referenced inference calls: {len(bundle.inference_calls)}")
        print(f"skipped: {dict(bundle.skipped)}")
        print(f"excluded: {dict(bundle.excluded)}")
        print(f"fragmented: {dict(bundle.fragmented)}")
        print(f"policy digest: {bundle.policy.digest}")
        print(f"as of: {bundle.as_of.isoformat() if bundle.as_of else 'none'}")
    for name in (
        "manifest.json",
        "attributed_completions.jsonl",
        "rollouts.jsonl",
        "inference_calls.jsonl",
        "inference_call_identities.jsonl",
        "repository_identities.jsonl",
        "repository_renames.jsonl",
    ):
        with (destination / name).open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        print(f"sha256: {name} {digest}")
    if args.sample:
        for name in ("attributed_completions.jsonl", "rollouts.jsonl"):
            with (destination / name).open(encoding="utf-8") as handle:
                for line in islice(handle, args.sample):
                    print(f"sample {name}: {line}", end="")
    return 0


def _attributed_completion_pipeline(
    store: FactStore | None,
    mirrors: MirrorManager | None,
    org: str | None,
    out_dir: str,
    base_name: str,
    *,
    label: str,
    extra: dict | None = None,
    from_bundle: str | None = None,
    profile: str | None = None,
) -> int:
    """Project complete evidence groups from a validated, live bundle context."""
    from sediment_export.bounded_training import project_training_bundle

    with ExitStack() as contexts:
        if from_bundle is not None:
            bundle = contexts.enter_context(open_derived_bundle(Path(from_bundle)))
        else:
            if store is None or mirrors is None or org is None:
                raise ValueError("direct export requires the fact store and mirrors")
            bundle = contexts.enter_context(
                build_derived_bundle_context(
                    store, mirrors, org, policy=_default_derivation_policy()
                )
            )
        projection = contexts.enter_context(
            project_training_bundle(
                bundle, objective=base_name.replace("_", "-"), **(extra or {})
            )
        )
        split_enabled = bundle.policy.eval_fraction > 0
        if profile is not None:
            from sediment_export.compatibility import write_compatible_export

            # The consumer adapter reads the staged rows within the open context;
            # its own published view is the profile's declared envelope.
            summary = write_compatible_export(
                projection.rows,
                out_dir,
                profile,
                split_enabled=split_enabled,
                canonical_skipped=projection.skipped,
            )
            written = summary["written"]
            print(f"compatibility profile: {profile}")
            print(f"dataset diagnostics: {summary['diagnostics']}")
        else:
            result = write_jsonl(
                projection.rows,
                Path(out_dir) / f"{base_name}.jsonl",
                split_enabled=split_enabled,
                max_bytes=projection.remaining_bytes,
            )
            written = result.written
        count_str = ui.style(str(len(projection.rows)), "bleached", "bold")
        print(f"{label} projected: {count_str}  skipped: {dict(projection.skipped)}")
        _print_written(written)
    return 0


def cmd_export_rlvr(
    store: FactStore | None, org: str | None, args: argparse.Namespace
) -> int:
    profile_name = getattr(args, "profile", None)
    if profile_name is not None:
        from sediment_export.compatibility import get_profile, require_dependencies

        profile = get_profile(profile_name, objective="rlvr")
        if profile.consumer != args.target:
            raise ValueError("profile and --target must name the same consumer")
        require_dependencies(profile)
    elif getattr(args, "consumer_config", None):
        raise ValueError("--consumer-config requires --profile")
    with ExitStack() as contexts:
        bundle = (
            contexts.enter_context(open_derived_bundle(Path(args.from_bundle)))
            if args.from_bundle
            else None
        )
        needs_mirror = bundle is None or args.target != "nemo-gym"
        mirrors = (
            _mirrors(getattr(args, "_mirror_path", None)) if needs_mirror else None
        )
        if needs_mirror and mirrors is None:
            return 1
        if profile_name is not None:
            from sediment_export.consumer_rlvr import (
                export_rlvr_profile,
                load_consumer_settings,
            )

            if bundle is None:
                bundle = contexts.enter_context(
                    build_derived_bundle_context(
                        store, mirrors, org, policy=_default_derivation_policy()
                    )
                )
            # The consumer adapter reads the validated file-backed bundle within
            # this context; its own hydrated view is the profile's envelope.
            summary = export_rlvr_profile(
                bundle,
                mirrors,
                args.out,
                profile_name,
                load_consumer_settings(getattr(args, "consumer_config", None)),
            )
            print(f"compatibility profile: {profile_name}")
            print(f"rows: {summary['rows']}  skipped: {summary['skipped']}")
            print(f"canonical skipped: {summary['canonical_skipped']}")
            print(f"fragmented: {summary['fragmented']}")
            print(f"dataset diagnostics: {summary['diagnostics']}")
            _print_written(summary["written"])
            return 0
        if bundle is not None:
            summary = export_rlvr_from_bundle(
                bundle,
                mirrors,
                args.out,
                target=args.target,
            )
        else:
            summary = export_rlvr(store, mirrors, org, args.out, target=args.target)
    print(f"rollouts derived: {ui.style(str(summary['rollouts']), 'bleached', 'bold')}")
    print(f"task rows: {summary['task_rows']}  skipped: {summary['task_skipped']}")
    print(
        f"rollout rows: {summary['rollout_rows']}  "
        f"skipped: {summary['rollout_skipped']}"
    )
    print(f"fragmented: {summary['fragmented']}")
    _print_written(summary["written"])
    if summary["environment_manifest"]:
        print(
            f"{ui.glyph('✓', 'phosphor')}wrote {summary['environment_manifest']} "
            "(experimental taskset manifest)"
        )
    return 0


def cmd_export_dpo(
    store: FactStore | None, org: str | None, args: argparse.Namespace
) -> int:
    profile_name = getattr(args, "profile", None)
    if profile_name is not None:
        from sediment_export.compatibility import get_profile, require_dependencies

        require_dependencies(get_profile(profile_name, objective="dpo"))
    mirrors = (
        None if args.from_bundle else _mirrors(getattr(args, "_mirror_path", None))
    )
    if not args.from_bundle and mirrors is None:
        return 1
    return _attributed_completion_pipeline(
        store,
        mirrors,
        org,
        args.out,
        "dpo",
        label="pairs",
        extra={"policy": DPOPolicy(recipe_id=args.recipe)},
        from_bundle=args.from_bundle,
        profile=profile_name,
    )


def cmd_export_sft(
    store: FactStore | None, org: str | None, args: argparse.Namespace
) -> int:
    profile_name = getattr(args, "profile", None)
    if profile_name is not None:
        from sediment_export.compatibility import get_profile, require_dependencies

        require_dependencies(get_profile(profile_name, objective="sft"))
    mirrors = (
        None if args.from_bundle else _mirrors(getattr(args, "_mirror_path", None))
    )
    if not args.from_bundle and mirrors is None:
        return 1
    return _attributed_completion_pipeline(
        store,
        mirrors,
        org,
        args.out,
        "sft",
        label="samples",
        extra={"policy": SFTPolicy(recipe_id=args.recipe)},
        from_bundle=args.from_bundle,
        profile=profile_name,
    )


def cmd_export_diff_sft(
    store: FactStore | None, org: str | None, args: argparse.Namespace
) -> int:
    mirrors = _mirrors(getattr(args, "_mirror_path", None))
    if mirrors is None:
        return 1
    return _attributed_completion_pipeline(
        store,
        mirrors,
        org,
        args.out,
        "diff_sft",
        label="samples",
        extra={"mirrors": mirrors, "policy": SFTPolicy(recipe_id=args.recipe)},
        from_bundle=args.from_bundle,
    )


def cmd_export_recovery(store: FactStore, org: str, args: argparse.Namespace) -> int:
    mirrors = _mirrors(getattr(args, "_mirror_path", None))
    if mirrors is None:
        return 1
    fraction = DerivationPolicy().eval_fraction
    split_enabled = fraction > 0
    with store.read_snapshot() as snapshot:
        derivation = derive_recovery_result(snapshot, mirrors, org)
        inference_calls = {
            inference_fact_id(call): call for call in snapshot.read_inference_calls(org)
        }
    projection = project_recovery(derivation.pairs, inference_calls, fraction)
    rows = recovery_to_export_rows(projection.rows)
    result = write_jsonl(
        rows, Path(args.out) / "recovery.jsonl", split_enabled=split_enabled
    )
    pairs = ui.style(str(len(derivation.pairs)), "bleached", "bold")
    print(f"pairs derived: {pairs}  skipped: {dict(derivation.skipped)}")
    print(f"recovery rows: {len(projection.rows)}  skipped: {dict(projection.skipped)}")
    _print_written(result.written)
    return 0


class _CoderFormatter(argparse.HelpFormatter):
    """Coder-style help, pinned to an 80-column wrap so golden --help files
    stay deterministic across terminals and CI: a ``USAGE:``
    block, UPPERCASE section headings, subcommand rows without the
    ``{a,b,c}`` metavar header, descriptions indented under usage. The
    layout is identical piped or interactive — color is a separate
    TTY-gated pass (``ui.style_help``) in ``_ArgumentParser.format_help``."""

    _SECTIONS = {"positional arguments": "arguments"}

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("width", 80)
        super().__init__(*args, **kwargs)

    def _format_usage(self, usage, actions, groups, prefix):
        bare = super()._format_usage(usage, actions, groups, "").strip("\n")
        indented = "\n".join(f"  {line}" for line in bare.splitlines())
        return f"USAGE:\n{indented}\n\n"

    def start_section(self, heading):
        if heading:
            heading = self._SECTIONS.get(heading, heading).upper()
        super().start_section(heading)

    def _format_text(self, text):
        # Descriptions sit indented under the USAGE block, coder-style.
        if "%(prog)" in text:
            text = text % {"prog": self._prog}
        return self._fill_text(text, self._width - 2, "  ") + "\n\n"

    def _format_action(self, action):
        if isinstance(action, argparse._SubParsersAction):
            # Subcommand rows render directly at section indent — no
            # "{report,mirror-gc,...}" header row above them.
            return "".join(self._format_action(sub) for sub in action._get_subactions())
        return super()._format_action(action)


def _propagate_descriptions(parser: argparse.ArgumentParser) -> None:
    """Coder-style per-command help: a subcommand without an explicit
    description reuses its one-line listing help under its USAGE block."""
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        help_by_name = {p.dest: p.help for p in action._choices_actions}
        for name, child in action.choices.items():
            if child.description is None:
                child.description = help_by_name.get(name)
            _propagate_descriptions(child)


def _subparsers_action(
    parser: argparse.ArgumentParser,
) -> argparse._SubParsersAction | None:
    """The subparsers action on *parser*, or None for a leaf command."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _subcommand_display_name(action: argparse._SubParsersAction) -> str:
    """The name argparse prints for a missing/invalid subcommand — the
    subparsers ``metavar`` (``<command>``/``<format>``), falling back to
    ``dest`` the way ``argparse._get_action_name`` does."""
    if action.metavar not in (None, argparse.SUPPRESS):
        return action.metavar
    return action.dest


class _ArgumentParser(argparse.ArgumentParser):
    """ArgumentParser whose subparsers inherit the coder-style formatter
    (``add_subparsers`` defaults ``parser_class`` to ``type(self)``) and
    whose help gets the TTY-gated color pass — plain text everywhere else,
    so the goldens capture exactly what a pipe sees."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("formatter_class", _CoderFormatter)
        super().__init__(*args, **kwargs)

    def error(self, message):
        # A missing or unknown subcommand is the one case where the full
        # listing IS the answer to "what do I type next?" — print it to
        # stderr before the one-line diagnostic (stdout stays empty, exit 2).
        # A bad flag on a leaf command keeps argparse's terse usage line.
        # This distinguishes the two by matching argparse's own message
        # text, which is not a stable contract; test_cli_help.py pins the
        # split on both sides.
        if (
            self.prog == "sediment export rlvr"
            and message.startswith("the following arguments are required:")
            and "--target" in message
        ):
            self.print_usage(sys.stderr)
            self.exit(
                2,
                f"{self.prog}: error: {message}; --target choices: sediment, "
                "swe-bench, or nemo-gym; see docs/exports/rlvr-export.md\n",
            )

        sub = _subparsers_action(self)
        if sub is not None:
            name = _subcommand_display_name(sub)
            if message == f"the following arguments are required: {name}":
                self._print_message(self.format_help(sys.stderr), sys.stderr)
                self.exit(2, f"{self.prog}: error: {message}\n")
            elif message.startswith(f"argument {name}: invalid choice: "):
                # Drop the ``(choose from 'a', 'b', …)`` tail — the listing
                # now sits directly above — and name the thing by what this
                # level calls it: a command up top, a format under `export`.
                value = message.removeprefix(
                    f"argument {name}: invalid choice: "
                ).rsplit(" (choose from ", 1)[0]
                self._print_message(self.format_help(sys.stderr), sys.stderr)
                noun = name.strip("<>")
                self.exit(2, f"{self.prog}: error: invalid {noun} {value}\n")
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {message}\n")

    def format_help(self, stream=None):
        # Coder's section order: COMMANDS/FORMATS listings above OPTIONS.
        # Stable sort, idempotent — only the default options group moves.
        # *stream* is where the text is headed: the color pass is TTY-gated
        # on it, so help written to a redirected stderr stays plain. argparse
        # calls this with no argument, which gates on stdout as before.
        self._action_groups.sort(key=lambda g: g.title == "options")
        text = super().format_help()
        if self.prog == "sediment":
            title = ui.style(
                f"{self.prog} v{__version__}", "bleached", "bold", stream=stream
            )
            tagline = ui.style(_TAGLINE, "dim", stream=stream)
            text = f"{title} — {tagline}\n\n{text}"
        return ui.style_help(text, stream=stream)


def _print_dispatched_help(parser: argparse.ArgumentParser, *, prog: str) -> int:
    """Render a forwarded command through the public CLI help contract.

    Report, mirror-GC, and attribution modules keep standalone parsers because
    their execution seams have different dependency constraints. The installed
    command still owns their public path and presentation.
    """
    parser.prog = prog
    parser.formatter_class = _CoderFormatter
    parser._action_groups.sort(key=lambda group: group.title == "options")
    print(ui.style_help(parser.format_help(), stream=sys.stdout), end="")
    return 0


def _dpo_profile_name(name: str) -> str:
    """Keep retired profile migration guidance at the argument boundary."""
    from sediment_export.compatibility import CompatibilityError, get_profile

    try:
        return get_profile(name, objective="dpo").id
    except CompatibilityError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


class _VersionAction(argparse._VersionAction):
    def __call__(self, parser, namespace, values, option_string=None):
        banner = ui.version_banner(self.version % {"prog": parser.prog})
        if banner is None:
            return super().__call__(parser, namespace, values, option_string)
        # argparse's help formatter folds whitespace, which distorts the mark.
        parser._print_message(banner, sys.stdout)
        parser.exit()


def build_parser() -> argparse.ArgumentParser:
    """The argparse tree, extracted so the golden --help test can walk it."""
    parser = _ArgumentParser(
        prog="sediment",
        usage="sediment <command> [options]",
        description=__doc__.splitlines()[1],
    )
    parser.add_argument(
        "--version", action=_VersionAction, version=f"%(prog)s {__version__}"
    )
    # prog passed explicitly: argparse otherwise derives it by formatting the
    # parent usage through the formatter, which would drag the USAGE: heading
    # into every subcommand's prog.
    sub = parser.add_subparsers(
        dest="command",
        required=True,
        title="commands",
        metavar="<command>",
        prog=parser.prog,
    )

    # Stubs for --help only — the dispatch in main() runs first.
    sub.add_parser("report", help="read-only reports (sediment report --help)")
    sub.add_parser(
        "delivery", help="prepared capture delivery (sediment delivery --help)"
    )
    sub.add_parser(
        "mirror-gc",
        help="remove mirrors past push retention (dry-run; --apply deletes)",
    )

    p_facts = sub.add_parser(
        "facts", help="fact counts per table (remote; --database-url for direct)"
    )
    p_facts.add_argument(
        "--database-url", help="read PostgreSQL directly instead of the API"
    )
    p_facts.set_defaults(func=cmd_facts)

    from .evidence import command as evidence_command

    p_evidence = sub.add_parser(
        "evidence", help="read selected Session evidence with operator authority"
    )
    evidence_sub = p_evidence.add_subparsers(
        dest="evidence_operation",
        required=True,
        title="operations",
        metavar="<operation>",
        prog=p_evidence.prog,
    )
    for operation, help_text in (
        ("inventory", "print a complete bounded Session inventory as JSON"),
        ("inspect", "print one Inference call's part manifest as JSON"),
        ("fetch", "write exact selected parts to a private local packet"),
    ):
        command = evidence_sub.add_parser(operation, help=help_text)
        command.add_argument("session_id", metavar="SESSION", help="source Session ID")
        if operation == "inspect":
            command.add_argument(
                "inference_call_id",
                metavar="INFERENCE_CALL",
                help="Inference call Fact ID",
            )
        elif operation == "fetch":
            command.add_argument(
                "--references",
                required=True,
                metavar="PATH",
                help="version 1 selection JSON (64 KiB; 1–32 distinct references)",
            )
            command.add_argument(
                "--output",
                required=True,
                metavar="PATH",
                help="packet destination (0600; must not exist)",
            )
        command.set_defaults(func=evidence_command)

    p_demo = sub.add_parser(
        "demo", help="plant one synthetic session so facts is non-zero"
    )
    p_demo.add_argument(
        "--force",
        action="store_true",
        help="allow a non-loopback server (writes synthetic facts to it)",
    )
    p_demo.set_defaults(func=cmd_demo)

    p_db = sub.add_parser(
        "db", help="provision database roles or inspect and upgrade the schema"
    )
    db_sub = p_db.add_subparsers(
        dest="db_operation",
        required=True,
        title="operations",
        metavar="<operation>",
        prog=p_db.prog,
    )
    for operation, help_text, func in (
        ("status", "inspect the schema revision without changing it", cmd_db_status),
        ("upgrade", "upgrade the schema under an advisory lock", cmd_db_upgrade),
    ):
        command = db_sub.add_parser(operation, help=help_text)
        command.add_argument(
            "--database-url",
            help="PostgreSQL URL (default: SEDIMENT_DATABASE_URL)",
        )
        command.set_defaults(func=func)

    provision = db_sub.add_parser(
        "provision",
        help="provision roles and migrate using isolated bootstrap credentials",
        description=(
            "Read SEDIMENT_BOOTSTRAP_DATABASE_URL, SEDIMENT_MIGRATOR_PASSWORD, "
            "SEDIMENT_RUNTIME_PASSWORD, and SEDIMENT_OPERATOR_PASSWORD from "
            "the environment. Stop services before provisioning."
        ),
    )
    provision.set_defaults(func=cmd_db_provision)

    for verb, help_text in (
        ("quarantine", "quarantine one fact by id"),
        ("release", "release one quarantined fact by id"),
    ):
        p = sub.add_parser(verb, help=help_text)
        p.add_argument("table", choices=_TABLES)
        p.add_argument("fact_id")
        p.add_argument("--reason", required=True)
        p.set_defaults(func=cmd_quarantine_or_release)

    p_log = sub.add_parser("quarantine-log", help="the org's quarantine history")
    p_log.add_argument("--tail", type=int, default=20, help="rows shown (default 20)")
    p_log.add_argument("--all", action="store_true", help="show the full history")
    p_log.set_defaults(func=cmd_quarantine_log)

    p_qc = sub.add_parser(
        "quarantine-inference-calls",
        help="bulk-quarantine inference calls by filter (dry-run by default)",
    )
    p_qc.add_argument("--session-id")
    p_qc.add_argument(
        "--provider",
        metavar="{" + ",".join(provider.value for provider in GatewayProvider) + "}",
        help="gateway provider filter",
    )
    p_qc.add_argument(
        "--between",
        nargs=2,
        metavar=("FROM", "TO"),
        help="ISO-8601 capture-time bounds, timezone-aware, inclusive",
    )
    p_qc.add_argument(
        "--all", action="store_true", help="allow a filterless (org-wide) run"
    )
    p_qc.add_argument(
        "--apply", action="store_true", help="write; without it, dry-run only"
    )
    p_qc.add_argument("--reason", required=True)
    p_qc.set_defaults(func=cmd_quarantine_inference_calls)

    # Stubs for --help only — the attribution dispatch in main() runs first
    # (the report/mirror-gc pattern).
    sub.add_parser(
        "install",
        help="wire a repo + this machine: git hooks, agent hooks, telemetry "
        "env (sediment install --help)",
    )
    sub.add_parser(
        "uninstall", help="remove the per-repo git hooks (--agents: user-level too)"
    )
    sub.add_parser("doctor", help="check attribution + server health on this machine")

    p_server = sub.add_parser(
        "server", help="run a local API server with managed PostgreSQL"
    )
    p_server.add_argument("--host", default="127.0.0.1")
    p_server.add_argument("--port", type=int, default=8000)
    p_server.add_argument(
        "--root",
        default=str(Path.home() / ".sediment" / "server"),
        help="server data, PostgreSQL binaries, credentials, and logs "
        "(default: ~/.sediment/server)",
    )
    p_server.set_defaults(func=cmd_server)

    p_login = sub.add_parser(
        "login",
        help="store credentials for a server",
        # Explicit description (it beats _propagate_descriptions): the token
        # resolution order is the thing a reader most needs, and stating it
        # here feeds both --help and the generated CLI reference.
        description="store verified operator credentials, or separate ingest "
        "credentials with --capture. Remote URLs must use "
        "HTTPS; HTTP is accepted only for localhost or a literal loopback "
        "IP address. A loopback URL enrolls both credentials sediment server "
        "generated in ~/.sediment/server/server.env; any other server "
        "prompts, or takes --with-token on stdin.",
    )
    p_login.add_argument(
        "url",
        help="HTTPS server base URL or loopback HTTP URL, e.g. http://127.0.0.1:8000",
    )
    p_login.add_argument(
        "--capture",
        action="store_true",
        help="enroll an ingest credential for capture without replacing operator login",
    )
    p_login.add_argument(
        "--with-token",
        action="store_true",
        help="read the token from stdin instead of prompting (unattended)",
    )
    p_login.set_defaults(func=cmd_login)

    p_logout = sub.add_parser("logout", help="remove stored credentials")
    p_logout.add_argument(
        "--server", help="server URL to log out of (default: current)"
    )
    p_logout.set_defaults(func=cmd_logout)

    p_commit = sub.add_parser("commit", help="pretty-print attributions for a commit")
    p_commit.add_argument("sha", help="full 40- or 64-char commit SHA")
    p_commit.add_argument(
        "--repo", help="observed repository name; ambiguous names require identity"
    )
    p_commit.add_argument(
        "--repository-provider",
        choices=[provider.value for provider in ForgeProvider],
        help="forge provider (requires host and ID)",
    )
    p_commit.add_argument(
        "--repository-host", help="forge hostname (requires provider and ID)"
    )
    p_commit.add_argument(
        "--repository-id", help="provider repository ID (requires provider and host)"
    )
    p_commit.add_argument(
        "--as-of", help="inclusive evidence boundary as an aware RFC 3339 timestamp"
    )
    p_commit.set_defaults(func=cmd_commit)

    p_derive = sub.add_parser(
        "derive", help="materialize a reviewed canonical derivation bundle"
    )
    p_derive.add_argument("--out", required=True, help="new bundle directory")
    p_derive.add_argument("--policy", help="strict derivation policy TOML file")
    p_derive.add_argument("--since", help="inclusive RFC 3339 completion time")
    p_derive.add_argument("--until", help="exclusive RFC 3339 completion time")
    p_derive.add_argument(
        "--users",
        nargs="+",
        metavar="USER",
        help="space-separated user IDs (default: every user in the organization)",
    )
    p_derive.add_argument(
        "--sample",
        type=int,
        default=0,
        metavar="N",
        help="print the first N canonical rows after writing",
    )
    p_derive.set_defaults(func=cmd_derive)

    p_e = sub.add_parser("export", help="run an export projection")
    e_sub = p_e.add_subparsers(
        dest="format",
        required=True,
        title="formats",
        metavar="<format>",
        prog=p_e.prog,
    )

    p_rlvr = e_sub.add_parser("rlvr", help="export one explicit RLVR target")
    p_rlvr.add_argument("--out", required=True, help="output directory")
    p_rlvr.add_argument(
        "--target",
        required=True,
        choices=("sediment", "swe-bench", "nemo-gym"),
        help="explicit consumer contract",
    )
    p_rlvr.add_argument(
        "--from", dest="from_bundle", help="validated derived bundle directory"
    )
    from sediment_export.compatibility import PROFILES

    p_rlvr.add_argument(
        "--profile",
        choices=tuple(p.id for p in PROFILES if p.objective == "rlvr"),
        help="exact optional consumer compatibility profile",
    )
    p_rlvr.add_argument(
        "--consumer-config",
        help="JSON task and response configuration for the selected profile",
    )
    p_rlvr.set_defaults(func=cmd_export_rlvr)

    p_dpo = e_sub.add_parser("dpo", help="dpo.jsonl — chosen/rejected pairs")
    p_dpo.add_argument("--out", required=True, help="output directory")
    p_dpo.add_argument(
        "--recipe",
        choices=("dpo_human", "dpo_outcome"),
        default="dpo_human",
        help="evidence recipe (default: dpo_human; dpo_outcome requires opt-in)",
    )
    p_dpo.add_argument(
        "--from", dest="from_bundle", help="validated derived bundle directory"
    )
    p_dpo.add_argument(
        "--profile",
        choices=tuple(p.id for p in PROFILES if p.objective == "dpo"),
        type=_dpo_profile_name,
        help="exact optional consumer compatibility profile",
    )
    p_dpo.set_defaults(func=cmd_export_dpo)

    p_sft = e_sub.add_parser("sft", help="sft.jsonl — supervised training targets")
    p_sft.add_argument("--out", required=True, help="output directory")
    p_sft.add_argument(
        "--recipe",
        choices=("sft_curated", "sft_verified"),
        default="sft_curated",
        help="evidence recipe (default: sft_curated; sft_verified requires opt-in)",
    )
    p_sft.add_argument(
        "--from", dest="from_bundle", help="validated derived bundle directory"
    )
    p_sft.add_argument(
        "--profile",
        choices=tuple(p.id for p in PROFILES if p.objective == "sft"),
        help="exact optional consumer compatibility profile",
    )
    p_sft.set_defaults(func=cmd_export_sft)

    p_diff = e_sub.add_parser(
        "diff-sft", help="diff_sft.jsonl — per-commit, per-file training rows"
    )
    p_diff.add_argument("--out", required=True, help="output directory")
    p_diff.add_argument(
        "--recipe",
        choices=("sft_curated", "sft_verified"),
        default="sft_curated",
        help="SFT evidence recipe (default: sft_curated; sft_verified requires opt-in)",
    )
    p_diff.add_argument(
        "--from", dest="from_bundle", help="validated derived bundle directory"
    )
    p_diff.set_defaults(func=cmd_export_diff_sft)

    p_rec = e_sub.add_parser(
        "recovery",
        help="recovery.jsonl — red-to-green CI-transition recovery pairs",
    )
    p_rec.add_argument("--out", required=True, help="output directory")
    p_rec.add_argument(
        "--recipe",
        choices=("recovery_ci",),
        default="recovery_ci",
        help="evidence recipe (default: recovery_ci)",
    )
    p_rec.set_defaults(func=cmd_export_recovery)

    _propagate_descriptions(parser)
    return parser


# Remote verbs speak HTTP through the client seam; they never open the fact
# store and never construct Settings.
_REMOTE_VERBS = {"login", "logout", "commit", "facts", "demo", "evidence"}

# Dispatched pre-argparse to the stdlib-only attribution module.
# install/uninstall/doctor get help stubs; the hook-plumbing verbs are
# execution-only (hidden from --help).
_ATTRIBUTION_VERBS = {
    "cursor-hook",
    "install",
    "uninstall",
    "doctor",
    "mark",
    "stamp",
    "union-squash-notes",
    "push-notes",
    "repair-notes",
}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["delivery"]:
        module = importlib.import_module("sediment_cli.delivery")
        if argv[1:] in (["-h"], ["--help"]):
            return _print_dispatched_help(
                module.build_parser(), prog="sediment delivery"
            )
        if len(argv) == 3 and argv[2] in {"-h", "--help"}:
            parser = module.build_parser(prog="sediment delivery")
            commands = next(
                action.choices
                for action in parser._actions
                if isinstance(action, argparse._SubParsersAction)
            )
            if argv[1] in commands:
                return _print_dispatched_help(
                    commands[argv[1]], prog=f"sediment delivery {argv[1]}"
                )
        return _public_status(module.main(argv[1:], prog="sediment delivery"))
    # Dispatched before argparse: report/mirror-gc forward argv verbatim to
    # the module's own parser and never construct Settings (see _REPORTS).
    if argv[:1] == ["report"]:
        return _run_report(argv[1:])
    if argv[:1] == ["mirror-gc"]:
        module = importlib.import_module("sediment_api.mirror_gc")
        if argv[1:] in (["-h"], ["--help"]):
            return _print_dispatched_help(
                module.build_parser(), prog="sediment mirror-gc"
            )
        try:
            return _public_status(module.main(argv[1:]))
        except (DatabaseOperationError, OSError, ValueError) as exc:
            return _fail(str(exc))
    if argv[:1] == ["transcript"]:
        module = importlib.import_module("sediment_cli.transcript")
        return _public_status(module.main(argv[1:]))
    if argv[:1] and argv[0] in _ATTRIBUTION_VERBS:
        # Forwarded verbatim to the attribution module's own parser —
        # stdlib-only, never constructs Settings. The plumbing verbs (mark,
        # stamp, union-squash-notes, push-notes, repair-notes) get no help
        # stub: hooks invoke them, humans don't.
        module = importlib.import_module("sediment_cli.attribution")
        if argv[0] in {"install", "uninstall", "doctor"} and argv[1:] in (
            ["-h"],
            ["--help"],
        ):
            action = _subparsers_action(module.build_parser())
            return _print_dispatched_help(
                action.choices[argv[0]], prog=f"sediment {argv[0]}"
            )
        return _public_status(module.main(argv))

    args = build_parser().parse_args(argv)

    if args.command == "export" and getattr(args, "from_bundle", None):
        try:
            args._mirror_path = os.environ.get("SEDIMENT_MIRROR_PATH")
            return args.func(None, None, args)
        except (OSError, ValueError) as exc:
            return _fail(str(exc))

    if args.command == "server":
        # Pre-Settings dispatch: provision before the API imports its settings.
        try:
            return args.func(args)
        except (DatabaseOperationError, OSError, ValueError) as exc:
            return _fail(str(exc))

    if args.command == "db":
        from sediment_core.postgres_migrations import MigrationError

        try:
            return args.func(args)
        except (DatabaseOperationError, MigrationError, ValueError) as exc:
            return _fail(str(exc))

    if args.command in _REMOTE_VERBS:
        try:
            return args.func(args)
        except (ClientError, DatabaseOperationError, ValueError) as exc:
            return _fail(str(exc))

    try:
        _prepare_store_command(args)

        # Deferred so --help needs no SEDIMENT_ORG_ID. Keep construction
        # inside this boundary because Pydantic settings validation is an
        # expected operator-input failure.
        from sediment_api.config import settings
        from sediment_api.database import one_shot_fact_store

        args._mirror_path = settings.mirror_path
        with one_shot_fact_store(
            settings.database_url.get_secret_value(), operation=f"run {args.command}"
        ) as store:
            return args.func(store, settings.org_id, args)
    except (DatabaseOperationError, OSError, ValueError) as exc:
        # One net for every validation error below (empty reason via the
        # QuarantineRecord validator, naive --between bounds, pydantic
        # settings): clean `error:` line, not a
        # traceback.
        return _fail(str(exc))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
