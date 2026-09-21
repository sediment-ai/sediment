# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Recovery-pair derivation tests over real facts and real git fixtures — never
mocked (per AGENTS.md). Every scenario stores real ``CIOutcome`` facts and
mirrors an actual git repository through ``MirrorManager``, then runs
``derive_recovery_pairs`` as a consumer would.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from gitfixtures import commit_all, make_remote, make_work_repo, run_git
from sediment_core import (
    CIOutcome,
    CIProvider,
    CIResult,
    ForgeProvider,
    GatewayProvider,
    InferenceCall,
    InferenceMessage,
    Push,
    TextPart,
)
from sediment_core.store import FactStore
from sediment_derive import (
    MirrorError,
    MirrorManager,
    Provenance,
    RecoveryPolicy,
    RepoMirror,
    derive_recovery_pairs,
    derive_recovery_result,
)
from sediment_derive.recovery import _diff_line_count

ORG = "acme-corp"
REPO = "acme-corp/backend-service"
BRANCH = "main"
BASE = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)


def _at(minutes: int) -> datetime:
    return BASE + timedelta(minutes=minutes)


def _mirror_repo(tmp_path: Path, work: Path) -> MirrorManager:
    """Mirror the work repo's full history (every branch it pushed to the
    remote) through a real ``MirrorManager`` — recovery pairing reads the
    mirror directly and needs no stored ``Push`` fact."""
    remote = make_remote(tmp_path, work)
    mirrors = MirrorManager(str(tmp_path / "mirrors"))
    head = run_git(work, "rev-parse", "HEAD").strip()
    push = Push(
        org_id=ORG,
        provider=ForgeProvider.GITHUB,
        repo=REPO,
        clone_url=str(remote),
        ref="refs/heads/main",
        before_sha="0" * 40,
        after_sha=head,
    )
    mirrors.ensure(push)
    return mirrors


def _outcome(
    commit_sha: str,
    result: CIResult,
    *,
    minutes: int,
    repo: str = REPO,
    branch: str = BRANCH,
    workflow_name: str = "",
    workflow_path: str | None = ".github/workflows/tests.yml",
    run_id: str | None = None,
    run_attempt: int | None = None,
) -> CIOutcome:
    return CIOutcome(
        org_id=ORG,
        provider=CIProvider.GITHUB_ACTIONS,
        run_id=run_id or f"run-{minutes}-{result}-{commit_sha[:8]}",
        run_attempt=run_attempt,
        repo=repo,
        commit_sha=commit_sha,
        branch=branch,
        result=result,
        workflow_name=workflow_name,
        workflow_path=workflow_path,
        captured_at=_at(minutes),
    )


def _inference_call(session_id: str, prompt: str, output: str) -> InferenceCall:
    return InferenceCall(
        org_id=ORG,
        session_id=session_id,
        user_id="dev",
        gateway_provider=GatewayProvider.LITELLM,
        model="claude-sonnet-5",
        input_messages=[
            InferenceMessage(role="user", parts=[TextPart(content=prompt)])
        ],
        output_messages=[
            InferenceMessage(role="assistant", parts=[TextPart(content=output)])
        ],
        input_tokens=10,
        output_tokens=20,
        duration_ms=50,
        observed_at=datetime.now(UTC) - timedelta(minutes=5),
    )


def test_failed_then_passed_pairs_with_the_fixing_diff(
    tmp_path: Path, postgres_store
) -> None:
    work = make_work_repo(tmp_path)
    (work / "adder.py").write_text("def add(a, b):\n    return a - b\n")
    failed_sha = commit_all(work, "add adder (buggy)")
    (work / "adder.py").write_text("def add(a, b):\n    return a + b\n")
    fixed_sha = commit_all(work, "fix adder")
    mirrors = _mirror_repo(tmp_path, work)
    store = postgres_store
    failed = _outcome(failed_sha, CIResult.FAILED, minutes=0)
    fixed = _outcome(fixed_sha, CIResult.PASSED, minutes=1)
    store.store_ci_outcome(failed)
    store.store_ci_outcome(fixed)

    samples = derive_recovery_pairs(store, mirrors, ORG)

    assert len(samples) == 1
    s = samples[0]
    assert s.org_id == ORG
    assert s.repo == REPO
    assert s.branch == BRANCH
    assert s.failed_commit_sha == failed_sha
    assert s.fixed_commit_sha == fixed_sha
    assert s.failed_outcome_id == failed.outcome_id
    assert s.fixed_outcome_id == fixed.outcome_id
    assert "-    return a - b" in s.recovery_diff
    assert "+    return a + b" in s.recovery_diff
    mirror = mirrors.open(ORG, REPO)
    assert mirror is not None
    assert s.recovery_diff == mirror.diff_range(failed_sha, fixed_sha)
    assert s.failed_inference_call_ids == []
    assert s.fixed_inference_call_ids == []
    assert s.provenance == Provenance(policy_version="5", quarantine_revision=0)


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
def test_non_verdict_outcome_between_failed_and_passed_still_pairs(
    tmp_path: Path, postgres_store, non_verdict: CIResult
) -> None:
    work = make_work_repo(tmp_path)
    (work / "adder.py").write_text("def add(a, b):\n    return a - b\n")
    failed_sha = commit_all(work, "add adder (buggy)")
    (work / "adder.py").write_text("def add(a, b):\n    return a - b  # wip\n")
    mid_sha = commit_all(work, "wip, CI run cancelled")
    (work / "adder.py").write_text("def add(a, b):\n    return a + b\n")
    fixed_sha = commit_all(work, "fix adder")
    mirrors = _mirror_repo(tmp_path, work)
    store = postgres_store
    store.store_ci_outcome(_outcome(failed_sha, CIResult.FAILED, minutes=0))
    store.store_ci_outcome(_outcome(mid_sha, non_verdict, minutes=1))
    store.store_ci_outcome(_outcome(fixed_sha, CIResult.PASSED, minutes=2))

    samples = derive_recovery_pairs(store, mirrors, ORG)

    assert len(samples) == 1
    assert samples[0].failed_commit_sha == failed_sha
    assert samples[0].fixed_commit_sha == fixed_sha


