# SPDX-License-Identifier: AGPL-3.0-or-later
"""Report cohorts cannot hide organization-wide decision ambiguity."""

from datetime import UTC, datetime, timedelta

import pytest
from sediment_core import (
    AgentHarness,
    DeveloperDecision,
    EditObservation,
    FactTable,
    GatewayProvider,
    InferenceCall,
    InferenceMessage,
    InteractionMode,
    TextPart,
    ToolCallPart,
)
from sediment_derive import MirrorManager
from sediment_export import (
    OperationalReportScope,
    generate_accepted_work_lifecycle_report,
    generate_model_report_result,
)
from sediment_api.services.operational_reports import generate_operational_model_report


def _population(
    store,
    boundary,
    *,
    case="tool-tool",
    same_session=False,
    org_id="acme",
    outside_at=None,
    outside_org=None,
    decision_key="shared",
    include_decisions=True,
):
    def call(identity, session, observed, gateway, provider_id, tool_id):
        return InferenceCall(
            inference_call_id=identity,
            org_id=org_id,
            session_id=session,
            gateway_provider=gateway,
            model="model-a",
            model_call_id=provider_id,
            input_messages=[],
            output_messages=[
                InferenceMessage(
                    role="assistant",
                    parts=[
                        TextPart(content="def answer(): return 42"),
                        *(
                            [
                                ToolCallPart(
                                    id=tool_id,
                                    name="Edit",
                                    arguments={
                                        "text": "\ud800\x00",
                                        "number": float("nan"),
                                    },
                                ),
                                ToolCallPart(id=tool_id, name="Edit", arguments={}),
                            ]
                            if tool_id
                            else []
                        ),
                    ],
                )
            ],
            observed_at=observed,
        )

    inside = call(
        "inside",
        "current",
        boundary - timedelta(days=10),
        GatewayProvider.LITELLM,
        "shared" if case == "provider-provider" else "inside-provider",
        None if case in {"provider-provider", "older-only"} else "shared",
    )
    outside = call(
        "outside",
        "current" if same_session else "older",
        outside_at or boundary - timedelta(days=40),
        next(
            provider
            for provider in GatewayProvider
            if provider != GatewayProvider.LITELLM
        ),
        "outside-provider" if case == "tool-tool" else "shared",
        "shared" if case == "tool-tool" else None,
    )
    if outside_org is not None:
        outside = outside.model_copy(update={"org_id": outside_org})
    for fact in (outside, inside):
        assert store.store_inference_call(fact)
    decisions = [
        DeveloperDecision(
            org_id=org_id,
            session_id=inside.session_id,
            call_id=decision_key,
            agent_harness=AgentHarness.CLAUDE_CODE,
            file_path="a.py",
            accepted=accepted,
            explicit=True,
            interaction_mode=InteractionMode.AGENT,
            occurred_at=inside.observed_at + timedelta(seconds=index),
            captured_at=inside.observed_at,
        )
        for index, accepted in enumerate((True, False) if include_decisions else ())
    ]
    for decision in decisions:
        assert store.store_decision(decision)
    store.store_edit_observation(
        EditObservation(
            org_id=org_id,
            session_id=inside.session_id,
            call_id="shared",
            agent_harness=AgentHarness.CLAUDE_CODE,
            file_path="a.py",
            applied_text="def answer(): return 42",
            observed_file_text="def answer(): return 42",
            external_lines_added=1,
            external_lines_removed=0,
            occurred_at=inside.observed_at,
            captured_at=inside.observed_at,
        )
    )
    return inside, outside, decisions


