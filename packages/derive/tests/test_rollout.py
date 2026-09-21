# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Rollout-derivation tests over real facts and real git fixtures — never mocked
(per AGENTS.md). Trajectory reconstruction (prefix chaining, compaction
segmentation, the decision join) runs against a real ``FactStore``; the
commit-binding cases build an actual git repository, stamp a real
``refs/notes/sediment`` note, mirror it through ``MirrorManager``, and run
``derive_rollouts`` as a consumer would.

The agent's output is rendered from ``InferenceCall.output_messages`` as
text, so a turn's work product is asserted on ``Turn.completion``; the
structured response-side calls ride on ``Turn.tool_calls``, carried verbatim
from ``InferenceCall.output_messages``.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from gitfixtures import FIB, commit_all, make_remote, make_work_repo, run_git
from sediment_core import (
    CIOutcome,
    CIProvider,
    CIResult,
    AgentHarness,
    DeveloperDecision,
    InteractionMode,
    FactTable,
    ForgeProvider,
    GatewayProvider,
    InferenceCall,
    InferenceMessage,
    Push,
    ReasoningPart,
    TextPart,
    ToolCallPart,
)
from sediment_capture.gateway import LiteLLMAdapter
from sediment_core.store import FactStore
from sediment_derive import (
    AttributionSource,
    CommitRef,
    MirrorManager,
    RolloutPolicy,
    derive_rollouts,
    derive_rollout_result,
    split_of,
)

ORG = "acme-corp"
REPO = "acme-corp/backend-service"
BASE = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)

FIB_OUT = "def fibonacci(n):\n    return n if n <= 1 else fibonacci(n - 1)\n"
CART_OUT = "class ShoppingCart:\n    def add_item(self, item):\n        pass\n"
BETA_OUT = "class BetaQueue:\n    def enqueue(self, task):\n        return task\n"


def _at(minutes: int) -> datetime:
    return BASE + timedelta(minutes=minutes)


def _inference_call(
    session_id: str,
    messages: list[InferenceMessage],
    completion: str,
    *,
    call_id: str | None = None,
    captured_at: datetime | None = None,
    provider: GatewayProvider = GatewayProvider.LITELLM,
    tool_calls: list[ToolCallPart] | None = None,
) -> InferenceCall:
    return InferenceCall(
        org_id=ORG,
        session_id=session_id,
        user_id="dev",
        gateway_provider=provider,
        model="claude-sonnet-5",
        input_messages=messages,
        output_messages=[
            InferenceMessage(
                role="assistant",
                parts=[TextPart(content=completion), *(tool_calls or [])],
            )
        ],
        input_tokens=10,
        output_tokens=20,
        duration_ms=50,
        model_call_id=call_id,
        observed_at=captured_at if captured_at is not None else _at(0),
    )


def _decision(
    session_id: str,
    call_id: str | None,
    *,
    accepted: bool = True,
    file_path: str = "math_utils.py",
    occurred_at: datetime | None = None,
) -> DeveloperDecision:
    return DeveloperDecision(
        org_id=ORG,
        session_id=session_id,
        user_id="dev",
        agent_harness=AgentHarness.CLAUDE_CODE,
        file_path=file_path,
        accepted=accepted,
        explicit=True,
        interaction_mode=InteractionMode.AGENT,
        call_id=call_id,
        occurred_at=occurred_at if occurred_at is not None else _at(0),
    )


def _mirrors(tmp_path: Path) -> MirrorManager:
    return MirrorManager(str(tmp_path / "mirrors"))


def _msg(role: str, content: str) -> InferenceMessage:
    return InferenceMessage(role=role, parts=[TextPart(content=content)])


def _cc_msg(role: str, text: str, *, cache: bool = False) -> InferenceMessage:
    """Translate cache-breakpoint metadata from its provider envelope."""
    block: dict = {"type": "text", "text": text}
    if cache:
        block["cache_control"] = {"type": "ephemeral"}
    return (
        LiteLLMAdapter()
        .normalize(
            {"messages": [{"role": role, "content": [block]}]},
            session_id="s1",
            user_id="dev",
            org_id=ORG,
        )
        .input_messages[0]
    )


# ── trajectory reconstruction ─────────────────────────────────────────────


def test_three_chained_completions_form_one_segment_three_turns(
    tmp_path: Path,
    postgres_store,
) -> None:
    # A session's calls chain by shared message prefix into one trajectory;
    # each turn's new_messages are exactly the suffix that call added.
    m1 = [_msg("user", "write fib")]
    m2 = m1 + [_msg("assistant", FIB_OUT), _msg("user", "write cart")]
    m3 = m2 + [_msg("assistant", CART_OUT), _msg("user", "write beta")]
    c1 = _inference_call("s1", m1, FIB_OUT, call_id="r1", captured_at=_at(0))
    c2 = _inference_call("s1", m2, CART_OUT, call_id="r2", captured_at=_at(1))
    c3 = _inference_call("s1", m3, BETA_OUT, call_id="r3", captured_at=_at(2))
    store = postgres_store
    # Stored out of capture order — the derivation sorts on the facts, never
    # arrival order (ADR 0001).
    for c in (c2, c3, c1):
        store.store_inference_call(c)

    rollouts = derive_rollouts(store, _mirrors(tmp_path), ORG)
    assert len(rollouts) == 1
    r = rollouts[0]
    assert r.session_id == "s1"
    assert len(r.segments) == 1
    turns = r.segments[0]
    assert [t.inference_call_id for t in turns] == [
        c1.inference_call_id,
        c2.inference_call_id,
        c3.inference_call_id,
    ]
    # New-message slicing: first turn adds all of its messages; each later turn
    # adds only its suffix (the prior assistant echo + the new user prompt).
    assert turns[0].new_messages == m1
    assert turns[1].new_messages == [
        _msg("assistant", FIB_OUT),
        _msg("user", "write cart"),
    ]
    assert turns[2].new_messages == [
        _msg("assistant", CART_OUT),
        _msg("user", "write beta"),
    ]
    # The work product text lives on completion; no tool calls were captured
    # here, so the structured channel is empty, never fabricated.
    assert turns[0].completion == FIB_OUT
    assert turns[0].tool_calls == ()
    # No push/commit/CI → SFT-able, not reward-labeled.
    assert r.commits == []
    assert r.terminal_outcomes == []
    assert r.attribution_source == AttributionSource.JACCARD


