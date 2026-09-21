# SPDX-License-Identifier: AGPL-3.0-or-later
"""Canonical Derivations retain repository proof through real Git and PostgreSQL."""

import json
import pytest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import permutations

from gitfixtures import FIB, commit_all, make_remote, make_work_repo, run_git

from sediment_core import (
    CIOutcome,
    CIProvider,
    CIResult,
    ForgeProvider,
    FactTable,
    GatewayProvider,
    InferenceCall,
    InferenceMessage,
    Push,
    RepositoryRename,
    SessionCommitObservation,
    TextPart,
)
from sediment_derive import (
    AttributionSource,
    MirrorManager,
    build_repository_context,
    derive_attribution_result,
    derive_rollout_result,
    repository_identity_evidence_of,
    repository_identity_of,
)

ORG = "acme-corp"
A = "acme-corp/alpha"
B = "acme-corp/beta"
AT = datetime(2026, 9, 12, tzinfo=UTC)
LATER = AT + timedelta(hours=1)


def _context(*facts, as_of=LATER):
    return build_repository_context(
        [repository_identity_evidence_of(fact) for fact in facts],
        (),
        ORG,
        as_of=as_of,
    )


def _setup(tmp_path, store):
    work = make_work_repo(tmp_path)
    (work / "fibonacci.py").write_text(FIB)
    sha = commit_all(work, "fibonacci")
    note = {
        "v": 1,
        "sessions": [
            {"tool": "codex", "session_id": "session", "stamped_at": AT.isoformat()}
        ],
    }
    run_git(work, "notes", "--ref=sediment", "add", "-m", json.dumps(note), sha)
    remote = make_remote(tmp_path, work)
    call = InferenceCall(
        inference_call_id="call",
        org_id=ORG,
        session_id="session",
        user_id="developer",
        gateway_provider=GatewayProvider.LITELLM,
        model="model",
        input_messages=[
            InferenceMessage(
                role="user", parts=[TextPart(content="Implement fibonacci")]
            )
        ],
        output_messages=[
            InferenceMessage(role="assistant", parts=[TextPart(content=FIB)])
        ],
        input_tokens=1,
        output_tokens=2,
        duration_ms=1,
        observed_at=AT - timedelta(minutes=1),
    )
    first = Push(
        push_id="first",
        org_id=ORG,
        provider=ForgeProvider.GITHUB,
        repo=A,
        repository_provider=ForgeProvider.GITHUB,
        repository_host="github.com",
        repository_id="101",
        clone_url=str(remote),
        ref="refs/heads/main",
        before_sha="0" * 40,
        after_sha=sha,
        captured_at=AT,
    )
    later = first.model_copy(
        update={
            "push_id": "later",
            "repo": B,
            "ref": "refs/heads/feature",
            "captured_at": LATER,
        }
    )
    observation = SessionCommitObservation(
        observation_id="observation",
        org_id=ORG,
        repo=A,
        commit_sha=sha,
        session_id="session",
        source_push_id="first",
        captured_at=AT,
        repository_provider=ForgeProvider.GITHUB,
        repository_host="github.com",
        repository_id="101",
    )
    ci = CIOutcome(
        outcome_id="ci",
        org_id=ORG,
        repo=B,
        commit_sha=sha,
        branch="main",
        provider=CIProvider.GITHUB_ACTIONS,
        run_id="run",
        result=CIResult.PASSED,
        captured_at=LATER,
        repository_provider=ForgeProvider.GITHUB,
        repository_host="github.com",
        repository_id="101",
    )
    store.store_inference_call(call)
    store.store_push(first)
    store.store_push(later)
    store.store_session_commit_observation(observation)
    store.store_ci_outcome(ci)
    mirrors = MirrorManager(str(tmp_path / "mirrors"))
    declared = _context(first, later, observation, ci)
    mirrors.ensure(first, repository_context=declared)
    return mirrors, first, later, observation, ci, call, declared


