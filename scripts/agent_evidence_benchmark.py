# SPDX-License-Identifier: AGPL-3.0-or-later
"""Measure real authorized evidence reads; the scripted chooser is not a model.

Set SEDIMENT_TEST_DATABASE_URL to an owned disposable PostgreSQL cluster. Pass
--runtime to compare installed workspace revisions with identical synthetic
Facts. Each run owns and removes one scratch database. Results contain no token
or database credential. Source/response hashes permit exact paired comparisons.
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import resource
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time


ROOT = Path(__file__).resolve().parents[1]
ORG = "evidence-benchmark"
SESSIONS = tuple(f"source-{index:02d}" for index in range(32))
QUERY = "compatibility duplicate FIRST failed assertion"
KNOWN_REFERENCE = {
    "inference_call_id": "source-00-call-000",
    "side": "output",
    "message_index": 0,
    "part_index": 0,
}
START = datetime(2026, 1, 1, tzinfo=UTC)


def encoded(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, default=str).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def summarize(records):
    """Nearest-rank percentiles; refused requests never lower success latency."""

    def distribution(values):
        values = sorted(values)
        return {
            "count": len(values),
            **{
                f"p{percent}_seconds": values[
                    math.ceil(len(values) * percent / 100) - 1
                ]
                if values
                else None
                for percent in (50, 95)
            },
        }

    return {
        "success": distribution(r["seconds"] for r in records if r["status"] == 200),
        "refusal": distribution(r["seconds"] for r in records if r["status"] != 200),
        "statuses": dict(Counter(str(r["status"]) for r in records)),
        "reasons": dict(Counter(r["reason"] for r in records if r.get("reason"))),
    }


def fixture_calls(session_count):
    from sediment_core import (
        GatewayProvider,
        InferenceCall,
        InferenceMessage,
        TextPart,
        ToolCallResponsePart,
    )

    calls = 10 if session_count == 1 else 25
    size = 4096 if session_count < 32 else 4800
    for session in SESSIONS[:session_count]:
        for index in range(calls):
            identifier = f"{session}-call-{index:03d}"
            # Code-like, varied token text; no artificially compressible single byte.
            padding = "\n".join(
                f"record_{i} = normalize_value(field_{i}, row_{index}); # invoice log"
                for i in range(100)
            )[:size]
            text = "Compatibility requirement: duplicate keys keep the FIRST value. "
            input_part = TextPart(
                content=(text if index == 0 else "Invoice rows. ") + padding
            )
            output = (
                ToolCallResponsePart(
                    id=f"check-{identifier}",
                    result={
                        "failure": "Failed assertion: AssertionError, duplicate value replaced FIRST.",
                        "integer": 2**100,
                        "log": padding,
                    },
                )
                if index == 0
                else TextPart(content="Optional formatter unavailable. " + padding)
            )
            yield InferenceCall(
                inference_call_id=identifier,
                org_id=ORG,
                session_id=session,
                gateway_provider=GatewayProvider.LITELLM,
                observed_at=START + timedelta(seconds=index),
                user_id="private-identity",
                raw={"private": "not-for-retrieval"},
                input_messages=[InferenceMessage(role="user", parts=[input_part])],
                output_messages=[InferenceMessage(role="tool", parts=[output])],
            )


def overlapping_ingests(writes, reads):
    return sum(
        write.get("stored", False)
        and any(
            read["status"] == 200
            and read["started"] <= write["started"]
            and write["finished"] <= read["finished"]
            for read in reads
        )
        for write in writes
    )


def clean_environment(database_url, sessions):
    return {
        **{
            k: v
            for k, v in os.environ.items()
            if k in {"PATH", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT"}
        },
        "SEDIMENT_DATABASE_URL": database_url,
        "SEDIMENT_ORG_ID": ORG,
        "SEDIMENT_DEV_MODE": "true",
        "SEDIMENT_OPERATOR_TOKEN": secrets.token_hex(32),
        "SEDIMENT_API_BEARER_TOKEN": secrets.token_hex(32),
        "SEDIMENT_RETRIEVAL_TOKEN": secrets.token_hex(32),
        "SEDIMENT_RETRIEVAL_SESSION_IDS": json.dumps(list(sessions)),
    }


def diagnostic(output):
    """Fresh-process companion timings, not substituted for public API latency."""
    started = time.perf_counter()
    import sediment_api.worker  # noqa: F401

    imported = time.perf_counter()
    from sediment_api.routers.query import _query_response
    from sediment_core import FactStore, EvidenceReference
    from sediment_core.postgres_engine import create_postgres_engine
    from sediment_derive.context_retrieval import discover_context, retrieve_context
    from sqlalchemy import event

    helpers_imported = time.perf_counter()
    engine = create_postgres_engine(
        os.environ["SEDIMENT_DATABASE_URL"], api_work=True, single_connection=True
    )
    store = FactStore(engine)
    sessions = json.loads(os.environ["SEDIMENT_RETRIEVAL_SESSION_IDS"])
    queries = []
    active = {}
    phase = "setup"

    def before(connection, cursor, statement, parameters, context, many):
        active[id(context)] = time.perf_counter()

    def after(connection, cursor, statement, parameters, context, many):
        if statement.lstrip().upper().startswith("SELECT"):
            queries.append(
                {
                    "sql": statement,
                    "parameters": parameters,
                    "cursor_seconds": time.perf_counter() - active[id(context)],
                    "rows": cursor.rowcount,
                    "phase": phase,
                }
            )

    event.listen(engine, "before_cursor_execute", before)
    event.listen(engine, "after_cursor_execute", after)
    stages = {}
    try:
        with store.read_snapshot() as snapshot:
            phase = "discovery"
            stamp = time.perf_counter()
            source = snapshot.read_context_discovery_source(ORG, sessions)
            stages["discovery_source_seconds"] = time.perf_counter() - stamp
            stamp = time.perf_counter()
            found = discover_context(source, QUERY)
            stages["discovery_selector_seconds"] = time.perf_counter() - stamp
            stamp = time.perf_counter()
            response = _query_response(found, type(found), max_bytes=16384)
            stages["discovery_encode_seconds"] = time.perf_counter() - stamp
        with store.read_snapshot() as snapshot:
            phase = "selected"
            stamp = time.perf_counter()
            source = snapshot.read_context_source(ORG, found.items[0].session_id)
            stages["selected_source_seconds"] = time.perf_counter() - stamp
            stamp = time.perf_counter()
            selected = retrieve_context(source, QUERY)
            stages["selected_selector_seconds"] = time.perf_counter() - stamp
            stamp = time.perf_counter()
            _query_response(selected, type(selected), max_bytes=16384)
            stages["selected_encode_seconds"] = time.perf_counter() - stamp
        with store.read_snapshot() as snapshot:
            phase = "known_reference"
            stamp = time.perf_counter()
            exact = snapshot.read_evidence_parts(
                ORG, SESSIONS[0], [EvidenceReference(**KNOWN_REFERENCE)]
            )
            stages["known_reference_source_seconds"] = time.perf_counter() - stamp
            stamp = time.perf_counter()
            _query_response(exact, type(exact))
            stages["known_reference_encode_seconds"] = time.perf_counter() - stamp
        measured = time.perf_counter()
        event.remove(engine, "before_cursor_execute", before)
        event.remove(engine, "after_cursor_execute", after)
        with engine.connect() as connection:
            for query in queries:
                if query["sql"].startswith("SELECT inference_calls."):
                    columns = set(
                        re.findall(
                            r"inference_calls\.(\w+)", query["sql"].split("FROM", 1)[0]
                        )
                    ) - {"observed_at"}
                    sizes = " + ".join(
                        f"coalesce(octet_length(source.{name}),0)"
                        for name in sorted(columns)
                    )
                    query["transferred_variable_bytes"] = connection.exec_driver_sql(
                        f"SELECT coalesce(sum({sizes}),0) FROM ({query['sql']}) AS source",
                        query["parameters"],
                    ).scalar_one()
                if (
                    "inference_calls" in query["sql"]
                    or "fact_quarantine" in query["sql"]
                ):
                    query["plan"] = connection.exec_driver_sql(
                        "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query["sql"],
                        query["parameters"],
                    ).scalar_one()
        result = {
            "import_seconds": imported - started,
            "diagnostic_helper_import_seconds": helpers_imported - imported,
            "python": sys.version,
            "stages": stages,
            "diagnostic_total_seconds_excluding_plans": measured - started,
            "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            * (1 if sys.platform == "darwin" else 1024),
            "discovery_sha256": hashlib.sha256(response.body).hexdigest(),
            "queries": queries,
            "scope": "Fresh process diagnostic; cursor times can exclude fetch/decoding; plans run afterward.",
        }
        output.write_bytes(encoded(result))
    finally:
        engine.dispose()


class MemorySampler:
    def __init__(self, pid):
        self.pid = pid
        self.peak = self.samples = self.workers = 0
        self.error = None
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.sample, daemon=True)

    def sample(self):
        while not self.stop.is_set():
            try:
                rows = [
                    tuple(map(int, row.split()))
                    for row in subprocess.check_output(
                        ["ps", "-axo", "pid=,ppid=,rss="], text=True, timeout=5
                    ).splitlines()
                ]
                included = {self.pid}
                while True:
                    expanded = included | {
                        pid for pid, parent, rss in rows if parent in included
                    }
                    if expanded == included:
                        break
                    included = expanded
                self.peak = max(
                    self.peak,
                    sum(rss * 1024 for pid, parent, rss in rows if pid in included),
                )
                self.workers = max(self.workers, len(included) - 1)
                self.samples += 1
            except Exception:
                self.error = "process_sample_failed"
                return
            self.stop.wait(0.05)

    def finish(self):
        self.stop.set()
        self.thread.join(timeout=6)
        return {
            "peak_api_tree_rss_bytes": self.peak,
            "maximum_descendants": self.workers,
            "samples": self.samples,
            "error": self.error,
            "interval_seconds": 0.05,
            "scope": "API and descendants; excludes PostgreSQL; sampled RSS may miss peaks and double-count shared pages.",
        }


@contextmanager
def api_server(python, environment, directory):
    import httpx

    with socket.socket() as listener, (directory / "api.log").open("wb") as log:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        endpoint = f"http://127.0.0.1:{listener.getsockname()[1]}"
        process = subprocess.Popen(
            [
                str(python),
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
            with httpx.Client(
                base_url=endpoint, trust_env=False, timeout=0.5
            ) as client:
                while True:
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("api_startup_failed")
                    try:
                        if client.get("/health").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.1)
            yield endpoint, process.pid
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)


def request(client, method, route, **kwargs):
    started = time.perf_counter()
    response = client.request(method, route, **kwargs)
    finished = time.perf_counter()
    value = response.json()
    detail = value.get("detail") if isinstance(value, dict) else None
    record = {
        "route": route,
        "started": started,
        "finished": finished,
        "seconds": finished - started,
        "status": response.status_code,
        "bytes": len(response.content),
        "sha256": hashlib.sha256(response.content).hexdigest(),
        "reason": detail
        if isinstance(detail, str)
        else detail.get("reason")
        if isinstance(detail, dict)
        else None,
    }
    if response.status_code not in {200, 503}:
        raise RuntimeError(f"unexpected_http_status_{response.status_code}")
    if response.status_code == 200 and (
        b"private-identity" in response.content
        or b"not-for-retrieval" in response.content
    ):
        raise RuntimeError("private_content_exposed")
    return record, value


def verify_selected(value, mode, reference, expected_part):
    """Canonical bytes distinguish integer values from rounded JSON floats."""
    items = (
        [item["evidence"] for item in value["items"]]
        if mode == "keyword"
        else value["items"]
    )
    matches = [
        item
        for item in items
        if item["reference"] == reference
        and encoded(item["part"]) == encoded(expected_part)
    ]
    if len(matches) != 1 or (mode != "keyword" and len(items) != 1):
        raise RuntimeError("exact_selected_evidence_changed")


def flow(endpoint, token, mode, barrier=None, known_part=None):
    import httpx

    with httpx.Client(
        base_url=endpoint,
        headers={"Authorization": f"Bearer {token}"},
        trust_env=False,
        timeout=35,
        follow_redirects=False,
    ) as client:
        if barrier is not None:
            barrier.wait(timeout=15)
        started = time.perf_counter()
        rows = []
        session, reference = SESSIONS[0], KNOWN_REFERENCE
        expected_part = known_part
        if mode != "known":
            record, result = request(
                client,
                "POST",
                "/query/context/discover",
                json={"schema_version": 1, "query": QUERY},
            )
            rows.append(record)
            if record["status"] != 200:
                return rows, {
                    "status": record["status"],
                    "seconds": time.perf_counter() - started,
                    "reason": record["reason"],
                }
            # Transport probe only: choose the first returned candidate, no utility claim.
            candidate = result["items"][0]
            session = candidate["session_id"]
            reference = candidate["preview"]["reference"]
            expected_part = candidate["preview"]["part"]
        if mode == "keyword":
            route = "/query/context/selected"
            body = {"schema_version": 1, "session_id": session, "query": QUERY}
        else:
            route = "/query/context/evidence/read"
            body = {
                "schema_version": 1,
                "session_id": session,
                "references": [reference],
            }
        record, result = request(client, "POST", route, json=body)
        rows.append(record)
        if record["status"] == 200:
            verify_selected(result, mode, reference, expected_part)
        return rows, {
            "status": record["status"],
            "seconds": time.perf_counter() - started,
            "reason": record["reason"],
        }


def live_probe(endpoint, token, stop, writes, health, *, prefix="live", count=2000):
    import httpx

    payload = json.loads(
        (
            ROOT
            / "packages/capture/tests/fixtures/litellm_standard_logging_object.json"
        ).read_text()
    )
    with httpx.Client(
        base_url=endpoint,
        headers={"Authorization": f"Bearer {token}"},
        trust_env=False,
        timeout=10,
    ) as client:
        index = 0
        while not stop.is_set() and index < count:
            payload["litellm_call_id"] = f"{prefix}-{index:06d}"
            record, value = request(
                client,
                "POST",
                "/ingest/gateway",
                json={
                    "provider": "litellm",
                    "session_id": "live-outside-grant",
                    "payload": payload,
                },
            )
            record.update(value)
            if value.get("stored") is not True or not value.get("fact_id"):
                raise RuntimeError("ingest_not_stored")
            writes.append(record)
            health.append(request(client, "GET", "/health")[0])
            index += 1
            stop.wait(0.1)


def seed(store, engine, session_count, background):
    from sediment_core import GatewayProvider, InferenceCall, InferenceMessage, TextPart
    from sqlalchemy import text

    fingerprint = hashlib.sha256()
    for call in fixture_calls(session_count):
        fingerprint.update(call.model_dump_json().encode())
        if not store.store_inference_call(call):
            raise RuntimeError("fixture_duplicate")
    for index in range(background + 1):
        call = InferenceCall(
            inference_call_id=f"outside-{index:06d}",
            org_id=ORG if index < background else "other-org",
            session_id="outside-grant",
            gateway_provider=GatewayProvider.LITELLM,
            observed_at=START,
            input_messages=[],
            output_messages=[
                InferenceMessage(
                    role="assistant",
                    parts=[
                        TextPart(
                            content="Compatibility duplicate FIRST failed assertion, outside grant."
                        )
                    ],
                )
            ],
        )
        store.store_inference_call(call)
    with engine.begin() as connection:
        connection.exec_driver_sql("ANALYZE")
        source = (
            connection.execute(
                text(
                    "SELECT count(*) AS calls, sum(octet_length(session_id) + octet_length(inference_call_id) + octet_length(input_messages) + octet_length(output_messages) + coalesce(octet_length(model_provider),0) + coalesce(octet_length(model),0)) AS source_bytes FROM inference_calls WHERE org_id=:org AND session_id=ANY(:sessions)"
                ),
                {"org": ORG, "sessions": list(SESSIONS[:session_count])},
            )
            .mappings()
            .one()
        )
        version = connection.exec_driver_sql("SELECT version()").scalar_one()
    return {
        "sessions": session_count,
        "background_same_org_calls": background,
        "background_other_org_calls": 1,
        "fixture_sha256": fingerprint.hexdigest(),
        **dict(source),
        "postgresql": version,
        "commit_observations": 0,
        "description": "Synthetic uncommitted requirements and failed attempts; scripted chooser does not measure model quality.",
    }


def run(args):
    import httpx
    from release_rehearsal import scratch_database
    from sediment_core import FactStore
    from sediment_core.postgres_engine import create_postgres_engine
    from sediment_core.postgres_migrations import upgrade_database
    from sqlalchemy import text

    args.output.mkdir(parents=True, exist_ok=False)
    runtime = args.runtime.resolve()
    python = runtime / ".venv/bin/python"
    report = {
        "schema_version": 1,
        "started_at_utc": datetime.now(UTC).isoformat(),
        "runtime": str(runtime),
        "revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=runtime, text=True
        ).strip(),
        "dirty": subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=runtime, text=True
        ).splitlines(),
        "python": sys.version,
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "samples": args.samples,
        "waves": args.waves,
        "percentile_method": "nearest rank",
        "cache": "warm; first public flow discarded",
        "qualification": "Synthetic infrastructure benchmark, not a decision-model evaluation or customer capacity qualification.",
        "traffic": "One mode per run. Single-client samples precede the live sender; concurrent waves include verified gateway ingestion.",
        "scenarios": [],
    }
    with scratch_database(os.environ["SEDIMENT_TEST_DATABASE_URL"]) as database_url:
        upgrade_database(database_url)
        engine = create_postgres_engine(database_url)
        try:
            store = FactStore(engine)
            source = seed(store, engine, args.sessions, args.background)
            known_part = (
                next(fixture_calls(args.sessions))
                .output_messages[0]
                .parts[0]
                .model_dump(mode="python")
            )
            report["source"] = source
            environment = clean_environment(database_url, SESSIONS[: args.sessions])
            subprocess.run(
                [
                    str(python),
                    str(Path(__file__).resolve()),
                    "--diagnostic",
                    str(args.output / "diagnostic.json"),
                ],
                env=environment,
                cwd=args.output,
                check=True,
                timeout=120,
            )
            if args.diagnostic_only:
                (args.output / "report.json").write_bytes(encoded(report))
                return
            with api_server(python, environment, args.output) as (endpoint, pid):
                sampler = MemorySampler(pid)
                sampler.thread.start()
                writes, health, all_reads = [], [], []
                stop = threading.Event()
                control, control_health, probe_errors = [], [], []
                live_probe(
                    endpoint,
                    environment["SEDIMENT_API_BEARER_TOKEN"],
                    stop,
                    control,
                    control_health,
                    prefix="control",
                    count=5,
                )
                report["ingest_control"] = {
                    "summary": summarize(control),
                    "receipts": control,
                }

                def send():
                    try:
                        live_probe(
                            endpoint,
                            environment["SEDIMENT_API_BEARER_TOKEN"],
                            stop,
                            writes,
                            health,
                        )
                    except Exception as error:
                        probe_errors.append(type(error).__name__)

                sender = threading.Thread(target=send, daemon=True)
                try:
                    for mode in args.modes:
                        flow(
                            endpoint,
                            environment["SEDIMENT_RETRIEVAL_TOKEN"],
                            mode,
                            known_part=known_part,
                        )
                        for concurrency in args.clients:
                            rows, flows = [], []
                            count = args.samples if concurrency == 1 else args.waves
                            if concurrency > 1 and not sender.is_alive():
                                sender.start()
                            stamp = time.perf_counter()
                            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                                for wave in range(count):
                                    barrier = threading.Barrier(concurrency)
                                    futures = [
                                        pool.submit(
                                            flow,
                                            endpoint,
                                            environment["SEDIMENT_RETRIEVAL_TOKEN"],
                                            mode,
                                            barrier,
                                            known_part,
                                        )
                                        for _ in range(concurrency)
                                    ]
                                    for future in futures:
                                        requests, completed = future.result()
                                        rows.extend(requests)
                                        flows.append(completed)
                            elapsed = time.perf_counter() - stamp
                            all_reads.extend(rows)
                            scenario = {
                                "mode": mode,
                                "concurrent_ingest": sender.is_alive(),
                                "clients": concurrency,
                                "elapsed_seconds": elapsed,
                                "requests": rows,
                                "flows": flows,
                                "flow_summary": summarize(flows),
                                "route_summaries": {
                                    route: summarize(
                                        [r for r in rows if r["route"] == route]
                                    )
                                    for route in sorted({r["route"] for r in rows})
                                },
                                "completed_flows_per_second": sum(
                                    f["status"] == 200 for f in flows
                                )
                                / elapsed,
                            }
                            report["scenarios"].append(scenario)
                            (args.output / "report.json").write_bytes(encoded(report))
                finally:
                    stop.set()
                    if sender.ident is not None:
                        sender.join(timeout=15)
                    report["memory"] = sampler.finish()
                if sender.is_alive() or sampler.error or probe_errors:
                    raise RuntimeError("probe_cleanup_or_sampling_failed")
                with engine.connect() as connection:
                    ids = set(
                        connection.execute(
                            text(
                                "SELECT inference_call_id FROM inference_calls WHERE org_id=:org AND session_id='live-outside-grant'"
                            ),
                            {"org": ORG},
                        ).scalars()
                    )
                if ids != {w["fact_id"] for w in writes + control if w.get("stored")}:
                    raise RuntimeError("ingest_receipt_mismatch")
                overlap = overlapping_ingests(writes, all_reads)
                if any(c > 1 for c in args.clients) and not overlap:
                    raise RuntimeError("concurrent_ingest_not_observed")
                report["ingest"] = {
                    "summary": summarize(writes),
                    "receipts": writes,
                    "verified_facts": len(ids),
                    "fully_overlapping_receipts": overlap,
                }
                report["health"] = {"summary": summarize(health), "requests": health}
                with httpx.Client(base_url=endpoint, trust_env=False) as client:
                    if client.get("/health").status_code != 200:
                        raise RuntimeError("final_health_failed")
            report["scratch_database_removed_after_run"] = True
        finally:
            engine.dispose()
    (args.output / "report.json").write_bytes(encoded(report))
    print(f"Evidence benchmark saved to {args.output / 'report.json'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--sessions", type=int, choices=(1, 8, 32), default=8)
    parser.add_argument("--background", type=int, default=0)
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--waves", type=int, default=10)
    parser.add_argument(
        "--clients", type=int, choices=(1, 5, 10), nargs="+", default=[1, 5, 10]
    )
    parser.add_argument(
        "--modes", choices=("keyword", "exact", "known"), nargs="+", default=["keyword"]
    )
    parser.add_argument("--diagnostic", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--diagnostic-only", action="store_true")
    args = parser.parse_args()
    if args.diagnostic:
        diagnostic(args.diagnostic)
    elif (
        args.output is None
        or not 0 <= args.background <= 100000
        or not 1 <= args.samples <= 1000
        or not 1 <= args.waves <= 100
        or len(args.modes) != 1
    ):
        parser.error(
            "output and bounded nonnegative background/positive samples/waves required"
        )
    else:
        run(args)


if __name__ == "__main__":
    main()