def test_rollout_result_preserves_the_list_returning_contract(
    tmp_path: Path, postgres_store
) -> None:
    completion = _inference_call(
        "s1", [_msg("user", "write fib")], FIB_OUT, call_id="r1"
    )
    store = postgres_store
    store.store_inference_call(completion)
    mirrors = _mirrors(tmp_path)

    result = derive_rollout_result(store, mirrors, ORG)

    assert result.rollouts == derive_rollouts(store, mirrors, ORG)
    assert result.skipped == {}


def test_turn_carries_the_completion_facts_tool_calls_in_order(
    tmp_path: Path,
    postgres_store,
) -> None:
    # The structured response-side tool calls ride on the Turn verbatim —
    # same objects, same order — and a completion that captured none yields
    # an empty tuple.
    calls = [
        ToolCallPart(
            id="toolu_1", name="write_file", arguments={"path": "math_utils.py"}
        ),
        ToolCallPart(id="toolu_2", name="run_tests", arguments={}),
    ]
    m1 = [_msg("user", "write fib")]
    first = _inference_call("s1", m1, FIB_OUT, captured_at=_at(0), tool_calls=calls)
    m2 = m1 + first.output_messages + [_msg("user", "write cart")]
    store = postgres_store
    store.store_inference_call(first)
    store.store_inference_call(_inference_call("s1", m2, CART_OUT, captured_at=_at(1)))

    turns = derive_rollouts(store, _mirrors(tmp_path), ORG)[0].segments[0]
    assert turns[0].tool_calls == tuple(calls)
    assert turns[1].tool_calls == ()


def test_compaction_breaks_the_prefix_and_opens_a_second_segment(
    tmp_path: Path,
    postgres_store,
) -> None:
    # When the runtime compacts context, the later request's history is no
    # longer prefixed by the earlier one. The segment closes and a new one
    # opens — never a fuzzy stitch (a stitched trajectory is poisoned).
    m1 = [_msg("user", "write fib")]
    m2 = m1 + [_msg("assistant", FIB_OUT), _msg("user", "write cart")]
    # Compacted: earlier turns replaced by a summary, so m2 is NOT a prefix.
    m3 = [_msg("system", "[summary of earlier turns]"), _msg("user", "write beta")]
    store = postgres_store
    store.store_inference_call(_inference_call("s1", m1, FIB_OUT, captured_at=_at(0)))
    store.store_inference_call(_inference_call("s1", m2, CART_OUT, captured_at=_at(1)))
    store.store_inference_call(_inference_call("s1", m3, BETA_OUT, captured_at=_at(2)))

    r = derive_rollouts(store, _mirrors(tmp_path), ORG)[0]
    assert [len(seg) for seg in r.segments] == [2, 1]
    # The post-compaction turn re-adds its whole (rewritten) history.
    assert r.segments[1][0].new_messages == m3
    assert r.segments[1][0].completion == BETA_OUT


def test_changed_input_reasoning_breaks_the_rollout_prefix(
    tmp_path: Path, postgres_store
) -> None:
    m1 = [
        _msg("user", "write fib"),
        InferenceMessage(
            role="assistant", parts=[ReasoningPart(content="Use recursion.")]
        ),
    ]
    m2 = [
        _msg("user", "write fib"),
        InferenceMessage(
            role="assistant", parts=[ReasoningPart(content="Use iteration.")]
        ),
        _msg("user", "continue"),
    ]
    store = postgres_store
    store.store_inference_call(_inference_call("s1", m1, FIB_OUT, captured_at=_at(0)))
    store.store_inference_call(_inference_call("s1", m2, CART_OUT, captured_at=_at(1)))

    rollout = derive_rollouts(store, _mirrors(tmp_path), ORG)[0]

    assert [len(segment) for segment in rollout.segments] == [1, 1]
    assert rollout.segments[1][0].new_messages == m2


def test_decision_joins_to_its_turn_by_call_id(tmp_path: Path, postgres_store) -> None:
    m1 = [_msg("user", "write fib")]
    m2 = m1 + [_msg("assistant", FIB_OUT), _msg("user", "write cart")]
    c1 = _inference_call("s1", m1, FIB_OUT, call_id="r1", captured_at=_at(0))
    c2 = _inference_call("s1", m2, CART_OUT, call_id="r2", captured_at=_at(1))
    store = postgres_store
    store.store_inference_call(c1)
    store.store_inference_call(c2)
    accept = _decision("s1", "r2", accepted=True)
    store.store_decision(accept)
    # A keyless decision (no call_id) can never join.
    store.store_decision(_decision("s1", None, accepted=False, file_path=""))

    turns = derive_rollouts(store, _mirrors(tmp_path), ORG)[0].segments[0]
    by_id = {t.inference_call_id: t for t in turns}
    assert by_id[c1.inference_call_id].decisions == ()
    assert by_id[c2.inference_call_id].decisions == (accept,)


def test_ambiguous_call_id_is_dropped_not_misattributed(
    tmp_path: Path, postgres_store
) -> None:
    # Unique-or-drop: a call_id resolving to more than one turn cannot be
    # bound to any of them. Two completions share "dup" across providers (the
    # UNIQUE index is per provider, so both persist); the decision keyed on it
    # attaches to neither.
    store = postgres_store
    store.store_inference_call(
        _inference_call(
            "s1",
            [_msg("user", "a")],
            FIB_OUT,
            call_id="dup",
            captured_at=_at(0),
            provider=GatewayProvider.LITELLM,
        )
    )
    store.store_inference_call(
        _inference_call(
            "s1",
            [_msg("user", "b")],
            CART_OUT,
            call_id="dup",
            captured_at=_at(1),
            provider=GatewayProvider.PORTKEY,
        )
    )
    store.store_decision(_decision("s1", "dup"))

    result = derive_rollout_result(store, _mirrors(tmp_path), ORG)
    r = result.rollouts[0]
    assert all(t.decisions == () for seg in r.segments for t in seg)
    assert result.skipped == {"ambiguous_decision_call_id": 1}


# ── cache_control normalization ───────────────────────────────────────────


