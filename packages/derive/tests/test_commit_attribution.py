# SPDX-License-Identifier: AGPL-3.0-or-later
"""Target Attribution preserves the complete Derivation over real Git and Facts."""

from datetime import UTC, datetime, timedelta

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
)
from sediment_derive import (
    AttributionPolicy,
    CommitKey,
    MirrorManager,
    derive_attributions,
)
from sediment_derive import attribution
from sediment_derive.mirror import RepoMirror
from sediment_derive.repository_context import read_repository_context
from sediment_derive.repository_identity import (
    build_repository_context,
    repository_identity_of,
)

ORG = "commit-target"
REPO = "commit-target/service"
T0 = datetime(2026, 9, 1, 12, tzinfo=UTC)


def _call(identifier, text, at=T0, session="session"):
    return InferenceCall(
        inference_call_id=identifier,
        org_id=ORG,
        session_id=session,
        gateway_provider=GatewayProvider.LITELLM,
        input_messages=[],
        output_messages=[
            InferenceMessage(role="assistant", parts=[TextPart(content=text)])
        ],
        observed_at=at,
    )


def _history(tmp_path, store, *, older_commit=False):
    work = make_work_repo(tmp_path)
    (work / "README.md").write_text("root\n")
    base = commit_all(work, "root")
    if older_commit:
        (work / "README.md").write_text("earlier history\n")
        base = commit_all(work, "earlier")
    (work / "fib.py").write_text(FIB)
    target = commit_all(work, "target")
    (work / "cart.py").write_text(CART)
    later = commit_all(work, "later")
    remote = make_remote(tmp_path, work)
    first = Push(
        push_id="first",
        org_id=ORG,
        provider=ForgeProvider.GITHUB,
        repo=REPO,
        clone_url=str(remote),
        ref="refs/heads/main",
        before_sha=base,
        after_sha=target,
        captured_at=T0,
    )
    second = first.model_copy(
        update={
            "push_id": "second",
            "before_sha": target,
            "after_sha": later,
            "captured_at": T0 + timedelta(days=60),
        }
    )
    store.store_push(first)
    store.store_push(second)
    mirrors = MirrorManager(str(tmp_path / "mirrors"))
    mirrors.ensure(first)
    return mirrors, first, second, target


@pytest.mark.parametrize("count", [255, 256, 257, 1000])
def test_old_push_batches_preserve_owner_without_per_push_git(
    tmp_path, postgres_store, monkeypatch, count
):
    from sediment_derive import mirror as mirror_module

    mirrors, first, _, target = _history(tmp_path, postgres_store, older_commit=True)
    mirror = mirrors.open(ORG, REPO)
    before = run_git(mirror.path, "rev-parse", f"{first.before_sha}^").strip()
    for index in reversed(range(count)):
        postgres_store.store_push(
            first.model_copy(
                update={
                    "push_id": f"history-{index:04}",
                    "ref": f"refs/heads/history-{index:04}",
                    "before_sha": before,
                    "after_sha": first.before_sha,
                    "captured_at": T0 - timedelta(days=1, seconds=count - index),
                }
            )
        )
    postgres_store.store_inference_call(_call("target-call", FIB))
    context = read_repository_context(postgres_store, ORG, as_of=T0)
    expected = [
        row
        for row in derive_attributions(
            postgres_store,
            mirrors,
            ORG,
            repository_context=context,
            note_session_ids_by_commit={},
        )
        if row.commit_sha == target
    ]
    commands = []
    original = mirror_module._run_git

    def recording_git(path, *args):
        commands.append(args)
        return original(path, *args)

    monkeypatch.setattr(mirror_module, "_run_git", recording_git)
    actual = attribution.derive_commit_attributions(
        postgres_store,
        mirrors,
        ORG,
        target,
        repository_context=context,
        note_session_ids_by_commit={},
    )
    assert actual == expected
    assert [row.source_push_id for row in actual] == [first.push_id]
    assert not any(".." in argument for command in commands for argument in command)
    # One target existence check, one rejection per batch, and two diff reads.
    assert len(commands) == (count + 255) // 256 + 3


