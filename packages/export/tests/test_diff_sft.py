# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Diff-shaped SFT projection tests — real ``AttributedCompletion``/``InferenceCall``/
``DeveloperDecision``/``CIOutcome`` instances, same style as
``test_sft.py``/``test_dpo.py`` (per AGENTS.md: never mocked), PLUS a real
git mirror: unlike the plain SFT projection, ``project_diff_sft`` reads a
commit's added-lines text straight off a real mirror (``mirror.py``), so a
real bare-remote fixture is unavoidable for anything beyond a mirror-absent
skip test.

Git helpers are inline, not imported from ``packages/derive/tests/
gitfixtures.py`` or duplicated in another export test module: mirroring
``test_attributed_completions.py``'s and ``test_rlvr.py``'s own note on why (a shared
fixture file would need a unique module basename to dodge a pytest
collection collision across ``packages/derive/tests`` and
``packages/export/tests``). Unlike ``test_attributed_completions.py``, no ``FactStore`` or
``derive_attributions`` run here: ``AttributedCompletion`` is a plain dataclass (never
mocked, but hand-constructed is the established pattern -- see
``test_sft.py``/``test_dpo.py``), so the git fixture here exists purely to
give ``MirrorManager`` something real to open and diff.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

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
)
from sediment_derive import (
    AttributionSource,
    MirrorManager,
    Provenance,
    SessionAbandonment,
)

from sediment_export import (
    ExportRow,
    LabelConfidencePolicy,
    SFTPolicy,
    diff_sft_to_export_rows,
    project_diff_sft as _project_diff_sft,
    project_sft as _project_sft,
)
from sediment_export.attributed_completions import AttributedCompletion
from export_factories import inference_call, message

ORG = "acme-corp"
REPO = "acme-corp/backend-service"
MODEL = "claude-sonnet-5"

FIB = (
    "def fibonacci(n: int) -> int:\n"
    "    if n <= 1:\n"
    "        return n\n"
    "    return fibonacci(n - 1) + fibonacci(n - 2)\n"
)
CART = (
    "class ShoppingCart:\n"
    "    def add_item(self, item, quantity):\n"
    "        self.items[item] = quantity\n"
    "        return self.items\n"
)


def project_diff_sft(attributed_completions, inference_calls, mirrors, policy=None):
    """Exercise the verified recipe in legacy CI-mechanics tests."""

    return _project_diff_sft(
        attributed_completions,
        inference_calls,
        mirrors,
        policy or SFTPolicy(recipe_id="sft_verified"),
    )


def project_sft(attributed_completions, inference_calls, policy=None):
    return _project_sft(
        attributed_completions,
        inference_calls,
        policy or SFTPolicy(recipe_id="sft_verified"),
    )


# Real-git helpers, kept inline; see the module docstring.


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


def _commit(work: Path, message: str) -> str:
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", message)
    return _git(work, "rev-parse", "HEAD").strip()


def _make_remote(tmp_path: Path, work: Path) -> Path:
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    _git(work, "push", "-q", str(remote), "refs/heads/*:refs/heads/*")
    return remote


def _mirror_manager(tmp_path: Path, work: Path, head: str, *, repo: str = REPO):
    """A real ``MirrorManager`` whose ``(ORG, repo)`` mirror is ready to
    diff ``head`` -- ``ensure`` (create + fetch) once, like a push webhook
    would, then every test reads it via the read-only ``open``."""
    remote = _make_remote(tmp_path, work)
    push = Push(
        org_id=ORG,
        provider=ForgeProvider.GITHUB,
        repo=repo,
        clone_url=str(remote),
        ref="refs/heads/main",
        before_sha="0" * 40,
        after_sha=head,
    )
    mirrors = MirrorManager(str(tmp_path / "mirrors"))
    mirrors.ensure(push)
    return mirrors


def _two_file_commit(tmp_path: Path) -> tuple[Path, str]:
    """One commit adding ``math_utils.py`` (FIB) and ``cart.py`` (CART) in
    the same commit -- the multi-file-in-one-commit shape a diff-shaped
    group is built for."""
    work = _work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    (work / "cart.py").write_text(CART)
    head = _commit(work, "add fibonacci and cart")
    return work, head


