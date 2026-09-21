# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Per-model outcome report tests.

``build_model_report`` is a pure aggregation over ``InferenceCall``/``AttributedCompletion``
facts — no store, no mirrors, no real git needed (unlike ``test_attributed_completions.py``,
which exercises ``assemble_attributed_completions`` itself against real git fixtures).
Fixtures here build ``AttributedCompletion``/``InferenceCall``/``DeveloperDecision``/
``CIOutcome`` directly, per AGENTS.md's "never mock a model" rule — these are
real Pydantic/dataclass instances, just hand-assembled rather than derived.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from sediment_core import (
    CIOutcome,
    CIProvider,
    CIResult,
    AgentHarness,
    DeveloperDecision,
    InteractionMode,
    GatewayProvider,
    InferenceCall,
    InferenceMessage,
    TextPart,
    ToolCallPart,
    SessionCommitObservation,
)
from sediment_derive import (
    AttributionShareAlert,
    AttributionShareAlertKind,
    AttributionSource,
    EditFate,
    Fate,
    FateResult,
    Provenance,
    RecoveryResult,
    RecoverySample,
    RepoAttributionShare,
    SessionAbandonment,
)

from sediment_export import (
    AbandonmentSummary,
    CIGrain,
    DiffSizeStats,
    ModelOutcomeReport,
    ModelTemporalTrend,
    OutcomeReportPolicy,
    OperationalReportScope,
    RecoveryDiffSizeDistribution,
    RecoveryYieldReport,
    SignalFunnelReport,
    StratificationMetric,
    StratificationStatus,
    TrendMetric,
    TrendStatus,
    StratifiedRate,
    AttributedCompletion,
    build_model_report,
    build_model_report_result,
    build_recovery_yield_report,
    build_signal_funnel_report,
    build_stratification_checks,
    build_temporal_trend_report,
    shrink_stratified_rates,
    scope_model_report_evidence,
    wilson_score_interval,
)

ORG = "acme-corp"
REPO = "acme-corp/backend-service"
NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
WORKFLOW_CI = "CI"
WORKFLOW_LINT = "Lint"
WORKFLOW_TESTS = "Tests"


def test_outcome_metrics_use_attribution_rate_vocabulary_only() -> None:
    assert hasattr(TrendMetric, "ATTRIBUTION_RATE")
    assert not hasattr(TrendMetric, "SURVIVAL_RATE")
    assert hasattr(StratificationMetric, "ATTRIBUTION_RATE")
    assert not hasattr(StratificationMetric, "SURVIVAL_RATE")


def test_model_report_scope_keeps_late_linked_evidence_through_as_of() -> None:
    inside = _inference_call("inside", "model-a", observed_at=NOW)
    outside = _inference_call(
        "outside", "model-a", observed_at=NOW - timedelta(days=40)
    )
    before_as_of = _ci_outcome(CIResult.PASSED).model_copy(
        update={"captured_at": NOW + timedelta(days=2)}
    )
    after_as_of = _ci_outcome(CIResult.FAILED, run_id="after-as-of").model_copy(
        update={"captured_at": NOW + timedelta(days=8)}
    )
    rows = [
        _attributed_completion(
            "inside",
            decisions=[
                _decision(accepted=True, explicit=True).model_copy(
                    update={"captured_at": NOW + timedelta(days=8)}
                )
            ],
            ci_outcomes=[before_as_of, after_as_of],
        ),
        _attributed_completion("outside", commit_sha=_sha("outside")),
    ]
    scope = OperationalReportScope(
        NOW - timedelta(days=30),
        NOW + timedelta(days=1),
        NOW + timedelta(days=7),
    )

    completions, attributed = scope_model_report_evidence(
        [outside, inside], rows, scope
    )

    assert [call.inference_call_id for call in completions] == ["inside"]
    assert [row.inference_call_id for row in attributed] == ["inside"]
    assert attributed[0].ci_outcomes == [before_as_of]
    assert attributed[0].decisions == []


def test_model_report_scope_excludes_abandonment_derived_after_as_of() -> None:
    call = _inference_call("inside", "model-a", observed_at=NOW)
    row = _abandoned_attributed_completion("inside")
    assert row.abandonment is not None
    row = replace(
        row,
        abandonment=replace(row.abandonment, as_of=NOW + timedelta(days=8)),
    )
    scope = OperationalReportScope(
        NOW - timedelta(days=1),
        NOW + timedelta(days=1),
        NOW + timedelta(days=7),
    )

    _completions, attributed = scope_model_report_evidence([call], [row], scope)

    assert attributed == []


def test_model_report_result_uses_explicit_scope() -> None:
    calls = [
        _inference_call("inside", "model-a", observed_at=NOW),
        _inference_call("outside", "model-a", observed_at=NOW - timedelta(days=40)),
    ]
    scope = OperationalReportScope(
        NOW - timedelta(days=30),
        NOW + timedelta(days=1),
        NOW + timedelta(days=7),
    )

    result = build_model_report_result(calls, [], scope=scope)

    assert len(result.rows) == 1
    assert result.rows[0].model == "model-a"
    assert result.rows[0].completions == 1


def test_model_report_scope_rejects_unscoped_abandonment_summary() -> None:
    scope = OperationalReportScope(
        NOW - timedelta(days=30),
        NOW + timedelta(days=1),
        NOW + timedelta(days=7),
    )
    summary = AbandonmentSummary(abandoned_sessions=1, negative_completions=1)

    with pytest.raises(ValueError, match="scoped abandonment summary"):
        build_model_report_result([], [], scope=scope, abandonment=summary)


def _sha(seed: str) -> str:
    """A valid full-length commit sha, deterministic per seed name."""
    return hashlib.sha1(seed.encode()).hexdigest()


def _inference_call(
    inference_call_id: str,
    model: str | None,
    *,
    observed_at: datetime = NOW,
    session_id: str = "sess-1",
) -> InferenceCall:
    return InferenceCall(
        inference_call_id=inference_call_id,
        org_id=ORG,
        session_id=session_id,
        user_id="dev",
        gateway_provider=GatewayProvider.LITELLM,
        model=model,
        input_messages=[
            InferenceMessage(role="user", parts=[TextPart(content="do it")])
        ],
        output_messages=[
            InferenceMessage(role="assistant", parts=[TextPart(content="done")])
        ],
        observed_at=observed_at,
    )


def _decision(
    *,
    accepted: bool,
    explicit: bool,
    agent_harness: AgentHarness = AgentHarness.CLAUDE_CODE,
    session_id: str = "sess-1",
    call_id: str | None = None,
) -> DeveloperDecision:
    return DeveloperDecision(
        org_id=ORG,
        session_id=session_id,
        user_id="dev",
        agent_harness=agent_harness,
        file_path="a.py",
        accepted=accepted,
        explicit=explicit,
        interaction_mode=InteractionMode.AGENT,
        call_id=call_id,
        occurred_at=NOW,
    )


def _ci_outcome(
    result: CIResult,
    *,
    commit_sha: str = _sha("sha1"),
    repo: str = REPO,
    workflow_name: str = WORKFLOW_CI,
    run_id: str | None = None,
    run_attempt: int | None = None,
) -> CIOutcome:
    return CIOutcome(
        org_id=ORG,
        provider=CIProvider.GITHUB_ACTIONS,
        run_id=run_id or f"run/{commit_sha}/{workflow_name}/{result}",
        run_attempt=run_attempt,
        repo=repo,
        commit_sha=commit_sha,
        branch="main",
        result=result,
        workflow_name=workflow_name,
        run_url=f"run/{commit_sha}",
        captured_at=NOW,
    )


def _attributed_completion(
    inference_call_id: str,
    *,
    score: float = 0.9,
    decisions: list[DeveloperDecision] | None = None,
    ci_outcomes: list[CIOutcome] | None = None,
    commit_sha: str = _sha("sha1"),
    repo: str = REPO,
    observed: bool = True,
    session_id: str = "sess-1",
) -> AttributedCompletion:
    return AttributedCompletion(
        org_id=ORG,
        session_id=session_id,
        inference_call_id=inference_call_id,
        repo=repo,
        commit_sha=commit_sha,
        file_path="a.py",
        similarity_score=score,
        attribution_source=AttributionSource.GIT_NOTES,
        decisions=decisions or [],
        ci_outcomes=ci_outcomes or [],
        provenance=Provenance(policy_version="1", quarantine_revision=0),
        split="train",
        session_commit_observations=(
            SessionCommitObservation(
                observation_id=f"observation/{repo}/{commit_sha}",
                org_id=ORG,
                repo=repo,
                commit_sha=commit_sha,
                session_id=session_id,
                source_push_id="push",
                captured_at=NOW,
            ),
        )
        if observed
        else (),
    )


def _abandoned_attributed_completion(inference_call_id: str) -> AttributedCompletion:
    return AttributedCompletion(
        org_id=ORG,
        session_id="sess-1",
        inference_call_id=inference_call_id,
        repo=None,
        commit_sha=None,
        file_path=None,
        similarity_score=None,
        attribution_source=None,
        decisions=[_decision(accepted=True, explicit=True)],
        ci_outcomes=[],
        provenance=Provenance(policy_version="3", quarantine_revision=0),
        split="train",
        abandonment=SessionAbandonment(
            org_id=ORG,
            session_id="sess-1",
            accepted_decisions=1,
            explicit_accepted_decisions=1,
            last_decision_at=NOW,
            as_of=NOW,
            provenance=Provenance(policy_version="2", quarantine_revision=0),
        ),
    )


