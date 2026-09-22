# SPDX-License-Identifier: AGPL-3.0-or-later
"""HTTP contract checks for bounded operational reports."""

from __future__ import annotations

from datetime import UTC, datetime
import sys

import pytest
from sediment_core import (
    FactTable,
    GatewayProvider,
    InferenceCall,
    InferenceMessage,
    TextPart,
)


def _inference_call(call_id: str, *, model: str = "claude-sonnet-5") -> InferenceCall:
    return InferenceCall(
        inference_call_id=call_id,
        org_id="testorg",
        session_id=f"session-{call_id}",
        user_id="agent:api",
        gateway_provider=GatewayProvider.LITELLM,
        model_provider="anthropic",
        model=model,
        input_messages=[InferenceMessage(role="user", parts=[TextPart(content="hi")])],
        output_messages=[
            InferenceMessage(role="assistant", parts=[TextPart(content="hello")])
        ],
        observed_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
    )


def _params() -> dict[str, str]:
    return {
        "cohort_start": "2026-08-07T12:00:00Z",
        "cohort_end": "2026-09-06T12:00:00Z",
        "as_of": "2026-09-06T12:00:00Z",
    }


def _worker_service(monkeypatch, service_name: str, source: str, extra: str = ""):
    """Run fault injection in a real child, at the service's actual process seam."""
    from sediment_api import workers

    code = (
        "from sediment_api import worker\n"
        "from sediment_api.routers import reports\n"
        "from sediment_api.services import operational_reports\n"
        + source
        + "\n"
        + f"operational_reports.{service_name} = replacement\n"
        + extra
        + "\nraise SystemExit(worker.main())\n"
    )
    monkeypatch.setattr(workers, "_WORKER_COMMAND", (sys.executable, "-c", code))


def test_empty_lifecycle_report_returns_versioned_scoped_envelope(
    client, monkeypatch, tmp_path
) -> None:
    from sediment_api.config import settings

    monkeypatch.setattr(settings, "mirror_path", str(tmp_path / "mirrors"))
    store = client.app.state.fact_store
    sessions_before = store.count_sessions("testorg")

    response = client.get(
        "/v1/reports/accepted-work-lifecycle",
        params=_params(),
        headers={"Authorization": "Bearer test-operator-token-3a7e-2f6c"},
    )

    assert response.status_code == 200
    assert "etag" not in response.headers
    payload = response.json()
    assert payload["schema_version"] == 1
    assert payload["scope"] == {
        "cohort_start": "2026-08-07T12:00:00Z",
        "cohort_end": "2026-09-06T12:00:00Z",
        "as_of": "2026-09-06T12:00:00Z",
        "max_inference_calls": 50_000,
    }
    assert payload["report"]["org_id"] == "testorg"
    assert payload["report"]["accepted_work"]["accepted_calls"] == 0
    assert payload["report"]["provenance"]["lifecycle"] == {
        "policy_version": "3",
        "quarantine_revision": 0,
        "policy_digest": None,
    }
    assert datetime.fromisoformat(payload["scope"]["as_of"]).tzinfo is UTC
    assert store.count_sessions("testorg") == sessions_before


def test_empty_model_report_returns_versioned_scoped_envelope(
    client, monkeypatch, tmp_path
) -> None:
    from sediment_api.config import settings

    monkeypatch.setattr(settings, "mirror_path", str(tmp_path / "mirrors"))

    first = client.get(
        "/v1/reports/model-outcomes",
        params=_params(),
        headers={"Authorization": "Bearer test-operator-token-3a7e-2f6c"},
    )
    second = client.get(
        "/v1/reports/model-outcomes",
        params=_params(),
        headers={"Authorization": "Bearer test-operator-token-3a7e-2f6c"},
    )

    assert first.status_code == 200
    assert second.content == first.content
    payload = first.json()
    assert payload["schema_version"] == 1
    assert payload["scope"]["max_inference_calls"] == 50_000
    assert payload["report"]["rows"] == []
    assert payload["report"]["fate_provenance"] == {
        "policy_version": "1",
        "quarantine_revision": 0,
        "policy_digest": None,
    }