_PROMPT = [message("user", "write fib and cart")]


def _completion(
    inference_call_id: str, *, messages: list[InferenceMessage] | None = None
) -> InferenceCall:
    return inference_call(
        inference_call_id=inference_call_id,
        org_id=ORG,
        session_id="sess-1",
        model=MODEL,
        input_messages=list(messages) if messages is not None else list(_PROMPT),
        output="def fibonacci...class ShoppingCart...",
    )


def _decision(*, accepted: bool, explicit: bool = True) -> DeveloperDecision:
    return DeveloperDecision(
        org_id=ORG,
        session_id="sess-1",
        user_id="dev",
        agent_harness=AgentHarness.CLAUDE_CODE,
        file_path="math_utils.py",
        accepted=accepted,
        explicit=explicit,
        interaction_mode=InteractionMode.AGENT,
        occurred_at=datetime.now(UTC),
    )


def _ci(
    result: CIResult,
    *,
    sha: str,
    run: str = "run/1",
    run_attempt: int | None = None,
    workflow_name: str = "CI",
) -> CIOutcome:
    return CIOutcome(
        org_id=ORG,
        provider=CIProvider.GITHUB_ACTIONS,
        run_id=run,
        repo=REPO,
        commit_sha=sha,
        branch="main",
        result=result,
        run_attempt=run_attempt,
        workflow_name=workflow_name,
        run_url=run,
    )


def _attributed_completion(
    *,
    inference_call_id: str,
    commit_sha: str,
    file_path: str,
    decisions: list[DeveloperDecision] = (),
    ci_outcomes: list[CIOutcome] = (),
    similarity_score: float = 1.0,
    attribution_source: AttributionSource = AttributionSource.GIT_NOTES,
    split: str = "train",
) -> AttributedCompletion:
    return AttributedCompletion(
        org_id=ORG,
        session_id="sess-1",
        inference_call_id=inference_call_id,
        repo=REPO,
        commit_sha=commit_sha,
        file_path=file_path,
        similarity_score=similarity_score,
        attribution_source=attribution_source,
        decisions=list(decisions),
        ci_outcomes=list(ci_outcomes),
        provenance=Provenance(policy_version="1", quarantine_revision=0),
        split=split,
    )


def _abandoned_attributed_completion(inference_call_id: str) -> AttributedCompletion:
    now = datetime.now(UTC)
    return AttributedCompletion(
        org_id=ORG,
        session_id="sess-1",
        inference_call_id=inference_call_id,
        repo=None,
        commit_sha=None,
        file_path=None,
        similarity_score=None,
        attribution_source=None,
        decisions=[_decision(accepted=True)],
        ci_outcomes=[],
        provenance=Provenance(policy_version="3", quarantine_revision=0),
        split="train",
        abandonment=SessionAbandonment(
            org_id=ORG,
            session_id="sess-1",
            accepted_decisions=1,
            explicit_accepted_decisions=1,
            last_decision_at=now,
            as_of=now,
            provenance=Provenance(policy_version="2", quarantine_revision=0),
        ),
    )


def test_abandonment_is_never_a_diff_sft_positive_even_with_a_zero_floor(
    tmp_path: Path,
) -> None:
    abandoned = _abandoned_attributed_completion("c-abandoned")

    out = project_diff_sft(
        [abandoned],
        {"c-abandoned": _completion("c-abandoned")},
        MirrorManager(str(tmp_path / "mirrors")),
        SFTPolicy(min_confidence=0.0),
    )

    assert out.rows == []
    assert out.skipped == {"abandoned": 1}


