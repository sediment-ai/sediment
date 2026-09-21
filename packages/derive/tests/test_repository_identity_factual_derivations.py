# SPDX-License-Identifier: AGPL-3.0-or-later
"""Factual identity survives rename through real store and Git consumers."""

from datetime import UTC, datetime, timedelta

import pytest

from gitfixtures import FIB, commit_all, make_remote, make_work_repo
from sediment_core import (
    CIOutcome,
    DeveloperDecision,
    GatewayProvider,
    InferenceCall,
    InferenceMessage,
    PullRequestMerge,
    Push,
    SessionCommitObservation,
    TextPart,
)
from sediment_derive import (
    MirrorManager,
    derive_abandonment,
    derive_attribution_result,
    derive_recovery_result,
    derive_merge_retention_result,
    read_repository_context,
)

ORG = "acme-corp"
A, B = "acme-corp/alpha", "acme-corp/beta"
AT = datetime(2026, 9, 12, tzinfo=UTC)
END = AT + timedelta(hours=1)
IDENTITY = dict(
    repository_provider="github", repository_host="github.com", repository_id="101"
)


def _setup(tmp_path, store, *, head_id="101", mirror_head=True):
    work = make_work_repo(tmp_path)
    (work / "README.md").write_text("base\n")
    base = commit_all(work, "base")
    (work / "fibonacci.py").write_text(FIB)
    source = commit_all(work, "source")
    (work / "fibonacci.py").write_text(FIB + "\n# fixed\n")
    head = commit_all(work, "fix")
    remote = make_remote(tmp_path, work)
    first = Push(
        org_id=ORG,
        push_id="first",
        provider="github",
        repo=A,
        clone_url=str(remote),
        ref="refs/heads/main",
        before_sha=base,
        after_sha=source,
        captured_at=AT,
        **IDENTITY,
    )
    fixed = Push(
        org_id=ORG,
        push_id="fixed",
        provider="github",
        repo=B,
        clone_url=str(remote),
        ref="refs/heads/main",
        before_sha=source,
        after_sha=head,
        captured_at=END,
        **IDENTITY,
    )
    call = InferenceCall(
        org_id=ORG,
        inference_call_id="call",
        session_id="session",
        gateway_provider=GatewayProvider.LITELLM,
        model="model",
        input_messages=[
            InferenceMessage(role="user", parts=[TextPart(content="Write Fibonacci")])
        ],
        output_messages=[
            InferenceMessage(role="assistant", parts=[TextPart(content=FIB)])
        ],
        input_tokens=1,
        output_tokens=1,
        duration_ms=1,
        observed_at=AT - timedelta(minutes=1),
    )
    observation = SessionCommitObservation(
        org_id=ORG,
        observation_id="observed",
        repo=A,
        session_id="session",
        commit_sha=source,
        source_push_id="first",
        captured_at=AT,
        **IDENTITY,
    )
    red = CIOutcome(
        org_id=ORG,
        outcome_id="red",
        provider="github_actions",
        repo=A,
        commit_sha=source,
        branch="main",
        run_id="red",
        result="failed",
        workflow_id="tests",
        workflow_name="tests",
        captured_at=AT,
        pr_number=7,
        **IDENTITY,
    )
    green = CIOutcome(
        org_id=ORG,
        outcome_id="green",
        provider="github_actions",
        repo=B,
        commit_sha=head,
        branch="main",
        run_id="green",
        result="passed",
        workflow_id="tests",
        workflow_name="tests",
        captured_at=END,
        pr_number=7,
        **IDENTITY,
    )
    merge = PullRequestMerge(
        org_id=ORG,
        merge_id="merge",
        provider="github",
        repo=B,
        pr_number=7,
        head_repo=B if head_id in (None, "101") else "fork/project",
        head_ref="topic",
        head_sha=head,
        base_ref="main",
        base_sha=base,
        merge_commit_sha=head,
        merged_at=END,
        captured_at=END,
        head_repository_provider="github" if head_id is not None else None,
        head_repository_host="github.com" if head_id is not None else None,
        head_repository_id=head_id,
        **IDENTITY,
    )
    decision = DeveloperDecision(
        org_id=ORG,
        session_id="session",
        agent_harness="codex",
        file_path="fibonacci.py",
        accepted=True,
        explicit=True,
        interaction_mode="agent",
        occurred_at=AT,
        captured_at=AT,
    )
    for fact, writer in [
        (first, "store_push"),
        (fixed, "store_push"),
        (call, "store_inference_call"),
        (observation, "store_session_commit_observation"),
        (red, "store_ci_outcome"),
        (green, "store_ci_outcome"),
        (merge, "store_pull_request_merge"),
        (decision, "store_decision"),
    ]:
        getattr(store, writer)(fact)
    fork = None
    if head_id not in (None, "101"):
        fork = Push.model_validate(
            {
                **fixed.model_dump(),
                "push_id": "fork",
                "repo": "fork/project",
                "repository_id": head_id,
            }
        )
        store.store_push(fork)
    with store.read_snapshot() as snapshot:
        context = read_repository_context(snapshot, ORG, as_of=END)
    mirrors = MirrorManager(str(tmp_path / "mirrors"))
    mirrors.ensure(fixed, repository_context=context)
    if fork is not None and mirror_head:
        mirrors.ensure(fork, repository_context=context)
    return mirrors, context, first, fixed, observation, red, green, merge