def test_consecutive_failed_pairs_only_the_last_red_with_the_green(
    tmp_path: Path,
    postgres_store,
) -> None:
    work = make_work_repo(tmp_path)
    (work / "adder.py").write_text("def add(a, b):\n    return a - b\n")
    failed1_sha = commit_all(work, "add adder (buggy v1)")
    (work / "adder.py").write_text("def add(a, b):\n    return a * b\n")
    failed2_sha = commit_all(work, "add adder (buggy v2)")
    (work / "adder.py").write_text("def add(a, b):\n    return a + b\n")
    fixed_sha = commit_all(work, "fix adder")
    mirrors = _mirror_repo(tmp_path, work)
    store = postgres_store
    store.store_ci_outcome(_outcome(failed1_sha, CIResult.FAILED, minutes=0))
    store.store_ci_outcome(_outcome(failed2_sha, CIResult.FAILED, minutes=1))
    store.store_ci_outcome(_outcome(fixed_sha, CIResult.PASSED, minutes=2))

    samples = derive_recovery_pairs(store, mirrors, ORG)

    assert len(samples) == 1
    assert samples[0].failed_commit_sha == failed2_sha
    assert samples[0].fixed_commit_sha == fixed_sha


def test_retried_pass_does_not_seed_a_phantom_recovery_pair(
    tmp_path: Path, postgres_store
) -> None:
    work = make_work_repo(tmp_path)
    (work / "adder.py").write_text("def add(a, b):\n    return a - b\n")
    retried_sha = commit_all(work, "flaky commit")
    (work / "adder.py").write_text("def add(a, b):\n    return a + b\n")
    later_sha = commit_all(work, "unrelated later change")
    mirrors = _mirror_repo(tmp_path, work)
    store = postgres_store
    run_id = "run-retried"
    passed_retry = _outcome(
        retried_sha,
        CIResult.PASSED,
        minutes=0,
        run_id=run_id,
        run_attempt=2,
    ).model_copy(update={"outcome_id": _ID_LO})
    failed_first = _outcome(
        retried_sha,
        CIResult.FAILED,
        minutes=0,
        run_id=run_id,
        run_attempt=1,
    ).model_copy(update={"outcome_id": _ID_HI})
    later_pass = _outcome(later_sha, CIResult.PASSED, minutes=1)
    for outcome in (passed_retry, failed_first, later_pass):
        store.store_ci_outcome(outcome)

    result = derive_recovery_result(store, mirrors, ORG)

    assert result.pairs == []
    assert result.skipped["unreliable_ci_resolution"] == 1


def test_ambiguous_commit_verdict_cannot_seed_recovery(
    tmp_path: Path, postgres_store
) -> None:
    work = make_work_repo(tmp_path)
    (work / "adder.py").write_text("def add(a, b):\n    return a - b\n")
    failed_sha = commit_all(work, "add adder (buggy)")
    (work / "adder.py").write_text("def add(a, b):\n    return a + b\n")
    fixed_sha = commit_all(work, "fix adder")
    mirrors = _mirror_repo(tmp_path, work)
    store = postgres_store

    store.store_ci_outcome(
        _outcome(
            failed_sha,
            CIResult.FAILED,
            minutes=0,
            workflow_name="tests",
        )
    )
    store.store_ci_outcome(
        _outcome(
            failed_sha,
            CIResult.PASSED,
            minutes=0,
            workflow_name="lint",
        )
    )
    store.store_ci_outcome(
        _outcome(
            fixed_sha,
            CIResult.PASSED,
            minutes=1,
            workflow_name="tests",
        )
    )

    result = derive_recovery_result(store, mirrors, ORG)

    assert result.pairs == []
    assert result.skipped["ambiguous_workflow_verdicts"] == 1


def test_non_ancestor_pair_is_skipped(tmp_path: Path, postgres_store) -> None:
    # Simulates a force-push / branch-name-reuse scenario: the "fixed" commit
    # is not reachable from the "failed" one at all — an unrelated history.
    work = make_work_repo(tmp_path)
    (work / "adder.py").write_text("def add(a, b):\n    return a - b\n")
    failed_sha = commit_all(work, "add adder (buggy)")
    run_git(work, "checkout", "-q", "--orphan", "unrelated")
    run_git(work, "rm", "-rqf", ".")
    (work / "adder.py").write_text("def add(a, b):\n    return a + b\n")
    fixed_sha = commit_all(work, "unrelated root commit")
    run_git(work, "checkout", "-q", "main")
    mirrors = _mirror_repo(tmp_path, work)
    store = postgres_store
    store.store_ci_outcome(_outcome(failed_sha, CIResult.FAILED, minutes=0))
    store.store_ci_outcome(_outcome(fixed_sha, CIResult.PASSED, minutes=1))

    mirror = mirrors.open(ORG, REPO)
    assert mirror is not None
    assert not mirror.is_ancestor(failed_sha, fixed_sha)

    samples = derive_recovery_pairs(store, mirrors, ORG)

    assert samples == []


