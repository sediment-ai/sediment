# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import gc
import hashlib
import logging
import multiprocessing
import resource
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, select

from sediment_core import GatewayProvider, InferenceCall, InferenceMessage, TextPart
from sediment_core import FactStore

_CORPUS_ROWS = 64
_RAW_BYTES = 1024 * 1024
_PROJECTED_LIMIT_MIB = 16.0
_AUDIT_CONTROL_MIN_MIB = 32.0
_T0 = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)

logger = logging.getLogger(__name__)


def _raw_payload(index: int) -> str:
    return hashlib.shake_256(f"sediment-559-{index}".encode()).hexdigest(
        _RAW_BYTES // 2
    )


def _call(index: int) -> InferenceCall:
    return InferenceCall(
        inference_call_id=f"inference-{index:03d}",
        org_id="acme",
        session_id=f"session-{index:03d}",
        gateway_provider=GatewayProvider.LITELLM,
        model_provider="anthropic",
        model="claude-sonnet",
        input_messages=[
            InferenceMessage(role="user", parts=[TextPart(content="Fix storage")])
        ],
        output_messages=[
            InferenceMessage(role="assistant", parts=[TextPart(content="Done")])
        ],
        model_call_id=f"model-call-{index:03d}",
        observed_at=_T0 + timedelta(seconds=index),
        raw={"payload": _raw_payload(index)},
    )


def _max_rss_mib() -> float:
    if sys.platform == "linux":
        # ru_maxrss can retain the spawning parent's high-water mark across
        # exec. VmHWM belongs to this process's address space and can be reset.
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                return float(line.split()[1]) / 1024
        raise AssertionError("Linux peak resident memory is unavailable")
    value = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value / (1024 * 1024 if sys.platform == "darwin" else 1024)


def _measure(database_url: str, mode: str, queue) -> None:
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(select(1)).scalar_one()
        gc.collect()
        if sys.platform == "linux":
            Path("/proc/self/clear_refs").write_text("5\n")
        baseline = _max_rss_mib()
        store = FactStore(engine)
        with store.read_snapshot() as snapshot:
            if mode == "summary":
                rows = snapshot.read_inference_call_summaries("acme")
            elif mode == "rollout":
                rows = snapshot.read_rollout_inference_calls("acme")
            else:
                rows = snapshot.read_inference_calls("acme")
        assert len(rows) == _CORPUS_ROWS
        if mode == "audit":
            assert sum(len(row.raw["payload"]) for row in rows) == (
                _CORPUS_ROWS * _RAW_BYTES
            )
        queue.put(("ok", _max_rss_mib() - baseline))
    except BaseException as exc:
        queue.put(("error", repr(exc)))
    finally:
        engine.dispose()


def _measured_growth(database_url: str, mode: str) -> float:
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    process = context.Process(target=_measure, args=(database_url, mode, queue))
    process.start()
    process.join(60)
    if process.is_alive():
        process.terminate()
        process.join()
        raise AssertionError(f"{mode} memory measurement timed out")
    status, value = queue.get(timeout=5)
    assert process.exitcode == 0
    assert status == "ok", value
    return float(value)


def test_postgres_projected_reads_bound_peak_memory(postgres_database_factory) -> None:
    database_url = postgres_database_factory()
    engine = create_engine(database_url)
    try:
        store = FactStore(engine)
        for index in range(_CORPUS_ROWS):
            assert store.store_inference_call(_call(index)) is True
    finally:
        engine.dispose()

    # A full-suite parent can peak above every reader before spawning it.
    # The audit control must still expose a full-row projection regression.
    prior_peak = b"x" * (_CORPUS_ROWS * _RAW_BYTES * 4)
    del prior_peak
    gc.collect()

    summary_growth = _measured_growth(database_url, "summary")
    rollout_growth = _measured_growth(database_url, "rollout")
    audit_growth = _measured_growth(database_url, "audit")

    logger.info(
        "postgres_projection_memory summary_growth_mib=%.2f "
        "rollout_growth_mib=%.2f audit_growth_mib=%.2f",
        summary_growth,
        rollout_growth,
        audit_growth,
    )

    assert summary_growth < _PROJECTED_LIMIT_MIB
    assert rollout_growth < _PROJECTED_LIMIT_MIB
    assert audit_growth > _AUDIT_CONTROL_MIN_MIB