def _cc_session(tmp_path: Path, store: FactStore):
    """The diagnosis's minimal reproduction: one logical 3-turn conversation
    whose consecutive request histories are byte-identical in conversation text
    and differ *only* in where the ephemeral cache_control marker sits — the SDK
    relocates it to the newest block of each request."""
    m1 = [_cc_msg("user", "Fix the bug", cache=True)]
    m2 = [
        _cc_msg("user", "Fix the bug"),
        _cc_msg("assistant", "Done"),
        _cc_msg("user", "Add a test", cache=True),  # marker moved to block 3
    ]
    m3 = [
        _cc_msg("user", "Fix the bug"),
        _cc_msg("assistant", "Done"),
        _cc_msg("user", "Add a test"),
        _cc_msg("assistant", "Added"),
        _cc_msg("user", "Ship it", cache=True),  # marker moved to block 5
    ]
    store.store_inference_call(_inference_call("s1", m1, "Done", captured_at=_at(0)))
    store.store_inference_call(_inference_call("s1", m2, "Added", captured_at=_at(1)))
    store.store_inference_call(_inference_call("s1", m3, "Shipped", captured_at=_at(2)))
    return store, (m1, m2, m3)


def test_relocating_cache_control_marker_yields_one_segment(
    tmp_path: Path, postgres_store
) -> None:
    # A moved cache_control marker is not conversation, so the three chained
    # calls form ONE trajectory, not three fragments.
    store, (m1, m2, m3) = _cc_session(tmp_path, postgres_store)

    r = derive_rollouts(store, _mirrors(tmp_path), ORG)[0]
    assert [len(seg) for seg in r.segments] == [3]
    # Translation removed envelope metadata before creating these typed Facts.
    # Derivation preserves their messages, including every continued suffix.
    turns = r.segments[0]
    assert turns[0].new_messages == m1
    assert turns[1].new_messages == m2[len(m1) :]
    assert turns[2].new_messages == m3[len(m2) :]


def test_relocating_marker_plus_real_rewrite_still_splits(
    tmp_path: Path, postgres_store
) -> None:
    # Negative control: the three cache_control-only pairs collapse to one
    # segment, but a genuine history rewrite appended after still opens a new
    # segment — the filter subtracts false splits, never a true one.
    store, (_m1, _m2, m3) = _cc_session(tmp_path, postgres_store)
    m4 = [
        _cc_msg("system", "[summary of earlier turns]"),
        _cc_msg("user", "Ship it", cache=True),
    ]
    store.store_inference_call(
        _inference_call("s1", m4, "Shipped again", captured_at=_at(3))
    )

    r = derive_rollouts(store, _mirrors(tmp_path), ORG)[0]
    assert [len(seg) for seg in r.segments] == [3, 1]
    assert r.segments[1][0].new_messages == m4


def test_cache_control_inside_tool_result_remains_semantic(
    tmp_path: Path, postgres_store
) -> None:
    # This key lives inside a canonical result, not provider cache metadata.
    from sediment_core import ToolCallResponsePart

    def result_message(value: str) -> InferenceMessage:
        return InferenceMessage(
            role="tool",
            parts=[
                ToolCallResponsePart(
                    id="tool-1", result={"cache_control": value, "text": "contents"}
                )
            ],
        )

    m1 = [result_message("keep")]
    m2 = [result_message("replace"), _msg("assistant", "ok"), _msg("user", "next")]
    postgres_store.store_inference_call(
        _inference_call("s1", m1, "ok", captured_at=_at(0))
    )
    postgres_store.store_inference_call(
        _inference_call("s1", m2, "done", captured_at=_at(1))
    )
    result = derive_rollout_result(postgres_store, _mirrors(tmp_path), ORG)
    assert [len(seg) for seg in result.rollouts[0].segments] == [1, 1]
    assert result.fragmented == {"input_history_changed": 1}


def test_actual_text_change_in_an_early_block_still_splits(
    tmp_path: Path, postgres_store
) -> None:
    # Normalization drops only cache_control. A single changed character in an
    # early block's text is real divergence: the prefix breaks and it splits.
    m1 = [_cc_msg("user", "Fix the bug", cache=True)]
    m2 = [
        _cc_msg("user", "Fix the typo"),  # was "Fix the bug" — genuine rewrite
        _cc_msg("assistant", "Done"),
        _cc_msg("user", "Add a test", cache=True),
    ]
    store = postgres_store
    store.store_inference_call(_inference_call("s1", m1, "Done", captured_at=_at(0)))
    store.store_inference_call(_inference_call("s1", m2, "Added", captured_at=_at(1)))

    r = derive_rollouts(store, _mirrors(tmp_path), ORG)[0]
    assert [len(seg) for seg in r.segments] == [1, 1]


def test_cache_control_normalization_is_deterministic_across_runs(
    tmp_path: Path,
    postgres_store,
) -> None:
    # A pure comparison-time view: re-deriving over the same facts reproduces the
    # identical single-segment rollout (ADR 0001).
    store, _ = _cc_session(tmp_path, postgres_store)
    mirrors = _mirrors(tmp_path)
    first = derive_rollouts(store, mirrors, ORG)
    assert derive_rollouts(store, mirrors, ORG) == first
    assert [len(seg) for seg in first[0].segments] == [3]


# ── commit binding (real git) ─────────────────────────────────────────────


def _push_and_mirror(
    tmp_path: Path, store: FactStore, work: Path, before: str, after: str
) -> MirrorManager:
    remote = make_remote(tmp_path, work)
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
    # Within the attribution lookback, anchored (like ingest) shortly before
    # the push whose captured_at defaults to now.
    return datetime.now(UTC) - timedelta(minutes=5)


def test_notes_bind_commits_with_attribution_source_notes(
    tmp_path: Path, postgres_store
) -> None:
    work = make_work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head = commit_all(work, "add fibonacci")
    run_git(work, "notes", "--ref=sediment", "add", "-m", _note("sess-noted"), head)
    store = postgres_store
    mirrors = _push_and_mirror(tmp_path, store, work, "0" * 40, head)
    store.store_inference_call(
        _inference_call(
            "sess-noted", [_msg("user", "write fib")], FIB, captured_at=_recent()
        )
    )

    r = _only(derive_rollouts(store, mirrors, ORG), "sess-noted")
    assert r.commits == [CommitRef(repo=REPO, commit_sha=head)]
    assert r.attribution_source == AttributionSource.GIT_NOTES
    # No CI stored → emitted, but not reward-labeled.
    assert r.terminal_outcomes == []