def test_model_report_route_keeps_uncommitted_direct_accept(
    client, monkeypatch, tmp_path
):
    from sediment_core import (
        AgentHarness,
        DeveloperDecision,
        InteractionMode,
        ToolCallPart,
    )
    from sediment_api.config import settings

    monkeypatch.setattr(settings, "mirror_path", str(tmp_path / "mirrors"))
    call = _inference_call("direct").model_copy(
        update={
            "output_messages": [
                InferenceMessage(
                    role="assistant",
                    parts=[ToolCallPart(id="tool", name="Edit", arguments={})],
                )
            ]
        }
    )
    store = client.app.state.fact_store
    store.store_inference_call(call)
    store.store_decision(
        DeveloperDecision(
            org_id=call.org_id,
            session_id=call.session_id,
            call_id="tool",
            agent_harness=AgentHarness.CLAUDE_CODE,
            file_path="a.py",
            accepted=True,
            explicit=True,
            interaction_mode=InteractionMode.AGENT,
            occurred_at=call.observed_at,
            captured_at=call.observed_at,
        )
    )

    response = client.get(
        "/v1/reports/model-outcomes",
        params=_params(),
        headers={"Authorization": "Bearer test-operator-token-3a7e-2f6c"},
    )

    assert response.status_code == 200
    report = response.json()["report"]
    assert report["rows"][0]["explicit_accepts"] == 1
    assert report["rows"][0]["ci_linked"] == 0
    assert report["abandonment"]["negative_completions"] == 0
    assert report["abandonment"]["derivation_skipped"]["session_commit_unobserved"] == 1


@pytest.mark.parametrize(
    "params",
    [
        {},
        {
            "cohort_start": "2026-08-07T12:00:00",
            "cohort_end": "2026-09-06T12:00:00Z",
            "as_of": "2026-09-06T12:00:00Z",
        },
        {
            "cohort_start": "2026-09-06T12:00:00Z",
            "cohort_end": "2026-09-06T12:00:00Z",
            "as_of": "2026-09-06T12:00:00Z",
        },
        {
            "cohort_start": "2026-08-01T12:00:00Z",
            "cohort_end": "2026-09-06T12:00:00Z",
            "as_of": "2026-09-06T12:00:00Z",
        },
        {
            "cohort_start": "2026-08-07T12:00:00Z",
            "cohort_end": "2026-09-06T12:00:00Z",
            "as_of": "2026-09-05T12:00:00Z",
        },
    ],
)
def test_report_scope_rejects_absent_naive_or_invalid_bounds(client, params) -> None:
    response = client.get(
        "/v1/reports/model-outcomes",
        params=params,
        headers={"Authorization": "Bearer test-operator-token-3a7e-2f6c"},
    )

    assert response.status_code == 422


def test_report_scope_accepts_exactly_31_days(client, monkeypatch, tmp_path) -> None:
    from sediment_api.config import settings

    monkeypatch.setattr(settings, "mirror_path", str(tmp_path / "mirrors"))
    params = _params() | {"cohort_start": "2026-08-06T12:00:00Z"}

    response = client.get(
        "/v1/reports/model-outcomes",
        params=params,
        headers={"Authorization": "Bearer test-operator-token-3a7e-2f6c"},
    )

    assert response.status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "/v1/reports/model-outcomes",
        "/v1/reports/accepted-work-lifecycle",
    ],
)
def test_report_routes_require_bearer_auth(client, path) -> None:
    response = client.get(path, params=_params())

    assert response.status_code == 401


@pytest.mark.parametrize(
    ("path", "service_name"),
    [
        ("/v1/reports/model-outcomes", "model_report_payload"),
        (
            "/v1/reports/accepted-work-lifecycle",
            "generate_lifecycle_report",
        ),
    ],
)
def test_report_overflow_returns_client_safe_conflict(
    client, monkeypatch, path, service_name
) -> None:
    _worker_service(
        monkeypatch,
        service_name,
        "from sediment_core import OperationalReportLimitExceeded\n"
        "def replacement(*args, **kwargs):\n"
        "    raise OperationalReportLimitExceeded('composite filter exceeds 30000 keys')",
    )

    response = client.get(
        path,
        params=_params(),
        headers={"Authorization": "Bearer test-operator-token-3a7e-2f6c"},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "report evidence exceeds the fixed limit"}


def test_report_does_not_disguise_programming_value_error(client, monkeypatch) -> None:
    _worker_service(
        monkeypatch,
        "model_report_payload",
        "def replacement(*args, **kwargs):\n    raise ValueError('programming defect')",
    )
    response = client.get(
        "/v1/reports/model-outcomes",
        params=_params(),
        headers={"Authorization": "Bearer test-operator-token-3a7e-2f6c"},
    )
    assert response.status_code == 500
    assert response.json() == {"detail": "work failed"}


def test_report_awaits_child_work_to_completion(client, monkeypatch) -> None:
    import time

    _worker_service(
        monkeypatch,
        "model_report_payload",
        "import time\ndef replacement(*args, **kwargs):\n"
        "    time.sleep(0.05)\n    return {'rows': []}",
    )

    started = time.perf_counter()
    response = client.get(
        "/v1/reports/model-outcomes",
        params=_params(),
        headers={"Authorization": "Bearer test-operator-token-3a7e-2f6c"},
    )
    elapsed = time.perf_counter() - started

    assert response.status_code == 200
    assert elapsed >= 0.04


