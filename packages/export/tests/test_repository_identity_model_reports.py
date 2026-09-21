# SPDX-License-Identifier: AGPL-3.0-or-later
"""Model report evidence follows captured repository lifetimes across consumers."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from sediment_core import CIOutcome, InferenceCall, Push, SessionCommitObservation
from sediment_derive import (
    AttributionSource,
    Provenance,
    build_repository_context,
    repository_identity_evidence_of,
    repository_identity_of,
)
from sediment_export import AttributedCompletion, build_model_report_result
from sediment_export.outcome_report import model_report_inputs

ORG = "identity-reports"
OLD = "acme/old"
NEW = "acme/new"
AT = datetime(2026, 9, 12, tzinfo=UTC)
LATER = AT + timedelta(hours=1)
SHA = "a" * 40
IDENTITY = dict(
    repository_provider="github", repository_host="github.com", repository_id="101"
)


def _evidence(
    *,
    identity=IDENTITY,
    repo=OLD,
    ci_repo=NEW,
    suffix="",
    model="model",
    legacy_observation=False,
):
    push = Push(
        push_id=f"push{suffix}",
        org_id=ORG,
        provider="github",
        repo=repo,
        clone_url="/synthetic/remote.git",
        ref="refs/heads/main",
        before_sha="0" * 40,
        after_sha=SHA,
        captured_at=AT,
        **identity,
    )
    observation = SessionCommitObservation(
        observation_id=f"observation{suffix}",
        org_id=ORG,
        repo=repo,
        commit_sha=SHA,
        session_id=f"session{suffix}",
        source_push_id=push.push_id,
        captured_at=AT,
        **({} if legacy_observation else identity),
    )
    ci = CIOutcome(
        outcome_id=f"ci{suffix}",
        org_id=ORG,
        provider="github_actions",
        repo=ci_repo,
        commit_sha=SHA,
        branch="main",
        run_id=f"run{suffix}",
        result="passed",
        captured_at=LATER,
        **identity,
    )
    call = InferenceCall(
        inference_call_id=f"call{suffix}",
        org_id=ORG,
        session_id=observation.session_id,
        user_id="developer",
        gateway_provider="litellm",
        model=model,
        observed_at=AT,
        input_messages=[],
        output_messages=[],
    )
    row = AttributedCompletion(
        org_id=ORG,
        session_id=call.session_id,
        inference_call_id=call.inference_call_id,
        repo=repo,
        commit_sha=SHA,
        file_path="code.py",
        similarity_score=1.0,
        attribution_source=AttributionSource.GIT_NOTES,
        decisions=[],
        ci_outcomes=[ci],
        provenance=Provenance(policy_version="test", quarantine_revision=0),
        split="train",
        session_commit_observations=(observation,),
        repository_identity=repository_identity_of(push),
        source_push_id=push.push_id,
    )
    return call, row, push, observation, ci


def _context(*facts, as_of=LATER):
    return build_repository_context(
        [repository_identity_evidence_of(fact) for fact in facts], (), ORG, as_of=as_of
    )


@pytest.mark.parametrize("legacy_observation", [False, True])
def test_model_inputs_keep_renamed_ci_and_original_source_observation(
    legacy_observation,
):
    call, row, push, observation, ci = _evidence(legacy_observation=legacy_observation)
    other_ci = ci.model_copy(
        update={"outcome_id": "fork-ci", "run_id": "fork-run", "repository_id": "202"}
    )
    context = _context(push, observation, ci, other_ci)
    supplied = replace(row, ci_outcomes=[other_ci, ci])
    calls, rows = model_report_inputs(
        [call], [supplied], None, LATER, repository_context=context
    )
    assert calls == [call]
    assert rows[0].ci_outcomes == [ci]
    assert rows[0].session_commit_observations == (observation,)
    assert observation.repo == OLD and ci.repo == NEW


def test_model_panels_count_renamed_lifetime_once_and_keep_permutation():
    call, row, push, observation, ci = _evidence()
    context = _context(push, observation, ci)
    second = replace(row, repo=NEW, file_path="another.py")
    reports = [
        build_model_report_result(
            [call], rows, now=LATER, repository_context=context, include_trends=True
        )
        for rows in ([row, second], [second, row])
    ]
    assert reports[0] == reports[1]
    report = reports[0]
    assert report.rows[0].ci_linked == report.rows[0].ci_passed == 1
    assert report.rows[0].session_commit_observation_ids == (
        observation.observation_id,
    )
    assert report.signal_funnel[0].ci_linked == 1
    assert report.rows[0].completions == 1


def test_model_strata_separate_reused_slug_lifetimes():
    populations = [
        _evidence(
            identity={**IDENTITY, "repository_id": repository},
            repo=OLD,
            ci_repo=OLD,
            suffix=f"-{repository}-{model}",
            model=model,
        )
        for repository in ("101", "202")
        for model in ("model-a", "model-b")
    ]
    context = _context(*(fact for population in populations for fact in population[2:]))
    result = build_model_report_result(
        [p[0] for p in populations],
        [p[1] for p in populations],
        now=LATER,
        repository_context=context,
    )
    assert [row.ci_linked for row in result.rows] == [2, 2]
    check = next(
        check for check in result.stratification if check.metric == "ci_pass_rate"
    )
    assert check.checked_repos == 2
    assert len(check.shrunk_rates) == 2
    assert {
        entry.repository_identity.repository_id for entry in check.shrunk_rates
    } == {"101", "202"}
    assert all(entry.repo == OLD for entry in check.shrunk_rates)


def test_identified_report_without_context_keeps_direct_population_and_counts_loss():
    call, row, *_ = _evidence()
    report = build_model_report_result([call], [row], now=LATER)
    assert report.rows[0].completions == report.rows[0].attributed_inference_calls == 1
    assert report.rows[0].ci_linked == 0
    assert (
        report.repository_skipped["attribution_edges"]["repository_identity_unresolved"]
        == 1
    )


def test_context_boundary_is_default_and_later_ci_stays_absent():
    call, row, push, observation, ci = _evidence()
    context = _context(push, observation, ci, as_of=AT)
    report = build_model_report_result([call], [row], repository_context=context)
    assert report.rows[0].completions == report.rows[0].attributed_inference_calls == 1
    assert report.rows[0].ci_linked == 0
    with pytest.raises(ValueError, match="boundary"):
        build_model_report_result([call], [row], now=LATER, repository_context=context)


def test_report_refuses_mismatched_push_source_despite_equal_commit():
    call, row, push, observation, ci = _evidence()
    foreign_push = push.model_copy(
        update={"push_id": "other-push", "repository_id": "202"}
    )
    context = _context(push, observation, ci, foreign_push)
    report = build_model_report_result(
        [call],
        [replace(row, source_push_id=foreign_push.push_id)],
        repository_context=context,
    )
    assert report.rows[0].ci_linked == 0
    assert (
        report.repository_skipped["attribution_edges"]["repository_identity_conflict"]
        == 1
    )


def test_complete_context_outside_cohort_claim_declines_legacy_join():
    from sediment_core import DeveloperDecision, InferenceMessage, ToolCallPart

    call, row, push, observation, ci = _evidence(identity={}, ci_repo=OLD)
    call = call.model_copy(
        update={
            "output_messages": [
                InferenceMessage(
                    role="assistant",
                    parts=[ToolCallPart(id="tool", name="Edit", arguments={})],
                )
            ]
        }
    )
    decision = DeveloperDecision(
        decision_id="decision",
        org_id=ORG,
        session_id=call.session_id,
        user_id="developer",
        agent_harness="codex",
        interaction_mode="agent",
        call_id="tool",
        accepted=True,
        explicit=True,
        file_path="code.py",
        occurred_at=AT,
        captured_at=AT,
    )
    claim = push.model_copy(
        update={"push_id": "outside-cohort", **IDENTITY, "captured_at": LATER}
    )
    legacy_context = _context(push, observation, ci)
    claimed_context = _context(push, observation, ci, claim)
    before = build_model_report_result(
        [call], [row], decisions=[decision], repository_context=legacy_context
    )
    after = build_model_report_result(
        [call], [row], decisions=[decision], repository_context=claimed_context
    )
    assert before.rows[0].ci_linked == 1 and after.rows[0].ci_linked == 0
    assert before.rows[0].explicit_accepts == after.rows[0].explicit_accepts == 1
    assert before.rows[0].completions == after.rows[0].completions == 1
    assert (
        after.repository_skipped["attribution_edges"]["repository_identity_unresolved"]
        == 1
    )


def test_complete_ci_population_precedes_commit_and_model_narrowing():
    call, row, push, observation, ci = _evidence()
    sibling = CIOutcome.model_validate(
        {
            **ci.model_dump(),
            "outcome_id": "conflict",
            "repository_id": "202",
            "commit_sha": "c" * 40,
            "run_attempt": 2,
            "result": "failed",
        }
    )
    context = _context(push, observation, ci, sibling)
    for population in ([ci, sibling], [sibling, ci]):
        report = build_model_report_result(
            [call],
            [row],
            repository_context=context,
            ci_population=population,
            include_trends=True,
        )
        assert report.rows[0].attributed_inference_calls == 1
        assert report.rows[0].ci_linked == report.signal_funnel[0].ci_linked == 0
        assert report.ci_skipped == {"runs": {"conflicting_run_identity": 1}}
        assert (
            sum(
                window.denominator
                for trend in report.trends
                if trend.metric == "ci_pass_rate"
                for window in trend.windows
            )
            == 0
        )


def test_one_commit_cannot_gain_different_verdicts_from_model_local_artifacts():
    first = _evidence(suffix="a", model="model-a")
    second = _evidence(suffix="b", model="model-b")
    failed = CIOutcome.model_validate({**second[4].model_dump(), "result": "failed"})
    second_row = replace(second[1], ci_outcomes=[failed])
    context = _context(*first[2:], *second[2:4], failed)
    report = build_model_report_result(
        [first[0], second[0]], [first[1], second_row], repository_context=context
    )
    assert [row.ci_linked for row in report.rows] == [0, 0]
    assert report.ci_skipped == {"commits": {"ambiguous_workflow_verdicts": 1}}


def test_nested_model_panels_resolve_declared_ci_generator_once(monkeypatch):
    from sediment_export import outcome_report

    call, row, push, observation, ci = _evidence()
    context = _context(push, observation, ci)
    shared = outcome_report.derive_ci_resolution_result
    evaluations = []

    def record(population, *args, **kwargs):
        evaluations.append(tuple(population))
        return shared(evaluations[-1], *args, **kwargs)

    monkeypatch.setattr(outcome_report, "derive_ci_resolution_result", record)
    report = build_model_report_result(
        [call],
        [row, replace(row, file_path="other.py")],
        repository_context=context,
        ci_population=(fact for fact in [ci]),
        include_trends=True,
    )
    assert report.rows[0].ci_linked == 1
    assert evaluations == [(ci,)]