def test_oversized_diff_is_skipped_and_logged(
    tmp_path: Path, postgres_store, caplog: pytest.LogCaptureFixture
) -> None:
    work = make_work_repo(tmp_path)
    (work / "adder.py").write_text(
        "def add(a, b):\n    return a - b\n\n\ndef sub(a, b):\n    return a + b\n"
    )
    failed_sha = commit_all(work, "add adder+sub (both buggy)")
    (work / "adder.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"
    )
    fixed_sha = commit_all(work, "fix adder and sub")
    mirrors = _mirror_repo(tmp_path, work)
    store = postgres_store
    store.store_ci_outcome(_outcome(failed_sha, CIResult.FAILED, minutes=0))
    store.store_ci_outcome(_outcome(fixed_sha, CIResult.PASSED, minutes=1))
    # The real diff here changes 4 lines (2 removed, 2 added); a cap of 2
    # makes it oversized without needing a huge fixture.
    policy = RecoveryPolicy(max_recovery_diff_lines=2)

    with caplog.at_level(logging.DEBUG, logger="sediment.derive.recovery"):
        samples = derive_recovery_pairs(store, mirrors, ORG, policy)

    assert samples == []
    assert any(
        r.message == "recovery_diff_oversized"
        and r.__dict__.get("failed_commit") == failed_sha
        and r.__dict__.get("fixed_commit") == fixed_sha
        for r in caplog.records
    )


def test_recovery_result_tracks_diff_sizes_at_actual_cap_boundary(
    tmp_path: Path,
    postgres_store,
) -> None:
    work = make_work_repo(tmp_path)
    sizes = [2, 9, 10, 11, 30]
    candidates: list[tuple[str, str, str]] = []
    for index, line_count in enumerate(sizes):
        (work / f"red_{index}.txt").write_text(f"red {index}\n")
        failed_sha = commit_all(work, f"red candidate {index}")
        (work / f"fix_{index}.txt").write_text(
            "".join(f"line {line}\n" for line in range(line_count))
        )
        fixed_sha = commit_all(work, f"fix candidate {index}")
        candidates.append((f"case-{index:02d}", failed_sha, fixed_sha))

    mirrors = _mirror_repo(tmp_path, work)
    store = postgres_store
    for index, (branch, failed_sha, fixed_sha) in enumerate(candidates):
        store.store_ci_outcome(
            _outcome(failed_sha, CIResult.FAILED, minutes=index * 2, branch=branch)
        )
        store.store_ci_outcome(
            _outcome(
                fixed_sha,
                CIResult.PASSED,
                minutes=index * 2 + 1,
                branch=branch,
            )
        )

    result = derive_recovery_result(
        store,
        mirrors,
        ORG,
        RecoveryPolicy(max_recovery_diff_lines=10),
    )

    assert [_diff_line_count(pair.recovery_diff) for pair in result.pairs] == [
        2,
        9,
        10,
    ]
    assert result.skipped == Counter({"diff_oversized": 2})
    assert result.max_recovery_diff_lines == 10
    assert result.kept_diff_line_counts == [2, 9, 10]
    assert result.dropped_diff_line_counts == [11, 30]


def test_no_mirror_on_disk_yields_zero_pairs_no_crash(
    tmp_path: Path, postgres_store
) -> None:
    store = postgres_store
    store.store_ci_outcome(_outcome("a" * 40, CIResult.FAILED, minutes=0))
    store.store_ci_outcome(_outcome("b" * 40, CIResult.PASSED, minutes=1))
    mirrors = MirrorManager(str(tmp_path / "mirrors"))  # nothing ever mirrored

    samples = derive_recovery_pairs(store, mirrors, ORG)

    assert samples == []