def test_unstamped_repo_falls_back_to_jaccard_binding(
    tmp_path: Path, postgres_store
) -> None:
    work = make_work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head = commit_all(work, "add fibonacci")  # no note
    store = postgres_store
    mirrors = _push_and_mirror(tmp_path, store, work, "0" * 40, head)
    store.store_inference_call(
        _inference_call(
            "sess-1", [_msg("user", "write fib")], FIB, captured_at=_recent()
        )
    )

    r = _only(derive_rollouts(store, mirrors, ORG), "sess-1")
    assert r.commits == [CommitRef(repo=REPO, commit_sha=head)]
    assert r.attribution_source == AttributionSource.JACCARD


def test_malformed_attribution_note_is_counted_before_jaccard_fallback(
    tmp_path: Path,
    postgres_store,
) -> None:
    work = make_work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head = commit_all(work, "add fibonacci")
    run_git(work, "notes", "--ref=sediment", "add", "-m", "{not-json", head)
    store = postgres_store
    mirrors = _push_and_mirror(tmp_path, store, work, "0" * 40, head)
    store.store_inference_call(
        _inference_call(
            "sess-1", [_msg("user", "write fib")], FIB, captured_at=_recent()
        )
    )

    result = derive_rollout_result(store, mirrors, ORG)

    assert result.rollouts[0].attribution_source == AttributionSource.JACCARD
    assert result.skipped["attribution_note_unreadable"] == 1


def test_terminal_outcomes_carry_every_ci_result_for_bound_commits(
    tmp_path: Path,
    postgres_store,
) -> None:
    # All outcomes for an attributed commit are carried (a red run and its
    # green re-run) — consumers pick their own reduction.
    work = make_work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head = commit_all(work, "add fibonacci")
    run_git(work, "notes", "--ref=sediment", "add", "-m", _note("sess-noted"), head)
    store = postgres_store
    mirrors = _push_and_mirror(tmp_path, store, work, "0" * 40, head)
    store.store_inference_call(
        _inference_call(
            "sess-noted", [_msg("user", "write fib")], FIB, captured_at=_recent()
        )
    )
    for result, run in ((CIResult.FAILED, "run/1"), (CIResult.PASSED, "run/2")):
        store.store_ci_outcome(
            CIOutcome(
                org_id=ORG,
                provider=CIProvider.GITHUB_ACTIONS,
                run_id=run,
                repo=REPO,
                commit_sha=head,
                branch="main",
                result=result,
                run_url=run,
            )
        )

    r = _only(derive_rollouts(store, mirrors, ORG), "sess-noted")
    assert {o.result for o in r.terminal_outcomes} == {CIResult.FAILED, CIResult.PASSED}
    assert all(o.commit_sha == head for o in r.terminal_outcomes)


def test_same_sha_in_a_different_repo_does_not_contaminate_the_reward(
    tmp_path: Path,
    postgres_store,
) -> None:
    # Forks and shared-history repos can carry the same commit_sha. Terminal
    # selection keys on (repo, sha), so only the CI verdict from the repo the
    # session was attributed to attaches — never the other repo's.
    work = make_work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head = commit_all(work, "add fibonacci")
    run_git(work, "notes", "--ref=sediment", "add", "-m", _note("sess-noted"), head)
    store = postgres_store
    mirrors = _push_and_mirror(tmp_path, store, work, "0" * 40, head)
    store.store_inference_call(
        _inference_call(
            "sess-noted", [_msg("user", "write fib")], FIB, captured_at=_recent()
        )
    )
    # The attributed repo's verdict — this one must land.
    store.store_ci_outcome(
        CIOutcome(
            org_id=ORG,
            provider=CIProvider.GITHUB_ACTIONS,
            run_id="ours/1",
            repo=REPO,
            commit_sha=head,
            branch="main",
            result=CIResult.PASSED,
            run_url="ours/1",
        )
    )
    # A fork sharing the exact same commit_sha, with the opposite verdict — it
    # must NOT contaminate this rollout's reward.
    store.store_ci_outcome(
        CIOutcome(
            org_id=ORG,
            provider=CIProvider.GITHUB_ACTIONS,
            run_id="theirs/1",
            repo="other-corp/fork",
            commit_sha=head,
            branch="main",
            result=CIResult.FAILED,
            run_url="theirs/1",
        )
    )

    r = _only(derive_rollouts(store, mirrors, ORG), "sess-noted")
    assert [(o.repo, o.result) for o in r.terminal_outcomes] == [
        (REPO, CIResult.PASSED)
    ]


def test_identical_shas_in_different_repos_remain_distinct_commits(
    tmp_path: Path,
    postgres_store,
) -> None:
    work = make_work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head = commit_all(work, "add fibonacci")
    run_git(work, "notes", "--ref=sediment", "add", "-m", _note("sess-noted"), head)
    remote = make_remote(tmp_path, work)
    store = postgres_store
    mirrors = _mirrors(tmp_path)
    repos = (REPO, "acme-corp/backend-fork")
    for repo in repos:
        push = Push(
            org_id=ORG,
            provider=ForgeProvider.GITHUB,
            repo=repo,
            clone_url=str(remote),
            ref="refs/heads/main",
            before_sha="0" * 40,
            after_sha=head,
        )
        mirrors.ensure(push)
        store.store_push(push)
    store.store_inference_call(
        _inference_call(
            "sess-noted", [_msg("user", "write fib")], FIB, captured_at=_recent()
        )
    )

    rollout = _only(derive_rollouts(store, mirrors, ORG), "sess-noted")

    assert rollout.commits == [
        CommitRef(repo=repo, commit_sha=head) for repo in sorted(repos)
    ]


def test_rename_window_loses_notes_bound_commits(
    tmp_path: Path, postgres_store
) -> None:
    # The notes scan must enumerate repos from disk truth,
    # not push Facts. Push.repo keeps the pre-rename name forever, so after
    # MirrorManager.rename moves the mirror directory, a push-derived scan
    # opens the old name, finds nothing, and the session's commits -- and
    # their reward labels -- vanish. Disk truth keeps the binding.
    NEW_REPO = "acme-corp/backend-renamed"
    work = make_work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head = commit_all(work, "add fibonacci")
    run_git(work, "notes", "--ref=sediment", "add", "-m", _note("sess-noted"), head)
    store = postgres_store
    mirrors = _push_and_mirror(tmp_path, store, work, "0" * 40, head)
    store.store_inference_call(
        _inference_call(
            "sess-noted", [_msg("user", "write fib")], FIB, captured_at=_recent()
        )
    )

    assert mirrors.rename(ORG, REPO, NEW_REPO) is True

    r = _only(derive_rollouts(store, mirrors, ORG), "sess-noted")
    assert r.commits == [CommitRef(repo=NEW_REPO, commit_sha=head)]
    assert r.attribution_source == AttributionSource.GIT_NOTES