def _fate(
    observation_id: str,
    call_id: str,
    fate: EditFate,
    *,
    external_lines_added: int | None = None,
    external_lines_removed: int | None = None,
) -> Fate:
    return Fate(
        observation_id=observation_id,
        org_id=ORG,
        agent_harness=AgentHarness.CLAUDE_CODE,
        session_id="sess-1",
        call_id=call_id,
        score={
            EditFate.DELETED: 0.0,
            EditFate.PARTIALLY_MODIFIED: 0.5,
            EditFate.UNMODIFIED: 1.0,
        }[fate],
        fate=fate,
        external_lines_added=external_lines_added,
        external_lines_removed=external_lines_removed,
        provenance=Provenance(policy_version="1", quarantine_revision=3),
    )


@pytest.mark.parametrize("builder", [build_model_report, build_model_report_result])
@pytest.mark.parametrize("explicit", [True, False])
def test_direct_model_decisions_and_fates_need_no_attribution(builder, explicit):
    call = _inference_call("call", "model-a").model_copy(
        update={
            "output_messages": [
                InferenceMessage(
                    role="assistant",
                    parts=[ToolCallPart(id="tool", name="Edit", arguments={})],
                )
            ]
        }
    )
    decisions = [
        _decision(accepted=accepted, explicit=explicit, call_id="tool").model_copy(
            update={"captured_at": NOW}
        )
        for accepted in (True, False)
    ]
    fate_result = FateResult(
        fates=[
            _fate("observation", "tool", EditFate.UNMODIFIED, external_lines_added=1)
        ]
    )
    expected = None
    for population in (decisions, list(reversed(decisions)), decisions * 2):
        report = builder(
            [call], [], now=NOW, decisions=population, fate_result=fate_result
        )
        row = report[0] if isinstance(report, list) else report.rows[0]
        assert row.explicit_accepts == row.explicit_rejects == int(explicit)
        assert row.explicit_rejects_by_agent_harness == (
            {"claude-code": 1} if explicit else {}
        )
        assert row.fates == {"unmodified": 1}
        assert row.explicit_accept_fates == ({"unmodified": 1} if explicit else {})
        assert row.fates_with_external_changes == {"unmodified": 1}
        assert row.attributed_inference_calls == row.ci_linked == row.ci_passed == 0
        assert row.provenance.policy_version == "4"
        if expected is not None:
            assert report == expected
        expected = report


@pytest.mark.parametrize("builder", [build_model_report, build_model_report_result])
@pytest.mark.parametrize(
    "case,reason",
    [
        ("ambiguous", "ambiguous_decision_call_id"),
        ("wrong-org", "decision_org_mismatch"),
        ("wrong-session", "decision_session_mismatch"),
        ("unmatched", "unmatched_decision_call_id"),
        ("keyless", "missing_decision_call_id"),
        ("late", None),
    ],
)
def test_direct_model_decision_attachment_preserves_scope_and_losses(
    builder, case, reason, caplog
):
    call = _inference_call("inside", "model-a").model_copy(
        update={"model_call_id": "tool", "observed_at": NOW - timedelta(hours=1)}
    )
    decision = _decision(accepted=True, explicit=True, call_id="tool").model_copy(
        update={"captured_at": NOW}
    )
    calls = [call]
    if case == "ambiguous":
        # Uniqueness precedes report cohort and Session filtering.
        calls.append(
            _inference_call(
                "outside",
                "model-b",
                observed_at=NOW - timedelta(days=40),
                session_id="other-session",
            ).model_copy(update={"model_call_id": "tool"})
        )
    else:
        update = {
            "wrong-org": {"org_id": "other-org"},
            "wrong-session": {"session_id": "other-session"},
            "unmatched": {"call_id": "other-tool"},
            "keyless": {"call_id": None},
            "late": {"captured_at": NOW + timedelta(microseconds=1)},
        }[case]
        decision = decision.model_copy(update=update)
    scope = OperationalReportScope(NOW - timedelta(days=1), NOW, NOW)
    for population in (calls, list(reversed(calls))):
        report = builder(population, [], scope=scope, decisions=[decision])
        rows = report if isinstance(report, list) else report.rows
        assert [row.model for row in rows] == ["model-a"]
        assert rows[0].explicit_accepts == 0
    if reason is not None:
        assert f"reason={reason}" in caplog.text


def test_direct_model_decisions_are_authoritative_over_artifact_fanout():
    call = _inference_call("call", "model-a").model_copy(
        update={"model_call_id": "tool"}
    )
    accept = _decision(accepted=True, explicit=True, call_id="tool").model_copy(
        update={"captured_at": NOW}
    )
    reject = _decision(accepted=False, explicit=True, call_id="tool")
    artifacts = [
        _attributed_completion("call", decisions=[accept, reject]),
        replace(
            _attributed_completion("call", decisions=[accept, reject]), file_path="b.py"
        ),
    ]
    [row] = build_model_report([call], artifacts, now=NOW, decisions=[accept, accept])
    assert row.explicit_accepts == 1
    assert row.explicit_rejects == 0
    [empty] = build_model_report([call], artifacts, now=NOW, decisions=[])
    assert empty.explicit_accepts == empty.explicit_rejects == 0


@pytest.mark.parametrize("builder", [build_model_report, build_model_report_result])
def test_direct_model_decision_as_of_does_not_move_the_call_cohort(builder):
    call = _inference_call(
        "call", "model-a", observed_at=NOW - timedelta(days=25)
    ).model_copy(update={"model_call_id": "tool"})
    decision = _decision(accepted=True, explicit=True, call_id="tool").model_copy(
        update={"captured_at": NOW}
    )
    scope = OperationalReportScope(
        NOW - timedelta(days=30), NOW - timedelta(days=20), NOW
    )

    report = builder([call], [], scope=scope, decisions=[decision])

    [row] = report if isinstance(report, list) else report.rows
    assert row.completions == row.explicit_accepts == 1
    assert row.since_days == 10


def test_model_report_counts_joined_fates_once_per_observation() -> None:
    accepted = _decision(accepted=True, explicit=True, call_id="call-accepted")
    implicit = _decision(accepted=True, explicit=False, call_id="call-implicit")
    model_a = _inference_call("a", "model-a")
    model_b = _inference_call("b", "model-b")
    # Repeated Attributed-completion attachment for model A must not multiply
    # one Edit observation.
    attributed = [
        _attributed_completion("a", decisions=[accepted]),
        _attributed_completion("a", decisions=[accepted], commit_sha=_sha("other")),
        _attributed_completion("b", decisions=[implicit]),
    ]
    fate_result = FateResult(
        fates=[
            _fate("o-accepted", "call-accepted", EditFate.DELETED),
            _fate(
                "o-implicit",
                "call-implicit",
                EditFate.PARTIALLY_MODIFIED,
                external_lines_removed=2,
            ),
            _fate("o-unmatched", "call-unmatched", EditFate.UNMODIFIED),
        ],
        skipped=Counter({"invalid_score": 2}),
        provenance=Provenance(policy_version="1", quarantine_revision=3),
    )

    rows = build_model_report(
        [model_b, model_a], attributed, now=NOW, fate_result=fate_result
    )

    assert rows[0].model == "model-a"
    assert rows[0].fates == {"deleted": 1}
    assert rows[0].explicit_accept_fates == {"deleted": 1}
    assert rows[0].fates_with_external_changes == {}
    assert rows[1].model == "model-b"
    assert rows[1].fates == {"partially_modified": 1}
    assert rows[1].explicit_accept_fates == {}
    assert rows[1].fates_with_external_changes == {"partially_modified": 1}


def test_model_report_result_carries_global_fate_diagnostics() -> None:
    provenance = Provenance(policy_version="1", quarantine_revision=4)
    fate_result = FateResult(
        skipped=Counter({"scorer_error": 1}), provenance=provenance
    )

    result = build_model_report_result([], [], now=NOW, fate_result=fate_result)

    assert result.fate_skipped == {"scorer_error": 1}
    assert result.fate_provenance == provenance


def test_model_report_fate_counts_are_independent_of_input_order() -> None:
    decision = _decision(accepted=True, explicit=True, call_id="call-1")
    completions = [_inference_call("b", "model-b"), _inference_call("a", "model-a")]
    attributed = [
        _attributed_completion("b", decisions=[decision]),
        _attributed_completion("a", decisions=[decision]),
    ]
    fate_result = FateResult(
        fates=[_fate("o-1", "call-1", EditFate.UNMODIFIED)],
        provenance=Provenance(policy_version="1", quarantine_revision=0),
    )

    assert build_model_report(
        completions, attributed, now=NOW, fate_result=fate_result
    ) == build_model_report(
        list(reversed(completions)),
        list(reversed(attributed)),
        now=NOW,
        fate_result=fate_result,
    )


def test_abandonment_does_not_inflate_attribution_or_similarity_metrics() -> None:
    completions = [
        _inference_call("c-attributed", "model-a"),
        _inference_call("c-abandoned", "model-a"),
    ]
    attributed = _attributed_completion("c-attributed", score=0.75)
    abandoned = _abandoned_attributed_completion("c-abandoned")

    [row] = build_model_report(completions, [attributed, abandoned], now=NOW)

    assert row.attributed_inference_calls == 1
    assert row.attribution_rate == 0.5
    assert row.mean_similarity == 0.75

    [funnel] = build_signal_funnel_report(completions, [attributed, abandoned], now=NOW)
    assert funnel.attributed == 1
    assert funnel.attribution_rate == 0.5


def test_model_report_accepts_inference_call_facts() -> None:
    labeled = _attributed_completion("v2-call")

    [row] = build_model_report(
        [_inference_call("v2-call", "model-v2")], [labeled], now=NOW
    )

    assert row.model == "model-v2"
    assert row.completions == 1
    assert row.attributed_inference_calls == 1
    assert (
        build_model_report([_inference_call("model-absent", None)], [], now=NOW) == []
    )