def test_identified_attribution_uses_one_key_and_retained_source_push(
    tmp_path, postgres_store
):
    mirrors, first, later, observation, ci, call, declared = _setup(
        tmp_path, postgres_store
    )
    expected = None
    for pushes in permutations((first, later)):
        result = derive_attribution_result(
            postgres_store,
            mirrors,
            ORG,
            pushes=list(pushes),
            repository_context=declared,
            as_of=LATER,
        )
        assert result.skipped == {}
        assert len(result.attributions) == 1
        row = result.attributions[0]
        assert row.repo == A and row.source_push_id == first.push_id
        assert row.repository_identity == repository_identity_of(first)
        assert row.attribution_source == AttributionSource.GIT_NOTES
        assert row.inference_call_id == call.inference_call_id
        expected = expected or result
        assert result == expected
    direct = derive_attribution_result(postgres_store, mirrors, ORG)
    assert direct.attributions == expected.attributions


def test_preloaded_identified_push_without_context_declines(tmp_path, postgres_store):
    mirrors, first, *_ = _setup(tmp_path, postgres_store)
    result = derive_attribution_result(postgres_store, mirrors, ORG, pushes=[first])
    assert result.attributions == []
    assert result.skipped == {"repository_identity_unresolved": 1}


@pytest.mark.parametrize("explicit_as_of", [None, LATER])
def test_identified_rollout_retains_original_observation_and_renamed_ci(
    tmp_path, postgres_store, explicit_as_of
):
    mirrors, first, later, observation, ci, call, declared = _setup(
        tmp_path, postgres_store
    )
    postgres_store.store_inference_call(
        call.model_copy(
            update={
                "inference_call_id": "future-call",
                "session_id": "future-session",
                "observed_at": LATER + timedelta(seconds=1),
            }
        )
    )
    result = derive_rollout_result(
        postgres_store, mirrors, ORG, repository_context=declared, as_of=explicit_as_of
    )
    assert len(result.rollouts) == 1
    row = result.rollouts[0]
    assert row.commits[0].repo == A
    assert row.commits[0].repository_identity == repository_identity_of(first)
    assert row.session_commit_observations == (observation,)
    assert row.terminal_outcomes == [ci]
    assert (
        row.session_commit_observations[0].repo == A
        and row.terminal_outcomes[0].repo == B
    )


def test_complete_context_loader_includes_rename_only_claims_and_boundary(
    postgres_store,
):
    from sediment_derive.repository_context import read_repository_context

    rename = RepositoryRename(
        rename_id="rename",
        org_id=ORG,
        repository_provider=ForgeProvider.GITHUB,
        repository_host="github.com",
        repository_id="101",
        old_repo=A,
        new_repo=B,
        captured_at=LATER,
    )
    postgres_store.store_repository_rename(rename)
    with postgres_store.read_snapshot() as snapshot:
        before = read_repository_context(snapshot, ORG, as_of=AT)
        complete = read_repository_context(snapshot, ORG)
    assert before.resolve_reference(ORG, A).key is not None
    assert complete.as_of == LATER
    assert complete.resolve_reference(ORG, A).reason == "repository_identity_unresolved"


def test_legacy_supplements_keep_source_conflicts_and_complete_claims(postgres_store):
    from sediment_derive.repository_context import read_repository_context

    legacy = Push(
        push_id="legacy",
        org_id=ORG,
        provider=ForgeProvider.GITHUB,
        repo=A,
        clone_url="/synthetic/remote.git",
        ref="refs/heads/main",
        before_sha="0" * 40,
        after_sha="a" * 40,
        captured_at=AT,
    )
    postgres_store.store_push(legacy)
    projection = repository_identity_evidence_of(legacy)
    conflict = replace(projection, repo=B)
    with postgres_store.read_snapshot() as snapshot:
        context = read_repository_context(
            snapshot, ORG, as_of=LATER, supplemental_legacy_evidence=[conflict]
        )
    assert context.as_of == LATER
    assert context.resolve_fact(legacy).reason == "repository_identity_conflict"

    observation = SessionCommitObservation(
        observation_id="preloaded",
        org_id=ORG,
        repo=B,
        commit_sha="b" * 40,
        session_id="session",
        source_push_id="historical-absent",
        captured_at=AT,
    )
    rename = RepositoryRename(
        rename_id="rename",
        org_id=ORG,
        repository_provider=ForgeProvider.GITHUB,
        repository_host="github.com",
        repository_id="101",
        old_repo=B,
        new_repo="acme-corp/gamma",
        captured_at=LATER,
    )
    postgres_store.store_repository_rename(rename)
    supplement = [repository_identity_evidence_of(observation)]
    with postgres_store.read_snapshot() as snapshot:
        before = read_repository_context(
            snapshot, ORG, as_of=AT, supplemental_legacy_evidence=supplement
        )
        after = read_repository_context(
            snapshot, ORG, supplemental_legacy_evidence=supplement
        )
    assert before.resolve_fact(observation).key is not None
    assert after.as_of == LATER
    assert after.resolve_fact(observation).reason == "repository_identity_unresolved"