def test_multi_file_group_becomes_one_sample_with_confidence_as_the_min(
    tmp_path: Path,
) -> None:
    _work, head = _two_file_commit(tmp_path)
    mirrors = _mirror_manager(tmp_path, _work, head)

    # Same completion, same commit, two files -- one group, the commit's CI
    # pass shared by both members (CI is commit-granular). Different
    # attribution/similarity_score per file so the members' resolved
    # confidences genuinely differ: math_utils.py is an explicit accept
    # (1.0*1.1 capped to 1.0), cart.py an implicit-accept JACCARD guess
    # discounted by its similarity_score (0.6*1.1*1.1*0.5 = 0.363) -- min()
    # must pick the latter. A lowered floor keeps that deliberately-weak
    # member from being excluded by the confidence floor before min() runs.
    t_math = _attributed_completion(
        inference_call_id="c-1",
        commit_sha=head,
        file_path="math_utils.py",
        decisions=[_decision(accepted=True)],
        ci_outcomes=[_ci(CIResult.PASSED, sha=head)],
    )
    t_cart = _attributed_completion(
        inference_call_id="c-1",
        commit_sha=head,
        file_path="cart.py",
        decisions=[_decision(accepted=True, explicit=False)],
        ci_outcomes=[_ci(CIResult.PASSED, sha=head)],
        attribution_source=AttributionSource.JACCARD,
        similarity_score=0.5,
    )

    from dataclasses import replace
    from sediment_core import SessionCommitObservation

    source = SessionCommitObservation(
        observation_id="selected-source",
        org_id=ORG,
        session_id="sess-1",
        repo=REPO,
        commit_sha=head,
        source_push_id="push",
    )
    unrelated = source.model_copy(
        update={"observation_id": "unrelated", "repo": "other/repo"}
    )
    t_math = replace(t_math, session_commit_observations=(source, unrelated))
    t_cart = replace(t_cart, session_commit_observations=(source,))

    policy = SFTPolicy(recipe_id="sft_verified", min_confidence=0.3)
    out = project_diff_sft(
        [t_math, t_cart], {"c-1": _completion("c-1")}, mirrors, policy
    )

    assert len(out.rows) == 1
    row = out.rows[0]
    assert row.metadata.org_id == ORG
    assert row.metadata.session_id == "sess-1"
    assert row.metadata.repo == REPO
    assert row.metadata.commit_sha == head
    mirror = mirrors.open(ORG, REPO)
    assert mirror is not None
    raw_diff = mirror.fetch_commit_diff(REPO, head)
    assert row.prompt == [{"role": "user", "content": "write fib and cart"}]
    assert row.completion == [{"role": "assistant", "content": raw_diff}]
    assert row.tools == []
    assert raw_diff.index("cart.py") < raw_diff.index("math_utils.py")
    assert "FIB" not in raw_diff  # the genuine patch, not a synthesized target

    from sediment_export.label_confidence import resolve_confidence

    expected = min(
        resolve_confidence(t_math, LabelConfidencePolicy()),
        resolve_confidence(t_cart, LabelConfidencePolicy()),
    )
    assert row.metadata.label_confidence == pytest.approx(expected)
    assert row.metadata.ci_reliability == pytest.approx(1.0)
    assert row.metadata.source_ids.inference_call_id == "c-1"
    assert row.metadata.source_ids.decision_ids == sorted(
        d.decision_id for d in [*t_math.decisions, *t_cart.decisions]
    )

    shuffled = project_diff_sft(
        [t_cart, t_math], {"c-1": _completion("c-1")}, mirrors, policy
    )
    assert out == shuffled

    from sediment_export import write_jsonl

    forward_path = tmp_path / "forward.jsonl"
    shuffled_path = tmp_path / "shuffled.jsonl"
    write_jsonl(diff_sft_to_export_rows(out.rows), forward_path, split_enabled=False)
    write_jsonl(
        diff_sft_to_export_rows(shuffled.rows),
        shuffled_path,
        split_enabled=False,
    )
    assert forward_path.read_bytes() == shuffled_path.read_bytes()

    assert out.rows[0].metadata.attribution_sources == (
        AttributionSource.GIT_NOTES,
        AttributionSource.JACCARD,
    )
    assert out.rows[0].metadata.source_ids.session_commit_observation_ids == (
        "selected-source",
    )


def test_missing_attributed_file_invalidates_the_whole_diff_sample(
    tmp_path: Path,
) -> None:
    work = _work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head = _commit(work, "add fibonacci")
    mirrors = _mirror_manager(tmp_path, work, head)
    ci_outcome = _ci(CIResult.PASSED, sha=head)
    present = _attributed_completion(
        inference_call_id="c-1",
        commit_sha=head,
        file_path="math_utils.py",
        ci_outcomes=[ci_outcome],
    )
    missing = _attributed_completion(
        inference_call_id="c-1",
        commit_sha=head,
        file_path="missing.py",
        ci_outcomes=[ci_outcome],
    )

    out = project_diff_sft([present, missing], {"c-1": _completion("c-1")}, mirrors)

    assert out.rows == []
    assert out.skipped == {"file_diff_unavailable": 1}


