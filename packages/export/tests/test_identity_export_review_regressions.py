# SPDX-License-Identifier: AGPL-3.0-or-later
"""Public assembly and diff-SFT retain qualified evidence and exact loss units."""

from dataclasses import replace
from datetime import timedelta
from itertools import permutations

import pytest

from sediment_core import CIOutcome, FactTable, Push, SessionCommitObservation
from sediment_derive import (
    Attribution,
    AttributionSource,
    MirrorManager,
    read_repository_context,
)
from sediment_export.attributed_completions import (
    assemble_attributed_completion_result,
    assemble_attributed_completions_result,
)
from sediment_export.derived_bundle import (
    build_derived_bundle,
    read_derived_bundle,
    write_derived_bundle,
    validate_derived_bundle,
)
from sediment_export.diff_sft import project_diff_sft
from sediment_export.sft import SFTPolicy
from test_derived_bundle import _identified_scenario, _inference_call, ORG, T0
from test_diff_sft import FIB, _work_repo, _commit, _make_remote


def _alias_scenario(tmp_path, store):
    work = _work_repo(tmp_path)
    (work / "README.md").write_text("base\n")
    base = _commit(work, "base")
    for name in ("first.py", "second.py"):
        (work / name).write_text(FIB)
    head = _commit(work, "two files")
    remote = _make_remote(tmp_path, work)
    identity = dict(
        repository_provider="github", repository_host="github.com", repository_id="101"
    )
    push = Push(
        org_id=ORG,
        push_id="two-file-push",
        provider="github",
        repo="acme/old",
        clone_url=str(remote),
        ref="refs/heads/main",
        before_sha=base,
        after_sha=head,
        captured_at=T0,
        **identity,
    )
    outcome = CIOutcome(
        org_id=ORG,
        outcome_id="two-file-ci",
        provider="github_actions",
        repo="acme/new",
        commit_sha=head,
        branch="main",
        run_id="two-file-run",
        result="passed",
        captured_at=T0,
        **identity,
    )
    observation = SessionCommitObservation(
        org_id=ORG,
        observation_id="two-file-observation",
        session_id="session-1",
        repo=push.repo,
        commit_sha=head,
        source_push_id=push.push_id,
        captured_at=T0,
        **identity,
    )
    call = _inference_call(
        "session-1", "alice", inference_call_id="two-file-call", captured_at=T0
    )
    for writer, fact in (
        ("store_push", push),
        ("store_ci_outcome", outcome),
        ("store_session_commit_observation", observation),
        ("store_inference_call", call),
    ):
        getattr(store, writer)(fact)
    with store.read_snapshot() as snapshot:
        context = read_repository_context(snapshot, ORG)
    mirrors = MirrorManager(str(tmp_path / "mirrors"))
    mirrors.ensure(push, repository_context=context)
    bundle = build_derived_bundle(store, mirrors, ORG)
    assert len(bundle.attributed_completions) == 2
    return bundle, mirrors, context, push, outcome, observation


def _attribution(row, repo):
    return Attribution(
        org_id=row.org_id,
        session_id=row.session_id,
        inference_call_id=row.inference_call_id,
        repo=repo,
        commit_sha=row.commit_sha,
        file_path=row.file_path,
        similarity_score=row.similarity_score,
        attribution_source=row.attribution_source,
        provenance=row.provenance,
        repository_identity=row.repository_identity,
        source_push_id=row.source_push_id,
    )


def test_one_unresolved_observation_is_counted_once_in_public_assembly(
    tmp_path, postgres_store
):
    _, mirrors, push, _, _, _ = _identified_scenario(tmp_path, postgres_store)
    later = Push.model_validate(
        {
            **push.model_dump(),
            "push_id": "later-push",
            "ref": "refs/heads/other",
            "captured_at": T0 + timedelta(seconds=30),
        }
    )
    postgres_store.store_push(later)
    postgres_store.quarantine_fact(
        ORG, FactTable.PUSHES, push.push_id, reason="source isolation"
    )
    result = assemble_attributed_completion_result(postgres_store, mirrors, ORG)
    assert len(postgres_store.read_session_commit_observations(ORG)) == 1
    assert result.attributed_completions
    assert result.skipped["repository_source_absent"] == 1


