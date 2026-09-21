# SPDX-License-Identifier: AGPL-3.0-or-later
"""Repository lifetime isolation at the real Git substrate boundary."""

from datetime import UTC, datetime, timedelta
import json
import select
import subprocess
import sys
import threading

import pytest
from gitfixtures import commit_all, make_remote, make_work_repo, run_git
from sediment_core import FactTable, ForgeProvider, Push, RepositoryIdentityEvidence
from sediment_derive.mirror import MirrorManager, MirrorPolicyError
from sediment_derive.gc import GCAction, gc_mirrors
from sediment_derive.repository_identity import (
    build_repository_context,
)

ORG = "acme-corp"
T0 = datetime(2026, 9, 1, tzinfo=UTC)


@pytest.fixture
def remote(tmp_path):
    work = make_work_repo(tmp_path)
    (work / "source.py").write_text("value = 1\n")
    head = commit_all(work, "initial")
    return make_remote(tmp_path, work), head


def _push(remote, *, repo="acme-corp/alpha", repository_id="101", **kwargs):
    path, head = remote
    return Push(
        org_id=ORG,
        provider=ForgeProvider.GITHUB,
        repository_provider=ForgeProvider.GITHUB if repository_id else None,
        repository_host="github.com" if repository_id else None,
        repository_id=repository_id,
        repo=repo,
        clone_url=str(path),
        ref="refs/heads/main",
        before_sha="0" * 40,
        after_sha=head,
        captured_at=T0,
        **kwargs,
    )


def _context(*pushes):
    return build_repository_context(
        [
            RepositoryIdentityEvidence(
                source_table=FactTable.PUSHES,
                source_fact_id=p.push_id,
                role="repo",
                org_id=p.org_id,
                repo=p.repo,
                repository_provider=p.repository_provider,
                repository_host=p.repository_host,
                repository_id=p.repository_id,
                captured_at=p.captured_at,
            )
            for p in pushes
        ],
        [],
        ORG,
        as_of=T0 + timedelta(days=1),
    )


def test_identified_push_requires_evidence_before_filesystem_changes(tmp_path, remote):
    manager = MirrorManager(str(tmp_path / "mirrors"))
    with pytest.raises(MirrorPolicyError, match="repository_source_absent"):
        manager.ensure(_push(remote))
    assert not manager.base.exists()


def test_rename_keeps_stable_mirror_and_legacy_namespace_separate(tmp_path, remote):
    first = _push(remote)
    renamed = _push(remote, repo="acme-corp/beta")
    context = _context(first, renamed)
    manager = MirrorManager(str(tmp_path / "mirrors"))
    original = manager.ensure(first, repository_context=context)
    after = manager.ensure(renamed, repository_context=context)
    key = context.resolve_fact(first).key
    assert original.path == after.path
    assert manager.open_repository(key).refs() == original.refs()
    assert manager.open(ORG, first.repo) is None
    assert manager.open(ORG, renamed.repo) is None
    assert manager.list_mirrored_repos(ORG) == []
    assert manager.list_mirrored_repositories(ORG) == [key]
    assert "101" in str(original.path)
    assert "alpha" not in str(original.path)
    assert "beta" not in str(original.path)


def test_shared_commit_fork_and_host_have_distinct_mirrors(tmp_path, remote):
    original = _push(remote)
    fork = _push(remote, repo="acme-corp/fork", repository_id="202")
    other_host = original.model_copy(
        update={"push_id": "other-host", "repository_host": "forge.example.com"}
    )
    context = _context(original, fork, other_host)
    manager = MirrorManager(str(tmp_path / "mirrors"))
    # The same literal slug is unsafe to refresh when another lifetime claims it.
    paths = [
        manager.ensure(push, repository_context=_context(push)).path
        for push in (original, fork, other_host)
    ]
    assert len(set(paths)) == 3
    assert len(manager.list_mirrored_repositories(ORG)) == 3
    for push in (original, fork, other_host):
        mirror = manager.open_repository(context.resolve_fact(push).key)
        assert mirror.commit_exists(push.after_sha)