# ── quarantine + determinism ──────────────────────────────────────────────


def test_quarantined_completion_drops_from_the_trajectory(
    tmp_path: Path, postgres_store
) -> None:
    m1 = [_msg("user", "write fib")]
    m2 = m1 + [_msg("assistant", FIB_OUT), _msg("user", "write cart")]
    c1 = _inference_call("s1", m1, FIB_OUT, captured_at=_at(0))
    c2 = _inference_call("s1", m2, CART_OUT, captured_at=_at(1))
    store = postgres_store
    store.store_inference_call(c1)
    store.store_inference_call(c2)

    before = derive_rollouts(store, _mirrors(tmp_path), ORG)[0]
    assert len(before.segments[0]) == 2
    assert before.provenance.quarantine_revision == 0

    store.quarantine_fact(
        ORG,
        FactTable.INFERENCE_CALLS,
        c2.inference_call_id,
        reason="leaked ingest token",
    )
    after = derive_rollouts(store, _mirrors(tmp_path), ORG)[0]
    assert [t.inference_call_id for seg in after.segments for t in seg] == [
        c1.inference_call_id
    ]
    assert after.provenance.quarantine_revision > 0


def test_determinism_same_facts_and_policy_yield_identical_rollouts(
    tmp_path: Path,
    postgres_store,
) -> None:
    work = make_work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head = commit_all(work, "add fibonacci")
    run_git(work, "notes", "--ref=sediment", "add", "-m", _note("sess-noted"), head)
    store = postgres_store
    mirrors = _push_and_mirror(tmp_path, store, work, "0" * 40, head)
    c = _inference_call(
        "sess-noted",
        [_msg("user", "write fib")],
        FIB,
        call_id="r1",
        captured_at=_recent(),
    )
    store.store_inference_call(c)
    store.store_decision(_decision("sess-noted", "r1"))
    store.store_ci_outcome(
        CIOutcome(
            org_id=ORG,
            provider=CIProvider.GITHUB_ACTIONS,
            run_id="run/1",
            repo=REPO,
            commit_sha=head,
            branch="main",
            result=CIResult.PASSED,
            run_url="run/1",
        )
    )
    policy = RolloutPolicy()

    first = derive_rollouts(store, mirrors, ORG, policy)
    assert derive_rollouts(store, mirrors, ORG, policy) == first
    for _ in range(2):
        streamed = []
        result = derive_rollout_result(
            store, mirrors, ORG, policy, rollout_sink=streamed.append
        )
        assert streamed == first
        assert result.rollouts == []
    # And the reward + decision actually landed on the trajectory.
    r = _only(first, "sess-noted")
    assert [o.result for o in r.terminal_outcomes] == [CIResult.PASSED]
    assert len(r.segments[0][0].decisions) == 1


def test_determinism_shuffled_ingest_order_yields_identical_rollouts(
    tmp_path: Path,
    postgres_store,
    postgres_store_factory,
) -> None:
    # The multi-repo shape stresses the set-backed notes binding and the
    # (committer time, repo, sha) commit ordering: one SHA noted in two repos,
    # CI in both, two inference calls, one decision — stored in opposite
    # orders into two stores must derive identical rollouts.
    work = make_work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head = commit_all(work, "add fibonacci")
    run_git(work, "notes", "--ref=sediment", "add", "-m", _note("sess-noted"), head)
    remote = make_remote(tmp_path, work)
    repos = (REPO, "acme-corp/backend-fork")
    pushes = [
        Push(
            org_id=ORG,
            provider=ForgeProvider.GITHUB,
            repo=repo,
            clone_url=str(remote),
            ref="refs/heads/main",
            before_sha="0" * 40,
            after_sha=head,
        )
        for repo in repos
    ]
    calls = [
        _inference_call(
            "sess-noted",
            [_msg("user", "write fib")],
            FIB,
            call_id="r1",
            captured_at=_recent(),
        ),
        _inference_call(
            "sess-noted",
            [
                _msg("user", "write fib"),
                _msg("assistant", FIB),
                _msg("user", "now cart"),
            ],
            CART_OUT,
            call_id="r2",
            captured_at=_recent() + timedelta(minutes=1),
        ),
    ]
    decisions = [_decision("sess-noted", "r1")]
    outcomes = [
        CIOutcome(
            org_id=ORG,
            provider=CIProvider.GITHUB_ACTIONS,
            run_id=f"run/{repo}",
            repo=repo,
            commit_sha=head,
            branch="main",
            result=CIResult.PASSED,
            run_url=f"run/{repo}",
        )
        for repo in repos
    ]

    rollouts_by_order = []
    for reverse in (False, True):
        order = -1 if reverse else 1
        _, store = postgres_store_factory()
        mirrors = MirrorManager(str(tmp_path / f"mirrors-{reverse}"))
        for push in pushes[::order]:
            mirrors.ensure(push)
            store.store_push(push)
        for call in calls[::order]:
            store.store_inference_call(call)
        for decision in decisions[::order]:
            store.store_decision(decision)
        for outcome in outcomes[::order]:
            store.store_ci_outcome(outcome)
        expected = derive_rollout_result(store, mirrors, ORG)
        streamed = []
        result = derive_rollout_result(
            store, mirrors, ORG, rollout_sink=streamed.append
        )
        assert streamed == expected.rollouts
        assert result.skipped == expected.skipped
        assert result.fragmented == expected.fragmented
        rollouts_by_order.append(streamed)

    assert rollouts_by_order[0] == rollouts_by_order[1]
    rollout = _only(rollouts_by_order[0], "sess-noted")
    assert rollout.commits == [
        CommitRef(repo=repo, commit_sha=head) for repo in sorted(repos)
    ]
    assert [o.repo for o in rollout.terminal_outcomes] == sorted(repos)


# ── eval holdout split ────────────────────────────────────────────────────