def test_model_report_result_carries_the_shared_abandonment_summary() -> None:
    summary = AbandonmentSummary(
        abandoned_sessions=2,
        grade_eligible_sessions=1,
        implicit_only_sessions=1,
        negative_completions=3,
        explicit_accepts_unjoined=1,
        derivation_skipped={"within_grace_horizon": 4},
        provenance=Provenance(policy_version="2", quarantine_revision=0),
    )

    result = build_model_report_result([], [], now=NOW, abandonment=summary)

    assert result.abandonment == summary


def test_model_report_result_carries_attribution_share_and_alerts() -> None:
    share = RepoAttributionShare(
        org_id=ORG,
        repo=REPO,
        window_start=NOW - timedelta(days=30),
        window_end=NOW,
        agent_plausible_commits=4,
        git_notes_attributed=0,
        jaccard_attributed=3,
        unattributed=1,
        git_notes_share=0.0,
        git_notes_share_ci=(0.0, 0.4899),
        provenance=Provenance(policy_version="1", quarantine_revision=0),
    )
    alert = AttributionShareAlert(
        org_id=ORG,
        repo=REPO,
        kind=AttributionShareAlertKind.ZERO_SHARE_NONZERO_ACTIVITY,
        current=share,
        baseline=None,
        reason="4 agent-plausible commits with zero git-notes attribution",
    )

    result = build_model_report_result(
        [],
        [],
        now=NOW,
        attribution_share=[share],
        attribution_alerts=[alert],
    )

    assert result.attribution_share == [share]
    assert result.attribution_alerts == [alert]


def _recovery_sample() -> RecoverySample:
    return RecoverySample(
        org_id=ORG,
        repo=REPO,
        branch="main",
        workflow_name="tests",
        workflow_path=".github/workflows/tests.yml",
        failed_commit_sha="a" * 40,
        fixed_commit_sha="b" * 40,
        failed_outcome_id="failed",
        fixed_outcome_id="fixed",
        recovery_diff="diff --git a/a.py b/a.py\n",
        failed_inference_call_ids=[],
        fixed_inference_call_ids=[],
        provenance=Provenance(policy_version="2", quarantine_revision=0),
    )


def _ci_trials(
    cases: list[tuple[str, str, int, int, str]],
) -> tuple[list[InferenceCall], list[AttributedCompletion]]:
    """CI-linked fixtures for ``(model, repo, passed, failed, id prefix)`` cases."""
    completions: list[InferenceCall] = []
    attributed_completions: list[AttributedCompletion] = []
    for model, repo, passed, failed, prefix in cases:
        for index in range(passed + failed):
            inference_call_id = f"{prefix}-c{index}"
            commit_sha = _sha(f"{prefix}-sha{index}")
            result = CIResult.PASSED if index < passed else CIResult.FAILED
            completions.append(_inference_call(inference_call_id, model))
            attributed_completions.append(
                _attributed_completion(
                    inference_call_id,
                    ci_outcomes=[_ci_outcome(result, commit_sha=commit_sha, repo=repo)],
                    commit_sha=commit_sha,
                    repo=repo,
                )
            )
    return completions, attributed_completions


EMPTY_DIFF_SIZE_STATS = DiffSizeStats(
    count=0, min=None, median=None, p90=None, max=None, histogram={}
)


# ── two-model fixture: exact counts/rates and sort order ───────────────────


def test_two_model_exact_counts_rates_and_sort_order() -> None:
    # "gpt-4o": 2 completions, 1 attributed (1 attributed completion), CI-linked+passed,
    # one explicit accept.
    gpt_c1 = _inference_call("gpt-c1", "gpt-4o")
    gpt_c2 = _inference_call("gpt-c2", "gpt-4o")
    gpt_attributed_completion = _attributed_completion(
        "gpt-c1",
        score=0.8,
        decisions=[_decision(accepted=True, explicit=True)],
        ci_outcomes=[_ci_outcome(CIResult.PASSED)],
    )

    # "claude-sonnet-5": 3 completions, 2 attributed (2 attributed_completions), one
    # CI-linked-but-failed, one explicit reject, one implicit accept (must
    # not count).
    claude_c1 = _inference_call("claude-c1", "claude-sonnet-5")
    claude_c2 = _inference_call("claude-c2", "claude-sonnet-5")
    claude_c3 = _inference_call("claude-c3", "claude-sonnet-5")
    claude_attributed_completion_1 = _attributed_completion(
        "claude-c1",
        score=0.6,
        decisions=[_decision(accepted=False, explicit=True)],
        ci_outcomes=[_ci_outcome(CIResult.FAILED, commit_sha=_sha("sha2"))],
        commit_sha=_sha("sha2"),
    )
    claude_attributed_completion_2 = _attributed_completion(
        "claude-c2",
        score=0.4,
        decisions=[_decision(accepted=True, explicit=False)],  # implicit
        commit_sha=_sha("sha3"),
    )

    completions = [gpt_c1, gpt_c2, claude_c1, claude_c2, claude_c3]
    attributed_completions = [
        gpt_attributed_completion,
        claude_attributed_completion_1,
        claude_attributed_completion_2,
    ]

    rows = build_model_report(completions, attributed_completions, now=NOW)

    assert [r.model for r in rows] == ["claude-sonnet-5", "gpt-4o"]

    claude, gpt = rows

    assert claude == ModelOutcomeReport(
        model="claude-sonnet-5",
        since_days=None,
        completions=3,
        attributed_inference_calls=2,
        attribution_rate=2 / 3,
        attribution_rate_ci=wilson_score_interval(2, 3),
        ci_linked=1,
        ci_passed=0,
        ci_pass_rate=0.0,
        ci_pass_rate_ci=wilson_score_interval(0, 1),
        explicit_accepts=0,
        explicit_rejects=1,
        mean_similarity=(0.6 + 0.4) / 2,
        provenance=Provenance(policy_version="4", quarantine_revision=0),
        grain=CIGrain.COMMIT,
        ci_failures_by_workflow={WORKFLOW_CI: 1},
        explicit_rejects_by_agent_harness={"claude-code": 1},
        session_commit_observation_ids=tuple(
            sorted(f"observation/{REPO}/{_sha(sha)}" for sha in ("sha2", "sha3"))
        ),
    )
    assert gpt == ModelOutcomeReport(
        model="gpt-4o",
        since_days=None,
        completions=2,
        attributed_inference_calls=1,
        attribution_rate=0.5,
        attribution_rate_ci=wilson_score_interval(1, 2),
        ci_linked=1,
        ci_passed=1,
        ci_pass_rate=1.0,
        ci_pass_rate_ci=wilson_score_interval(1, 1),
        explicit_accepts=1,
        explicit_rejects=0,
        mean_similarity=0.8,
        provenance=Provenance(policy_version="4", quarantine_revision=0),
        grain=CIGrain.COMMIT,
        session_commit_observation_ids=(f"observation/{REPO}/{_sha('sha1')}",),
    )


# ── since_days windows both numerator and denominator ───────────────────────


def test_since_days_window_excludes_old_completions_from_both_sides() -> None:
    recent = _inference_call("recent", "gpt-4o", observed_at=NOW - timedelta(days=1))
    old = _inference_call(
        "old", "gpt-4o", observed_at=NOW - timedelta(days=40), session_id="sess-2"
    )
    recent_attributed_completion = _attributed_completion("recent", score=1.0)
    old_attributed_completion = _attributed_completion(
        "old", score=0.5, commit_sha=_sha("sha-old")
    )

    rows = build_model_report(
        [recent, old],
        [recent_attributed_completion, old_attributed_completion],
        since_days=7,
        now=NOW,
    )

    assert len(rows) == 1
    row = rows[0]
    # The old completion is dropped from the denominator (completions) AND
    # its attributed completion from the numerator (attributed_inference_calls) — not just one.
    assert row.completions == 1
    assert row.attributed_inference_calls == 1
    assert row.mean_similarity == 1.0  # only the recent attributed completion's score


def test_since_days_window_can_exclude_a_model_entirely() -> None:
    old = _inference_call("old", "gpt-4o", observed_at=NOW - timedelta(days=40))
    rows = build_model_report([old], [], since_days=7, now=NOW)
    assert rows == []


# ── zero-division safety ────────────────────────────────────────────────────


def test_zero_attributions_yields_zero_rates_never_crashes() -> None:
    completion = _inference_call("c1", "gpt-4o")
    rows = build_model_report([completion], [], now=NOW)

    assert len(rows) == 1
    row = rows[0]
    assert row.completions == 1
    assert row.attributed_inference_calls == 0
    assert row.attribution_rate == 0.0
    assert row.ci_linked == 0
    assert row.ci_passed == 0
    assert row.ci_pass_rate == 0.0
    assert row.explicit_accepts == 0
    assert row.explicit_rejects == 0
    assert row.ci_failures_by_workflow == {}
    assert row.explicit_rejects_by_agent_harness == {}
    assert row.mean_similarity == 0.0


def test_no_completions_or_attributed_completions_yields_empty_report() -> None:
    assert build_model_report([], []) == []


# ── signal funnel: completion attrition toward training rows ───────────────


