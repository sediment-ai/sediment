# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lifecycle and merge reports retain exact qualified repository evidence."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "derive" / "tests"))

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
    read_repository_context,
)

from sediment_export import (
    generate_accepted_work_lifecycle_report,
    generate_merge_retention_report_result,
    OperationalReportScope,
)


ORG = "acme-corp"
A, B = "acme-corp/alpha", "acme-corp/beta"
AT = datetime(2026, 9, 12, tzinfo=UTC)
END = AT + timedelta(hours=1)
IDENTITY = dict(
    repository_provider="github", repository_host="github.com", repository_id="101"
)


def _setup(
    tmp_path, store, *, head_id="101", mirror_head=True, legacy_observation=False
):
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
        model_call_id="agent-call",
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
        **({} if legacy_observation else IDENTITY),
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
        call_id="agent-call",
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


@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("legacy_observation", [False, True])
def test_lifecycle_and_merge_report_keep_renamed_factual_chain(
    tmp_path, postgres_store, scoped, legacy_observation
):
    mirrors, context, _, _, observation, *_ = _setup(
        tmp_path, postgres_store, legacy_observation=legacy_observation
    )
    scope = (
        OperationalReportScope(AT - timedelta(hours=1), AT + timedelta(minutes=1), END)
        if scoped
        else None
    )
    report = generate_accepted_work_lifecycle_report(
        postgres_store, mirrors, ORG, scope=scope, repository_context=context
    )
    assert (
        report.accepted_work.accepted_calls
        == report.accepted_work.attributed.count
        == 1
    )
    assert report.accepted_work.pull_request_membership.count == 1
    assert report.accepted_work.ci_linked.count == report.accepted_work.ci_failed == 1
    assert report.merge_durability.scored_rows == 1
    [stratum] = [entry for entry in report.strata if entry.dimension == "repository"]
    assert stratum.repository_identity.repository_id == "101"
    merge = generate_merge_retention_report_result(
        postgres_store, mirrors, ORG, repository_context=context
    )
    assert (
        merge.report.attributed_file_candidates
        == merge.report.scored_rows
        == len(merge.rows)
        == 1
    )
    assert merge.report.joined_pull_requests == merge.report.scored_pull_requests == 1
    assert merge.rows[0].session_commit_observation_ids == (observation.observation_id,)
    assert (
        observation.repo == A
        and merge.rows[0].repository_identity.repository_id == "101"
    )


def test_lifecycle_renamed_ci_retry_and_foreign_host_run_stay_separate(
    tmp_path, postgres_store
):
    mirrors, _, _, _, _, red, *_ = _setup(tmp_path, postgres_store)
    retry = CIOutcome.model_validate(
        {
            **red.model_dump(),
            "outcome_id": "retry",
            "run_attempt": 2,
            "repo": B,
            "result": "passed",
            "captured_at": END,
        }
    )
    foreign = CIOutcome.model_validate(
        {
            **red.model_dump(),
            "outcome_id": "foreign",
            "repository_host": "enterprise.example",
            "branch": "other",
        }
    )
    postgres_store.store_ci_outcome(retry)
    postgres_store.store_ci_outcome(foreign)
    report = generate_accepted_work_lifecycle_report(
        postgres_store, mirrors, ORG, as_of=END
    )
    assert report.accepted_work.ci_linked.count == report.accepted_work.ci_passed == 1
    assert report.accepted_work.ci_failed == 0
    assert "ambiguous_ci_verdict" not in report.accepted_work.skips


def test_missing_head_identity_keeps_membership_and_visible_scoring_loss(
    tmp_path, postgres_store
):
    mirrors, context, *_ = _setup(tmp_path, postgres_store, head_id=None)
    report = generate_accepted_work_lifecycle_report(
        postgres_store, mirrors, ORG, repository_context=context
    )
    assert report.accepted_work.pull_request_membership.count == 1
    assert report.merge_durability.scored_rows == 0
    assert report.merge_durability.scoring_skips["repository_identity_unresolved"] == 1
    merge = generate_merge_retention_report_result(
        postgres_store, mirrors, ORG, repository_context=context
    )
    assert merge.report.candidates_joined_to_merge == 1
    assert merge.report.scored_rows == len(merge.rows) == 0