def test_rollout_uses_canonical_default_split_primitive(
    tmp_path: Path,
    postgres_store,
) -> None:
    # The canonical default is eval_fraction 0.1. Each rollout's split is
    # exactly the standalone primitive's verdict on its
    # session — one hash, one code path, whether it surfaces as a rollout or
    # (later) a attributed completion.
    store = postgres_store
    for sid in ("alpha", "bravo", "charlie", "delta"):
        store.store_inference_call(
            _inference_call(
                sid, [_msg("user", "write fib")], FIB_OUT, captured_at=_at(0)
            )
        )

    for rollout in derive_rollouts(store, _mirrors(tmp_path), ORG):
        assert rollout.split == split_of(rollout.session_id, 0.1)

    policy = RolloutPolicy(eval_fraction=0.5)
    rollouts = derive_rollouts(store, _mirrors(tmp_path), ORG, policy)
    for r in rollouts:
        assert r.split == split_of(r.session_id, 0.5)
    # Re-derivation reproduces the identical split assignment.
    again = derive_rollouts(store, _mirrors(tmp_path), ORG, policy)
    assert [r.split for r in again] == [r.split for r in rollouts]


def test_eval_fraction_flips_only_the_split_leaving_the_rollout_byte_identical(
    tmp_path: Path,
    postgres_store,
) -> None:
    # Turning the holdout on must change nothing but the split label — segments,
    # commits, terminal outcomes, and provenance stay byte-for-byte identical,
    # so an unconfigured deployment's derived data is unchanged.
    work = make_work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head = commit_all(work, "add fibonacci")
    run_git(work, "notes", "--ref=sediment", "add", "-m", _note("sess-noted"), head)
    store = postgres_store
    mirrors = _push_and_mirror(tmp_path, store, work, "0" * 40, head)
    store.store_inference_call(
        _inference_call(
            "sess-noted", [_msg("user", "write fib")], FIB, captured_at=_recent()
        )
    )
    store.store_ci_outcome(
        CIOutcome(
            org_id=ORG,
            provider=CIProvider.GITHUB_ACTIONS,
            run_id="run/1",
            repo=REPO,
            commit_sha=head,
            branch="main",
            result=CIResult.PASSED,
            run_url="run/1",
        )
    )

    off = derive_rollouts(store, mirrors, ORG, RolloutPolicy(eval_fraction=0.0))
    on = derive_rollouts(store, mirrors, ORG, RolloutPolicy(eval_fraction=0.5))
    assert all(r.split == "train" for r in off)
    # Normalising the split to a constant collapses the two to equality iff
    # every other field is identical.
    normalize = lambda rs: [dataclasses.replace(r, split="train") for r in rs]  # noqa: E731
    assert normalize(on) == normalize(off)


def test_rollout_policy_rejects_an_out_of_range_fraction() -> None:
    # The config loader is the primary 0.0–0.5 boundary, but the policy re-checks
    # so no code path can smuggle a degenerate fraction into the split.
    RolloutPolicy(eval_fraction=0.5)  # boundary is allowed
    with pytest.raises(ValueError):
        RolloutPolicy(eval_fraction=0.6)
    with pytest.raises(ValueError):
        RolloutPolicy(eval_fraction=-0.1)


def _only(rollouts: list, session_id: str):
    matches = [r for r in rollouts if r.session_id == session_id]
    assert len(matches) == 1, f"expected one rollout for {session_id}"
    return matches[0]


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("text", None),
        ("tool", None),
        ("reasoning", None),
        ("non_finite", None),
        ("object_order", None),
        ("absent_output", "prior_output_absent"),
        ("empty_output_parts", "prior_output_absent"),
        ("empty_output_text", "prior_output_absent"),
        ("output_absent_and_input_changed", "prior_output_absent"),
        ("absent_echo", "prior_output_not_replayed"),
        ("contradictory_echo", "prior_output_not_replayed"),
        ("rewritten_input", "input_history_changed"),
        ("shortened_input", "input_history_changed"),
        ("regrouped_echo", "prior_output_not_replayed"),
        ("reordered_parts", "prior_output_not_replayed"),
        ("changed_role", "prior_output_not_replayed"),
        ("literal_json", "input_history_changed"),
        ("literal_json_order", "input_history_changed"),
        ("literal_json_unchanged", None),
        ("boolean_vs_number", "prior_output_not_replayed"),
        ("semantic_cache_control", "prior_output_not_replayed"),
        ("different_non_finite", "prior_output_not_replayed"),
    ],
)
def test_foundation_typed_continuity_retains_turns_and_counts_boundaries(
    tmp_path: Path, postgres_store, case: str, reason: str | None
) -> None:
    """Public Derivation compares typed Facts and preserves each retained Turn."""
    before = [_msg("user", "Implement the change")]
    output = [_msg("assistant", "ok")]
    echo = [_msg("assistant", "ok")]
    next_before = before
    if case in {
        "tool",
        "object_order",
        "semantic_cache_control",
        "non_finite",
        "different_non_finite",
        "boolean_vs_number",
    }:
        arguments = {"path": "a.py", "cache_control": "keep"}
        replay_arguments = {"cache_control": "keep", "path": "a.py"}
        if case in {"non_finite", "different_non_finite"}:
            arguments = {"numbers": [float("nan"), float("inf"), -float("inf")]}
            replay_arguments = {"numbers": [float("nan"), float("inf"), -float("inf")]}
            if case == "different_non_finite":
                replay_arguments["numbers"][1] = -float("inf")
        elif case == "semantic_cache_control":
            replay_arguments["cache_control"] = "replace"
        elif case == "boolean_vs_number":
            arguments = {"enabled": True}
            replay_arguments = {"enabled": 1}
        output = [
            InferenceMessage(
                role="assistant",
                parts=[ToolCallPart(id="tool-1", name="Edit", arguments=arguments)],
                finish_reason="tool_calls",
            )
        ]
        echo = [
            InferenceMessage(
                role="assistant",
                parts=[
                    ToolCallPart(id="tool-1", name="Edit", arguments=replay_arguments)
                ],
            )
        ]
    elif case in {"reasoning", "reordered_parts", "regrouped_echo"}:
        parts = [ReasoningPart(content="Check the constraints"), TextPart(content="ok")]
        output = [InferenceMessage(role="assistant", parts=parts, finish_reason="stop")]
        echo = [InferenceMessage(role="assistant", parts=parts)]
        if case == "reordered_parts":
            echo = [InferenceMessage(role="assistant", parts=list(reversed(parts)))]
        elif case == "regrouped_echo":
            echo = [InferenceMessage(role="assistant", parts=[part]) for part in parts]
    elif case in {"absent_output", "output_absent_and_input_changed"}:
        output = []
        if case == "output_absent_and_input_changed":
            next_before = [_msg("user", "A different request")]
    elif case == "empty_output_parts":
        output = [InferenceMessage(role="assistant", parts=[])]
    elif case == "empty_output_text":
        output = [_msg("assistant", "")]
    elif case == "absent_echo":
        echo = []
    elif case == "contradictory_echo":
        echo = [_msg("assistant", "A different answer")]
    elif case == "changed_role":
        echo = [_msg("user", "ok")]
    elif case == "rewritten_input":
        next_before = [_msg("user", "Summarized context")]
    elif case == "shortened_input":
        next_before = []
    elif case == "literal_json":
        before = [_msg("user", '[{"cache_control":"keep","value":1}]')]
        next_before = [_msg("user", '[{"cache_control":"replace","value":1}]')]
    elif case in {"literal_json_order", "literal_json_unchanged"}:
        before = [_msg("user", '[{"first":1,"second":2}]')]
        next_before = (
            before
            if case == "literal_json_unchanged"
            else [_msg("user", '[{"second":2,"first":1}]')]
        )
    after = next_before + echo + [_msg("user", "Continue")]
    first = _inference_call("s1", before, "unused", captured_at=_at(0)).model_copy(
        update={"output_messages": output}
    )
    second = _inference_call("s1", after, "done", captured_at=_at(1))
    for call in (second, first):
        postgres_store.store_inference_call(call)
    result = derive_rollout_result(postgres_store, _mirrors(tmp_path), ORG)
    rollout = result.rollouts[0]
    assert [len(segment) for segment in rollout.segments] == (
        [2] if reason is None else [1, 1]
    )
    assert result.fragmented == ({} if reason is None else {reason: 1})
    assert result.skipped == {}
    assert [
        turn.inference_call_id for segment in rollout.segments for turn in segment
    ] == [first.inference_call_id, second.inference_call_id]
    expected_messages = after[len(before) :] if reason is None else after
    # NaN isn't reflexive; compare the lossless categories without JSON-mode
    # Pydantic serialization, which would convert them to null.
    assert json.dumps(
        [
            message.model_dump(mode="python")
            for message in rollout.segments[-1][-1].new_messages
        ],
        sort_keys=True,
    ) == json.dumps(
        [message.model_dump(mode="python") for message in expected_messages],
        sort_keys=True,
    )
    assert rollout.provenance.policy_version == "4"


