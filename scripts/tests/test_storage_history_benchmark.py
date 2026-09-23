# SPDX-License-Identifier: AGPL-3.0-or-later
"""Storage calibration measures lossless Facts without claiming target capacity."""

import json
import shutil

import pytest

from scripts import storage_history_benchmark as benchmark


def test_workload_preserves_exact_history_and_varies_distinct_turns():
    profile = benchmark.Workload((1, 3), 512, 128, "varied", 17)
    calls = list(benchmark.fixture_calls(profile))
    assert len(calls) == 3
    assert benchmark.canonical_bytes(calls[2]) == benchmark.canonical_bytes(
        list(benchmark.fixture_calls(profile))[2]
    )
    assert calls[2].input_messages[:3] == calls[1].input_messages
    assert calls[2].input_messages[3].parts == calls[1].output_messages[0].parts
    assert calls[0].input_messages[0] != calls[1].input_messages[-1]
    assert calls[2].raw["messages"][0]["content"] == (
        calls[0].input_messages[0].parts[0].content
    )


@pytest.mark.parametrize("checkpoints", [(), (0,), (251,), (2, 1), (1, 1), (True,)])
def test_invalid_population_refuses_before_resources(checkpoints):
    with pytest.raises(ValueError):
        benchmark.Workload(checkpoints, 512, 128, "varied", 17)


def test_checkpoint_records_real_storage_and_separate_refusals(
    postgres_engine, postgres_store, monkeypatch
):
    import sediment_core.store as store_module

    profile = benchmark.Workload((1, 3), 512, 128, "varied", 17)
    calls = list(benchmark.fixture_calls(profile))
    expected = benchmark.store_calls(postgres_store, calls)
    sample = benchmark.checkpoint(postgres_engine, postgres_store, calls[-1], expected)
    assert sample["calls"] == 3
    assert sample["logical_bytes"]["raw"] > 0
    assert sample["relations"]["inference_calls"]["total_bytes"] > 0
    assert sample["reads"]["exact_output"]["status"] == "success"
    assert sample["reads"]["streamed_facts"]["sha256"] == expected
    monkeypatch.setattr(store_module, "INFERENCE_SESSION_BYTES_LIMIT", 1)
    limited = benchmark.read_costs(postgres_store, calls[-1], expected)
    assert limited["full_session"]["status"] == "capacity_refusal"
    assert limited["exact_output"]["status"] == "success"


def test_large_part_population_refuses_context_but_exact_output_survives(
    postgres_engine, postgres_store
):
    profile = benchmark.Workload((46,), 8, 4, "varied", 17)
    calls = list(benchmark.fixture_calls(profile))
    digest = benchmark.store_calls(postgres_store, calls)
    reads = benchmark.read_costs(postgres_store, calls[-1], digest)
    assert reads["context_source"]["status"] == "capacity_refusal"
    assert reads["context_source"]["reason"] == "retrieval_part_limit"
    assert reads["full_session"]["status"] == "success"
    assert reads["exact_output"]["status"] == "success"


def test_controls_preserve_distinct_facts_redelivery_and_quarantine(postgres_store):
    result = benchmark.semantic_controls(postgres_store)
    assert result == {
        "lossless": True,
        "redelivery": True,
        "distinct_facts": True,
        "quarantine": True,
    }


def test_read_costs_compare_the_retained_redacted_fact(postgres_store):
    from sediment_core import InferenceMessage, TextPart

    call = next(benchmark.fixture_calls(benchmark.Workload((1,), 32, 16)))
    call = call.model_copy(
        update={
            "output_messages": [
                InferenceMessage(
                    role="assistant", parts=[TextPart(content="sk-" + "x" * 48)]
                )
            ]
        }
    )
    expected = benchmark.store_calls(postgres_store, [call])
    assert (
        benchmark.read_costs(postgres_store, call, expected)["exact_output"]["status"]
        == "success"
    )