def test_model_report_preserves_policy_and_writes_no_state(
    client, monkeypatch, tmp_path
) -> None:
    from sediment_api.config import settings

    monkeypatch.setattr(settings, "mirror_path", str(tmp_path / "mirrors"))
    store = client.app.state.fact_store
    store.store_inference_call(_inference_call("policy-call"))
    sessions_before = store.count_sessions("testorg")
    facts_before = store.count_facts(
        "testorg", FactTable.INFERENCE_CALLS, include_quarantined=True
    )

    response = client.get(
        "/v1/reports/model-outcomes",
        params=_params(),
        headers={"Authorization": "Bearer test-operator-token-3a7e-2f6c"},
    )

    assert response.status_code == 200
    row = response.json()["report"]["rows"][0]
    assert row["model"] == "claude-sonnet-5"
    assert row["provenance"] == {
        "policy_version": "4",
        "quarantine_revision": 0,
        "policy_digest": None,
    }
    assert store.count_sessions("testorg") == sessions_before
    assert (
        store.count_facts(
            "testorg", FactTable.INFERENCE_CALLS, include_quarantined=True
        )
        == facts_before
    )


def test_reports_exclude_quarantined_facts_and_stamp_revision(
    client, monkeypatch, tmp_path
) -> None:
    from sediment_api.config import settings

    monkeypatch.setattr(settings, "mirror_path", str(tmp_path / "mirrors"))
    store = client.app.state.fact_store
    call = _inference_call("quarantined-call")
    store.store_inference_call(call)
    store.quarantine_fact(
        "testorg",
        FactTable.INFERENCE_CALLS,
        call.inference_call_id,
        reason="operator review",
    )
    revision = store.quarantine_revision("testorg")

    model = client.get(
        "/v1/reports/model-outcomes",
        params=_params(),
        headers={"Authorization": "Bearer test-operator-token-3a7e-2f6c"},
    )
    lifecycle = client.get(
        "/v1/reports/accepted-work-lifecycle",
        params=_params(),
        headers={"Authorization": "Bearer test-operator-token-3a7e-2f6c"},
    )

    assert model.status_code == lifecycle.status_code == 200
    assert model.json()["report"]["rows"] == []
    assert model.json()["report"]["fate_provenance"]["quarantine_revision"] == revision
    lifecycle_provenance = lifecycle.json()["report"]["provenance"]
    assert {item["quarantine_revision"] for item in lifecycle_provenance.values()} == {
        revision
    }


@pytest.mark.parametrize(
    "error_type",
    ["OperationalError", "DisconnectionError", "InterfaceError", "TimeoutError"],
)
def test_report_database_unavailability_uses_the_sanitized_shared_503(
    client, monkeypatch, error_type
) -> None:
    _worker_service(
        monkeypatch,
        "model_report_payload",
        f"from sqlalchemy.exc import {error_type}\n"
        "def replacement(*args, **kwargs):\n"
        f"    raise {error_type}('private-statement', {{'token': 'private-token'}}, RuntimeError('private-driver-detail'))",
    )

    response = client.get(
        "/v1/reports/model-outcomes",
        params=_params(),
        headers={"Authorization": "Bearer test-operator-token-3a7e-2f6c"},
    )

    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "code": "database_unavailable",
            "message": "PostgreSQL fact store unavailable",
        }
    }
    assert "private" not in response.text


def test_report_rejects_a_serialized_response_over_64_mib(client, monkeypatch) -> None:
    from sediment_api.routers import reports

    assert reports._MAX_REPORT_RESPONSE_BYTES == 64 * 1024 * 1024
    _worker_service(
        monkeypatch,
        "model_report_payload",
        "def replacement(*args):\n    return {'rows': [{'model': 'x' * 256}]}",
        "reports._MAX_REPORT_RESPONSE_BYTES = 128",
    )

    response = client.get(
        "/v1/reports/model-outcomes",
        params=_params(),
        headers={"Authorization": "Bearer test-operator-token-3a7e-2f6c"},
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "report response exceeds the fixed limit"}


def test_representative_bounded_payload_meets_regression_ceilings(
    client, monkeypatch, tmp_path
) -> None:
    import time

    from sediment_api.config import settings

    monkeypatch.setattr(settings, "mirror_path", str(tmp_path / "mirrors"))
    store = client.app.state.fact_store
    for index in range(250):
        store.store_inference_call(
            _inference_call(f"bounded-{index:04d}", model=f"model-{index:04d}")
        )

    started = time.perf_counter()
    response = client.get(
        "/v1/reports/model-outcomes",
        params=_params(),
        headers={"Authorization": "Bearer test-operator-token-3a7e-2f6c"},
    )
    elapsed = time.perf_counter() - started

    assert response.status_code == 200
    assert len(response.json()["report"]["rows"]) == 250
    # This guards a representative 250-model payload. It doesn't claim to
    # benchmark the maximum 50,000-call cohort or a 64 MiB response.
    assert elapsed < 10.0
    assert len(response.content) < 64 * 1024 * 1024