def test_signal_funnel_counts_every_completion_stage_and_retention_rate() -> None:
    completions = [
        _inference_call("uncorrelated", "gpt-4o"),
        _inference_call("attribution-only", "gpt-4o"),
        _inference_call("ci-passed", "gpt-4o"),
        _inference_call("ci-failed", "gpt-4o"),
        _inference_call("explicit-only", "gpt-4o"),
        _inference_call("implicit-only", "gpt-4o"),
        _inference_call("cancelled-only", "gpt-4o"),
    ]
    attributed_completions = [
        _attributed_completion("attribution-only", commit_sha=_sha("sha-attribution")),
        _attributed_completion(
            "ci-passed",
            ci_outcomes=[_ci_outcome(CIResult.PASSED, commit_sha=_sha("sha-passed"))],
            commit_sha=_sha("sha-passed"),
        ),
        _attributed_completion(
            "ci-failed",
            ci_outcomes=[_ci_outcome(CIResult.FAILED, commit_sha=_sha("sha-failed"))],
            commit_sha=_sha("sha-failed"),
        ),
        _attributed_completion(
            "explicit-only",
            decisions=[_decision(accepted=False, explicit=True)],
            commit_sha=_sha("sha-explicit"),
        ),
        _attributed_completion(
            "implicit-only",
            decisions=[_decision(accepted=True, explicit=False)],
            commit_sha=_sha("sha-implicit"),
        ),
        _attributed_completion(
            "cancelled-only",
            ci_outcomes=[
                _ci_outcome(CIResult.CANCELLED, commit_sha=_sha("sha-cancelled"))
            ],
            commit_sha=_sha("sha-cancelled"),
        ),
    ]

    rows = build_signal_funnel_report(completions, attributed_completions, now=NOW)

    assert rows == [
        SignalFunnelReport(
            model="gpt-4o",
            since_days=None,
            completions_total=7,
            attributed=6,
            attribution_rate=6 / 7,
            ci_linked=3,
            ci_linked_retention_rate=3 / 6,
            has_decision=1,
            has_decision_retention_rate=1 / 6,
            training_row_eligible=3,
            training_row_eligible_retention_rate=3 / 6,
        )
    ]


def test_signal_funnel_dedups_multi_file_completion_and_sorts_models() -> None:
    accept = _decision(accepted=True, explicit=True)
    completions = [
        _inference_call("b1", "model-b"),
        _inference_call("a1", "model-a", session_id="sess-2"),
    ]
    attributed_completions = [
        _attributed_completion("b1", decisions=[accept], commit_sha=_sha("sha1")),
        _attributed_completion("b1", decisions=[accept], commit_sha=_sha("sha1")),
        _attributed_completion(
            "a1",
            session_id="sess-2",
            ci_outcomes=[_ci_outcome(CIResult.PASSED, commit_sha=_sha("sha2"))],
            commit_sha=_sha("sha2"),
        ),
    ]

    rows = build_signal_funnel_report(completions, attributed_completions, now=NOW)

    assert [row.model for row in rows] == ["model-a", "model-b"]
    assert rows[0].training_row_eligible == 1
    assert rows[1].has_decision == 1
    assert rows[1].training_row_eligible == 1


def test_signal_funnel_since_days_windows_completions_and_attributed_completions_together() -> (
    None
):
    recent = _inference_call("recent", "gpt-4o", observed_at=NOW - timedelta(days=1))
    old = _inference_call(
        "old", "gpt-4o", observed_at=NOW - timedelta(days=40), session_id="sess-2"
    )
    rows = build_signal_funnel_report(
        [recent, old],
        [
            _attributed_completion(
                "recent",
                ci_outcomes=[
                    _ci_outcome(CIResult.PASSED, commit_sha=_sha("recent-sha"))
                ],
                commit_sha=_sha("recent-sha"),
            ),
            _attributed_completion(
                "old",
                ci_outcomes=[_ci_outcome(CIResult.PASSED, commit_sha=_sha("old-sha"))],
                commit_sha=_sha("old-sha"),
            ),
        ],
        since_days=7,
        now=NOW,
    )

    assert rows == [
        SignalFunnelReport(
            model="gpt-4o",
            since_days=7,
            completions_total=1,
            attributed=1,
            attribution_rate=1.0,
            ci_linked=1,
            ci_linked_retention_rate=1.0,
            has_decision=0,
            has_decision_retention_rate=0.0,
            training_row_eligible=1,
            training_row_eligible_retention_rate=1.0,
        )
    ]


def test_signal_funnel_zero_completions_yields_empty_report_without_nan() -> None:
    assert build_signal_funnel_report([], [], now=NOW) == []


# ── Wilson score confidence intervals ──────────────────────────────────────


def test_wilson_score_interval_matches_hand_checked_reference_case() -> None:
    # Hand-checked 95% Wilson score interval for k=8 successes out of n=10
    # trials, using z=1.9599639845400534. math.isclose (not exact equality)
    # since the last bit of a sqrt/NormalDist computation can legitimately
    # differ by 1 ULP across platforms/library versions.
    lower, upper = wilson_score_interval(8, 10)
    assert math.isclose(lower, 0.4901624715366419, rel_tol=1e-9)
    assert math.isclose(upper, 0.9433178485456246, rel_tol=1e-9)


def test_wilson_score_interval_zero_trials_returns_documented_sentinel() -> None:
    assert wilson_score_interval(0, 0) == (0.0, 0.0)


def test_wilson_score_interval_one_hundred_percent_small_n_is_not_wald() -> None:
    lower, upper = wilson_score_interval(3, 3)

    assert lower < 1.0
    assert upper <= 1.0
    assert (lower, upper) != (1.0, 1.0)


def test_report_rate_intervals_use_the_same_counts_as_point_estimates() -> None:
    completions = [_inference_call(f"c{i}", "gpt-4o") for i in range(10)]
    attributed_completions = [
        _attributed_completion(f"c{i}", ci_outcomes=[_ci_outcome(CIResult.PASSED)])
        for i in range(8)
    ]

    [row] = build_model_report(completions, attributed_completions, now=NOW)

    assert row.attribution_rate == 0.8
    assert row.attribution_rate_ci == wilson_score_interval(8, 10)
    assert row.ci_pass_rate == 1.0
    assert row.ci_pass_rate_ci == wilson_score_interval(1, 1)


# ── implicit signals never inflate accept/reject columns ───────────────────


def test_implicit_only_decision_does_not_inflate_accept_reject_columns() -> None:
    completion = _inference_call("c1", "gpt-4o")
    attributed_completion = _attributed_completion(
        "c1",
        decisions=[
            _decision(accepted=True, explicit=False),
            _decision(accepted=False, explicit=False),
        ],
    )

    rows = build_model_report([completion], [attributed_completion], now=NOW)

    assert len(rows) == 1
    row = rows[0]
    assert row.explicit_accepts == 0
    assert row.explicit_rejects == 0
    # The attributed completion itself still counts toward attribution — only the acceptance
    # columns are blind to implicit-only decisions.
    assert row.attributed_inference_calls == 1


def test_mixed_explicit_and_implicit_decisions_count_only_explicit() -> None:
    completion = _inference_call("c1", "gpt-4o")
    attributed_completion = _attributed_completion(
        "c1",
        decisions=[
            _decision(accepted=True, explicit=True),
            _decision(accepted=True, explicit=False),
            _decision(accepted=False, explicit=True),
        ],
    )

    rows = build_model_report([completion], [attributed_completion], now=NOW)

    row = rows[0]
    assert row.explicit_accepts == 1
    assert row.explicit_rejects == 1


def test_explicit_rejects_break_down_by_agent_harness_and_ignore_implicit() -> None:
    completion = _inference_call("c1", "gpt-4o")
    attributed_completion = _attributed_completion(
        "c1",
        decisions=[
            _decision(
                accepted=False,
                explicit=True,
                agent_harness=AgentHarness.CLAUDE_CODE,
            ),
            _decision(
                accepted=False,
                explicit=True,
                agent_harness=AgentHarness.COPILOT,
            ),
            _decision(
                accepted=False,
                explicit=False,
                agent_harness=AgentHarness.CODEX,
            ),
            _decision(
                accepted=True,
                explicit=True,
                agent_harness=AgentHarness.CODEX,
            ),
        ],
    )

    [row] = build_model_report([completion], [attributed_completion], now=NOW)

    assert row.explicit_rejects == 2
    assert row.explicit_rejects_by_agent_harness == {
        "claude-code": 1,
        "copilot": 1,
    }


# ── CI: any-passed, non-empty-linked ────────────────────────────────────────


def test_ci_linked_uses_numbered_retry_verdict_instead_of_any_pass() -> None:
    completion = _inference_call("c1", "gpt-4o")
    attributed_completion = _attributed_completion(
        "c1",
        ci_outcomes=[
            _ci_outcome(
                CIResult.FAILED,
                commit_sha=_sha("sha1"),
                run_id="run/retry",
                run_attempt=1,
            ),
            _ci_outcome(
                CIResult.PASSED,
                commit_sha=_sha("sha1"),
                run_id="run/retry",
                run_attempt=2,
            ),
        ],
    )

    rows = build_model_report([completion], [attributed_completion], now=NOW)
    row = rows[0]
    assert row.ci_linked == 1
    assert row.ci_passed == 1
    assert row.ci_failures_by_workflow == {}
    assert row.ci_pass_rate == 1.0


@pytest.mark.parametrize(
    "result",
    [
        CIResult.ERROR,
        CIResult.TIMED_OUT,
        CIResult.CANCELLED,
        CIResult.SKIPPED,
        CIResult.NEUTRAL,
        CIResult.UNKNOWN,
    ],
)
def test_ci_pass_rate_excludes_non_verdict_outcomes(result: CIResult) -> None:
    completion = _inference_call("c1", "gpt-4o")
    attributed_completion = _attributed_completion(
        "c1",
        ci_outcomes=[_ci_outcome(result)],
    )

    [row] = build_model_report([completion], [attributed_completion], now=NOW)

    assert row.ci_linked == 0
    assert row.ci_passed == 0
    assert row.ci_pass_rate == 0.0


