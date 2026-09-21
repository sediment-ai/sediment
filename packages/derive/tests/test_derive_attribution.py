# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Attribution-derivation tests over real git fixtures and a real FactStore —
never mocked (per AGENTS.md). Every scenario builds an actual repository with
subprocess git, mirrors it through MirrorManager, stores real facts, and runs
``derive_attributions`` as a consumer would.

The re-derivation test verifies that re-running the
derivation with a changed policy reproduces different, internally consistent
results over the SAME facts.

Fixture repos set a local user.name/user.email before committing: CI runners
have no global git identity.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from gitfixtures import CART, FIB, commit_all, make_remote, make_work_repo, run_git
from sediment_core import (
    FactTable,
    ForgeProvider,
    GatewayProvider,
    InferenceCall,
    InferenceMessage,
    Push,
    TextPart,
    ToolCallPart,
)
from sediment_core.store import FactStore
from sediment_derive import (
    AttributionSource,
    AttributionPolicy,
    MirrorManager,
    SimilarityPolicy,
    derive_attribution_result,
    derive_attributions,
)
from sediment_derive.attribution import _scope_candidates

ORG = "acme-corp"
REPO = "acme-corp/backend-service"

# Shares 6 of CART's 9 token types (jaccard 0.6): above the notes floor
# (0.3), below the org-wide jaccard threshold (0.7) — the "weaker but noted"
# candidate.
CART_PARTIAL = "class ShoppingCart:\n    def add_item(self, item):\n        pass\n"


def _note(*session_ids: str) -> str:
    return json.dumps(
        {
            "v": 1,
            "sessions": [
                {
                    "tool": "claude-code",
                    "session_id": s,
                    "stamped_at": "2026-07-13T00:00:00+00:00",
                }
                for s in session_ids
            ],
        }
    )


def _mirror_and_store(
    tmp_path: Path,
    store: FactStore,
    work: Path,
    before: str,
    after: str,
) -> tuple[FactStore, MirrorManager, Push]:
    """Push the work repo to a bare remote, mirror it, and store the Push
    fact — the state the derivation starts from after a real webhook."""
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
    return store, mirrors, push


def _inference_call(session_id: str, text: str) -> InferenceCall:
    return InferenceCall(
        org_id=ORG,
        session_id=session_id,
        user_id="dev",
        gateway_provider=GatewayProvider.LITELLM,
        model="claude-sonnet-5",
        input_messages=[
            InferenceMessage(role="user", parts=[TextPart(content="write it")])
        ],
        output_messages=[
            InferenceMessage(role="assistant", parts=[TextPart(content=text)])
        ],
        input_tokens=10,
        output_tokens=20,
        duration_ms=50,
        # Captured shortly BEFORE the push, as in real ingest order — the
        # lookback window is anchored on push.captured_at, and a completion
        # captured after the push can't have produced its commits.
        observed_at=datetime.now(UTC) - timedelta(minutes=5),
    )


def test_stamped_commit_correlates_via_notes_deterministically(
    tmp_path: Path,
    postgres_store,
) -> None:
    # A commit stamped with a session note attributes to that session's
    # completion with attribution_source="git_notes", and the derivation is
    # deterministic: two runs over the same facts yield identical results.
    work = make_work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head = commit_all(work, "add fibonacci")
    run_git(work, "notes", "--ref=sediment", "add", "-m", _note("sess-noted"), head)
    store, mirrors, _ = _mirror_and_store(
        tmp_path, postgres_store, work, "0" * 40, head
    )
    fib = _inference_call("sess-noted", FIB)
    store.store_inference_call(fib)
    store.store_inference_call(_inference_call("sess-other", CART))  # decoy

    first = derive_attributions(store, mirrors, ORG)
    assert len(first) == 1
    c = first[0]
    assert c.attribution_source == AttributionSource.GIT_NOTES
    assert (c.repo, c.commit_sha, c.file_path) == (REPO, head, "math_utils.py")
    assert c.session_id == "sess-noted"
    assert c.inference_call_id == fib.inference_call_id
    assert c.similarity_score == 1.0
    assert c.provenance.policy_version == "3"
    assert c.provenance.quarantine_revision == 0

    assert derive_attributions(store, mirrors, ORG) == first