def test_explicit_reject_on_one_member_drops_the_whole_multi_file_group(
    tmp_path: Path,
) -> None:
    _work, head = _two_file_commit(tmp_path)
    mirrors = _mirror_manager(tmp_path, _work, head)

    # c-1 touches both files in this commit; one file's decision is an
    # explicit reject. Even though math_utils.py on its own would be a
    # perfectly good SFT row, the group is only as trustworthy as its worst
    # member -- the whole sample must drop.
    t_math = _attributed_completion(
        inference_call_id="c-1",
        commit_sha=head,
        file_path="math_utils.py",
        ci_outcomes=[_ci(CIResult.PASSED, sha=head)],
    )
    t_cart = _attributed_completion(
        inference_call_id="c-1",
        commit_sha=head,
        file_path="cart.py",
        decisions=[_decision(accepted=False)],  # explicit reject
        ci_outcomes=[_ci(CIResult.PASSED, sha=head)],
    )

    # A second, unrelated completion in the same store, to also exercise the
    # multi-completion shape of the wider fixture: its own (single-file)
    # group is unaffected by c-1's reject.
    t_other = _attributed_completion(
        inference_call_id="c-2",
        commit_sha=head,
        file_path="math_utils.py",
        ci_outcomes=[_ci(CIResult.PASSED, sha=head)],
    )

    completions = {"c-1": _completion("c-1"), "c-2": _completion("c-2")}
    out = project_diff_sft([t_math, t_cart, t_other], completions, mirrors)

    assert len(out.rows) == 1
    assert out.rows[0].metadata.source_ids.inference_call_id == "c-2"
    assert out.skipped["explicit_reject"] == 1


def test_ci_failed_on_one_member_drops_the_whole_group(tmp_path: Path) -> None:
    _work, head = _two_file_commit(tmp_path)
    mirrors = _mirror_manager(tmp_path, _work, head)

    t_math = _attributed_completion(
        inference_call_id="c-1",
        commit_sha=head,
        file_path="math_utils.py",
        decisions=[_decision(accepted=True)],
    )
    t_cart = _attributed_completion(
        inference_call_id="c-1",
        commit_sha=head,
        file_path="cart.py",
        ci_outcomes=[_ci(CIResult.FAILED, sha=head)],
    )

    out = _project_diff_sft([t_math, t_cart], {"c-1": _completion("c-1")}, mirrors)
    assert out.rows == []
    assert out.skipped["resolved_ci_failure"] == 1


def test_retried_pass_is_not_verified_diff_sft_eligibility(
    tmp_path: Path,
) -> None:
    work = _work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head = _commit(work, "add fibonacci")
    mirrors = _mirror_manager(tmp_path, work, head)
    attributed_completion = _attributed_completion(
        inference_call_id="c-1",
        commit_sha=head,
        file_path="math_utils.py",
        ci_outcomes=[
            _ci(CIResult.PASSED, sha=head, run_attempt=2),
            _ci(CIResult.FAILED, sha=head, run_attempt=1),
        ],
    )

    out = project_diff_sft(
        [attributed_completion],
        {"c-1": _completion("c-1")},
        mirrors,
        SFTPolicy(recipe_id="sft_verified", min_confidence=0.0),
    )

    assert out.rows == []
    assert out.skipped["unreliable_ci_resolution"] == 1