def test_abandonment_observation_keeps_committed_status_across_rename(
    tmp_path, postgres_store
):
    mirrors, context, *_ = _setup(tmp_path, postgres_store)
    result = derive_abandonment(
        postgres_store, mirrors, ORG, repository_context=context
    )
    assert [row.status for row in result.outcomes] == ["committed"]
    assert result.as_of == END
    assert result.sessions == []


def test_abandonment_quarantined_source_remains_unknown(tmp_path, postgres_store):
    mirrors, _, first, *_ = _setup(tmp_path, postgres_store)
    postgres_store.quarantine_fact(ORG, "pushes", first.push_id, reason="test")
    result = derive_abandonment(postgres_store, mirrors, ORG, as_of=END)
    assert [row.status for row in result.outcomes] == ["attribution_unavailable"]
    assert result.skipped["repository_source_absent"] == 1
    assert result.sessions == []


def test_abandonment_preloaded_identified_observation_requires_context(
    tmp_path, postgres_store
):
    mirrors, _, _, _, observation, *_ = _setup(tmp_path, postgres_store)
    result = derive_abandonment(
        postgres_store,
        mirrors,
        ORG,
        session_commit_observations=[observation],
        as_of=END,
    )
    assert [row.status for row in result.outcomes] == ["attribution_unavailable"]
    assert result.skipped["repository_identity_unresolved"] == 1


def test_recovery_rename_uses_common_identity_and_exact_source_evidence(
    tmp_path, postgres_store
):
    mirrors, context, first, fixed, observation, red, green, _ = _setup(
        tmp_path, postgres_store
    )
    result = derive_recovery_result(
        postgres_store, mirrors, ORG, repository_context=context
    )
    assert len(result.pairs) == 1
    [pair] = result.pairs
    assert pair.repository_identity.repository_id == "101"
    assert (pair.repo, pair.failed_outcome_id, pair.fixed_outcome_id) == (
        A,
        "red",
        "green",
    )
    assert (
        pair.failed_commit_sha == first.after_sha
        and pair.fixed_commit_sha == fixed.after_sha
    )
    assert pair.failed_attribution_evidence[0].session_commit_observation_ids == (
        observation.observation_id,
    )
    assert postgres_store.read_ci_outcomes(ORG) == [red, green]


def test_merge_rename_preserves_membership_scores_and_observations(
    tmp_path, postgres_store
):
    mirrors, context, _, _, observation, *_ = _setup(tmp_path, postgres_store)
    attrs = derive_attribution_result(
        postgres_store, mirrors, ORG, repository_context=context
    ).attributions
    attrs = [row for row in attrs if row.commit_sha == observation.commit_sha]
    result = derive_merge_retention_result(
        postgres_store, mirrors, ORG, attributions=attrs, repository_context=context
    )
    assert result.joined_pull_requests == 1 and result.joined_candidates == 1
    [row] = result.rows
    assert row.repository_identity.repository_id == "101" and row.repo == A
    assert row.session_commit_observation_ids == ("observed",)
    assert row.head_retention_score == row.merge_retention_score == 1.0
    assert result.membership_outcomes[0].repository_identity == row.repository_identity
    assert result.session_commit_observations == (observation,)
    assert result == derive_merge_retention_result(
        postgres_store,
        mirrors,
        ORG,
        attributions=list(reversed(attrs)),
        repository_context=context,
    )


def _source_attributions(store, mirrors, context, observation):
    return [
        row
        for row in derive_attribution_result(
            store, mirrors, ORG, repository_context=context
        ).attributions
        if row.commit_sha == observation.commit_sha
        and row.repository_identity.repository_id == "101"
    ]


