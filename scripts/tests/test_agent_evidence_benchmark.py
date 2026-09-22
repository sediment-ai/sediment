# SPDX-License-Identifier: AGPL-3.0-or-later
"""Benchmark accounting preserves refusals and exact synthetic evidence."""

import json
import threading
from types import SimpleNamespace

import pytest
import httpx

from scripts import agent_evidence_benchmark as benchmark


def test_percentiles_separate_fast_refusals_from_successes():
    records = [{"status": 200, "seconds": value} for value in (1.0, 2.0, 3.0, 4.0)] + [
        {"status": 503, "seconds": 0.001, "reason": "work capacity exceeded"}
    ]
    summary = benchmark.summarize(records)
    assert summary["success"]["p50_seconds"] == 2.0
    assert summary["success"]["p95_seconds"] == 4.0
    assert summary["refusal"]["p50_seconds"] == 0.001
    assert summary["statuses"] == {"200": 4, "503": 1}
    assert summary["reasons"] == {"work capacity exceeded": 1}
    assert benchmark.summarize([])["success"]["p95_seconds"] is None


def test_fixture_is_repeatable_and_contains_uncommitted_exact_evidence():
    calls = list(benchmark.fixture_calls(1))
    assert len(calls) == 10
    assert calls == list(benchmark.fixture_calls(1))
    assert (
        calls[0]
        .input_messages[0]
        .parts[0]
        .content.startswith("Compatibility requirement")
    )
    result = calls[0].output_messages[0].parts[0].result
    assert result["integer"] == 2**100
    assert "AssertionError" in result["failure"]
    assert calls[0].inference_call_id == benchmark.KNOWN_REFERENCE["inference_call_id"]
    assert calls[0].session_id == benchmark.SESSIONS[0]


@pytest.mark.parametrize("sessions,calls", [(1, 10), (8, 200), (32, 800)])
def test_profiles_remain_within_source_budgets(sessions, calls):
    values = list(benchmark.fixture_calls(sessions))
    assert len(values) == calls
    assert (
        sum(len(m.parts) for c in values for m in c.input_messages + c.output_messages)
        == 2 * calls
    )
    assert sum(len(c.model_dump_json()) for c in values) < 8 * 1024**2


def test_ingest_overlap_requires_a_verified_receipt_inside_a_success():
    reads = [{"started": 1.0, "finished": 2.0, "status": 200}]
    writes = [
        {"started": 1.1, "finished": 1.2, "stored": True},
        {"started": 0.9, "finished": 1.2, "stored": True},
        {"started": 1.2, "finished": 1.3, "stored": False},
    ]
    assert benchmark.overlapping_ingests(writes, reads) == 1


def test_exact_flow_uses_returned_candidate_and_reference(monkeypatch):
    client_type = httpx.Client
    sent = []
    reference = {**benchmark.KNOWN_REFERENCE, "inference_call_id": "returned-call"}
    part = {"type": "text", "content": "Exact returned source"}

    def handle(request):
        import json

        sent.append(json.loads(request.content))
        if request.url.path.endswith("discover"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "session_id": "returned-session",
                            "preview": {"reference": reference, "part": part},
                        }
                    ]
                },
            )
        return httpx.Response(
            200, json={"items": [{"reference": reference, "part": part}]}
        )

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: client_type(**kwargs, transport=httpx.MockTransport(handle)),
    )
    rows, completed = benchmark.flow(
        "http://benchmark.test", "synthetic-token", "exact"
    )
    assert completed["status"] == 200
    assert len(rows) == 2
    assert sent[1] == {
        "schema_version": 1,
        "session_id": "returned-session",
        "references": [reference],
    }


def test_refused_discovery_never_issues_selected_read(monkeypatch):
    client_type = httpx.Client
    sent = []

    def handle(request):
        sent.append(request.url.path)
        return httpx.Response(503, json={"detail": "work capacity exceeded"})

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: client_type(**kwargs, transport=httpx.MockTransport(handle)),
    )
    rows, completed = benchmark.flow(
        "http://benchmark.test", "synthetic-token", "keyword"
    )
    assert completed["status"] == 503
    assert len(rows) == 1
    assert sent == ["/query/context/discover"]