def test_unstamped_commit_falls_back_to_jaccard(tmp_path: Path, postgres_store) -> None:
    work = make_work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head = commit_all(work, "add fibonacci")  # no note
    store, mirrors, _ = _mirror_and_store(
        tmp_path, postgres_store, work, "0" * 40, head
    )
    fib = _inference_call("sess-1", FIB)
    store.store_inference_call(fib)

    result = derive_attributions(store, mirrors, ORG)
    assert len(result) == 1
    c = result[0]
    assert c.attribution_source == AttributionSource.JACCARD
    assert c.inference_call_id == fib.inference_call_id
    assert c.session_id == "sess-1"  # the completion's real session (ADR 0002)
    assert c.similarity_score >= AttributionPolicy().jaccard.min_similarity


def test_structured_tool_call_correlates_without_persisted_scoring_text(
    tmp_path: Path,
    postgres_store,
) -> None:
    work = make_work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head = commit_all(work, "add fibonacci")
    store, mirrors, push = _mirror_and_store(
        tmp_path, postgres_store, work, "0" * 40, head
    )
    call = InferenceCall(
        inference_call_id="inference-structured",
        org_id=ORG,
        session_id="sess-structured",
        gateway_provider=GatewayProvider.LITELLM,
        model="claude-sonnet-5",
        input_messages=[],
        output_messages=[
            InferenceMessage(
                role="assistant",
                parts=[
                    ToolCallPart(
                        id="tool-structured",
                        name="Write",
                        arguments={"file_path": "math_utils.py", "content": FIB},
                    )
                ],
            )
        ],
        observed_at=push.captured_at - timedelta(minutes=5),
    )
    store.store_inference_call(call)

    first = derive_attributions(store, mirrors, ORG)

    assert len(first) == 1
    assert first[0].inference_call_id == call.inference_call_id
    assert first[0].session_id == call.session_id
    assert first[0].similarity_score >= AttributionPolicy().jaccard.min_similarity
    assert derive_attributions(store, mirrors, ORG) == first


def test_attribution_result_preserves_rows_and_counts_missing_mirror(
    tmp_path: Path,
    postgres_store,
) -> None:
    work = make_work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head = commit_all(work, "add fibonacci")
    store, mirrors, _ = _mirror_and_store(
        tmp_path, postgres_store, work, "0" * 40, head
    )
    store.store_inference_call(_inference_call("sess-1", FIB))

    present = derive_attribution_result(store, mirrors, ORG)
    assert present.attributions == derive_attributions(store, mirrors, ORG)
    assert present.skipped == {}

    absent = derive_attribution_result(
        store, MirrorManager(str(tmp_path / "empty-mirrors")), ORG
    )
    assert absent.attributions == []
    assert absent.skipped == {"mirror_absent": 1}


def test_notes_supersede_jaccard_for_same_commit_file(
    tmp_path: Path, postgres_store
) -> None:
    # The supersede invariant: the noted session's completion scores only 0.6
    # (below the org-wide jaccard threshold) while an un-noted session holds an EXACT
    # match (1.0) — the recorded fact still beats the stronger guess, and the
    # (commit, file) key yields exactly one attribution.
    work = make_work_repo(tmp_path)
    (work / "cart.py").write_text(CART)
    head = commit_all(work, "add cart")
    run_git(work, "notes", "--ref=sediment", "add", "-m", _note("sess-noted"), head)
    store, mirrors, _ = _mirror_and_store(
        tmp_path, postgres_store, work, "0" * 40, head
    )
    noted = _inference_call("sess-noted", CART_PARTIAL)
    store.store_inference_call(noted)
    store.store_inference_call(
        _inference_call("sess-other", CART)
    )  # exact, but un-noted

    result = derive_attributions(store, mirrors, ORG)
    assert len(result) == 1
    c = result[0]
    assert c.attribution_source == AttributionSource.GIT_NOTES
    assert c.session_id == "sess-noted"
    assert c.inference_call_id == noted.inference_call_id
    assert c.similarity_score == pytest.approx(0.6)