def test_loader_refuses_identified_supplements(postgres_store):
    from sediment_derive.repository_context import read_repository_context

    identified = Push(
        push_id="identified",
        org_id=ORG,
        provider=ForgeProvider.GITHUB,
        repo=A,
        clone_url="/synthetic/remote.git",
        ref="refs/heads/main",
        before_sha="0" * 40,
        after_sha="a" * 40,
        captured_at=AT,
        repository_provider=ForgeProvider.GITHUB,
        repository_host="github.com",
        repository_id="101",
    )
    with postgres_store.read_snapshot() as snapshot:
        with pytest.raises(ValueError, match="legacy"):
            read_repository_context(
                snapshot,
                ORG,
                supplemental_legacy_evidence=[
                    repository_identity_evidence_of(identified)
                ],
            )


def test_explicit_context_remains_exact_for_preloaded_legacy_push(
    tmp_path, postgres_store
):
    empty = _context()
    push = Push(
        push_id="preloaded",
        org_id=ORG,
        provider=ForgeProvider.GITHUB,
        repo=A,
        clone_url="/synthetic/remote.git",
        ref="refs/heads/main",
        before_sha="0" * 40,
        after_sha="a" * 40,
        captured_at=AT,
    )
    result = derive_attribution_result(
        postgres_store,
        MirrorManager(str(tmp_path / "mirrors")),
        ORG,
        pushes=[push],
        repository_context=empty,
    )
    assert result.attributions == []
    assert result.skipped == {"repository_source_absent": 1}


def test_identified_copied_notes_and_shared_sha_do_not_bind_fork(
    tmp_path, postgres_store
):
    mirrors, first, later, observation, ci, call, _ = _setup(tmp_path, postgres_store)
    fork_remote = tmp_path / "fork.git"
    run_git(tmp_path, "clone", "--mirror", first.clone_url, str(fork_remote))
    fork = first.model_copy(
        update={
            "push_id": "fork",
            "repository_id": "202",
            "repo": "acme-corp/fork",
            "clone_url": str(fork_remote),
        }
    )
    fork_ci = ci.model_copy(
        update={
            "outcome_id": "fork-ci",
            "repo": fork.repo,
            "repository_id": "202",
            "run_id": "fork-run",
            "result": CIResult.FAILED,
        }
    )
    postgres_store.store_push(fork)
    postgres_store.store_ci_outcome(fork_ci)
    declared = _context(first, later, observation, ci, fork, fork_ci)
    mirrors.ensure(fork, repository_context=declared)
    assert run_git(fork_remote, "notes", "--ref=sediment", "show", first.after_sha)
    population = (first, later, observation, ci, fork, fork_ci)
    for order in (population, tuple(reversed(population))):
        result = derive_rollout_result(
            postgres_store, mirrors, ORG, repository_context=_context(*order)
        )
        assert len(result.rollouts) == 1
        row = result.rollouts[0]
        assert len(row.commits) == 1
        assert row.commits[0].repository_identity == repository_identity_of(first)
        assert row.session_commit_observations == (observation,)
        assert row.terminal_outcomes == [ci]


