# SPDX-License-Identifier: AGPL-3.0-or-later
"""Synthetic capacity profiles retain complete histories and replay identities."""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest


SIM = Path(__file__).resolve().parents[1]
ANCHOR = datetime(2026, 9, 13, 12, tzinfo=UTC)
PROFILE = {
    "schema_version": 1,
    "developers": 2,
    "history_weeks": 2,
    "sessions_per_week": 2,
    "calls_per_session": 3,
    "history_bytes": 127,
    "output_bytes": 79,
    "live_interval_ms": 20,
    "max_live_calls": 100,
    "job_timeout_seconds": 120,
    "max_process_rss_mib": 2048,
    "max_workspace_mib": 4096,
}


def _module():
    name = "sediment_test_capacity_workload"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, SIM / "capacity_workload.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _load(tmp_path, values=None):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(PROFILE if values is None else values))
    return _module().load_profile(path)


def test_profile_totals_count_sessions_across_all_developers(tmp_path):
    profile = _load(tmp_path)
    assert profile.total_sessions == 4
    assert profile.total_calls == 12
    with pytest.raises(FrozenInstanceError):
        profile.developers = 7


@pytest.mark.parametrize("field", PROFILE)
@pytest.mark.parametrize("value", [True, False, 1.0, "1", None, [], {}])
def test_profile_requires_integer_fields(tmp_path, field, value):
    with pytest.raises(ValueError, match=field):
        _load(tmp_path, {**PROFILE, field: value})


@pytest.mark.parametrize("field", PROFILE)
def test_profile_rejects_missing_fields(tmp_path, field):
    values = dict(PROFILE)
    del values[field]
    with pytest.raises(ValueError, match="missing"):
        _load(tmp_path, values)


@pytest.mark.parametrize("value", [[], None, "profile", 42])
def test_profile_requires_an_object(tmp_path, value):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="object"):
        _module().load_profile(path)


def test_profile_rejects_unknown_fields(tmp_path):
    with pytest.raises(ValueError, match="unknown"):
        _load(tmp_path, {**PROFILE, "line_changes": 200000})


def test_profile_rejects_duplicate_fields(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(PROFILE)[:-1] + ', "developers": 3}')
    with pytest.raises(ValueError, match="duplicate"):
        _module().load_profile(path)


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", 2),
        ("developers", 0),
        ("developers", 1001),
        ("history_weeks", 0),
        ("history_weeks", 27),
        ("sessions_per_week", 0),
        ("sessions_per_week", 100001),
        ("calls_per_session", 0),
        ("calls_per_session", 1001),
        ("history_bytes", 0),
        ("history_bytes", 1048577),
        ("output_bytes", 0),
        ("output_bytes", 1048577),
        ("live_interval_ms", 0),
        ("live_interval_ms", 60001),
        ("max_live_calls", 0),
        ("max_live_calls", 100001),
        ("job_timeout_seconds", 0),
        ("job_timeout_seconds", 3601),
        ("max_process_rss_mib", 0),
        ("max_process_rss_mib", 65537),
        ("max_workspace_mib", 0),
        ("max_workspace_mib", 1048577),
    ],
)
def test_profile_rejects_values_outside_generator_limits(tmp_path, field, value):
    with pytest.raises(ValueError, match=field):
        _load(tmp_path, {**PROFILE, field: value})


def test_profile_checks_bounds_when_constructed_directly():
    with pytest.raises(ValueError, match="history_weeks"):
        _module().CapacityProfile(**{**PROFILE, "history_weeks": False})


def test_profile_allows_population_above_runtime_limit_for_refusal_rehearsals(tmp_path):
    profile = _load(
        tmp_path,
        {
            **PROFILE,
            "history_weeks": 24,
            "sessions_per_week": 1000,
            "calls_per_session": 10,
        },
    )
    assert profile.total_calls == 240000
    with pytest.raises(ValueError, match="total_calls"):
        _load(tmp_path, {**PROFILE, "history_weeks": 26, "sessions_per_week": 4000})


def test_profile_caps_cumulative_history_in_each_session(tmp_path):
    with pytest.raises(ValueError, match="Session"):
        _load(
            tmp_path,
            {
                **PROFILE,
                "calls_per_session": 16,
                "history_bytes": 1048576,
                "output_bytes": 1048576,
            },
        )