def test_observed_note_sessions_override_mutable_mirror_note(
    tmp_path: Path, postgres_store
) -> None:
    work = make_work_repo(tmp_path)
    (work / "cart.py").write_text(CART)
    head = commit_all(work, "add cart")
    run_git(work, "notes", "--ref=sediment", "add", "-m", _note("sess-later"), head)
    store, mirrors, _ = _mirror_and_store(
        tmp_path, postgres_store, work, "0" * 40, head
    )
    earlier = _inference_call("sess-earlier", CART_PARTIAL)
    store.store_inference_call(earlier)
    store.store_inference_call(_inference_call("sess-later", CART))

    result = derive_attribution_result(
        store,
        mirrors,
        ORG,
        note_sessions_by_commit={(REPO, head): frozenset({"sess-earlier"})},
    )

    assert len(result.attributions) == 1
    attribution = result.attributions[0]
    assert attribution.attribution_source == AttributionSource.GIT_NOTES
    assert attribution.session_id == "sess-earlier"
    assert attribution.inference_call_id == earlier.inference_call_id


@pytest.mark.parametrize(
    "bad_note",
    [
        "not json at all {{{",
        # privacy-contract violation: extra field on a session record
        json.dumps(
            {
                "v": 1,
                "sessions": [
                    {
                        "tool": "claude-code",
                        "session_id": "sess-noted",
                        "stamped_at": "t",
                        "prompt": "SECRET",
                    }
                ],
            }
        ),
        # unknown schema version
        json.dumps({"v": 2, "sessions": []}),
        # JSON booleans are not integer schema versions
        json.dumps(
            {
                "v": True,
                "sessions": [
                    {
                        "tool": "claude-code",
                        "session_id": "sess-1",
                        "stamped_at": "t",
                    }
                ],
            }
        ),
        # concatenated body where ONE payload is bad: the whole note drops
        "not json at all {{{\n\n" + json.dumps({"v": 1, "sessions": []}),
    ],
)
def test_malformed_note_fails_soft_to_jaccard(
    tmp_path: Path, postgres_store, bad_note: str
) -> None:
    # A malformed/hostile note must never crash the derivation — the commit
    # degrades to the jaccard fallback (skip + log).
    work = make_work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head = commit_all(work, "add fibonacci")
    run_git(work, "notes", "--ref=sediment", "add", "-m", bad_note, head)
    store, mirrors, _ = _mirror_and_store(
        tmp_path, postgres_store, work, "0" * 40, head
    )
    store.store_inference_call(_inference_call("sess-1", FIB))

    result = derive_attributions(store, mirrors, ORG)
    assert [c.attribution_source for c in result] == [AttributionSource.JACCARD]


def test_concatenated_note_from_rewrite_unions_sessions(
    tmp_path: Path, postgres_store
) -> None:
    # notes.rewriteMode defaults to concatenate, so with notes.rewriteRef set
    # (the stamper installer does) an amend/squash leaves TWO blank-line-
    # separated payloads in one note. The reader unions the sessions:
    # a session appearing only in the appended payload still gets notes
    # attribution.
    work = make_work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head = commit_all(work, "add fibonacci")
    concatenated = _note("sess-other") + "\n\n" + _note("sess-noted")
    run_git(work, "notes", "--ref=sediment", "add", "-m", concatenated, head)
    store, mirrors, _ = _mirror_and_store(
        tmp_path, postgres_store, work, "0" * 40, head
    )
    store.store_inference_call(_inference_call("sess-noted", FIB))

    result = derive_attributions(store, mirrors, ORG)
    assert [c.attribution_source for c in result] == [AttributionSource.GIT_NOTES]
    assert result[0].session_id == "sess-noted"