def test_maximum_valid_identity_components_fit_filesystem_paths(tmp_path, remote):
    push = _push(remote, repository_id="9" * 20).model_copy(
        update={
            "org_id": "a" * 64,
            "repository_host": ".".join(["b" * 63] * 3 + ["c" * 61]),
        }
    )
    evidence = RepositoryIdentityEvidence(
        source_table=FactTable.PUSHES,
        source_fact_id=push.push_id,
        role="repo",
        org_id=push.org_id,
        repo=push.repo,
        repository_provider=push.repository_provider,
        repository_host=push.repository_host,
        repository_id=push.repository_id,
        captured_at=push.captured_at,
    )
    context = build_repository_context([evidence], [], push.org_id, as_of=T0)
    manager = MirrorManager(str(tmp_path / "mirrors"))
    mirror = manager.ensure(push, repository_context=context)
    assert mirror.commit_exists(push.after_sha)
    assert manager.list_mirrored_repositories(push.org_id) == [
        context.resolve_fact(push).key
    ]


def test_known_reused_name_cannot_change_prior_origin_or_refs(tmp_path, remote):
    original = _push(remote)
    renamed = _push(remote, repo="acme-corp/beta")
    replacement = _push(remote, repository_id="303")
    manager = MirrorManager(str(tmp_path / "mirrors"))
    before = manager.ensure(original, repository_context=_context(original))
    refs = before.refs()
    origin = run_git(before.path, "config", "remote.origin.url")
    conflict = _context(original, renamed, replacement)
    with pytest.raises(
        MirrorPolicyError, match="repository_mirror_identity_unresolved"
    ):
        manager.ensure(original, repository_context=conflict)
    assert before.refs() == refs
    assert run_git(before.path, "config", "remote.origin.url") == origin
    assert manager.open_repository(conflict.resolve_fact(replacement).key) is None
    # The distinct, proved renamed location remains eligible.
    assert manager.ensure(renamed, repository_context=conflict).path == before.path


def test_origin_mutation_cannot_change_identity_or_enumeration(tmp_path, remote):
    push = _push(remote)
    context = _context(push)
    manager = MirrorManager(str(tmp_path / "mirrors"))
    mirror = manager.ensure(push, repository_context=context)
    refs, keys = mirror.refs(), manager.list_mirrored_repositories(ORG)
    run_git(mirror.path, "config", "remote.origin.url", "https://example.com/foreign")
    assert manager.list_mirrored_repositories(ORG) == keys
    assert manager.open_repository(context.resolve_fact(push).key).refs() == refs
    assert manager.open(ORG, push.repo) is None


def test_legacy_mirror_is_not_promoted_when_identity_appears(tmp_path, remote):
    legacy = _push(remote, repository_id=None)
    identified = _push(remote)
    manager = MirrorManager(str(tmp_path / "mirrors"))
    legacy_mirror = manager.ensure(legacy)
    stable = manager.ensure(identified, repository_context=_context(identified))
    assert stable.path != legacy_mirror.path
    assert manager.open(ORG, legacy.repo).path == legacy_mirror.path
    assert len(manager.list_mirrored_repositories(ORG)) == 2
    with pytest.raises(MirrorPolicyError, match="repository_identity_unresolved"):
        manager.ensure(legacy, repository_context=_context(legacy, identified))


@pytest.mark.parametrize(
    "operation", ["ensure", "refresh_snapshot", "observation_capture"]
)
def test_substituted_source_fact_declines_before_creating_locks(
    tmp_path, remote, operation
):
    push = _push(remote)
    changed = push.model_copy(update={"repository_id": "202"})
    manager = MirrorManager(str(tmp_path / "mirrors"))
    with pytest.raises(MirrorPolicyError, match="repository_identity_conflict"):
        if operation == "ensure":
            manager.ensure(changed, repository_context=_context(push))
        else:
            with getattr(manager, operation)(
                changed, repository_context=_context(push)
            ):
                pytest.fail("unproved source must not enter capture")
    assert not manager.base.exists()