def test_foundation_fragmentation_determinism_and_default_policy(
    tmp_path: Path, postgres_store_factory
) -> None:
    before = [_msg("user", "Implement the change")]
    first = _inference_call("s1", before, "ok", captured_at=_at(0))
    second = _inference_call(
        "s1", before + [_msg("assistant", "contradiction")], "done", captured_at=_at(1)
    )
    third = _inference_call(
        "s1",
        second.input_messages + second.output_messages + [_msg("user", "Continue")],
        "finished",
        captured_at=_at(2),
    )
    _, forward = postgres_store_factory()
    _, shuffled = postgres_store_factory()
    for call in (first, second, third):
        forward.store_inference_call(call)
    for call in (third, first, second):
        shuffled.store_inference_call(call)
    mirrors = _mirrors(tmp_path)
    expected = derive_rollout_result(forward, mirrors, ORG)
    assert expected.fragmented == {"prior_output_not_replayed": 1}
    assert derive_rollout_result(forward, mirrors, ORG) == expected
    assert derive_rollout_result(shuffled, mirrors, ORG) == expected
    assert RolloutPolicy().policy_version == "4"


def test_rollout_intentionally_checks_ambiguity_within_each_session(
    tmp_path: Path, postgres_store
) -> None:
    calls = [
        _inference_call(
            session,
            [_msg("user", session)],
            FIB_OUT,
            call_id="shared",
            provider=provider,
            captured_at=_at(index),
        )
        for index, (session, provider) in enumerate(
            [("s1", GatewayProvider.LITELLM), ("s2", GatewayProvider.PORTKEY)]
        )
    ]
    decision = _decision("s1", "shared")
    for call in calls:
        postgres_store.store_inference_call(call)
    postgres_store.store_decision(decision)
    result = derive_rollout_result(postgres_store, _mirrors(tmp_path), ORG)
    decisions = {
        rollout.session_id: [
            d
            for segment in rollout.segments
            for turn in segment
            for d in turn.decisions
        ]
        for rollout in result.rollouts
    }
    assert decisions == {"s1": [decision], "s2": []}
    assert result.skipped == {}
    assert all(row.provenance.policy_version == "4" for row in result.rollouts)


def test_public_rollout_observation_identity_boundary_and_permutation(
    tmp_path, postgres_store
):
    import random
    from sediment_core import SessionCommitObservation

    work = make_work_repo(tmp_path)
    (work / "README").write_text("base")
    base = commit_all(work, "base")
    (work / "math_utils.py").write_text(FIB)
    head = commit_all(work, "add fibonacci")
    run_git(work, "notes", "--ref=sediment", "add", "-m", _note("sess-noted"), head)
    mirrors = _push_and_mirror(tmp_path, postgres_store, work, base, head)
    postgres_store.store_inference_call(
        _inference_call(
            "sess-noted", [_msg("user", "write fib")], FIB, captured_at=_recent()
        )
    )
    boundary = datetime.now(UTC)
    exact = SessionCommitObservation(
        observation_id="exact",
        org_id=ORG,
        session_id="sess-noted",
        repo=REPO,
        commit_sha=head,
        source_push_id="push",
        captured_at=boundary,
    )
    earlier = exact.model_copy(
        update={
            "observation_id": "earlier",
            "captured_at": boundary - timedelta(seconds=1),
        }
    )
    observations = [
        exact,
        earlier,
        *[
            exact.model_copy(update={"observation_id": label, **changes})
            for label, changes in [
                ("wrong-org", {"org_id": "other"}),
                ("wrong-session", {"session_id": "other"}),
                ("wrong-repo", {"repo": "acme-corp/other"}),
                ("wrong-commit", {"commit_sha": base}),
                ("late", {"captured_at": boundary + timedelta(microseconds=1)}),
            ]
        ],
    ]
    results = []
    for seed in range(4):
        shuffled = list(observations)
        random.Random(seed).shuffle(shuffled)
        result = derive_rollout_result(
            postgres_store,
            mirrors,
            ORG,
            session_commit_observations=shuffled,
            as_of=boundary,
        )
        rollout = _only(result.rollouts, "sess-noted")
        assert rollout.commits == [CommitRef(repo=REPO, commit_sha=head)]
        assert rollout.session_commit_observations == (earlier, exact)
        results.append(result)
    assert all(result == results[0] for result in results)


