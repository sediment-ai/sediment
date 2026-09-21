# SPDX-License-Identifier: AGPL-3.0-or-later
"""
RLVR projection tests — end to end over REAL rollouts (per AGENTS.md: never
mocked; real git for the mirror-backed cases).

Every test builds an actual git repository, stamps a real
``refs/notes/sediment`` note, mirrors it through ``MirrorManager``, writes real
completion/decision/CI facts to a real ``FactStore``, runs ``derive_rollouts``,
and then projects. The task rows' ``base_commit``/``reference_patch`` are checked
against what ``git diff`` actually produces, so a projection bug can't hide
behind a hand-written expectation.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from operator import attrgetter
from pathlib import Path
from typing import get_type_hints

import pytest

from sediment_core import (
    CIOutcome,
    CIProvider,
    CIResult,
    AgentHarness,
    DeveloperDecision,
    InteractionMode,
    ForgeProvider,
    InferenceCall,
    InferenceMessage,
    Push,
    CommitSha,
    NonEmptyId,
    OrgId,
    RepoSlug,
    ToolCallPart,
    TextPart,
)
from sediment_core.store import FactStore
from sediment_derive import (
    AttributionSource,
    CIResolution,
    CommitRef,
    MirrorManager,
    Provenance,
    Rollout,
    RolloutPolicy,
    Turn,
    derive_rollouts,
    split_of,
)
from sediment_derive.diff import parse_unified_diff

import sediment_export as export_api
import sediment_export.rlvr as rlvr_module
from sediment_export import (
    VerifierCommands,
    project_sediment_rollouts,
    project_sediment_tasks,
    write_jsonl,
)
from sediment_export.rlvr import _first_user_message
from export_factories import inference_call, message

ORG = "acme-corp"
REPO = "acme-corp/backend-service"
WORKFLOW_NAME = "CI"
WORKFLOW_PATH = ".github/workflows/ci.yml"

FIB = "def fibonacci(n):\n    return n if n <= 1 else fibonacci(n - 1)\n"
CART = "class ShoppingCart:\n    def add_item(self, item):\n        pass\n"


# Real-git helpers kept inline: a shared file would need a repo-unique module
# basename, and its fixtures would collide with the derive suite's.


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def _work_repo(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "dev@example.com")
    _git(work, "config", "user.name", "Dev")
    return work


def _commit(work: Path, message: str, *, when: str | None = None) -> str:
    _git(work, "add", "-A")
    env = dict(os.environ)
    if when is not None:
        # Pin author+committer date so multi-commit fixtures have unambiguous
        # ancestry order (real commits are seconds apart; only same-second
        # commits tie-break by sha, which is a derive-ordering edge, not this
        # projection's concern).
        env["GIT_AUTHOR_DATE"] = when
        env["GIT_COMMITTER_DATE"] = when
    subprocess.run(
        ["git", "commit", "-q", "-m", message],
        cwd=work,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return _git(work, "rev-parse", "HEAD").strip()


def _make_remote(tmp_path: Path, work: Path) -> Path:
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    _git(work, "push", "-q", str(remote), "refs/heads/*:refs/heads/*")
    _git(work, "push", "-q", str(remote), "refs/notes/*:refs/notes/*")
    return remote


def _note(session_id: str) -> str:
    return json.dumps(
        {
            "v": 1,
            "sessions": [
                {
                    "tool": "claude-code",
                    "session_id": session_id,
                    "stamped_at": "2026-07-15T00:00:00+00:00",
                }
            ],
        }
    )


def _recent() -> datetime:
    # Within the attribution lookback, anchored shortly before the push whose
    # captured_at defaults to now (matches the derive suite's convention).
    return datetime.now(UTC) - timedelta(minutes=5)


def _completion(
    session_id: str,
    messages: list[InferenceMessage],
    completion: str,
    *,
    call_id: str | None = None,
    captured_at: datetime | None = None,
    tool_calls: list[ToolCallPart] | None = None,
) -> InferenceCall:
    return inference_call(
        inference_call_id=None,
        org_id=ORG,
        session_id=session_id,
        model="claude-sonnet-5",
        input_messages=messages,
        output=completion,
        model_call_id=call_id,
        observed_at=captured_at if captured_at is not None else _recent(),
        tool_calls=tool_calls or [],
    )


def _push_and_mirror(
    tmp_path: Path, store: FactStore, work: Path, before: str, after: str
) -> MirrorManager:
    remote = _make_remote(tmp_path, work)
    push = Push(
        org_id=ORG,
        provider=ForgeProvider.GITHUB,
        repo=REPO,
        clone_url=str(remote),
        ref="refs/heads/main",
        before_sha=before,
        after_sha=after,
    )
    mirrors = MirrorManager(str(tmp_path / "mirrors"))
    mirrors.ensure(push)
    store.store_push(push)
    return mirrors


def _store_ci(
    store: FactStore,
    commit_sha: str,
    result: CIResult,
    *,
    repo: str = REPO,
    run: str,
    run_attempt: int | None = None,
) -> CIOutcome:
    outcome = CIOutcome(
        org_id=ORG,
        provider=CIProvider.GITHUB_ACTIONS,
        run_id=run,
        repo=repo,
        commit_sha=commit_sha,
        branch="main",
        result=result,
        run_attempt=run_attempt,
        workflow_name=WORKFLOW_NAME,
        workflow_path=WORKFLOW_PATH,
        run_url=run,
    )
    store.store_ci_outcome(outcome)
    return outcome


def _scenario(
    postgres_store_factory,
    tmp_path: Path,
    *,
    stamp: bool = True,
    ci: CIResult | None = CIResult.PASSED,
    prompt: str = "write fib",
    session_id: str = "sess-1",
    eval_fraction: float = 0.0,
):
    """A stamped, mirrored, reward-labeled single-commit rollout. Returns the
    rollouts plus the mirror + fixture shas for gold-patch ground truth."""
    work = _work_repo(tmp_path)
    (work / "math_utils.py").write_text("")  # scaffold gives the head a parent
    scaffold = _commit(work, "scaffold")
    (work / "math_utils.py").write_text(FIB)
    head = _commit(work, "add fibonacci")
    if stamp:
        _git(work, "notes", "--ref=sediment", "add", "-m", _note(session_id), head)

    _, store = postgres_store_factory()
    mirrors = _push_and_mirror(tmp_path, store, work, "0" * 40, head)
    store.store_inference_call(
        _completion(session_id, [message(role="user", content=prompt)], FIB)
    )
    if ci is not None:
        _store_ci(store, head, ci, run="run/1")

    policy = RolloutPolicy(eval_fraction=eval_fraction)
    rollouts = derive_rollouts(store, mirrors, ORG, policy)
    return rollouts, mirrors, work, scaffold, head, store


def test_task_row_shape_and_reference_patch_match_real_git(
    tmp_path: Path, postgres_store_factory
) -> None:
    rollouts, mirrors, work, scaffold, head, _ = _scenario(
        postgres_store_factory, tmp_path
    )
    result = project_sediment_tasks(rollouts, mirrors, VerifierCommands.empty())

    assert len(result.rows) == 1
    body = result.rows[0].body
    assert body["instance_id"] == f"{ORG}-sess-1-{head[:7]}"
    assert body["repo"] == REPO
    assert body["base_commit"] == scaffold  # parent of the first attributed commit
    assert body["problem_statement"] == "write fib"

    # reference_patch is the real base..head diff.
    expected = _git(work, "diff", "-M", scaffold, head)
    key = attrgetter("file_path")
    assert sorted(parse_unified_diff(body["reference_patch"]), key=key) == sorted(
        parse_unified_diff(expected), key=key
    )
    assert "def fibonacci" in body["reference_patch"]

    assert body["verifier_results"] == [
        rollouts[0].terminal_outcomes[0].model_dump(mode="json")
    ]
    assert body["ci_resolution"]["verdict"] == "passed"
    assert body["ci_resolution"]["reliability"] == 1.0
    assert body["ci_resolution"]["suspected_flake"] is False
    assert "verification" not in body
    assert body["attribution_source"] == "git_notes"
    assert body["split"] == "train"
    assert body["provenance"]["policy_version"] == "4"

    assert result.rows[0].body["session_commit_observation_ids"] == ()


def test_failed_task_verifier_result_has_no_failure_bucket_field(
    tmp_path: Path,
    postgres_store_factory,
) -> None:
    # failure_bucket was dropped: it was always just workflow_name again on a
    # FAILED row, derivable from the workflow_name + recorded_result fields
    # already in the same reward block — pinning its absence here, on both a
    # FAILED and a PASSED row.
    failed_path = tmp_path / "failed"
    failed_path.mkdir()
    failed_rollouts, failed_mirrors, *_ = _scenario(
        postgres_store_factory, failed_path, ci=CIResult.FAILED
    )
    failed_result = (
        project_sediment_tasks(
            failed_rollouts, failed_mirrors, VerifierCommands.empty()
        )
        .rows[0]
        .body["verifier_results"][0]
    )

    assert failed_result["result"] == "failed"
    assert failed_result["workflow_name"] == WORKFLOW_NAME
    assert "failure_bucket" not in failed_result

    passed_path = tmp_path / "passed"
    passed_path.mkdir()
    passed_rollouts, passed_mirrors, *_ = _scenario(postgres_store_factory, passed_path)
    passed_result = (
        project_sediment_tasks(
            passed_rollouts, passed_mirrors, VerifierCommands.empty()
        )
        .rows[0]
        .body["verifier_results"][0]
    )

    assert passed_result["result"] == "passed"
    assert "failure_bucket" not in passed_result


def test_numbered_retry_resolves_semantically_and_exports_zero_reliability(
    tmp_path: Path,
    postgres_store_factory,
) -> None:
    rollouts, mirrors, *_ = _scenario(postgres_store_factory, tmp_path, ci=None)
    original = rollouts[0].terminal_outcomes
    assert original == []
    commit_sha = rollouts[0].commits[-1].commit_sha
    failed = CIOutcome(
        org_id=ORG,
        provider=CIProvider.GITHUB_ACTIONS,
        run_id="run/retry",
        run_attempt=1,
        repo=REPO,
        commit_sha=commit_sha,
        branch="main",
        result=CIResult.FAILED,
        workflow_name=WORKFLOW_NAME,
        workflow_path=WORKFLOW_PATH,
    )
    passed = failed.model_copy(
        update={
            "outcome_id": "00000000-0000-4000-8000-000000000001",
            "run_attempt": 2,
            "result": CIResult.PASSED,
        }
    )
    failed = failed.model_copy(
        update={"outcome_id": "00000000-0000-4000-8000-000000000002"}
    )
    rollout = replace(rollouts[0], terminal_outcomes=[passed, failed])

    result = project_sediment_tasks([rollout], mirrors, VerifierCommands.empty())

    resolution = result.rows[0].body["ci_resolution"]
    assert resolution["verdict"] == "passed"
    assert resolution["reliability"] == 0.0
    assert resolution["suspected_flake"] is True
    assert resolution["source_outcome_ids"] == tuple(
        sorted([failed.outcome_id, passed.outcome_id])
    )
    assert {item["result"] for item in result.rows[0].body["verifier_results"]} == {
        "failed",
        "passed",
    }

    shuffled = project_sediment_tasks(
        [replace(rollout, terminal_outcomes=[failed, passed])],
        mirrors,
        VerifierCommands.empty(),
    )
    assert result == shuffled


def test_later_non_verdict_commit_does_not_hide_last_resolved_verdict(
    tmp_path: Path,
    postgres_store_factory,
) -> None:
    rollouts, mirrors, *_ = _scenario(postgres_store_factory, tmp_path)
    rollout = rollouts[0]
    later_sha = "b" * 40
    cancelled = rollout.terminal_outcomes[0].model_copy(
        update={
            "outcome_id": "00000000-0000-4000-8000-000000000099",
            "run_id": "run/cancelled",
            "commit_sha": later_sha,
            "result": CIResult.CANCELLED,
        }
    )
    rollout = replace(
        rollout,
        commits=[*rollout.commits, CommitRef(repo=REPO, commit_sha=later_sha)],
        terminal_outcomes=[*rollout.terminal_outcomes, cancelled],
    )

    result = project_sediment_tasks([rollout], mirrors, VerifierCommands.empty())

    assert len(result.rows) == 1
    assert result.rows[0].body["ci_resolution"]["verdict"] == "passed"
    assert result.rows[0].body["ci_resolution"]["source_outcome_ids"] == (
        rollouts[0].terminal_outcomes[0].outcome_id,
    )


def test_reference_patch_spans_multiple_attributed_commits(
    tmp_path: Path, postgres_store_factory
) -> None:
    work = _work_repo(tmp_path)
    (work / "math_utils.py").write_text("")
    scaffold = _commit(work, "scaffold", when="2026-07-15T12:00:00")
    (work / "math_utils.py").write_text(FIB)
    c1 = _commit(work, "add fibonacci", when="2026-07-15T12:01:00")
    (work / "cart.py").write_text(CART)
    c2 = _commit(work, "add cart", when="2026-07-15T12:02:00")
    for sha in (c1, c2):
        _git(work, "notes", "--ref=sediment", "add", "-m", _note("sess-1"), sha)

    _, store = postgres_store_factory()
    mirrors = _push_and_mirror(tmp_path, store, work, "0" * 40, c2)
    store.store_inference_call(
        _completion("sess-1", [message(role="user", content="build")], FIB)
    )
    _store_ci(store, c2, CIResult.PASSED, run="run/1")

    rollouts = derive_rollouts(store, mirrors, ORG)
    body = (
        project_sediment_tasks(rollouts, mirrors, VerifierCommands.empty()).rows[0].body
    )

    assert body["instance_id"] == f"{ORG}-sess-1-{c1[:7]}"  # FIRST attributed commit
    assert body["base_commit"] == scaffold
    paths = {fd.file_path for fd in parse_unified_diff(body["reference_patch"])}
    assert paths == {"math_utils.py", "cart.py"}  # spans both commits


def test_task_patch_stops_at_the_commit_with_verifier_evidence(
    tmp_path: Path, postgres_store_factory
) -> None:
    work = _work_repo(tmp_path)
    (work / "math_utils.py").write_text("")
    scaffold = _commit(work, "scaffold", when="2026-07-15T12:00:00")
    (work / "math_utils.py").write_text(FIB)
    passed = _commit(work, "add fibonacci", when="2026-07-15T12:01:00")
    (work / "later.py").write_text("unverified = True\n")
    unverified = _commit(work, "add unverified work", when="2026-07-15T12:02:00")
    for sha in (passed, unverified):
        _git(work, "notes", "--ref=sediment", "add", "-m", _note("sess-1"), sha)

    _, store = postgres_store_factory()
    mirrors = _push_and_mirror(tmp_path, store, work, "0" * 40, unverified)
    store.store_inference_call(
        _completion("sess-1", [message(role="user", content="build")], FIB)
    )
    _store_ci(store, passed, CIResult.PASSED, run="run/1")
    rollouts = derive_rollouts(store, mirrors, ORG)

    sediment = (
        project_sediment_tasks(rollouts, mirrors, VerifierCommands.empty()).rows[0].body
    )
    swe_bench = (
        export_api.project_swe_bench_tasks(rollouts, mirrors, VerifierCommands.empty())
        .rows[0]
        .body
    )

    expected = _git(work, "diff", "-M", scaffold, passed)
    assert sediment["reference_patch"] == expected
    assert sediment["verifier_results"][0]["commit_sha"] == passed
    assert sediment["ci_resolution"]["commit_sha"] == passed
    assert swe_bench["patch"] == expected
    assert swe_bench["metadata"]["verifier_results"][0]["commit_sha"] == passed
    assert swe_bench["metadata"]["ci_resolution"]["commit_sha"] == passed
    assert "later.py" not in sediment["reference_patch"]
    assert "later.py" not in swe_bench["patch"]


def test_verification_command_present_only_for_configured_repo(
    tmp_path: Path, postgres_store_factory
) -> None:
    rollouts, mirrors, *_ = _scenario(postgres_store_factory, tmp_path)

    configured = VerifierCommands.load(_write_toml(tmp_path, REPO, "pytest -q"))
    row = project_sediment_tasks(rollouts, mirrors, configured).rows[0].body
    assert row["verification"]["verification_command"] == "pytest -q"

    # A config for a DIFFERENT repo leaves this row degraded (field absent).
    other = VerifierCommands.load(_write_toml(tmp_path, "acme-corp/other", "make test"))
    row2 = project_sediment_tasks(rollouts, mirrors, other).rows[0].body
    assert "verification" not in row2


def test_no_ci_rollout_excluded_from_tasks_but_present_in_rollouts(
    tmp_path: Path,
    postgres_store_factory,
) -> None:
    rollouts, mirrors, *_ = _scenario(postgres_store_factory, tmp_path, ci=None)

    tasks = project_sediment_tasks(rollouts, mirrors, VerifierCommands.empty())
    assert tasks.rows == []
    assert tasks.skipped == {"missing_verifier_evidence": 1}

    rollout_rows = project_sediment_rollouts(rollouts).rows
    assert len(rollout_rows) == 1
    assert rollout_rows[0].body["verifier_results"] == []  # SFT-able, not labeled


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
def test_non_verdict_ci_is_not_a_task(
    tmp_path: Path, postgres_store_factory, non_verdict: CIResult
) -> None:
    rollouts, mirrors, *_ = _scenario(postgres_store_factory, tmp_path, ci=non_verdict)
    tasks = project_sediment_tasks(rollouts, mirrors, VerifierCommands.empty())
    assert tasks.rows == []
    assert tasks.skipped == {"missing_verifier_evidence": 1}


def test_rollout_projection_preserves_exact_ci_fact(
    tmp_path: Path, postgres_store_factory
) -> None:
    rollouts, _, *_ = _scenario(postgres_store_factory, tmp_path, ci=CIResult.ERROR)
    outcome = rollouts[0].terminal_outcomes[0]
    rich = outcome.model_copy(
        update={
            "run_attempt": 2,
            "workflow_id": "workflow-9",
            "provider_result": "startup_failure",
            "error_type": "runner_lost",
            "reason": "runner stopped responding",
            "source_event_type": "dev.cdevents.pipelinerun.finished.0.2.0",
            "source_spec_version": "0.5.0",
            "source_event_id": "event-9",
        }
    )
    rollouts[0] = replace(rollouts[0], terminal_outcomes=[rich])

    [row] = project_sediment_rollouts(rollouts).rows

    assert row.body["verifier_results"] == [rich.model_dump(mode="json")]
    assert row.body["ci_resolutions"][0]["verdict"] is None


def test_rollout_with_no_attributed_commit_is_skipped(
    tmp_path: Path, postgres_store_factory
) -> None:
    # A session with a completion but no push/notes/attribution binding.
    _, store = postgres_store_factory()
    store.store_inference_call(
        _completion("sess-1", [message(role="user", content="hi")], "unrelated output")
    )
    mirrors = MirrorManager(str(tmp_path / "mirrors"))
    rollouts = derive_rollouts(store, mirrors, ORG)

    tasks = project_sediment_tasks(rollouts, mirrors, VerifierCommands.empty())
    assert tasks.rows == []
    assert tasks.skipped == {"no_attributed_commit": 1}
    # But the rollout is still emitted as a trajectory row.
    assert len(project_sediment_rollouts(rollouts).rows) == 1


def test_jaccard_attribution_projects_with_provenance(
    tmp_path: Path, postgres_store_factory
) -> None:
    # An unstamped repo binds via jaccard (the completion text matches the
    # diff); the row carries the attribution source in its provenance so
    # consumers can filter downstream.
    rollouts, mirrors, *_ = _scenario(postgres_store_factory, tmp_path, stamp=False)
    assert rollouts[0].attribution_source == "jaccard"

    tasks = project_sediment_tasks(rollouts, mirrors, VerifierCommands.empty())
    assert len(tasks.rows) == 1
    assert tasks.rows[0].body["attribution_source"] == "jaccard"


def test_task_projection_is_deterministic_across_runs(
    tmp_path: Path, postgres_store_factory
) -> None:
    rollouts, mirrors, *_, store = _scenario(postgres_store_factory, tmp_path)
    first = [
        r.body
        for r in project_sediment_tasks(
            rollouts, mirrors, VerifierCommands.empty()
        ).rows
    ]
    second = [
        r.body
        for r in project_sediment_tasks(
            rollouts, mirrors, VerifierCommands.empty()
        ).rows
    ]
    assert first == second
    # Re-deriving the rollouts and re-projecting reproduces byte-identical rows.
    again = derive_rollouts(store, mirrors, ORG)
    third = [
        r.body
        for r in project_sediment_tasks(again, mirrors, VerifierCommands.empty()).rows
    ]
    assert first == third


def test_rollout_row_shape_turns_decisions_and_verifier_results(
    tmp_path: Path,
    postgres_store_factory,
) -> None:
    work = _work_repo(tmp_path)
    (work / "math_utils.py").write_text("")
    _commit(work, "scaffold")
    (work / "math_utils.py").write_text(FIB)
    head = _commit(work, "add fibonacci")
    _git(work, "notes", "--ref=sediment", "add", "-m", _note("sess-1"), head)

    _, store = postgres_store_factory()
    mirrors = _push_and_mirror(tmp_path, store, work, "0" * 40, head)
    m1 = [message(role="user", content="write fib")]
    m2 = m1 + [
        message(role="assistant", content=FIB),
        message(role="user", content="cart"),
    ]
    store.store_inference_call(
        _completion("sess-1", m1, FIB, call_id="r1", captured_at=_recent())
    )
    store.store_inference_call(
        _completion(
            "sess-1",
            m2,
            CART,
            call_id="r2",
            captured_at=_recent(),
            tool_calls=[
                ToolCallPart(
                    id="toolu_9", name="write_file", arguments={"path": "cart.py"}
                )
            ],
        )
    )
    store.store_decision(
        DeveloperDecision(
            org_id=ORG,
            session_id="sess-1",
            user_id="dev",
            agent_harness=AgentHarness.CLAUDE_CODE,
            file_path="math_utils.py",
            accepted=True,
            explicit=True,
            interaction_mode=InteractionMode.AGENT,
            call_id="r2",
            occurred_at=_recent(),
        )
    )
    _store_ci(store, head, CIResult.PASSED, run="run/1")

    rollouts = derive_rollouts(store, mirrors, ORG)
    rows = project_sediment_rollouts(rollouts).rows
    assert len(rows) == 1
    body = rows[0].body
    assert body["session_id"] == "sess-1"
    assert body["segment_index"] == 0

    turns = body["turns"]
    assert len(turns) == 2
    assert turns[0]["new_messages"] == [
        {"role": "user", "parts": [{"type": "text", "content": "write fib"}]}
    ]
    assert turns[0]["completion"] == FIB
    # The structured tool calls ride each turn — [] when the capture saw
    # none, the canonical structured parts when it did.
    assert turns[0]["tool_calls"] == []
    assert turns[1]["tool_calls"] == [
        {
            "type": "tool_call",
            "id": "toolu_9",
            "name": "write_file",
            "arguments": {"path": "cart.py"},
        }
    ]
    assert turns[0]["decisions"] == []
    # The decision joined its turn by call_id.
    assert turns[1]["decisions"] == [
        {
            "accepted": True,
            "explicit": True,
            "agent_harness": "claude-code",
            "interaction_mode": "agent",
            "file_path": "math_utils.py",
        }
    ]
    assert [o["result"] for o in body["verifier_results"]] == ["passed"]
    assert body["verifier_results"][0]["workflow_path"] == WORKFLOW_PATH
    assert body["ci_resolutions"][0]["verdict"] == "passed"
    assert body["ci_resolutions"][0]["reliability"] == 1.0
    assert body["ci_resolution"]["verdict"] == "passed"
    assert body["ci_resolution"]["reliability"] == 1.0
    assert "reward_channels" not in body
    assert body["attribution_source"] == "git_notes"


def test_compaction_yields_one_rollout_row_per_segment(
    tmp_path: Path, postgres_store_factory
) -> None:
    _, store = postgres_store_factory()
    m1 = [message(role="user", content="write fib")]
    m2 = m1 + [
        message(role="assistant", content=FIB),
        message(role="user", content="cart"),
    ]
    # Compacted: m3 is not prefixed by m2, so the derivation opens a 2nd segment.
    m3 = [
        message(role="system", content="[summary]"),
        message(role="user", content="beta"),
    ]
    base = _recent()
    store.store_inference_call(_completion("s1", m1, FIB, captured_at=base))
    store.store_inference_call(
        _completion("s1", m2, CART, captured_at=base + timedelta(minutes=1))
    )
    store.store_inference_call(
        _completion("s1", m3, "beta", captured_at=base + timedelta(minutes=2))
    )
    mirrors = MirrorManager(str(tmp_path / "mirrors"))

    rows = project_sediment_rollouts(derive_rollouts(store, mirrors, ORG)).rows
    assert [r.body["segment_index"] for r in rows] == [0, 1]
    assert [len(r.body["turns"]) for r in rows] == [2, 1]


def test_split_partition_is_total_and_file_naming_matches_both_modes(
    tmp_path: Path,
    postgres_store_factory,
) -> None:
    # A rollout's row inherits its split; the writer routes it to the matching
    # file and never creates the empty other side. (The total/no-overlap
    # invariant across many rows is covered at the writer level in test_jsonl.)
    frac = 0.5
    rollouts, mirrors, *_ = _scenario(
        postgres_store_factory, tmp_path, session_id="sess-1", eval_fraction=frac
    )
    tasks = project_sediment_tasks(rollouts, mirrors, VerifierCommands.empty())
    assert len(tasks.rows) == 1
    row = tasks.rows[0]
    # The row inherits the rollout's split, computed by the SAME primitive.
    assert row.split == split_of("sess-1", frac)
    assert row.body["split"] == row.split

    # Split-enabled write routes the row to the file matching its split, and the
    # other file is not created (empty partition, no-truncate guard).
    out = tmp_path / "out" / "tasks.jsonl"
    write_jsonl(tasks.rows, out, split_enabled=True)
    side = row.split
    other = "eval" if side == "train" else "train"
    assert (tmp_path / "out" / f"tasks.{side}.jsonl").exists()
    assert not (tmp_path / "out" / f"tasks.{other}.jsonl").exists()
    assert not out.exists()  # unsplit path never written in split mode


def test_split_disabled_writes_single_file(
    tmp_path: Path, postgres_store_factory
) -> None:
    rollouts, mirrors, *_ = _scenario(
        postgres_store_factory, tmp_path, eval_fraction=0.0
    )
    tasks = project_sediment_tasks(rollouts, mirrors, VerifierCommands.empty())
    out = tmp_path / "out" / "tasks.jsonl"
    write_jsonl(tasks.rows, out, split_enabled=False)
    assert out.exists()
    assert not (tmp_path / "out" / "tasks.train.jsonl").exists()


def test_jsonl_round_trips_against_documented_task_schema(
    tmp_path: Path, postgres_store_factory
) -> None:
    rollouts, mirrors, *_ = _scenario(postgres_store_factory, tmp_path)
    tasks = project_sediment_tasks(rollouts, mirrors, VerifierCommands.empty())
    out = tmp_path / "tasks.jsonl"
    write_jsonl(tasks.rows, out, split_enabled=False)

    loaded = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(loaded) == 1
    row = loaded[0]
    assert set(row) == {
        "instance_id",
        "recipe_id",
        "recipe_version",
        "reward_source",
        "repo",
        "base_commit",
        "problem_statement",
        "reference_patch",
        "verifier_results",
        "ci_resolution",
        "attribution_source",
        "session_commit_observation_ids",
        "repository_identity",
        "split",
        "provenance",
        "schema_id",
        "schema_version",
    }
    assert set(row["provenance"]) == {
        "policy_version",
        "quarantine_revision",
        "policy_digest",
    }
    assert row["verifier_results"][0]["result"] == "passed"
    assert row["ci_resolution"]["verdict"] == "passed"
    # Round-trip is lossless: reloaded body equals the projected body.
    assert row == json.loads(json.dumps(tasks.rows[0].body))


def test_empty_projection_writes_nothing_and_leaves_prior_file(
    tmp_path: Path, postgres_store_factory
) -> None:
    # An org whose only rollout is skipped ⇒ empty task projection ⇒ no write,
    # a prior good dataset untouched.
    rollouts, mirrors, *_ = _scenario(
        postgres_store_factory, tmp_path, ci=None
    )  # skipped: no pass/fail
    tasks = project_sediment_tasks(rollouts, mirrors, VerifierCommands.empty())
    assert tasks.rows == []

    out = tmp_path / "tasks.jsonl"
    prior = '{"instance_id": "kept"}\n'
    out.write_text(prior)
    write_jsonl(tasks.rows, out, split_enabled=False)
    assert out.read_text() == prior


def _blocks(*blocks: dict) -> str:
    """A first user message's content in its content-block wire shape: a
    JSON-encoded list, exactly as it lands in a ``TextPart.content`` field."""
    return json.dumps(list(blocks))


def _rollout_with_first_user(content: str) -> Rollout:
    """A minimal rollout whose first user message carries ``content`` — enough
    to exercise ``_first_user_message`` without a mirror or facts."""
    turn = Turn(
        new_messages=[message(role="user", content=content)],
        completion="ok",
        decisions=(),
        inference_call_id="c1",
    )
    return Rollout(
        org_id=ORG,
        session_id="sess-1",
        segments=[[turn]],
        commits=[],
        attribution_source=AttributionSource.GIT_NOTES,
        terminal_outcomes=[],
        provenance=Provenance(policy_version="1", quarantine_revision=0),
        split="train",
    )


@pytest.mark.parametrize(
    "content",
    [
        _blocks({"type": "text", "text": "literal"}),
        _blocks({"type": "text"}),
        "plain text\nwith whitespace ",
        '[{"type": "text", "text": "truncated',
        "[1, 2]",
        '["do X", "do Y"]',
        "[]",
        "true",
        "42",
    ],
)
def test_first_user_message_retains_canonical_text(content):
    assert _first_user_message(_rollout_with_first_user(content)) == content


def test_structured_message_and_tool_call_project_without_legacy_flattening() -> None:
    tool_call = ToolCallPart(
        id="tool-1", name="Write", arguments={"path": "app.py", "content": "x=1"}
    )
    turn = Turn(
        new_messages=[
            InferenceMessage(
                role="user", parts=[TextPart(content="Implement the feature.")]
            )
        ],
        completion="x=1",
        decisions=(),
        inference_call_id="inference-1",
        tool_calls=(tool_call,),
    )
    rollout = Rollout(
        org_id=ORG,
        session_id="sess-1",
        segments=[[turn]],
        commits=[],
        attribution_source=AttributionSource.GIT_NOTES,
        terminal_outcomes=[],
        provenance=Provenance(policy_version="1", quarantine_revision=0),
        split="train",
    )

    assert _first_user_message(rollout) == "Implement the feature."
    [row] = project_sediment_rollouts([rollout]).rows
    [emitted] = row.body["turns"]
    assert emitted["new_messages"] == [
        {
            "role": "user",
            "parts": [{"type": "text", "content": "Implement the feature."}],
        }
    ]
    assert emitted["tool_calls"] == [
        {
            "type": "tool_call",
            "id": "tool-1",
            "name": "Write",
            "arguments": {"path": "app.py", "content": "x=1"},
        }
    ]


def test_first_user_message_absent_user_keeps_skip_behavior() -> None:
    # A system-only opener has no user message → the existing "" (skip) contract.
    turn = Turn(
        new_messages=[message(role="system", content="[summary]")],
        completion="ok",
        decisions=(),
        inference_call_id="c1",
    )
    rollout = Rollout(
        org_id=ORG,
        session_id="sess-1",
        segments=[[turn]],
        commits=[],
        attribution_source=AttributionSource.GIT_NOTES,
        terminal_outcomes=[],
        provenance=Provenance(policy_version="1", quarantine_revision=0),
        split="train",
    )
    assert _first_user_message(rollout) == ""


def test_task_prompt_is_verbatim_end_to_end(
    tmp_path: Path, postgres_store_factory
) -> None:
    # A canonical TextPart can contain a quoted provider-shaped JSON example.
    # Its literal text survives the FactStore, Rollout, and task projection.
    prompt = _blocks(
        {"type": "text", "text": "<session>\nYou are working on math_utils."},
        {"type": "text", "text": "Add a fibonacci function."},
    )
    rollouts, mirrors, *_ = _scenario(postgres_store_factory, tmp_path, prompt=prompt)
    tasks = project_sediment_tasks(rollouts, mirrors, VerifierCommands.empty())
    assert len(tasks.rows) == 1
    emitted = tasks.rows[0].body["problem_statement"]
    assert emitted.encode("utf-8") == prompt.encode("utf-8")


def _write_toml(tmp_path: Path, repo: str, command: str) -> Path:
    path = tmp_path / f"verifiers-{repo.replace('/', '_')}.toml"
    path.write_text(
        f'[repos."{repo}"]\nverification_command = "{command}"\n', encoding="utf-8"
    )
    return path


def test_sediment_target_keeps_audit_evidence_without_empty_reward_channels(
    tmp_path: Path,
    postgres_store_factory,
) -> None:
    rollouts, mirrors, work, scaffold, head, _ = _scenario(
        postgres_store_factory, tmp_path
    )
    commands = export_api.VerifierCommands.empty()

    task = export_api.project_sediment_tasks(rollouts, mirrors, commands).rows[0].body
    rollout = export_api.project_sediment_rollouts(rollouts).rows[0].body

    assert task["problem_statement"] == "write fib"
    assert task["reference_patch"] == _git(work, "diff", "-M", scaffold, head)
    assert task["verifier_results"][0]["result"] == "passed"
    assert task["verifier_results"][0]["outcome_id"]
    assert task["verifier_results"] == [
        rollouts[0].terminal_outcomes[-1].model_dump(mode="json")
    ]
    assert task["ci_resolution"]["verdict"] == "passed"
    assert "verification" not in task
    assert "prompt" not in task
    assert "gold_patch" not in task
    assert "reward" not in task

    assert [result["result"] for result in rollout["verifier_results"]] == ["passed"]
    assert "terminal_outcomes" not in rollout
    assert "reward_channels" not in rollout


def test_rlvr_skip_reason_vocabularies_are_closed() -> None:
    assert rlvr_module.SEDIMENT_TASK_SKIP_REASONS == (
        "conflicting_run_identity",
        "ambiguous_workflow_verdicts",
        "repository_identity_absent",
        "repository_identity_conflict",
        "repository_identity_unresolved",
        "repository_mirror_identity_unresolved",
        "repository_source_absent",
        "non_finite_number",
        "unrepresentable_unicode",
        "no_attributed_commit",
        "missing_verifier_evidence",
        "mirror_absent",
        "no_commit_in_verifier_repo",
        "degenerate_commit_range",
        "root_commit_no_base",
        "reference_patch_unavailable",
    )
    assert rlvr_module.SWE_BENCH_SKIP_REASONS == (
        "conflicting_run_identity",
        "ambiguous_workflow_verdicts",
        "repository_identity_absent",
        "repository_identity_conflict",
        "repository_identity_unresolved",
        "repository_mirror_identity_unresolved",
        "repository_source_absent",
        "non_finite_number",
        "unrepresentable_unicode",
        "no_attributed_commit",
        "missing_verifier_evidence",
        "failed_reference_patch",
        "mirror_absent",
        "no_commit_in_verifier_repo",
        "degenerate_commit_range",
        "root_commit_no_base",
        "reference_patch_unavailable",
    )
    assert rlvr_module.ROLLOUT_SKIP_REASONS == (
        "conflicting_run_identity",
        "ambiguous_workflow_verdicts",
        "repository_identity_absent",
        "repository_identity_conflict",
        "repository_identity_unresolved",
        "repository_mirror_identity_unresolved",
        "repository_source_absent",
        "non_finite_number",
        "unrepresentable_unicode",
        "no_segments",
    )


def test_every_emitted_resolver_skip_is_in_each_target_vocabulary(
    tmp_path: Path,
    postgres_store_factory,
) -> None:
    rollouts, mirrors, *_ = _scenario(postgres_store_factory, tmp_path)
    rollout = rollouts[0]
    failed = rollout.terminal_outcomes[0].model_copy(
        update={
            "outcome_id": "00000000-0000-4000-8000-000000000099",
            "run_id": "run/other-workflow",
            "result": CIResult.FAILED,
            "workflow_name": "Lint",
            "workflow_path": ".github/workflows/lint.yml",
        }
    )
    ambiguous = replace(
        rollout,
        terminal_outcomes=[*rollout.terminal_outcomes, failed],
    )
    commands = VerifierCommands.empty()

    projections = (
        (
            project_sediment_tasks([ambiguous], mirrors, commands),
            rlvr_module.SEDIMENT_TASK_SKIP_REASONS,
        ),
        (
            export_api.project_swe_bench_tasks([ambiguous], mirrors, commands),
            rlvr_module.SWE_BENCH_SKIP_REASONS,
        ),
        (
            project_sediment_rollouts([ambiguous]),
            rlvr_module.ROLLOUT_SKIP_REASONS,
        ),
        (
            export_api.project_nemo_gym_rollouts([ambiguous], commands),
            rlvr_module.ROLLOUT_SKIP_REASONS,
        ),
    )

    for projection, vocabulary in projections:
        assert projection.skipped["ambiguous_workflow_verdicts"] == 1
        assert set(projection.skipped) <= set(vocabulary)


def test_swe_bench_emits_only_recorded_passing_reference_patches(
    tmp_path: Path,
    postgres_store_factory,
) -> None:
    passed_path = tmp_path / "passed"
    failed_path = tmp_path / "failed"
    missing_path = tmp_path / "missing"
    for path in (passed_path, failed_path, missing_path):
        path.mkdir()

    passed, mirrors, work, scaffold, head, _ = _scenario(
        postgres_store_factory, passed_path
    )
    failed, failed_mirrors, *_ = _scenario(
        postgres_store_factory, failed_path, ci=CIResult.FAILED
    )
    missing, missing_mirrors, *_ = _scenario(
        postgres_store_factory, missing_path, ci=None
    )

    result = export_api.project_swe_bench_tasks(
        passed, mirrors, export_api.VerifierCommands.empty()
    )
    assert len(result.rows) == 1
    row = result.rows[0].body
    assert set(row) == {
        "instance_id",
        "repo",
        "base_commit",
        "problem_statement",
        "patch",
        "metadata",
    }
    assert row["patch"] == _git(work, "diff", "-M", scaffold, head)
    assert row["metadata"]["verifier_results"][0]["result"] == "passed"
    assert row["metadata"]["ci_resolution"]["verdict"] == "passed"
    assert "test_patch" not in row
    assert "version" not in row
    assert "FAIL_TO_PASS" not in row
    assert "PASS_TO_PASS" not in row

    failed_result = export_api.project_swe_bench_tasks(
        failed, failed_mirrors, export_api.VerifierCommands.empty()
    )
    assert failed_result.rows == []
    assert failed_result.skipped == {"failed_reference_patch": 1}

    missing_result = export_api.project_swe_bench_tasks(
        missing, missing_mirrors, export_api.VerifierCommands.empty()
    )
    assert missing_result.rows == []
    assert missing_result.skipped == {"missing_verifier_evidence": 1}


def test_nemo_gym_maps_recorded_verifier_results_to_numeric_reward(
    tmp_path: Path,
    postgres_store_factory,
) -> None:
    passed_path = tmp_path / "passed"
    failed_path = tmp_path / "failed"
    missing_path = tmp_path / "missing"
    for path in (passed_path, failed_path, missing_path):
        path.mkdir()

    passed, *_ = _scenario(postgres_store_factory, passed_path)
    failed, *_ = _scenario(postgres_store_factory, failed_path, ci=CIResult.FAILED)
    missing, *_ = _scenario(postgres_store_factory, missing_path, ci=None)

    passed_row = (
        export_api.project_nemo_gym_rollouts(
            passed, export_api.VerifierCommands.empty()
        )
        .rows[0]
        .body
    )
    failed_row = (
        export_api.project_nemo_gym_rollouts(
            failed, export_api.VerifierCommands.empty()
        )
        .rows[0]
        .body
    )
    missing_row = (
        export_api.project_nemo_gym_rollouts(
            missing, export_api.VerifierCommands.empty()
        )
        .rows[0]
        .body
    )

    assert passed_row["responses_create_params"]["input"]
    assert passed_row["response"]["turns"]
    assert passed_row["reward"] == 1.0
    assert failed_row["reward"] == 0.0
    assert "reward" not in missing_row
    assert [
        result["result"] for result in passed_row["metadata"]["verifier_results"]
    ] == ["passed"]
    assert "verification" not in passed_row["metadata"]
    assert "resources_server" not in json.dumps(passed_row)
    assert "environment" not in json.dumps(passed_row)


def test_verification_configuration_stays_separate_for_every_target(
    tmp_path: Path,
    postgres_store_factory,
) -> None:
    rollouts, mirrors, *_ = _scenario(postgres_store_factory, tmp_path)
    config = tmp_path / "verifiers.toml"
    config.write_text(
        f'[repos."{REPO}"]\nverification_command = "pytest -q"\n',
        encoding="utf-8",
    )
    commands = export_api.VerifierCommands.load(config)

    sediment = export_api.project_sediment_tasks(rollouts, mirrors, commands).rows[0]
    swe_bench = export_api.project_swe_bench_tasks(rollouts, mirrors, commands).rows[0]
    nemo_gym = export_api.project_nemo_gym_rollouts(rollouts, commands).rows[0]

    expected = {"verification_command": "pytest -q"}
    assert sediment.body["verification"] == expected
    assert sediment.body["verifier_results"][0]["result"] == "passed"
    assert sediment.body["ci_resolution"]["verdict"] == "passed"
    assert swe_bench.body["metadata"]["verification"] == expected
    assert swe_bench.body["metadata"]["verifier_results"][0]["result"] == "passed"
    assert swe_bench.body["metadata"]["ci_resolution"]["verdict"] == "passed"
    assert nemo_gym.body["metadata"]["verification"] == expected
    assert nemo_gym.body["metadata"]["verifier_results"][0]["result"] == "passed"


def test_rlvr_rows_name_the_versioned_ci_recipe_and_closed_reward_source(
    tmp_path: Path,
    postgres_store_factory,
) -> None:
    passed_path = tmp_path / "passed"
    failed_path = tmp_path / "failed"
    missing_path = tmp_path / "missing"
    for path in (passed_path, failed_path, missing_path):
        path.mkdir()

    passed, passed_mirrors, *_ = _scenario(postgres_store_factory, passed_path)
    failed, failed_mirrors, *_ = _scenario(
        postgres_store_factory, failed_path, ci=CIResult.FAILED
    )
    missing, *_ = _scenario(postgres_store_factory, missing_path, ci=None)
    commands = VerifierCommands.empty()

    passed_task = project_sediment_tasks(passed, passed_mirrors, commands).rows[0].body
    failed_task = project_sediment_tasks(failed, failed_mirrors, commands).rows[0].body
    swe_bench = (
        export_api.project_swe_bench_tasks(passed, passed_mirrors, commands)
        .rows[0]
        .body
    )
    passed_rollout = project_sediment_rollouts(passed).rows[0].body
    missing_rollout = project_sediment_rollouts(missing).rows[0].body
    nemo_passed = export_api.project_nemo_gym_rollouts(passed, commands).rows[0].body
    nemo_failed = export_api.project_nemo_gym_rollouts(failed, commands).rows[0].body
    nemo_missing = export_api.project_nemo_gym_rollouts(missing, commands).rows[0].body

    assert passed_task["recipe_id"] == "rlvr_ci"
    assert passed_task["recipe_version"] == 1
    assert passed_task["reward_source"] == "resolved_ci_pass"
    assert failed_task["recipe_id"] == "rlvr_ci"
    assert failed_task["recipe_version"] == 1
    assert failed_task["reward_source"] == "resolved_ci_fail"
    assert swe_bench["metadata"]["recipe_id"] == "rlvr_ci"
    assert swe_bench["metadata"]["recipe_version"] == 1
    assert swe_bench["metadata"]["reward_source"] == "resolved_ci_pass"
    assert passed_rollout["recipe_id"] == "rlvr_ci"
    assert passed_rollout["recipe_version"] == 1
    assert passed_rollout["reward_source"] == "resolved_ci_pass"
    assert passed_rollout["ci_resolution"]["verdict"] == "passed"
    assert missing_rollout["recipe_id"] == "rlvr_ci"
    assert missing_rollout["recipe_version"] == 1
    assert "reward_source" not in missing_rollout
    assert "ci_resolution" not in missing_rollout
    assert nemo_passed["metadata"]["recipe_id"] == "rlvr_ci"
    assert nemo_passed["metadata"]["recipe_version"] == 1
    assert nemo_passed["metadata"]["reward_source"] == "resolved_ci_pass"
    assert nemo_failed["metadata"]["reward_source"] == "resolved_ci_fail"
    assert nemo_missing["metadata"]["recipe_id"] == "rlvr_ci"
    assert nemo_missing["metadata"]["recipe_version"] == 1
    assert "reward_source" not in nemo_missing["metadata"]


def test_rlvr_derived_contracts_use_validated_identity_types() -> None:
    assert hasattr(rlvr_module, "RLVRDecisionRow")
    assert hasattr(rlvr_module, "RLVRInferenceMessageRow")
    assert hasattr(rlvr_module, "RLVRTurnRow")
    assert hasattr(rlvr_module, "NemoGymResponsesCreateParams")
    assert hasattr(rlvr_module, "NemoGymResponse")

    task = get_type_hints(rlvr_module.SedimentTaskRow, include_extras=True)
    rollout = get_type_hints(rlvr_module.SedimentRolloutRow, include_extras=True)
    swe_bench = get_type_hints(rlvr_module.SWEBenchTaskRow, include_extras=True)
    nemo = get_type_hints(rlvr_module.NemoGymMetadata, include_extras=True)
    nemo_rollout = get_type_hints(rlvr_module.NemoGymRolloutRow, include_extras=True)
    turn = get_type_hints(rlvr_module.RLVRTurnRow, include_extras=True)
    commands = get_type_hints(VerifierCommands, include_extras=True)
    for_repo = get_type_hints(VerifierCommands.for_repo, include_extras=True)

    assert task["instance_id"] == NonEmptyId
    assert task["repo"] == RepoSlug
    assert task["base_commit"] == CommitSha
    assert rollout["instance_id"] == NonEmptyId
    assert rollout["org_id"] == OrgId
    assert rollout["session_id"] == NonEmptyId
    assert swe_bench["instance_id"] == NonEmptyId
    assert swe_bench["repo"] == RepoSlug
    assert swe_bench["base_commit"] == CommitSha
    assert nemo["instance_id"] == NonEmptyId
    assert nemo["org_id"] == OrgId
    assert nemo["session_id"] == NonEmptyId
    assert task["verifier_results"] == list[CIOutcome]
    assert task["ci_resolution"] == CIResolution
    assert rollout["turns"] == list[rlvr_module.RLVRTurnRow]
    assert rollout["verifier_results"] == list[CIOutcome]
    assert rollout["ci_resolutions"] == list[CIResolution]
    assert rollout["ci_resolution"] == CIResolution | None
    assert swe_bench["metadata"] == rlvr_module.SWEBenchMetadata
    assert nemo["verifier_results"] == list[CIOutcome]
    assert nemo["ci_resolution"] == CIResolution | None
    assert nemo_rollout["responses_create_params"] == (
        rlvr_module.NemoGymResponsesCreateParams
    )
    assert nemo_rollout["response"] == rlvr_module.NemoGymResponse
    assert turn["inference_call_id"] == NonEmptyId
    assert turn["new_messages"] == list[rlvr_module.RLVRInferenceMessageRow]
    assert commands["_by_repo"] == dict[RepoSlug, str]
    assert for_repo["repo"] == RepoSlug


def test_sediment_rollout_identifies_the_selected_resolution_reliability(
    tmp_path: Path,
    postgres_store_factory,
) -> None:
    rollouts, *_ = _scenario(postgres_store_factory, tmp_path)
    rollout = rollouts[0]
    clean = rollout.terminal_outcomes[0]
    later_sha = "b" * 40
    failed = clean.model_copy(
        update={
            "outcome_id": "00000000-0000-4000-8000-000000000010",
            "run_id": "run/flaky",
            "run_attempt": 1,
            "commit_sha": later_sha,
            "result": CIResult.FAILED,
        }
    )
    passed = failed.model_copy(
        update={
            "outcome_id": "00000000-0000-4000-8000-000000000011",
            "run_attempt": 2,
            "result": CIResult.PASSED,
        }
    )
    commits = [*rollout.commits, CommitRef(repo=REPO, commit_sha=later_sha)]
    with_flake = replace(
        rollout,
        commits=commits,
        terminal_outcomes=[clean, failed, passed],
    )

    selected_flake = project_sediment_rollouts([with_flake]).rows[0].body
    selected_clean = (
        project_sediment_rollouts(
            [replace(with_flake, commits=list(reversed(commits)))]
        )
        .rows[0]
        .body
    )

    assert selected_flake["ci_resolution"]["commit_sha"] == later_sha
    assert selected_flake["ci_resolution"]["reliability"] == 0.0
    assert selected_clean["ci_resolution"]["commit_sha"] == clean.commit_sha
    assert selected_clean["ci_resolution"]["reliability"] == 1.0
    assert selected_flake != selected_clean


def test_every_rlvr_target_is_byte_deterministic_under_shuffled_input(
    tmp_path: Path, postgres_store_factory, monkeypatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    rollouts, mirrors, *_ = _scenario(postgres_store_factory, source)
    second = replace(rollouts[0], session_id="sess-0")
    ordered = [rollouts[0], second]
    shuffled = list(reversed(ordered))
    monkeypatch.delenv("SEDIMENT_VERIFIER_COMMANDS_FILE", raising=False)
    monkeypatch.delenv("SEDIMENT_REWARD_COMMANDS_FILE", raising=False)
    monkeypatch.delenv("SEDIMENT_OPENENV_IMAGE", raising=False)
    monkeypatch.delenv("SEDIMENT_OPENENV_PACKAGE", raising=False)
    monkeypatch.delenv("SEDIMENT_NEMO_GYM_RESOURCES_SERVER", raising=False)
    monkeypatch.delenv("SEDIMENT_NEMO_GYM_CONFIG", raising=False)

    for target in ("sediment", "swe-bench", "nemo-gym"):
        first = tmp_path / f"{target}-first"
        same = tmp_path / f"{target}-same"
        reordered = tmp_path / f"{target}-reordered"
        export_api.export_rlvr_from_rollouts(
            ordered, mirrors, first, target=target, split_enabled=False
        )
        export_api.export_rlvr_from_rollouts(
            ordered, mirrors, same, target=target, split_enabled=False
        )
        export_api.export_rlvr_from_rollouts(
            shuffled, mirrors, reordered, target=target, split_enabled=False
        )

        expected = {path.name: path.read_bytes() for path in sorted(first.iterdir())}
        assert expected == {
            path.name: path.read_bytes() for path in sorted(same.iterdir())
        }
        assert expected == {
            path.name: path.read_bytes() for path in sorted(reordered.iterdir())
        }


def test_export_refuses_to_mix_targets_in_one_output_directory(
    tmp_path: Path, postgres_store_factory, monkeypatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    rollouts, mirrors, *_ = _scenario(postgres_store_factory, source)
    out = tmp_path / "out"
    monkeypatch.delenv("SEDIMENT_VERIFIER_COMMANDS_FILE", raising=False)
    monkeypatch.delenv("SEDIMENT_OPENENV_IMAGE", raising=False)
    monkeypatch.delenv("SEDIMENT_OPENENV_PACKAGE", raising=False)
    monkeypatch.delenv("SEDIMENT_NEMO_GYM_RESOURCES_SERVER", raising=False)
    monkeypatch.delenv("SEDIMENT_NEMO_GYM_CONFIG", raising=False)

    export_api.export_rlvr_from_rollouts(
        rollouts, mirrors, out, target="sediment", split_enabled=False
    )
    before = {path.name: path.read_bytes() for path in sorted(out.iterdir())}

    with pytest.raises(ValueError, match="already belongs to RLVR target sediment"):
        export_api.export_rlvr_from_rollouts(
            rollouts, mirrors, out, target="nemo-gym", split_enabled=False
        )

    assert {path.name: path.read_bytes() for path in sorted(out.iterdir())} == before

    with pytest.raises(ValueError, match="already contains sediment artifacts"):
        export_api.export_rlvr_from_rollouts(
            rollouts, mirrors, out, target="sediment", split_enabled=False
        )

    assert {path.name: path.read_bytes() for path in sorted(out.iterdir())} == before


def test_output_claim_rejects_a_concurrent_generation(
    tmp_path: Path, postgres_store_factory
) -> None:
    out = tmp_path / "out"
    out.mkdir()

    with rlvr_module._claim_output_target(out, "sediment"):
        with pytest.raises(ValueError, match="export already in progress"):
            with rlvr_module._claim_output_target(out, "sediment"):
                pass


def test_export_refuses_unclaimed_rlvr_artifacts(
    tmp_path: Path, postgres_store_factory
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    stale = out / "tasks.jsonl"
    stale.write_text('{"old": true}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="artifacts without a target claim"):
        export_api.export_rlvr_from_rollouts(
            [],
            MirrorManager(str(tmp_path / "mirrors")),
            out,
            target="swe-bench",
            split_enabled=False,
        )

    assert stale.read_text(encoding="utf-8") == '{"old": true}\n'


def test_nemo_gym_export_is_mirror_free_and_matches_mirrored_baseline(
    tmp_path: Path, postgres_store_factory, monkeypatch
) -> None:
    """``export_rlvr_from_rollouts`` projects ``nemo-gym`` without touching a
    mirror: passing ``None`` is accepted and byte-identical to a real mirror."""
    rollouts, mirrors, *_ = _scenario(postgres_store_factory, tmp_path)
    monkeypatch.delenv("SEDIMENT_VERIFIER_COMMANDS_FILE", raising=False)
    monkeypatch.delenv("SEDIMENT_REWARD_COMMANDS_FILE", raising=False)
    monkeypatch.delenv("SEDIMENT_OPENENV_IMAGE", raising=False)
    monkeypatch.delenv("SEDIMENT_OPENENV_PACKAGE", raising=False)
    monkeypatch.delenv("SEDIMENT_NEMO_GYM_RESOURCES_SERVER", raising=False)
    monkeypatch.delenv("SEDIMENT_NEMO_GYM_CONFIG", raising=False)

    with_mirrors = tmp_path / "with-mirrors"
    without_mirrors = tmp_path / "no-mirrors"
    export_api.export_rlvr_from_rollouts(
        rollouts, mirrors, with_mirrors, target="nemo-gym", split_enabled=False
    )
    export_api.export_rlvr_from_rollouts(
        rollouts, None, without_mirrors, target="nemo-gym", split_enabled=False
    )

    with_mirrors_files = {
        path.name for path in with_mirrors.iterdir() if not path.name.startswith(".")
    }
    without_mirrors_files = {
        path.name for path in without_mirrors.iterdir() if not path.name.startswith(".")
    }
    assert with_mirrors_files == without_mirrors_files == {"rollouts.jsonl"}
    assert (with_mirrors / "rollouts.jsonl").read_bytes() == (
        without_mirrors / "rollouts.jsonl"
    ).read_bytes()


@pytest.mark.parametrize("target", ["sediment", "swe-bench"])
def test_export_rlvr_from_rollouts_rejects_missing_mirror_for_patched_targets(
    tmp_path: Path, postgres_store_factory, target
) -> None:
    """Only ``nemo-gym`` is mirror-free; the patched targets that resolve
    reference patches must reject ``mirrors=None`` with a clear contract error
    rather than dereferencing ``None`` deep in the projection."""
    rollouts, *_ = _scenario(postgres_store_factory, tmp_path)
    out = tmp_path / "out"

    with pytest.raises(ValueError, match="only 'nemo-gym' projects without a mirror"):
        export_api.export_rlvr_from_rollouts(
            rollouts, None, out, target=target, split_enabled=False
        )

    assert not out.exists() or not any(out.iterdir())


def test_rollout_and_all_rlvr_targets_preserve_only_emitted_commit_observations(
    tmp_path, postgres_store_factory
):
    from sediment_core import SessionCommitObservation, FactTable

    rollouts, mirrors, _work, scaffold, head, store = _scenario(
        postgres_store_factory, tmp_path
    )
    boundary = datetime.now(UTC)
    observed = SessionCommitObservation(
        observation_id="head-observed",
        org_id=ORG,
        session_id="sess-1",
        repo=REPO,
        commit_sha=head,
        source_push_id="push",
        captured_at=boundary,
    )
    unrelated = observed.model_copy(
        update={"observation_id": "base-observed", "commit_sha": scaffold}
    )
    wrong_session = observed.model_copy(
        update={"observation_id": "wrong-session", "session_id": "other"}
    )
    for fact in (wrong_session, unrelated, observed):
        store.store_session_commit_observation(fact)
    [canonical] = derive_rollouts(store, mirrors, ORG)
    assert canonical.session_commit_observations == (observed,)
    # Metadata references only the emitted resolution, even if a caller supplies
    # additional source Facts. The base commit needs no Session observation.
    enriched = replace(
        canonical, session_commit_observations=(unrelated, wrong_session, observed)
    )
    commands = VerifierCommands.empty()
    bodies = [
        project_sediment_tasks([enriched], mirrors, commands).rows[0].body,
        project_sediment_rollouts([enriched]).rows[0].body,
        export_api.project_swe_bench_tasks([enriched], mirrors, commands)
        .rows[0]
        .body["metadata"],
        export_api.project_nemo_gym_rollouts([enriched], commands)
        .rows[0]
        .body["metadata"],
    ]
    for body in bodies:
        assert body["session_commit_observation_ids"] == ("head-observed",)
        assert body["recipe_version"] == 1
    audit = replace(enriched, terminal_outcomes=[])
    assert (
        export_api.project_nemo_gym_rollouts([audit], commands)
        .rows[0]
        .body["metadata"]["session_commit_observation_ids"]
        == ()
    )
    assert (
        project_sediment_rollouts([audit])
        .rows[0]
        .body["session_commit_observation_ids"]
        == ()
    )
    store.quarantine_fact(
        ORG,
        FactTable.SESSION_COMMIT_OBSERVATIONS,
        observed.observation_id,
        reason="test",
    )
    [quarantined] = derive_rollouts(store, mirrors, ORG)
    assert quarantined.session_commit_observations == ()
    assert project_sediment_tasks([quarantined], mirrors, commands).rows


@pytest.mark.parametrize(
    "target",
    ["sediment_tasks", "swe_bench_tasks", "sediment_rollouts", "nemo_gym_rollouts"],
)
@pytest.mark.parametrize(
    "raw,reason",
    [
        ({"nested": [float("nan")]}, "non_finite_number"),
        ({"\ud800": "key"}, "unrepresentable_unicode"),
    ],
)
def test_every_rlvr_target_declines_nested_metadata_once(
    tmp_path, postgres_store_factory, target, raw, reason
):
    rollouts, mirrors, *_ = _scenario(postgres_store_factory, tmp_path)
    rollout = rollouts[0]
    bad = rollout.terminal_outcomes[0].model_copy(update={"raw": raw})
    rollout = replace(rollout, terminal_outcomes=[bad])
    projector = getattr(rlvr_module, "project_" + target)
    args = (
        ([rollout], mirrors, VerifierCommands.empty())
        if target.endswith("tasks")
        else (
            ([rollout],)
            if target == "sediment_rollouts"
            else ([rollout], VerifierCommands.empty())
        )
    )
    out = projector(*args)
    assert out.rows == []
    assert dict(out.skipped) == {reason: 1}


@pytest.mark.parametrize("target", ["sediment_rollouts", "nemo_gym_rollouts"])
@pytest.mark.parametrize(
    "bad,reason",
    [("\ud800", "unrepresentable_unicode"), (float("inf"), "non_finite_number")],
)
def test_rlvr_representation_is_per_emitted_segment(target, bad, reason):
    from export_factories import tool_call

    rollout = _rollout_with_first_user("é\x00🪨")
    valid = rollout.segments[0][0]
    invalid = replace(
        valid,
        inference_call_id="c2",
        tool_calls=[tool_call("t", "run", {"nested": [bad, bad]})],
    )
    rollout = replace(rollout, segments=[[invalid], [valid]])
    projector = getattr(rlvr_module, "project_" + target)
    args = (
        ([rollout],)
        if target == "sediment_rollouts"
        else ([rollout], VerifierCommands.empty())
    )
    first = projector(*args)
    assert first == projector(*args)
    assert len(first.rows) == 1
    assert dict(first.skipped) == {reason: 1}


@pytest.mark.parametrize("target", ["sediment_tasks", "swe_bench_tasks"])
def test_rlvr_task_omitted_turn_content_does_not_decline(
    tmp_path, postgres_store_factory, target
):
    from export_factories import tool_call

    rollouts, mirrors, *_ = _scenario(postgres_store_factory, tmp_path)
    rollout = rollouts[0]
    turn = replace(
        rollout.segments[0][0],
        completion="\ud800",
        tool_calls=[tool_call("t", "run", {"bad": float("nan")})],
    )
    rollout = replace(rollout, segments=[[turn]])
    out = getattr(rlvr_module, "project_" + target)(
        [rollout], mirrors, VerifierCommands.empty()
    )
    assert len(out.rows) == 1
    assert dict(out.skipped) == {}


@pytest.mark.parametrize("target", ["sediment_tasks", "swe_bench_tasks"])
@pytest.mark.parametrize(
    "prompt",
    [
        '  [{"type":"text","text":"quoted example"}]\n',
        '[{"type":"image","source":{"data":"example"}}]',
        '[{"type":"text","text":null}, {"type":"text"}]',
        "literal é 🪨\n\t with spaces ",
    ],
)
def test_canonical_task_text_is_verbatim_for_each_target(
    tmp_path, postgres_store_factory, target, prompt
):
    rollouts, mirrors, *_ = _scenario(postgres_store_factory, tmp_path, prompt=prompt)
    projector = getattr(rlvr_module, "project_" + target)
    result = projector(rollouts, mirrors, VerifierCommands.empty())
    assert len(result.rows) == 1
    emitted = result.rows[0].body["problem_statement"]
    assert emitted.encode("utf-8") == prompt.encode("utf-8")
    path = tmp_path / "literal.jsonl"
    write_jsonl(result.rows, path, split_enabled=False)
    assert json.loads(path.read_text())["problem_statement"].encode(
        "utf-8"
    ) == prompt.encode("utf-8")
    assert result == projector(rollouts, mirrors, VerifierCommands.empty())


def test_explicit_non_text_task_parts_remain_visibly_marked():
    rollout = _rollout_with_first_user("unused")
    turn = replace(
        rollout.segments[0][0],
        new_messages=[
            InferenceMessage(
                role="user",
                parts=[
                    TextPart(content='[{"type":"text","text":"literal"}]'),
                    ToolCallPart(id="t", name="run", arguments={}),
                    TextPart(content="last"),
                ],
            )
        ],
    )
    assert _first_user_message(replace(rollout, segments=[[turn]])) == (
        '[{"type":"text","text":"literal"}]\n[non-text content omitted]\nlast'
    )


def test_rlvr_prepares_all_artifacts_before_publishing_any(
    tmp_path, postgres_store_factory, monkeypatch
):
    source = tmp_path / "source"
    source.mkdir()
    rollouts, mirrors, *_ = _scenario(postgres_store_factory, source)
    output = tmp_path / "output"
    original = rlvr_module.write_jsonl

    def fail_trajectory_write(rows, path, *, split_enabled, max_bytes=None):
        if Path(path).name == "rollouts.jsonl":
            raise OSError("trajectory staging failed")
        return original(rows, path, split_enabled=split_enabled, max_bytes=max_bytes)

    monkeypatch.setattr(rlvr_module, "write_jsonl", fail_trajectory_write)
    with pytest.raises(OSError, match="trajectory staging failed"):
        export_api.export_rlvr_from_rollouts(
            rollouts, mirrors, output, target="sediment", split_enabled=False
        )
    assert not (output / "tasks.jsonl").exists()
    assert not (output / "rollouts.jsonl").exists()
    assert not (output / "environment.yaml").exists()
    assert {path.name for path in output.iterdir()} <= {".sediment-rlvr-target"}


def test_rlvr_public_export_bounds_lazy_rollout_payloads(tmp_path):
    from collections.abc import Sequence
    import gc
    import tracemalloc
    import weakref

    class Histories(Sequence):
        previous = None

        def __len__(self):
            return 24

        def __getitem__(self, index):
            if not 0 <= index < len(self):
                raise IndexError(index)
            assert self.previous is None or self.previous() is None
            row = replace(
                _rollout_with_first_user("x" * (1024 * 1024)),
                session_id=f"session-{23 - index:02d}",
            )
            self.previous = weakref.ref(row)
            return row

    gc.collect()
    tracemalloc.start()
    try:
        result = export_api.export_rlvr_from_rollouts(
            Histories(),
            None,
            tmp_path / "output",
            target="nemo-gym",
            split_enabled=False,
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result["rollouts"] == result["rollout_rows"] == 24
    assert peak < 16 * 1024 * 1024, f"RLVR export allocated {peak} bytes"
    with (tmp_path / "output" / "rollouts.jsonl").open() as handle:
        identifiers = [json.loads(line)["metadata"]["session_id"] for line in handle]
    assert identifiers == sorted(identifiers)


@pytest.mark.parametrize("target", ["sediment", "swe-bench", "nemo-gym"])
@pytest.mark.parametrize("workflow", ["same", "missing", "divergent"])
def test_rlvr_staged_groups_match_whole_projection_bytes_and_counts(
    tmp_path, postgres_store_factory, monkeypatch, target, workflow
):
    source = tmp_path / "source"
    source.mkdir()
    rollouts, mirrors, *_ = _scenario(postgres_store_factory, source)
    first = replace(rollouts[0], session_id="same", split="eval")
    altered_outcome = first.terminal_outcomes[0].model_copy(
        update={
            "workflow_path": None
            if workflow == "missing"
            else ".github/workflows/other.yml"
            if workflow == "divergent"
            else WORKFLOW_PATH,
        }
    )
    # Equal sort keys remain separate rows and retain their original stable order.
    duplicate_key = replace(
        first,
        terminal_outcomes=[altered_outcome],
        segments=[first.segments[0], first.segments[0]],
        split="train",
    )
    empty = replace(first, session_id="earlier", segments=[])
    unrepresentable = replace(
        first.segments[0][0], new_messages=[message(role="user", content="\ud800")]
    )
    mixed = replace(
        first, session_id="later", segments=[first.segments[0], [unrepresentable]]
    )
    records = [mixed, duplicate_key, empty, first]
    for key in (
        "VERIFIER_COMMANDS_FILE",
        "REWARD_COMMANDS_FILE",
        "OPENENV_IMAGE",
        "OPENENV_PACKAGE",
        "NEMO_GYM_RESOURCES_SERVER",
        "NEMO_GYM_CONFIG",
    ):
        monkeypatch.delenv(f"SEDIMENT_{key}", raising=False)
    expected_tasks = rlvr_module.Projection()
    expected_trajectories = rlvr_module.Projection()
    if target == "sediment":
        expected_tasks = rlvr_module.project_sediment_tasks(
            records, mirrors, VerifierCommands.empty()
        )
        expected_trajectories = rlvr_module.project_sediment_rollouts(records)
    elif target == "swe-bench":
        expected_tasks = rlvr_module.project_swe_bench_tasks(
            records, mirrors, VerifierCommands.empty()
        )
    else:
        expected_trajectories = rlvr_module.project_nemo_gym_rollouts(
            records, VerifierCommands.empty()
        )
    reference = tmp_path / "reference"
    if target != "nemo-gym":
        write_jsonl(expected_tasks.rows, reference / "tasks.jsonl", split_enabled=True)
    if target != "swe-bench":
        write_jsonl(
            expected_trajectories.rows, reference / "rollouts.jsonl", split_enabled=True
        )
    if target == "sediment":
        manifest = rlvr_module.build_manifest(
            expected_tasks.rows,
            rlvr_module.OpenEnvRuntimeSettings(),
            split_enabled=True,
            nemo_gym_settings=rlvr_module.NemoGymRuntimeSettings(),
        )
        rlvr_module.write_manifest(manifest, reference)
        if workflow != "same":
            assert "runtime_reference" not in manifest["environments"][0]
    actual = tmp_path / "actual"
    result = export_api.export_rlvr_from_rollouts(
        records, mirrors, actual, target=target, split_enabled=True
    )
    assert result["rollouts"] == len(records)
    assert result["task_rows"] == len(expected_tasks.rows)
    assert result["rollout_rows"] == len(expected_trajectories.rows)
    assert result["task_skipped"] == dict(expected_tasks.skipped)
    assert result["rollout_skipped"] == dict(expected_trajectories.skipped)
    assert {path.name: path.read_bytes() for path in reference.iterdir()} == {
        path.name: path.read_bytes()
        for path in actual.iterdir()
        if not path.name.startswith(".")
    }


def test_rlvr_record_capacity_failure_precedes_publication(tmp_path, monkeypatch):
    from sediment_export.derived_bundle import BundleCapacityError, BundleLimits
    from sediment_export.staged_rows import ExportRowStore

    def constrained_store(**kwargs):
        return ExportRowStore(limits=BundleLimits(max_record_bytes=128), **kwargs)

    monkeypatch.setattr(rlvr_module, "ExportRowStore", constrained_store, raising=False)
    output = tmp_path / "output"
    with pytest.raises(BundleCapacityError, match="record exceeds"):
        export_api.export_rlvr_from_rollouts(
            [_rollout_with_first_user("x" * 1024)],
            None,
            output,
            target="nemo-gym",
            split_enabled=False,
        )
    assert {path.name for path in output.iterdir()} <= {".sediment-rlvr-target"}


def test_rlvr_prepared_files_share_the_private_staging_budget(tmp_path, monkeypatch):
    from sediment_core import OperationalReportLimitExceeded
    from sediment_export.derived_bundle import BundleLimits
    from sediment_export.staged_rows import ExportRowStore

    rollout = _rollout_with_first_user("captured input")
    rows = rlvr_module.project_nemo_gym_rollouts(
        [rollout], VerifierCommands.empty()
    ).rows
    with ExportRowStore() as measure:
        population = measure.records("rows")
        population.extend(rows)
        source_bytes = population.encoded_bytes
    output_bytes = sum(
        len((json.dumps(row.body, ensure_ascii=True) + "\n").encode("utf-8"))
        for row in rows
    )

    def constrained_store(**kwargs):
        return ExportRowStore(
            limits=BundleLimits(max_staging_bytes=source_bytes + output_bytes - 1),
            **kwargs,
        )

    monkeypatch.setattr(rlvr_module, "ExportRowStore", constrained_store)
    output = tmp_path / "output"
    with pytest.raises(OperationalReportLimitExceeded, match="JSONL output exceeds"):
        export_api.export_rlvr_from_rollouts(
            [rollout], None, output, target="nemo-gym", split_enabled=False
        )
    assert {path.name for path in output.iterdir()} <= {".sediment-rlvr-target"}