def test_recovery_result_counts_pairs_and_every_skip_reason(
    tmp_path: Path, postgres_store
) -> None:
    work = make_work_repo(tmp_path)

    (work / "adder.py").write_text("def add(a, b):\n    return a - b\n")
    failed_sha = commit_all(work, "add buggy adder")
    (work / "adder.py").write_text("def add(a, b):\n    return a + b\n")
    fixed_sha = commit_all(work, "fix adder")

    (work / "big.py").write_text("a = 1\nb = 2\nc = 3\nd = 4\n")
    oversized_failed_sha = commit_all(work, "add oversized buggy file")
    (work / "big.py").write_text("a = 10\nb = 20\nc = 30\nd = 40\n")
    oversized_fixed_sha = commit_all(work, "fix oversized file")

    run_git(work, "checkout", "-q", "--orphan", "unrelated")
    run_git(work, "rm", "-rqf", ".")
    (work / "other.py").write_text("x = 1\n")
    unrelated_sha = commit_all(work, "unrelated root commit")
    run_git(work, "checkout", "-q", "main")

    mirrors = _mirror_repo(tmp_path, work)
    store = postgres_store
    store.store_ci_outcome(
        _outcome(failed_sha, CIResult.FAILED, minutes=0, branch="success")
    )
    store.store_ci_outcome(
        _outcome(fixed_sha, CIResult.PASSED, minutes=1, branch="success")
    )
    store.store_ci_outcome(
        _outcome(
            fixed_sha,
            CIResult.FAILED,
            minutes=2,
            branch="flaky",
            run_id="run-flaky",
            run_attempt=1,
        )
    )
    store.store_ci_outcome(
        _outcome(
            fixed_sha,
            CIResult.PASSED,
            minutes=3,
            branch="flaky",
            run_id="run-flaky",
            run_attempt=2,
        )
    )
    store.store_ci_outcome(
        _outcome(failed_sha, CIResult.FAILED, minutes=4, branch="force-push")
    )
    store.store_ci_outcome(
        _outcome(unrelated_sha, CIResult.PASSED, minutes=5, branch="force-push")
    )
    store.store_ci_outcome(
        _outcome(
            oversized_failed_sha,
            CIResult.FAILED,
            minutes=6,
            branch="oversized",
        )
    )
    store.store_ci_outcome(
        _outcome(
            oversized_fixed_sha,
            CIResult.PASSED,
            minutes=7,
            branch="oversized",
        )
    )
    # Well-formed sha (the schema rejects malformed ones at construction)
    # that no commit in the mirror carries: ancestry check errors out.
    store.store_ci_outcome(
        _outcome("e" * 40, CIResult.FAILED, minutes=8, branch="bad-sha")
    )
    store.store_ci_outcome(
        _outcome(fixed_sha, CIResult.PASSED, minutes=9, branch="bad-sha")
    )

    no_mirror_repo = "acme-corp/unmirrored"
    store.store_ci_outcome(
        _outcome(
            "a" * 40,
            CIResult.FAILED,
            minutes=10,
            repo=no_mirror_repo,
            branch="main",
        )
    )
    store.store_ci_outcome(
        _outcome(
            "b" * 40,
            CIResult.PASSED,
            minutes=11,
            repo=no_mirror_repo,
            branch="main",
        )
    )

    diff_unavailable_repo = "acme-corp/diff-unavailable"
    store.store_ci_outcome(
        _outcome(
            "c" * 40,
            CIResult.FAILED,
            minutes=12,
            repo=diff_unavailable_repo,
            branch="main",
        )
    )
    store.store_ci_outcome(
        _outcome(
            "d" * 40,
            CIResult.PASSED,
            minutes=13,
            repo=diff_unavailable_repo,
            branch="main",
        )
    )

    class DiffUnavailableMirror(RepoMirror):
        def is_ancestor(self, ancestor_sha: str, descendant_sha: str) -> bool:
            return True

        def diff_range(self, base_sha: str, head_sha: str) -> str:
            raise MirrorError("diff unavailable")

    class OverlayMirrorManager:
        def __init__(self, real: MirrorManager, overlay: RepoMirror) -> None:
            self.real = real
            self.overlay = overlay

        def open_repository(self, key) -> RepoMirror | None:
            if key.repo == diff_unavailable_repo:
                return self.overlay
            return self.real.open_repository(key)

        def read_repository_snapshot(self, keys):
            return self.real.read_repository_snapshot(keys)

    overlay = OverlayMirrorManager(mirrors, DiffUnavailableMirror(tmp_path))
    policy = RecoveryPolicy(max_recovery_diff_lines=2)

    result = derive_recovery_result(store, overlay, ORG, policy)

    assert len(result.pairs) == 1
    assert result.pairs[0].failed_commit_sha == failed_sha
    assert result.pairs[0].fixed_commit_sha == fixed_sha
    assert result.skipped == Counter(
        {
            "unreliable_ci_resolution": 1,
            "not_ancestor": 1,
            "diff_oversized": 1,
            "ancestry_check_failed": 1,
            "mirror_absent": 1,
            "diff_unavailable": 1,
        }
    )
    assert derive_recovery_pairs(store, overlay, ORG, policy) == result.pairs


def test_cross_workflow_green_never_pairs_with_another_checks_red(
    tmp_path: Path,
    postgres_store,
) -> None:
    # Two checks on one branch: "tests" goes red on X and stays red on Y,
    # while the fast "lint" check goes green on Y in between. Without
    # per-workflow lineages, (X tests-FAILED, Y lint-PASSED) would emit a
    # fabricated "fix" for a commit whose tests still fail.
    work = make_work_repo(tmp_path)
    (work / "adder.py").write_text("def add(a, b):\n    return a - b\n")
    failed_sha = commit_all(work, "add adder (buggy)")
    (work / "adder.py").write_text("def add(a, b):\n    return a % b\n")
    still_bad_sha = commit_all(work, "attempt fix (still buggy)")
    mirrors = _mirror_repo(tmp_path, work)
    store = postgres_store
    tests = dict(workflow_name="tests", workflow_path=".github/workflows/tests.yml")
    lint = dict(workflow_name="lint", workflow_path=".github/workflows/lint.yml")
    store.store_ci_outcome(_outcome(failed_sha, CIResult.FAILED, minutes=0, **tests))
    store.store_ci_outcome(_outcome(still_bad_sha, CIResult.PASSED, minutes=1, **lint))
    store.store_ci_outcome(_outcome(still_bad_sha, CIResult.FAILED, minutes=2, **tests))

    assert derive_recovery_pairs(store, mirrors, ORG) == []