def test_target_matches_full_history_and_reads_only_target_diff(
    tmp_path, postgres_store, monkeypatch
):
    mirrors, first, second, target = _history(tmp_path, postgres_store)
    postgres_store.store_inference_call(_call("target-call", FIB))
    postgres_store.store_inference_call(
        _call("unrelated-call", CART, second.captured_at)
    )
    context = read_repository_context(postgres_store, ORG, as_of=second.captured_at)
    expected = [
        row
        for row in derive_attributions(
            postgres_store,
            mirrors,
            ORG,
            repository_context=context,
            note_session_ids_by_commit={},
        )
        if row.commit_sha == target
    ]
    assert len(expected) == 1
    diff_reads = []
    original = RepoMirror.fetch_commit_diff

    def recording_diff(self, repo, sha):
        diff_reads.append(sha)
        return original(self, repo, sha)

    monkeypatch.setattr(RepoMirror, "fetch_commit_diff", recording_diff)
    actual = attribution.derive_commit_attributions(
        postgres_store,
        mirrors,
        ORG,
        target,
        repository_context=context,
        note_session_ids_by_commit={},
    )
    assert actual == expected
    assert diff_reads == [target]


def _target_and_oracle(
    store, mirrors, target, *, boundary, policy=None, notes=None, key=None
):
    context = read_repository_context(store, ORG, as_of=boundary)
    note_map = notes or {}
    expected = [
        row
        for row in derive_attributions(
            store,
            mirrors,
            ORG,
            policy,
            repository_context=context,
            note_session_ids_by_commit=note_map,
        )
        if row.commit_sha == target
        and (
            key is None
            or context.resolve_reference(
                row.org_id, row.repo, repository_identity=row.repository_identity
            ).key
            == key
        )
    ]
    actual = attribution.derive_commit_attributions(
        store,
        mirrors,
        ORG,
        target,
        policy,
        repository_context=context,
        note_session_ids_by_commit=note_map,
        repository_key=key,
    )
    assert actual == expected
    return actual


@pytest.mark.parametrize("visibility", ["visible", "quarantined", "released"])
def test_earliest_owner_precedes_candidate_availability(
    tmp_path, postgres_store, visibility
):
    mirrors, first, second, target = _history(tmp_path, postgres_store)
    later = first.model_copy(
        update={
            "push_id": "overlapping",
            "after_sha": second.after_sha,
            "captured_at": T0 + timedelta(days=2),
            "ref": "refs/heads/overlap",
        }
    )
    postgres_store.store_push(later)
    postgres_store.store_inference_call(_call("later-match", FIB, later.captured_at))
    if visibility != "visible":
        postgres_store.quarantine_fact(
            ORG, FactTable.PUSHES, first.push_id, reason="test"
        )
    if visibility == "released":
        postgres_store.release_fact(ORG, FactTable.PUSHES, first.push_id, reason="test")
    rows = _target_and_oracle(
        postgres_store, mirrors, target, boundary=second.captured_at
    )
    assert [row.source_push_id for row in rows] == (
        [later.push_id] if visibility == "quarantined" else []
    )


@pytest.mark.parametrize(
    "mode,cap,expected",
    [
        ("normal", 2, True),
        ("normal", 1, False),
        ("forced", 2, False),
        ("created", 2, False),
        ("unknown", 2, False),
        ("empty", 2, False),
    ],
)
def test_capped_non_head_membership_and_fallbacks(
    tmp_path, postgres_store, mode, cap, expected
):
    mirrors, first, second, target = _history(tmp_path, postgres_store)
    postgres_store.quarantine_fact(ORG, FactTable.PUSHES, first.push_id, reason="test")
    before = {"created": "0" * 40, "unknown": "f" * 40, "empty": second.after_sha}.get(
        mode, first.before_sha
    )
    owner = first.model_copy(
        update={
            "push_id": "range",
            "ref": "refs/heads/range",
            "before_sha": before,
            "after_sha": second.after_sha,
            "forced": mode == "forced",
        }
    )
    postgres_store.store_push(owner)
    postgres_store.store_inference_call(_call("match", FIB))
    rows = _target_and_oracle(
        postgres_store,
        mirrors,
        target,
        boundary=T0,
        policy=AttributionPolicy(max_commits_per_push=cap),
    )
    assert bool(rows) == expected


