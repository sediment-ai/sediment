# SPDX-License-Identifier: AGPL-3.0-or-later
"""Capacity probes fail visibly and measure only completed concurrent traffic."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import httpx

from scripts import capacity_rehearsal as rehearsal


def test_plan_counts_complete_repeated_history_without_claiming_qualification():
    from sim.capacity_workload import CapacityProfile

    values = json.loads(
        (rehearsal.ROOT / "sim/profiles/capacity-smoke.json").read_text()
    )
    values.update(history_weeks=24, sessions_per_week=100, calls_per_session=100)
    profile = CapacityProfile(**values)
    plan = rehearsal.population_plan(profile)
    assert plan["historical_sessions"] == 2400
    assert plan["historical_calls"] == 240000
    assert plan["qualification"] == "unmeasured"
    assert plan["parts_per_session"] == 10100
    assert plan["repeated_text_bytes_per_session"] == (
        5050 * (profile.history_bytes + profile.output_bytes)
    )
    assert plan["context_parts_fit"] is True
    assert plan["context_parts_limit"] == 16_384
    assert plan["context_text_alone_fits"] is True
    assert plan["context_source_bytes_limit"] == 64 * 1024 * 1024
    assert plan["bundle_identity_population_fits"] is False
    assert plan["physical_database_bytes"] is None


def test_plan_only_never_connects_or_runs_jobs(tmp_path):
    output = tmp_path / "plan"
    result = subprocess.run(
        [
            sys.executable,
            str(Path(rehearsal.__file__)),
            "--profile",
            str(rehearsal.ROOT / "sim/profiles/capacity-pilot.json"),
            "--out",
            str(output),
            "--plan-only",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "SEDIMENT_DATABASE_URL": "must-not-connect"},
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "unmeasured" in result.stdout
    plan = json.loads((output / "plan.json").read_text())
    assert plan["historical_calls"] == 240000
    assert plan["repeated_text_bytes"] == 124108800000
    assert not (output / "report.json").exists()


def test_capture_identity_and_org_scoped_fact_identity_are_distinct():
    envelope = {
        "capture": {"id": "7bbad4f9-886f-4564-8838-ed0741baf2f7"},
        "session_id": "capacity-session",
        "payload": {"litellm_call_id": "capacity-call"},
    }
    fact_id = "bf46e1a0-49b4-4aee-aa82-034e4fe40ad8"
    with httpx.Client(
        base_url="http://capacity.test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"fact_id": fact_id, "stored": True}
            )
        ),
    ) as client:
        receipt = rehearsal.post_gateway(client, envelope)
    assert receipt["fact_id"] == fact_id
    assert receipt["model_call_id"] == "capacity-call"
    assert receipt["session_id"] == "capacity-session"


@pytest.mark.parametrize("changed", [False, True])
def test_exact_read_probe_checks_the_complete_selected_part(changed):
    reference = {
        "inference_call_id": "call",
        "side": "output",
        "message_index": 0,
        "part_index": 0,
    }
    part = {"type": "text", "content": "synthetic response"}
    response_part = {**part, "content": "changed"} if changed else part
    with httpx.Client(
        base_url="http://capacity.test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"items": [{"reference": reference, "part": response_part}]},
            )
        ),
    ) as client:
        if changed:
            with pytest.raises(rehearsal.ProbeFailure, match="exact_evidence_changed"):
                rehearsal.read_exact(client, "session", reference, part)
        else:
            receipt = rehearsal.read_exact(client, "session", reference, part)
            assert receipt["http_status"] == 200
            assert receipt["finished"] >= receipt["started"]


def test_exact_read_probe_counts_capacity_separately():
    with httpx.Client(
        base_url="http://capacity.test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                503, json={"detail": "work capacity exceeded"}
            )
        ),
    ) as client:
        receipt = rehearsal.read_exact(client, "session", {}, {})
    assert receipt["http_status"] == 503
    assert receipt["reason"] == "work capacity exceeded"


def test_overlap_requires_request_and_receipt_inside_job():
    receipts = [
        {"started": 0.9, "finished": 1.1, "stored": True},
        {"started": 1.2, "finished": 1.3, "stored": True},
        {"started": 1.4, "finished": 1.5, "stored": False},
        {"started": 1.9, "finished": 2.1, "stored": True},
    ]
    assert rehearsal.overlapping_receipts(receipts, 1.0, 2.0) == 1


def test_fingerprints_stream_files_and_count_rows(tmp_path):
    (tmp_path / "rows.jsonl").write_bytes(b'{"a":1}\n{"a":2}\n')
    first = rehearsal.fingerprints(tmp_path)
    assert first["rows.jsonl"]["rows"] == 2
    assert first["rows.jsonl"]["bytes"] == 16
    (tmp_path / "rows.jsonl").write_bytes(b'{"a":1}\n{"a":3}\n')
    assert first != rehearsal.fingerprints(tmp_path)


def test_process_timeout_records_failure_and_stops_process(tmp_path):
    jobs = []
    with pytest.raises(rehearsal.ProbeFailure, match="job_timeout"):
        rehearsal.run_job(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            name="timeout",
            workspace=tmp_path,
            env=dict(os.environ),
            timeout=0.15,
            jobs=jobs,
        )
    assert jobs[0]["status"] == "failed"
    assert jobs[0]["reason"] == "job_timeout"
    with pytest.raises(ProcessLookupError):
        os.kill(jobs[0]["pid"], 0)


def test_failed_command_does_not_publish_credential_in_error(tmp_path):
    jobs = []
    with pytest.raises(rehearsal.ProbeFailure) as error:
        rehearsal.run_job(
            [sys.executable, "-c", "raise RuntimeError('private-credential')"],
            name="failed",
            workspace=tmp_path,
            env=dict(os.environ),
            timeout=10,
            jobs=jobs,
        )
    assert "private-credential" not in str(error.value)
    assert "private-credential" not in json.dumps(jobs)
    assert jobs[0]["returncode"] != 0


def test_invalid_profile_fails_before_creating_output(tmp_path):
    profile = tmp_path / "invalid.json"
    profile.write_text('{"schema_version": 99}')
    output = tmp_path / "result"
    result = subprocess.run(
        [
            sys.executable,
            str(Path(rehearsal.__file__)),
            "--profile",
            str(profile),
            "--out",
            str(output),
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "SEDIMENT_DATABASE_URL": "must-not-connect"},
        timeout=20,
    )
    assert result.returncode != 0
    assert not output.exists()
    assert "Traceback" not in result.stderr


def test_output_directory_is_private_and_cannot_be_reused(tmp_path):
    output = tmp_path / "output"
    rehearsal.prepare_workspace(output)
    assert output.stat().st_mode & 0o777 == 0o700
    with pytest.raises(FileExistsError):
        rehearsal.prepare_workspace(output)


def test_api_cleanup_stops_detached_report_worker(tmp_path, postgres_database_factory):
    from sqlalchemy import create_engine

    database = postgres_database_factory()
    env = {k: v for k, v in os.environ.items() if not k.startswith("SEDIMENT_")}
    env.update(
        SEDIMENT_DATABASE_URL=database,
        SEDIMENT_ORG_ID="simcorp",
        SEDIMENT_DEV_MODE="true",
        SEDIMENT_OPERATOR_TOKEN="capacity-test-operator",
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = rehearsal.start_api(port, tmp_path, env)
    engine = create_engine(database)
    worker_pid = None
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}", timeout=15, trust_env=False
        ) as client:
            until = time.monotonic() + 15
            while True:
                try:
                    if client.get("/health").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                assert time.monotonic() < until and server.poll() is None
                time.sleep(0.05)
            # A real report blocks on this table until its worker is cancelled.
            with engine.connect() as blocker:
                blocker.exec_driver_sql(
                    "LOCK TABLE inference_calls IN ACCESS EXCLUSIVE MODE"
                )
                boundary = datetime.now(UTC)
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pending = pool.submit(
                        client.get,
                        "/v1/reports/model-outcomes",
                        params={
                            "cohort_start": (boundary - timedelta(days=7)).isoformat(),
                            "cohort_end": boundary.isoformat(),
                            "as_of": boundary.isoformat(),
                        },
                        headers={"Authorization": "Bearer capacity-test-operator"},
                    )
                    until = time.monotonic() + 10
                    while worker_pid is None:
                        rows = subprocess.check_output(
                            ["ps", "-axo", "pid=,ppid="], text=True
                        )
                        children = [
                            int(row.split()[0])
                            for row in rows.splitlines()
                            if int(row.split()[1]) == server.pid
                        ]
                        if children:
                            worker_pid = children[0]
                            break
                        assert time.monotonic() < until
                        time.sleep(0.02)
                    assert os.getpgid(worker_pid) != os.getpgid(server.pid)
                    rehearsal.stop_process(server)
                    with pytest.raises(ProcessLookupError):
                        os.kill(worker_pid, 0)
                    try:
                        pending.result(timeout=5)
                    except httpx.HTTPError:
                        pass
    finally:
        if worker_pid is not None:
            try:
                os.killpg(worker_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        rehearsal.stop_process(server)
        engine.dispose()


@pytest.mark.parametrize("interrupt", [signal.SIGTERM, signal.SIGINT])
def test_interruption_records_failure_and_cleans_database(
    tmp_path, postgres_admin_url, interrupt
):
    from sqlalchemy import create_engine

    engine = create_engine(postgres_admin_url)
    output = tmp_path / "interrupted"
    child = subprocess.Popen(
        [
            sys.executable,
            str(Path(rehearsal.__file__)),
            "--profile",
            str(rehearsal.ROOT / "sim/profiles/capacity-smoke.json"),
            "--out",
            str(output),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "SEDIMENT_DATABASE_URL": postgres_admin_url},
    )
    try:
        until = time.monotonic() + 15
        while (
            not (output / "semantic-seed.log").exists()
            or (output / "semantic-seed.log").stat().st_size == 0
        ):
            assert child.poll() is None and time.monotonic() < until
            time.sleep(0.05)
        child.send_signal(interrupt)
        child.wait(timeout=15)
        report = json.loads((output / "report.json").read_text())
        assert report["status"] == "failed"
        assert report["reason"] == "interrupted"
        assert report["jobs"][0]["status"] == "failed"
        with pytest.raises(ProcessLookupError):
            os.kill(report["jobs"][0]["pid"], 0)
        with engine.connect() as connection:
            after = set(
                connection.exec_driver_sql("SELECT datname FROM pg_database").scalars()
            )
        assert report["scratch_database_name"] not in after
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        engine.dispose()


@pytest.mark.parametrize("deadline", [None, 1, "no_overlap"])
def test_real_capacity_smoke_conserves_receipts_and_cleans_database(
    tmp_path, postgres_admin_url, deadline
):
    from sqlalchemy import create_engine, text

    engine = create_engine(postgres_admin_url)

    def databases():
        with engine.connect() as connection:
            return set(
                connection.execute(
                    text("SELECT datname FROM pg_database WHERE datname LIKE :prefix"),
                    {"prefix": "sediment_rehearsal_%"},
                ).scalars()
            )

    try:
        output = tmp_path / "smoke"
        profile = rehearsal.ROOT / "sim/profiles/capacity-smoke.json"
        if deadline is not None:
            values = json.loads(profile.read_text())
            if deadline == "no_overlap":
                values["max_live_calls"] = 1
            else:
                values["job_timeout_seconds"] = deadline
            profile = tmp_path / "timeout-profile.json"
            profile.write_text(json.dumps(values))
        result = subprocess.run(
            [
                sys.executable,
                str(Path(rehearsal.__file__)),
                "--profile",
                str(profile),
                "--out",
                str(output),
            ],
            capture_output=True,
            text=True,
            env={**os.environ, "SEDIMENT_DATABASE_URL": postgres_admin_url},
            timeout=240,
        )
        report = json.loads((output / "report.json").read_text())
        assert report["scratch_database_name"] not in databases()
        if deadline is not None:
            assert result.returncode == 1
            assert report["status"] == "failed"
            assert report["reason"] == (
                "no_ingest_overlap" if deadline == "no_overlap" else "job_timeout"
            )
            with pytest.raises(ProcessLookupError):
                os.kill(report["jobs"][0]["pid"], 0)
            if deadline == "no_overlap":
                receipts = [
                    json.loads(line)
                    for path in (output / "receipts").glob("*.jsonl")
                    if path.name != "exact.jsonl"
                    for line in path.read_text().splitlines()
                ]
                assert len(receipts) == 26
                assert sum(row["stored"] for row in receipts) == 25
                assert all(
                    set(row)
                    == {
                        "fact_id",
                        "stored",
                        "session_id",
                        "model_call_id",
                        "started",
                        "finished",
                        "wire_bytes",
                    }
                    for row in receipts
                )
            return
        assert result.returncode == 0, report
        assert report["status"] == "passed"
        assert report["receipt_conservation"] is True
        assert report["fixed_bundle_determinism"] is True
        assert report["push_probe"]["observation_id"]
        assert report["push_probe"]["redelivery_stored"] is False
        assert report["live_receipts"] > 0
        assert report["exact_retrieval"]["success"]["count"] > 0
        assert report["exact_retrieval"]["refusal"]["count"] == 0
        assert any(job.get("concurrent_exact_reads", 0) > 0 for job in report["jobs"])
        assert report["receipt_files"]["historical.jsonl"]["rows"] == 25
        assert report["receipt_files"]["live.jsonl"]["rows"] == report["live_receipts"]
        assert report["resources"]["samples"] > 0
        assert report["resources"]["postgresql_memory_bytes"] is None
        assert all(
            job["concurrent_stored_receipts"] > 0
            for job in report["jobs"]
            if job["name"].startswith("mixed-") or "http_status" in job
        )
    finally:
        engine.dispose()