@pytest.mark.parametrize(
    "changed", [{"integer": float(2**100)}, {"integer": 2**100 + 1}]
)
def test_exact_validation_refuses_changed_or_float_rewritten_integer(changed):
    item = {"reference": benchmark.KNOWN_REFERENCE, "part": changed}
    with pytest.raises(RuntimeError, match="exact_selected_evidence_changed"):
        benchmark.verify_selected(
            {"items": [item]}, "known", benchmark.KNOWN_REFERENCE, {"integer": 2**100}
        )


def test_exact_validation_refuses_wrong_occurrence_even_when_content_matches():
    item = {
        "reference": {**benchmark.KNOWN_REFERENCE, "part_index": 1},
        "part": {"type": "text", "content": "same"},
    }
    with pytest.raises(RuntimeError, match="exact_selected_evidence_changed"):
        benchmark.verify_selected(
            {"items": [item]}, "exact", benchmark.KNOWN_REFERENCE, item["part"]
        )


@pytest.mark.parametrize(
    "detail,reason",
    [
        ("work capacity exceeded", "work capacity exceeded"),
        ({"reason": "work unavailable"}, "work unavailable"),
        (
            {"code": "database_unavailable", "message": "PostgreSQL unavailable"},
            "database_unavailable",
        ),
    ],
)
def test_live_probe_records_refusal_then_distinct_stored_attempt(
    monkeypatch, detail, reason
):
    client_type = httpx.Client
    sent = []

    def handle(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        sent.append(json.loads(request.content)["payload"]["litellm_call_id"])
        if len(sent) == 1:
            return httpx.Response(503, json={"detail": detail})
        return httpx.Response(200, json={"fact_id": "stored-fact", "stored": True})

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: client_type(**kwargs, transport=httpx.MockTransport(handle)),
    )
    writes, health = [], []
    benchmark.live_probe(
        "http://benchmark.test",
        "synthetic-token",
        threading.Event(),
        writes,
        health,
        prefix="probe",
        count=2,
    )
    assert sent == ["probe-000000", "probe-000001"]
    assert [write["status"] for write in writes] == [503, 200]
    assert "fact_id" not in writes[0] and "stored" not in writes[0]
    assert writes[1]["fact_id"] == "stored-fact" and writes[1]["stored"] is True
    summary = benchmark.summarize(writes)
    assert summary["refusal"]["count"] == summary["success"]["count"] == 1
    assert summary["reasons"] == {reason: 1}
    assert len(health) == 2 and all(row["status"] == 200 for row in health)


def test_failed_live_probe_reaches_later_real_http_tier_without_thread_restart(
    monkeypatch, tmp_path, postgres_admin_url
):
    original_probe = benchmark.live_probe

    def failed_live_probe(*args, **kwargs):
        if kwargs.get("prefix") == "control":
            return original_probe(*args, **kwargs)
        raise RuntimeError("injected_live_probe_failure")

    monkeypatch.setenv("SEDIMENT_TEST_DATABASE_URL", postgres_admin_url)
    monkeypatch.syspath_prepend(str(benchmark.ROOT / "scripts"))
    monkeypatch.setattr(benchmark, "live_probe", failed_live_probe)
    output = tmp_path / "failed-probe"
    args = SimpleNamespace(
        output=output,
        runtime=benchmark.ROOT,
        sessions=1,
        background=0,
        samples=1,
        waves=1,
        clients=[5, 10],
        modes=["known"],
        diagnostic_only=False,
    )
    with pytest.raises(RuntimeError, match="^probe_cleanup_or_sampling_failed$"):
        benchmark.run(args)
    report = json.loads((output / "report.json").read_text())
    assert [scenario["clients"] for scenario in report["scenarios"]] == [5, 10]
    assert all(
        scenario["flow_summary"]["success"]["count"] >= 1
        for scenario in report["scenarios"]
    )