@pytest.mark.parametrize("exact_ci", [False, True])
def test_merge_uses_independent_fork_head_mirror(tmp_path, postgres_store, exact_ci):
    mirrors, context, _, _, observation, _, _, merge = _setup(
        tmp_path, postgres_store, head_id="202"
    )
    attrs = _source_attributions(postgres_store, mirrors, context, observation)
    arguments = dict(attributions=attrs, repository_context=context)
    if not exact_ci:
        arguments["ci_outcomes"] = []
    result = derive_merge_retention_result(postgres_store, mirrors, ORG, **arguments)
    assert len(result.rows) == 1
    assert result.rows[0].repository_identity.repository_id == "101"
    assert result.rows[0].head_retention_score == 1.0
    head_key = context.resolve_fact(merge, role="head_repo").key
    assert mirrors.remove_repository(head_key)
    # The target still has identical head objects, but cannot stand in for the fork.
    target = mirrors.open_repository(context.resolve_fact(merge).key)
    assert target.commit_exists(merge.head_sha)
    after = derive_merge_retention_result(postgres_store, mirrors, ORG, **arguments)
    assert after.rows == []
    assert [item.status for item in after.membership_outcomes] == [
        "joined" if exact_ci else "ancestry_unresolved"
    ]
    if exact_ci:
        assert after.skipped["mirror_absent"] == 1


@pytest.mark.parametrize("exact_ci", [False, True])
def test_merge_absent_head_identity_never_borrows_target_proof(
    tmp_path, postgres_store, exact_ci
):
    mirrors, context, _, _, observation, *_ = _setup(
        tmp_path, postgres_store, head_id=None
    )
    arguments = dict(
        attributions=_source_attributions(
            postgres_store, mirrors, context, observation
        ),
        repository_context=context,
    )
    if not exact_ci:
        arguments["ci_outcomes"] = []
    result = derive_merge_retention_result(postgres_store, mirrors, ORG, **arguments)
    assert result.rows == []
    assert [item.status for item in result.membership_outcomes] == [
        "joined" if exact_ci else "ancestry_unresolved"
    ]
    assert result.skipped["repository_identity_unresolved"] == 1


@pytest.mark.parametrize(
    "other_identity",
    [
        {"repository_id": "202"},
        {"repository_host": "github.enterprise.example"},
    ],
)
def test_recovery_qualified_other_lifetime_cannot_supply_green(
    tmp_path, postgres_store, other_identity
):
    mirrors, context, _, _, _, _, green, _ = _setup(tmp_path, postgres_store)
    postgres_store.quarantine_fact(ORG, "ci_outcomes", green.outcome_id, reason="test")
    sibling = CIOutcome.model_validate(
        {
            **green.model_dump(),
            "outcome_id": "other-green",
            "run_id": "other-green",
            **other_identity,
        }
    )
    postgres_store.store_ci_outcome(sibling)
    result = derive_recovery_result(postgres_store, mirrors, ORG, as_of=END)
    assert result.pairs == []


def test_recovery_explicit_boundary_and_shuffled_snapshot_agree(
    tmp_path, postgres_store
):
    mirrors, context, *_ = _setup(tmp_path, postgres_store)
    expected = derive_recovery_result(
        postgres_store, mirrors, ORG, repository_context=context
    )
    assert derive_recovery_result(postgres_store, mirrors, ORG, as_of=AT).pairs == []
    with postgres_store.read_snapshot() as snapshot:
        assert (
            derive_recovery_result(
                snapshot, mirrors, ORG, as_of=END, repository_context=context
            )
            == expected
        )
    with pytest.raises(ValueError, match="boundary"):
        derive_recovery_result(
            postgres_store, mirrors, ORG, as_of=AT, repository_context=context
        )


def test_recovery_counts_quarantined_observation_source_without_losing_pair(
    tmp_path, postgres_store
):
    mirrors, _, first, *_ = _setup(tmp_path, postgres_store)
    postgres_store.quarantine_fact(ORG, "pushes", first.push_id, reason="test")
    result = derive_recovery_result(postgres_store, mirrors, ORG, as_of=END)
    assert len(result.pairs) == 1
    assert result.skipped["repository_source_absent"] == 1
    assert result.pairs[0].failed_attribution_evidence == ()