def test_ambiguous_workflows_do_not_create_report_failures() -> None:
    model_a = _inference_call("a1", "model-a")
    model_b = _inference_call("b1", "model-b", session_id="sess-2")
    attributed_completions = [
        _attributed_completion(
            "a1",
            ci_outcomes=[
                _ci_outcome(
                    CIResult.FAILED,
                    commit_sha=_sha("sha-a"),
                    workflow_name=WORKFLOW_LINT,
                ),
                _ci_outcome(
                    CIResult.FAILED,
                    commit_sha=_sha("sha-a"),
                    workflow_name=WORKFLOW_TESTS,
                ),
                _ci_outcome(
                    CIResult.PASSED,
                    commit_sha=_sha("sha-a"),
                    workflow_name=WORKFLOW_CI,
                ),
            ],
            commit_sha=_sha("sha-a"),
        ),
        _attributed_completion(
            "b1",
            session_id="sess-2",
            ci_outcomes=[
                _ci_outcome(
                    CIResult.FAILED,
                    commit_sha=_sha("sha-b"),
                    workflow_name=WORKFLOW_TESTS,
                )
            ],
            commit_sha=_sha("sha-b"),
        ),
    ]

    rows = build_model_report([model_b, model_a], attributed_completions, now=NOW)

    assert {r.model: r.ci_failures_by_workflow for r in rows} == {
        "model-a": {},
        "model-b": {WORKFLOW_TESTS: 1},
    }


def test_one_failed_ci_run_on_a_multi_file_commit_counts_once() -> None:
    # Assembly attaches the same CIOutcome to every attributed completion of its
    # commit (the (repo, commit_sha) join): a 3-file commit failing Lint
    # once must report Lint=1, not Lint=3 — mirrors
    # test_one_decision_on_a_multi_file_completion_counts_once's dedup
    # precedent, but keyed on outcome_id instead of decision_id.
    failed_lint = _ci_outcome(
        CIResult.FAILED, commit_sha=_sha("sha1"), workflow_name=WORKFLOW_LINT
    )
    completion = _inference_call("c-1", model="model-a")
    attributed_completions = [
        _attributed_completion(
            "c-1", ci_outcomes=[failed_lint], commit_sha=_sha("sha1")
        ),
        _attributed_completion(
            "c-1", ci_outcomes=[failed_lint], commit_sha=_sha("sha1")
        ),
        _attributed_completion(
            "c-1", ci_outcomes=[failed_lint], commit_sha=_sha("sha1")
        ),
    ]
    [row] = build_model_report([completion], attributed_completions, now=NOW)
    assert row.ci_failures_by_workflow == {WORKFLOW_LINT: 1}


def test_two_distinct_failed_runs_on_the_same_workflow_both_count() -> None:
    # Two different commits, both failing Lint, are two real runs — the
    # outcome_id dedup must not collapse distinct CIOutcomes that merely
    # share a workflow_name.
    completion = _inference_call("c-1", model="model-a")
    attributed_completions = [
        _attributed_completion(
            "c-1",
            ci_outcomes=[
                _ci_outcome(
                    CIResult.FAILED,
                    commit_sha=_sha("sha1"),
                    workflow_name=WORKFLOW_LINT,
                )
            ],
            commit_sha=_sha("sha1"),
        ),
        _attributed_completion(
            "c-1",
            ci_outcomes=[
                _ci_outcome(
                    CIResult.FAILED,
                    commit_sha=_sha("sha2"),
                    workflow_name=WORKFLOW_LINT,
                )
            ],
            commit_sha=_sha("sha2"),
        ),
    ]
    [row] = build_model_report([completion], attributed_completions, now=NOW)
    assert row.ci_failures_by_workflow == {WORKFLOW_LINT: 2}


def test_attributed_completion_with_no_ci_outcomes_is_not_ci_linked() -> None:
    completion = _inference_call("c1", "gpt-4o")
    attributed_completion = _attributed_completion("c1", ci_outcomes=[])

    rows = build_model_report([completion], [attributed_completion], now=NOW)
    row = rows[0]
    assert row.ci_linked == 0
    assert row.ci_pass_rate == 0.0


# ── CI grain: a multi-file commit's CI outcome must count once ─────────────


def test_multi_file_commit_ci_outcome_counts_once_under_commit_grain() -> None:
    # Assembly attaches the same outcome list to every attributed completion of a
    # commit: a 3-file commit with one CI run must report ci_linked == 1,
    # not 3 — CIGrain.COMMIT is the default, so no explicit policy needed.
    completion = _inference_call("c1", "gpt-4o")
    outcome = _ci_outcome(CIResult.PASSED, commit_sha=_sha("sha1"))
    attributed_completions = [
        _attributed_completion("c1", ci_outcomes=[outcome], commit_sha=_sha("sha1")),
        _attributed_completion("c1", ci_outcomes=[outcome], commit_sha=_sha("sha1")),
        _attributed_completion("c1", ci_outcomes=[outcome], commit_sha=_sha("sha1")),
    ]

    rows = build_model_report([completion], attributed_completions, now=NOW)
    row = rows[0]
    assert row.ci_linked == 1
    assert row.ci_passed == 1
    assert row.ci_pass_rate == 1.0


def test_multi_file_commit_ci_outcome_still_counts_thrice_under_attributed_completion_grain() -> (
    None
):
    # CIGrain.ATTRIBUTED_COMPLETION is the superseded behavior: preserved, non-default, and
    # explicitly known-inflated — this pins that it remains available and
    # behaves exactly as before, not that it is desirable.
    completion = _inference_call("c1", "gpt-4o")
    outcome = _ci_outcome(CIResult.PASSED, commit_sha=_sha("sha1"))
    attributed_completions = [
        _attributed_completion("c1", ci_outcomes=[outcome], commit_sha=_sha("sha1")),
        _attributed_completion("c1", ci_outcomes=[outcome], commit_sha=_sha("sha1")),
        _attributed_completion("c1", ci_outcomes=[outcome], commit_sha=_sha("sha1")),
    ]

    rows = build_model_report(
        [completion],
        attributed_completions,
        now=NOW,
        policy=OutcomeReportPolicy(ci_grain=CIGrain.ATTRIBUTED_COMPLETION),
    )
    row = rows[0]
    assert row.ci_linked == 3
    assert row.ci_passed == 3
    assert row.ci_pass_rate == 1.0
    assert row.provenance == Provenance(policy_version="4", quarantine_revision=0)
    assert row.grain == CIGrain.ATTRIBUTED_COMPLETION


def test_commit_grain_does_not_over_collapse_distinct_commits() -> None:
    # Two different commits must still be counted separately under
    # CIGrain.COMMIT — the dedup is by commit_sha, not a blanket collapse.
    completion = _inference_call("c1", "gpt-4o")
    attributed_completions = [
        _attributed_completion(
            "c1",
            ci_outcomes=[_ci_outcome(CIResult.PASSED, commit_sha=_sha("sha1"))],
            commit_sha=_sha("sha1"),
        ),
        _attributed_completion(
            "c1",
            ci_outcomes=[_ci_outcome(CIResult.FAILED, commit_sha=_sha("sha2"))],
            commit_sha=_sha("sha2"),
        ),
    ]

    rows = build_model_report([completion], attributed_completions, now=NOW)
    row = rows[0]
    assert row.ci_linked == 2
    assert row.ci_passed == 1
    assert row.ci_pass_rate == 0.5


def test_same_sha_in_two_repos_counts_as_two_commits() -> None:
    # Forks share commit SHAs, so the commit-grain dedup keys on
    # (repo, commit_sha) — the ci join's own key, never sha alone. The same
    # sha in two repos is two distinct CI trials, and the passed one must
    # not be clobbered by the failed one in either input order.
    fork = "acme-corp/backend-fork"
    completion = _inference_call("c1", "gpt-4o")
    t_pass = _attributed_completion("c1", ci_outcomes=[_ci_outcome(CIResult.PASSED)])
    t_fail = _attributed_completion(
        "c1", ci_outcomes=[_ci_outcome(CIResult.FAILED, repo=fork)], repo=fork
    )

    for attributed_completions in ([t_pass, t_fail], [t_fail, t_pass]):
        row = build_model_report([completion], attributed_completions, now=NOW)[0]
        assert row.ci_linked == 2
        assert row.ci_passed == 1
        assert row.ci_pass_rate == 0.5


def test_two_completions_on_one_commit_count_one_ci_trial() -> None:
    # The commit-grain dedup also collapses completion fan-out: two
    # completions attributed to the same commit share its single CI run,
    # so it is one trial, not two.
    completions = [_inference_call("c1", "gpt-4o"), _inference_call("c2", "gpt-4o")]
    outcome = _ci_outcome(CIResult.PASSED)
    attributed_completions = [
        _attributed_completion("c1", ci_outcomes=[outcome]),
        _attributed_completion("c2", ci_outcomes=[outcome]),
    ]

    row = build_model_report(completions, attributed_completions, now=NOW)[0]
    assert row.ci_linked == 1
    assert row.ci_passed == 1


# ── a attributed completion whose completion is missing is dropped, never guessed at ──────


def test_attributed_completion_with_unresolvable_inference_call_id_is_dropped() -> None:
    completion = _inference_call("c1", "gpt-4o")
    orphan_attributed_completion = _attributed_completion("does-not-exist")

    rows = build_model_report([completion], [orphan_attributed_completion], now=NOW)

    assert len(rows) == 1
    assert rows[0].attributed_inference_calls == 0


