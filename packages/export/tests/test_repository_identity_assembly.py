# SPDX-License-Identifier: AGPL-3.0-or-later
"""Canonical assembly keeps captured repository lifetimes and source anchors."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sediment_core import CIOutcome, CIResult, Push, SessionCommitObservation
from sediment_derive import (
    AbandonmentResult,
    Attribution,
    AttributionSource,
    MirrorManager,
    Provenance,
    read_repository_context,
)
from sediment_derive.repository_identity import repository_identity_of
from sediment_export.attributed_completions import (
    assemble_attributed_completions_result,
    session_commit_observation_ids,
)

ORG = "identity-assembly"
AT = datetime(2026, 9, 12, tzinfo=UTC)
SHA = "a" * 40
IDENTITY = dict(
    repository_provider="github", repository_host="github.com", repository_id="101"
)


def _population(store):
    push = Push(
        org_id=ORG,
        push_id="source",
        provider="github",
        repo="acme/old",
        ref="refs/heads/main",
        before_sha="0" * 40,
        after_sha=SHA,
        clone_url="https://github.com/acme/old.git",
        captured_at=AT,
        **IDENTITY,
    )
    outcome = CIOutcome(
        org_id=ORG,
        outcome_id="same-lifetime",
        provider="github_actions",
        repo="acme/new",
        commit_sha=SHA,
        branch="main",
        run_id="run-1",
        result="passed",
        captured_at=AT,
        **IDENTITY,
    )
    other = outcome.model_copy(
        update={
            "outcome_id": "different-lifetime",
            "run_id": "run-2",
            "repository_id": "202",
            "result": CIResult.FAILED,
        }
    )
    observation = SessionCommitObservation(
        org_id=ORG,
        observation_id="original-observation",
        repo=push.repo,
        commit_sha=SHA,
        session_id="session",
        source_push_id=push.push_id,
        captured_at=AT,
        **IDENTITY,
    )
    store.store_push(push)
    for fact in (outcome, other):
        store.store_ci_outcome(fact)
    store.store_session_commit_observation(observation)
    with store.read_snapshot() as snapshot:
        context = read_repository_context(snapshot, ORG, as_of=AT)
    attribution = Attribution(
        org_id=ORG,
        repo="acme/old",
        commit_sha=SHA,
        session_id="session",
        inference_call_id="call",
        file_path="file.py",
        similarity_score=1.0,
        attribution_source=AttributionSource.GIT_NOTES,
        provenance=Provenance(policy_version="1", quarantine_revision=0),
        repository_identity=repository_identity_of(push),
        source_push_id=push.push_id,
    )
    return attribution, outcome, other, observation, context


def _assemble(store, tmp_path, attribution, outcomes, observation, **kwargs):
    return assemble_attributed_completions_result(
        store,
        MirrorManager(str(tmp_path / "mirrors")),
        ORG,
        attributions=[attribution],
        completions=[],
        decisions=[],
        edit_observations=[],
        ci_outcomes=outcomes,
        session_commit_observations=[observation],
        abandonment=AbandonmentResult(),
        as_of=AT,
        **kwargs,
    )


def test_assembly_joins_renamed_ci_and_preserves_exact_sources(
    tmp_path, postgres_store
):
    attribution, outcome, other, observation, context = _population(postgres_store)
    result = _assemble(
        postgres_store,
        tmp_path,
        attribution,
        [outcome, other],
        observation,
        repository_context=context,
    )
    [row] = result.rows
    assert row.repository_identity == attribution.repository_identity
    assert row.source_push_id == "source"
    assert row.repo == "acme/new"
    assert row.ci_outcomes == [outcome]
    assert row.session_commit_observations == (observation,)
    assert session_commit_observation_ids(row, repository_context=context) == (
        observation.observation_id,
    )
    assert (
        _assemble(
            postgres_store,
            tmp_path,
            attribution,
            [other, outcome],
            observation,
            repository_context=context,
        )
        == result
    )


@pytest.mark.parametrize("source", [None, "absent"])
def test_assembly_declines_identified_attribution_without_exact_push(
    tmp_path, postgres_store, source
):
    attribution, outcome, _, observation, context = _population(postgres_store)
    result = _assemble(
        postgres_store,
        tmp_path,
        replace(attribution, source_push_id=source),
        [outcome],
        observation,
        repository_context=context,
    )
    assert result.rows == []


def test_preloaded_identified_assembly_requires_declared_context(
    tmp_path, postgres_store
):
    attribution, outcome, _, observation, _ = _population(postgres_store)
    result = _assemble(postgres_store, tmp_path, attribution, [outcome], observation)
    assert result.rows == []


def test_verified_sft_uses_renamed_ci_and_keeps_repository_identity(
    tmp_path, postgres_store
):
    from export_factories import inference_call, message
    from sediment_export.sft import SFTPolicy, project_sft

    attribution, outcome, other, observation, context = _population(postgres_store)
    row = _assemble(
        postgres_store,
        tmp_path,
        attribution,
        [outcome, other],
        observation,
        repository_context=context,
    ).rows[0]
    call = inference_call(
        "call",
        org_id=ORG,
        session_id="session",
        input_messages=[message("user", "write code")],
        observed_at=AT,
    )
    result = project_sft(
        [row],
        {"call": call},
        SFTPolicy(recipe_id="sft_verified"),
        repository_context=context,
    )
    [sample] = result.rows
    assert sample.metadata.repository_identity == row.repository_identity
    assert sample.metadata.session_commit_observation_ids == (
        observation.observation_id,
    )
    assert sample.metadata.eligibility_source == "resolved_ci_pass"


def test_verified_dpo_retains_independent_repository_lifetimes(
    tmp_path, postgres_store
):
    from export_factories import inference_call, message
    from sediment_derive.repository_identity import repository_identity_of
    from sediment_export.dpo import DPOPolicy, project_dpo

    attribution, outcome, other, observation, context = _population(postgres_store)
    chosen = _assemble(
        postgres_store,
        tmp_path,
        attribution,
        [outcome],
        observation,
        repository_context=context,
    ).rows[0]
    # Cross-repository pairs remain valid: the prompt/model bucket is unchanged.
    rejected = replace(
        chosen,
        inference_call_id="rejected",
        repo=other.repo,
        repository_identity=repository_identity_of(other),
        source_push_id=None,
        ci_outcomes=[other],
        session_commit_observations=(),
    )
    calls = {
        key: inference_call(
            key,
            org_id=ORG,
            session_id="session",
            input_messages=[message("user", "write code")],
            observed_at=AT,
            output="def add(a, b): return a + b"
            if key == "call"
            else "def add(a, b): return a - b",
        )
        for key in ("call", "rejected")
    }
    result = project_dpo(
        [chosen, rejected],
        calls,
        DPOPolicy(recipe_id="dpo_outcome"),
        repository_context=context,
    )
    [pair] = result.rows
    assert pair.metadata.chosen_repository_identity == chosen.repository_identity
    assert pair.metadata.rejected_repository_identity == rejected.repository_identity
    assert (
        pair.metadata.chosen_repository_identity
        != pair.metadata.rejected_repository_identity
    )
    assert pair.metadata.chosen_session_commit_observation_ids == (
        observation.observation_id,
    )
    assert pair.metadata.rejected_session_commit_observation_ids == ()
    assert (
        project_dpo(
            [rejected, chosen],
            calls,
            DPOPolicy(recipe_id="dpo_outcome"),
            repository_context=context,
        )
        == result
    )


def test_explicit_repository_boundary_excludes_later_captured_decision(
    tmp_path, postgres_store
):
    from datetime import timedelta
    from export_factories import inference_call, message
    from sediment_core import DeveloperDecision

    attribution, outcome, _, observation, context = _population(postgres_store)
    call = inference_call(
        "call",
        org_id=ORG,
        session_id="session",
        input_messages=[message("user", "write code")],
        model_call_id="native",
        observed_at=AT,
    )
    decision = DeveloperDecision(
        org_id=ORG,
        session_id="session",
        agent_harness="codex",
        file_path="file.py",
        accepted=True,
        explicit=True,
        interaction_mode="agent",
        call_id="native",
        occurred_at=AT,
        captured_at=AT + timedelta(seconds=1),
    )
    postgres_store.store_inference_call(call)
    postgres_store.store_decision(decision)
    result = assemble_attributed_completions_result(
        postgres_store,
        MirrorManager(str(tmp_path / "mirrors")),
        ORG,
        attributions=[attribution],
        abandonment=AbandonmentResult(),
        repository_context=context,
    )
    assert result.rows[0].decisions == []


def test_dataset_diagnostics_reuse_declared_repository_context(
    tmp_path, postgres_store
):
    from export_factories import inference_call, message
    from sediment_export import DPOPolicy, SFTPolicy
    from sediment_export.dataset_diagnostics import (
        build_dataset_diagnostics,
        build_dpo_bucket_sparsity,
    )

    attribution, outcome, _, observation, context = _population(postgres_store)
    row = _assemble(
        postgres_store,
        tmp_path,
        attribution,
        [outcome],
        observation,
        repository_context=context,
    ).rows[0]
    call = inference_call(
        "call",
        org_id=ORG,
        session_id="session",
        input_messages=[message("user", "write code")],
        observed_at=AT,
    )
    calls = {"call": call}
    sparsity = build_dpo_bucket_sparsity(
        [row],
        calls,
        DPOPolicy(recipe_id="dpo_outcome"),
        repository_context=context,
    )
    assert sparsity.total_candidates == sparsity.singleton_candidates == 1
    report = build_dataset_diagnostics(
        [],
        [],
        attributed_completions=[row],
        inference_calls=calls,
        sft_policy=SFTPolicy(recipe_id="sft_verified", min_confidence=0.8),
        repository_context=context,
    )
    [floor] = report.confidence_floor_exclusions
    assert floor.otherwise_eligible_completions == floor.excluded_by_floor == 1
    assert floor.eligibility_source == "resolved_ci_pass"
    assert (
        build_dpo_bucket_sparsity(
            [row], calls, DPOPolicy(recipe_id="dpo_outcome")
        ).total_candidates
        == 0
    )


def test_assembly_exposes_context_at_its_complete_evidence_boundary(
    tmp_path, postgres_store
):
    from datetime import timedelta
    from sediment_core import DeveloperDecision

    _population(postgres_store)
    later = AT + timedelta(hours=2)
    postgres_store.store_decision(
        DeveloperDecision(
            org_id=ORG,
            session_id="later-session",
            call_id="unattached",
            file_path="late.py",
            agent_harness="codex",
            accepted=True,
            explicit=True,
            interaction_mode="agent",
            occurred_at=later,
            captured_at=later,
        )
    )
    result = assemble_attributed_completions_result(
        postgres_store,
        MirrorManager(str(tmp_path / "mirrors")),
        ORG,
        attributions=[],
        abandonment=AbandonmentResult(),
    )
    assert result.repository_context is not None
    assert result.repository_context.as_of == later