@pytest.mark.parametrize("mode", ["forced", "created", "unknown", "empty"])
def test_head_fallback_ownership(tmp_path, postgres_store, mode):
    mirrors, first, second, target = _history(tmp_path, postgres_store)
    postgres_store.quarantine_fact(ORG, FactTable.PUSHES, first.push_id, reason="test")
    before = {"created": "0" * 40, "unknown": "f" * 40, "empty": target}.get(
        mode, first.before_sha
    )
    owner = first.model_copy(
        update={
            "push_id": "fallback",
            "ref": "refs/heads/fallback",
            "before_sha": before,
            "forced": mode == "forced",
        }
    )
    postgres_store.store_push(owner)
    postgres_store.store_inference_call(_call("match", FIB))
    rows = _target_and_oracle(postgres_store, mirrors, target, boundary=T0)
    assert rows[0].source_push_id == owner.push_id


@pytest.mark.parametrize("early_match", [False, True])
def test_failed_batch_proof_preserves_non_head_owner_before_direct_head(
    tmp_path, postgres_store, early_match, caplog
):
    mirrors, first, second, target = _history(tmp_path, postgres_store)
    postgres_store.quarantine_fact(ORG, FactTable.PUSHES, first.push_id, reason="test")
    missing = first.model_copy(
        update={
            "push_id": "missing-head",
            "ref": "refs/heads/missing",
            "after_sha": "f" * 40,
            "captured_at": T0 - timedelta(days=1),
        }
    )
    owner = first.model_copy(
        update={
            "push_id": "non-head-owner",
            "ref": "refs/heads/range",
            "after_sha": second.after_sha,
        }
    )
    later = first.model_copy(
        update={
            "push_id": "later-head",
            "ref": "refs/heads/later",
            "captured_at": T0 + timedelta(days=2),
        }
    )
    for push in (later, owner, missing):
        postgres_store.store_push(push)
    if early_match:
        postgres_store.store_inference_call(_call("early-match", FIB))
    postgres_store.store_inference_call(_call("late-match", FIB, later.captured_at))
    rows = _target_and_oracle(
        postgres_store, mirrors, target, boundary=later.captured_at
    )
    assert [row.source_push_id for row in rows] == (
        [owner.push_id] if early_match else []
    )
    assert "commit_owner_prefilter_unavailable" in caplog.text


@pytest.mark.parametrize("earlier_id", ["a-first", "Z-first"])
def test_equal_time_pushes_and_shuffled_ingestion(
    tmp_path, postgres_store, postgres_store_factory, earlier_id
):
    mirrors, first, second, target = _history(tmp_path, postgres_store)
    earlier = first.model_copy(update={"push_id": earlier_id, "ref": "refs/heads/tie"})
    postgres_store.store_push(earlier)
    calls = [_call("a-call", FIB), _call("Z-call", FIB)]
    for call in calls:
        postgres_store.store_inference_call(call)
    expected = _target_and_oracle(postgres_store, mirrors, target, boundary=T0)
    assert expected[0].source_push_id == earlier_id
    assert expected[0].inference_call_id == "Z-call"
    _, shuffled = postgres_store_factory()
    for push in (earlier, second, first):
        shuffled.store_push(push)
    for call in reversed(calls):
        shuffled.store_inference_call(call)
    assert _target_and_oracle(shuffled, mirrors, target, boundary=T0) == expected
    assert (
        _target_and_oracle(
            shuffled, mirrors, target, boundary=T0 - timedelta(microseconds=1)
        )
        == []
    )


def test_note_population_and_multiple_disjoint_owner_windows(
    tmp_path, postgres_store, monkeypatch
):
    mirrors, first, second, target = _history(tmp_path, postgres_store)
    twin = first.model_copy(
        update={
            "push_id": "twin",
            "repo": "commit-target/twin",
            "captured_at": second.captured_at,
        }
    )
    postgres_store.store_push(twin)
    mirrors.ensure(twin)
    postgres_store.store_inference_call(
        _call("early-noted", FIB, T0 - timedelta(days=3), "early-session")
    )
    postgres_store.store_inference_call(
        _call("late-noted", FIB, twin.captured_at - timedelta(days=3), "late-session")
    )
    postgres_store.store_inference_call(
        _call("gap", CART, T0 + timedelta(days=30), "gap-session")
    )
    context = read_repository_context(postgres_store, ORG, as_of=twin.captured_at)
    key = context.resolve_reference(
        ORG, first.repo, repository_identity=repository_identity_of(first)
    ).key
    twin_key = context.resolve_reference(
        ORG, twin.repo, repository_identity=repository_identity_of(twin)
    ).key
    notes = {
        CommitKey(key, target): frozenset({"early-session"}),
        CommitKey(twin_key, target): frozenset({"late-session"}),
    }
    rows = _target_and_oracle(
        postgres_store, mirrors, target, boundary=twin.captured_at, notes=notes
    )
    assert {row.inference_call_id for row in rows} == {"early-noted", "late-noted"}
    assert (
        len(
            _target_and_oracle(
                postgres_store,
                mirrors,
                target,
                boundary=twin.captured_at,
                notes=notes,
                key=key,
            )
        )
        == 1
    )
    from sediment_core import store as store_module

    original = store_module._FactSnapshot.read_attribution_candidates
    populations = []

    def recording_candidates(self, *args, **kwargs):
        found = original(self, *args, **kwargs)
        populations.append({row.inference_call_id for row in found})
        return found

    monkeypatch.setattr(
        store_module._FactSnapshot, "read_attribution_candidates", recording_candidates
    )
    assert (
        attribution.derive_commit_attributions(
            postgres_store,
            mirrors,
            ORG,
            target,
            repository_context=context,
            note_session_ids_by_commit=notes,
        )
        == rows
    )
    assert populations == [{"early-noted"}, {"late-noted"}]