def test_duplicate_fetch_location_requires_proved_name_and_retains_source(
    tmp_path, remote
):
    original = _push(remote)
    renamed = _push(remote, repo="acme-corp/beta")
    manager = MirrorManager(str(tmp_path / "mirrors"))
    context = _context(original, renamed)
    first = manager.ensure(original, repository_context=context)
    replay = manager.ensure(
        original,
        repository_context=context,
        fetch_repo=renamed.repo,
        fetch_clone_url=renamed.clone_url,
    )
    assert replay.path == first.path
    assert original.repo == "acme-corp/alpha"
    with pytest.raises(MirrorPolicyError):
        manager.ensure(
            original,
            repository_context=context,
            fetch_repo="acme-corp/unknown",
            fetch_clone_url=renamed.clone_url,
        )


@pytest.mark.parametrize("key", ["../escape", None, {"org_id": ORG}])
def test_public_keys_validate_before_filesystem_access(tmp_path, key):
    manager = MirrorManager(str(tmp_path / "mirrors"))
    with pytest.raises(ValueError):
        manager.open_repository(key)
    with pytest.raises(ValueError):
        manager.remove_repository(key)
    with pytest.raises(ValueError):
        with manager.read_repository_snapshot([key]):
            pytest.fail("malformed key acquired a lock")
    assert not manager.base.exists()


def test_renamed_refresh_waits_for_same_identity_read_lock(tmp_path, remote):
    original = _push(remote)
    renamed = _push(remote, repo="acme-corp/beta")
    context = _context(original, renamed)
    manager = MirrorManager(str(tmp_path / "mirrors"))
    manager.ensure(original, repository_context=context)
    started, completed = threading.Event(), threading.Event()
    errors = []

    def refresh():
        started.set()
        try:
            manager.ensure(renamed, repository_context=context)
        except Exception as exc:
            errors.append(exc)
        finally:
            completed.set()

    with manager.read_repository_snapshot([context.resolve_fact(original).key]):
        thread = threading.Thread(target=refresh)
        thread.start()
        assert started.wait(2)
        assert not completed.wait(0.1)
    assert completed.wait(5)
    thread.join(1)
    assert errors == []


def test_process_exit_releases_stable_repository_lock(tmp_path, remote):
    push = _push(remote)
    context = _context(push)
    manager = MirrorManager(str(tmp_path / "mirrors"))
    manager.ensure(push, repository_context=context)
    code = """
import json, sys
from sediment_core import ForgeProvider
from sediment_derive.mirror import MirrorManager
from sediment_derive.repository_identity import IdentifiedRepositoryKey, RepositoryIdentity
base, org, host, repo_id = json.loads(sys.argv[1])
key = IdentifiedRepositoryKey(org, RepositoryIdentity(ForgeProvider.GITHUB, host, repo_id))
with MirrorManager(base).read_repository_snapshot([key]):
    print('locked', flush=True)
    sys.stdin.read()
"""
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            code,
            json.dumps([str(manager.base), ORG, "github.com", "101"]),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert select.select([process.stdout], [], [], 5)[0], (
            "child did not acquire lock"
        )
        assert process.stdout.readline().strip() == "locked"
        process.terminate()
        process.communicate(timeout=5)
        # A subsequent process must acquire the same stable lock after exit.
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                code,
                json.dumps([str(manager.base), ORG, "github.com", "101"]),
            ],
            input="",
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert probe.returncode == 0, probe.stderr
        assert probe.stdout.strip() == "locked"
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def test_remove_stable_repository_preserves_legacy_and_fork(tmp_path, remote):
    original = _push(remote)
    fork = _push(remote, repository_id="202", repo="acme-corp/fork")
    legacy = _push(remote, repository_id=None)
    context = _context(original, fork)
    manager = MirrorManager(str(tmp_path / "mirrors"))
    for push in (original, fork):
        manager.ensure(push, repository_context=context)
    manager.ensure(legacy)
    key = context.resolve_fact(original).key
    assert manager.remove_repository(key)
    assert not manager.remove_repository(key)
    assert manager.open_repository(context.resolve_fact(fork).key) is not None
    assert manager.open(ORG, legacy.repo) is not None


