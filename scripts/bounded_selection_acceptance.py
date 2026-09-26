# SPDX-License-Identifier: AGPL-3.0-or-later
"""Exercise bounded K/J1 selection against the real API and PostgreSQL.

A loopback JEV stand-in returns controlled answers. This check proves factual
reads, grant and Quarantine enforcement, exact delivery, and fallback wiring;
it doesn't measure model judgment or cost.

Usage: uv run python scripts/bounded_selection_acceptance.py \\
  --database-url postgresql+psycopg://postgres:postgres@127.0.0.1:5432/postgres
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.error import URLError
from urllib.request import urlopen

from release_rehearsal import scratch_database

from sediment_core import (
    FactStore,
    FactTable,
    GatewayProvider,
    InferenceCall,
    InferenceMessage,
    TextPart,
    ToolCallPart,
    ToolCallResponsePart,
)
from sediment_core.postgres_engine import create_postgres_engine
from sediment_core.postgres_migrations import upgrade_database

import bounded_evidence_selection as selector
import bounded_selection_eval as runner
import session_context_retrieval_eval as legacy

ORG = "bounded-selection-acceptance"
TASKS = (("event-rollup", "missing"), ("layered-config", "correction"))
# A stand-in harness system prompt with similar vocabulary and size.
SYSTEM = (
    "You are an expert coding assistant operating inside a coding agent harness. "
    "You help users by reading files, executing commands, editing code, and "
    "writing new files.\n\nAvailable tools:\n- read: Read file contents\n- bash: "
    "Execute bash commands\n- edit: Make precise edits to files\n- write: Create "
    "or overwrite files\n\nGuidelines:\n- Use bash for file operations like ls, "
    "rg, find\n- Be concise in your responses\n- Show file paths clearly when "
    "working with files\n" + "Harness documentation paths and topics. " * 40
)


def conversation(family: str, profile: str) -> tuple[list, list]:
    """A synthetic final call shaped like a captured multi-prompt Session."""
    module = runner.labels()["families"][family]["module"]
    workspace = runner.FIXTURES / "families" / family / "workspace"
    prompts = runner.source_prompts(family, profile)
    check = subprocess.run(
        [sys.executable, "-B", "check_" + module],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=20,
    ).stdout
    inputs = [
        InferenceMessage(role="system", parts=[TextPart(content=SYSTEM)]),
        InferenceMessage(role="user", parts=[TextPart(content=prompts[0])]),
        InferenceMessage(
            role="assistant",
            parts=[
                TextPart(content=f"I'll read {module} first."),
                ToolCallPart(id="call-read", name="read", arguments={"path": module}),
            ],
        ),
        InferenceMessage(
            role="tool",
            parts=[
                ToolCallResponsePart(
                    id="call-read", result=(workspace / module).read_text()
                )
            ],
        ),
        InferenceMessage(
            role="assistant",
            parts=[
                ToolCallPart(
                    id="call-check",
                    name="bash",
                    arguments={"command": "python3 check_" + module},
                )
            ],
        ),
        InferenceMessage(
            role="tool", parts=[ToolCallResponsePart(id="call-check", result=check)]
        ),
    ]
    for prompt in prompts[1:]:
        inputs += [
            InferenceMessage(role="assistant", parts=[TextPart(content="Recorded.")]),
            InferenceMessage(role="user", parts=[TextPart(content=prompt)]),
        ]
    outputs = [InferenceMessage(role="assistant", parts=[TextPart(content="Noted.")])]
    return inputs, outputs


def seed(store: FactStore) -> dict:
    sessions = {}
    for family, profile in (*TASKS, ("duration-parse", "missing")):
        inputs, outputs = conversation(family, profile)
        call = InferenceCall(
            org_id=ORG,
            session_id=f"source-{family}-{profile}-{secrets.token_hex(4)}",
            gateway_provider=GatewayProvider.LITELLM,
            input_messages=inputs,
            output_messages=outputs,
        )
        store.store_inference_call(call)
        sessions[(family, profile)] = call
    return sessions


class StandIn(BaseHTTPRequestHandler):
    """Controlled JEV answers keyed by candidate content; not a model."""

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(body)
        if self.server.mode == "error":
            payload, status = b'{"detail":"stand-in failure"}', 503
        else:
            rule = self.server.rule
            answers = {}
            for candidate in body["state"]["candidates"]:
                carries = rule in json.dumps(candidate["part"])
                values = (0.9, 0.9, 0.1) if carries else (0.2, 0.1, 0.1)
                for name, value in zip(selector.PROPOSITIONS, values, strict=True):
                    answers[f"{candidate['id']}_{name}"] = {
                        "type": "noul",
                        "noul": value,
                    }
            payload = json.dumps(
                {
                    "model": selector.JEV_MODEL,
                    "answers": answers,
                    "usage": {"input_tokens": 900, "output_tokens": 12},
                }
            ).encode()
            status = 200
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@contextmanager
def stand_in():
    server = ThreadingHTTPServer(("127.0.0.1", 0), StandIn)
    server.requests, server.mode, server.rule = [], "answer", ""
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original = selector.JEV_URL
    selector.JEV_URL = f"http://127.0.0.1:{server.server_address[1]}/v1/systemone"
    try:
        yield server
    finally:
        selector.JEV_URL = original
        server.shutdown()


@contextmanager
def api(url: str, sources: list[str], directory: Path):
    retrieval, operator = secrets.token_hex(32), secrets.token_hex(32)
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "TMPDIR", "LANG", "LC_ALL"}
    } | {
        "SEDIMENT_ORG_ID": ORG,
        "SEDIMENT_DATABASE_URL": url,
        "SEDIMENT_DEV_MODE": "true",
        "SEDIMENT_OPERATOR_TOKEN": operator,
        "SEDIMENT_RETRIEVAL_TOKEN": retrieval,
        "SEDIMENT_RETRIEVAL_SESSION_IDS": json.dumps(sources),
    }
    with socket.socket() as listener, open(directory / "api.log", "wb") as log:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        endpoint = f"http://127.0.0.1:{listener.getsockname()[1]}"
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "sediment_api.main:app",
                "--fd",
                str(listener.fileno()),
                "--no-access-log",
            ],
            cwd=directory,
            env=environment,
            pass_fds=(listener.fileno(),),
            start_new_session=True,
            stdout=log,
            stderr=log,
        )
        try:
            deadline = time.monotonic() + 30
            while True:
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("Disposable API startup failed")
                try:
                    with urlopen(endpoint + "/health", timeout=0.5) as response:
                        if response.status == 200:
                            break
                except (URLError, TimeoutError):
                    time.sleep(0.1)
            yield {
                "api_url": endpoint,
                "operator_api_url": endpoint,
                "retrieval_token": retrieval,
                "operator_token": operator,
            }
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def expected_history(config: dict, session: str) -> tuple[str, int]:
    histories, _ = legacy.read_captured_calls(config, session, 1, complete=True)
    inventory = legacy.operator_read(
        config, "/query/evidence", params={"session_id": session}
    )
    history = legacy.verify_prefix(histories)
    return legacy.digest(legacy.encoded(history)), inventory["quarantine_revision"]


def run(database_url: str) -> dict:
    checks: dict[str, bool] = {}
    with (
        scratch_database(database_url) as url,
        tempfile.TemporaryDirectory(prefix="sediment-bounded-acceptance-") as tmp,
    ):
        directory = Path(tmp)
        upgrade_database(url)
        engine = create_postgres_engine(url)
        try:
            store = FactStore(engine)
            calls = seed(store)
            granted = [calls[task].session_id for task in TASKS]
            with api(url, granted, directory) as config, stand_in() as jev:
                results = {}
                for family, profile in TASKS:
                    call = calls[(family, profile)]
                    digest, revision = expected_history(config, call.session_id)
                    query = runner.continuation_text(family, profile)
                    rule = runner.labels()["families"][family]["rule"]
                    jev.rule = rule

                    def select(arm, name, key="stand-in-key", **change):
                        arguments = {
                            "session_id": call.session_id,
                            "inference_call_id": call.inference_call_id,
                            "expected_history_sha256": digest,
                            "expected_quarantine_revision": revision,
                        } | change
                        return selector.select_evidence(
                            config,
                            arguments["session_id"],
                            arguments["inference_call_id"],
                            query,
                            arm,
                            directory / f"{family}-{profile}-{name}",
                            expected_history_sha256=arguments[
                                "expected_history_sha256"
                            ],
                            expected_quarantine_revision=arguments[
                                "expected_quarantine_revision"
                            ],
                            jev_api_key=key if arm == "J1" else None,
                        )

                    before = len(jev.requests)
                    k = select("K", "k")
                    j1 = select("J1", "j1")
                    jev.mode = "error"
                    fallback = select("J1", "fallback")
                    jev.mode = "answer"
                    detail = j1.metrics["selection"]
                    anchors = [json.dumps(r, sort_keys=True) for r in detail["initial"]]
                    delivered = [
                        json.dumps(i["reference"], sort_keys=True) for i in j1.items
                    ]
                    results[f"{family}/{profile}"] = {
                        "k_parts": len(k.items),
                        "k_bytes": k.metrics["context_bytes"],
                        "j1_parts": len(j1.items),
                        "j1_bytes": j1.metrics["context_bytes"],
                        "j1_decision": detail["decision"],
                        "j1_added": detail["added"],
                        "rule_delivered_by_k": rule in k.context_text,
                        "rule_delivered_by_j1": rule in j1.context_text,
                    }
                    checks[f"{family}: K is bounded and exact"] = (
                        k.status == "selected"
                        and k.metrics["context_bytes"] <= selector.CONTEXT_BYTES_LIMIT
                        and k.metrics["jev"]["attempted_calls"] == 0
                    )
                    checks[f"{family}: J1 keeps initial evidence first"] = (
                        delivered[: len(anchors)] == anchors and len(anchors) == 2
                    )
                    checks[f"{family}: J1 made one selector call"] = (
                        j1.metrics["jev"]["attempted_calls"] == 1
                        and len(jev.requests) - before == 2
                        and j1.metrics["usage"]["input_tokens"] == 900
                    )
                    checks[f"{family}: provider failure uses K output"] = (
                        fallback.context_text == k.context_text
                        and fallback.metrics["selection"]["decision"] == "fallback"
                        and fallback.metrics["usage"]["input_tokens"] is None
                    )
                    other = calls[TASKS[1 - TASKS.index((family, profile))]]
                    for name, change, reason in (
                        (
                            "other-granted-session",
                            {"session_id": other.session_id},
                            "source_unavailable",
                        ),
                        (
                            "invented-call",
                            {"inference_call_id": "invented-call"},
                            "source_unavailable",
                        ),
                        (
                            "changed-history",
                            {"expected_history_sha256": "0" * 64},
                            "source_mismatch",
                        ),
                    ):
                        try:
                            select("J1", name, **change)
                            checks[f"{family}: {name} refused"] = False
                        except selector.BoundedSelectionError as exc:
                            checks[f"{family}: {name} refused"] = exc.reason == reason
                outside = calls[("duration-parse", "missing")]
                try:
                    selector.select_evidence(
                        config,
                        outside.session_id,
                        outside.inference_call_id,
                        "durations",
                        "K",
                        directory / "outside-grant",
                        expected_history_sha256="0" * 64,
                    )
                    checks["outside grant refused"] = False
                except selector.BoundedSelectionError as exc:
                    checks["outside grant refused"] = (
                        exc.reason == "source_grant_invalid"
                    )
                family, profile = TASKS[0]
                call = calls[(family, profile)]
                digest, revision = expected_history(config, call.session_id)
                store.quarantine_fact(
                    ORG,
                    FactTable.INFERENCE_CALLS,
                    call.inference_call_id,
                    reason="bounded-selection acceptance",
                )
                try:
                    selector.select_evidence(
                        config,
                        call.session_id,
                        call.inference_call_id,
                        runner.continuation_text(family, profile),
                        "J1",
                        directory / "quarantined",
                        expected_history_sha256=digest,
                        expected_quarantine_revision=revision,
                        jev_api_key="stand-in-key",
                    )
                    checks["quarantine refuses frozen source"] = False
                except selector.BoundedSelectionError as exc:
                    checks["quarantine refuses frozen source"] = (
                        exc.reason == "source_changed"
                    )
                for path in directory.rglob("*"):
                    if path.is_file() and path.name != "api.log":
                        content = path.read_bytes()
                        checks.setdefault("no credential in records", True)
                        if (
                            config["retrieval_token"].encode() in content
                            or config["operator_token"].encode() in content
                            or b"stand-in-key" in content
                        ):
                            checks["no credential in records"] = False
        finally:
            engine.dispose()
    return {"passed": all(checks.values()), "checks": checks, "results": results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    args = parser.parse_args()
    result = run(args.database_url)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
