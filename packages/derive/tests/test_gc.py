# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Mirror GC tests — real bare mirrors and a real ``FactStore``, never
mocked (per AGENTS.md). ``gc_mirrors`` is not an ADR-0001 derivation (it
deletes directories, a filesystem side effect), but ``now`` is still
caller-supplied so every scenario here is deterministic.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from gitfixtures import FIB, commit_all, make_remote, make_work_repo
from sediment_core import FactTable, ForgeProvider, Push
from sediment_core.store import FactStore
from sediment_derive import MirrorManager
from sediment_derive.gc import GCAction, MirrorGCPolicy, gc_mirrors

ORG = "acme-corp"
NOW = datetime(2026, 8, 1, tzinfo=UTC)


@pytest.fixture
def store(postgres_store) -> Iterator[FactStore]:
    yield postgres_store


def _mirrored_repo(
    tmp_path: Path, manager: MirrorManager, name: str, *, org_id: str = ORG
) -> str:
    root = tmp_path / name
    root.mkdir()
    work = make_work_repo(root)
    (work / "a.py").write_text(FIB)
    head = commit_all(work, "init")
    remote = make_remote(root, work)
    repo = f"{org_id}/{name}"
    manager.ensure(
        Push(
            org_id=org_id,
            provider=ForgeProvider.GITHUB,
            repo=repo,
            clone_url=str(remote),
            ref="refs/heads/main",
            before_sha="0" * 40,
            after_sha=head,
        )
    )
    return repo


def _store_push(
    store: FactStore, *, org_id: str, repo: str, captured_at: datetime
) -> str:
    push = Push(
        org_id=org_id,
        provider=ForgeProvider.GITHUB,
        repo=repo,
        clone_url="file:///dev/null",
        ref="refs/heads/main",
        before_sha="0" * 40,
        after_sha="a" * 40,
        captured_at=captured_at,
    )
    store.store_push(push)
    return push.push_id


def test_gc_keeps_repo_with_recent_push(tmp_path: Path, store: FactStore) -> None:
    manager = MirrorManager(str(tmp_path / "mirrors"))
    repo = _mirrored_repo(tmp_path, manager, "fresh")
    _store_push(store, org_id=ORG, repo=repo, captured_at=NOW - timedelta(days=1))

    results = gc_mirrors(store, manager, ORG, now=NOW)
    assert len(results) == 1
    assert results[0].action == GCAction.KEPT
    assert results[0].skip_reason == "within_retention"
    assert manager.open(ORG, repo) is not None


def test_gc_removes_repo_past_retention_only_when_not_dry_run(
    tmp_path: Path, store: FactStore
) -> None:
    manager = MirrorManager(str(tmp_path / "mirrors"))
    repo = _mirrored_repo(tmp_path, manager, "stale")
    _store_push(store, org_id=ORG, repo=repo, captured_at=NOW - timedelta(days=200))

    dry_run_results = gc_mirrors(store, manager, ORG, now=NOW)  # dry_run=True default
    assert dry_run_results[0].action == GCAction.REMOVED
    assert manager.open(ORG, repo) is not None  # nothing actually deleted

    applied_results = gc_mirrors(store, manager, ORG, now=NOW, dry_run=False)
    assert applied_results[0].action == GCAction.REMOVED
    assert applied_results[0].skip_reason is None
    assert manager.open(ORG, repo) is None


def test_gc_default_retention_is_ninety_days(tmp_path: Path, store: FactStore) -> None:
    manager = MirrorManager(str(tmp_path / "mirrors"))
    repo = _mirrored_repo(tmp_path, manager, "boundary")
    _store_push(store, org_id=ORG, repo=repo, captured_at=NOW - timedelta(days=91))
    results = gc_mirrors(store, manager, ORG, now=NOW, dry_run=False)
    assert results[0].action == GCAction.REMOVED


def test_gc_custom_retention_policy(tmp_path: Path, store: FactStore) -> None:
    manager = MirrorManager(str(tmp_path / "mirrors"))
    repo = _mirrored_repo(tmp_path, manager, "recent-ish")
    _store_push(store, org_id=ORG, repo=repo, captured_at=NOW - timedelta(days=10))
    results = gc_mirrors(store, manager, ORG, MirrorGCPolicy(retention_days=5), now=NOW)
    assert results[0].action == GCAction.REMOVED


def test_gc_repo_with_no_push_facts_is_quarantine_ambiguous_and_kept(
    tmp_path: Path, store: FactStore
) -> None:
    # A mirror can exist with zero Push facts in the default read — pre-seeded
    # out of band, or (below) fully quarantined. Either way GC must not guess.
    manager = MirrorManager(str(tmp_path / "mirrors"))
    repo = _mirrored_repo(tmp_path, manager, "orphan")

    results = gc_mirrors(store, manager, ORG, now=NOW, dry_run=False)
    assert results[0].action == GCAction.KEPT
    assert results[0].skip_reason == "quarantine_ambiguous"
    assert results[0].last_push_captured_at is None
    assert manager.open(ORG, repo) is not None