def test_one_decision_on_a_multi_file_completion_counts_once() -> None:
    # Assembly attaches the same decision to every attributed completion of its
    # completion (org-scope call_id join): a 3-file commit with one explicit
    # accept must report 1 accept, not 3.
    accept = _decision(accepted=True, explicit=True)
    completion = _inference_call("c-1", model="model-a")
    attributed_completions = [
        _attributed_completion("c-1", decisions=[accept], commit_sha=_sha("sha1")),
        _attributed_completion("c-1", decisions=[accept], commit_sha=_sha("sha1")),
        _attributed_completion("c-1", decisions=[accept], commit_sha=_sha("sha1")),
    ]
    [row] = build_model_report([completion], attributed_completions)
    assert row.explicit_accepts == 1
    assert row.explicit_rejects == 0


# ── recovery-pair yield ────────────────────────────────────────────────────


def test_recovery_yield_report_counts_failed_runs_pairs_and_skips() -> None:
    recovery = RecoveryResult(
        pairs=[_recovery_sample()],
        skipped=Counter({"mirror_absent": 2, "diff_oversized": 1}),
    )

    row = build_recovery_yield_report(
        [
            _ci_outcome(CIResult.FAILED, commit_sha="a" * 40),
            _ci_outcome(CIResult.FAILED, commit_sha="b" * 40),
            _ci_outcome(CIResult.PASSED, commit_sha="c" * 40),
            _ci_outcome(CIResult.CANCELLED, commit_sha="d" * 40),
        ],
        recovery,
    )

    assert row == RecoveryYieldReport(
        total_failed_ci_runs=2,
        recovery_pairs=1,
        recovery_yield_rate=0.5,
        skipped=Counter({"mirror_absent": 2, "diff_oversized": 1}),
        diff_size_distribution=RecoveryDiffSizeDistribution(
            max_recovery_diff_lines=200,
            kept=EMPTY_DIFF_SIZE_STATS,
            dropped=EMPTY_DIFF_SIZE_STATS,
        ),
    )


def test_recovery_yield_report_zero_failed_runs_has_zero_rate() -> None:
    row = build_recovery_yield_report([], RecoveryResult())

    assert row.total_failed_ci_runs == 0
    assert row.recovery_pairs == 0
    assert row.recovery_yield_rate == 0.0
    assert row.skipped == Counter()
    assert row.diff_size_distribution == RecoveryDiffSizeDistribution(
        max_recovery_diff_lines=200,
        kept=EMPTY_DIFF_SIZE_STATS,
        dropped=EMPTY_DIFF_SIZE_STATS,
    )


def test_recovery_yield_report_summarizes_diff_sizes_at_cap_boundary() -> None:
    recovery = RecoveryResult(
        pairs=[_recovery_sample(), _recovery_sample(), _recovery_sample()],
        skipped=Counter({"diff_oversized": 2}),
        max_recovery_diff_lines=10,
        kept_diff_line_counts=[2, 9, 10],
        dropped_diff_line_counts=[11, 30],
    )

    row = build_recovery_yield_report(
        [_ci_outcome(CIResult.FAILED, commit_sha=f"{n:040x}") for n in range(5)],
        recovery,
    )

    assert row.diff_size_distribution == RecoveryDiffSizeDistribution(
        max_recovery_diff_lines=10,
        kept=DiffSizeStats(
            count=3,
            min=2,
            median=9,
            p90=10,
            max=10,
            histogram={"1-5": 1, "6-10": 2},
        ),
        dropped=DiffSizeStats(
            count=2,
            min=11,
            median=20.5,
            p90=30,
            max=30,
            histogram={"11-25": 1, "26-50": 1},
        ),
    )


# ── repo stratification: Simpson's-paradox guard ───────────────────────────


def test_ci_stratification_flags_real_simpsons_paradox() -> None:
    # Hand-checked construction:
    #   easy repo: model-a 80/100 = 80% < model-b 9/10 = 90%
    #   hard repo: model-a 1/10 = 10% < model-b 20/100 = 20%
    #   aggregate: model-a 81/110 = 73.6% > model-b 29/110 = 26.4%
    # Model A wins only because it is over-represented in the easy repo.
    cases = [
        ("model-a", "acme/easy", 80, 20, "a-easy"),
        ("model-a", "acme/hard", 1, 9, "a-hard"),
        ("model-b", "acme/easy", 9, 1, "b-easy"),
        ("model-b", "acme/hard", 20, 80, "b-hard"),
    ]
    completions, attributed_completions = _ci_trials(cases)

    rows = build_model_report(completions, attributed_completions, now=NOW)
    checks = build_stratification_checks(
        completions, attributed_completions, rows, now=NOW
    )

    ci_check = checks[0]
    assert ci_check.metric == StratificationMetric.CI_PASS_RATE
    assert ci_check.status == StratificationStatus.REVERSAL_DETECTED
    [comparison] = ci_check.comparisons
    assert comparison.left_model == "model-a"
    assert comparison.right_model == "model-b"
    assert comparison.aggregate_left.numerator == 81
    assert comparison.aggregate_left.denominator == 110
    assert comparison.aggregate_right.numerator == 29
    assert comparison.aggregate_right.denominator == 110
    assert comparison.aggregate_delta > 0
    [adjusted] = ci_check.adjusted_comparisons
    assert adjusted.left_model == "model-a"
    assert adjusted.right_model == "model-b"
    assert math.isclose(adjusted.naive_delta, 52 / 110)
    # Mantel-Haenszel pooled risk difference:
    #   easy weight = 100*10/(100+10), delta = 0.80 - 0.90 = -0.10
    #   hard weight = 10*100/(10+100), delta = 0.10 - 0.20 = -0.10
    #   pooled = (w_easy*-0.10 + w_hard*-0.10) / (w_easy + w_hard)
    assert math.isclose(adjusted.adjusted_delta, -0.10)
    assert adjusted.compared_repos == 2
    assert adjusted.skipped_repos == 0
    assert {reversal.repo for reversal in comparison.reversals} == {
        "acme/easy",
        "acme/hard",
    }
    assert all(reversal.repo_delta < 0 for reversal in comparison.reversals)


def test_ci_stratification_clean_case_reports_no_reversal() -> None:
    cases = [
        ("model-a", "acme/easy", 9, 1, "clean-a-easy"),
        ("model-a", "acme/hard", 6, 4, "clean-a-hard"),
        ("model-b", "acme/easy", 8, 2, "clean-b-easy"),
        ("model-b", "acme/hard", 5, 5, "clean-b-hard"),
    ]
    completions, attributed_completions = _ci_trials(cases)

    rows = build_model_report(completions, attributed_completions, now=NOW)
    ci_check = build_stratification_checks(
        completions, attributed_completions, rows, now=NOW
    )[0]

    assert ci_check.status == StratificationStatus.NO_REVERSAL_DETECTED
    [comparison] = ci_check.comparisons
    assert comparison.aggregate_delta > 0
    assert comparison.reversals == []
    [adjusted] = ci_check.adjusted_comparisons
    assert math.isclose(adjusted.naive_delta, 0.10)
    assert math.isclose(adjusted.adjusted_delta, 0.10)
    assert adjusted.compared_repos == 2


def test_ci_stratification_adjusted_estimate_is_deterministic() -> None:
    cases = [
        ("model-a", "acme/easy", 80, 20, "det-a-easy"),
        ("model-a", "acme/hard", 1, 9, "det-a-hard"),
        ("model-b", "acme/easy", 9, 1, "det-b-easy"),
        ("model-b", "acme/hard", 20, 80, "det-b-hard"),
    ]
    completions, attributed_completions = _ci_trials(cases)

    rows = build_model_report(completions, attributed_completions, now=NOW)
    checks = build_stratification_checks(
        completions, attributed_completions, rows, now=NOW
    )
    assert checks == build_stratification_checks(
        completions, attributed_completions, rows, now=NOW
    )

    shuffled_completions = list(reversed(completions))
    shuffled_attributed_completions = list(reversed(attributed_completions))
    shuffled_rows = build_model_report(
        shuffled_completions, shuffled_attributed_completions, now=NOW
    )
    shuffled_checks = build_stratification_checks(
        shuffled_completions, shuffled_attributed_completions, shuffled_rows, now=NOW
    )

    assert shuffled_rows == rows
    assert shuffled_checks == checks


def test_shrink_stratified_rates_pulls_tiny_extreme_cell_toward_grand_mean() -> None:
    rates = [
        StratifiedRate("tiny", numerator=3, denominator=3, rate=1.0),
        StratifiedRate("large", numerator=250, denominator=500, rate=0.5),
        StratifiedRate("anchor", numerator=50, denominator=100, rate=0.5),
    ]

    tiny, large, _ = shrink_stratified_rates(rates)

    assert tiny.grand_mean == 303 / 603
    assert tiny.shrunk_rate < 0.55
    assert abs(tiny.shrunk_rate - tiny.raw_rate) > 0.45
    assert math.isclose(large.shrunk_rate, large.raw_rate, abs_tol=0.002)


def test_shrink_stratified_rates_uses_hand_computable_moments() -> None:
    rates = [
        StratifiedRate("small", numerator=0, denominator=10, rate=0.0),
        StratifiedRate("large", numerator=80, denominator=100, rate=0.8),
    ]

    small, large = shrink_stratified_rates(rates)

    grand_mean = 80 / 110
    binomial_variance = grand_mean * (1 - grand_mean)
    observed_variance = (
        (10 * ((0.0 - grand_mean) ** 2)) + (100 * ((0.8 - grand_mean) ** 2))
    ) / 110
    expected_sampling_variance = (2 * binomial_variance) / 110
    between_variance = observed_variance - expected_sampling_variance
    expected_pooling_strength = (binomial_variance / between_variance) - 1
    expected_small_weight = 10 / (10 + expected_pooling_strength)
    expected_large_weight = 100 / (100 + expected_pooling_strength)

    assert math.isclose(small.pooling_strength, expected_pooling_strength)
    assert math.isclose(
        small.shrunk_rate,
        (expected_small_weight * 0.0) + ((1 - expected_small_weight) * grand_mean),
    )
    assert math.isclose(
        large.shrunk_rate,
        (expected_large_weight * 0.8) + ((1 - expected_large_weight) * grand_mean),
    )


