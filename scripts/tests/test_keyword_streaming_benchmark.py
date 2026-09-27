# SPDX-License-Identifier: AGPL-3.0-or-later
"""Qualification fixtures and exact response checks cannot hide refusals."""

import json

import httpx
import pytest

from scripts import keyword_streaming_benchmark as benchmark


def test_growing_fixture_has_exact_repeated_history_and_deterministic_ids():
    profile = benchmark.Profile(calls=3)
    calls = list(benchmark.fixture_calls(profile))
    assert calls == list(benchmark.fixture_calls(profile))
    assert calls[2].input_messages[:3] == calls[1].input_messages
    assert calls[2].input_messages[3].parts == calls[1].output_messages[0].parts
    assert calls[0].input_messages[0] != calls[1].input_messages[-1]
    assert len(calls[-1].input_messages[-1].parts[0].content.encode()) == 8192
    assert len(calls[-1].output_messages[0].parts[0].content.encode()) == 2048
    assert (
        calls[-1].raw["messages"][0]["content"]
        == calls[0].input_messages[0].parts[0].content
    )


def test_approved_profile_has_10100_parts_and_493_mib_text():
    parts = text_bytes = count = 0
    for call in benchmark.fixture_calls(benchmark.Profile()):
        count += 1
        for message in call.input_messages + call.output_messages:
            parts += len(message.parts)
            text_bytes += sum(len(part.content.encode()) for part in message.parts)
    assert (count, parts, text_bytes) == (100, 10100, 51712000)


def test_eager_oracle_preserves_complete_counts_and_quarantine():
    profile = benchmark.Profile(calls=3)
    calls = list(benchmark.fixture_calls(profile))
    full = benchmark.oracle_packets(calls, profile.sessions_ids)
    assert full["fixed"] == full["selected"]
    selected = json.loads(full["selected"])
    assert selected["coverage"]["scanned_parts"] == 12
    assert selected["coverage"]["complete_visible_scan"] is True
    assert sum(selected["skipped"].values()) + len(selected["items"]) == 12
    hidden = benchmark.oracle_packets(
        calls,
        profile.sessions_ids,
        quarantined={calls[-1].inference_call_id},
        revision=1,
    )
    coverage = json.loads(hidden["selected"])["coverage"]
    assert coverage["scanned_parts"] == 6
    assert coverage["visible_inference_calls"] == 2
    assert coverage["quarantined_inference_calls"] == 1
    assert json.loads(hidden["reference"])["quarantine_revision"] == 1


def test_aggregate_grant_oracle_is_complete():
    profile = benchmark.Profile(calls=3, sessions=2, entropy="repeated")
    result = benchmark.oracle_packets(
        list(benchmark.fixture_calls(profile)), profile.sessions_ids
    )
    assert "fixed" not in result
    discovery = json.loads(result["discover"])
    assert discovery["coverage"]["authorized_sessions"] == 2
    assert discovery["coverage"]["matched_parts"] == 24
    assert discovery["coverage"]["found_sessions"] == 2
    assert discovery["items"][0]["session_id"] == profile.sessions_ids[0]
    assert discovery["skipped"]["response_budget"] == 1


def test_refusal_is_distinct_from_an_eager_oracle_success():
    good = httpx.Response(200, content=b'{"complete":true}')
    benchmark.check_response(good, b'{"complete":true}', None)
    with pytest.raises(RuntimeError, match="oracle_mismatch"):
        benchmark.check_response(
            httpx.Response(200, content=b'{"complete":false}'), good.content, None
        )
    refused = httpx.Response(
        409,
        json={"detail": {"reason": "retrieval_state_limit", "limit_bytes": 33554432}},
    )
    benchmark.check_response(refused, None, "retrieval_state_limit")
    with pytest.raises(RuntimeError, match="unexpected_refusal"):
        benchmark.check_response(refused, good.content, None)
    with pytest.raises(RuntimeError, match="expected_refusal"):
        benchmark.check_response(good, good.content, "retrieval_state_limit")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"calls": 0},
        {"calls": 151},
        {"sessions": 33},
        {"calls": 100, "sessions": 11},
        {"entropy": "unknown"},
    ],
)
def test_invalid_workload_refuses_before_resources(kwargs):
    with pytest.raises(ValueError):
        benchmark.Profile(**kwargs)


def test_state_control_has_small_parts_and_large_shared_role():
    call = next(benchmark.fixture_calls(benchmark.Profile(scenario="state-limit")))
    assert len(call.input_messages[0].role) == 7 * 1024**2
    assert len(call.input_messages[0].parts) == 3
    assert len(call.output_messages[0].parts[0].content) < 100
    assert (
        benchmark.expected_reason("state-limit", "selected") == "retrieval_state_limit"
    )
    assert benchmark.expected_reason("state-limit", "discover") is None
    assert benchmark.expected_reason("part-limit", "discover") == "retrieval_part_limit"
    assert benchmark.expected_reason("row-limit", "selected") == "evidence_source_limit"


def test_control_part_builder_stays_below_declared_row_target():
    message = benchmark.tiny_part_message(4096)
    assert len(message.parts) > 50
    assert len(json.dumps(message.model_dump()).encode()) < 4096


def test_existing_output_is_never_overwritten(tmp_path):
    output = tmp_path / "existing"
    output.mkdir()
    with pytest.raises(FileExistsError):
        benchmark.run(benchmark.Profile(calls=2), output, "must-not-connect")


def test_paired_wave_requires_overlapping_completed_requests():
    rows = [
        {
            "clients": 2,
            "route": "/query/context",
            "wave": 0,
            "started": 1.0,
            "finished": 2.0,
        },
        {
            "clients": 2,
            "route": "/query/context",
            "wave": 0,
            "started": 1.5,
            "finished": 2.5,
        },
    ]
    assert benchmark.wave_overlaps(rows) == [True]
    rows[1]["started"] = 2.1
    assert benchmark.wave_overlaps(rows) == [False]