def test_gc_fully_quarantined_push_history_is_quarantine_ambiguous_not_removed(
    tmp_path: Path, store: FactStore
) -> None:
    # A repo whose only Push fact is
    # quarantined reads as zero-push through the default path, same as a true
    # orphan. GC must never reach for include_quarantined=True to tell them
    # apart — it stays kept either way.
    manager = MirrorManager(str(tmp_path / "mirrors"))
    repo = _mirrored_repo(tmp_path, manager, "quarantined")
    push_id = _store_push(
        store, org_id=ORG, repo=repo, captured_at=NOW - timedelta(days=200)
    )
    store.quarantine_fact(
        ORG, FactTable.PUSHES, push_id, reason="incident response test"
    )

    results = gc_mirrors(store, manager, ORG, now=NOW, dry_run=False)
    assert results[0].action == GCAction.KEPT
    assert results[0].skip_reason == "quarantine_ambiguous"
    assert manager.open(ORG, repo) is not None


def test_gc_only_touches_named_org(tmp_path: Path, store: FactStore) -> None:
    manager = MirrorManager(str(tmp_path / "mirrors"))
    acme_repo = _mirrored_repo(tmp_path, manager, "svc-a", org_id="acme-corp")
    other_repo = _mirrored_repo(tmp_path, manager, "svc-b", org_id="other-org")
    _store_push(
        store,
        org_id="other-org",
        repo=other_repo,
        captured_at=NOW - timedelta(days=200),
    )

    results = gc_mirrors(store, manager, "acme-corp", now=NOW, dry_run=False)
    assert [r.repo for r in results] == [acme_repo]
    assert manager.open("other-org", other_repo) is not None  # untouched


def test_gc_results_ordered_by_repo_name(tmp_path: Path, store: FactStore) -> None:
    manager = MirrorManager(str(tmp_path / "mirrors"))
    repo_z = _mirrored_repo(tmp_path, manager, "z-service")
    repo_a = _mirrored_repo(tmp_path, manager, "a-service")
    for repo in (repo_z, repo_a):
        _store_push(store, org_id=ORG, repo=repo, captured_at=NOW - timedelta(days=1))

    results = gc_mirrors(store, manager, ORG, now=NOW)
    assert [r.repo for r in results] == sorted([repo_z, repo_a])


def test_gc_reports_remove_failed_when_remove_returns_false(
    tmp_path: Path, store: FactStore
) -> None:
    # A concurrent rename/remove can clear the directory first, so
    # remove() returns False. That must not be misreported as REMOVED.
    manager = MirrorManager(str(tmp_path / "mirrors"))
    repo = _mirrored_repo(tmp_path, manager, "raced")
    _store_push(store, org_id=ORG, repo=repo, captured_at=NOW - timedelta(days=200))
    original_remove = manager.remove
    calls: list[str] = []

    def _fake_remove(org_id: str, target_repo: str) -> bool:
        calls.append(target_repo)
        original_remove(org_id, target_repo)  # actually clear it, like a racer would
        return False

    manager.remove = _fake_remove  # type: ignore[method-assign]

    results = gc_mirrors(store, manager, ORG, now=NOW, dry_run=False)

    assert calls == [repo]
    assert results[0].action == GCAction.KEPT
    assert results[0].skip_reason == "remove_failed"


def test_gc_remove_oserror_is_caught_and_run_continues(
    tmp_path: Path, store: FactStore
) -> None:
    # An OSError from shutil.rmtree (permissions, dir swapped for a
    # file) must not abort the loop mid-run -- the remaining repos still
    # get processed, and the failure is reported rather than raised.
    manager = MirrorManager(str(tmp_path / "mirrors"))
    repo_broken = _mirrored_repo(tmp_path, manager, "broken")
    repo_ok = _mirrored_repo(tmp_path, manager, "ok")
    for repo in (repo_broken, repo_ok):
        _store_push(store, org_id=ORG, repo=repo, captured_at=NOW - timedelta(days=200))
    original_remove = manager.remove

    def _flaky_remove(org_id: str, target_repo: str) -> bool:
        if target_repo == repo_broken:
            raise OSError("permission denied")
        return original_remove(org_id, target_repo)

    manager.remove = _flaky_remove  # type: ignore[method-assign]

    results = gc_mirrors(store, manager, ORG, now=NOW, dry_run=False)

    by_repo = {r.repo: r for r in results}
    assert by_repo[repo_broken].action == GCAction.KEPT
    assert by_repo[repo_broken].skip_reason == "remove_failed"
    assert manager.open(ORG, repo_broken) is not None  # untouched
    assert by_repo[repo_ok].action == GCAction.REMOVED
    assert by_repo[repo_ok].skip_reason is None
    assert manager.open(ORG, repo_ok) is None  # the other repo still ran


def test_policy_rejects_zero_and_negative_retention() -> None:
    # The destructive version of the config typo list_push_commits clamps:
    # retention <= 0 flips the cutoff into the future and would mark every
    # mirror with push history for removal.
    for bad in (0, -90):
        with pytest.raises(ValueError, match="retention_days must be >= 1"):
            MirrorGCPolicy(retention_days=bad)
    assert MirrorGCPolicy(retention_days=1).retention_days == 1
