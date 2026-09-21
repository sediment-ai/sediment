# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bounded operational-report application service tests."""

from datetime import UTC, datetime, timedelta

from sediment_core import ForgeProvider, Push, SessionCommitObservation
from sediment_export import OperationalReportScope
from sediment_derive import MirrorManager
import pytest


@pytest.mark.parametrize("entry_point", ["scoped", "rows", "result"])
def test_model_report_generators_keep_direct_decisions_and_fates_without_commits(
    postgres_store, tmp_path, entry_point
):
    from sediment_core import (
        AgentHarness,
        DeveloperDecision,
        EditObservation,
        FactTable,
        GatewayProvider,
        InferenceCall,
        InferenceMessage,
        InteractionMode,
        ToolCallPart,
    )
    from sediment_export import generate_model_report, generate_model_report_result
    from sediment_api.services.operational_reports import (
        generate_operational_model_report,
    )

    end = datetime.now(UTC)
    occurred = end - timedelta(days=20)
    call = InferenceCall(
        org_id="acme",
        session_id="session",
        inference_call_id="inference",
        gateway_provider=GatewayProvider.LITELLM,
        model="model-a",
        input_messages=[],
        output_messages=[
            InferenceMessage(
                role="assistant",
                parts=[ToolCallPart(id="tool", name="Edit", arguments={})],
            )
        ],
        observed_at=occurred,
    )
    decisions = [
        DeveloperDecision(
            org_id="acme",
            session_id="session",
            call_id="tool",
            agent_harness=AgentHarness.CLAUDE_CODE,
            file_path="a.py",
            accepted=accepted,
            explicit=True,
            interaction_mode=InteractionMode.AGENT,
            occurred_at=occurred + timedelta(seconds=index),
            captured_at=occurred,
        )
        for index, accepted in enumerate((True, False))
    ]
    observation = EditObservation(
        org_id="acme",
        session_id="session",
        call_id="tool",
        agent_harness=AgentHarness.CLAUDE_CODE,
        file_path="a.py",
        applied_text="def answer(): return 42",
        observed_file_text="def answer(): return 42",
        external_lines_added=1,
        external_lines_removed=0,
        occurred_at=occurred,
        captured_at=occurred,
    )
    postgres_store.store_inference_call(call)
    for decision in decisions:
        postgres_store.store_decision(decision)
    postgres_store.store_edit_observation(observation)
    mirrors = MirrorManager(tmp_path / "mirrors")

    def build():
        if entry_point == "scoped":
            report = generate_operational_model_report(
                postgres_store,
                mirrors,
                "acme",
                OperationalReportScope.trailing_days(30, as_of=end),
            )
            assert report.attributed_completions == []
            assert report.result.abandonment.negative_completions == 0
            return report.result.rows[0]
        if entry_point == "rows":
            return generate_model_report(postgres_store, mirrors, "acme")[0]
        return generate_model_report_result(postgres_store, mirrors, "acme").rows[0]

    row = build()
    assert (row.explicit_accepts, row.explicit_rejects) == (1, 1)
    assert row.explicit_rejects_by_agent_harness == {"claude-code": 1}
    assert row.fates == row.explicit_accept_fates == {"unmodified": 1}
    assert row.fates_with_external_changes == {"unmodified": 1}
    assert row.attributed_inference_calls == row.ci_linked == 0

    # Captured evidence after the boundary cannot change direct counts or Fate.
    postgres_store.store_decision(
        decisions[0].model_copy(
            update={
                "decision_id": "late-decision",
                "occurred_at": occurred + timedelta(seconds=5),
                "captured_at": end + timedelta(days=1),
            }
        )
    )
    postgres_store.store_edit_observation(
        observation.model_copy(
            update={
                "observation_id": "late-observation",
                "occurred_at": occurred + timedelta(seconds=5),
                "captured_at": end + timedelta(days=1),
                "observed_file_text": "",
            }
        )
    )
    assert build() == row

    postgres_store.quarantine_fact(
        "acme", FactTable.DEVELOPER_DECISIONS, decisions[0].decision_id, reason="review"
    )
    quarantined = build()
    assert (quarantined.explicit_accepts, quarantined.explicit_rejects) == (0, 1)
    assert quarantined.fates == {"unmodified": 1}
    assert quarantined.explicit_accept_fates == {}
    postgres_store.quarantine_fact(
        "acme", FactTable.EDIT_OBSERVATIONS, observation.observation_id, reason="review"
    )
    without_fate = build()
    assert without_fate.explicit_rejects == 1
    assert without_fate.fates == without_fate.fates_with_external_changes == {}


def test_operational_model_report_service_owns_explicit_scope() -> None:
    from sediment_api.services.operational_reports import model_report_payload

    scope = OperationalReportScope.trailing_days(
        30, as_of=datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    )

    assert callable(model_report_payload)
    assert scope.as_of.isoformat() == "2026-09-06T12:00:00+00:00"


