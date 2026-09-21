# SPDX-License-Identifier: AGPL-3.0-or-later
"""Repository context reaches diagnostic CI and Confidence interpretation."""

from datetime import timedelta

import pytest

from sediment_core import CIOutcome, DeveloperDecision, FactTable, RepositoryRename
from sediment_derive import read_repository_context
from sediment_export import (
    LabelConfidencePolicy,
    build_decision_latency_report,
    build_label_confidence_inspection,
    build_label_confidence_sensitivity,
    generate_decision_latency_report,
    generate_label_confidence_inspection,
    generate_label_confidence_sensitivity,
    stratified_sample,
)
from sediment_export.attributed_completions import assemble_attributed_completions
from sediment_export.sft import SFTPolicy
from test_identity_export_review_regressions import _alias_scenario
from test_derived_bundle import ORG, T0

POLICY = LabelConfidencePolicy(ci_pass_multiplier=1.2)
SFT = SFTPolicy(recipe_id="sft_verified", label_confidence=POLICY)


def _scenario(tmp_path, store):
    bundle, mirrors, _, push, ci, observation = _alias_scenario(tmp_path, store)
    call = bundle.inference_calls[0]
    decision = DeveloperDecision(
        org_id=ORG,
        session_id=call.session_id,
        decision_id="late-decision",
        agent_harness="codex",
        accepted=True,
        explicit=False,
        interaction_mode="agent",
        file_path="first.py",
        call_id=call.model_call_id,
        occurred_at=T0 + timedelta(seconds=2),
        captured_at=T0 + timedelta(seconds=2),
    )
    store.store_decision(decision)
    store.store_repository_rename(
        RepositoryRename(
            org_id=ORG,
            repository_provider="github",
            repository_host="github.com",
            repository_id="101",
            old_repo="acme/old",
            new_repo="acme/new",
            captured_at=T0,
        )
    )
    return mirrors, call, push, ci, observation


def _generate(kind, store, mirrors):
    if kind == "inspection":
        return generate_label_confidence_inspection(
            store,
            mirrors,
            ORG,
            label_confidence_policy=POLICY,
            sft_policy=SFT,
        )
    if kind == "sensitivity":
        return generate_label_confidence_sensitivity(
            store,
            mirrors,
            ORG,
            label_confidence_policy=POLICY,
            sft_policy=SFT,
            grids={"ci_pass_multiplier": [1.2]},
        )
    return generate_decision_latency_report(
        store,
        mirrors,
        ORG,
        label_confidence_policy=POLICY,
    )


def _assert_result(kind, result, *, ci_available):
    confidence = 0.72 if ci_available else 0.6
    if kind == "inspection":
        assert len(result) == 2
        for row in result:
            assert row.repo == "acme/new"
            assert row.ci_bucket == ("ci_pass" if ci_available else "ci_absent")
            assert row.eligibility_source == (
                "resolved_ci_pass" if ci_available else None
            )
            assert row.confidence.final == pytest.approx(confidence)
            assert len(row.decisions) == 1
            if ci_available:
                assert row.ci_resolution.source_outcome_ids == ("two-file-ci",)
    elif kind == "sensitivity":
        [row] = result
        assert row.eligibility_source == ("resolved_ci_pass" if ci_available else None)
        assert row.affected_attributed_completions == (2 if ci_available else 0)
        assert row.sft_eligible_count == (2 if ci_available else 0)
        assert row.all_confidences.count == 2
        assert row.all_confidences.mean == pytest.approx(confidence)
    else:
        assert result.total_decisions == result.included_decisions == 1
        [bucket] = result.buckets
        assert bucket.mean_latency_ms == 2000
        assert bucket.confidence_count == 1
        assert bucket.mean_confidence == pytest.approx(confidence)


