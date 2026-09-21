# SPDX-License-Identifier: AGPL-3.0-or-later
"""Attempt-aware CI resolution tests over immutable ``CIOutcome`` facts."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import get_type_hints

import pytest

from sediment_core import (
    BranchName,
    CIOutcome,
    CIProvider,
    CIResult,
    CommitSha,
    NonEmptyId,
    OrgId,
    RepoSlug,
    WorkflowName,
)
from sediment_derive import (
    CIResolution,
    CIResolutionPolicy,
    CIWorkflowResolution,
    Provenance,
    derive_ci_resolution_result,
)

ORG = "acme-corp"
REPO = "acme-corp/backend-service"
OTHER_REPO = "acme-corp/frontend-service"
SHA = "a" * 40
BASE = datetime(2026, 8, 21, 0, 0, tzinfo=UTC)


def _outcome(
    result: CIResult,
    *,
    outcome_id: str,
    run_id: str = "run-1",
    run_attempt: int | None = None,
    repo: str = REPO,
    commit_sha: str = SHA,
    workflow_id: str | None = "workflow-1",
    workflow_name: str = "CI",
    workflow_path: str | None = ".github/workflows/ci.yml",
    minutes: int = 0,
) -> CIOutcome:
    return CIOutcome(
        outcome_id=outcome_id,
        org_id=ORG,
        provider=CIProvider.GITHUB_ACTIONS,
        run_id=run_id,
        run_attempt=run_attempt,
        repo=repo,
        commit_sha=commit_sha,
        branch="main",
        result=result,
        workflow_id=workflow_id,
        workflow_name=workflow_name,
        workflow_path=workflow_path,
        provider_result=str(result),
        captured_at=BASE + timedelta(minutes=minutes),
    )


def _only(outcomes: list[CIOutcome], policy: CIResolutionPolicy | None = None):
    result = derive_ci_resolution_result(outcomes, policy)
    assert len(result.resolutions) == 1
    return result, result.resolutions[0]


def test_single_attempt_without_provider_attempt_keeps_the_observed_verdict() -> None:
    result, resolution = _only(
        [_outcome(CIResult.PASSED, outcome_id="single", run_attempt=None)]
    )

    assert result.skipped == {}
    assert resolution.verdict == CIResult.PASSED
    assert resolution.reliability == 1.0
    assert resolution.suspected_flake is False
    assert resolution.source_outcome_ids == ("single",)
    assert resolution.verdict_outcome_ids == ("single",)
    assert resolution.non_verdict_outcome_ids == ()


def test_numbered_fail_then_pass_uses_attempt_order_not_fact_metadata() -> None:
    failed = _outcome(
        CIResult.FAILED,
        outcome_id="z-generated-first",
        run_attempt=1,
        minutes=20,
    )
    passed = _outcome(
        CIResult.PASSED,
        outcome_id="a-generated-last",
        run_attempt=2,
        minutes=0,
    )

    _, resolution = _only([passed, failed])

    assert resolution.verdict == CIResult.PASSED
    assert resolution.reliability == 0.0
    assert resolution.suspected_flake is True
    assert resolution.verdict_outcome_ids == (passed.outcome_id,)
    assert set(resolution.source_outcome_ids) == {
        failed.outcome_id,
        passed.outcome_id,
    }


def test_numbered_pass_then_fail_resolves_to_the_failed_retry() -> None:
    passed = _outcome(CIResult.PASSED, outcome_id="passed", run_attempt=1)
    failed = _outcome(CIResult.FAILED, outcome_id="failed", run_attempt=2)

    _, resolution = _only([failed, passed])

    assert resolution.verdict == CIResult.FAILED
    assert resolution.reliability == 0.0
    assert resolution.suspected_flake is True
    assert resolution.verdict_outcome_ids == (failed.outcome_id,)


def test_null_attempt_sorts_as_attempt_zero_before_numbered_attempts() -> None:
    unnumbered = _outcome(CIResult.FAILED, outcome_id="null", run_attempt=None)
    numbered = _outcome(CIResult.PASSED, outcome_id="one", run_attempt=1)

    _, resolution = _only([numbered, unnumbered])

    assert resolution.verdict == CIResult.PASSED
    assert resolution.suspected_flake is True


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
def test_non_verdict_only_history_never_creates_a_label(
    non_verdict: CIResult,
) -> None:
    outcome = _outcome(non_verdict, outcome_id=str(non_verdict))

    result, resolution = _only([outcome])

    assert result.skipped == {}
    assert resolution.verdict is None
    assert resolution.reliability is None
    assert resolution.suspected_flake is False
    assert resolution.verdict_outcome_ids == ()
    assert resolution.non_verdict_outcome_ids == (outcome.outcome_id,)


def test_non_verdict_evidence_is_preserved_without_changing_reliability() -> None:
    failed = _outcome(CIResult.FAILED, outcome_id="failed", run_attempt=1)
    error = _outcome(CIResult.ERROR, outcome_id="error", run_attempt=2)
    passed = _outcome(CIResult.PASSED, outcome_id="passed", run_attempt=3)

    _, resolution = _only([passed, error, failed])

    assert resolution.verdict == CIResult.PASSED
    assert resolution.reliability == 0.0
    assert resolution.non_verdict_outcome_ids == (error.outcome_id,)


def test_separate_non_verdict_workflow_adjusts_aggregate_reliability() -> None:
    policy = CIResolutionPolicy(non_verdict_reliability=0.25)
    outcomes = [
        _outcome(CIResult.PASSED, outcome_id="tests", run_id="run-tests"),
        _outcome(
            CIResult.TIMED_OUT,
            outcome_id="lint-timeout",
            run_id="run-lint",
            workflow_id="workflow-2",
            workflow_name="Lint",
            workflow_path=".github/workflows/lint.yml",
        ),
    ]

    _, resolution = _only(outcomes, policy)

    assert resolution.verdict == CIResult.PASSED
    assert resolution.reliability == 0.25
    assert (
        next(
            workflow
            for workflow in resolution.workflow_resolutions
            if workflow.run_id == "run-lint"
        ).reliability
        == 0.25
    )


def test_resolution_identity_fields_use_validated_domain_types() -> None:
    resolution = get_type_hints(CIResolution, include_extras=True)
    workflow = get_type_hints(CIWorkflowResolution, include_extras=True)

    assert resolution["org_id"] == OrgId
    assert resolution["repo"] == RepoSlug
    assert resolution["commit_sha"] == CommitSha
    assert resolution["source_outcome_ids"] == tuple[NonEmptyId, ...]
    assert workflow["run_id"] == NonEmptyId
    assert workflow["workflow_id"] == NonEmptyId | None
    assert workflow["workflow_name"] == WorkflowName
    assert workflow["workflow_path"] == WorkflowName | None
    assert workflow["branch"] == BranchName


def test_conflicting_run_identity_is_skipped_and_counted() -> None:
    outcomes = [
        _outcome(CIResult.FAILED, outcome_id="one", run_attempt=1),
        _outcome(
            CIResult.PASSED,
            outcome_id="two",
            run_attempt=2,
            repo=OTHER_REPO,
        ),
    ]

    result = derive_ci_resolution_result(outcomes)

    assert result.resolutions == []
    assert result.skipped == {"conflicting_run_identity": 1}


def test_conflicting_workflow_verdicts_return_no_aggregate_verdict() -> None:
    outcomes = [
        _outcome(CIResult.PASSED, outcome_id="lint", run_id="run-lint"),
        _outcome(
            CIResult.FAILED,
            outcome_id="tests",
            run_id="run-tests",
            workflow_id="workflow-2",
            workflow_name="Tests",
            workflow_path=".github/workflows/tests.yml",
        ),
    ]

    result, resolution = _only(outcomes)

    assert result.skipped == {"ambiguous_workflow_verdicts": 1}
    assert resolution.verdict is None
    assert resolution.reliability is None
    assert resolution.verdict_outcome_ids == ("lint", "tests")


def test_agreeing_workflows_use_the_weakest_lineage_reliability() -> None:
    outcomes = [
        _outcome(
            CIResult.FAILED,
            outcome_id="lint-1",
            run_id="run-lint",
            run_attempt=1,
        ),
        _outcome(
            CIResult.PASSED,
            outcome_id="lint-2",
            run_id="run-lint",
            run_attempt=2,
        ),
        _outcome(
            CIResult.PASSED,
            outcome_id="tests",
            run_id="run-tests",
            workflow_id="workflow-2",
            workflow_name="Tests",
            workflow_path=".github/workflows/tests.yml",
        ),
    ]

    _, resolution = _only(outcomes)

    assert resolution.verdict == CIResult.PASSED
    assert resolution.reliability == 0.0
    assert resolution.suspected_flake is True


def test_same_commit_sha_in_two_repositories_resolves_in_isolation() -> None:
    outcomes = [
        _outcome(CIResult.PASSED, outcome_id="backend"),
        _outcome(
            CIResult.FAILED,
            outcome_id="frontend",
            run_id="run-2",
            repo=OTHER_REPO,
        ),
    ]

    result = derive_ci_resolution_result(outcomes)

    assert [(row.repo, row.verdict) for row in result.resolutions] == [
        (REPO, CIResult.PASSED),
        (OTHER_REPO, CIResult.FAILED),
    ]


def test_resolution_provenance_uses_the_resolution_policy() -> None:
    policy = CIResolutionPolicy(
        clean_reliability=0.9,
        suspected_flake_reliability=0.2,
        policy_version="7",
    )

    _, resolution = _only([_outcome(CIResult.PASSED, outcome_id="passed")], policy)

    assert resolution.reliability == 0.9
    assert resolution.provenance == Provenance(
        policy_version="7",
        quarantine_revision=0,
        policy_digest=policy.digest,
    )


def test_resolution_is_identical_across_repeated_and_shuffled_runs() -> None:
    outcomes = [
        _outcome(CIResult.FAILED, outcome_id="failed", run_attempt=1),
        _outcome(CIResult.ERROR, outcome_id="error", run_attempt=2),
        _outcome(CIResult.PASSED, outcome_id="passed", run_attempt=3),
        _outcome(
            CIResult.PASSED,
            outcome_id="tests",
            run_id="run-tests",
            workflow_id="workflow-2",
            workflow_name="Tests",
            workflow_path=".github/workflows/tests.yml",
        ),
    ]
    shuffled = list(outcomes)
    random.Random(543).shuffle(shuffled)

    first = derive_ci_resolution_result(outcomes)
    repeated = derive_ci_resolution_result(outcomes)
    reordered = derive_ci_resolution_result(shuffled)

    assert first == repeated == reordered
