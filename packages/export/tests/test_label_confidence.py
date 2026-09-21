# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Confidence-resolution tests — real ``AttributedCompletion``/``DeveloperDecision``/
``CIOutcome`` instances (per AGENTS.md: never mocked), never real git (that's
``test_attributed_completions.py``'s job; ``resolve_confidence`` is a pure function of a
``AttributedCompletion``'s already-assembled fields, so a hand-built ``AttributedCompletion`` is the
right level of isolation here).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sediment_core import (
    CIOutcome,
    CIProvider,
    CIResult,
    AgentHarness,
    DeveloperDecision,
    InteractionMode,
)
from sediment_derive import (
    AttributionSource,
    CIResolutionPolicy,
    Provenance,
    SessionAbandonment,
)

from sediment_export import (
    LabelConfidencePolicy,
    LabelConfidenceSettings,
    ci_failed,
    ci_passed,
    decision_branch,
    resolve_confidence,
    resolve_confidence_breakdown,
)
from sediment_export.label_confidence import resolve_policy_v3_confidence
from sediment_export.attributed_completions import AttributedCompletion

ORG = "acme-corp"
REPO = "acme-corp/backend-service"
SHA = "a" * 40


def _decision(
    *,
    accepted: bool,
    explicit: bool,
    agent_harness: AgentHarness = AgentHarness.CLAUDE_CODE,
    call_id: str = "call-1",
    edit_retention_score: float | None = None,
) -> DeveloperDecision:
    return DeveloperDecision(
        org_id=ORG,
        session_id="sess-1",
        user_id="dev",
        agent_harness=agent_harness,
        file_path="a.py",
        accepted=accepted,
        explicit=explicit,
        interaction_mode=InteractionMode.AGENT,
        call_id=call_id,
        edit_retention_score=edit_retention_score,
        occurred_at=datetime.now(UTC),
    )


def _ci(
    result: CIResult,
    *,
    run: str = "run/1",
    run_attempt: int | None = None,
    workflow_id: str | None = "workflow-1",
    workflow_name: str = "CI",
    workflow_path: str | None = ".github/workflows/ci.yml",
) -> CIOutcome:
    return CIOutcome(
        org_id=ORG,
        provider=CIProvider.GITHUB_ACTIONS,
        run_id=run,
        run_attempt=run_attempt,
        repo=REPO,
        commit_sha=SHA,
        branch="main",
        result=result,
        workflow_id=workflow_id,
        workflow_name=workflow_name,
        workflow_path=workflow_path,
        run_url=run,
    )


def _attributed_completion(
    *,
    decisions: list[DeveloperDecision] = (),
    ci_outcomes: list[CIOutcome] = (),
    attribution_source: AttributionSource = AttributionSource.GIT_NOTES,
    similarity_score: float = 1.0,
) -> AttributedCompletion:
    return AttributedCompletion(
        org_id=ORG,
        session_id="sess-1",
        inference_call_id="comp-1",
        repo=REPO,
        commit_sha=SHA,
        file_path="a.py",
        similarity_score=similarity_score,
        attribution_source=attribution_source,
        decisions=list(decisions),
        ci_outcomes=list(ci_outcomes),
        provenance=Provenance(policy_version="1", quarantine_revision=0),
        split="train",
    )


def _abandoned_completion() -> AttributedCompletion:
    now = datetime.now(UTC)
    return AttributedCompletion(
        org_id=ORG,
        session_id="sess-1",
        inference_call_id="comp-abandoned",
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
            last_decision_at=now,
            as_of=now,
            provenance=Provenance(policy_version="2", quarantine_revision=0),
        ),
    )


def test_abandonment_precedes_the_attached_explicit_accept() -> None:
    policy = LabelConfidencePolicy(explicit_reject_confidence=0.15)
    abandoned = _abandoned_completion()

    assert decision_branch(abandoned) == "abandoned"
    assert resolve_confidence(abandoned, policy) == pytest.approx(0.15)
    breakdown = resolve_confidence_breakdown(abandoned, policy)
    assert breakdown is not None
    assert breakdown.decision_factor == pytest.approx(0.15)
    assert breakdown.ci_factor == pytest.approx(1.0)
    assert breakdown.ci_reliability == pytest.approx(1.0)
    assert breakdown.similarity_discount == pytest.approx(1.0)
    assert breakdown.final == pytest.approx(0.15)