def test_shrink_stratified_rates_equal_denominators_have_uniform_shrinkage() -> None:
    shrunk = shrink_stratified_rates(
        [
            StratifiedRate("low", numerator=1, denominator=10, rate=0.1),
            StratifiedRate("mid", numerator=5, denominator=10, rate=0.5),
            StratifiedRate("high", numerator=9, denominator=10, rate=0.9),
        ]
    )

    weights = {rate.shrinkage_weight for rate in shrunk}
    assert len(weights) == 1


def test_ci_stratification_includes_deterministic_shrunk_repo_rates() -> None:
    cases = [
        ("model-a", "acme/easy", 9, 1, "shrunk-a-easy"),
        ("model-a", "acme/hard", 3, 7, "shrunk-a-hard"),
        ("model-b", "acme/easy", 8, 2, "shrunk-b-easy"),
        ("model-b", "acme/hard", 2, 8, "shrunk-b-hard"),
    ]
    completions, attributed_completions = _ci_trials(cases)

    rows = build_model_report(completions, attributed_completions, now=NOW)
    shuffled_rows = build_model_report(
        list(reversed(completions)),
        attributed_completions[::2] + attributed_completions[1::2],
        now=NOW,
    )

    ci_check = build_stratification_checks(
        completions, attributed_completions, rows, now=NOW
    )[0]
    shuffled_ci_check = build_stratification_checks(
        list(reversed(completions)),
        attributed_completions[::2] + attributed_completions[1::2],
        shuffled_rows,
        now=NOW,
    )[0]

    assert ci_check == shuffled_ci_check
    model_a_easy = next(
        entry for entry in ci_check.shrunk_rates if entry.repo == "acme/easy"
    ).model_rates["model-a"]
    assert model_a_easy.raw_rate == 0.9
    assert model_a_easy.shrunk_rate < model_a_easy.raw_rate
    assert model_a_easy.grand_mean == 22 / 40


def test_ci_stratification_single_repo_degrades_gracefully() -> None:
    completions, attributed_completions = _ci_trials(
        [
            ("model-a", "acme/only", 8, 2, "single-a"),
            ("model-b", "acme/only", 7, 3, "single-b"),
        ]
    )

    rows = build_model_report(completions, attributed_completions, now=NOW)
    ci_check = build_stratification_checks(
        completions, attributed_completions, rows, now=NOW
    )[0]

    assert ci_check.status == StratificationStatus.NOT_ENOUGH_REPOS
    assert ci_check.checked_repos == 1
    assert ci_check.comparisons == []
    [adjusted] = ci_check.adjusted_comparisons
    assert math.isclose(adjusted.naive_delta, 0.10)
    assert math.isclose(adjusted.adjusted_delta, 0.10)
    assert adjusted.compared_repos == 1


def test_attribution_rate_stratification_is_explicitly_not_checkable_by_repo() -> None:
    checks = build_stratification_checks([], [], now=NOW)
    attribution_check = checks[1]
    assert attribution_check.metric == StratificationMetric.ATTRIBUTION_RATE
    assert attribution_check.status == StratificationStatus.NOT_CHECKABLE
    assert "InferenceCall has no canonical repo field" in attribution_check.reason


# ── temporal trend/drift detection ─────────────────────────────────────────


def _attribution_rate_windows(
    rates: list[float],
    *,
    model: str = "gpt-4o",
    per_window: int = 10,
) -> tuple[list[InferenceCall], list[AttributedCompletion]]:
    completions: list[InferenceCall] = []
    attributed_completions: list[AttributedCompletion] = []
    start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    for window_index, rate in enumerate(rates):
        captured_at = start + timedelta(days=window_index * 7)
        survived = round(rate * per_window)
        for item_index in range(per_window):
            inference_call_id = f"trend-w{window_index}-c{item_index}"
            completions.append(
                _inference_call(inference_call_id, model, observed_at=captured_at)
            )
            if item_index < survived:
                attributed_completions.append(
                    _attributed_completion(
                        inference_call_id,
                        commit_sha=_sha(f"trend-sha-{window_index}-{item_index}"),
                    )
                )
    return completions, attributed_completions


def _trend_for(
    rates: list[float],
    metric: TrendMetric = TrendMetric.ATTRIBUTION_RATE,
) -> tuple[list[InferenceCall], list[AttributedCompletion], ModelTemporalTrend]:
    completions, attributed_completions = _attribution_rate_windows(rates)
    trends = build_temporal_trend_report(completions, attributed_completions, now=NOW)
    trend = next(t for t in trends if t.metric == metric)
    return completions, attributed_completions, trend


def test_temporal_trend_detects_significant_increase() -> None:
    _, _, trend = _trend_for([0.1, 0.2, 0.3, 0.6, 0.8, 1.0])

    assert trend.metric == TrendMetric.ATTRIBUTION_RATE
    assert [window.rate for window in trend.windows] == [
        0.1,
        0.2,
        0.3,
        0.6,
        0.8,
        1.0,
    ]
    assert trend.test.status == TrendStatus.SIGNIFICANT_INCREASE
    assert trend.test.p_value is not None
    assert trend.test.p_value < 0.05


def test_temporal_trend_does_not_false_positive_on_flat_noisy_rates() -> None:
    _, _, trend = _trend_for([0.5, 0.4, 0.6, 0.5, 0.5, 0.4, 0.6])

    assert trend.test.status == TrendStatus.NO_SIGNIFICANT_TREND
    assert trend.test.p_value is not None
    assert trend.test.p_value > 0.05


def test_temporal_trend_degrades_gracefully_with_too_few_windows() -> None:
    _, _, trend = _trend_for([0.1, 1.0])

    assert trend.test.status == TrendStatus.INSUFFICIENT_DATA
    assert trend.test.p_value is None
    assert "need at least three" in trend.test.reason


def test_temporal_attribution_excludes_abandonment_evidence() -> None:
    completion = _inference_call("c-abandoned", "model-a")

    trends = build_temporal_trend_report(
        [completion],
        [_abandoned_attributed_completion(completion.inference_call_id)],
        now=NOW,
    )

    attribution = next(
        item for item in trends if item.metric == TrendMetric.ATTRIBUTION_RATE
    )
    assert len(attribution.windows) == 1
    assert attribution.windows[0].numerator == 0
    assert attribution.windows[0].denominator == 1
    assert attribution.windows[0].rate == 0.0


def test_temporal_trend_is_deterministic_under_shuffled_inputs() -> None:
    completions, attributed_completions, trend = _trend_for(
        [0.1, 0.2, 0.3, 0.6, 0.8, 1.0]
    )

    shuffled = build_temporal_trend_report(
        list(reversed(completions)),
        list(reversed(attributed_completions)),
        now=NOW,
    )

    assert shuffled == build_temporal_trend_report(
        completions, attributed_completions, now=NOW
    )
    assert trend == next(
        t for t in shuffled if t.metric == TrendMetric.ATTRIBUTION_RATE
    )


def test_inferred_commit_cannot_enter_factual_ci_counts_but_decision_remains():
    call = _inference_call("observation-gap", "model-a", observed_at=NOW)
    row = _attributed_completion(
        call.inference_call_id,
        decisions=[_decision(accepted=True, explicit=True)],
        observed=False,
        ci_outcomes=[_ci_outcome(CIResult.PASSED)],
    )
    result = build_model_report([call], [row], now=NOW)[0]
    assert result.ci_linked == 0
    assert result.ci_passed == 0
    assert result.explicit_accepts == 1
    assert result.session_commit_unobserved == 1


@pytest.mark.parametrize(
    "case",
    [
        "matching",
        "late",
        "wrong_org",
        "wrong_repo",
        "wrong_commit",
        "wrong_session",
        "absent",
    ],
)
def test_all_public_model_outcome_builders_enforce_qualified_observation_boundary(case):
    call = _inference_call("call", "model")
    row = _attributed_completion(
        "call",
        ci_outcomes=[_ci_outcome(CIResult.PASSED)],
        decisions=[_decision(accepted=True, explicit=True)],
    )
    fact = row.session_commit_observations[0]
    changes = {
        "late": {"captured_at": NOW + timedelta(microseconds=1)},
        "wrong_org": {"org_id": "other"},
        "wrong_repo": {"repo": "other/repo"},
        "wrong_commit": {"commit_sha": "b" * 40},
        "wrong_session": {"session_id": "other"},
    }
    row = replace(
        row,
        session_commit_observations=()
        if case == "absent"
        else (fact.model_copy(update=changes.get(case, {})),),
    )
    rows = [row, replace(row, file_path="second.py")]
    expected = int(case == "matching")
    report = build_model_report_result([call], rows, now=NOW)
    assert report.rows[0].ci_passed == expected
    assert report.rows[0].session_commit_unobserved == 1 - expected
    assert report.rows[0].explicit_accepts == 1
    funnel = build_signal_funnel_report([call], rows, now=NOW)[0]
    assert funnel.ci_linked == expected
    assert funnel.session_commit_unobserved == 1 - expected
    ci_trend = next(
        item
        for item in build_temporal_trend_report([call], rows, now=NOW)
        if item.metric == TrendMetric.CI_PASS_RATE
    )
    assert sum(item.denominator for item in ci_trend.windows) == expected
    assert ci_trend.session_commit_unobserved == 1 - expected
    assert report == build_model_report_result([call], list(reversed(rows)), now=NOW)