@pytest.mark.parametrize("case", ["tool-tool", "provider-provider", "tool-provider"])
@pytest.mark.parametrize("same_session", [False, True])
def test_scoped_reports_keep_org_wide_ambiguity_and_quarantine_control(
    postgres_store, tmp_path, case, same_session
):
    boundary = datetime.now(UTC)
    inside, outside, decisions = _population(
        postgres_store, boundary, case=case, same_session=same_session
    )
    mirrors = MirrorManager(tmp_path / "mirrors")
    scope = OperationalReportScope.trailing_days(30, as_of=boundary)

    scoped = generate_operational_model_report(postgres_store, mirrors, "acme", scope)
    history = generate_model_report_result(postgres_store, mirrors, "acme")
    lifecycle = generate_accepted_work_lifecycle_report(
        postgres_store, mirrors, "acme", scope=scope
    )
    assert scoped.result.rows[0].completions == 1
    assert history.rows[0].completions == 2
    assert (
        scoped.result.rows[0].explicit_accepts == history.rows[0].explicit_accepts == 0
    )
    assert scoped.result.rows[0].explicit_rejects == 0
    assert scoped.result.rows[0].fates == {}
    history_lifecycle = generate_accepted_work_lifecycle_report(
        postgres_store, mirrors, "acme"
    )
    assert (
        lifecycle.accepted_work.accepted_calls
        == history_lifecycle.accepted_work.accepted_calls
        == 0
    )
    assert lifecycle.accepted_work.skips["ambiguous_decision_call_id"] == 2

    postgres_store.quarantine_fact(
        "acme", FactTable.INFERENCE_CALLS, outside.inference_call_id, reason="control"
    )
    unique = generate_operational_model_report(
        postgres_store, mirrors, "acme", scope
    ).result.rows[0]
    assert (unique.completions, unique.explicit_accepts, unique.explicit_rejects) == (
        1,
        1,
        1,
    )
    assert unique.explicit_rejects_by_agent_harness == {"claude-code": 1}
    assert (
        unique.fates
        == unique.explicit_accept_fates
        == unique.fates_with_external_changes
        == {"unmodified": 1}
    )
    assert (
        generate_accepted_work_lifecycle_report(
            postgres_store, mirrors, "acme", scope=scope
        ).accepted_work.accepted_calls
        == 1
    )


@pytest.mark.parametrize("offset,expected", [(0, 0), (1, 1)])
def test_scoped_identity_witnesses_use_explicit_as_of(
    postgres_store, tmp_path, offset, expected
):
    boundary = datetime.now(UTC) - timedelta(days=2)
    _population(
        postgres_store, boundary, outside_at=boundary + timedelta(microseconds=offset)
    )
    mirrors = MirrorManager(tmp_path / "mirrors")
    scope = OperationalReportScope.trailing_days(30, as_of=boundary)
    report = generate_operational_model_report(postgres_store, mirrors, "acme", scope)
    assert report.result.rows[0].completions == 1
    assert report.result.rows[0].explicit_accepts == expected
    assert (
        generate_accepted_work_lifecycle_report(
            postgres_store, mirrors, "acme", scope=scope
        ).accepted_work.accepted_calls
        == expected
    )


def test_foreign_identity_does_not_hide_unique_cohort_decisions(
    postgres_store, tmp_path
):
    boundary = datetime.now(UTC)
    _population(postgres_store, boundary, outside_org="foreign")
    scope = OperationalReportScope.trailing_days(30, as_of=boundary)
    mirrors = MirrorManager(tmp_path / "mirrors")
    row = generate_operational_model_report(
        postgres_store, mirrors, "acme", scope
    ).result.rows[0]
    assert (row.completions, row.explicit_accepts, row.explicit_rejects) == (1, 1, 1)
    assert (
        generate_accepted_work_lifecycle_report(
            postgres_store, mirrors, "acme", scope=scope
        ).accepted_work.accepted_calls
        == 1
    )


def test_older_unique_call_in_cohort_session_does_not_become_cohort_work(
    postgres_store, tmp_path
):
    boundary = datetime.now(UTC)
    _population(postgres_store, boundary, case="older-only", same_session=True)
    scope = OperationalReportScope.trailing_days(30, as_of=boundary)
    mirrors = MirrorManager(tmp_path / "mirrors")
    row = generate_operational_model_report(
        postgres_store, mirrors, "acme", scope
    ).result.rows[0]
    assert (row.completions, row.explicit_accepts, row.explicit_rejects) == (1, 0, 0)
    lifecycle = generate_accepted_work_lifecycle_report(
        postgres_store, mirrors, "acme", scope=scope
    )
    assert lifecycle.accepted_work.accepted_calls == 0
    assert not lifecycle.accepted_work.skips.get("unmatched_decision_call_id")