def test_label_confidence_policy_version_covers_attempt_resolution() -> None:
    assert LabelConfidencePolicy().policy_version == "4"


def test_no_decision_and_no_ci_resolves_none() -> None:
    assert resolve_confidence(_attributed_completion()) is None


def test_explicit_accept_resolves_near_one() -> None:
    t = _attributed_completion(decisions=[_decision(accepted=True, explicit=True)])
    assert resolve_confidence(t) == pytest.approx(1.0)


def test_explicit_reject_resolves_to_the_floor_regardless_of_ci() -> None:
    t = _attributed_completion(
        decisions=[_decision(accepted=False, explicit=True)],
        ci_outcomes=[_ci(CIResult.PASSED)],
    )
    assert resolve_confidence(t) == pytest.approx(0.0)


def test_explicit_reject_wins_over_a_sibling_explicit_accept_on_one_attributed_completion() -> (
    None
):
    t = _attributed_completion(
        decisions=[
            _decision(accepted=True, explicit=True, call_id="c1"),
            _decision(accepted=False, explicit=True, call_id="c2"),
        ]
    )
    assert resolve_confidence(t) == pytest.approx(0.0)


def test_explicit_accept_with_ci_fail_still_combines_multiplicatively() -> None:
    policy = LabelConfidencePolicy()
    t = _attributed_completion(
        decisions=[_decision(accepted=True, explicit=True)],
        ci_outcomes=[_ci(CIResult.FAILED)],
    )
    expected = policy.explicit_accept_confidence * policy.ci_fail_multiplier
    assert resolve_confidence(t, policy) == pytest.approx(expected)


def test_implicit_accept_is_a_smaller_bonus_than_explicit_and_stays_capped() -> None:
    policy = LabelConfidencePolicy()
    t = _attributed_completion(decisions=[_decision(accepted=True, explicit=False)])
    expected = min(1.0, policy.baseline_confidence * policy.implicit_accept_multiplier)
    assert resolve_confidence(t, policy) == pytest.approx(expected)
    assert resolve_confidence(t, policy) < resolve_confidence(
        _attributed_completion(decisions=[_decision(accepted=True, explicit=True)]),
        policy,
    )


def test_implicit_reject_is_a_penalty_not_a_hard_veto() -> None:
    policy = LabelConfidencePolicy()
    t = _attributed_completion(decisions=[_decision(accepted=False, explicit=False)])
    expected = policy.baseline_confidence * policy.implicit_reject_multiplier
    resolved = resolve_confidence(t, policy)
    assert resolved == pytest.approx(expected)
    assert resolved > 0.0  # a penalty, not the explicit-reject floor


def test_codex_implicit_accept_is_neutral_not_a_bonus() -> None:
    # Codex emits tool_decision only for actions it runs (no interactive
    # reject, no revert/undo channel), so an implicit accept from Codex
    # carries none of the "survived without revert" evidence the general
    # implicit-accept bonus assumes. It should resolve to a neutral 1.0
    # multiplier on baseline_confidence, not implicit_accept_multiplier.
    policy = LabelConfidencePolicy()
    t = _attributed_completion(
        decisions=[
            _decision(accepted=True, explicit=False, agent_harness=AgentHarness.CODEX)
        ]
    )
    assert resolve_confidence(t, policy) == pytest.approx(policy.baseline_confidence)


def test_codex_explicit_accept_gets_the_normal_explicit_bonus() -> None:
    # A Codex decision with explicit=True is a reviewed diff approval — a
    # real human gesture, same strength as any other source's explicit
    # accept. The Codex accept-only nuance only applies to implicit decisions.
    policy = LabelConfidencePolicy()
    t = _attributed_completion(
        decisions=[
            _decision(accepted=True, explicit=True, agent_harness=AgentHarness.CODEX)
        ]
    )
    assert resolve_confidence(t, policy) == pytest.approx(
        policy.explicit_accept_confidence
    )