def test_rederivation_changed_policy_same_facts_consistent_results(
    tmp_path: Path,
    postgres_store,
) -> None:
    # Re-running the derivation with a
    # changed policy over the SAME facts reproduces different but internally
    # consistent results. One commit touches two files — an exact match (1.0)
    # and a partial match (0.6) — so the threshold decides how many attribute.
    work = make_work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    (work / "cart.py").write_text(CART_PARTIAL)
    head = commit_all(work, "add both")
    store, mirrors, _ = _mirror_and_store(
        tmp_path, postgres_store, work, "0" * 40, head
    )
    store.store_inference_call(_inference_call("sess-1", FIB))
    store.store_inference_call(_inference_call("sess-2", CART))  # 0.6 vs CART_PARTIAL

    strict = AttributionPolicy(
        jaccard=SimilarityPolicy(
            min_similarity=0.7,
            lookback_window_minutes=60,
        ),
        policy_version="strict",
    )
    loose = AttributionPolicy(
        jaccard=SimilarityPolicy(
            min_similarity=0.5,
            lookback_window_minutes=60,
        ),
        policy_version="loose",
    )

    strict_result = derive_attributions(store, mirrors, ORG, strict)
    loose_result = derive_attributions(store, mirrors, ORG, loose)

    # Different results from the same facts: the loose policy admits the
    # partial match the strict one rejects.
    assert {c.file_path for c in strict_result} == {"math_utils.py"}
    assert {c.file_path for c in loose_result} == {"math_utils.py", "cart.py"}
    # Internally consistent: every attribution clears its own policy's
    # threshold and carries that policy's version in its provenance.
    for result, policy in ((strict_result, strict), (loose_result, loose)):
        for c in result:
            assert c.similarity_score >= policy.jaccard.min_similarity
            assert c.provenance.policy_version == policy.policy_version
    # And each policy is reproducible: same facts, same policy, same output.
    assert derive_attributions(store, mirrors, ORG, strict) == strict_result
    assert derive_attributions(store, mirrors, ORG, loose) == loose_result


def test_quarantined_completion_never_correlates(
    tmp_path: Path, postgres_store
) -> None:
    # Quarantine folds into the derivation on the next run (ADR 0001): the
    # fact reads exclude the quarantined completion, and the provenance
    # stamp changes with the quarantine state.
    work = make_work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head = commit_all(work, "add fibonacci")
    run_git(work, "notes", "--ref=sediment", "add", "-m", _note("sess-noted"), head)
    store, mirrors, _ = _mirror_and_store(
        tmp_path, postgres_store, work, "0" * 40, head
    )
    fib = _inference_call("sess-noted", FIB)
    store.store_inference_call(fib)

    before = derive_attributions(store, mirrors, ORG)
    assert [c.inference_call_id for c in before] == [fib.inference_call_id]
    assert before[0].provenance.quarantine_revision == 0

    store.quarantine_fact(
        ORG,
        FactTable.INFERENCE_CALLS,
        fib.inference_call_id,
        reason="leaked ingest token",
    )
    # Neither notes attribution (its session is noted) nor the jaccard
    # fallback may see it now.
    assert derive_attributions(store, mirrors, ORG) == []


def test_multi_commit_push_respects_cap(tmp_path: Path, postgres_store) -> None:
    # Three commits pushed, cap of two: only the two NEWEST commits get a
    # attribution pass (the head is what attribution targets first).
    texts = {
        "alpha.py": "def alpha_metric(value):\n    return value * 3\n",
        "beta.py": (
            "class BetaQueue:\n"
            "    def enqueue(self, task):\n"
            "        self.tasks.append(task)\n"
        ),
        "gamma.py": "def gamma_hash(data):\n    seed = 17\n    return seed ^ 42\n",
    }
    work = make_work_repo(tmp_path)
    (work / "README.md").write_text("# repo\n")
    base = commit_all(work, "root")
    for name, text in texts.items():
        (work / name).write_text(text)
        commit_all(work, f"add {name}")
    head = run_git(work, "rev-parse", "HEAD").strip()
    store, mirrors, _ = _mirror_and_store(tmp_path, postgres_store, work, base, head)
    for name, text in texts.items():
        store.store_inference_call(_inference_call(f"sess-{name}", text))

    policy = AttributionPolicy(max_commits_per_push=2)
    result = derive_attributions(store, mirrors, ORG, policy)
    assert {c.file_path for c in result} == {"beta.py", "gamma.py"}