def test_pairs_form_within_one_workflow_stream_and_carry_its_identity(
    tmp_path: Path,
    postgres_store,
) -> None:
    # The real fix pairs inside the "tests" lineage even though a "lint"
    # PASSED lands between the red and the green in capture order.
    work = make_work_repo(tmp_path)
    (work / "adder.py").write_text("def add(a, b):\n    return a - b\n")
    failed_sha = commit_all(work, "add adder (buggy)")
    (work / "adder.py").write_text("def add(a, b):\n    return a + b\n")
    fixed_sha = commit_all(work, "fix adder")
    mirrors = _mirror_repo(tmp_path, work)
    store = postgres_store
    tests = dict(workflow_name="tests", workflow_path=".github/workflows/tests.yml")
    lint = dict(workflow_name="lint", workflow_path=".github/workflows/lint.yml")
    failed = _outcome(failed_sha, CIResult.FAILED, minutes=0, **tests)
    passed_lint = _outcome(fixed_sha, CIResult.PASSED, minutes=1, **lint)
    passed_tests = _outcome(fixed_sha, CIResult.PASSED, minutes=2, **tests)
    store.store_ci_outcome(failed)
    store.store_ci_outcome(passed_lint)
    store.store_ci_outcome(passed_tests)

    samples = derive_recovery_pairs(store, mirrors, ORG)

    assert len(samples) == 1
    s = samples[0]
    assert s.failed_outcome_id == failed.outcome_id
    assert s.fixed_outcome_id == passed_tests.outcome_id
    assert s.workflow_name == "tests"
    assert s.workflow_path == ".github/workflows/tests.yml"


def test_same_commit_rerun_pass_spends_the_failed(
    tmp_path: Path, postgres_store
) -> None:
    # A flaky-test rerun: the same workflow goes red then green on the
    # identical commit. No diff exists, and the rerun proved the commit was
    # never really red — so the FAILED is spent, and a later green on a
    # descendant commit does not resurrect it.
    work = make_work_repo(tmp_path)
    (work / "adder.py").write_text("def add(a, b):\n    return a - b\n")
    flaky_sha = commit_all(work, "add adder (flaky red)")
    (work / "adder.py").write_text("def add(a, b):\n    return a + b\n")
    later_sha = commit_all(work, "unrelated later change")
    mirrors = _mirror_repo(tmp_path, work)
    store = postgres_store
    store.store_ci_outcome(_outcome(flaky_sha, CIResult.FAILED, minutes=0))
    store.store_ci_outcome(_outcome(flaky_sha, CIResult.PASSED, minutes=1))
    store.store_ci_outcome(_outcome(later_sha, CIResult.PASSED, minutes=2))

    assert derive_recovery_pairs(store, mirrors, ORG) == []


def test_gate_failed_candidate_still_spends_the_failed(
    tmp_path: Path, postgres_store
) -> None:
    # The next PASSED is *the* candidate fix, not the start of a search: when
    # it fails the ancestry gate (force-push / branch reuse), the FAILED is
    # spent and a later legitimate PASSED does not pair either.
    work = make_work_repo(tmp_path)
    (work / "adder.py").write_text("def add(a, b):\n    return a - b\n")
    failed_sha = commit_all(work, "add adder (buggy)")
    run_git(work, "checkout", "-q", "--orphan", "unrelated")
    run_git(work, "rm", "-rqf", ".")
    (work / "other.py").write_text("x = 1\n")
    unrelated_sha = commit_all(work, "unrelated root commit")
    run_git(work, "checkout", "-q", "main")
    (work / "adder.py").write_text("def add(a, b):\n    return a + b\n")
    real_fix_sha = commit_all(work, "fix adder")
    mirrors = _mirror_repo(tmp_path, work)
    store = postgres_store
    store.store_ci_outcome(_outcome(failed_sha, CIResult.FAILED, minutes=0))
    store.store_ci_outcome(_outcome(unrelated_sha, CIResult.PASSED, minutes=1))
    store.store_ci_outcome(_outcome(real_fix_sha, CIResult.PASSED, minutes=2))

    assert derive_recovery_pairs(store, mirrors, ORG) == []


def test_enrichment_attaches_attributed_inference_calls_to_the_failed_commit(
    tmp_path: Path,
    postgres_store,
) -> None:
    # A stored push whose head is the failed commit + a completion whose text
    # matches that commit's added lines: the jaccard attribution binds it, and
    # the pair's failed_inference_call_ids carry it. (A zero-``before`` push walks
    # head-only, so the push must name the failed commit as its head.)
    buggy = "def add(a, b):\n    return a - b\n"
    work = make_work_repo(tmp_path)
    (work / "adder.py").write_text(buggy)
    failed_sha = commit_all(work, "add adder (buggy)")
    (work / "adder.py").write_text("def add(a, b):\n    return a + b\n")
    fixed_sha = commit_all(work, "fix adder")
    store = postgres_store
    mirrors = _mirror_repo(tmp_path, work)
    store.store_push(
        Push(
            org_id=ORG,
            provider=ForgeProvider.GITHUB,
            repo=REPO,
            clone_url="unused-local-mirror",
            ref="refs/heads/main",
            before_sha="0" * 40,
            after_sha=failed_sha,
        )
    )
    completion = _inference_call("sess-1", "write add", buggy)
    store.store_inference_call(completion)
    store.store_ci_outcome(_outcome(failed_sha, CIResult.FAILED, minutes=0))
    store.store_ci_outcome(_outcome(fixed_sha, CIResult.PASSED, minutes=1))

    samples = derive_recovery_pairs(store, mirrors, ORG)

    assert len(samples) == 1
    assert samples[0].failed_inference_call_ids == [completion.inference_call_id]
    assert samples[0].fixed_inference_call_ids == []

    from sediment_core import SessionCommitObservation
    from sediment_derive import AttributionSource

    [evidence] = samples[0].failed_attribution_evidence
    assert evidence.inference_call_id == completion.inference_call_id
    assert evidence.attribution_sources == (AttributionSource.JACCARD,)
    assert evidence.session_commit_observation_ids == ()
    fact = SessionCommitObservation(
        observation_id="enrichment-observation",
        org_id=ORG,
        repo=REPO,
        commit_sha=failed_sha,
        session_id="sess-1",
        source_push_id="push",
    )
    store.store_session_commit_observation(fact)
    [enriched] = derive_recovery_pairs(store, mirrors, ORG)
    assert enriched.failed_attribution_evidence[0].session_commit_observation_ids == (
        "enrichment-observation",
    )
    assert enriched.fixed_attribution_evidence == ()