def test_attribution_share_groups_one_lifetime_and_alerts_never_borrow_fork_baseline(
    tmp_path, postgres_store
):
    from sediment_derive import (
        AttributionSharePolicy,
        RepositoryIdentity,
        check_attribution_share_alerts,
        derive_attribution_share,
    )

    mirrors, first, later, observation, ci, call, declared = _setup(
        tmp_path, postgres_store
    )
    rows = derive_attribution_share(
        postgres_store, mirrors, ORG, repository_context=declared, as_of=LATER
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.repo == A and row.repository_identity == repository_identity_of(first)
    assert row.agent_plausible_commits == row.git_notes_attributed == 1
    for pushes in permutations((first, later)):
        assert (
            derive_attribution_share(
                postgres_store,
                mirrors,
                ORG,
                pushes=list(pushes),
                repository_context=declared,
            )
            == rows
        )
    assert derive_attribution_share(postgres_store, mirrors, ORG) == rows
    fork = replace(
        row,
        repository_identity=RepositoryIdentity(
            ForgeProvider.GITHUB, "github.com", "202"
        ),
        git_notes_attributed=0,
        jaccard_attributed=1,
        git_notes_share=0.0,
    )
    alerts = check_attribution_share_alerts(
        [fork], [row], AttributionSharePolicy(min_cases_for_decline_verdict=1)
    )
    assert len(alerts) == 1 and alerts[0].baseline is None
    assert alerts[0].repository_identity == fork.repository_identity
    renamed = replace(
        fork, repository_identity=row.repository_identity, repo="acme-corp/new-name"
    )
    same_lifetime = check_attribution_share_alerts(
        [renamed], [row], AttributionSharePolicy(min_cases_for_decline_verdict=1)
    )
    assert same_lifetime[0].baseline == row
    for isolated in (
        replace(renamed, org_id="other-org"),
        replace(
            renamed,
            repository_identity=replace(row.repository_identity, host="forge.example"),
        ),
    ):
        assert (
            check_attribution_share_alerts(
                [isolated],
                [row],
                AttributionSharePolicy(min_cases_for_decline_verdict=1),
            )[0].baseline
            is None
        )


def test_attribution_share_preloaded_identified_without_context_is_counted(
    tmp_path, postgres_store
):
    from sediment_derive import derive_attribution_share_result

    mirrors, first, *_ = _setup(tmp_path, postgres_store)
    result = derive_attribution_share_result(
        postgres_store, mirrors, ORG, pushes=[first]
    )
    assert result.rows == []
    assert result.skipped == {"repository_identity_unresolved": 1}


def test_public_precision_generator_preserves_identified_truth(
    tmp_path, postgres_store
):
    from sediment_derive import IdentifiedRepositoryKey
    from sediment_derive.precision_report import (
        GroundTruthRow,
        generate_precision_report_for_org,
    )

    mirrors, first, later, observation, ci, call, declared = _setup(
        tmp_path, postgres_store
    )
    truth = GroundTruthRow(
        "identified",
        call.inference_call_id,
        first.after_sha,
        "fibonacci.py",
        AttributionSource.GIT_NOTES,
        None,
        "synthetic",
        expected_repository=IdentifiedRepositoryKey(ORG, repository_identity_of(first)),
    )
    report = generate_precision_report_for_org(
        postgres_store, mirrors, ORG, [truth], repository_context=declared
    )
    assert report.by_source[AttributionSource.GIT_NOTES].default.true_positives == 1


@pytest.mark.parametrize("conflicting", [True, False])
def test_legacy_note_alias_conflicts_decline_without_arrival_order(
    tmp_path, postgres_store_factory, conflicting
):
    from sediment_derive import derive_attribution_share_result

    _, original = postgres_store_factory()
    _, first, _, _, _, call, _ = _setup(tmp_path, original)
    _, store = postgres_store_factory()
    legacy = first.model_copy(
        update={
            "repository_provider": None,
            "repository_host": None,
            "repository_id": None,
        }
    )
    store.store_push(legacy)
    store.store_inference_call(call)
    mirrors = MirrorManager(str(tmp_path / "legacy-mirrors"))
    mirrors.ensure(legacy)
    entries = [
        ((A, first.after_sha), frozenset({"session"})),
        (
            (A.upper(), first.after_sha),
            frozenset({"another-session" if conflicting else "session"}),
        ),
    ]
    outcomes = []
    for order in permutations(entries):
        notes = dict(order)
        attribution = derive_attribution_result(
            store, mirrors, ORG, note_sessions_by_commit=notes
        )
        share = derive_attribution_share_result(
            store, mirrors, ORG, note_sessions_by_commit=notes
        )
        assert (
            attribution.skipped
            == share.skipped
            == ({"repository_identity_conflict": 2} if conflicting else {})
        )
        assert attribution.attributions[0].attribution_source == (
            AttributionSource.JACCARD if conflicting else AttributionSource.GIT_NOTES
        )
        assert share.rows[0].git_notes_attributed == (0 if conflicting else 1)
        outcomes.append((attribution, share))
    assert outcomes[0] == outcomes[1]


def test_implicit_repository_boundary_keeps_post_push_grace_calls(
    tmp_path, postgres_store_factory
):
    from sediment_derive import derive_attribution_share_result

    _, original = postgres_store_factory()
    _, first, _, _, _, call, _ = _setup(tmp_path, original)
    _, store = postgres_store_factory()
    legacy = first.model_copy(
        update={
            "repository_provider": None,
            "repository_host": None,
            "repository_id": None,
        }
    )
    store.store_push(legacy)
    call = call.model_copy(
        update={"observed_at": AT + timedelta(minutes=1), "session_id": "unnoted"}
    )
    store.store_inference_call(call)
    mirrors = MirrorManager(str(tmp_path / "legacy-mirrors"))
    mirrors.ensure(legacy)
    attribution = derive_attribution_result(store, mirrors, ORG)
    assert len(attribution.attributions) == 1
    share = derive_attribution_share_result(store, mirrors, ORG)
    assert share.rows[0].jaccard_attributed == 1
    assert (
        derive_attribution_share_result(store, mirrors, ORG, pushes=[legacy]) == share
    )
    rollout = derive_rollout_result(store, mirrors, ORG)
    assert rollout.rollouts[0].commits[0].commit_sha == first.after_sha
    assert (
        derive_rollout_result(store, mirrors, ORG, session_commit_observations=[])
        == rollout
    )
    explicit = _context(legacy, as_of=AT)
    assert (
        derive_attribution_result(
            store, mirrors, ORG, repository_context=explicit
        ).attributions
        == []
    )
    assert (
        derive_rollout_result(store, mirrors, ORG, repository_context=explicit).rollouts
        == []
    )
    assert (
        derive_attribution_share_result(
            store, mirrors, ORG, repository_context=explicit
        ).rows
        == []
    )


@pytest.mark.parametrize("conflict", ["commit", "repository"])
def test_rollout_qualifies_complete_ci_runs_before_selecting_commits(
    tmp_path, postgres_store, conflict
):
    mirrors, _, _, _, ci, _, _ = _setup(tmp_path, postgres_store)
    contradictory = CIOutcome.model_validate(
        {
            **ci.model_dump(),
            "outcome_id": "contradictory-retry",
            "run_attempt": 2,
            "commit_sha": "b" * 40 if conflict == "commit" else ci.commit_sha,
            "repository_id": "202" if conflict == "repository" else ci.repository_id,
            "result": "failed",
            "captured_at": LATER + timedelta(seconds=1),
        }
    )
    postgres_store.store_ci_outcome(contradictory)
    result = derive_rollout_result(postgres_store, mirrors, ORG)
    assert len(result.rollouts) == 1 and result.rollouts[0].commits
    assert result.rollouts[0].terminal_outcomes == []
    assert result.skipped["conflicting_run_identity"] == 1
    before = derive_rollout_result(postgres_store, mirrors, ORG, as_of=LATER)
    assert before.rollouts[0].terminal_outcomes == [ci]
    postgres_store.quarantine_fact(
        ORG,
        FactTable.CI_OUTCOMES,
        contradictory.outcome_id,
        reason="quarantined conflicting rehearsal retry",
    )
    after = derive_rollout_result(postgres_store, mirrors, ORG)
    assert after.rollouts[0].terminal_outcomes == [ci]
    assert "conflicting_run_identity" not in after.skipped