def test_scoped_pushes_derive_only_that_push(tmp_path: Path, postgres_store) -> None:
    # The trigger's shape: pushes=[push] bounds the walk to that push's
    # commits instead of the repo's whole (stored) history.
    work = make_work_repo(tmp_path)
    (work / "README.md").write_text("# repo\n")
    base = commit_all(work, "root")
    (work / "math_utils.py").write_text(FIB)
    first_head = commit_all(work, "add fibonacci")
    (work / "cart.py").write_text(CART)
    second_head = commit_all(work, "add cart")
    # The stored history covers both commits; the scope covers only the last.
    store, mirrors, _ = _mirror_and_store(
        tmp_path, postgres_store, work, base, second_head
    )
    second_push = Push(
        org_id=ORG,
        provider=ForgeProvider.GITHUB,
        repo=REPO,
        clone_url="unused",
        ref="refs/heads/main",
        before_sha=first_head,
        after_sha=second_head,
    )
    store.store_inference_call(_inference_call("sess-1", FIB))
    store.store_inference_call(_inference_call("sess-2", CART))

    full = derive_attributions(store, mirrors, ORG)
    assert {c.file_path for c in full} == {"math_utils.py", "cart.py"}
    scoped = derive_attributions(store, mirrors, ORG, pushes=[second_push])
    assert {(c.commit_sha, c.file_path) for c in scoped} == {(second_head, "cart.py")}


def test_equal_score_tie_resolves_by_fact_identity_not_insertion_order(
    tmp_path: Path,
    postgres_store,
    postgres_store_factory,
) -> None:
    # ADR 0001: re-ingesting the same facts in a different arrival order must
    # derive the same result. Two completions with identical captured_at and
    # identical text (equal score) are inserted in opposite orders into two
    # stores; both derivations must pick the same inference_call_id — the
    # smallest, never the first-inserted.
    work = make_work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head = commit_all(work, "add fibonacci")
    store_a, mirrors, _ = _mirror_and_store(
        tmp_path, postgres_store, work, "0" * 40, head
    )
    captured = datetime.now(UTC) - timedelta(minutes=5)
    twins = [
        _inference_call("sess-1", FIB).model_copy(
            update={"inference_call_id": f"{i}-twin", "captured_at": captured}
        )
        for i in ("aaa", "zzz")
    ]
    for c in twins:
        store_a.store_inference_call(c)
    _, store_b = postgres_store_factory()
    store_b.store_push(store_a.read_pushes(ORG)[0])
    for c in reversed(twins):
        store_b.store_inference_call(c)

    winner_a = [c.inference_call_id for c in derive_attributions(store_a, mirrors, ORG)]
    winner_b = [c.inference_call_id for c in derive_attributions(store_b, mirrors, ORG)]
    assert winner_a == winner_b == ["aaa-twin"]