@pytest.mark.parametrize(
    "preloaded,expected", [(None, 1), ("empty", 0), ("complete", 0), ("unique", 1)]
)
def test_preloaded_identity_population_is_authoritative_for_public_builders(
    postgres_store, tmp_path, preloaded, expected
):
    from sediment_derive import (
        AbandonmentResult,
        Attribution,
        AttributionSource,
        Provenance,
    )
    from sediment_export import (
        assemble_attributed_completions_result,
        build_model_report,
        build_model_report_result,
    )

    boundary = datetime.now(UTC)
    inside, _, decisions = _population(postgres_store, boundary)
    identities = postgres_store.read_inference_call_identities(
        "acme", observed_through=boundary, limit=2
    )
    supplied = (
        None
        if preloaded is None
        else []
        if preloaded == "empty"
        else identities
        if preloaded == "complete"
        else [
            item
            for item in identities
            if item.inference_call_id == inside.inference_call_id
        ]
    )
    kwargs = dict(decisions=decisions, decision_identities=supplied)
    for population in (
        supplied,
        list(reversed(supplied)) if supplied is not None else None,
    ):
        kwargs["decision_identities"] = population
        assembly = assemble_attributed_completions_result(
            postgres_store,
            MirrorManager(tmp_path / "mirrors"),
            "acme",
            completions=[inside],
            attributions=[
                Attribution(
                    org_id="acme",
                    session_id=inside.session_id,
                    inference_call_id=inside.inference_call_id,
                    repo="acme/repo",
                    commit_sha="a" * 40,
                    file_path="a.py",
                    similarity_score=1.0,
                    attribution_source=AttributionSource.JACCARD,
                    provenance=Provenance("2", 0),
                )
            ],
            edit_observations=[],
            ci_outcomes=[],
            abandonment=AbandonmentResult(),
            session_commit_observations=[],
            as_of=boundary,
            **kwargs,
        )
        assert len(assembly.rows) == 1
        assert (
            sum(decision.accepted for decision in assembly.rows[0].decisions)
            == expected
        )
        for builder in (build_model_report, build_model_report_result):
            result = builder([inside], assembly.rows, now=boundary, **kwargs)
            row = (result if isinstance(result, list) else result.rows)[0]
            assert (row.completions, row.explicit_accepts, row.explicit_rejects) == (
                1,
                expected,
                expected,
            )


@pytest.mark.parametrize("path", ["model-outcomes", "accepted-work-lifecycle"])
def test_report_http_identity_budget_and_ambiguity(client, monkeypatch, tmp_path, path):
    import sys
    from sediment_api import workers
    from sediment_api.config import settings

    boundary = datetime.now(UTC)
    store = client.app.state.fact_store
    _, outside, _ = _population(store, boundary, org_id="testorg")
    monkeypatch.setattr(settings, "mirror_path", str(tmp_path / "mirrors"))
    monkeypatch.setattr(
        workers,
        "_WORKER_COMMAND",
        (
            sys.executable,
            "-c",
            "from sediment_api import worker\n"
            "from sediment_api.services import operational_reports\n"
            "from sediment_export import accepted_work_lifecycle\n"
            "operational_reports._SUPPORTING_FACT_LIMIT = 2\n"
            "accepted_work_lifecycle._SUPPORTING_FACT_LIMIT = 2\n"
            "raise SystemExit(worker.main())",
        ),
    )
    scope = OperationalReportScope.trailing_days(30, as_of=boundary)
    params = {
        key: getattr(scope, key).isoformat()
        for key in ("cohort_start", "cohort_end", "as_of")
    }

    def get():
        return client.get(
            f"/v1/reports/{path}",
            params=params,
            headers={"Authorization": "Bearer test-operator-token-3a7e-2f6c"},
        )

    response = get()
    assert response.status_code == 200
    report = response.json()["report"]
    assert (
        report["rows"][0]["explicit_accepts"]
        if path == "model-outcomes"
        else report["accepted_work"]["accepted_calls"]
    ) == 0
    store.store_inference_call(
        outside.model_copy(
            update={"inference_call_id": "third", "model_call_id": "third-provider"}
        )
    )
    response = get()
    assert response.status_code == 409
    assert response.json() == {"detail": "report evidence exceeds the fixed limit"}
    store.quarantine_fact(
        "testorg", FactTable.INFERENCE_CALLS, "third", reason="budget control"
    )
    assert get().status_code == 200