def test_enrichment_attaches_attributed_inference_calls_to_the_fixed_commit(
    tmp_path: Path,
    postgres_store,
) -> None:
    # Mirror image of the failed-side test: the push is headed at the FIXED
    # commit and the completion's text matches that commit's added lines, so
    # the pair's fixed_inference_call_ids carry it while failed_inference_call_ids stays
    # empty. The eval split reads both sides.
    buggy = "def add(a, b):\n    return a - b\n"
    fixed_src = "def add(a, b):\n    return a + b\n"
    # The fixed commit adds one line; the jaccard bind scores the completion
    # against that line's tokens, so the completion text is the added line
    # itself (a snippet completion — the common case for a one-line fix).
    completion_text = "return a + b"
    work = make_work_repo(tmp_path)
    (work / "adder.py").write_text(buggy)
    failed_sha = commit_all(work, "add adder (buggy)")
    (work / "adder.py").write_text(fixed_src)
    fixed_sha = commit_all(work, "fix adder")
    store = postgres_store
    mirrors = _mirror_repo(tmp_path, work)
    store.store_push(
        Push(
            org_id=ORG,
            provider=ForgeProvider.GITHUB,
            repo=REPO,
            clone_url="unused-local-mirror",
            ref="refs/heads/main",
            before_sha="0" * 40,
            after_sha=fixed_sha,
        )
    )
    completion = _inference_call("sess-fix", "fix add", completion_text)
    store.store_inference_call(completion)
    store.store_ci_outcome(_outcome(failed_sha, CIResult.FAILED, minutes=0))
    store.store_ci_outcome(_outcome(fixed_sha, CIResult.PASSED, minutes=1))

    samples = derive_recovery_pairs(store, mirrors, ORG)

    assert len(samples) == 1
    assert samples[0].failed_inference_call_ids == []
    assert samples[0].fixed_inference_call_ids == [completion.inference_call_id]

    from sediment_core import SessionCommitObservation
    from sediment_derive import AttributionSource

    [evidence] = samples[0].fixed_attribution_evidence
    assert evidence.inference_call_id == completion.inference_call_id
    assert evidence.attribution_sources == (AttributionSource.JACCARD,)
    assert evidence.session_commit_observation_ids == ()
    fact = SessionCommitObservation(
        observation_id="enrichment-observation",
        org_id=ORG,
        repo=REPO,
        commit_sha=fixed_sha,
        session_id="sess-fix",
        source_push_id="push",
    )
    store.store_session_commit_observation(fact)
    [enriched] = derive_recovery_pairs(store, mirrors, ORG)
    assert enriched.fixed_attribution_evidence[0].session_commit_observation_ids == (
        "enrichment-observation",
    )
    assert enriched.failed_attribution_evidence == ()


def test_fixed_side_stamping_is_ingest_order_independent(
    tmp_path: Path, postgres_store_factory
) -> None:
    # Same facts, shuffled ingest order -> identical samples (ADR 0001). The
    # fixed-side enrichment reads the org-wide attribution map, so the
    # shuffle must not change which side an id lands on either.
    buggy = "def add(a, b):\n    return a - b\n"
    fixed_src = "def add(a, b):\n    return a + b\n"
    completion_text = "return a + b"  # the fixed commit's added line
    work = make_work_repo(tmp_path)
    (work / "adder.py").write_text(buggy)
    failed_sha = commit_all(work, "add adder (buggy)")
    (work / "adder.py").write_text(fixed_src)
    fixed_sha = commit_all(work, "fix adder")
    mirrors = _mirror_repo(tmp_path, work)

    # Construct the facts once — ids are uuid-stamped at construction, so two
    # constructions are not the same facts. Only the WRITE order shuffles.
    completion = _inference_call("sess-fix", "fix add", completion_text)
    push = Push(
        org_id=ORG,
        provider=ForgeProvider.GITHUB,
        repo=REPO,
        clone_url="unused-local-mirror",
        ref="refs/heads/main",
        before_sha="0" * 40,
        after_sha=fixed_sha,
    )
    outcomes = [
        _outcome(failed_sha, CIResult.FAILED, minutes=0),
        _outcome(fixed_sha, CIResult.PASSED, minutes=1),
    ]

    def write_all(store: FactStore, *, reversed_order: bool) -> None:
        writes = [
            lambda: store.store_push(push),
            lambda: store.store_inference_call(completion),
            *[lambda o=o: store.store_ci_outcome(o) for o in outcomes],
        ]
        if reversed_order:
            writes.reverse()
        for write in writes:
            write()

    _, store_a = postgres_store_factory()
    write_all(store_a, reversed_order=False)
    _, store_b = postgres_store_factory()
    write_all(store_b, reversed_order=True)

    assert derive_recovery_pairs(store_a, mirrors, ORG) == derive_recovery_pairs(
        store_b, mirrors, ORG
    )