def test_copilot_implicit_accept_is_unchanged_by_the_codex_nuance() -> None:
    # Regression guard: the Codex neutralization must not leak to other
    # implicit-accept sources.
    policy = LabelConfidencePolicy()
    t = _attributed_completion(
        decisions=[
            _decision(accepted=True, explicit=False, agent_harness=AgentHarness.COPILOT)
        ]
    )
    expected = min(1.0, policy.baseline_confidence * policy.implicit_accept_multiplier)
    assert resolve_confidence(t, policy) == pytest.approx(expected)


def test_claude_code_implicit_accept_is_unchanged_by_the_codex_nuance() -> None:
    # Regression guard: same as above, for the default source.
    policy = LabelConfidencePolicy()
    t = _attributed_completion(
        decisions=[
            _decision(
                accepted=True, explicit=False, agent_harness=AgentHarness.CLAUDE_CODE
            )
        ]
    )
    expected = min(1.0, policy.baseline_confidence * policy.implicit_accept_multiplier)
    assert resolve_confidence(t, policy) == pytest.approx(expected)


def test_edit_retention_one_matches_the_flat_implicit_accept_multiplier() -> None:
    policy = LabelConfidencePolicy()
    t = _attributed_completion(
        decisions=[_decision(accepted=True, explicit=False, edit_retention_score=1.0)]
    )
    expected = min(1.0, policy.baseline_confidence * policy.implicit_accept_multiplier)
    assert resolve_confidence(t, policy) == pytest.approx(expected)


def test_edit_retention_zero_matches_the_implicit_reject_baseline() -> None:
    policy = LabelConfidencePolicy()
    t = _attributed_completion(
        decisions=[_decision(accepted=True, explicit=False, edit_retention_score=0.0)]
    )
    expected = policy.baseline_confidence * policy.implicit_reject_multiplier
    assert resolve_confidence(t, policy) == pytest.approx(expected)


def test_edit_retention_half_is_the_exact_midpoint_interpolation() -> None:
    policy = LabelConfidencePolicy()
    t = _attributed_completion(
        decisions=[_decision(accepted=True, explicit=False, edit_retention_score=0.5)]
    )
    lerp_factor = (
        policy.implicit_reject_multiplier
        + (policy.implicit_accept_multiplier - policy.implicit_reject_multiplier) * 0.5
    )
    expected = policy.baseline_confidence * lerp_factor
    assert resolve_confidence(t, policy) == pytest.approx(expected)
    # Sanity: for the default policy this is also just the average of the
    # flat accept/reject multipliers.
    avg_factor = (
        policy.implicit_accept_multiplier + policy.implicit_reject_multiplier
    ) / 2
    assert resolve_confidence(t, policy) == pytest.approx(
        policy.baseline_confidence * avg_factor
    )


def test_edit_retention_none_falls_back_to_the_flat_multiplier_exactly() -> None:
    # Critical regression guard: an implicit accept with no edit_retention_score
    # (every non-Copilot source, and older Copilot events) must resolve to
    # the flat multiplier, exactly as it did before the interpolation.
    policy = LabelConfidencePolicy()
    t = _attributed_completion(
        decisions=[_decision(accepted=True, explicit=False, edit_retention_score=None)]
    )
    expected = min(1.0, policy.baseline_confidence * policy.implicit_accept_multiplier)
    assert resolve_confidence(t, policy) == pytest.approx(expected)


def test_codex_implicit_accept_with_edit_retention_stays_neutral() -> None:
    # The Codex accept-only neutralization is checked BEFORE the
    # edit_retention_score interpolation, so even a (hypothetical — Codex doesn't
    # populate this today) Codex edit_retention_score can't override the neutral
    # 1.0 multiplier outcome.
    policy = LabelConfidencePolicy()
    t = _attributed_completion(
        decisions=[
            _decision(
                accepted=True,
                explicit=False,
                agent_harness=AgentHarness.CODEX,
                edit_retention_score=1.0,
            )
        ]
    )
    assert resolve_confidence(t, policy) == pytest.approx(policy.baseline_confidence)