def test_gc_retains_renamed_identity_without_retaining_reused_name(
    tmp_path, remote, postgres_store
):
    old = _push(remote).model_copy(update={"captured_at": T0 - timedelta(days=200)})
    renamed = _push(remote, repo="acme-corp/beta").model_copy(
        update={"ref": "refs/heads/renamed"}
    )
    reused = _push(remote, repository_id="303").model_copy(
        update={"captured_at": T0 - timedelta(days=200)}
    )
    manager = MirrorManager(str(tmp_path / "mirrors"))
    for push in (old, renamed, reused):
        postgres_store.store_push(push)
        manager.ensure(push, repository_context=_context(push))
    context = _context(old, renamed, reused)
    results = gc_mirrors(postgres_store, manager, ORG, now=T0, dry_run=False)
    by_id = {r.repository_identity.repository_id: r for r in results}
    assert by_id["101"].action == GCAction.KEPT
    assert by_id["101"].last_push_captured_at == T0
    assert by_id["303"].action == GCAction.REMOVED
    assert manager.open_repository(context.resolve_fact(old).key) is not None
    assert manager.open_repository(context.resolve_fact(reused).key) is None


def test_gc_orphan_identity_remains_visible_without_guessed_name(
    tmp_path, remote, postgres_store
):
    push = _push(remote)
    manager = MirrorManager(str(tmp_path / "mirrors"))
    manager.ensure(push, repository_context=_context(push))
    [result] = gc_mirrors(postgres_store, manager, ORG, now=T0, dry_run=False)
    assert result.repo is None
    assert result.repository_identity.repository_id == "101"
    assert result.action == GCAction.KEPT
    assert result.skip_reason == "quarantine_ambiguous"


def test_gc_json_and_text_distinguish_repositories_with_same_label(
    tmp_path, remote, postgres_store, capsys
):
    from sediment_api.mirror_gc import _print_table, _result_dict
    from sediment_derive.gc import MirrorGCPolicy

    manager = MirrorManager(str(tmp_path / "mirrors"))
    for repository_id in ("101", "202"):
        push = _push(remote, repository_id=repository_id)
        postgres_store.store_push(push)
        manager.ensure(push, repository_context=_context(push))
    results = gc_mirrors(postgres_store, manager, ORG, now=T0)
    rows = [_result_dict(r, dry_run=True, policy=MirrorGCPolicy()) for r in results]
    assert {row["repository_identity"]["repository_id"] for row in rows} == {
        "101",
        "202",
    }
    _print_table(results, dry_run=True)
    printed = capsys.readouterr().out
    assert "github.com/101" in printed
    assert "github.com/202" in printed


def test_nested_repository_snapshots_retain_outer_locks_and_reject_expansion(tmp_path):
    code = """
import fcntl, sys, threading
from sediment_derive.mirror import MirrorManager
from sediment_derive.repository_identity import LegacyRepositoryKey
manager = MirrorManager(sys.argv[1])
key = LegacyRepositoryKey("acme-corp", "acme-corp/alpha")
other = LegacyRepositoryKey("acme-corp", "acme-corp/beta")
started, entered = threading.Event(), threading.Event()
def concurrent_read():
    started.set()
    with manager.read_repository_snapshot([key]):
        entered.set()
def assert_locked():
    name = str(manager._repository_path(key).relative_to(manager.base))
    with manager._lock_file(name).open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        raise AssertionError("outer lock was released")
try:
    with manager.read_repository_snapshot([key]):
        with manager.read_repository_snapshot([key]):
            assert_locked()
        assert_locked()
        try:
            with manager.read_repository_snapshot([other]):
                raise AssertionError("nested snapshot expanded")
        except ValueError:
            pass
        thread = threading.Thread(target=concurrent_read)
        thread.start()
        assert started.wait(1)
        assert not entered.wait(0.1)
        raise RuntimeError("exercise cleanup")
except RuntimeError:
    pass
assert entered.wait(2)
thread.join(2)
with manager.read_repository_snapshot([key, other]):
    assert_locked()
"""
    subprocess.run(
        [sys.executable, "-c", code, str(tmp_path / "mirrors")],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
