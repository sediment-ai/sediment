# SPDX-License-Identifier: AGPL-3.0-or-later
"""Canonical assembly resolves complete CI lineages before selecting commits."""

from datetime import timedelta
from dataclasses import replace

import pytest

from sediment_core import CIOutcome
from sediment_derive import read_repository_context
from sediment_export.attributed_completions import (
    assemble_attributed_completion_result,
    assemble_attributed_completions_result,
)
from sediment_export.sft import project_sft, SFTPolicy
from sediment_export.derived_bundle import (
    build_derived_bundle,
    read_derived_bundle,
    write_derived_bundle,
    validate_derived_bundle,
)
from sediment_export.label_confidence import resolve_ci_resolution
from test_identity_export_review_regressions import _alias_scenario, _attribution
from test_derived_bundle import ORG, T0, _scenario, _inference_call, REPO


def _context(store):
    with store.read_snapshot() as snapshot:
        return read_repository_context(snapshot, ORG)


def _facts(bundle):
    return [_attribution(row, row.repo) for row in bundle.attributed_completions]


@pytest.mark.parametrize("selection", ["all", "selected", "preloaded_population"])
def test_complete_conflicting_run_cannot_become_a_verified_training_pass(
    tmp_path, postgres_store, selection
):
    bundle, mirrors, _, _, outcome, _ = _alias_scenario(tmp_path, postgres_store)
    conflicting = CIOutcome.model_validate(
        {
            **outcome.model_dump(),
            "outcome_id": "conflicting-attempt",
            "repository_id": "202",
            "commit_sha": "b" * 40,
            "result": "failed",
            "run_attempt": 2,
        }
    )
    postgres_store.store_ci_outcome(conflicting)
    context = _context(postgres_store)
    if selection == "all":
        result = assemble_attributed_completion_result(
            postgres_store, mirrors, ORG, repository_context=context
        )
        rows = result.attributed_completions
    else:
        kwargs = (
            {"ci_population": (conflicting, outcome)}
            if selection == "preloaded_population"
            else {}
        )
        result = assemble_attributed_completions_result(
            postgres_store,
            mirrors,
            ORG,
            attributions=_facts(bundle),
            ci_outcomes=[outcome],
            repository_context=context,
            **kwargs,
        )
        rows = result.rows
    assert len(rows) == 2
    assert all(row.ci_outcomes == [] for row in rows)
    assert result.skipped["conflicting_run_identity"] == 1
    projected = project_sft(
        rows,
        {call.inference_call_id: call for call in bundle.inference_calls},
        SFTPolicy(recipe_id="sft_verified"),
        repository_context=context,
    )
    assert projected.rows == []
    assert len(postgres_store.read_ci_outcomes(ORG)) == 2


def test_selected_success_keeps_earlier_failed_attempt_and_non_verdict(
    tmp_path, postgres_store
):
    bundle, mirrors, _, _, success, _ = _alias_scenario(tmp_path, postgres_store)
    # The existing attempt is legacy attempt 0; preserve original Facts and IDs.
    failed = CIOutcome.model_validate(
        {
            **success.model_dump(),
            "outcome_id": "failed-attempt",
            "run_attempt": 1,
            "result": "failed",
        }
    )
    passed = CIOutcome.model_validate(
        {**success.model_dump(), "outcome_id": "passed-attempt", "run_attempt": 3}
    )
    timeout = CIOutcome.model_validate(
        {
            **success.model_dump(),
            "outcome_id": "timeout-attempt",
            "run_attempt": 2,
            "result": "timed_out",
        }
    )
    for item in (failed, passed, timeout):
        postgres_store.store_ci_outcome(item)
    context = _context(postgres_store)
    result = assemble_attributed_completions_result(
        postgres_store,
        mirrors,
        ORG,
        attributions=_facts(bundle),
        ci_outcomes=[passed],
        ci_population=(timeout, passed, success, failed),
        repository_context=context,
        policy_digest=bundle.policy.digest,
    )
    assert result.skipped == {}
    assert all(
        {item.outcome_id for item in row.ci_outcomes}
        == {item.outcome_id for item in (success, failed, passed, timeout)}
        for row in result.rows
    )
    assert (
        project_sft(
            result.rows,
            {call.inference_call_id: call for call in bundle.inference_calls},
            SFTPolicy(recipe_id="sft_verified"),
            repository_context=context,
        ).rows
        == []
    )
    rebuilt = build_derived_bundle(postgres_store, mirrors, ORG)
    restored = read_derived_bundle(
        write_derived_bundle(
            replace(rebuilt, attributed_completions=tuple(result.rows)),
            tmp_path / "complete-attempts",
        )
    )
    verified = validate_derived_bundle(restored)
    for row in restored.attributed_completions:
        resolution = resolve_ci_resolution(row, repository_context=verified)
        assert resolution.suspected_flake and resolution.reliability == 0.0
        assert set(resolution.source_outcome_ids) == {
            item.outcome_id for item in (success, failed, passed, timeout)
        }
    assert (
        project_sft(
            restored.attributed_completions,
            {call.inference_call_id: call for call in restored.inference_calls},
            SFTPolicy(recipe_id="sft_verified"),
            repository_context=verified,
        ).rows
        == []
    )