def test_low_edit_retention_does_not_trigger_the_reject_tie_break() -> None:
    # edit_retention_score is a strength signal on an accept that already happened,
    # not an accept/reject signal itself — it must never re-route a decision
    # into the reject branch, even at edit_retention_score == 0.0.
    policy = LabelConfidencePolicy()
    accept_low_retention = _attributed_completion(
        decisions=[_decision(accepted=True, explicit=False, edit_retention_score=0.0)]
    )
    explicit_reject = _attributed_completion(
        decisions=[_decision(accepted=False, explicit=True)]
    )
    assert resolve_confidence(accept_low_retention, policy) != resolve_confidence(
        explicit_reject, policy
    )
    assert resolve_confidence(accept_low_retention, policy) > 0.0


def test_ci_pass_raises_and_ci_fail_lowers_a_no_decision_attributed_completion() -> (
    None
):
    policy = LabelConfidencePolicy()
    passed = resolve_confidence(
        _attributed_completion(ci_outcomes=[_ci(CIResult.PASSED)]), policy
    )
    failed = resolve_confidence(
        _attributed_completion(ci_outcomes=[_ci(CIResult.FAILED)]), policy
    )
    neutral = policy.baseline_confidence
    assert passed == pytest.approx(neutral * policy.ci_pass_multiplier)
    assert failed == pytest.approx(neutral * policy.ci_fail_multiplier)
    assert failed < neutral < passed


@pytest.mark.parametrize(
    "non_verdict",
    [
        CIResult.ERROR,
        CIResult.TIMED_OUT,
        CIResult.CANCELLED,
        CIResult.SKIPPED,
        CIResult.NEUTRAL,
        CIResult.UNKNOWN,
    ],
)
def test_non_verdict_ci_does_not_move_confidence(non_verdict: CIResult) -> None:
    policy = LabelConfidencePolicy()
    t = _attributed_completion(
        decisions=[_decision(accepted=True, explicit=False)],
        ci_outcomes=[_ci(non_verdict)],
    )
    expected = min(1.0, policy.baseline_confidence * policy.implicit_accept_multiplier)
    assert resolve_confidence(t, policy) == pytest.approx(expected)


def test_numbered_retry_verdict_and_reliability_are_separate_factors() -> None:
    t = _attributed_completion(
        ci_outcomes=[
            _ci(CIResult.FAILED, run_attempt=1),
            _ci(CIResult.PASSED, run_attempt=2),
        ]
    )
    assert ci_passed(t) is True
    assert ci_failed(t) is False
    breakdown = resolve_confidence_breakdown(t)
    assert breakdown is not None
    assert breakdown.ci_factor == pytest.approx(
        LabelConfidencePolicy().ci_pass_multiplier
    )
    assert breakdown.ci_reliability == pytest.approx(0.0)
    assert breakdown.final == pytest.approx(0.0)


def test_conflicting_workflow_verdicts_create_no_directional_ci_label() -> None:
    t = _attributed_completion(
        ci_outcomes=[
            _ci(CIResult.PASSED, run="run-lint"),
            _ci(
                CIResult.FAILED,
                run="run-tests",
                workflow_id="workflow-2",
                workflow_name="Tests",
                workflow_path=".github/workflows/tests.yml",
            ),
        ]
    )

    assert ci_passed(t) is False
    assert ci_failed(t) is False


def test_all_failed_ci_outcomes_resolve_as_failed() -> None:
    t = _attributed_completion(
        ci_outcomes=[
            _ci(CIResult.FAILED, run="run/1"),
            _ci(CIResult.FAILED, run="run/2"),
        ]
    )
    assert ci_passed(t) is False
    assert ci_failed(t) is True


def test_jaccard_attribution_discounts_by_similarity_score() -> None:
    policy = LabelConfidencePolicy()
    t = _attributed_completion(
        decisions=[_decision(accepted=True, explicit=True)],
        attribution_source=AttributionSource.JACCARD,
        similarity_score=0.5,
    )
    assert resolve_confidence(t, policy) == pytest.approx(
        policy.explicit_accept_confidence * 0.5
    )


