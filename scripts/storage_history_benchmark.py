# SPDX-License-Identifier: AGPL-3.0-or-later
"""Calibrate repeated-history storage in owned, disposable PostgreSQL databases.

Set SEDIMENT_TEST_DATABASE_URL. Run each entropy/compression combination separately.
Results describe the declared synthetic Session, not customer capacity. No Fact
layout changes or retained data rewrites occur. Native clients are required to
verify a compressed logical backup by restoring it into another owned database.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
import platform
from pathlib import Path
import resource
import shutil
import subprocess
import sys
import time
from uuid import NAMESPACE_URL, uuid5

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.release_rehearsal import scratch_database  # noqa: E402

ORG = "storage-benchmark"
SESSION = "history-session"
START = datetime(2026, 1, 1, tzinfo=UTC)
COLUMNS = ("input_messages", "output_messages", "raw")


class BenchmarkFailure(RuntimeError):
    """Content-free failure suitable for the command's result report."""


@dataclass(frozen=True)
class Workload:
    checkpoints: tuple[int, ...] = (1, 10, 100, 250)
    input_bytes: int = 8192
    output_bytes: int = 2048
    entropy: str = "varied"
    seed: int = 17

    def __post_init__(self):
        if (
            not self.checkpoints
            or any(type(n) is not int or not 1 <= n <= 250 for n in self.checkpoints)
            or tuple(sorted(set(self.checkpoints))) != self.checkpoints
        ):
            raise ValueError("checkpoints must increase within 1..250")
        for n in (self.input_bytes, self.output_bytes):
            if type(n) is not int or not 1 <= n <= 16384:
                raise ValueError("message sizes must be within 1..16384 bytes")
        if self.entropy not in {"repeated", "varied"} or type(self.seed) is not int:
            raise ValueError("invalid entropy or seed")