@pytest.mark.parametrize("kind", ["inspection", "sensitivity", "latency"])
@pytest.mark.parametrize("decline", ["quarantine", "conflicting_run"])
def test_generators_preserve_renamed_ci_and_late_decisions_then_decline_invalid_ci(
    tmp_path, postgres_store, kind, decline
):
    mirrors, _, _, ci, _ = _scenario(tmp_path, postgres_store)
    _assert_result(kind, _generate(kind, postgres_store, mirrors), ci_available=True)
    if decline == "quarantine":
        postgres_store.quarantine_fact(
            ORG, FactTable.CI_OUTCOMES, ci.outcome_id, reason="review"
        )
    else:
        postgres_store.store_ci_outcome(
            CIOutcome.model_validate(
                {
                    **ci.model_dump(),
                    "outcome_id": "other-lifetime-attempt",
                    "repository_id": "202",
                    "commit_sha": "b" * 40,
                    "run_attempt": 2,
                    "result": "failed",
                }
            )
        )
    _assert_result(kind, _generate(kind, postgres_store, mirrors), ci_available=False)


@pytest.mark.parametrize("kind", ["inspection", "sensitivity", "latency"])
def test_preloaded_diagnostic_builders_use_authoritative_context_and_keep_v3_comparison(
    tmp_path, postgres_store, kind
):
    mirrors, call, _, ci, _ = _scenario(tmp_path, postgres_store)
    with postgres_store.read_snapshot() as snapshot:
        context = read_repository_context(
            snapshot, ORG, as_of=T0 + timedelta(seconds=2)
        )
        rows = assemble_attributed_completions(
            snapshot, mirrors, ORG, repository_context=context
        )

    def build(context):
        if kind == "inspection":
            return build_label_confidence_inspection(
                [call], rows, policy=POLICY, sft_policy=SFT, repository_context=context
            )
        if kind == "sensitivity":
            return build_label_confidence_sensitivity(
                rows,
                label_confidence_policy=POLICY,
                sft_policy=SFT,
                grids={"ci_pass_multiplier": [1.2]},
                repository_context=context,
            )
        return build_decision_latency_report(
            [call], rows, policy=POLICY, repository_context=context
        )

    _assert_result(kind, build(context), ci_available=True)
    _assert_result(kind, build(None), ci_available=False)
    postgres_store.quarantine_fact(
        ORG, FactTable.CI_OUTCOMES, ci.outcome_id, reason="review"
    )
    with postgres_store.read_snapshot() as snapshot:
        unavailable = read_repository_context(snapshot, ORG, as_of=context.as_of)
    # Preserve the historical v3 comparison over the carried source Facts.
    result = build(unavailable)
    _assert_result(kind, result, ci_available=False)
    if kind == "inspection":
        assert all(
            row.ci_resolution_skips == {"repository_source_absent": 1} for row in result
        )
        assert all(row.policy_v3_confidence == pytest.approx(0.72) for row in result)
        assert all(
            row.confidence_delta_from_policy_v3 == pytest.approx(-0.12)
            for row in result
        )
    elif kind == "sensitivity":
        [row] = result
        assert row.policy_v3_all_confidences.mean == pytest.approx(0.72)
        assert row.all_mean_delta_from_policy_v3 == pytest.approx(-0.12)
    assert build(unavailable) == result
    rows.reverse()
    assert build(unavailable) == result


def test_stratified_sample_uses_identified_ci_cells(tmp_path, postgres_store):
    from dataclasses import replace

    mirrors, _, _, _, _ = _scenario(tmp_path, postgres_store)
    with postgres_store.read_snapshot() as snapshot:
        context = read_repository_context(
            snapshot, ORG, as_of=T0 + timedelta(seconds=2)
        )
        present = assemble_attributed_completions(
            snapshot, mirrors, ORG, repository_context=context
        )[0]
    absent = [
        replace(present, file_path=name, ci_outcomes=[]) for name in ("a.py", "b.py")
    ]
    sample = stratified_sample(
        [*absent, present], n=2, sft_policy=SFT, repository_context=context
    )
    assert sample == [absent[0], present]