@pytest.mark.parametrize("binding", ["notes", "jaccard"])
def test_rollout_sink_receives_final_facts_and_preserves_materialized_output(
    postgres_store, tmp_path, binding
):
    work = make_work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head = commit_all(work, "add fibonacci")
    if binding == "notes":
        run_git(work, "notes", "--ref=sediment", "add", "-m", _note("session"), head)
    mirrors = _push_and_mirror(tmp_path, postgres_store, work, "0" * 40, head)
    call = _inference_call(
        "session", [_msg("user", "write fib")], FIB, call_id="r1", captured_at=_recent()
    )
    decision = _decision("session", "r1").model_copy(
        update={"raw": {"evidence": "decision source"}}
    )
    outcome = CIOutcome(
        org_id=ORG,
        provider=CIProvider.GITHUB_ACTIONS,
        run_id="run/1",
        repo=REPO,
        commit_sha=head,
        branch="main",
        result=CIResult.PASSED,
        raw={"evidence": "CI source"},
    )
    postgres_store.store_inference_call(call)
    postgres_store.store_decision(decision)
    postgres_store.store_ci_outcome(outcome)
    expected = derive_rollout_result(postgres_store, mirrors, ORG)
    emitted = []

    def receive(rollout):
        # The callback observes final canonical Facts, including their raw fields.
        assert rollout.segments[0][0].decisions == (decision,)
        assert rollout.terminal_outcomes == [outcome]
        emitted.append(rollout)

    result = derive_rollout_result(postgres_store, mirrors, ORG, rollout_sink=receive)
    assert emitted == expected.rollouts
    assert result.rollouts == []
    assert result.skipped == expected.skipped
    assert result.fragmented == expected.fragmented


def test_rollout_sink_bounds_histories_and_releases_prior_rollouts(
    postgres_store, tmp_path
):
    import gc
    import tracemalloc
    import weakref

    for session in range(16):
        for index in range(2):
            postgres_store.store_inference_call(
                _inference_call(
                    f"session-{session:02d}",
                    [_msg("user", "x" * (1024 * 1024))],
                    "done",
                    captured_at=_at(index),
                )
            )
    references = []
    session_ids = []
    total_content = 0

    def receive(rollout):
        nonlocal total_content
        gc.collect()
        assert all(reference() is None for reference in references)
        references.append(weakref.ref(rollout))
        session_ids.append(rollout.session_id)
        total_content += sum(
            len(message.parts[0].content)
            for segment in rollout.segments
            for turn in segment
            for message in turn.new_messages
        )

    gc.collect()
    tracemalloc.start()
    try:
        result = derive_rollout_result(
            postgres_store, _mirrors(tmp_path), ORG, rollout_sink=receive
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 14 * 1024 * 1024, f"streamed Rollouts allocated {peak} bytes"
    assert total_content == 32 * 1024 * 1024
    assert session_ids == sorted(session_ids)
    assert len(session_ids) == 16
    assert result.rollouts == []
    assert result.skipped == {}
    assert result.fragmented == {"prior_output_not_replayed": 16}


def test_rollout_sink_preserves_complete_sessions_and_one_snapshot(
    postgres_store, tmp_path
):
    earlier = _inference_call(
        "session-a", [_msg("user", "earlier")], "one", captured_at=_at(-1000)
    )
    inside = _inference_call(
        "session-a", [_msg("user", "inside")], "two", captured_at=_at(0)
    )
    future = _inference_call(
        "session-a", [_msg("user", "future")], "three", captured_at=_at(10)
    )
    second = _inference_call(
        "session-b", [_msg("user", "second")], "done", captured_at=_at(0)
    )
    for call in (future, second, inside, earlier):
        postgres_store.store_inference_call(call)
    emitted = []

    def receive(rollout):
        emitted.append(rollout)
        if rollout.session_id == "session-a":
            postgres_store.quarantine_fact(
                ORG,
                FactTable.INFERENCE_CALLS,
                second.inference_call_id,
                reason="review",
            )
            postgres_store.store_inference_call(
                _inference_call(
                    "late-session",
                    [_msg("user", "late arrival")],
                    "done",
                    captured_at=_at(-1),
                )
            )

    result = derive_rollout_result(
        postgres_store, _mirrors(tmp_path), ORG, as_of=_at(0), rollout_sink=receive
    )
    assert [row.session_id for row in emitted] == ["session-a", "session-b"]
    assert [
        turn.inference_call_id for segment in emitted[0].segments for turn in segment
    ] == [earlier.inference_call_id, inside.inference_call_id]
    assert emitted[1].segments[0][0].inference_call_id == second.inference_call_id
    assert result.fragmented == {"input_history_changed": 1}
    after = derive_rollouts(postgres_store, _mirrors(tmp_path), ORG, as_of=_at(0))
    assert [row.session_id for row in after] == ["late-session", "session-a"]


def test_rollout_sink_failure_propagates_without_a_success_result(
    postgres_store, tmp_path
):
    for session in ("session-a", "session-b"):
        postgres_store.store_inference_call(
            _inference_call(session, [_msg("user", session)], "done")
        )
    emitted = []

    def refuse(rollout):
        emitted.append(rollout.session_id)
        raise OSError("private staging is full")

    with pytest.raises(OSError, match="private staging is full"):
        derive_rollout_result(
            postgres_store, _mirrors(tmp_path), ORG, rollout_sink=refuse
        )
    assert emitted == ["session-a"]
    assert len(derive_rollouts(postgres_store, _mirrors(tmp_path), ORG)) == 2


def test_rollout_session_capacity_failure_propagates_after_prior_staging(
    postgres_store, tmp_path, monkeypatch
):
    from sediment_core import OperationalReportLimitExceeded
    from sediment_core import store as store_module

    for session, content in (("session-a", "small"), ("session-b", "x" * 2000)):
        postgres_store.store_inference_call(
            _inference_call(session, [_msg("user", content)], "done")
        )
    monkeypatch.setattr(store_module, "INFERENCE_SESSION_BYTES_LIMIT", 1000)
    staged = []
    with pytest.raises(OperationalReportLimitExceeded, match="Session.*encoded.*bytes"):
        derive_rollout_result(
            postgres_store,
            _mirrors(tmp_path),
            ORG,
            rollout_sink=lambda row: staged.append(row.session_id),
        )
    assert staged == ["session-a"]