def test_merge_report_pure_population_separates_same_slug_and_pr_lifetimes(
    tmp_path, postgres_store
):
    from collections import Counter
    from dataclasses import replace
    from sediment_derive import (
        derive_merge_retention_result,
        build_repository_context,
        repository_identity_evidence_of,
        RepositoryIdentity,
        Provenance,
    )
    from sediment_export import build_merge_retention_report

    mirrors, context, push, _, observation, _, _, merge = _setup(
        tmp_path, postgres_store
    )
    result = derive_merge_retention_result(
        postgres_store, mirrors, ORG, repository_context=context
    )
    [row] = result.rows
    other_identity = RepositoryIdentity("github", "github.com", "202")
    other_push = Push.model_validate(
        {**push.model_dump(), "push_id": "other-push", "repository_id": "202"}
    )
    other_observation = SessionCommitObservation.model_validate(
        {
            **observation.model_dump(),
            "observation_id": "other-observation",
            "source_push_id": other_push.push_id,
            "repository_id": "202",
        }
    )
    other_merge = PullRequestMerge.model_validate(
        {
            **merge.model_dump(),
            "merge_id": "other-merge",
            "repository_id": "202",
            "head_repository_id": "202",
        }
    )
    other_row = replace(
        row,
        repository_identity=other_identity,
        merge_id="other-merge",
        session_commit_observation_ids=(other_observation.observation_id,),
    )
    population = [push, observation, merge, other_push, other_observation, other_merge]
    shared = build_repository_context(
        [repository_identity_evidence_of(fact) for fact in population],
        (),
        ORG,
        as_of=END,
    )
    combined = replace(
        result,
        rows=[row, other_row],
        membership_outcomes=[],
        session_commit_observations=(observation, other_observation),
    )
    arguments = dict(
        explicit_accepted_inference_call_ids={"call"},
        attribution_skipped=Counter(),
        decision_attachment_skipped=Counter(),
        attribution_provenance=Provenance("test", 0),
    )
    report = build_merge_retention_report(
        ORG, combined, repository_context=shared, **arguments
    )
    assert (
        report.attributed_file_candidates
        == report.joined_pull_requests
        == report.scored_pull_requests
        == report.scored_rows
        == 2
    )
    assert (
        build_merge_retention_report(
            ORG,
            replace(
                combined,
                rows=list(reversed(combined.rows)),
                session_commit_observations=tuple(
                    reversed(combined.session_commit_observations)
                ),
            ),
            repository_context=shared,
            **arguments,
        )
        == report
    )
    missing = build_merge_retention_report(ORG, combined, **arguments)
    assert missing.attributed_file_candidates == missing.scored_rows == 0
    assert (
        missing.repository_skipped["candidate_edges"]["repository_identity_unresolved"]
        == 2
    )


@pytest.mark.parametrize("scoped", [False, True])
def test_lifecycle_run_conflict_outside_selected_commit_declines_verdict(
    tmp_path, postgres_store, scoped
):
    mirrors, _, _, _, _, red, *_ = _setup(tmp_path, postgres_store)
    sibling = CIOutcome.model_validate(
        {
            **red.model_dump(),
            "outcome_id": "contradictory",
            "run_attempt": 2,
            "repository_id": "202",
            "commit_sha": "c" * 40,
            "result": "passed",
            "captured_at": END,
        }
    )
    postgres_store.store_ci_outcome(sibling)
    scope = (
        OperationalReportScope(AT - timedelta(hours=1), AT + timedelta(minutes=1), END)
        if scoped
        else None
    )
    report = generate_accepted_work_lifecycle_report(
        postgres_store, mirrors, ORG, scope=scope, as_of=END
    )
    assert (
        report.accepted_work.accepted_calls
        == report.accepted_work.pull_request_membership.count
        == 1
    )
    assert report.accepted_work.ci_linked.count == 1
    assert report.accepted_work.ci_passed == report.accepted_work.ci_failed == 0
    assert report.accepted_work.skips["ambiguous_ci_verdict"] == 1


@pytest.mark.parametrize("legacy_observation", [False, True])
def test_lifecycle_missing_source_preserves_acceptance_and_counts_observation_loss(
    tmp_path, postgres_store, legacy_observation
):
    mirrors, _, first, *_ = _setup(
        tmp_path, postgres_store, legacy_observation=legacy_observation
    )
    postgres_store.quarantine_fact(ORG, "pushes", first.push_id, reason="synthetic")
    report = generate_accepted_work_lifecycle_report(
        postgres_store, mirrors, ORG, as_of=END
    )
    assert report.accepted_work.accepted_calls == 1
    assert (
        report.accepted_work.attributed.count
        == report.accepted_work.ci_linked.count
        == 0
    )
    assert (
        report.repository_skipped["session_observations"][
            "repository_identity_unresolved"
            if legacy_observation
            else "repository_source_absent"
        ]
        == 1
    )
    assert report.session_attrition.attribution_unavailable == 1