@pytest.mark.parametrize("changed", ["result", "repo", "time", "raw"])
@pytest.mark.parametrize("supplied_population", [False, True])
def test_selected_same_id_contradiction_rejects_without_substitution(
    tmp_path, postgres_store, changed, supplied_population
):
    bundle, mirrors, context, _, outcome, _ = _alias_scenario(tmp_path, postgres_store)
    changes = {
        "result": {"result": "failed"},
        "repo": {"repo": "acme/unproved"},
        "time": {"captured_at": T0 + timedelta(seconds=1)},
        "raw": {"raw": {"changed": True}},
    }[changed]
    selected = CIOutcome.model_validate({**outcome.model_dump(), **changes})
    kwargs = {"ci_population": [outcome]} if supplied_population else {}
    with pytest.raises(ValueError, match="CI.*population"):
        assemble_attributed_completions_result(
            postgres_store,
            mirrors,
            ORG,
            attributions=_facts(bundle),
            ci_outcomes=[selected],
            repository_context=context,
            **kwargs,
        )


@pytest.mark.parametrize("empty", ["selected", "population"])
def test_explicit_empty_ci_inputs_remain_authoritative(tmp_path, postgres_store, empty):
    bundle, mirrors, context, _, outcome, _ = _alias_scenario(tmp_path, postgres_store)
    kwargs = (
        {"ci_outcomes": [], "ci_population": [outcome]}
        if empty == "selected"
        else {"ci_population": []}
    )
    result = assemble_attributed_completions_result(
        postgres_store,
        mirrors,
        ORG,
        attributions=_facts(bundle),
        repository_context=context,
        **kwargs,
    )
    assert len(result.rows) == 2 and all(row.ci_outcomes == [] for row in result.rows)
    assert result.skipped == {}


def test_declared_legacy_full_ci_population_retains_original_fact(
    tmp_path, postgres_store
):
    call = _inference_call(
        "legacy", "alice", inference_call_id="legacy-call", captured_at=T0
    )
    _, mirrors = _scenario(tmp_path, postgres_store, [call])
    [push] = postgres_store.read_pushes(ORG)
    outcome = CIOutcome(
        org_id=ORG,
        outcome_id="declared",
        provider="github_actions",
        repo=REPO,
        commit_sha=push.after_sha,
        branch="main",
        run_id="declared-run",
        result="passed",
        captured_at=push.captured_at,
        raw={"captured": [1, "original"]},
    )
    assert postgres_store.read_ci_outcomes(ORG) == []
    result = assemble_attributed_completions_result(
        postgres_store, mirrors, ORG, ci_population=[outcome]
    )
    assert len(result.rows) == 1
    assert result.rows[0].ci_outcomes == [outcome]
    assert result.rows[0].ci_outcomes[0] is outcome


def test_future_selected_source_cannot_select_an_earlier_commit(
    tmp_path, postgres_store
):
    bundle, mirrors, context, _, outcome, _ = _alias_scenario(tmp_path, postgres_store)
    future = CIOutcome.model_validate(
        {
            **outcome.model_dump(),
            "outcome_id": "future-ci",
            "run_id": "future-run",
            "captured_at": T0 + timedelta(seconds=1),
        }
    )
    postgres_store.store_ci_outcome(future)
    result = assemble_attributed_completions_result(
        postgres_store,
        mirrors,
        ORG,
        attributions=_facts(bundle),
        ci_outcomes=[future],
        ci_population=[outcome, future],
        repository_context=context,
    )
    assert all(row.ci_outcomes == [] for row in result.rows)
    assert result.skipped == {}


def test_declared_ci_population_is_bounded_before_consumption(
    tmp_path, postgres_store, monkeypatch
):
    from sediment_core import OperationalReportLimitExceeded
    from sediment_export import attributed_completions as owner

    bundle, mirrors, context, _, outcome, _ = _alias_scenario(tmp_path, postgres_store)
    monkeypatch.setattr(owner, "REPOSITORY_IDENTITY_LIMIT", 2)
    consumed = 0

    def population():
        nonlocal consumed
        while True:
            consumed += 1
            yield outcome

    with pytest.raises(OperationalReportLimitExceeded, match="CI population"):
        assemble_attributed_completions_result(
            postgres_store,
            mirrors,
            ORG,
            attributions=_facts(bundle),
            ci_population=population(),
            repository_context=context,
        )
    assert consumed == 3


def test_selected_source_missing_from_declared_population_rejects(
    tmp_path, postgres_store
):
    bundle, mirrors, context, _, outcome, _ = _alias_scenario(tmp_path, postgres_store)
    with pytest.raises(ValueError, match="CI.*population"):
        assemble_attributed_completions_result(
            postgres_store,
            mirrors,
            ORG,
            attributions=_facts(bundle),
            ci_outcomes=[outcome],
            ci_population=[],
            repository_context=context,
        )