def test_model_cli_identity_budget_and_ambiguity(
    postgres_store, monkeypatch, tmp_path, capsys
):
    import json
    from contextlib import nullcontext
    from sediment_api.reports import model_report
    from sediment_api.services import operational_reports

    _, outside, _ = _population(postgres_store, datetime.now(UTC))
    monkeypatch.setattr(
        model_report,
        "one_shot_fact_store",
        lambda *a, **kw: nullcontext(postgres_store),
    )
    monkeypatch.setattr(operational_reports, "_SUPPORTING_FACT_LIMIT", 2)
    args = [
        "--org",
        "acme",
        "--json",
        "--since-days",
        "30",
        "--mirror-path",
        str(tmp_path / "mirrors"),
    ]
    assert model_report.main(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["rows"][0]["explicit_accepts"] == 0
    postgres_store.store_inference_call(
        outside.model_copy(
            update={"inference_call_id": "third", "model_call_id": "third-provider"}
        )
    )
    assert model_report.main(args) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "identity population exceeds 2" in captured.err


@pytest.mark.parametrize("include_decisions", [True, False])
def test_keyless_or_absent_decisions_need_no_org_identity_read(
    postgres_store, tmp_path, monkeypatch, include_decisions
):
    from sediment_api.services import operational_reports
    from sediment_export import accepted_work_lifecycle

    boundary = datetime.now(UTC)
    _, outside, _ = _population(
        postgres_store, boundary, decision_key=None, include_decisions=include_decisions
    )
    postgres_store.store_inference_call(
        outside.model_copy(
            update={"inference_call_id": "third", "model_call_id": "third-provider"}
        )
    )
    for module in (operational_reports, accepted_work_lifecycle):
        monkeypatch.setattr(module, "_SUPPORTING_FACT_LIMIT", 2)
    scope = OperationalReportScope.trailing_days(30, as_of=boundary)
    mirrors = MirrorManager(tmp_path / "mirrors")
    row = generate_operational_model_report(
        postgres_store, mirrors, "acme", scope
    ).result.rows[0]
    assert (row.completions, row.explicit_accepts) == (1, 0)
    lifecycle = generate_accepted_work_lifecycle_report(
        postgres_store, mirrors, "acme", scope=scope
    )
    assert lifecycle.accepted_work.accepted_calls == 0
    assert lifecycle.accepted_work.skips.get("missing_decision_call_id", 0) == (
        2 if include_decisions else 0
    )


def test_operational_canonical_assembly_uses_complete_identity_witnesses(
    postgres_store, tmp_path
):
    import subprocess
    from sediment_core import ForgeProvider, Push, SessionCommitObservation

    boundary = datetime.now(UTC)
    inside, outside, _ = _population(postgres_store, boundary)
    work = tmp_path / "work"
    work.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=work, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.com")
    (work / "a.py").write_text("def answer(): return 42\n")
    git("add", "a.py")
    git("commit", "-q", "-m", "answer")
    head = git("rev-parse", "HEAD")
    push = Push(
        org_id="acme",
        provider=ForgeProvider.GITHUB,
        repo="acme/repo",
        clone_url=str(work),
        ref="refs/heads/main",
        before_sha="0" * 40,
        after_sha=head,
        captured_at=inside.observed_at + timedelta(seconds=5),
    )
    mirrors = MirrorManager(tmp_path / "mirrors")
    mirrors.ensure(push)
    postgres_store.store_push(push)
    postgres_store.store_session_commit_observation(
        SessionCommitObservation(
            org_id="acme",
            session_id=inside.session_id,
            repo=push.repo,
            commit_sha=head,
            source_push_id=push.push_id,
            captured_at=push.captured_at,
        )
    )
    scope = OperationalReportScope.trailing_days(30, as_of=boundary)
    report = generate_operational_model_report(postgres_store, mirrors, "acme", scope)
    [artifact] = report.attributed_completions
    assert artifact.decisions == []
    assert report.result.rows[0].explicit_accepts == 0
    postgres_store.quarantine_fact(
        "acme", FactTable.INFERENCE_CALLS, outside.inference_call_id, reason="control"
    )
    unique = generate_operational_model_report(postgres_store, mirrors, "acme", scope)
    assert len(unique.attributed_completions[0].decisions) == 2
    assert unique.result.rows[0].explicit_accepts == 1
