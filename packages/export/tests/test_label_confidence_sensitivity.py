# SPDX-License-Identifier: AGPL-3.0-or-later
"""LabelConfidencePolicy sensitivity tests.

The sweep is a pure aggregation over already-assembled ``AttributedCompletion`` objects, so
these tests use real dataclass/Pydantic fact instances and no git.
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
from sediment_derive import AttributionSource, Provenance, SessionAbandonment

from sediment_export import build_label_confidence_sensitivity
from sediment_export.attributed_completions import AttributedCompletion

ORG = "acme-corp"
REPO = "acme-corp/backend-service"
NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


def _decision(
    *,
    accepted: bool,
    explicit: bool,
    agent_harness: AgentHarness = AgentHarness.CLAUDE_CODE,
    session_id: str = "sess-1",
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
        call_id="call-1",
        occurred_at=NOW,
    )


def _ci(
    result: CIResult,
    *,
    commit_sha: str,
    run_id: str | None = None,
    run_attempt: int | None = None,
) -> CIOutcome:
    return CIOutcome(
        org_id=ORG,
        provider=CIProvider.GITHUB_ACTIONS,
        run_id=run_id or f"run/{commit_sha}",
        run_attempt=run_attempt,
        repo=REPO,
        commit_sha=commit_sha,
        branch="main",
        result=result,
        run_url=f"run/{commit_sha}",
    )


def _attributed_completion(
    inference_call_id: str,
    *,
    decisions: list[DeveloperDecision] | None = None,
    ci_result: CIResult | None = CIResult.PASSED,
    commit_sha: str = "a" * 40,
) -> AttributedCompletion:
    ci_outcomes = [_ci(ci_result, commit_sha=commit_sha)] if ci_result else []
    return AttributedCompletion(
        org_id=ORG,
        session_id=f"sess-{inference_call_id}",
        inference_call_id=inference_call_id,
        repo=REPO,
        commit_sha=commit_sha,
        file_path=f"{inference_call_id}.py",
        similarity_score=1.0,
        attribution_source=AttributionSource.GIT_NOTES,
        decisions=decisions or [],
        ci_outcomes=ci_outcomes,
        provenance=Provenance(policy_version="1", quarantine_revision=0),
        split="train",
    )


def _abandoned_attributed_completion() -> AttributedCompletion:
    return AttributedCompletion(
        org_id=ORG,
        session_id="sess-abandoned",
        inference_call_id="abandoned",
        repo=None,
        commit_sha=None,
        file_path=None,
        similarity_score=None,
        attribution_source=None,
        decisions=[
            _decision(
                accepted=True,
                explicit=True,
                session_id="sess-abandoned",
            )
        ],
        ci_outcomes=[],
        provenance=Provenance(policy_version="3", quarantine_revision=0),
        split="train",
        abandonment=SessionAbandonment(
            org_id=ORG,
            session_id="sess-abandoned",
            accepted_decisions=1,
            explicit_accepted_decisions=1,
            last_decision_at=NOW,
            as_of=NOW,
            provenance=Provenance(policy_version="2", quarantine_revision=0),
        ),
    )


def test_abandonment_affects_reject_floor_but_never_sft_eligibility() -> None:
    rows = build_label_confidence_sensitivity(
        [_abandoned_attributed_completion()],
        sft_min_confidence=0.0,
        grids={"explicit_reject_confidence": (0.0, 0.2)},
    )

    assert [row.affected_attributed_completions for row in rows] == [1, 1]
    assert [row.sft_eligible_count for row in rows] == [0, 0]


def test_implicit_accept_sweep_stays_separate_from_curated_eligibility() -> None:
    attributed_completions = [
        _attributed_completion(
            "implicit-accept",
            decisions=[_decision(accepted=True, explicit=False)],
            commit_sha="a" * 40,
        ),
        _attributed_completion(
            "explicit-accept",
            decisions=[_decision(accepted=True, explicit=True)],
            commit_sha="b" * 40,
        ),
        _attributed_completion("no-decision", commit_sha="c" * 40),
        _attributed_completion(
            "implicit-reject",
            decisions=[_decision(accepted=False, explicit=False)],
            commit_sha="d" * 40,
        ),
        _attributed_completion(
            "explicit-reject",
            decisions=[_decision(accepted=False, explicit=True)],
            commit_sha="e" * 40,
        ),
    ]

    rows = build_label_confidence_sensitivity(
        attributed_completions,
        grids={"implicit_accept_multiplier": (0.8, 1.0, 1.1, 1.5)},
    )

    explicit_rows = [row for row in rows if row.eligibility_source == "explicit_accept"]
    ineligible_rows = [row for row in rows if row.eligibility_source is None]
    assert [row.sft_eligible_count for row in explicit_rows] == [1, 1, 1, 1]
    assert all(row.affected_attributed_completions == 0 for row in explicit_rows)
    assert [row.sft_eligible_count for row in ineligible_rows] == [0, 0, 0, 0]
    assert all(row.affected_attributed_completions == 1 for row in ineligible_rows)


def test_unaffected_branch_sweep_has_no_shift() -> None:
    attributed_completions = [
        _attributed_completion(
            "explicit-accept",
            decisions=[_decision(accepted=True, explicit=True)],
            commit_sha="a" * 40,
        ),
        _attributed_completion("no-decision", commit_sha="b" * 40),
    ]

    rows = build_label_confidence_sensitivity(
        attributed_completions,
        grids={"implicit_accept_multiplier": (1.0, 1.1, 1.5)},
    )

    assert len(rows) == 6
    assert all(row.affected_attributed_completions == 0 for row in rows)
    assert all(row.sft_eligible_delta == 0 for row in rows)
    assert all(row.all_mean_delta == pytest.approx(0.0) for row in rows)
    assert all(row.all_median_delta == pytest.approx(0.0) for row in rows)


def test_label_confidence_sensitivity_is_deterministic_under_input_order() -> None:
    attributed_completions = [
        _attributed_completion(
            "implicit-accept",
            decisions=[_decision(accepted=True, explicit=False)],
            commit_sha="a" * 40,
        ),
        _attributed_completion(
            "implicit-reject",
            decisions=[_decision(accepted=False, explicit=False)],
            commit_sha="b" * 40,
        ),
        _attributed_completion("no-decision", commit_sha="c" * 40),
    ]

    grids = {"baseline_confidence": (0.5, 0.6, 0.7)}
    assert build_label_confidence_sensitivity(
        attributed_completions, grids=grids
    ) == build_label_confidence_sensitivity(
        list(reversed(attributed_completions)), grids=grids
    )


def test_sensitivity_sweeps_ci_reliability_and_compares_policy_v3() -> None:
    commit_sha = "f" * 40
    attributed_completion = _attributed_completion(
        "retry",
        ci_result=None,
        commit_sha=commit_sha,
    )
    attributed_completion = attributed_completion.__class__(
        **{
            **attributed_completion.__dict__,
            "ci_outcomes": [
                _ci(
                    CIResult.FAILED,
                    commit_sha=commit_sha,
                    run_id="run/retry",
                    run_attempt=1,
                ),
                _ci(
                    CIResult.PASSED,
                    commit_sha=commit_sha,
                    run_id="run/retry",
                    run_attempt=2,
                ),
            ],
        }
    )

    [row] = build_label_confidence_sensitivity(
        [attributed_completion],
        sft_min_confidence=0.0,
        grids={"ci_resolution.suspected_flake_reliability": (0.5,)},
    )

    assert row.affected_attributed_completions == 1
    assert row.all_confidences.mean == pytest.approx(0.33)
    assert row.policy_v3_all_confidences.mean == pytest.approx(0.66)
    assert row.all_mean_delta_from_policy_v3 == pytest.approx(-0.33)
    assert row.eligibility_source is None
    assert row.policy_v3_sft_eligible_count == 0