def test_operational_model_report_returns_canonical_payload_without_writes(
    tmp_path, postgres_store, monkeypatch
) -> None:
    from sediment_api.services import operational_reports

    scope = OperationalReportScope.trailing_days(
        30, as_of=datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    )
    before = postgres_store.count_sessions("acme")

    captured = {}

    def attribution_share(*args, **kwargs):
        captured.update(kwargs)
        return [], []

    monkeypatch.setattr(
        operational_reports,
        "derive_model_report_attribution_share",
        attribution_share,
    )
    payload = operational_reports.model_report_payload(
        postgres_store, MirrorManager(tmp_path / "mirrors"), "acme", scope
    )

    assert set(payload) == {
        "rows",
        "stratification",
        "signal_funnel",
        "abandonment",
        "fate_skipped",
        "fate_provenance",
        "repository_skipped",
        "ci_skipped",
        "attribution_share",
        "attribution_alerts",
    }
    assert payload["fate_provenance"] == {
        "policy_version": "1",
        "quarantine_revision": 0,
        "policy_digest": None,
    }
    assert postgres_store.count_sessions("acme") == before
    assert captured["candidate_limit"] == 50_000
    assert captured["note_session_ids_by_commit"] == {}


def test_operational_model_report_rounds_partial_days_up(
    tmp_path, postgres_store, monkeypatch
) -> None:
    from sediment_api.services import operational_reports

    end = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    scope = OperationalReportScope(
        cohort_start=end - timedelta(hours=36),
        cohort_end=end,
        as_of=end,
    )
    captured = {}

    def attribution_share(*args, **kwargs):
        captured["since_days"] = args[3]
        return [], []

    monkeypatch.setattr(
        operational_reports,
        "derive_model_report_attribution_share",
        attribution_share,
    )

    operational_reports.generate_operational_model_report(
        postgres_store, MirrorManager(tmp_path / "mirrors"), "acme", scope
    )

    assert captured["since_days"] == 2


def test_operational_model_report_note_snapshot_covers_baseline_sessions(
    tmp_path, postgres_store, monkeypatch
) -> None:
    from contextlib import contextmanager
    from types import SimpleNamespace

    from sediment_api.services import operational_reports

    end = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    old_head = "a" * 40
    current_head = "b" * 40
    old_push = Push(
        org_id="acme",
        provider=ForgeProvider.GITHUB,
        repo="acme/repo",
        clone_url="https://example.com/acme/repo.git",
        ref="refs/heads/main",
        before_sha="0" * 40,
        after_sha=old_head,
        captured_at=end - timedelta(days=20),
    )
    for push in (
        old_push,
        Push(
            org_id="acme",
            provider=ForgeProvider.GITHUB,
            repo="acme/repo",
            clone_url="https://example.com/acme/repo.git",
            ref="refs/heads/main",
            before_sha=old_head,
            after_sha=current_head,
            captured_at=end - timedelta(days=1),
        ),
    ):
        postgres_store.store_push(push)
    postgres_store.store_session_commit_observation(
        SessionCommitObservation(
            org_id="acme",
            session_id="baseline-session",
            repo="acme/repo",
            commit_sha=old_head,
            source_push_id=old_push.push_id,
            captured_at=end - timedelta(days=19),
        )
    )

    class Mirrors:
        @contextmanager
        def read_repository_snapshot(self, keys):
            yield

        def open_repository(self, key):
            return self

        def list_push_commits(self, push, limit):
            return [push.after_sha]

    mirrors = Mirrors()
    captured = {}
    monkeypatch.setattr(
        operational_reports,
        "derive_attribution_result",
        lambda *args, **kwargs: SimpleNamespace(attributions=[]),
    )

    def attribution_share(*args, **kwargs):
        captured.update(kwargs)
        return [], []

    monkeypatch.setattr(
        operational_reports,
        "derive_model_report_attribution_share",
        attribution_share,
    )

    operational_reports.generate_operational_model_report(
        postgres_store,
        mirrors,
        "acme",
        OperationalReportScope.trailing_days(2, as_of=end),
    )

    from sediment_derive import CommitKey, LegacyRepositoryKey

    assert captured["note_session_ids_by_commit"][
        CommitKey(LegacyRepositoryKey("acme", "acme/repo"), old_head)
    ] == (frozenset({"baseline-session"}))