def test_notes_attribution_is_never_discounted_by_similarity_score() -> None:
    policy = LabelConfidencePolicy()
    t = _attributed_completion(
        decisions=[_decision(accepted=True, explicit=True)],
        attribution_source=AttributionSource.GIT_NOTES,
        similarity_score=0.1,  # deliberately low; NOTES ignores it
    )
    assert resolve_confidence(t, policy) == pytest.approx(
        policy.explicit_accept_confidence
    )


def test_confidence_is_always_capped_to_one() -> None:
    policy = LabelConfidencePolicy(
        baseline_confidence=1.0, implicit_accept_multiplier=2.0, ci_pass_multiplier=2.0
    )
    t = _attributed_completion(
        decisions=[_decision(accepted=True, explicit=False)],
        ci_outcomes=[_ci(CIResult.PASSED)],
    )
    assert resolve_confidence(t, policy) == pytest.approx(1.0)


# resolve_confidence_breakdown shares its whole implementation with
# resolve_confidence (the latter is a thin `.final`-only wrapper) — these
# tests pin that the breakdown's three factors combine to exactly the same
# number the float-only path produces, across a scenario from each ladder
# branch.


def test_no_signal_breakdown_is_none_same_as_resolve_confidence() -> None:
    t = _attributed_completion()
    assert resolve_confidence_breakdown(t) is None
    assert resolve_confidence(t) is None


@pytest.mark.parametrize(
    "attributed_completion_factory",
    [
        lambda: _attributed_completion(
            decisions=[_decision(accepted=True, explicit=True)]
        ),
        lambda: _attributed_completion(
            decisions=[_decision(accepted=False, explicit=True)]
        ),
        lambda: _attributed_completion(
            decisions=[_decision(accepted=True, explicit=False)]
        ),
        lambda: _attributed_completion(
            decisions=[_decision(accepted=False, explicit=False)]
        ),
        lambda: _attributed_completion(
            decisions=[
                _decision(
                    accepted=True, explicit=False, agent_harness=AgentHarness.CODEX
                )
            ]
        ),
        lambda: _attributed_completion(ci_outcomes=[_ci(CIResult.PASSED)]),
        lambda: _attributed_completion(ci_outcomes=[_ci(CIResult.FAILED)]),
        lambda: _attributed_completion(
            decisions=[_decision(accepted=True, explicit=True)],
            ci_outcomes=[_ci(CIResult.FAILED)],
        ),
        lambda: _attributed_completion(
            decisions=[_decision(accepted=True, explicit=True)],
            attribution_source=AttributionSource.JACCARD,
            similarity_score=0.5,
        ),
        lambda: _attributed_completion(
            decisions=[_decision(accepted=True, explicit=True)],
            ci_outcomes=[_ci(CIResult.PASSED)],
            attribution_source=AttributionSource.JACCARD,
            similarity_score=0.4,
        ),
    ],
)
def test_breakdown_factors_combine_to_the_same_float_resolve_confidence_returns(
    attributed_completion_factory,
) -> None:
    policy = LabelConfidencePolicy()
    t = attributed_completion_factory()
    breakdown = resolve_confidence_breakdown(t, policy)
    assert breakdown is not None
    recombined = max(
        0.0,
        min(
            1.0,
            breakdown.decision_factor
            * breakdown.ci_factor
            * breakdown.ci_reliability
            * breakdown.similarity_discount,
        ),
    )
    assert recombined == pytest.approx(breakdown.final)
    assert breakdown.final == pytest.approx(resolve_confidence(t, policy))


def test_breakdown_reports_the_ladder_branch_taken() -> None:
    policy = LabelConfidencePolicy()
    t = _attributed_completion(
        decisions=[_decision(accepted=True, explicit=False)],
        ci_outcomes=[_ci(CIResult.FAILED)],
        attribution_source=AttributionSource.JACCARD,
        similarity_score=0.5,
    )
    breakdown = resolve_confidence_breakdown(t, policy)
    assert breakdown is not None
    assert breakdown.decision_factor == pytest.approx(
        policy.baseline_confidence * policy.implicit_accept_multiplier
    )
    assert breakdown.ci_factor == pytest.approx(policy.ci_fail_multiplier)
    assert breakdown.ci_reliability == pytest.approx(1.0)
    assert breakdown.similarity_discount == pytest.approx(0.5)