@pytest.mark.parametrize("consumer", ["recovery", "merge"])
def test_implicit_consumer_boundary_preserves_post_push_attribution_grace(
    tmp_path, postgres_store, consumer
):
    mirrors, context, _, fixed, observation, *_ = _setup(tmp_path, postgres_store)
    call = InferenceCall.model_validate(
        {
            **postgres_store.read_inference_calls(ORG)[0].model_dump(),
            "inference_call_id": "late",
            "session_id": "late-session",
            "observed_at": END + timedelta(minutes=1),
            "output_messages": [
                {"role": "assistant", "parts": [{"type": "text", "content": "# fixed"}]}
            ],
        }
    )
    postgres_store.store_inference_call(call)
    postgres_store.store_session_commit_observation(
        SessionCommitObservation.model_validate(
            {
                **observation.model_dump(),
                "observation_id": "late-observation",
                "session_id": "late-session",
                "repo": B,
                "commit_sha": fixed.after_sha,
                "source_push_id": fixed.push_id,
                "captured_at": END,
            }
        )
    )
    derive = (
        derive_recovery_result
        if consumer == "recovery"
        else derive_merge_retention_result
    )
    implicit = derive(postgres_store, mirrors, ORG)
    # Refresh the exact context to include the captured observation at END.
    context = read_repository_context(postgres_store, ORG, as_of=END)
    explicit = derive(postgres_store, mirrors, ORG, repository_context=context)
    if consumer == "recovery":
        assert implicit.pairs[0].fixed_inference_call_ids == ["late"]
        assert explicit.pairs[0].fixed_inference_call_ids == []
    else:
        assert "late" in {row.inference_call_id for row in implicit.rows}
        assert "late" not in {row.inference_call_id for row in explicit.rows}
        assert implicit.as_of == call.observed_at


def test_factual_consumers_reproduce_shuffled_store_and_supplied_populations(
    tmp_path, postgres_store, postgres_store_factory
):
    mirrors, context, *_ = _setup(tmp_path, postgres_store)
    _, shuffled = postgres_store_factory()
    populations = [
        ("read_pushes", "store_push"),
        ("read_inference_calls", "store_inference_call"),
        ("read_session_commit_observations", "store_session_commit_observation"),
        ("read_ci_outcomes", "store_ci_outcome"),
        ("read_pull_request_merges", "store_pull_request_merge"),
        ("read_decisions", "store_decision"),
    ]
    # The public writer requires each observation's Push to exist. Within that
    # constraint, reverse Push order and every other family/row arrival order.
    for reader, writer in [populations[0], *reversed(populations[1:])]:
        for fact in reversed(getattr(postgres_store, reader)(ORG)):
            assert getattr(shuffled, writer)(fact)
    for derive in (
        derive_abandonment,
        derive_recovery_result,
        derive_merge_retention_result,
    ):
        assert derive(postgres_store, mirrors, ORG, as_of=END) == derive(
            shuffled, mirrors, ORG, as_of=END
        )
    attrs = derive_attribution_result(
        postgres_store, mirrors, ORG, repository_context=context
    ).attributions
    expected = derive_merge_retention_result(
        postgres_store, mirrors, ORG, attributions=attrs, repository_context=context
    )
    assert expected == derive_merge_retention_result(
        shuffled,
        mirrors,
        ORG,
        attributions=list(reversed(attrs)),
        merges=reversed(shuffled.read_pull_request_merges(ORG)),
        revisions=iter(()),
        ci_outcomes=reversed(shuffled.read_ci_outcomes(ORG)),
        session_commit_observations=reversed(
            shuffled.read_session_commit_observations(ORG)
        ),
        repository_context=context,
    )
    assert (
        derive_merge_retention_result(
            shuffled,
            mirrors,
            ORG,
            attributions=attrs,
            session_commit_observations=iter(()),
            repository_context=context,
        ).membership_outcomes
        == []
    )


@pytest.mark.parametrize("source_push_id", [None, "missing", "fork"])
def test_merge_declines_preloaded_attribution_without_exact_source_anchor(
    tmp_path, postgres_store, source_push_id
):
    from dataclasses import replace

    mirrors, context, _, _, observation, *_ = _setup(
        tmp_path, postgres_store, head_id="202"
    )
    [original] = _source_attributions(postgres_store, mirrors, context, observation)
    attribution = replace(original, source_push_id=source_push_id)
    result = derive_merge_retention_result(
        postgres_store,
        mirrors,
        ORG,
        attributions=[attribution],
        repository_context=context,
    )
    assert result.membership_outcomes == []
    reason = (
        "repository_identity_conflict"
        if source_push_id == "fork"
        else "repository_source_absent"
    )
    assert result.skipped[reason] == 1
