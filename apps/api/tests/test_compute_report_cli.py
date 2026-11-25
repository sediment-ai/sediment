# SPDX-License-Identifier: AGPL-3.0-or-later
"""Operator rendering for the compute report, over a real Fact store."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sediment_api.reports import compute_report as cli
from sediment_core import (
    ComputeAllocation,
    ComputeAllocationSource,
    GatewayProvider,
    InferenceCall,
)

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _seed(store, *, captured_at: datetime | None = None) -> None:
    for index, (cpu, work_s) in enumerate(
        [(2000, 60), (2000, 70), (2000, 80), (8000, 20), (8000, 30), (8000, 40)]
    ):
        session_id = f"session-{index}"
        store.store_compute_allocation(
            ComputeAllocation(
                org_id="acme",
                session_id=session_id,
                allocation_source=ComputeAllocationSource.RANDOMIZED,
                cpu_millicores=cpu,
                memory_bytes=None,
                occurred_at=T0 - timedelta(minutes=1),
                captured_at=captured_at or T0,
            )
        )
        for call, end_s in enumerate((5, 10 + work_s)):
            store.store_inference_call(
                InferenceCall(
                    inference_call_id=f"{session_id}-{call}",
                    org_id="acme",
                    session_id=session_id,
                    gateway_provider=GatewayProvider.LITELLM,
                    input_messages=[],
                    output_messages=[],
                    duration_ms=5_000,
                    observed_at=T0 + timedelta(seconds=end_s),
                )
            )
    store.store_compute_allocation(
        ComputeAllocation(
            org_id="acme",
            session_id="never-called",
            allocation_source=ComputeAllocationSource.AGENT_REQUEST,
            cpu_millicores=16000,
            occurred_at=T0,
            captured_at=captured_at or T0,
        )
    )


def test_compute_report_table_names_arms_comparisons_and_skips(
    postgres_store, postgres_database_url, capsys
) -> None:
    _seed(postgres_store)

    assert cli.main(["--org", "acme", "--database-url", postgres_database_url]) == 0

    output = capsys.readouterr().out
    assert "org: acme  window: all history" in output
    assert "Sessions with a Compute allocation: 7" in output
    assert "timed Sessions: 6" in output
    assert "no_inference_calls=1" in output
    assert "randomized Sessions reassigned to another source: 0" in output
    assert "randomized cpu=2000m memory=unknown: n=3" in output
    assert "between_calls_ms median=70.0s" in output
    assert (
        "randomized cpu=2000m memory=unknown -> randomized cpu=8000m "
        "memory=unknown (randomized)"
    ) in output
    assert "    elapsed_ms: -40.0s [95% CI" in output
    assert "small-n" in output
    assert "policy_version=1 quarantine_revision=0" in output


def test_compute_report_json_and_rows_out(
    postgres_store, postgres_database_url, tmp_path, capsys
) -> None:
    _seed(postgres_store)
    destination = tmp_path / "sessions.jsonl"

    assert (
        cli.main(
            [
                "--org",
                "acme",
                "--database-url",
                postgres_database_url,
                "--json",
                "--rows-out",
                str(destination),
            ]
        )
        == 0
    )

    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["timed_sessions"] == 6
    assert report["skipped"]["no_inference_calls"] == 1
    assert report["randomized_reassigned_sessions"] == 0
    assert report["comparisons"][0]["randomized"] is True
    assert "wrote 6 compute timing rows" in output.err
    rows = [json.loads(line) for line in destination.read_text().splitlines()]
    assert [row["session_id"] for row in rows] == [f"session-{i}" for i in range(6)]
    assert rows[0]["elapsed_ms"] == 70_000


def test_compute_report_since_days_windows_and_rejects_non_positive(
    postgres_store, postgres_database_url, monkeypatch, capsys
) -> None:
    _seed(postgres_store, captured_at=T0)
    assert (
        cli.main(
            [
                "--org",
                "acme",
                "--database-url",
                postgres_database_url,
                "--since-days",
                "0",
            ]
        )
        == 2
    )
    assert "positive integer" in capsys.readouterr().err

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return empty_now if tz is None else empty_now.astimezone(tz)

    empty_now = T0 + timedelta(days=30)
    monkeypatch.setattr(cli, "datetime", FrozenDatetime)
    assert (
        cli.main(
            [
                "--org",
                "acme",
                "--database-url",
                postgres_database_url,
                "--since-days",
                "7",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "org: acme  window: last 7 day(s)" in output
    assert "Sessions with a Compute allocation: 0" in output
    assert "arms: no data" in output

    class InsideDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return inside_now if tz is None else inside_now.astimezone(tz)

    inside_now = T0 + timedelta(days=3)
    monkeypatch.setattr(cli, "datetime", InsideDatetime)
    assert (
        cli.main(
            [
                "--org",
                "acme",
                "--database-url",
                postgres_database_url,
                "--since-days",
                "7",
                "--json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["sessions_with_allocation"] == 7
    assert report["timed_sessions"] == 6