def test_breakdown_similarity_discount_is_neutral_for_notes() -> None:
    t = _attributed_completion(
        decisions=[_decision(accepted=True, explicit=True)],
        attribution_source=AttributionSource.GIT_NOTES,
        similarity_score=0.1,
    )
    breakdown = resolve_confidence_breakdown(t)
    assert breakdown is not None
    assert breakdown.similarity_discount == pytest.approx(1.0)


def test_policy_v3_comparison_preserves_prior_pass_anywhere_semantics() -> None:
    attributed_completion = _attributed_completion(
        decisions=[],
        ci_outcomes=[
            _ci(CIResult.FAILED, run_attempt=1),
            _ci(CIResult.PASSED, run_attempt=2),
        ],
    )

    assert resolve_confidence(attributed_completion) == pytest.approx(0.0)
    assert resolve_policy_v3_confidence(attributed_completion) == pytest.approx(0.66)


def test_breakdown_final_stays_capped_even_when_raw_factors_exceed_one() -> None:
    policy = LabelConfidencePolicy(
        baseline_confidence=1.0, implicit_accept_multiplier=2.0, ci_pass_multiplier=2.0
    )
    t = _attributed_completion(
        decisions=[_decision(accepted=True, explicit=False)],
        ci_outcomes=[_ci(CIResult.PASSED)],
    )
    breakdown = resolve_confidence_breakdown(t, policy)
    assert breakdown is not None
    # Raw factors are recorded uncapped...
    assert breakdown.decision_factor * breakdown.ci_factor > 1.0
    # ...but `final` is still capped, matching resolve_confidence.
    assert breakdown.final == pytest.approx(1.0)
    assert breakdown.final == pytest.approx(resolve_confidence(t, policy))


def test_label_confidence_policy_rejects_out_of_range_confidence_floors() -> None:
    with pytest.raises(ValueError):
        LabelConfidencePolicy(baseline_confidence=1.5)
    with pytest.raises(ValueError):
        LabelConfidencePolicy(explicit_accept_confidence=-0.1)


def test_label_confidence_policy_rejects_negative_multipliers() -> None:
    with pytest.raises(ValueError):
        LabelConfidencePolicy(ci_pass_multiplier=-1.0)


def test_label_confidence_policy_accepts_a_versioned_ci_resolution_policy() -> None:
    resolution = CIResolutionPolicy(suspected_flake_reliability=0.25)
    policy = LabelConfidencePolicy(ci_resolution=resolution)
    t = _attributed_completion(
        ci_outcomes=[
            _ci(CIResult.FAILED, run_attempt=1),
            _ci(CIResult.PASSED, run_attempt=2),
        ]
    )

    breakdown = resolve_confidence_breakdown(t, policy)

    assert breakdown is not None
    assert breakdown.ci_reliability == pytest.approx(0.25)


def test_label_confidence_settings_default_matches_policy_default() -> None:
    assert LabelConfidenceSettings().resolve() == LabelConfidencePolicy()


def test_label_confidence_settings_reads_its_environment_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SEDIMENT_LABEL_CONFIDENCE_CI_FAIL_MULTIPLIER", "0.25")
    monkeypatch.setenv("SEDIMENT_LABEL_CONFIDENCE_BASELINE_CONFIDENCE", "0.4")
    monkeypatch.setenv("SEDIMENT_LABEL_CONFIDENCE_SUSPECTED_FLAKE_RELIABILITY", "0.2")
    resolved = LabelConfidenceSettings().resolve()
    assert resolved.ci_fail_multiplier == pytest.approx(0.25)
    assert resolved.baseline_confidence == pytest.approx(0.4)
    assert resolved.ci_resolution.suspected_flake_reliability == pytest.approx(0.2)
    # Untouched knobs keep their LabelConfidencePolicy default.
    assert resolved.ci_pass_multiplier == pytest.approx(
        LabelConfidencePolicy().ci_pass_multiplier
    )
