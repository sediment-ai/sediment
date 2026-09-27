# SPDX-License-Identifier: AGPL-3.0-or-later
"""Qualify bounded keyword retrieval against complete synthetic eager oracles.

Requires an owned SEDIMENT_TEST_DATABASE_URL. Each invocation owns one scratch
database. The 768 MiB sampled API-tree threshold is a planning acceptance for
the declared positive workload, not a universal decoded-memory guarantee.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import threading
import time
from uuid import NAMESPACE_URL, uuid5

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import agent_evidence_benchmark as live  # noqa: E402
from scripts import storage_history_benchmark as storage  # noqa: E402
from scripts.release_rehearsal import scratch_database  # noqa: E402

ORG = "keyword-benchmark"
QUERY = "compatibility duplicate assertion"
MIB = 1024**2
SCENARIOS = (
    "growing",
    "source-limit",
    "row-limit",
    "part-limit",
    "state-limit",
    "tiny-parts",
    "nested-tools",
)
ROUTES = {
    "fixed": "/query/context",
    "selected": "/query/context/selected",
    "discover": "/query/context/discover",
    "reference": "/query/context/evidence/read",
}


@dataclass(frozen=True)
class Profile:
    calls: int = 100
    sessions: int = 1
    entropy: str = "varied"
    scenario: str = "growing"
    seed: int = 17

    def __post_init__(self):
        if (
            type(self.calls) is not int
            or not 1 <= self.calls <= 150
            or type(self.sessions) is not int
            or not 1 <= self.sessions <= 32
            or self.calls * self.sessions > 1000
            or self.entropy not in {"varied", "repeated"}
            or self.scenario not in SCENARIOS
            or type(self.seed) is not int
            or (self.scenario != "growing" and self.sessions != 1)
        ):
            raise ValueError("invalid bounded benchmark profile")

    @property
    def sessions_ids(self):
        return tuple(f"keyword-{n:02d}" for n in range(self.sessions))


def tiny_part_message(target_bytes):
    from sediment_core import InferenceMessage, TextPart

    part = TextPart(content=QUERY)
    per_part = len(json.dumps(part.model_dump()).encode()) + 2
    # Leave room for the message envelope and surrounding source list.
    count = max(1, (target_bytes - 256) // per_part)
    return InferenceMessage(
        role="user", parts=[part.model_copy() for _ in range(count)]
    )


def fixture_calls(profile):
    from sediment_capture.gateway import LiteLLMAdapter
    from sediment_core import (
        GatewayProvider,
        InferenceCall,
        InferenceMessage,
        TextPart,
        ToolCallResponsePart,
    )

    if profile.scenario not in {"growing", "source-limit"}:
        small = InferenceMessage(role="assistant", parts=[TextPart(content=QUERY)])
        match profile.scenario:
            case "row-limit":
                source = InferenceMessage(
                    role="user", parts=[TextPart(content=QUERY + "x" * (8 * MIB))]
                )
            case "part-limit":
                source = InferenceMessage(
                    role="user", parts=[TextPart(content=QUERY) for _ in range(16385)]
                )
            case "tiny-parts":
                source = tiny_part_message(8 * MIB - 4096)
            case "state-limit":
                source = InferenceMessage(
                    role="m" * (7 * MIB),
                    parts=[TextPart(content=f"{QUERY} {n}") for n in range(3)],
                )
            case "nested-tools":
                tokens = " ".join(f"token_{n:07d}" for n in range(600000))
                value = {"text": QUERY + " " + tokens[: 7 * MIB]}
                for _ in range(12):
                    value = {"nested": [value]}
                source = InferenceMessage(
                    role="tool", parts=[ToolCallResponsePart(id="nested", result=value)]
                )
        yield InferenceCall(
            inference_call_id="control-call",
            org_id=ORG,
            session_id=profile.sessions_ids[0],
            gateway_provider=GatewayProvider.LITELLM,
            observed_at=storage.START,
            input_messages=[source],
            output_messages=[small],
            raw={"synthetic": True},
        )
        return
    count = 120 if profile.scenario == "source-limit" else profile.calls
    shape = storage.Workload((count,), 8192, 2048, profile.entropy, profile.seed)
    for session in profile.sessions_ids:
        history = []
        for turn in range(count):
            identifier = f"{session}-{turn:03d}"

            def body(role, size):
                prefix = QUERY + " "
                return prefix + storage.body(shape, turn, role, size)[len(prefix) :]

            history.append({"role": "user", "content": body("user", 8192)})
            output = {"role": "assistant", "content": body("assistant", 2048)}
            yield LiteLLMAdapter().normalize(
                {
                    "litellm_call_id": identifier,
                    "model": "synthetic-model",
                    "messages": list(history),
                    "response": {
                        "choices": [{"message": output, "finish_reason": "stop"}]
                    },
                },
                session_id=session,
                user_id=None,
                org_id=ORG,
                capture_id=uuid5(NAMESPACE_URL, identifier),
                observed_at=storage.START + timedelta(seconds=turn),
            )
            history.append(output)


def oracle_packets(calls, sessions, *, quarantined=frozenset(), revision=0, modes=None):
    """Project known retained Facts directly; never relax a production read cap."""
    from sediment_core import (
        ContextDiscoverySession,
        ContextDiscoverySource,
        EvidenceContextSource,
        EvidenceRead,
        EvidenceReadItem,
        EvidenceReference,
    )
    from sediment_core.evidence import encode_evidence_json
    from sediment_derive.context_retrieval import discover_context, retrieve_context

    modes = set(modes or ROUTES)
    groups = {session: [] for session in sessions}
    visible, hidden = dict.fromkeys(sessions, 0), dict.fromkeys(sessions, 0)
    for call in calls:
        if call.session_id not in groups:
            continue
        if call.inference_call_id in quarantined:
            hidden[call.session_id] += 1
            continue
        visible[call.session_id] += 1
        if not modes & {"fixed", "selected", "discover"}:
            continue
        for side in ("input", "output"):
            for mi, message in enumerate(getattr(call, f"{side}_messages")):
                for pi, part in enumerate(message.parts):
                    groups[call.session_id].append(
                        EvidenceReadItem(
                            EvidenceReference(call.inference_call_id, side, mi, pi),
                            call.observed_at,
                            message.role,
                            message.finish_reason,
                            part,
                        )
                    )
    result = {}
    if modes & {"fixed", "selected"}:
        selected = retrieve_context(
            EvidenceContextSource(
                session_id=sessions[0],
                quarantine_revision=revision,
                visible_inference_calls=visible[sessions[0]],
                quarantined_inference_calls=hidden[sessions[0]],
                items=tuple(groups[sessions[0]]),
            ),
            QUERY,
        )
        result["selected"] = encode_evidence_json(
            selected, type(selected), max_bytes=16384
        )
        if len(sessions) == 1:
            result["fixed"] = result["selected"]
    if "discover" in modes:
        discovery = discover_context(
            ContextDiscoverySource(
                authorized_sessions=len(sessions),
                quarantine_revision=revision,
                visible_inference_calls=sum(visible.values()),
                quarantined_inference_calls=sum(hidden.values()),
                sessions=tuple(
                    ContextDiscoverySession(s, tuple(groups[s]), None) for s in sessions
                ),
                commit=None,
            ),
            QUERY,
        )
        result["discover"] = encode_evidence_json(
            discovery, type(discovery), max_bytes=16384
        )
    if "reference" in modes:
        call = next(
            call
            for call in calls
            if call.session_id == sessions[0]
            and call.inference_call_id not in quarantined
        )
        message = call.output_messages[0]
        reference = EvidenceReadItem(
            EvidenceReference(call.inference_call_id, "output", 0, 0),
            call.observed_at,
            message.role,
            message.finish_reason,
            message.parts[0],
        )
        exact = EvidenceRead(
            session_id=sessions[0],
            quarantine_revision=revision,
            items=(reference,),
            schema_version=1,
        )
        result["reference"] = encode_evidence_json(exact, type(exact))
    return result


def expected_reason(scenario, mode):
    if mode == "reference":
        return None
    if scenario in {"source-limit", "row-limit"}:
        return "evidence_source_limit"
    if scenario in {"part-limit", "tiny-parts"}:
        return "retrieval_part_limit"
    if scenario == "state-limit" and mode != "discover":
        return "retrieval_state_limit"
    return None


def check_response(response, oracle, reason):
    if reason is not None:
        value = response.json()
        if (
            response.status_code != 409
            or set(value) != {"detail"}
            or value.get("detail", {}).get("reason") != reason
        ):
            raise RuntimeError("expected_refusal_not_observed")
        if reason == "retrieval_state_limit" and value["detail"] != {
            "reason": reason,
            "limit_bytes": 32 * MIB,
        }:
            raise RuntimeError("partial_state_refusal")
    elif response.status_code != 200:
        raise RuntimeError("unexpected_refusal")
    elif response.content != oracle:
        raise RuntimeError("oracle_mismatch")


def source_receipt(engine, calls):
    from sqlalchemy import text

    fingerprint = hashlib.sha256()
    for call in calls:
        fingerprint.update(storage.canonical_bytes(call) + b"\n")
    with engine.begin() as connection:
        connection.exec_driver_sql("ANALYZE")
        row = connection.execute(
            text(
                "SELECT count(*), sum(octet_length(input_messages)), sum(octet_length(output_messages)), "
                "sum(octet_length(raw)), max(octet_length(input_messages)+octet_length(output_messages)), "
                "sum(octet_length(inference_call_id)+octet_length(session_id)+"
                "coalesce(octet_length(model),0)+coalesce(octet_length(model_provider),0)) "
                "FROM inference_calls WHERE org_id=:org"
            ),
            {"org": ORG},
        ).one()
        settings = dict(
            connection.exec_driver_sql(
                "SELECT name, setting FROM pg_settings WHERE name IN "
                "('server_version','shared_buffers','work_mem','max_connections','default_toast_compression')"
            ).all()
        )
        physical = int(
            connection.exec_driver_sql(
                "SELECT pg_total_relation_size('inference_calls')"
            ).scalar_one()
        )
    return {
        "retained_fixture_sha256": fingerprint.hexdigest(),
        "stored": dict(
            zip(
                (
                    "calls",
                    "input_bytes",
                    "output_bytes",
                    "raw_bytes",
                    "maximum_message_row_bytes",
                    "identity_metadata_bytes",
                ),
                map(int, row),
                strict=True,
            )
        ),
        "part_occurrences": sum(
            len(m.parts) for c in calls for m in c.input_messages + c.output_messages
        ),
        "inference_relation_bytes_including_indexes_toast": physical,
        "database_settings": settings,
        "scope": "SQL byte totals are exact stored columns before probes; keyword transfer also includes projected metadata. Raw is measured but must not enter selection.",
    }


def wave_overlaps(reads):
    groups = {}
    for read in reads:
        if read["clients"] == 2:
            groups.setdefault((read["route"], read["wave"]), []).append(read)
    return [
        len(rows) == 2
        and max(r["started"] for r in rows) < min(r["finished"] for r in rows)
        for rows in groups.values()
    ]


def request(endpoint, token, mode, session, reference, oracle, reason, barrier=None):
    import httpx

    body = {"schema_version": 1}
    if mode == "reference":
        body.update(session_id=session, references=[reference])
    else:
        body.update(query=QUERY, max_bytes=16384)
        if mode == "selected":
            body["session_id"] = session
    with httpx.Client(
        base_url=endpoint,
        headers={"Authorization": f"Bearer {token}"},
        timeout=40,
        trust_env=False,
    ) as client:
        if barrier is not None:
            barrier.wait(timeout=15)
        started = time.perf_counter()
        response = client.post(ROUTES[mode], json=body)
        finished = time.perf_counter()
    check_response(response, oracle, reason)
    if response.headers.get("cache-control") != "no-store":
        raise RuntimeError("missing_no_store_response")
    value = response.json()
    return {
        "route": ROUTES[mode],
        "started": started,
        "finished": finished,
        "seconds": finished - started,
        "status": response.status_code,
        "bytes": len(response.content),
        "sha256": hashlib.sha256(response.content).hexdigest(),
        "reason": value.get("detail", {}).get("reason"),
        "coverage": value.get("coverage"),
        "skipped": value.get("skipped"),
        "result_status": value.get("status"),
        "oracle_equal": reason is None,
    }


def diagnostic(output, sessions):
    """Separate direct scans and plans; never substituted for public latency."""
    from sediment_core import FactStore
    from sediment_core.evidence import EvidenceReadError, encode_evidence_json
    from sediment_core.postgres_engine import create_postgres_engine
    from sediment_derive import context_retrieval as selectors
    from sqlalchemy import event

    engine = create_postgres_engine(
        os.environ["SEDIMENT_DATABASE_URL"], api_work=True, single_connection=True
    )
    queries, plans, scans, active = [], {}, {}, {}

    def before(connection, cursor, statement, parameters, context, many):
        active[id(context)] = time.perf_counter()

    def after(connection, cursor, statement, parameters, context, many):
        if statement.lstrip().startswith("SELECT"):
            if "inference_calls.raw" in statement:
                raise RuntimeError("keyword_source_selected_raw")
            digest = hashlib.sha256(statement.encode()).hexdigest()
            queries.append(
                {
                    "sql_sha256": digest,
                    "cursor_seconds": time.perf_counter() - active[id(context)],
                    "cursor_rows": cursor.rowcount,
                    "server_side_cursor": bool(getattr(cursor, "name", None)),
                    "yield_per": context.execution_options.get("yield_per"),
                }
            )
            if "inference_calls.input_messages" in statement:
                plans[digest] = (statement, parameters)

    event.listen(engine, "before_cursor_execute", before)
    event.listen(engine, "after_cursor_execute", after)
    try:
        for mode in ("selected", "discover"):
            started = time.perf_counter()
            try:
                with FactStore(engine).read_snapshot() as snapshot:
                    if hasattr(snapshot, "stream_context_source"):
                        scope = (
                            snapshot.stream_context_source(ORG, sessions[0])
                            if mode == "selected"
                            else snapshot.stream_context_discovery_source(ORG, sessions)
                        )
                        with scope as (metadata, items):
                            selector = (
                                selectors.retrieve_context_stream
                                if mode == "selected"
                                else selectors.discover_context_stream
                            )
                            result = selector(metadata, items, QUERY)
                            packet = encode_evidence_json(
                                result, type(result), max_bytes=16384
                            )
                    else:
                        source = (
                            snapshot.read_context_source(ORG, sessions[0])
                            if mode == "selected"
                            else snapshot.read_context_discovery_source(ORG, sessions)
                        )
                        selector = (
                            selectors.retrieve_context
                            if mode == "selected"
                            else selectors.discover_context
                        )
                        result = selector(source, QUERY)
                        packet = encode_evidence_json(
                            result, type(result), max_bytes=16384
                        )
                scans[mode] = {
                    "status": "success",
                    "sha256": hashlib.sha256(packet).hexdigest(),
                }
            except EvidenceReadError as error:
                scans[mode] = {"status": "refused", "detail": error.detail}
            scans[mode]["seconds"] = time.perf_counter() - started
        event.remove(engine, "before_cursor_execute", before)
        event.remove(engine, "after_cursor_execute", after)
        explained = {}
        with engine.connect() as connection:
            for digest, (statement, parameters) in plans.items():
                explained[digest] = connection.exec_driver_sql(
                    "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + statement, parameters
                ).scalar_one()
        output.write_bytes(
            live.encoded(
                {
                    "scans": scans,
                    "queries": queries,
                    "plans": explained,
                    "scope": "Fresh direct scan process, outside API measurements. Cursor time can exclude fetch/decoding; -1 rows means unknown. Plans re-execute SQL afterward and need not detoast projected fields.",
                }
            )
        )
    finally:
        engine.dispose()


def run(
    profile,
    output,
    admin_url,
    *,
    runtime=ROOT,
    samples=3,
    waves=2,
    quarantine=False,
    baseline_refusal=None,
    diagnostics=True,
    rss_limit_mib=768,
    database_cpu_limit=None,
    database_memory_mib=None,
):
    import httpx
    from sediment_core import FactStore, FactTable
    from sediment_core.postgres_engine import configure_libpq, create_postgres_engine
    from sediment_core.postgres_migrations import upgrade_database
    from sediment_core.redaction import redact_fact
    from sqlalchemy import text

    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    configure_libpq()
    report = {
        "profile": asdict(profile),
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "runtime": str(runtime),
        "revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=runtime, text=True
        ).strip(),
        "dirty": subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=runtime, text=True
        ).splitlines(),
        "platform": platform.platform(),
        "python": sys.version,
        "database_declared_limits": {
            "cpus": database_cpu_limit,
            "memory_mib": database_memory_mib,
            "verified_by_harness": False,
        },
        "rss_planning_acceptance_mib": rss_limit_mib,
        "phases": [],
        "qualification": "Synthetic declared workload; no model-quality, full-retention, or arbitrary fan-out qualification. Oracle runs outside measured workers. Caches are not reset; sample 0 includes lazy worker startup.",
    }

    def save():
        path = output / "report.json"
        path.write_bytes(live.encoded(report))
        path.chmod(0o600)

    try:
        with scratch_database(admin_url) as url:
            upgrade_database(url)
            engine = create_postgres_engine(url)
            try:
                store, calls = FactStore(engine), []
                for call in fixture_calls(profile):
                    if not store.store_inference_call(call):
                        raise RuntimeError("unexpected_redelivery")
                    calls.append(redact_fact(call)[0])
                report["source"] = source_receipt(engine, calls)
                environment = live.clean_environment(url, profile.sessions_ids)
                environment["SEDIMENT_ORG_ID"] = ORG
                if "DYLD_LIBRARY_PATH" in os.environ:
                    environment["DYLD_LIBRARY_PATH"] = os.environ["DYLD_LIBRARY_PATH"]
                if profile.sessions == 1:
                    environment.pop("SEDIMENT_RETRIEVAL_SESSION_IDS")
                    environment["SEDIMENT_RETRIEVAL_SESSION_ID"] = profile.sessions_ids[
                        0
                    ]
                modes = tuple(
                    mode for mode in ROUTES if mode != "fixed" or profile.sessions == 1
                )
                reasons = {
                    mode: (
                        baseline_refusal
                        if mode != "reference" and baseline_refusal
                        else expected_reason(profile.scenario, mode)
                    )
                    for mode in modes
                }
                # Even a baseline refusal keeps an independent complete positive oracle.
                oracle_modes = [
                    mode
                    for mode in modes
                    if expected_reason(profile.scenario, mode) is None
                ]
                phases = (
                    ("visible", "quarantined", "released")
                    if quarantine
                    else ("visible",)
                )
                if quarantine and (profile.scenario != "growing" or profile.calls < 2):
                    raise ValueError(
                        "Quarantine control requires growing history with at least two calls"
                    )
                for revision, phase in enumerate(phases):
                    excluded = set()
                    if phase == "quarantined":
                        store.quarantine_fact(
                            ORG,
                            FactTable.INFERENCE_CALLS,
                            calls[-1].inference_call_id,
                            reason="synthetic keyword control",
                        )
                        excluded.add(calls[-1].inference_call_id)
                    elif phase == "released":
                        store.release_fact(
                            ORG,
                            FactTable.INFERENCE_CALLS,
                            calls[-1].inference_call_id,
                            reason="synthetic keyword release",
                        )
                    packets = oracle_packets(
                        calls,
                        profile.sessions_ids,
                        quarantined=excluded,
                        revision=revision,
                        modes=oracle_modes,
                    )
                    reference = json.loads(packets["reference"])["items"][0][
                        "reference"
                    ]
                    phase_dir = output / phase
                    phase_dir.mkdir(mode=0o700)
                    writes, health, errors, reads = [], [], [], []
                    stop = threading.Event()
                    with live.api_server(
                        runtime / ".venv/bin/python", environment, phase_dir
                    ) as (endpoint, pid):
                        sampler = live.MemorySampler(pid)
                        sampler.thread.start()

                        def send():
                            try:
                                live.live_probe(
                                    endpoint,
                                    environment["SEDIMENT_API_BEARER_TOKEN"],
                                    stop,
                                    writes,
                                    health,
                                    prefix=phase,
                                    count=2000,
                                )
                            except Exception as error:
                                errors.append(type(error).__name__)

                        sender = threading.Thread(target=send, daemon=True)
                        try:
                            for clients, count in ((1, samples), (2, waves)):
                                if clients == 2:
                                    sender.start()
                                    deadline = time.monotonic() + 12
                                    while (
                                        not writes
                                        and not errors
                                        and time.monotonic() < deadline
                                    ):
                                        time.sleep(0.01)
                                    if not writes or errors:
                                        raise RuntimeError("capture_probe_start_failed")
                                with ThreadPoolExecutor(max_workers=clients) as pool:
                                    for mode in modes:
                                        for wave in range(count):
                                            barrier = threading.Barrier(clients)
                                            futures = [
                                                pool.submit(
                                                    request,
                                                    endpoint,
                                                    environment[
                                                        "SEDIMENT_RETRIEVAL_TOKEN"
                                                    ],
                                                    mode,
                                                    profile.sessions_ids[0],
                                                    reference,
                                                    packets.get(mode),
                                                    reasons[mode],
                                                    barrier,
                                                )
                                                for _ in range(clients)
                                            ]
                                            for future in futures:
                                                record = future.result()
                                                record["clients"] = clients
                                                record["wave"] = wave
                                                reads.append(record)
                        finally:
                            stop.set()
                            if sender.ident is not None:
                                sender.join(timeout=15)
                            memory = sampler.finish()
                        if sender.is_alive() or sampler.error or errors:
                            raise RuntimeError("probe_cleanup_or_sampling_failed")
                        if (
                            not writes
                            or not health
                            or any(r["status"] != 200 for r in writes + health)
                        ):
                            raise RuntimeError("capture_or_health_probe_failed")
                        admitted_windows = [
                            {**record, "status": 200}
                            for record in reads
                            if record["clients"] == 2
                        ]
                        admitted_overlap = live.overlapping_ingests(
                            writes, admitted_windows
                        )
                        if not admitted_overlap:
                            raise RuntimeError("acknowledged_capture_overlap_missing")
                        pairs = wave_overlaps(reads)
                        if not pairs or not all(pairs):
                            raise RuntimeError("paired_requests_did_not_overlap")
                        with httpx.Client(base_url=endpoint, trust_env=False) as client:
                            if client.get("/health").status_code != 200:
                                raise RuntimeError("final_health_failed")
                    with engine.connect() as connection:
                        stored_ids = set(
                            connection.execute(
                                text(
                                    "SELECT inference_call_id FROM inference_calls WHERE org_id=:org AND model_call_id LIKE :prefix"
                                ),
                                {"org": ORG, "prefix": f"{phase}-%"},
                            ).scalars()
                        )
                    if stored_ids != {r["fact_id"] for r in writes}:
                        raise RuntimeError("capture_receipt_mismatch")
                    planning_exceeded = (
                        profile.scenario == "growing"
                        and profile.calls == 100
                        and profile.sessions == 1
                        and baseline_refusal is None
                        and memory["peak_api_tree_rss_bytes"] > rss_limit_mib * MIB
                    )
                    report["phases"].append(
                        {
                            "phase": phase,
                            "oracle_sha256": {
                                mode: hashlib.sha256(packet).hexdigest()
                                for mode, packet in packets.items()
                            },
                            "reads": reads,
                            "overlapping_two_request_waves": len(pairs),
                            "memory": memory,
                            "rss_planning_acceptance_exceeded": planning_exceeded,
                            "summary": {
                                mode: live.summarize(
                                    [r for r in reads if r["route"] == route]
                                )
                                for mode, route in ROUTES.items()
                                if mode in modes
                            },
                            "ingest": {
                                "receipts": writes,
                                "summary": live.summarize(writes),
                                "verified_facts": len(stored_ids),
                                "fully_overlapping": live.overlapping_ingests(
                                    writes, reads
                                ),
                                "fully_overlapping_admitted_requests_including_expected_refusals": admitted_overlap,
                            },
                            "health": {
                                "requests": health,
                                "summary": live.summarize(health),
                            },
                        }
                    )
                    save()
                    if planning_exceeded:
                        raise RuntimeError("rss_planning_acceptance_exceeded")
                if diagnostics:
                    subprocess.run(
                        [
                            str(runtime / ".venv/bin/python"),
                            str(Path(__file__).resolve()),
                            "--diagnostic",
                            str(output / "diagnostic.json"),
                            "--sessions",
                            str(profile.sessions),
                        ],
                        env=environment,
                        cwd=output,
                        check=True,
                        timeout=180,
                    )
            finally:
                engine.dispose()
        report.update(status="passed", scratch_database_removed=True)
    except Exception as error:
        report.update(status="failed", error=type(error).__name__)
        raise
    finally:
        save()
    print(f"Keyword benchmark saved to {output / 'report.json'}")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--calls", type=int, default=100)
    parser.add_argument("--sessions", type=int, default=1)
    parser.add_argument("--entropy", choices=("varied", "repeated"), default="varied")
    parser.add_argument("--scenario", choices=SCENARIOS, default="growing")
    parser.add_argument("--samples", type=int, choices=range(1, 11), default=3)
    parser.add_argument("--waves", type=int, choices=range(1, 6), default=2)
    parser.add_argument("--quarantine", action="store_true")
    parser.add_argument(
        "--baseline-refusal", choices=("evidence_source_limit", "retrieval_part_limit")
    )
    parser.add_argument("--no-diagnostics", action="store_true")
    parser.add_argument("--rss-limit-mib", type=int, default=768)
    parser.add_argument("--database-cpu-limit", type=float)
    parser.add_argument("--database-memory-mib", type=int)
    parser.add_argument("--diagnostic", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    profile = Profile(args.calls, args.sessions, args.entropy, args.scenario)
    if args.diagnostic:
        diagnostic(args.diagnostic, profile.sessions_ids)
    elif args.output is None or not 128 <= args.rss_limit_mib <= 8192:
        parser.error("output and a planning RSS limit within 128..8192 MiB required")
    else:
        run(
            profile,
            args.output,
            os.environ["SEDIMENT_TEST_DATABASE_URL"],
            runtime=args.runtime,
            samples=args.samples,
            waves=args.waves,
            quarantine=args.quarantine,
            baseline_refusal=args.baseline_refusal,
            diagnostics=not args.no_diagnostics,
            rss_limit_mib=args.rss_limit_mib,
            database_cpu_limit=args.database_cpu_limit,
            database_memory_mib=args.database_memory_mib,
        )


if __name__ == "__main__":
    main()