def test_structured_equal_score_tie_ignores_insertion_order(
    tmp_path: Path, postgres_store, postgres_store_factory
) -> None:
    work = make_work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head = commit_all(work, "add fibonacci")
    store_a, mirrors, push = _mirror_and_store(
        tmp_path, postgres_store, work, "0" * 40, head
    )
    captured = push.captured_at - timedelta(minutes=5)
    twins = [
        InferenceCall(
            inference_call_id=f"{prefix}-structured-twin",
            org_id=ORG,
            session_id="sess-structured",
            gateway_provider=GatewayProvider.LITELLM,
            input_messages=[],
            output_messages=[
                InferenceMessage(
                    role="assistant",
                    parts=[
                        ToolCallPart(
                            id=f"tool-{prefix}",
                            name="Write",
                            arguments={"file_path": "math_utils.py", "content": FIB},
                        )
                    ],
                )
            ],
            observed_at=captured,
        )
        for prefix in ("aaa", "zzz")
    ]
    for call in twins:
        store_a.store_inference_call(call)
    _, store_b = postgres_store_factory()
    store_b.store_push(push)
    for call in reversed(twins):
        store_b.store_inference_call(call)

    winner_a = [c.inference_call_id for c in derive_attributions(store_a, mirrors, ORG)]
    winner_b = [c.inference_call_id for c in derive_attributions(store_b, mirrors, ORG)]

    assert winner_a == winner_b == ["aaa-structured-twin"]


def test_candidates_kwarg_matches_internal_scope(
    tmp_path: Path, postgres_store
) -> None:
    # A caller-supplied candidate set skips the internal read and tokenization
    # but yields the identical attributions as the default path — the same
    # facts produce identical output through either path.
    work = make_work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head = commit_all(work, "add fibonacci")
    store, mirrors, _ = _mirror_and_store(
        tmp_path, postgres_store, work, "0" * 40, head
    )
    store.store_inference_call(_inference_call("sess-1", FIB))
    store.store_inference_call(_inference_call("sess-2", CART))

    policy = AttributionPolicy()
    pushes = store.read_pushes(ORG)
    default = derive_attributions(store, mirrors, ORG, policy)
    with_candidates = derive_attributions(
        store,
        mirrors,
        ORG,
        policy,
        pushes=pushes,
        candidates=_scope_candidates(store, ORG, pushes, policy),
    )
    assert with_candidates == default
    assert default  # the fixture attributed at least one file


def test_candidates_kwarg_deterministic_across_ingest_order(
    tmp_path: Path, postgres_store, postgres_store_factory
) -> None:
    # ADR 0001 holds through the caller-supplied candidate path too: the same
    # facts ingested in a different order, each with its own candidate set,
    # derive identical attributions.
    work = make_work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head = commit_all(work, "add fibonacci")
    store_a, mirrors, _ = _mirror_and_store(
        tmp_path, postgres_store, work, "0" * 40, head
    )
    captured = datetime.now(UTC) - timedelta(minutes=5)
    twins = [
        _inference_call("sess-1", FIB).model_copy(
            update={"inference_call_id": f"{i}-twin", "observed_at": captured}
        )
        for i in ("aaa", "zzz")
    ]
    for c in twins:
        store_a.store_inference_call(c)
    _, store_b = postgres_store_factory()
    store_b.store_push(store_a.read_pushes(ORG)[0])
    for c in reversed(twins):
        store_b.store_inference_call(c)

    policy = AttributionPolicy()
    result_a = derive_attributions(
        store_a,
        mirrors,
        ORG,
        policy,
        candidates=_scope_candidates(store_a, ORG, store_a.read_pushes(ORG), policy),
    )
    result_b = derive_attributions(
        store_b,
        mirrors,
        ORG,
        policy,
        candidates=_scope_candidates(store_b, ORG, store_b.read_pushes(ORG), policy),
    )
    assert result_a == result_b
    assert [c.inference_call_id for c in result_a] == ["aaa-twin"]


def test_inference_call_observed_just_after_push_still_correlates(
    tmp_path: Path,
    postgres_store,
) -> None:
    # captured_at is ingest time: an agent that generates, auto-commits, and
    # pushes within one OTLP export interval delivers the completion AFTER
    # the push webhook. Capture slack keeps it correlatable; past the slack
    # it stays out.
    work = make_work_repo(tmp_path)
    (work / "math_utils.py").write_text(FIB)
    head = commit_all(work, "add fibonacci")
    run_git(work, "notes", "--ref=sediment", "add", "-m", _note("sess-noted"), head)
    store, mirrors, push = _mirror_and_store(
        tmp_path, postgres_store, work, "0" * 40, head
    )
    late = _inference_call("sess-noted", FIB).model_copy(
        update={"observed_at": push.captured_at + timedelta(minutes=5)}
    )
    store.store_inference_call(late)

    result = derive_attributions(store, mirrors, ORG)
    assert [c.attribution_source for c in result] == [AttributionSource.GIT_NOTES]

    too_late = AttributionPolicy(post_push_grace_period_minutes=1)
    assert derive_attributions(store, mirrors, ORG, too_late) == []