def test_diff_line_count_is_hunk_aware() -> None:
    # Removed content beginning "--" (an SQL comment) renders as "---…"
    # inside a hunk; a bare prefix filter would mistake it for a file header
    # and undercount, letting oversized diffs evade the cap.
    diff = (
        "diff --git a/q.sql b/q.sql\n"
        "index 1111111..2222222 100644\n"
        "--- a/q.sql\n"
        "+++ b/q.sql\n"
        "@@ -1,3 +1,3 @@\n"
        "--- old sql comment\n"
        "+++ new sql comment\n"
        " select 1;\n"
    )
    assert _diff_line_count(diff) == 2


def test_determinism_same_facts_and_policy_yield_identical_samples(
    tmp_path: Path,
    postgres_store,
) -> None:
    work = make_work_repo(tmp_path)
    (work / "adder.py").write_text("def add(a, b):\n    return a - b\n")
    failed_sha = commit_all(work, "add adder (buggy)")
    (work / "adder.py").write_text("def add(a, b):\n    return a + b\n")
    fixed_sha = commit_all(work, "fix adder")
    mirrors = _mirror_repo(tmp_path, work)
    store = postgres_store
    # Stored out of capture order — ordering is a function of the facts, never
    # ingest/read order (ADR 0001).
    store.store_ci_outcome(_outcome(fixed_sha, CIResult.PASSED, minutes=1))
    store.store_ci_outcome(_outcome(failed_sha, CIResult.FAILED, minutes=0))

    first = derive_recovery_pairs(store, mirrors, ORG)
    second = derive_recovery_pairs(store, mirrors, ORG)

    assert first == second
    assert len(first) == 1


def _tied_repo(tmp_path: Path) -> tuple[str, str, MirrorManager]:
    """A work repo with a buggy commit then its fix, mirrored — the two
    shas for a tied-timestamp red/green pair."""
    work = make_work_repo(tmp_path)
    (work / "adder.py").write_text("def add(a, b):\n    return a - b\n")
    failed_sha = commit_all(work, "add adder (buggy)")
    (work / "adder.py").write_text("def add(a, b):\n    return a + b\n")
    fixed_sha = commit_all(work, "fix adder")
    return failed_sha, fixed_sha, _mirror_repo(tmp_path, work)


# Pinned fact ids for the tied-timestamp tests: with random uuid4 ids the
# (captured_at, outcome_id) tie-break makes red-before-green a coin flip per
# test run, and a determinism assertion over zero pairs is vacuous. Pinning
# the ids makes each test exercise one ordering on every run.
_ID_LO = "00000000-0000-4000-8000-000000000001"
_ID_HI = "00000000-0000-4000-8000-000000000002"


def test_determinism_tied_captured_at_red_first_derives_the_pair(
    tmp_path: Path,
    postgres_store_factory,
) -> None:
    """Red and green CI outcomes sharing an identical ``captured_at`` must
    derive the same pairs across repeated runs and shuffled ingest order
    (ADR 0001). The tie-break is ``(captured_at, outcome_id)``: with the red
    fact's id sorting first, the pair derives — on every run, from either
    ingest order."""
    failed_sha, fixed_sha, mirrors = _tied_repo(tmp_path)
    failed = _outcome(failed_sha, CIResult.FAILED, minutes=0).model_copy(
        update={"outcome_id": _ID_LO}
    )
    fixed = _outcome(fixed_sha, CIResult.PASSED, minutes=0).model_copy(
        update={"outcome_id": _ID_HI}
    )

    # Same DB, two reads — must match, and the pair must actually derive.
    _, store_a = postgres_store_factory()
    store_a.store_ci_outcome(failed)
    store_a.store_ci_outcome(fixed)
    first = derive_recovery_pairs(store_a, mirrors, ORG)
    second = derive_recovery_pairs(store_a, mirrors, ORG)
    assert first == second
    assert len(first) == 1

    # Different DB, reversed write order — must match the first store's result.
    _, store_b = postgres_store_factory()
    store_b.store_ci_outcome(fixed)
    store_b.store_ci_outcome(failed)
    third = derive_recovery_pairs(store_b, mirrors, ORG)
    assert third == first


def test_determinism_tied_captured_at_green_first_derives_no_pair(
    tmp_path: Path,
    postgres_store_factory,
) -> None:
    """The other side of the tie: when the green fact's id sorts first, the
    ordering is PASSED-then-FAILED — no red-to-green transition, so no pair
    derives. Zero is the deterministic answer for these facts — the pair's
    existence under a tie is a pure function of fact identity — from either
    ingest order."""
    failed_sha, fixed_sha, mirrors = _tied_repo(tmp_path)
    failed = _outcome(failed_sha, CIResult.FAILED, minutes=0).model_copy(
        update={"outcome_id": _ID_HI}
    )
    fixed = _outcome(fixed_sha, CIResult.PASSED, minutes=0).model_copy(
        update={"outcome_id": _ID_LO}
    )

    _, store_a = postgres_store_factory()
    store_a.store_ci_outcome(failed)
    store_a.store_ci_outcome(fixed)
    first = derive_recovery_pairs(store_a, mirrors, ORG)
    assert first == derive_recovery_pairs(store_a, mirrors, ORG)
    assert first == []

    _, store_b = postgres_store_factory()
    store_b.store_ci_outcome(fixed)
    store_b.store_ci_outcome(failed)
    assert derive_recovery_pairs(store_b, mirrors, ORG) == []