def test_conflicting_workflows_skip_and_count_ambiguity(tmp_path: Path) -> None:
    work = _work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head = _commit(work, "add fibonacci")
    mirrors = _mirror_manager(tmp_path, work, head)
    attributed_completion = _attributed_completion(
        inference_call_id="c-1",
        commit_sha=head,
        file_path="math_utils.py",
        ci_outcomes=[
            _ci(CIResult.PASSED, sha=head, run="run/ci", workflow_name="CI"),
            _ci(
                CIResult.FAILED,
                sha=head,
                run="run/security",
                workflow_name="Security",
            ),
        ],
    )

    out = project_diff_sft(
        [attributed_completion], {"c-1": _completion("c-1")}, mirrors
    )

    assert out.rows == []
    assert out.skipped["ambiguous_workflow_verdicts"] == 1
    assert out.skipped["resolved_ci_failure"] == 1


def test_single_file_group_confidence_matches_plain_sft_projection(
    tmp_path: Path,
) -> None:
    work = _work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head = _commit(work, "add fibonacci")
    mirrors = _mirror_manager(tmp_path, work, head)

    attributed_completion = _attributed_completion(
        inference_call_id="c-1",
        commit_sha=head,
        file_path="math_utils.py",
        ci_outcomes=[_ci(CIResult.PASSED, sha=head)],
    )
    completions = {"c-1": _completion("c-1")}
    policy = SFTPolicy(recipe_id="sft_verified")

    sft_out = project_sft([attributed_completion], completions, policy)
    diff_out = project_diff_sft([attributed_completion], completions, mirrors, policy)

    assert len(sft_out.rows) == 1
    assert len(diff_out.rows) == 1
    # Same attributed completion, same policy, same resolve_confidence call underneath --
    # the diff-shaped sample's single-member confidence must be exactly the
    # plain projection's row confidence, not merely close.
    assert (
        diff_out.rows[0].metadata.label_confidence
        == sft_out.rows[0].metadata.label_confidence
    )
    patch = diff_out.rows[0].completion[0]["content"]
    assert patch.startswith("diff --git a/math_utils.py b/math_utils.py\n")
    assert "+def fibonacci(n: int) -> int:\n" in patch


def test_implicit_accept_only_group_is_not_a_training_row(tmp_path: Path) -> None:
    # Single-file parity with plain SFT on the POSITIVE side: an
    # implicit-accept-only attributed completion with no CI carries neither a reward nor an
    # explicit decision (CONTEXT.md's training-row rule), and project_sft
    # refuses it — so must the diff-shaped projection. Gating only on
    # "no member's CI failed" would have let this through.
    work = _work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head = _commit(work, "add fibonacci")
    mirrors = _mirror_manager(tmp_path, work, head)

    t = _attributed_completion(
        inference_call_id="c-1",
        commit_sha=head,
        file_path="math_utils.py",
        decisions=[_decision(accepted=True, explicit=False)],
    )
    completions = {"c-1": _completion("c-1")}

    sft_out = project_sft([t], completions)
    diff_out = project_diff_sft([t], completions, mirrors)

    assert sft_out.rows == []
    assert diff_out.rows == []
    assert diff_out.skipped["no_eligibility_source"] == 1


def test_cancelled_only_ci_group_is_not_a_training_row(tmp_path: Path) -> None:
    # A cancelled run is one non-verdict state: non-verdict-only
    # CI plus an implicit accept must not become a diff-shaped training row,
    # exactly as plain SFT refuses it.
    work = _work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head = _commit(work, "add fibonacci")
    mirrors = _mirror_manager(tmp_path, work, head)

    t = _attributed_completion(
        inference_call_id="c-1",
        commit_sha=head,
        file_path="math_utils.py",
        decisions=[_decision(accepted=True, explicit=False)],
        ci_outcomes=[_ci(CIResult.CANCELLED, sha=head)],
    )
    out = project_diff_sft([t], {"c-1": _completion("c-1")}, mirrors)
    assert out.rows == []
    assert out.skipped["no_eligibility_source"] == 1


def test_mirror_absent_skips_the_group(tmp_path: Path) -> None:
    mirrors = MirrorManager(str(tmp_path / "mirrors"))  # never ensured
    t = _attributed_completion(
        inference_call_id="c-1",
        commit_sha="a" * 40,
        file_path="math_utils.py",
        ci_outcomes=[_ci(CIResult.PASSED, sha="a" * 40)],
    )
    out = project_diff_sft([t], {"c-1": _completion("c-1")}, mirrors)
    assert out.rows == []
    assert out.skipped["mirror_absent"] == 1