def test_native_backup_restore_is_verified_and_source_is_not_admin(
    tmp_path, postgres_admin_url
):
    pg_dump = shutil.which("pg_dump")
    pg_restore = shutil.which("pg_restore")
    if not pg_dump or not pg_restore:
        pytest.skip("native PostgreSQL dump and restore clients unavailable")
    report = benchmark.run(
        benchmark.Workload((1, 3), 256, 64, "varied", 17),
        tmp_path / "run",
        postgres_admin_url,
        compression="pglz",
        pg_dump=pg_dump,
        pg_restore=pg_restore,
        timeout=60,
    )
    assert report["status"] == "passed"
    assert report["backup"]["restored_sha256"] == report["backup"]["source_sha256"]
    assert report["backup"]["bytes"] > 0
    assert report["backup"]["restore_controls"]["quarantine"]
    assert report["backup"]["quarantine_preserved"]
    assert report["scratch_databases_removed"] is True
    assert not list((tmp_path / "run").glob("*.dump"))
    assert "postgresql" not in json.dumps(report).lower().replace(
        "postgresql_version", ""
    )
    assert (tmp_path / "run" / "report.json").stat().st_mode & 0o777 == 0o600


def test_native_failure_does_not_expose_credentials(tmp_path):
    executable = tmp_path / "fail"
    executable.write_text("#!/bin/sh\nprintf 'password-private' >&2\nexit 1\n")
    executable.chmod(0o700)
    with pytest.raises(benchmark.BenchmarkFailure) as error:
        benchmark.native([str(executable)], {}, 2)
    assert "password-private" not in str(error.value)


def test_existing_output_is_never_overwritten(tmp_path):
    output = tmp_path / "existing"
    output.mkdir()
    marker = output / "report.json"
    marker.write_text("keep")
    with pytest.raises(FileExistsError):
        benchmark.run(
            benchmark.Workload((1,), 32, 16, "varied", 17),
            output,
            "must-not-connect",
            compression="default",
            pg_dump="pg_dump",
            pg_restore="pg_restore",
            timeout=5,
        )
    assert marker.read_text() == "keep"


@pytest.mark.parametrize("entropy", ["repeated", "varied"])
def test_declared_body_size_is_exact(entropy):
    profile = benchmark.Workload((1,), 8192, 2048, entropy, 17)
    call = next(benchmark.fixture_calls(profile))
    assert len(call.input_messages[0].parts[0].content.encode()) == 8192
    assert len(call.output_messages[0].parts[0].content.encode()) == 2048


@pytest.mark.parametrize("compression", ["pglz", "lz4"])
def test_native_compression_is_measured(postgres_database_factory, compression):
    from sqlalchemy import create_engine
    from sediment_core import FactStore

    engine = create_engine(
        postgres_database_factory(),
        connect_args={"options": f"-c default_toast_compression={compression}"},
    )
    try:
        profile = benchmark.Workload((3,), 8192, 2048, "repeated", 17)
        benchmark.store_calls(FactStore(engine), benchmark.fixture_calls(profile))
        measured = benchmark.storage_sizes(engine)
        assert measured["compression_algorithms"]["input_messages"][compression] == 3
        assert (
            measured["compressed_datum_bytes"]["input_messages"]
            < measured["logical_bytes"]["input_messages"]
        )
        relation = measured["relations"]["inference_calls"]
        assert (
            relation["total_bytes"]
            == relation["table_with_toast_bytes"] + relation["parent_indexes_bytes"]
        )
    finally:
        engine.dispose()


def test_failed_backup_retains_content_free_report_and_cleans_owned_database(
    tmp_path, postgres_admin_url, monkeypatch
):
    from contextlib import contextmanager
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    original = benchmark.scratch_database
    owned = []

    @contextmanager
    def tracked(url):
        with original(url) as scratch_url:
            owned.append(make_url(scratch_url).database)
            yield scratch_url

    monkeypatch.setattr(benchmark, "scratch_database", tracked)
    output = tmp_path / "failed"
    with pytest.raises(benchmark.BenchmarkFailure, match="native_command_failed"):
        benchmark.run(
            benchmark.Workload((1,), 32, 16, "varied", 17),
            output,
            postgres_admin_url,
            compression="default",
            pg_dump="/nonexistent/storage-benchmark-pg-dump",
            pg_restore="/nonexistent/storage-benchmark-pg-restore",
            timeout=30,
        )
    report = json.loads((output / "report.json").read_text())
    assert report["status"] == "failed"
    assert report["failure"] == "native_command_failed"
    assert postgres_admin_url not in json.dumps(report)
    assert owned
    engine = create_engine(postgres_admin_url)
    try:
        with engine.connect() as connection:
            for name in owned:
                assert (
                    connection.execute(
                        text("SELECT count(*) FROM pg_database WHERE datname=:name"),
                        {"name": name},
                    ).scalar_one()
                    == 0
                )
    finally:
        engine.dispose()