def test_missing_mirror_and_unknown_commit_avoid_candidate_content(
    tmp_path, postgres_store, monkeypatch
):
    mirrors, first, second, target = _history(tmp_path, postgres_store)
    context = read_repository_context(postgres_store, ORG, as_of=T0)
    from sediment_core import store as store_module

    def forbidden(*args, **kwargs):
        raise AssertionError("candidate content must follow an owner")

    monkeypatch.setattr(
        store_module._FactSnapshot, "read_attribution_candidates", forbidden
    )
    for manager, sha in (
        (MirrorManager(str(tmp_path / "missing")), target),
        (mirrors, "f" * 40),
        # The mirror contains the later head, but its Push is beyond as_of.
        (mirrors, second.after_sha),
    ):
        assert (
            attribution.derive_commit_attributions(
                postgres_store,
                manager,
                ORG,
                sha,
                repository_context=context,
                note_session_ids_by_commit={},
            )
            == []
        )


def test_stored_identified_owners_work_with_compact_source_representatives(
    tmp_path, postgres_store
):
    mirrors, first, second, target = _history(tmp_path, postgres_store)
    identified = first.model_copy(
        update={
            "push_id": "identified-owner",
            "repository_provider": ForgeProvider.GITHUB,
            "repository_host": "github.com",
            "repository_id": "123",
            "ref": "refs/heads/identified",
        }
    )
    representative = identified.model_copy(
        update={
            "push_id": "representative",
            "ref": "refs/heads/representative",
            "captured_at": T0 + timedelta(days=1),
        }
    )
    renamed = identified.model_copy(
        update={
            "push_id": "renamed",
            "ref": "refs/heads/renamed",
            "repo": "commit-target/renamed",
            "captured_at": T0 + timedelta(days=2),
        }
    )
    fork = identified.model_copy(
        update={
            "push_id": "fork",
            "repo": "commit-target/fork",
            "repository_id": "999",
            "ref": "refs/heads/fork",
            "captured_at": T0 + timedelta(days=3),
        }
    )
    for push in (identified, representative, renamed, fork):
        postgres_store.store_push(push)
        mirrors.ensure(
            push,
            repository_context=read_repository_context(
                postgres_store, ORG, as_of=push.captured_at
            ),
        )
    postgres_store.store_inference_call(_call("owner-call", FIB))
    postgres_store.store_inference_call(_call("fork-call", FIB, fork.captured_at))
    boundary = fork.captured_at
    expected = _target_and_oracle(postgres_store, mirrors, target, boundary=boundary)
    assert {row.source_push_id for row in expected} == {"identified-owner", "fork"}
    assert {row.repository_identity.repository_id for row in expected} == {"123", "999"}
    assert (
        next(row.repo for row in expected if row.source_push_id == "identified-owner")
        == "commit-target/renamed"
    )
    with postgres_store.read_snapshot() as snapshot:
        evidence = snapshot.read_repository_identities(ORG, captured_through=boundary)
        compact = build_repository_context(
            [row for row in evidence if row.source_fact_id != identified.push_id],
            (),
            ORG,
            as_of=boundary,
        )
        assert compact.resolve_fact(identified).key is None
        assert (
            attribution.derive_commit_attributions(
                snapshot,
                mirrors,
                ORG,
                target,
                repository_context=compact,
                note_session_ids_by_commit={},
            )
            == expected
        )