def test_qualified_aliases_are_canonical_after_assembly_and_bundle(
    tmp_path, postgres_store
):
    bundle, mirrors, context, push, outcome, observation = _alias_scenario(
        tmp_path, postgres_store
    )
    attributes = [
        _attribution(row, name)
        for row, name in zip(
            bundle.attributed_completions, ("acme/old", "acme/new"), strict=True
        )
    ]
    results = []
    for order in permutations(attributes):
        result = assemble_attributed_completions_result(
            postgres_store,
            mirrors,
            ORG,
            attributions=list(order),
            repository_context=context,
            policy_digest=bundle.policy.digest,
        )
        assert {row.repo for row in result.rows} == {"acme/new"}
        results.append(result)
    assert results[0] == results[1]
    assembled = replace(bundle, attributed_completions=tuple(results[0].rows))
    restored = read_derived_bundle(
        write_derived_bundle(assembled, tmp_path / "aliased-bundle")
    )
    verified = validate_derived_bundle(restored)
    projected = project_diff_sft(
        restored.attributed_completions,
        {call.inference_call_id: call for call in restored.inference_calls},
        mirrors,
        SFTPolicy(recipe_id="sft_verified"),
        repository_context=verified,
    )
    assert len(projected.rows) == 1, projected.skipped
    assert projected.rows[0].metadata.repo == "acme/new"
    assert projected.rows[0].metadata.source_ids.session_commit_observation_ids == (
        observation.observation_id,
    )
    assert all(
        row.session_commit_observations == (observation,)
        for row in restored.attributed_completions
    )
    assert all(row.ci_outcomes == [outcome] for row in restored.attributed_completions)
    assert push.repo == observation.repo == "acme/old"


def test_diff_sft_accepts_valid_member_aliases_with_deterministic_label(
    tmp_path, postgres_store
):
    bundle, mirrors, context, *_ = _alias_scenario(tmp_path, postgres_store)
    members = [
        replace(row, repo=name)
        for row, name in zip(
            bundle.attributed_completions, ("acme/old", "acme/new"), strict=True
        )
    ]
    calls = {call.inference_call_id: call for call in bundle.inference_calls}
    results = [
        project_diff_sft(
            order,
            calls,
            mirrors,
            SFTPolicy(recipe_id="sft_verified"),
            repository_context=context,
        )
        for order in permutations(members)
    ]
    assert results[0] == results[1]
    assert len(results[0].rows) == 1, results[0].skipped
    assert results[0].rows[0].metadata.repo == "acme/new"


@pytest.mark.parametrize("changed", ["unproved-label", "other-id", "other-host"])
def test_diff_sft_declines_each_unproved_member_without_joining_lifetimes(
    tmp_path, postgres_store, changed
):
    bundle, mirrors, context, *_ = _alias_scenario(tmp_path, postgres_store)
    first, second = bundle.attributed_completions
    if changed == "unproved-label":
        second = replace(second, repo="acme/unproved")
    else:
        identity = replace(
            second.repository_identity,
            **(
                {"repository_id": "303"}
                if changed == "other-id"
                else {"host": "unseen.example.com"}
            ),
        )
        second = replace(second, repository_identity=identity)
    result = project_diff_sft(
        [first, second],
        {call.inference_call_id: call for call in bundle.inference_calls},
        mirrors,
        SFTPolicy(recipe_id="sft_verified"),
        repository_context=context,
    )
    if changed == "unproved-label":
        assert result.rows == []
    else:
        assert len(result.rows) == 1
        assert "second.py" not in result.rows[0].completion[0]["content"]
    assert result.skipped["repository_identity_unresolved"] == 1


@pytest.mark.parametrize("supplied_observations", [False, True])
def test_mixed_assembly_keeps_native_legacy_notes_and_distinct_loss_units(
    tmp_path, postgres_store, supplied_observations
):
    from test_derived_bundle import _repository

    _, mirrors, push, _, _, _ = _identified_scenario(tmp_path, postgres_store)
    later = Push.model_validate(
        {
            **push.model_dump(),
            "push_id": "later-push",
            "ref": "refs/heads/other",
            "captured_at": T0 + timedelta(seconds=30),
        }
    )
    postgres_store.store_push(later)
    postgres_store.quarantine_fact(
        ORG, FactTable.PUSHES, push.push_id, reason="source isolation"
    )
    legacy_root = tmp_path / "legacy-source"
    legacy_root.mkdir()
    remote, head = _repository(legacy_root, "legacy-session")
    legacy = Push(
        org_id=ORG,
        push_id="legacy-push",
        provider="github",
        repo="acme/legacy",
        clone_url=str(remote),
        ref="refs/heads/main",
        before_sha="0" * 40,
        after_sha=head,
        captured_at=T0,
    )
    postgres_store.store_push(legacy)
    postgres_store.store_inference_call(
        _inference_call(
            "legacy-session", "alice", inference_call_id="legacy-call", captured_at=T0
        )
    )
    absent_repo = Push.model_validate(
        {**legacy.model_dump(), "push_id": "absent-repo", "repo": ""}
    )
    postgres_store.store_push(absent_repo)
    mirrors.ensure(legacy)
    kwargs = {"session_commit_observations": []} if supplied_observations else {}
    result = assemble_attributed_completion_result(
        postgres_store, mirrors, ORG, **kwargs
    )
    [legacy_row] = [
        row for row in result.attributed_completions if row.repo == legacy.repo
    ]
    assert legacy_row.attribution_source == AttributionSource.GIT_NOTES
    assert legacy_row.inference_call_id == "legacy-call"
    assert legacy_row.repository_identity is None
    assert result.skipped["repository_identity_absent"] == 1
    assert result.skipped["repository_source_absent"] == (
        0 if supplied_observations else 1
    )
    assert all(
        not row.session_commit_observations for row in result.attributed_completions
    )
