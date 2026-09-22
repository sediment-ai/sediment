# SPDX-License-Identifier: AGPL-3.0-or-later
"""Rehearse declared synthetic populations through a disposable deployment."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.release_rehearsal import scratch_database  # noqa: E402


class ProbeFailure(RuntimeError):
    """A closed, content-free failure category for a qualification gate."""


def prepare_workspace(path: Path) -> None:
    path.mkdir(mode=0o700)
    path.chmod(0o700)


def population_plan(profile) -> dict:
    """Expose declared dimensions and known ceilings without running a workload."""
    from sediment_core.evidence import (
        CONTEXT_SOURCE_PART_LIMIT,
        EVIDENCE_SOURCE_BYTES_LIMIT,
    )
    from sediment_export.derived_bundle import _IDENTITY_LIMIT

    turns = profile.calls_per_session
    text_bytes = (
        turns * (turns + 1) // 2 * (profile.history_bytes + profile.output_bytes)
    )
    parts = turns * (turns + 1)
    return {
        "schema_version": 1,
        "qualification": "unmeasured",
        "profile": asdict(profile),
        "historical_sessions": profile.total_sessions,
        "historical_calls": profile.total_calls,
        "parts_per_session": parts,
        "repeated_text_bytes_per_session": text_bytes,
        "repeated_text_bytes": text_bytes * profile.total_sessions,
        "context_parts_limit": CONTEXT_SOURCE_PART_LIMIT,
        "context_parts_fit": parts <= CONTEXT_SOURCE_PART_LIMIT,
        "context_source_bytes_limit": EVIDENCE_SOURCE_BYTES_LIMIT,
        "context_text_alone_fits": text_bytes <= EVIDENCE_SOURCE_BYTES_LIMIT,
        "bundle_identity_limit": _IDENTITY_LIMIT,
        "bundle_identity_population_fits": profile.total_calls <= _IDENTITY_LIMIT,
        "physical_database_bytes": None,
        "limitations": [
            "Text totals exclude serialization, raw payloads, and metadata.",
            "PostgreSQL compression, indexes, backup, and export costs are unmeasured.",
            "Passing these necessary checks does not establish request capacity.",
            "Keyword source limits do not describe exact-reference fetch capacity.",
        ],
    }


def fingerprints(directory: Path) -> dict:
    """Hash incrementally, including empty files and JSONL row counts."""
    result = {}
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        digest, size, rows = hashlib.sha256(), 0, 0
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
                rows += chunk.count(b"\n")
        result[str(path.relative_to(directory))] = {
            "bytes": size,
            "sha256": digest.hexdigest(),
            "rows": rows if path.suffix == ".jsonl" else None,
        }
    return result


def overlapping_receipts(receipts: list[dict], start: float, end: float) -> int:
    return sum(
        row["stored"] is True and start <= row["started"] <= row["finished"] <= end
        for row in receipts
    )


def stop_process(process: subprocess.Popen) -> None:
    """Stop only the process group that this runner created."""
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def run_job(command, *, name, workspace, env, timeout, jobs, on_started=None) -> dict:
    record = {"name": name, "started": time.monotonic(), "status": "running"}
    jobs.append(record)
    with (workspace / f"{name}.log").open("wb") as log:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        record["pid"] = process.pid
        try:
            if on_started is not None:
                on_started()
            record["returncode"] = process.wait(timeout=timeout)
            if record["returncode"] != 0:
                raise ProbeFailure("job_exit")
            record["status"] = "passed"
        except subprocess.TimeoutExpired:
            record.update(status="failed", reason="job_timeout")
            raise ProbeFailure("job_timeout") from None
        except KeyboardInterrupt:
            record.update(status="failed", reason="interrupted")
            raise
        except ProbeFailure as error:
            record.update(status="failed", reason=str(error))
            raise
        finally:
            stop_process(process)
            record["finished"] = time.monotonic()
            record["elapsed_seconds"] = record["finished"] - record["started"]
    return record


def start_api(port: int, workspace: Path, env: dict) -> subprocess.Popen:
    """Let lifespan cleanup cancel detached workers before the outer deadline."""
    with (workspace / "api.log").open("wb") as log:
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "sediment_api.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--timeout-graceful-shutdown",
                "1",
            ],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def record_failure(report: dict, reason: str) -> None:
    report["status"] = "failed"
    report.setdefault("reason", reason)
    failures = report.setdefault("failures", [])
    if reason not in failures:
        failures.append(reason)


def write_receipt(journal, receipt: dict) -> None:
    journal.write(json.dumps(receipt, sort_keys=True) + "\n")
    journal.flush()


class Monitor:
    """Sample the runner and descendants; PostgreSQL can be on another host."""

    def __init__(self, workspace: Path, jobs: list[dict]):
        self.workspace = workspace
        self.jobs = jobs
        self.stop = threading.Event()
        self.samples = 0
        self.peak_rss_bytes = 0
        self.peak_workspace_bytes = 0
        self.minimum_free_bytes = None
        self.error = None
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        while not self.stop.is_set():
            try:
                output = subprocess.run(
                    ["ps", "-axo", "pid=,ppid=,rss="],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=5,
                ).stdout
                rows = [tuple(map(int, line.split())) for line in output.splitlines()]
                included = {os.getpid()}
                while True:
                    children = {pid for pid, parent, _ in rows if parent in included}
                    if children <= included:
                        break
                    included |= children
                rss = sum(rss * 1024 for pid, _, rss in rows if pid in included)
                size = 0
                for path in self.workspace.rglob("*"):
                    try:
                        if path.is_file() and not path.is_symlink():
                            size += path.stat().st_size
                    except FileNotFoundError:
                        pass  # A completed stage can disappear between samples.
                stat = os.statvfs(self.workspace)
                free = stat.f_bavail * stat.f_frsize
                self.peak_rss_bytes = max(self.peak_rss_bytes, rss)
                self.peak_workspace_bytes = max(self.peak_workspace_bytes, size)
                if self.jobs and self.jobs[-1]["status"] == "running":
                    job = self.jobs[-1]
                    job["peak_process_rss_bytes"] = max(
                        job.get("peak_process_rss_bytes", 0), rss
                    )
                    job["peak_workspace_logical_bytes"] = max(
                        job.get("peak_workspace_logical_bytes", 0), size
                    )
                self.minimum_free_bytes = min(self.minimum_free_bytes or free, free)
                self.samples += 1
            except Exception:
                self.error = "resource_sample_failed"
                return
            self.stop.wait(0.1)

    def finish(self) -> dict:
        self.stop.set()
        self.thread.join(timeout=10)
        return {
            "samples": self.samples,
            "sample_interval_seconds": 0.1,
            "peak_process_rss_bytes": self.peak_rss_bytes,
            "peak_workspace_logical_bytes": self.peak_workspace_bytes,
            "minimum_filesystem_free_bytes": self.minimum_free_bytes,
            "error": self.error,
            "scope": "runner, sender, API, workers, and CLI descendants",
            "postgresql_memory_bytes": None,
            "limitations": [
                "Sampled RSS can miss short peaks and double-count shared pages.",
                "PostgreSQL, container memory, swap, and filesystem quotas are unmeasured.",
                "Workspace size is logical bytes; it excludes PostgreSQL storage.",
                "Budgets are checked after execution, not enforced memory or disk ceilings.",
            ],
        }


def post_gateway(client, envelope) -> dict:
    started = time.monotonic()
    response = client.post("/ingest/gateway", json=envelope)
    if response.status_code != 200:
        raise ProbeFailure("ingest_http_status")
    body = response.json()
    if set(body) != {"fact_id", "stored"} or type(body["stored"]) is not bool:
        raise ProbeFailure("ingest_receipt_shape")
    if not isinstance(body["fact_id"], str) or not body["fact_id"].strip():
        raise ProbeFailure("ingest_receipt_identity")
    return {
        **body,
        "started": started,
        "finished": time.monotonic(),
        "session_id": envelope["session_id"],
        "model_call_id": envelope["payload"]["litellm_call_id"],
        "wire_bytes": len(response.request.content),
    }


class Sender:
    """One in-flight request, bounded receipt bookkeeping, complete histories."""

    def __init__(self, profile, url, token, journal_path):
        self.profile, self.url, self.token = profile, url, token
        self.journal_path = journal_path
        self.receipts = []
        self.error = None
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        from sim.capacity_workload import gateway_envelope

        try:
            with (
                self.journal_path.open("x") as journal,
                httpx.Client(
                    base_url=self.url,
                    headers={"Authorization": f"Bearer {self.token}"},
                    timeout=10,
                    trust_env=False,
                ) as client,
            ):
                for index in range(self.profile.max_live_calls):
                    if self.stop.is_set():
                        break
                    envelope = gateway_envelope(
                        self.profile,
                        index,
                        observed_at=datetime.now(UTC),
                        live=True,
                    )
                    receipt = post_gateway(client, envelope)
                    write_receipt(journal, receipt)
                    if not receipt["stored"]:
                        raise ProbeFailure("unexpected_live_duplicate")
                    self.receipts.append(receipt)
                    self.stop.wait(self.profile.live_interval_ms / 1000)
        except ProbeFailure as error:
            self.error = str(error)
        except Exception:
            self.error = "ingest_transport_failure"

    def finish(self):
        self.stop.set()
        self.thread.join(timeout=15)
        if self.thread.is_alive():
            raise ProbeFailure("sender_cleanup_timeout")
        if self.error:
            raise ProbeFailure(self.error)


def read_exact(client, session_id, reference, expected_part) -> dict:
    """Check one known Fact part; capacity refusals remain separate observations."""
    started = time.monotonic()
    response = client.post(
        "/query/context/evidence/read",
        json={
            "schema_version": 1,
            "session_id": session_id,
            "references": [reference],
        },
    )
    receipt = {
        "started": started,
        "finished": time.monotonic(),
        "http_status": response.status_code,
        "reason": None,
    }
    value = response.json()
    if response.status_code == 503 and value.get("detail") == "work capacity exceeded":
        receipt["reason"] = "work capacity exceeded"
        return receipt
    if response.status_code != 200:
        raise ProbeFailure("exact_evidence_http_status")
    items = value.get("items", [])
    if (
        len(items) != 1
        or items[0].get("reference") != reference
        or items[0].get("part") != expected_part
    ):
        raise ProbeFailure("exact_evidence_changed")
    receipt["response_sha256"] = hashlib.sha256(response.content).hexdigest()
    return receipt


class Reader:
    """One in-flight exact read competes with reports while capture continues."""

    def __init__(self, url, token, receipt, expected_part, maximum, journal_path):
        self.url, self.token = url, token
        self.session_id = receipt["session_id"]
        self.reference = {
            "inference_call_id": receipt["fact_id"],
            "side": "output",
            "message_index": 0,
            "part_index": 0,
        }
        self.expected_part, self.maximum = expected_part, maximum
        self.journal_path = journal_path
        self.receipts = []
        self.error = None
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        try:
            with (
                self.journal_path.open("x") as journal,
                httpx.Client(
                    base_url=self.url,
                    headers={"Authorization": f"Bearer {self.token}"},
                    timeout=35,
                    trust_env=False,
                ) as client,
            ):
                for _ in range(self.maximum):
                    if self.stop.is_set():
                        break
                    receipt = read_exact(
                        client, self.session_id, self.reference, self.expected_part
                    )
                    write_receipt(journal, receipt)
                    self.receipts.append(receipt)
                    self.stop.wait(0.1)
        except ProbeFailure as error:
            self.error = str(error)
        except Exception:
            self.error = "exact_evidence_transport_failure"

    def finish(self):
        self.stop.set()
        self.thread.join(timeout=40)
        if self.thread.is_alive():
            raise ProbeFailure("reader_cleanup_timeout")
        if self.error:
            raise ProbeFailure(self.error)


def inventory(store) -> dict:
    from sediment_core import FactTable

    return {
        **{table.value: store.count_facts("simcorp", table) for table in FactTable},
        "sessions": store.count_sessions("simcorp"),
        "quarantine_revision": store.quarantine_revision("simcorp"),
    }


def run_http_job(client, route, params, *, name, jobs, workspace):
    record = {"name": name, "started": time.monotonic(), "status": "running"}
    jobs.append(record)
    try:
        response = client.get(route, params=params)
        record["http_status"] = response.status_code
        if response.status_code != 200:
            raise ProbeFailure("report_http_status")
        value = response.json()
        if value.get("schema_version") != 1 or "report" not in value:
            raise ProbeFailure("report_shape")
        (workspace / f"{name}.json").write_bytes(response.content)
        record.update(
            status="passed",
            bytes=len(response.content),
            sha256=hashlib.sha256(response.content).hexdigest(),
        )
    except Exception as error:
        reason = str(error) if isinstance(error, ProbeFailure) else "report_transport"
        record.update(status="failed", reason=reason)
        raise ProbeFailure(reason) from None
    except KeyboardInterrupt:
        record.update(status="failed", reason="interrupted")
        raise
    finally:
        record["finished"] = time.monotonic()
        record["elapsed_seconds"] = record["finished"] - record["started"]


def rehearse(profile, workspace: Path, admin_url: str) -> dict:
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    from sediment_core import FactStore
    from sediment_core.postgres_migrations import upgrade_database
    from sim.capacity_workload import gateway_envelope, historical_observed_at
    from sim.capacity_push_probe import PushProbeFailure, prepare_push, post_and_wait

    prepare_workspace(workspace)
    old_mask = os.umask(0o077)
    report = {
        "schema_version": 1,
        "synthetic": True,
        "status": "running",
        "profile": asdict(profile),
        "plan": population_plan(profile),
        "jobs": [],
        "qualification": "Declared synthetic profile; not partner deployment acceptance",
        "temporal_limit": "Historical gateway observations; contemporary semantic Push/CI seed",
    }
    monitor = Monitor(workspace, report["jobs"])
    report["source_revision"] = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout.strip()
    report["source_dirty"] = bool(
        subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout
    )
    report["profile_sha256"] = hashlib.sha256(
        json.dumps(asdict(profile), sort_keys=True).encode()
    ).hexdigest()
    monitor.thread.start()
    sender = reader = server = engine = None
    try:
        with ExitStack() as cleanup:
            database_url = cleanup.enter_context(scratch_database(admin_url))
            report["scratch_database_name"] = make_url(database_url).database
            upgrade_database(database_url)
            env = {k: v for k, v in os.environ.items() if not k.startswith("SEDIMENT_")}
            stage = workspace / "staging"
            stage.mkdir(mode=0o700)
            journals = workspace / "receipts"
            journals.mkdir(mode=0o700)
            env.update(
                {
                    "SEDIMENT_DATABASE_URL": database_url,
                    "SEDIMENT_BOOTSTRAP_DATABASE_URL": database_url,
                    "SEDIMENT_ORG_ID": "simcorp",
                    "SEDIMENT_DEV_MODE": "true",
                    "SEDIMENT_API_BEARER_TOKEN": "sim-token-9c41-ingest-2b7f",
                    "SEDIMENT_OPERATOR_TOKEN": "sim-operator-2ab6-token-8c1d",
                    "SEDIMENT_RETRIEVAL_TOKEN": "sim-retrieval-47bb-token-902d",
                    "SEDIMENT_RETRIEVAL_SESSION_ID": "capacity/history/session-000000",
                    "SEDIMENT_GITHUB_WEBHOOK_SECRET": "sim-webhook-secret-d5f2-91a",
                    "SEDIMENT_GITHUB_HOST": "git.simcorp.example",
                    "SEDIMENT_MIRROR_PATH": str(workspace / "seed" / "mirrors"),
                    "TMPDIR": str(stage),
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": os.devnull,
                }
            )
            cli = [sys.executable, "-m", "sediment_cli.cli"]

            def command(name, *args, on_started=None):
                return run_job(
                    [*cli, *args],
                    name=name,
                    workspace=workspace,
                    env=env,
                    timeout=profile.job_timeout_seconds,
                    jobs=report["jobs"],
                    on_started=on_started,
                )

            run_job(
                [
                    sys.executable,
                    "-m",
                    "sim.scenarios",
                    "--workdir",
                    str(workspace / "seed"),
                ],
                name="semantic-seed",
                workspace=workspace,
                env=env,
                timeout=profile.job_timeout_seconds,
                jobs=report["jobs"],
            )
            engine = create_engine(database_url)
            cleanup.callback(engine.dispose)
            store = FactStore(engine)
            report["seed_population"] = inventory(store)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            url = f"http://127.0.0.1:{port}"
            server = start_api(port, workspace, env)
            cleanup.callback(
                lambda: stop_process(server) if server is not None else None
            )
            with (
                (journals / "historical.jsonl").open("x") as journal,
                httpx.Client(
                    base_url=url,
                    timeout=10,
                    trust_env=False,
                    headers={
                        "Authorization": f"Bearer {env['SEDIMENT_API_BEARER_TOKEN']}"
                    },
                ) as client,
            ):
                deadline = time.monotonic() + 30
                while True:
                    try:
                        if client.get("/health").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    if server.poll() is not None or time.monotonic() >= deadline:
                        raise ProbeFailure("api_startup")
                    time.sleep(0.05)
                anchor = datetime.now(UTC)
                report["historical_anchor"] = anchor.isoformat()
                history_receipts = []
                for index in range(profile.total_calls):
                    envelope = gateway_envelope(
                        profile,
                        index,
                        observed_at=historical_observed_at(profile, index, anchor),
                    )
                    receipt = post_gateway(client, envelope)
                    write_receipt(journal, receipt)
                    if not receipt["stored"]:
                        raise ProbeFailure("unexpected_historical_duplicate")
                    history_receipts.append(receipt)
                duplicate = post_gateway(client, envelope)
                write_receipt(journal, duplicate)
                if duplicate["stored"] or duplicate["fact_id"] != receipt["fact_id"]:
                    raise ProbeFailure("duplicate_not_collapsed")
                report["historical_population"] = inventory(store)
                report["duplicate_replay"] = {"retained_fact_id": True, "stored": False}
            sender = Sender(
                profile, url, env["SEDIMENT_API_BEARER_TOKEN"], journals / "live.jsonl"
            )
            sender.thread.start()

            def finish_sender():
                try:
                    sender.finish()
                except ProbeFailure as error:
                    record_failure(report, str(error))

            cleanup.callback(finish_sender)
            selected = history_receipts[profile.calls_per_session - 1]
            selected_envelope = gateway_envelope(
                profile, profile.calls_per_session - 1, observed_at=anchor
            )
            reader = Reader(
                url,
                env["SEDIMENT_RETRIEVAL_TOKEN"],
                selected,
                {
                    "type": "text",
                    "content": selected_envelope["payload"]["response"]["choices"][0][
                        "message"
                    ]["content"],
                },
                profile.max_live_calls,
                journals / "exact.jsonl",
            )
            reader.thread.start()

            def finish_reader():
                try:
                    reader.finish()
                except ProbeFailure as error:
                    record_failure(report, str(error))

            cleanup.callback(finish_reader)
            mixed_start = len(report["jobs"])
            with httpx.Client(
                base_url=url,
                timeout=profile.job_timeout_seconds,
                trust_env=False,
                headers={"Authorization": f"Bearer {env['SEDIMENT_OPERATOR_TOKEN']}"},
            ) as client:
                for route in ("model-outcomes", "accepted-work-lifecycle"):
                    boundary = datetime.now(UTC)
                    run_http_job(
                        client,
                        f"/v1/reports/{route}",
                        {
                            "cohort_start": (boundary - timedelta(days=7)).isoformat(),
                            "cohort_end": boundary.isoformat(),
                            "as_of": boundary.isoformat(),
                        },
                        name=route,
                        jobs=report["jobs"],
                        workspace=workspace,
                    )
            policy = workspace / "policy.toml"
            policy.write_text("schema_version = 1\n[split]\neval_fraction = 0.0\n")
            deadline = time.monotonic() + 15
            while not sender.receipts:
                if sender.error or time.monotonic() >= deadline:
                    raise ProbeFailure("no_live_capture")
                time.sleep(0.01)
            probe = prepare_push(workspace / "seed", sender.receipts[0]["session_id"])
            pool = cleanup.enter_context(ThreadPoolExecutor(max_workers=1))
            pending = []

            def start_push():
                pending.append(
                    pool.submit(
                        post_and_wait,
                        url,
                        secret=env["SEDIMENT_GITHUB_WEBHOOK_SECRET"],
                        store=store,
                        probe=probe,
                        timeout=min(profile.job_timeout_seconds, 135),
                    )
                )

            derive_job = command(
                "mixed-derive",
                "derive",
                "--out",
                str(workspace / "mixed-bundle"),
                "--policy",
                str(policy),
                on_started=start_push,
            )
            try:
                report["push_probe"] = pending[0].result(timeout=140)
            except PushProbeFailure as error:
                report["push_probe"] = error.result
                raise ProbeFailure(str(error)) from None
            if not (
                derive_job["started"]
                <= report["push_probe"]["started"]
                <= report["push_probe"]["acknowledged"]
                <= derive_job["finished"]
            ):
                raise ProbeFailure("no_push_overlap")
            command(
                "mixed-sft",
                "export",
                "sft",
                "--recipe",
                "sft_verified",
                "--from",
                str(workspace / "mixed-bundle"),
                "--out",
                str(workspace / "mixed-training"),
            )
            command(
                "mixed-rlvr",
                "export",
                "rlvr",
                "--target",
                "sediment",
                "--from",
                str(workspace / "mixed-bundle"),
                "--out",
                str(workspace / "mixed-training"),
            )
            reader.finish()
            sender.finish()
            for job in report["jobs"][mixed_start:]:
                job["concurrent_exact_reads"] = sum(
                    item["http_status"] == 200
                    and job["started"] <= item["started"]
                    and item["finished"] <= job["finished"]
                    for item in reader.receipts
                )
                job["concurrent_stored_receipts"] = overlapping_receipts(
                    sender.receipts,
                    job["started"],
                    job["finished"],
                )
                if not job["concurrent_stored_receipts"]:
                    raise ProbeFailure("no_ingest_overlap")
            if not any(
                job["concurrent_exact_reads"] for job in report["jobs"][mixed_start:]
            ):
                raise ProbeFailure("no_exact_read_overlap")
            receipts = history_receipts + sender.receipts
            report["live_receipts"] = len(sender.receipts)
            latencies = sorted(row["finished"] - row["started"] for row in receipts)
            report["ingest_latency_seconds"] = {
                "max": latencies[-1],
                "mean": sum(latencies) / len(latencies),
                "p50": latencies[(len(latencies) - 1) // 2],
                "p95": latencies[(len(latencies) * 95 + 99) // 100 - 1],
            }
            report["ingest_wire_bytes"] = {
                "historical": sum(row["wire_bytes"] for row in history_receipts),
                "live": sum(row["wire_bytes"] for row in sender.receipts),
                "maximum_request": max(row["wire_bytes"] for row in receipts),
            }
            report["final_population"] = inventory(store)
            identities = store.read_inference_call_summaries("simcorp")
            ids = {
                (str(row.inference_call_id), row.model_call_id, row.session_id)
                for row in identities
            }
            expected = {
                (row["fact_id"], row["model_call_id"], row["session_id"])
                for row in receipts
            }
            if (
                not expected <= ids
                or len(ids)
                != len(expected) + report["seed_population"]["inference_calls"]
            ):
                raise ProbeFailure("receipt_conservation")
            report["receipt_conservation"] = True
            # Ingest and mirrors are stopped before repeated byte comparisons.
            stop_process(server)
            server = None
            for name in ("fixed-a", "fixed-b"):
                command(
                    name,
                    "derive",
                    "--out",
                    str(workspace / name),
                    "--policy",
                    str(policy),
                )
            if inventory(store) != report["final_population"]:
                raise ProbeFailure("fixed_population_changed")
            fixed_a, fixed_b = (
                fingerprints(workspace / name) for name in ("fixed-a", "fixed-b")
            )
            if not fixed_a or fixed_a != fixed_b:
                raise ProbeFailure("fixed_bundle_determinism")
            report["fixed_bundle_determinism"] = True
            report["bundle_files"] = fixed_a
            report["training_files"] = fingerprints(workspace / "mixed-training")
            for filename in ("sft.jsonl", "tasks.jsonl", "rollouts.jsonl"):
                if report["training_files"].get(filename, {}).get("rows", 0) < 1:
                    raise ProbeFailure("positive_training_control_absent")
            if report["status"] != "failed":
                report["status"] = "passed"
    except ProbeFailure as error:
        record_failure(report, str(error))
    except KeyboardInterrupt:
        record_failure(report, "interrupted")
    except Exception as error:
        record_failure(report, f"exception_{type(error).__name__}")
    finally:
        if reader is not None:
            from scripts.agent_evidence_benchmark import summarize

            report["exact_retrieval"] = summarize(
                [
                    {
                        "status": item["http_status"],
                        "seconds": item["finished"] - item["started"],
                        "reason": item["reason"],
                    }
                    for item in reader.receipts
                ]
            )
        report["receipt_files"] = fingerprints(workspace / "receipts")
        report["resources"] = monitor.finish()
        if monitor.error or not monitor.samples:
            record_failure(report, "resource_sample_failed")
        if monitor.peak_rss_bytes > profile.max_process_rss_mib * 1024**2:
            record_failure(report, "process_memory_budget")
        if monitor.peak_workspace_bytes > profile.max_workspace_mib * 1024**2:
            record_failure(report, "workspace_budget")
        (workspace / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        os.umask(old_mask)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        type=Path,
        required=True,
        help="strict version 1 synthetic workload JSON",
    )
    parser.add_argument(
        "--out", type=Path, required=True, help="unused private output directory"
    )
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="write unmeasured population estimates and known limits without connecting",
    )
    return parser


def main(argv=None) -> int:
    from sim.capacity_workload import load_profile

    args = build_parser().parse_args(argv)

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    previous_term = signal.signal(signal.SIGTERM, interrupted)
    try:
        profile = load_profile(args.profile)
        if args.plan_only:
            prepare_workspace(args.out.resolve())
            (args.out / "plan.json").write_text(
                json.dumps(population_plan(profile), indent=2) + "\n"
            )
            print(f"capacity plan unmeasured: {args.out / 'plan.json'}")
            return 0
        admin_url = os.environ.get("SEDIMENT_DATABASE_URL")
        if not admin_url:
            raise ProbeFailure("missing_database_url")
        report = rehearse(profile, args.out.resolve(), admin_url)
    except Exception as error:
        reason = str(error) if isinstance(error, ProbeFailure) else type(error).__name__
        print(f"capacity rehearsal failed: {reason}", file=sys.stderr)
        return 2
    finally:
        signal.signal(signal.SIGTERM, previous_term)
    print(f"capacity rehearsal {report['status']}: {args.out / 'report.json'}")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