def test_missing_commit_skips_and_counts_the_group(tmp_path: Path) -> None:
    work = _work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head = _commit(work, "add fibonacci")
    mirrors = _mirror_manager(tmp_path, work, head)
    absent_sha = "b" * 40
    attributed_completion = _attributed_completion(
        inference_call_id="c-1",
        commit_sha=absent_sha,
        file_path="math_utils.py",
        ci_outcomes=[_ci(CIResult.PASSED, sha=absent_sha)],
    )

    out = project_diff_sft(
        [attributed_completion], {"c-1": _completion("c-1")}, mirrors
    )

    assert out.rows == []
    assert out.skipped == {"commit_diff_unavailable": 1}


def test_empty_commit_patch_skips_and_counts_the_group(tmp_path: Path) -> None:
    work = _work_repo(tmp_path)
    (work / "README.md").write_text("seed\n")
    _commit(work, "seed")
    _git(work, "commit", "--allow-empty", "-q", "-m", "empty")
    head = _git(work, "rev-parse", "HEAD").strip()
    mirrors = _mirror_manager(tmp_path, work, head)
    attributed_completion = _attributed_completion(
        inference_call_id="c-1",
        commit_sha=head,
        file_path="math_utils.py",
        ci_outcomes=[_ci(CIResult.PASSED, sha=head)],
    )

    out = project_diff_sft(
        [attributed_completion], {"c-1": _completion("c-1")}, mirrors
    )

    assert out.rows == []
    assert out.skipped == {"empty_patch": 1}


def test_below_confidence_floor_is_excluded(tmp_path: Path) -> None:
    work = _work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head = _commit(work, "add fibonacci")
    mirrors = _mirror_manager(tmp_path, work, head)

    t = _attributed_completion(
        inference_call_id="c-1",
        commit_sha=head,
        file_path="math_utils.py",
        ci_outcomes=[_ci(CIResult.PASSED, sha=head)],
    )
    policy = SFTPolicy(recipe_id="sft_verified", min_confidence=0.99)
    out = project_diff_sft([t], {"c-1": _completion("c-1")}, mirrors, policy)
    assert out.rows == []
    assert out.skipped["below_confidence_floor"] == 1


def test_member_with_no_reward_signal_drops_the_group(tmp_path: Path) -> None:
    _work, head = _two_file_commit(tmp_path)
    mirrors = _mirror_manager(tmp_path, _work, head)

    t_math = _attributed_completion(
        inference_call_id="c-1",
        commit_sha=head,
        file_path="math_utils.py",
        decisions=[_decision(accepted=True)],
    )
    # No decision, no CI outcome at all -- survival evidence only. Such a
    # member fails plain SFT's eligibility bar, so the group drops there
    # (the no_reward_signal branch behind it is the fail-soft guard).
    t_cart = _attributed_completion(
        inference_call_id="c-1", commit_sha=head, file_path="cart.py"
    )

    out = project_diff_sft([t_math, t_cart], {"c-1": _completion("c-1")}, mirrors)
    assert out.rows == []
    assert out.skipped["no_eligibility_source"] == 1


def test_inference_call_not_found_is_skipped(tmp_path: Path) -> None:
    work = _work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head = _commit(work, "add fibonacci")
    mirrors = _mirror_manager(tmp_path, work, head)

    t = _attributed_completion(
        inference_call_id="ghost",
        commit_sha=head,
        file_path="math_utils.py",
        ci_outcomes=[_ci(CIResult.PASSED, sha=head)],
    )
    out = project_diff_sft([t], {}, mirrors)
    assert out.rows == []
    assert out.skipped["inference_call_not_found"] == 1


def test_split_propagates_from_the_attributed_completion(tmp_path: Path) -> None:
    work = _work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head = _commit(work, "add fibonacci")
    mirrors = _mirror_manager(tmp_path, work, head)

    t = _attributed_completion(
        inference_call_id="c-1",
        commit_sha=head,
        file_path="math_utils.py",
        ci_outcomes=[_ci(CIResult.PASSED, sha=head)],
        split="eval",
    )
    out = project_diff_sft([t], {"c-1": _completion("c-1")}, mirrors)
    assert out.rows[0].metadata.split == "eval"