@pytest.mark.parametrize("grain", list(CIGrain))
@pytest.mark.parametrize("observed", [False, True])
def test_factual_intermediates_gate_before_grain_and_comparison(grain, observed):
    from sediment_export.outcome_report import model_report_inputs
    from sediment_export.significance import compare_models

    calls = [_inference_call(name, name) for name in ("model-a", "model-b")]
    rows = [
        _attributed_completion(
            call.inference_call_id,
            observed=observed,
            commit_sha=_sha(call.inference_call_id),
            ci_outcomes=[_ci_outcome(result, commit_sha=_sha(call.inference_call_id))],
            decisions=[_decision(accepted=True, explicit=True)],
        )
        for call, result in zip(calls, (CIResult.PASSED, CIResult.FAILED), strict=True)
    ]
    rows += [replace(row, file_path="second.py") for row in rows]
    policy = OutcomeReportPolicy(ci_grain=grain)
    results = []
    for population in (rows, list(reversed(rows))):
        _, inputs = model_report_inputs(calls, population, None, NOW)
        assert sum(bool(row.ci_outcomes) for row in inputs) == 4 * observed
        report = build_model_report_result(
            calls, population, now=NOW, policy=policy, include_trends=True
        )
        unit = 1 if grain == CIGrain.COMMIT else 2
        assert [row.ci_linked for row in report.rows] == [unit * observed] * 2
        assert [row.session_commit_unobserved for row in report.rows] == [
            int(not observed)
        ] * 2
        assert report.rows[1].ci_failures_by_workflow == ({"CI": 1} if observed else {})
        check = next(
            item
            for item in report.stratification
            if item.metric == StratificationMetric.CI_PASS_RATE
        )
        assert check.session_commit_unobserved == 2 * int(not observed)
        assert bool(check.adjusted_comparisons) == observed
        # These public comparisons receive already-qualified populations.
        comparison = compare_models(*report.rows).ci_pass_rate
        assert comparison.n_a == comparison.n_b == unit * observed
        results.append(report)
    assert results[0] == results[1]


@pytest.mark.parametrize(
    "evidence_lag", [timedelta(), timedelta(hours=12), timedelta(days=7)]
)
def test_explicit_model_cohort_remains_authoritative_in_every_panel(evidence_lag):
    start, end = NOW - timedelta(hours=1), NOW + timedelta(hours=1)
    as_of = end + evidence_lag
    scope = OperationalReportScope(start, end, as_of)
    calls = [
        _inference_call("start", "model-a", observed_at=start),
        _inference_call("inside", "model-a"),
        _inference_call(
            "before", "outside", observed_at=start - timedelta(microseconds=1)
        ),
        _inference_call("end", "outside", observed_at=end),
    ]
    passed = CIOutcome.model_validate(
        {
            **_ci_outcome(CIResult.PASSED).model_dump(),
            "captured_at": as_of,
        }
    )
    late_failed = CIOutcome.model_validate(
        {
            **_ci_outcome(CIResult.FAILED, run_id="late").model_dump(),
            "captured_at": as_of + timedelta(microseconds=1),
        }
    )
    observed = _attributed_completion("start", ci_outcomes=[passed, late_failed])
    observed = replace(
        observed,
        session_commit_observations=(
            SessionCommitObservation.model_validate(
                {
                    **observed.session_commit_observations[0].model_dump(),
                    "captured_at": as_of,
                }
            ),
        ),
    )
    missing = _attributed_completion(
        "inside", observed=False, commit_sha=_sha("missing")
    )
    population = [observed, missing]
    expected = None
    for call_order, row_order in (
        (calls, population),
        (list(reversed(calls)), list(reversed(population))),
    ):
        result = build_model_report_result(
            call_order, row_order, scope=scope, include_trends=True
        )
        if expected is not None:
            assert result == expected
        expected = result
        [row] = result.rows
        assert row.model == "model-a"
        assert row.completions == row.attributed_inference_calls == 2
        assert row.ci_linked == row.ci_passed == row.session_commit_unobserved == 1
        assert row.ci_failures_by_workflow == {}
        [funnel] = result.signal_funnel
        assert funnel.model == row.model
        assert funnel.completions_total == funnel.attributed == 2
        assert funnel.since_days == row.since_days == 1
        assert funnel.ci_linked == funnel.session_commit_unobserved == 1
        assert result.stratification[0].session_commit_unobserved == 1
        trends = {trend.metric: trend for trend in result.trends}
        [attribution_window] = trends[TrendMetric.ATTRIBUTION_RATE].windows
        [ci_window] = trends[TrendMetric.CI_PASS_RATE].windows
        assert attribution_window.window_start == ci_window.window_start == start
        assert attribution_window.denominator == attribution_window.numerator == 2
        assert ci_window.denominator == ci_window.numerator == 1
    without_first = build_model_report_result(
        calls[1:], population, scope=scope, include_trends=True
    )
    assert without_first.trends[0].windows[0].window_start == start


@pytest.mark.parametrize("late_evidence", ["call", "ci", "decision"])
def test_model_report_fold_boundary_excludes_late_calls_ci_and_decisions(late_evidence):
    from zoneinfo import ZoneInfo

    zone = ZoneInfo("Europe/London")
    boundary = datetime(2026, 10, 25, 1, 30, tzinfo=zone, fold=0)
    late = datetime(2026, 10, 25, 1, 15, tzinfo=zone, fold=1)
    call = _inference_call("inference", "model-a", observed_at=boundary)
    future = _inference_call("future", "future-model", observed_at=late)
    ci = CIOutcome.model_validate(
        {**_ci_outcome(CIResult.PASSED).model_dump(), "captured_at": late}
    )
    decision = DeveloperDecision.model_validate(
        {
            **_decision(accepted=True, explicit=True, call_id="inference").model_dump(),
            "captured_at": late,
            "occurred_at": late,
        }
    )
    call = InferenceCall.model_validate(
        {**call.model_dump(), "model_call_id": "inference"}
    )
    artifact = _attributed_completion("inference", ci_outcomes=[ci])
    result = build_model_report_result(
        [call, future] if late_evidence == "call" else [call],
        [artifact] if late_evidence == "ci" else [],
        decisions=[decision] if late_evidence == "decision" else [],
        now=boundary,
    )
    [row] = result.rows
    assert row.model == "model-a"
    assert row.completions == 1
    assert row.ci_linked == row.explicit_accepts == 0
    [funnel] = result.signal_funnel
    assert funnel.model == "model-a"
    assert funnel.ci_linked == 0


@pytest.mark.parametrize("boundary_kind", ["cohort", "evidence"])
def test_scoped_model_report_preserves_earlier_fold_instants(boundary_kind):
    from zoneinfo import ZoneInfo

    zone = ZoneInfo("Europe/London")
    early = datetime(2026, 10, 25, 1, 30, tzinfo=zone, fold=0)
    late = datetime(2026, 10, 25, 1, 15, tzinfo=zone, fold=1)
    scope = OperationalReportScope(
        early - timedelta(days=1),
        late
        if boundary_kind == "cohort"
        else early.astimezone(UTC) + timedelta(minutes=1),
        late,
    )
    call = _inference_call("inference", "model-a", observed_at=early)
    ci = CIOutcome.model_validate(
        {**_ci_outcome(CIResult.PASSED).model_dump(), "captured_at": early}
    )
    decision = DeveloperDecision.model_validate(
        {
            **_decision(accepted=True, explicit=True).model_dump(),
            "captured_at": early,
            "occurred_at": early,
        }
    )
    artifact = _attributed_completion(
        "inference", ci_outcomes=[ci], decisions=[decision]
    )
    result = build_model_report_result([call], [artifact], scope=scope)
    [row] = result.rows
    assert row.completions == row.ci_linked == row.explicit_accepts == 1
    [funnel] = result.signal_funnel
    assert funnel.completions_total == funnel.ci_linked == funnel.has_decision == 1


def test_scoped_model_stratification_counts_the_same_lagged_ci_population():
    scope = OperationalReportScope(
        NOW - timedelta(hours=1), NOW + timedelta(hours=1), NOW + timedelta(days=7)
    )
    calls = [_inference_call("left", "model-a"), _inference_call("right", "model-b")]
    artifacts = [
        _attributed_completion(
            identifier,
            repo=repo,
            commit_sha=_sha(identifier),
            ci_outcomes=[
                _ci_outcome(
                    result,
                    repo=repo,
                    commit_sha=_sha(identifier),
                    run_id=f"{repo}/{identifier}",
                )
            ],
        )
        for repo in (REPO, "acme-corp/other")
        for identifier, result in (
            ("left", CIResult.PASSED),
            ("right", CIResult.FAILED),
        )
    ]
    report = build_model_report_result(calls, artifacts, scope=scope)
    check = report.stratification[0]
    assert check.checked_repos == 2
    [comparison] = check.comparisons
    assert comparison.compared_repos == 2
    left, right = report.rows
    assert comparison.aggregate_left.denominator == left.ci_linked == 2
    assert comparison.aggregate_left.numerator == left.ci_passed == 2
    assert comparison.aggregate_right.denominator == right.ci_linked == 2
    assert comparison.aggregate_right.numerator == right.ci_passed == 0
    assert all(
        set(entry.model_rates) == {"model-a", "model-b"} for entry in check.shrunk_rates
    )