def body(profile, turn, role, size):
    if profile.entropy == "repeated":
        label = "synthetic storage text "
        return (label * ((size + len(label) - 1) // len(label)))[:size]
    # Each turn differs; later calls repeat the exact prior message. Base85 has
    # a wider alphabet than hexadecimal, avoiding an optimistic entropy control.
    data = hashlib.shake_256(f"{profile.seed}:{turn}:{role}".encode()).digest(size)
    return base64.b85encode(data).decode("ascii")[:size]


def fixture_calls(profile):
    from sediment_capture.gateway import LiteLLMAdapter

    messages = []
    for index in range(profile.checkpoints[-1]):
        identifier = f"history-call-{index:06d}"
        messages.append(
            {
                "role": "user",
                "content": body(profile, index, "user", profile.input_bytes),
            }
        )
        output = {
            "role": "assistant",
            "content": body(profile, index, "assistant", profile.output_bytes),
        }
        payload = {
            "litellm_call_id": identifier,
            "model": "synthetic-model",
            "messages": list(messages),
            "response": {"choices": [{"message": output, "finish_reason": "stop"}]},
        }
        yield LiteLLMAdapter().normalize(
            payload,
            session_id=SESSION,
            user_id=None,
            org_id=ORG,
            capture_id=uuid5(NAMESPACE_URL, identifier),
            observed_at=START + timedelta(seconds=index),
        )
        messages.append(output)


def canonical_bytes(fact):
    return json.dumps(
        fact.model_dump(mode="python"),
        ensure_ascii=True,
        allow_nan=True,
        sort_keys=True,
        separators=(",", ":"),
        default=lambda value: value.isoformat(),
    ).encode("ascii")


def store_calls(store, calls):
    from sediment_core.redaction import redact_fact

    digest = hashlib.sha256()
    for call in calls:
        if not store.store_inference_call_receipt(call).stored:
            raise BenchmarkFailure("unexpected_redelivery")
        retained, _ = redact_fact(call)
        digest.update(canonical_bytes(retained) + b"\n")
    return digest.hexdigest()


def storage_sizes(engine):
    from sqlalchemy import text

    with engine.connect() as connection:
        logical, datum, algorithms = {}, {}, {}
        for column in COLUMNS:
            row = connection.execute(
                text(
                    f"SELECT coalesce(sum(octet_length({column})),0), "
                    f"coalesce(sum(pg_column_size({column})),0) "
                    "FROM inference_calls WHERE org_id=:org AND session_id=:session"
                ),
                {"org": ORG, "session": SESSION},
            ).one()
            logical[column], datum[column] = map(int, row)
            algorithms[column] = {
                str(method or "uncompressed"): count
                for method, count in connection.execute(
                    text(
                        f"SELECT pg_column_compression({column}), count(*) "
                        "FROM inference_calls WHERE org_id=:org AND session_id=:session "
                        "GROUP BY 1"
                    ),
                    {"org": ORG, "session": SESSION},
                )
            }
        relations = {}
        for row in connection.exec_driver_sql(
            "SELECT c.relname, pg_relation_size(c.oid), "
            "pg_relation_size(c.oid,'fsm'), pg_relation_size(c.oid,'vm'), "
            "pg_table_size(c.oid), pg_indexes_size(c.oid), "
            "CASE WHEN c.reltoastrelid=0 THEN 0 ELSE pg_relation_size(c.reltoastrelid) END, "
            "CASE WHEN c.reltoastrelid=0 THEN 0 ELSE pg_indexes_size(c.reltoastrelid) END, "
            "CASE WHEN c.reltoastrelid=0 THEN 0 ELSE pg_total_relation_size(c.reltoastrelid) END, "
            "pg_total_relation_size(c.oid) "
            "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='public' AND c.relkind='r' ORDER BY c.relname"
        ):
            relations[row[0]] = dict(
                zip(
                    (
                        "heap_main_bytes",
                        "heap_fsm_bytes",
                        "heap_vm_bytes",
                        "table_with_toast_bytes",
                        "parent_indexes_bytes",
                        "toast_main_bytes",
                        "toast_indexes_bytes",
                        "toast_total_bytes",
                        "total_bytes",
                    ),
                    map(int, row[1:]),
                    strict=True,
                )
            )
        return {
            "logical_bytes": logical,
            "compressed_datum_bytes": datum,
            "compression_algorithms": algorithms,
            "relations": relations,
            "database_bytes": connection.exec_driver_sql(
                "SELECT pg_database_size(current_database())"
            ).scalar_one(),
        }


def read_costs(store, last, expected):
    from sediment_core import EvidenceReference
    from sediment_core.evidence import EvidenceReadError
    from sediment_core.store import OperationalReportLimitExceeded

    count = int(last.model_call_id.rsplit("-", 1)[1]) + 1
    # Capture IDs map through the adapter; retain only bounded scalar identities.
    ids = {
        str(row.inference_call_id)
        for row in store.read_inference_call_summaries(ORG)
        if row.session_id == SESSION
    }
    if len(ids) != count:
        raise BenchmarkFailure("call_count_mismatch")
    result = {}
    started = time.perf_counter()
    digest = hashlib.sha256()
    encoded_bytes = 0
    for fact in store.iter_inference_calls_by_ids(ORG, ids):
        encoded = canonical_bytes(fact)
        digest.update(encoded + b"\n")
        encoded_bytes += len(encoded)
    if digest.hexdigest() != expected:
        raise BenchmarkFailure("lossless_read_mismatch")
    result["streamed_facts"] = {
        "status": "success",
        "seconds": time.perf_counter() - started,
        "calls": count,
        "canonical_bytes": encoded_bytes,
        "sha256": expected,
    }
    started = time.perf_counter()
    exact = store.read_evidence_parts(
        ORG, SESSION, [EvidenceReference(str(last.inference_call_id), "output", 0, 0)]
    )
    if len(exact.items) != 1 or exact.items[0].part != last.output_messages[0].parts[0]:
        raise BenchmarkFailure("exact_output_mismatch")
    result["exact_output"] = {
        "status": "success",
        "seconds": time.perf_counter() - started,
    }
    for name, operation in (
        ("context_source", lambda: store.read_context_source(ORG, SESSION)),
        ("full_session", lambda: store.read_session_inference_calls(ORG, SESSION)),
    ):
        started = time.perf_counter()
        try:
            value = operation()
            observed = (
                value.visible_inference_calls
                if name == "context_source"
                else len(value)
            )
            if observed != count:
                raise BenchmarkFailure("incomplete_session_read")
            if name == "context_source" and len(value.items) != count * (count + 1):
                raise BenchmarkFailure("incomplete_context_parts")
            record = {"status": "success", "calls": observed}
            del value
        except EvidenceReadError as error:
            if error.detail["reason"] not in {
                "evidence_source_limit",
                "retrieval_part_limit",
            }:
                raise
            record = {"status": "capacity_refusal", "reason": error.detail["reason"]}
        except OperationalReportLimitExceeded:
            record = {"status": "capacity_refusal", "reason": "session_content_limit"}
        record["seconds"] = time.perf_counter() - started
        result[name] = record
    return result


def checkpoint(engine, store, last, expected):
    sample = storage_sizes(engine)
    sample["calls"] = int(last.model_call_id.rsplit("-", 1)[1]) + 1
    sample["reads"] = read_costs(store, last, expected)
    return sample


def semantic_controls(store):
    from sediment_core import (
        FactTable,
        GatewayProvider,
        InferenceCall,
        InferenceMessage,
        TextPart,
        ToolCallResponsePart,
    )
    from sediment_core.redaction import redact_fact

    call = InferenceCall(
        inference_call_id="control-a",
        org_id=ORG,
        session_id="control-session",
        gateway_provider=GatewayProvider.LITELLM,
        model_call_id="control-a",
        observed_at=START,
        input_messages=[
            InferenceMessage(role="user", parts=[TextPart(content="zero\0lone\ud800")])
        ],
        output_messages=[
            InferenceMessage(
                role="tool",
                parts=[
                    ToolCallResponsePart(
                        id="control-tool",
                        result={
                            "huge": 2**100,
                            "nan": float("nan"),
                            "positive": float("inf"),
                            "negative": -float("inf"),
                        },
                    )
                ],
            )
        ],
        raw={"unrepresented": "raw\0\udfff", "huge": 2**100, "nan": float("nan")},
    )
    retained, _ = redact_fact(call)
    store.store_inference_call_receipt(call)
    duplicate = store.store_inference_call_receipt(call)
    if duplicate.stored or duplicate.fact_id != call.inference_call_id:
        raise BenchmarkFailure("redelivery_control")
    other = call.model_copy(
        update={"inference_call_id": "control-b", "model_call_id": "control-b"}
    )
    store.store_inference_call_receipt(other)
    facts = store.read_inference_calls_by_ids(ORG, {"control-a", "control-b"})
    if {str(f.inference_call_id) for f in facts} != {"control-a", "control-b"}:
        raise BenchmarkFailure("distinct_fact_control")
    if canonical_bytes(
        next(f for f in facts if f.inference_call_id == "control-a")
    ) != canonical_bytes(retained):
        raise BenchmarkFailure("exceptional_value_control")
    store.quarantine_fact(
        ORG, FactTable.INFERENCE_CALLS, "control-a", reason="synthetic control"
    )
    visible = store.read_inference_calls_by_ids(ORG, {"control-a", "control-b"})
    if [str(f.inference_call_id) for f in visible] != ["control-b"]:
        raise BenchmarkFailure("quarantine_control")
    # A replay must not release the quarantined Fact.
    store.store_inference_call_receipt(call)
    if store.read_inference_calls_by_ids(ORG, {"control-a"}):
        raise BenchmarkFailure("quarantine_replay_control")
    store.release_fact(
        ORG, FactTable.INFERENCE_CALLS, "control-a", reason="synthetic control"
    )
    if len(store.read_inference_calls_by_ids(ORG, {"control-a", "control-b"})) != 2:
        raise BenchmarkFailure("release_control")
    return {
        "lossless": True,
        "redelivery": True,
        "distinct_facts": True,
        "quarantine": True,
    }


def native(command, env, timeout):
    started = time.perf_counter()
    try:
        subprocess.run(
            command,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        raise BenchmarkFailure("native_command_failed") from None
    return time.perf_counter() - started


def pg_environment(database_url):
    from sqlalchemy.engine import make_url

    url = make_url(database_url)
    if url.query:
        raise BenchmarkFailure("native_client_requires_url_without_query_options")
    return {
        **{key: value for key, value in os.environ.items() if not key.startswith("PG")},
        "PGHOST": url.host or "localhost",
        "PGPORT": str(url.port or 5432),
        "PGUSER": url.username or "",
        "PGPASSWORD": url.password or "",
        "PGDATABASE": url.database or "",
        "PGCONNECT_TIMEOUT": "10",
    }


def run(profile, output, admin_url, *, compression, pg_dump, pg_restore, timeout):
    from sediment_core import FactStore
    from sediment_core.postgres_migrations import upgrade_database, inspect_revision
    from sediment_core.redaction import redact_fact
    from sqlalchemy import create_engine

    if compression not in {"default", "pglz", "lz4"} or not 1 <= timeout <= 3600:
        raise ValueError("invalid compression or timeout")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    old_mask = os.umask(0o077)
    report = {
        "schema_version": 1,
        "status": "running",
        "synthetic": True,
        "workload": asdict(profile),
        "compression_requested": compression,
        "qualification": "Single-Session storage calibration; not pilot capacity",
        "measurement_limits": [
            "Read timings follow insertion and storage scans; caches are not cold.",
            "Datum sizes exclude tuple/page/index overhead; relation totals include TOAST.",
            "Database sizes exclude WAL, mirrors, training exports and external backups.",
            "Capacity refusals are reported separately from successful reads.",
            "Peak RSS covers this Python process only, across the whole run.",
            "A verified logical dump is removed after restore; its size is not live disk use.",
        ],
        "checkpoints": [],
        "python_version": platform.python_version(),
        "platform": platform.platform(),
    }
    archive = output / "history.dump"
    try:
        report["source_revision"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, timeout=10
        ).strip()
        report["source_dirty"] = bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=ROOT, text=True, timeout=10
            )
        )
        with scratch_database(admin_url) as source_url:
            source_env = pg_environment(source_url)
            upgrade_database(source_url)
            settings = f"-c statement_timeout={timeout * 1000} -c lock_timeout=5000"
            if compression != "default":
                settings += f" -c default_toast_compression={compression}"
            options = {"options": settings, "connect_timeout": 10}
            engine = create_engine(source_url, connect_args=options)
            try:
                store = FactStore(engine)
                with engine.connect() as connection:
                    report["postgresql_version"] = connection.exec_driver_sql(
                        "SHOW server_version"
                    ).scalar_one()
                    report["default_toast_compression"] = connection.exec_driver_sql(
                        "SHOW default_toast_compression"
                    ).scalar_one()
                report["baseline"] = storage_sizes(engine)
                digest = hashlib.sha256()
                started = time.perf_counter()
                for index, call in enumerate(fixture_calls(profile), 1):
                    if time.perf_counter() - started > timeout:
                        raise BenchmarkFailure("seed_deadline")
                    if not store.store_inference_call_receipt(call).stored:
                        raise BenchmarkFailure("unexpected_redelivery")
                    retained, _ = redact_fact(call)
                    digest.update(canonical_bytes(retained) + b"\n")
                    if index in profile.checkpoints:
                        with engine.begin() as connection:
                            connection.exec_driver_sql("ANALYZE inference_calls")
                        sample = checkpoint(engine, store, call, digest.hexdigest())
                        sample["database_growth_bytes"] = (
                            sample["database_bytes"]
                            - report["baseline"]["database_bytes"]
                        )
                        sample["application_relation_growth_bytes"] = sum(
                            row["total_bytes"] for row in sample["relations"].values()
                        ) - sum(
                            row["total_bytes"]
                            for row in report["baseline"]["relations"].values()
                        )
                        sample["once_per_turn_text_bytes"] = index * (
                            profile.input_bytes + profile.output_bytes
                        )
                        sample["replayed_text_bytes"] = (
                            index
                            * (index - 1)
                            // 2
                            * (profile.input_bytes + profile.output_bytes)
                        )
                        report["checkpoints"].append(sample)
                report["seed_and_checkpoint_seconds"] = time.perf_counter() - started
                report["controls"] = semantic_controls(store)
                store.quarantine_fact(
                    ORG, "inference_calls", "control-a", reason="backup control"
                )
                quarantine_revision = store.quarantine_revision(ORG)
                report["backup"] = {
                    "quarantine_revision": quarantine_revision,
                    "compression": "gzip:6",
                    "source_sha256": digest.hexdigest(),
                    "dump_seconds": native(
                        [
                            pg_dump,
                            "--format=custom",
                            "--compress=gzip:6",
                            "--no-owner",
                            "--no-privileges",
                            "--file",
                            str(archive),
                        ],
                        source_env,
                        timeout,
                    ),
                    "bytes": archive.stat().st_size,
                }
                with scratch_database(admin_url) as restore_url:
                    backup = report["backup"]
                    backup["restore_seconds"] = native(
                        [
                            pg_restore,
                            "--exit-on-error",
                            "--no-owner",
                            "--no-privileges",
                            "--dbname",
                            pg_environment(restore_url)["PGDATABASE"],
                            str(archive),
                        ],
                        pg_environment(restore_url),
                        timeout,
                    )
                    if inspect_revision(restore_url).state.value != "at_head":
                        raise BenchmarkFailure("restored_revision")
                    restored_engine = create_engine(
                        restore_url,
                        connect_args={
                            "options": f"-c statement_timeout={timeout * 1000} -c lock_timeout=5000",
                            "connect_timeout": 10,
                        },
                    )
                    try:
                        restored = FactStore(restored_engine)
                        visible = restored.read_inference_calls_by_ids(
                            ORG, {"control-a", "control-b"}
                        )
                        if restored.quarantine_revision(ORG) != quarantine_revision or [
                            str(f.inference_call_id) for f in visible
                        ] != ["control-b"]:
                            raise BenchmarkFailure("restored_quarantine_mismatch")
                        backup["quarantine_preserved"] = True
                        restored.release_fact(
                            ORG,
                            "inference_calls",
                            "control-a",
                            reason="restore control",
                        )
                        backup["restored_sha256"] = read_costs(
                            restored, call, digest.hexdigest()
                        )["streamed_facts"]["sha256"]
                        backup["restore_controls"] = semantic_controls(restored)
                    finally:
                        restored_engine.dispose()
            finally:
                engine.dispose()
        report["scratch_databases_removed"] = True
        report["status"] = "passed"
    except (Exception, KeyboardInterrupt) as error:
        report["status"] = "failed"
        report["failure"] = (
            str(error) if isinstance(error, BenchmarkFailure) else type(error).__name__
        )
        raise BenchmarkFailure(report["failure"]) from None
    finally:
        archive.unlink(missing_ok=True)
        maximum = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        report["python_peak_rss_bytes"] = (
            maximum if sys.platform == "darwin" else maximum * 1024
        )
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        os.umask(old_mask)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--checkpoints", default="1,10,100,250")
    parser.add_argument("--input-bytes", type=int, default=8192)
    parser.add_argument("--output-bytes", type=int, default=2048)
    parser.add_argument("--entropy", choices=("varied", "repeated"), default="varied")
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument(
        "--compression", choices=("default", "pglz", "lz4"), default="default"
    )
    parser.add_argument("--pg-dump", default="pg_dump")
    parser.add_argument("--pg-restore", default="pg_restore")
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    try:
        profile = Workload(
            tuple(map(int, args.checkpoints.split(","))),
            args.input_bytes,
            args.output_bytes,
            args.entropy,
            args.seed,
        )
        clients = [shutil.which(value) for value in (args.pg_dump, args.pg_restore)]
        if not all(clients):
            raise BenchmarkFailure("native_postgres_clients_unavailable")
        run(
            profile,
            args.out,
            os.environ["SEDIMENT_TEST_DATABASE_URL"],
            compression=args.compression,
            pg_dump=clients[0],
            pg_restore=clients[1],
            timeout=args.timeout,
        )
    except (KeyError, ValueError, OSError, BenchmarkFailure) as error:
        reason = (
            str(error) if isinstance(error, BenchmarkFailure) else type(error).__name__
        )
        print(f"storage calibration failed: {reason}", file=sys.stderr)
        return 1
    print(f"storage calibration passed: {args.out / 'report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