def test_diff_sft_rows_round_trip_through_jsonl(tmp_path: Path) -> None:
    from sediment_export import write_jsonl

    work = _work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head = _commit(work, "add fibonacci")
    mirrors = _mirror_manager(tmp_path, work, head)

    t = _attributed_completion(
        inference_call_id="c-1",
        commit_sha=head,
        file_path="math_utils.py",
        ci_outcomes=[_ci(CIResult.PASSED, sha=head)],
    )
    out = project_diff_sft([t], {"c-1": _completion("c-1")}, mirrors)
    assert len(out.rows) == 1

    export_rows = diff_sft_to_export_rows(out.rows)
    assert all(isinstance(r, ExportRow) for r in export_rows)

    result = write_jsonl(export_rows, tmp_path / "diff_sft.jsonl", split_enabled=False)
    written_path = tmp_path / "diff_sft.jsonl"
    assert str(written_path) in result.written

    import json

    lines = written_path.read_text().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["prompt"] == [{"role": "user", "content": "write fib and cart"}]
    assert row["completion"][0]["role"] == "assistant"
    assert row["completion"][0]["content"].startswith(
        "diff --git a/math_utils.py b/math_utils.py\n"
    )
    assert row["tools"] == []
    assert row["metadata"]["commit_sha"] == head
    assert row["metadata"]["completion_id"] == "c-1"
    assert row["metadata"]["source_ids"]["inference_call_id"] == "c-1"
    assert "files" not in row


def test_diff_sft_policy_is_the_shared_sft_policy_type() -> None:
    with pytest.raises(ValueError):
        SFTPolicy(min_confidence=1.5)


def test_diff_sft_uses_the_selected_sft_recipe_and_preserves_eligibility_source(
    tmp_path: Path,
) -> None:
    work, head = _two_file_commit(tmp_path)
    mirrors = _mirror_manager(tmp_path, work, head)
    attributed_completions = [
        _attributed_completion(
            inference_call_id="c-1",
            commit_sha=head,
            file_path=file_path,
            decisions=[_decision(accepted=True)],
        )
        for file_path in ("math_utils.py", "cart.py")
    ]
    completions = {"c-1": _completion("c-1")}

    [sft_row] = _project_sft(attributed_completions, completions).rows
    first = _project_diff_sft(attributed_completions, completions, mirrors)
    repeated = _project_diff_sft(attributed_completions, completions, mirrors)
    shuffled = _project_diff_sft(
        list(reversed(attributed_completions)), completions, mirrors
    )
    assert first == repeated == shuffled
    [diff_row] = first.rows

    assert diff_row.metadata.recipe_id == "sft_curated"
    assert diff_row.metadata.recipe_version == 1
    assert diff_row.metadata.eligibility_source == "explicit_accept"
    assert diff_row.metadata.eligibility_source == sft_row.metadata.eligibility_source

    from sediment_export import write_jsonl

    paths = [tmp_path / f"curated-{index}.jsonl" for index in range(3)]
    for projection, path in zip((first, repeated, shuffled), paths, strict=True):
        write_jsonl(diff_sft_to_export_rows(projection.rows), path, split_enabled=False)
    assert len({path.read_bytes() for path in paths}) == 1


@pytest.mark.parametrize("bad_prompt", [False, True])
def test_diff_sft_representation_checks_only_emitted_content(tmp_path, bad_prompt):
    work, head = _two_file_commit(tmp_path)
    mirrors = _mirror_manager(tmp_path, work, head)
    call = _completion("c").model_copy(
        update={
            "output_messages": [message("assistant", "\ud800")],
            "raw": {"unused": float("nan")},
            "input_messages": [message("user", "\udfff" if bad_prompt else "valid")],
        }
    )
    attributed = _attributed_completion(
        inference_call_id="c",
        commit_sha=head,
        file_path="math_utils.py",
        decisions=[_decision(accepted=True)],
    )
    out = project_diff_sft([attributed], {"c": call}, mirrors, SFTPolicy())
    assert len(out.rows) == (0 if bad_prompt else 1)
    assert dict(out.skipped) == ({"unrepresentable_unicode": 1} if bad_prompt else {})