def test_profile_requires_a_session_for_each_declared_developer(tmp_path):
    with pytest.raises(ValueError, match="developers"):
        _load(tmp_path, {**PROFILE, "developers": 5})


@pytest.mark.parametrize("content", ["{", "{" * 20000, " " * 65537])
def test_profile_rejects_malformed_or_oversized_documents(tmp_path, content):
    path = tmp_path / "profile.json"
    path.write_text(content)
    with pytest.raises(ValueError, match="profile"):
        _module().load_profile(path)


def test_gateway_retains_all_prior_turns_and_exact_content_bytes(tmp_path):
    profile = _load(tmp_path)
    prior_messages = []
    fixture = (
        SIM.parent
        / "packages/capture/tests/fixtures/litellm_standard_logging_object.json"
    )
    original_fixture = fixture.read_bytes()
    for index in range(profile.calls_per_session):
        envelope = _module().gateway_envelope(profile, index, observed_at=ANCHOR)
        payload = envelope["payload"]
        user = payload["messages"][-1]
        assistant = payload["response"]["choices"][0]["message"]
        assert payload["messages"] == [*prior_messages, user]
        assert user["role"] == "user"
        assert len(user["content"].encode("ascii")) == profile.history_bytes
        assert len(assistant["content"].encode("ascii")) == profile.output_bytes
        prior_messages.extend(
            [user, {"role": "assistant", "content": assistant["content"]}]
        )
    assert fixture.read_bytes() == original_fixture
    next_session = _module().gateway_envelope(profile, 3, observed_at=ANCHOR)
    assert len(next_session["payload"]["messages"]) == 1


def test_gateway_identities_are_deterministic_unique_and_disjoint(tmp_path):
    profile = _load(tmp_path)
    envelopes = [
        _module().gateway_envelope(profile, index, observed_at=ANCHOR, live=live)
        for live in (False, True)
        for index in range(profile.total_calls)
    ]
    assert len({item["capture"]["id"] for item in envelopes}) == len(envelopes)
    assert len({item["payload"]["litellm_call_id"] for item in envelopes}) == len(
        envelopes
    )
    assert len({item["session_id"] for item in envelopes}) == profile.total_sessions * 2
    assert len({item["user_id"] for item in envelopes}) == profile.developers
    for item in envelopes:
        payload = item["payload"]
        assert UUID(item["capture"]["id"]).version == 5
        assert payload["id"] == payload["response"]["id"]
        assert payload["trace_id"] == item["session_id"]
        metadata = payload["metadata"]["requester_metadata"]
        assert metadata["session_id"] == item["session_id"]
        assert metadata["user_id"] == item["user_id"]
        assert payload["end_user"] == item["user_id"]
        assert "org_id" not in metadata
    assert envelopes[0] == _module().gateway_envelope(profile, 0, observed_at=ANCHOR)
    envelopes[0]["payload"]["messages"][0]["content"] = "mutation"
    assert _module().gateway_envelope(profile, 0, observed_at=ANCHOR) != envelopes[0]


def test_synthetic_bytes_do_not_inherit_fixture_token_or_cost_measurements(tmp_path):
    payload = _module().gateway_envelope(_load(tmp_path), 0, observed_at=ANCHOR)[
        "payload"
    ]
    assert "prompt_tokens" not in payload
    assert "completion_tokens" not in payload
    assert "total_tokens" not in payload
    assert "usage" not in payload["response"]
    assert "usage_object" not in payload["metadata"]
    assert "response_cost" not in payload
    assert "cost_breakdown" not in payload


@pytest.mark.parametrize("index", [-1, True, 1.5, "1", 12])
def test_gateway_rejects_invalid_historical_indices(tmp_path, index):
    with pytest.raises(ValueError, match="index"):
        _module().gateway_envelope(_load(tmp_path), index, observed_at=ANCHOR)


def test_gateway_enforces_separate_live_bound(tmp_path):
    profile = _load(tmp_path)
    assert _module().gateway_envelope(profile, 99, observed_at=ANCHOR, live=True)
    with pytest.raises(ValueError, match="index"):
        _module().gateway_envelope(profile, 100, observed_at=ANCHOR, live=True)
    with pytest.raises(ValueError, match="live"):
        _module().gateway_envelope(profile, 0, observed_at=ANCHOR, live=1)