@pytest.mark.parametrize(
    "failed_identity,fixed_identity,expected_pairs,absent",
    [
        (
            {"workflow_id": "same", "workflow_path": "old.yml", "workflow_name": "Old"},
            {
                "workflow_id": "same",
                "workflow_path": "renamed.yml",
                "workflow_name": "New",
            },
            1,
            0,
        ),
        (
            {"workflow_id": "one", "workflow_path": "shared.yml"},
            {"workflow_id": "two", "workflow_path": "shared.yml"},
            0,
            0,
        ),
        (
            {"workflow_id": "same", "provider": CIProvider.GITHUB_ACTIONS},
            {"workflow_id": "same", "provider": CIProvider.JENKINS},
            0,
            0,
        ),
        (
            {"workflow_id": "same", "workflow_path": "shared.yml"},
            {"workflow_path": "shared.yml"},
            0,
            0,
        ),
        ({"workflow_id": "shared.yml"}, {"workflow_path": "shared.yml"}, 0, 0),
        (
            {"workflow_path": "shared.yml", "workflow_name": "Old"},
            {"workflow_path": "shared.yml", "workflow_name": "New"},
            1,
            0,
        ),
        ({"workflow_path": "one.yml"}, {"workflow_path": "two.yml"}, 0, 0),
        ({"workflow_name": "same"}, {"workflow_name": "same"}, 0, 2),
        ({}, {}, 0, 2),
    ],
)
def test_recovery_definition_identity_matrix_is_deterministic(
    tmp_path,
    postgres_store_factory,
    failed_identity,
    fixed_identity,
    expected_pairs,
    absent,
):
    failed_sha, fixed_sha, mirrors = _tied_repo(tmp_path)

    def outcome(sha, result, minutes, identity):
        values = _outcome(sha, result, minutes=minutes, workflow_path=None).model_dump()
        values.update(identity)
        return CIOutcome.model_validate(values)

    failed = outcome(failed_sha, CIResult.FAILED, 0, failed_identity)
    fixed = outcome(fixed_sha, CIResult.PASSED, 1, fixed_identity)
    results = []
    for facts in ((failed, fixed), (fixed, failed)):
        _, store = postgres_store_factory()
        for fact in facts:
            store.store_ci_outcome(fact)
        result = derive_recovery_result(store, mirrors, ORG)
        assert result == derive_recovery_result(store, mirrors, ORG)
        assert len(result.pairs) == expected_pairs
        assert result.skipped == Counter({"workflow_identity_absent": absent})
        if result.pairs:
            assert result.pairs[0].failed_outcome_id == failed.outcome_id
            assert result.pairs[0].fixed_outcome_id == fixed.outcome_id
        results.append(result)
    assert results[0] == results[1]


@pytest.mark.parametrize(
    "definition", [{"workflow_id": "known"}, {"workflow_path": "known.yml"}]
)
def test_recovery_uses_identity_retained_from_earlier_run_attempt(
    tmp_path, postgres_store_factory, definition
):
    failed_sha, fixed_sha, mirrors = _tied_repo(tmp_path)
    values = _outcome(
        failed_sha,
        CIResult.FAILED,
        minutes=0,
        run_id="red",
        run_attempt=1,
        workflow_path=None,
    ).model_dump()
    values.update(definition)
    first = CIOutcome.model_validate(values)
    verdict = _outcome(
        failed_sha,
        CIResult.FAILED,
        minutes=1,
        run_id="red",
        run_attempt=2,
        workflow_path=None,
    )
    values = _outcome(
        fixed_sha, CIResult.PASSED, minutes=2, workflow_path=None
    ).model_dump()
    values.update(definition)
    fixed = CIOutcome.model_validate(values)
    results = []
    for facts in ((first, verdict, fixed), (fixed, verdict, first)):
        _, store = postgres_store_factory()
        for fact in facts:
            store.store_ci_outcome(fact)
        result = derive_recovery_result(store, mirrors, ORG)
        assert len(result.pairs) == 1
        assert result.pairs[0].failed_outcome_id == verdict.outcome_id
        assert result.pairs[0].fixed_outcome_id == fixed.outcome_id
        assert result.skipped == Counter()
        results.append(result)
    assert results[0] == results[1]


@pytest.mark.parametrize("ambiguous", [False, True])
def test_recovery_clean_lineage_checks_precede_missing_definition(
    tmp_path, postgres_store, ambiguous
):
    failed_sha, fixed_sha, mirrors = _tied_repo(tmp_path)
    for result, attempt in ((CIResult.FAILED, 1), (CIResult.PASSED, 2)):
        postgres_store.store_ci_outcome(
            _outcome(
                failed_sha,
                result,
                minutes=attempt,
                workflow_path=None,
                workflow_name=str(attempt) if ambiguous else "same",
                run_id=str(attempt) if ambiguous else "same",
                run_attempt=attempt,
            )
        )
    postgres_store.store_ci_outcome(_outcome(fixed_sha, CIResult.PASSED, minutes=3))
    result = derive_recovery_result(postgres_store, mirrors, ORG)
    assert result.pairs == []
    reason = "ambiguous_workflow_verdicts" if ambiguous else "unreliable_ci_resolution"
    assert result.skipped == Counter({reason: 1})