@pytest.mark.parametrize("location", ["prompt", "omitted_completion", "metadata"])
def test_diff_sft_nested_representation_and_metadata(tmp_path, location):
    from dataclasses import replace
    from export_factories import tool_call
    from sediment_derive import Provenance

    work, head = _two_file_commit(tmp_path)
    mirrors = _mirror_manager(tmp_path, work, head)
    bad_messages = [
        InferenceMessage(
            role="assistant", parts=[tool_call("t", "run", {"n": [float("inf")]})]
        )
    ]
    call = _completion("c").model_copy(
        update={
            "input_messages"
            if location == "prompt"
            else "output_messages": bad_messages
        }
    )
    attributed = _attributed_completion(
        inference_call_id="c",
        commit_sha=head,
        file_path="math_utils.py",
        decisions=[_decision(accepted=True)],
    )
    if location == "metadata":
        attributed = replace(
            attributed,
            provenance=Provenance(policy_version="v\ud800", quarantine_revision=0),
        )
    out = project_diff_sft([attributed], {"c": call}, mirrors, SFTPolicy())
    if location == "omitted_completion":
        assert len(out.rows) == 1
        assert dict(out.skipped) == {}
    else:
        assert out.rows == []
        assert dict(out.skipped) == {
            "non_finite_number"
            if location == "prompt"
            else "unrepresentable_unicode": 1
        }


def test_diff_sft_uses_exact_shared_sections_and_counts_once_per_commit(tmp_path):
    work = _work_repo(tmp_path)
    paths = ['quoted"file.py', "space file.py", "tab\tfile.py"]
    for path in paths:
        (work / path).write_text("++counter;\n")
    (work / "binary.py").write_bytes(b"binary\x00text")
    first = _commit(work, "quoted files")
    for path in paths:
        (work / path).write_text("++counter;\ncount = 2\n")
    (work / "binary.py").write_bytes(b"binary\x00changed")
    head = _commit(work, "update quoted files")
    mirrors = _mirror_manager(tmp_path, work, head)
    calls = {name: _completion(name) for name in ("c-1", "c-2")}
    members = [
        _attributed_completion(
            inference_call_id=name,
            commit_sha=commit,
            file_path=path,
            ci_outcomes=[_ci(CIResult.PASSED, sha=commit)],
        )
        for name in calls
        for commit in (first, head)
        for path in paths
    ]
    expected_patches = {
        commit: _git(work, "show", "--format=", commit, "--", *paths)
        for commit in (first, head)
    }

    result = project_diff_sft(members, calls, mirrors)

    assert [
        (row.metadata.completion_id, row.metadata.commit_sha) for row in result.rows
    ] == [(name, commit) for name in sorted(calls) for commit in sorted((first, head))]
    assert result.skipped == {"unsupported_diff_section": 2}
    assert all(
        row.completion
        == [{"role": "assistant", "content": expected_patches[row.metadata.commit_sha]}]
        for row in result.rows
    )
    assert project_diff_sft(members, calls, mirrors) == result
    assert project_diff_sft(reversed(members), calls, mirrors) == result


def test_diff_sft_preserves_rename_deletion_and_empty_file_sections(tmp_path):
    work = _work_repo(tmp_path)
    (work / "old name.py").write_text(FIB)
    (work / "deleted.py").write_text(CART)
    base = _commit(work, "base")
    target = 'renamed"file.py'
    (work / "old name.py").rename(work / target)
    (work / "deleted.py").unlink()
    (work / "empty.py").touch()
    head = _commit(work, "metadata and deletion")
    mirrors = _mirror_manager(tmp_path, work, head)
    members = [
        _attributed_completion(
            inference_call_id="c-1",
            commit_sha=head,
            file_path=path,
            ci_outcomes=[_ci(CIResult.PASSED, sha=head)],
        )
        for path in (target, "deleted.py", "empty.py")
    ]
    result = project_diff_sft(members, {"c-1": _completion("c-1")}, mirrors)
    assert result.skipped == {}
    assert len(result.rows) == 1
    assert result.rows[0].completion == [
        {"role": "assistant", "content": _git(work, "diff", "-M", base, head)}
    ]