def test_lifecycle_and_merge_historical_boundary_does_not_use_future_merge(
    tmp_path, postgres_store
):
    mirrors, _, *_ = _setup(tmp_path, postgres_store)
    report = generate_accepted_work_lifecycle_report(
        postgres_store, mirrors, ORG, as_of=AT
    )
    assert (
        report.accepted_work.accepted_calls
        == report.accepted_work.attributed.count
        == 1
    )
    assert report.accepted_work.pull_request_membership.count == 0
    assert report.merge_durability.scored_rows == 0
    merged = generate_merge_retention_report_result(
        postgres_store, mirrors, ORG, as_of=AT
    )
    assert merged.report.attributed_file_candidates == 1
    assert (
        merged.report.candidates_joined_to_merge
        == merged.report.scored_rows
        == len(merged.rows)
        == 0
    )


def test_direct_and_scoped_model_reports_keep_assembly_source_loss(
    tmp_path, postgres_store
):
    from sediment_export import generate_model_report_result, generate_model_report
    from sediment_api.services.operational_reports import (
        generate_operational_model_report,
    )

    mirrors, _, source, *_ = _setup(tmp_path, postgres_store)
    postgres_store.quarantine_fact(ORG, "pushes", source.push_id, reason="synthetic")
    direct = generate_model_report_result(postgres_store, mirrors, ORG, now=END)
    scoped = generate_operational_model_report(
        postgres_store,
        mirrors,
        ORG,
        OperationalReportScope(AT - timedelta(hours=1), AT + timedelta(minutes=1), END),
    ).result
    assert direct.rows[0].completions == scoped.rows[0].completions == 1
    assert direct.rows[0].explicit_accepts == scoped.rows[0].explicit_accepts == 1
    assert (
        direct.repository_skipped["assembly_sources"]["repository_source_absent"] == 1
    )
    assert (
        scoped.repository_skipped["assembly_sources"]["repository_source_absent"] == 1
    )
    assert generate_model_report(postgres_store, mirrors, ORG, now=END) == direct.rows


def test_dataset_generator_preserves_identified_ci_confidence(tmp_path, postgres_store):
    from sediment_export import DPOPolicy, SFTPolicy
    from sediment_export.dataset_diagnostics import generate_dataset_diagnostics

    mirrors, _, first, *_ = _setup(tmp_path, postgres_store)
    result = generate_dataset_diagnostics(
        postgres_store,
        mirrors,
        ORG,
        dpo_policy=DPOPolicy(recipe_id="dpo_outcome"),
        sft_policy=SFTPolicy(min_confidence=0.8),
    )
    assert result.dpo_bucket_sparsity[0].total_candidates == 1
    # A resolved CI failure excludes the accept before the Confidence floor.
    assert result.model_balance == []
    assert result.confidence_floor_exclusions == []
    postgres_store.quarantine_fact(ORG, "pushes", first.push_id, reason="source absent")
    declined = generate_dataset_diagnostics(
        postgres_store,
        mirrors,
        ORG,
        dpo_policy=DPOPolicy(recipe_id="dpo_outcome"),
        sft_policy=SFTPolicy(min_confidence=0.8),
    )
    assert declined.dpo_bucket_sparsity[0].total_candidates == 0
    assert declined.model_balance == []


@pytest.mark.parametrize("report_kind", ["lifecycle", "merge"])
def test_reports_hold_repository_snapshot_through_all_git_reads(
    tmp_path, postgres_store, monkeypatch, report_kind
):
    import fcntl

    mirrors, _, *_ = _setup(tmp_path, postgres_store)
    original_open = mirrors.open_repository
    opened = []

    def open_locked(key):
        name = str(mirrors._repository_path(key).relative_to(mirrors.base))
        with mirrors._lock_file(name).open("w") as handle:
            with pytest.raises(BlockingIOError):
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        opened.append(key)
        return original_open(key)

    monkeypatch.setattr(mirrors, "open_repository", open_locked)
    if report_kind == "lifecycle":
        report = generate_accepted_work_lifecycle_report(
            postgres_store, mirrors, ORG, as_of=END
        )
        assert report.merge_durability.scored_rows == 1
    else:
        result = generate_merge_retention_report_result(
            postgres_store, mirrors, ORG, as_of=END
        )
        assert result.report.scored_rows == 1
    assert opened