def test_operational_model_report_rejects_oversized_derived_keys_before_read(
    tmp_path, postgres_store, monkeypatch
) -> None:
    from types import SimpleNamespace

    from sediment_api.services import operational_reports
    from sediment_core import store as store_module

    scope = OperationalReportScope.trailing_days(
        30, as_of=datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    )
    attributions = [
        SimpleNamespace(
            org_id="acme",
            repo="acme/one",
            commit_sha="a" * 40,
            repository_identity=None,
        ),
        SimpleNamespace(
            org_id="acme",
            repo="acme/two",
            commit_sha="b" * 40,
            repository_identity=None,
        ),
    ]
    monkeypatch.setattr(store_module, "REPOSITORY_FILTER_KEY_LIMIT", 1)
    monkeypatch.setattr(
        operational_reports,
        "derive_attribution_result",
        lambda *a, **k: SimpleNamespace(attributions=attributions),
    )

    with pytest.raises(ValueError, match="Repository filter exceeds"):
        operational_reports.generate_operational_model_report(
            postgres_store, MirrorManager(tmp_path / "mirrors"), "acme", scope
        )


@pytest.mark.parametrize("evidence_lag_days", [0, 7])
def test_scoped_model_service_keeps_cohort_when_evidence_cutoff_advances(
    postgres_store, tmp_path, evidence_lag_days
):
    from sediment_core import GatewayProvider, InferenceCall
    from sediment_api.services.operational_reports import (
        generate_operational_model_report,
    )

    at = datetime(2026, 8, 1, 12, tzinfo=UTC)
    scope = OperationalReportScope(
        at - timedelta(hours=1),
        at + timedelta(hours=1),
        at + timedelta(hours=1, days=evidence_lag_days),
    )
    for identifier, observed_at in (
        ("before", scope.cohort_start - timedelta(microseconds=1)),
        ("start", scope.cohort_start),
        ("inside", at),
        ("end", scope.cohort_end),
    ):
        postgres_store.store_inference_call(
            InferenceCall(
                org_id="acme",
                session_id="session",
                inference_call_id=identifier,
                gateway_provider=GatewayProvider.LITELLM,
                model="model-a",
                input_messages=[],
                output_messages=[],
                observed_at=observed_at,
            )
        )
    report = generate_operational_model_report(
        postgres_store,
        MirrorManager(tmp_path / "mirrors"),
        "acme",
        scope,
        include_trends=True,
    ).result
    [row], [funnel] = report.rows, report.signal_funnel
    assert row.completions == funnel.completions_total == 2
    assert row.session_commit_unobserved == funnel.session_commit_unobserved == 0
    assert report.stratification[0].session_commit_unobserved == 0
    [trend] = [item for item in report.trends if item.metric == "attribution_rate"]
    [window] = trend.windows
    assert window.denominator == 2
    assert window.window_start == scope.cohort_start


@pytest.mark.parametrize(
    "entry_point", ["scoped_model", "model", "scoped_lifecycle", "lifecycle"]
)
def test_public_reports_do_not_materialize_large_unused_histories(
    postgres_store, postgres_engine, tmp_path, entry_point
):
    import gc
    import tracemalloc
    from sqlalchemy import event
    from sediment_core import GatewayProvider, InferenceCall, InferenceMessage, TextPart
    from sediment_export import (
        generate_model_report_result,
        generate_accepted_work_lifecycle_report,
    )
    from sediment_api.services.operational_reports import (
        generate_operational_model_report,
    )

    boundary = datetime(2026, 9, 6, tzinfo=UTC)
    payload = "x" * (1024 * 1024)
    for index in range(16):
        postgres_store.store_inference_call(
            InferenceCall(
                org_id="acme",
                session_id=f"session-{index}",
                gateway_provider=GatewayProvider.LITELLM,
                model="model-a",
                model_call_id=f"call-{index}",
                input_messages=[
                    InferenceMessage(role="user", parts=[TextPart(content=payload)])
                ],
                output_messages=[
                    InferenceMessage(role="assistant", parts=[TextPart(content="Done")])
                ],
                raw={"content": payload},
                observed_at=boundary - timedelta(days=1),
            )
        )
    del payload
    gc.collect()
    statements = []

    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(postgres_engine, "before_cursor_execute", record)
    scope = OperationalReportScope.trailing_days(30, as_of=boundary)
    mirrors = MirrorManager(tmp_path / "mirrors")
    tracemalloc.start()
    try:
        if entry_point == "scoped_model":
            report = generate_operational_model_report(
                postgres_store, mirrors, "acme", scope
            ).result
            assert report.rows[0].completions == 16
        elif entry_point == "model":
            report = generate_model_report_result(
                postgres_store, mirrors, "acme", now=boundary
            )
            assert report.rows[0].completions == 16
        else:
            report = generate_accepted_work_lifecycle_report(
                postgres_store,
                mirrors,
                "acme",
                scope=scope if entry_point == "scoped_lifecycle" else None,
                as_of=boundary,
            )
            assert report.org_id == "acme"
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
        event.remove(postgres_engine, "before_cursor_execute", record)
    assert peak < 16 * 1024 * 1024, f"{entry_point} allocated {peak} bytes"
    selected = " ".join(statement.partition("FROM")[0] for statement in statements)
    assert "inference_calls.input_messages" not in selected
    assert "inference_calls.raw" not in selected