def test_timestamps_cover_history_window_without_replacing_capture_time(tmp_path):
    module = _module()
    profile = _load(tmp_path)
    timestamps = [
        module.historical_observed_at(profile, index, ANCHOR)
        for index in range(profile.total_calls)
    ]
    assert timestamps[0] == ANCHOR - timedelta(weeks=profile.history_weeks)
    assert timestamps == sorted(set(timestamps))
    assert timestamps[-1] < ANCHOR
    assert sum(timestamp < ANCHOR - timedelta(weeks=1) for timestamp in timestamps) == 6
    offset_anchor = ANCHOR.astimezone(timezone(timedelta(hours=2)))
    assert module.historical_observed_at(profile, 0, offset_anchor) == timestamps[0]
    envelope = module.gateway_envelope(profile, 0, observed_at=offset_anchor)
    assert envelope["capture"]["observed_at"] == ANCHOR.isoformat()


@pytest.mark.parametrize("value", [datetime(2026, 9, 13), "2026-09-13", None])
def test_timestamps_require_aware_datetimes(tmp_path, value):
    module = _module()
    profile = _load(tmp_path)
    with pytest.raises(ValueError, match="observed_at"):
        module.gateway_envelope(profile, 0, observed_at=value)
    with pytest.raises(ValueError, match="anchor"):
        module.historical_observed_at(profile, 0, value)


@pytest.mark.parametrize("name", ["capacity-smoke", "capacity-pilot"])
def test_committed_profiles_declare_smoke_and_approved_pilot_populations(name):
    profile = _module().load_profile(SIM / "profiles" / f"{name}.json")
    if name == "capacity-smoke":
        assert profile.total_calls == 24
    else:
        assert profile.sessions_per_week == 100
        assert profile.calls_per_session == 100
        assert profile.history_weeks == 24
        assert profile.total_sessions == 2400
        assert profile.total_calls == 240000


def test_real_gateway_preserves_generated_history_and_replay_receipts(
    tmp_path, postgres_database_url, monkeypatch
):
    from fastapi.testclient import TestClient
    from pydantic import SecretStr

    monkeypatch.setenv("SEDIMENT_ORG_ID", "capacity-test")
    monkeypatch.setenv("SEDIMENT_DATABASE_URL", postgres_database_url)
    monkeypatch.setenv("SEDIMENT_DEV_MODE", "true")
    from sediment_api.config import settings

    monkeypatch.setattr(settings, "database_url", SecretStr(postgres_database_url))
    monkeypatch.setattr(settings, "dev_mode", True)
    monkeypatch.setattr(settings, "org_id", "capacity-test")
    monkeypatch.setattr(settings, "api_bearer_token", "capacity-test-ingest")
    from sediment_api.main import app

    module = _module()
    profile = _load(tmp_path)
    receipts = {}
    with TestClient(app) as client:
        for index in [2, 0, 1, 3]:
            envelope = module.gateway_envelope(
                profile,
                index,
                observed_at=module.historical_observed_at(profile, index, ANCHOR),
            )
            response = client.post(
                "/ingest/gateway",
                json=envelope,
                headers={"Authorization": "Bearer capacity-test-ingest"},
            )
            assert response.status_code == 200
            receipts[index] = response.json()
            assert receipts[index]["stored"] is True
            replay = client.post(
                "/ingest/gateway",
                json=envelope,
                headers={"Authorization": "Bearer capacity-test-ingest"},
            )
            assert replay.json() == {**receipts[index], "stored": False}
        calls = app.state.fact_store.read_inference_calls("capacity-test")
        assert {call.inference_call_id for call in calls} == {
            receipt["fact_id"] for receipt in receipts.values()
        }
        assert len(calls) == 4
        assert len(app.state.fact_store.read_sessions("capacity-test")) == 2
        assert [len(call.input_messages) for call in calls] == [1, 3, 5, 1]
        for index, call in enumerate(calls):
            assert call.observed_at == module.historical_observed_at(
                profile, index, ANCHOR
            )