def test_non_utf8_diff_and_note_never_crash_the_derivation(
    tmp_path: Path, postgres_store
) -> None:
    # Git objects are bytes: a latin-1 source file and a binary note blob
    # must degrade (replacement chars → jaccard fallback / no note), never
    # raise out of the derivation.
    work = make_work_repo(tmp_path)
    (work / "legacy.py").write_bytes(b"x = 1  # caf\xe9\n")
    head = commit_all(work, "add legacy latin-1 file")
    note_blob = tmp_path / "note.bin"
    note_blob.write_bytes(b'{"v": 1, \xff\xfe garbage')
    run_git(work, "notes", "--ref=sediment", "add", "-F", str(note_blob), head)
    store, mirrors, _ = _mirror_and_store(
        tmp_path, postgres_store, work, "0" * 40, head
    )
    store.store_inference_call(_inference_call("sess-1", "x = 1  # caf\n"))

    result = derive_attributions(store, mirrors, ORG)  # must not raise
    assert [c.attribution_source for c in result] == [AttributionSource.JACCARD]
    assert result[0].file_path == "legacy.py"


def test_non_code_files_are_skipped(tmp_path: Path, postgres_store) -> None:
    # Docs and lockfiles never attribute, even on an exact text match.
    work = make_work_repo(tmp_path)
    readme = "Sediment turns workflow traces into training data.\n"
    (work / "README.md").write_text(readme)
    (work / "math_utils.py").write_text(FIB)
    head = commit_all(work, "add readme and code")
    store, mirrors, _ = _mirror_and_store(
        tmp_path, postgres_store, work, "0" * 40, head
    )
    store.store_inference_call(
        _inference_call("sess-doc", readme)
    )  # exact README match
    store.store_inference_call(_inference_call("sess-code", FIB))

    result = derive_attributions(store, mirrors, ORG)
    assert [c.file_path for c in result] == ["math_utils.py"]


def test_attribution_uses_quoted_file_additions_and_counts_section_loss_once(
    tmp_path: Path, postgres_store_factory
) -> None:
    work = make_work_repo(tmp_path)
    (work / "a_plain.js").write_text("function unrelated() { return other(); }\n")
    path = 'quoted"file.js'
    authored = "++counter;\n"
    (work / path).write_text(authored)
    (work / "binary.js").write_bytes(b"binary\x00text")
    head = commit_all(work, "quoted additions")
    _, first_store = postgres_store_factory()
    _, shuffled_store = postgres_store_factory()
    _, mirrors, push = _mirror_and_store(tmp_path, first_store, work, "0" * 40, head)
    call = _inference_call("counter-session", authored)
    decoy = _inference_call("unrelated-session", "another different completion")
    for fact in [call, decoy]:
        first_store.store_inference_call(fact)
    for fact in [decoy, call]:
        shuffled_store.store_inference_call(fact)
    shuffled_store.store_push(push)

    result = derive_attribution_result(first_store, mirrors, ORG)

    assert [
        (row.file_path, row.inference_call_id, row.similarity_score)
        for row in result.attributions
    ] == [(path, call.inference_call_id, 1.0)]
    assert result.skipped == {"no_match": 1, "unsupported_diff_section": 1}
    assert result.attributions[0].provenance.policy_version == "3"
    assert derive_attribution_result(first_store, mirrors, ORG) == result
    assert derive_attribution_result(shuffled_store, mirrors, ORG) == result
