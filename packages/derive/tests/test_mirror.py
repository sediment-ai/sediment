# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Mirror tests against a real fixture bare remote.

Per AGENTS.md: real fixtures, not mocked strings — every test here builds an
actual git repository with subprocess git (shared helpers in gitfixtures.py),
mirrors it, and extracts real diffs from it.
"""

from __future__ import annotations

import json
import shutil
import threading
from operator import attrgetter
from pathlib import Path
from urllib.parse import quote

import pytest
from gitfixtures import CART, FIB, commit_all, make_remote, make_work_repo, run_git
from sediment_core import ForgeProvider, Push
from sediment_derive.diff import parse_unified_diff
from sediment_derive.mirror import (
    FETCH_REFSPECS,
    FileReadStatus,
    MirrorError,
    MirrorManager,
    MirrorPolicy,
    MirrorPolicyError,
)

GITHUB_DIFF_FIXTURE = Path(__file__).parent / "fixtures" / "github_diff.json"


def _push(
    remote: Path,
    before: str,
    after: str,
    *,
    repo: str = "acme-corp/backend-service",
    org_id: str = "acme-corp",
    **kwargs,
) -> Push:
    return Push(
        org_id=org_id,
        provider=ForgeProvider.GITHUB,
        repo=repo,
        clone_url=str(remote),
        ref="refs/heads/main",
        before_sha=before,
        after_sha=after,
        **kwargs,
    )


@pytest.fixture
def fixture_remote(tmp_path: Path) -> dict:
    """Work repo reproducing the github_diff.json fixture's commit: empty
    math_utils.py + README exist, then one commit adds the fibonacci body and
    a README line. Notes + PR refs present, as after the stamper ran."""
    work = make_work_repo(tmp_path)
    (work / "app").mkdir()
    (work / "app" / "math_utils.py").write_text("")
    (work / "README.md").write_text("# Backend Service\n")
    base = commit_all(work, "scaffold")

    (work / "app" / "math_utils.py").write_text(FIB)
    (work / "README.md").write_text("# Backend Service\nAdds a fibonacci helper.\n")
    head = commit_all(work, "add fibonacci helper")
    run_git(
        work, "notes", "--ref=sediment", "add", "-m", '{"tool":"claude-code"}', head
    )

    remote = make_remote(tmp_path, work)
    return {"work": work, "remote": remote, "base": base, "head": head}


@pytest.fixture
def fixture_push(fixture_remote: dict) -> Push:
    """The push that mirrors ``fixture_remote``'s whole history."""
    return _push(
        fixture_remote["remote"], fixture_remote["base"], fixture_remote["head"]
    )


def test_mirror_diff_parses_identically_to_github_fixture(
    tmp_path: Path, fixture_remote: dict, fixture_push: Push
) -> None:
    # The mirror-produced diff yields the same parsed FileDiffs as the
    # GitHub REST .diff fixture for the same change.
    mirror = MirrorManager(str(tmp_path / "mirrors")).ensure(fixture_push)
    mirror_diff = mirror.fetch_commit_diff(fixture_push.repo, fixture_remote["head"])

    github_diff = json.loads(GITHUB_DIFF_FIXTURE.read_text())["diff"]
    # Compare as sets: file order in a diff carries no meaning (attribution
    # scores every file independently) and git's tree order differs from the
    # fixture's hand-written order.
    key = attrgetter("file_path")
    assert sorted(parse_unified_diff(mirror_diff), key=key) == sorted(
        parse_unified_diff(github_diff), key=key
    )


def test_read_snapshot_blocks_mutation_for_every_requested_repository(
    tmp_path: Path, fixture_remote: dict, fixture_push: Push
) -> None:
    manager = MirrorManager(str(tmp_path / "mirrors"))
    manager.ensure(fixture_push)
    second_push = fixture_push.model_copy(
        update={"repo": "acme-corp/other-service", "push_id": "other-push"}
    )
    started = [threading.Event(), threading.Event()]
    completed = [threading.Event(), threading.Event()]

    def refresh(index: int, push: Push) -> None:
        started[index].set()
        manager.ensure(push)
        completed[index].set()

    with manager.read_snapshot(
        fixture_push.org_id, [second_push.repo, fixture_push.repo]
    ) as snapshot:
        threads = [
            threading.Thread(target=refresh, args=(0, fixture_push)),
            threading.Thread(target=refresh, args=(1, second_push)),
        ]
        for thread in threads:
            thread.start()
        assert all(event.wait(timeout=1) for event in started)
        assert snapshot.open(fixture_push.org_id, fixture_push.repo) is not None
        assert not any(event.wait(timeout=0.1) for event in completed)

    assert all(event.wait(timeout=5) for event in completed)
    for thread in threads:
        thread.join(timeout=1)


def test_refresh_snapshot_keeps_fetched_notes_stable_until_observation_finishes(
    tmp_path: Path, fixture_remote: dict, fixture_push: Push
) -> None:
    manager = MirrorManager(str(tmp_path / "mirrors"))
    refresh_started = threading.Event()
    refresh_completed = threading.Event()

    def refresh_again() -> None:
        refresh_started.set()
        manager.ensure(fixture_push)
        refresh_completed.set()

    with manager.refresh_snapshot(fixture_push) as snapshot:
        run_git(
            fixture_remote["work"],
            "notes",
            "--ref=sediment",
            "add",
            "-f",
            "-m",
            '{"tool":"cursor"}',
            fixture_remote["head"],
        )
        run_git(
            fixture_remote["work"],
            "push",
            "-q",
            "-f",
            str(fixture_remote["remote"]),
            "refs/notes/*:refs/notes/*",
        )
        thread = threading.Thread(target=refresh_again)
        thread.start()
        assert refresh_started.wait(timeout=1)
        assert not refresh_completed.wait(timeout=0.1)
        assert (
            run_git(
                snapshot.path,
                "notes",
                "--ref=sediment",
                "show",
                fixture_remote["head"],
            ).strip()
            == '{"tool":"claude-code"}'
        )

    assert refresh_completed.wait(timeout=5)
    thread.join(timeout=1)


def test_commit_exists_distinguishes_present_absent_and_non_commit(
    tmp_path: Path, fixture_remote: dict, fixture_push: Push
) -> None:
    # The gold-patch projection filters a session's commits to those the
    # reward repo's mirror can resolve. A real commit resolves; an unknown sha
    # does not; a tree object (which `rev-parse --verify` alone would accept)
    # is rejected by the `^{commit}` peel.
    mirror = MirrorManager(str(tmp_path / "mirrors")).ensure(fixture_push)
    assert mirror.commit_exists(fixture_remote["head"]) is True
    assert mirror.commit_exists(fixture_remote["base"]) is True
    assert mirror.commit_exists("0" * 40) is False
    tree = run_git(
        mirror.path, "rev-parse", f"{fixture_remote['head']}^{{tree}}"
    ).strip()
    assert mirror.commit_exists(tree) is False


def test_parent_commit_returns_parent_and_none_for_root(
    tmp_path: Path, fixture_remote: dict, fixture_push: Push
) -> None:
    # base_commit is the parent of the first attributed commit. The
    # fixture's head has the scaffold commit as its parent; the scaffold is a
    # root commit, so its parent is None (no base to check out onto).
    mirror = MirrorManager(str(tmp_path / "mirrors")).ensure(fixture_push)
    assert mirror.parent_commit(fixture_remote["head"]) == fixture_remote["base"]
    assert mirror.parent_commit(fixture_remote["base"]) is None


def test_parent_commit_raises_for_absent_commit(
    tmp_path: Path, fixture_push: Push
) -> None:
    mirror = MirrorManager(str(tmp_path / "mirrors")).ensure(fixture_push)
    with pytest.raises(MirrorError):
        mirror.parent_commit("0" * 40)


def test_diff_range_matches_git_diff_base_to_head(
    tmp_path: Path, fixture_remote: dict, fixture_push: Push
) -> None:
    # The gold patch is `git diff base..last`. diff_range must reproduce what a
    # human running `git diff base head` in the work repo sees — the full change
    # the session introduced, across every touched file.
    mirror = MirrorManager(str(tmp_path / "mirrors")).ensure(fixture_push)
    base, head = fixture_remote["base"], fixture_remote["head"]

    gold = mirror.diff_range(base, head)
    # Ground truth: the same diff computed directly in the work repo.
    expected = run_git(fixture_remote["work"], "diff", "-M", base, head)
    key = attrgetter("file_path")
    assert sorted(parse_unified_diff(gold), key=key) == sorted(
        parse_unified_diff(expected), key=key
    )
    assert "def fibonacci" in gold  # the substantive change is present


def test_is_ancestor_true_false_and_self(tmp_path: Path) -> None:
    # The task projection uses this to reject a degenerate base..last
    # range. A commit is its own ancestor (equal ⇒ True); the parent is an
    # ancestor of the child but not vice versa.
    work = make_work_repo(tmp_path)
    (work / "a.py").write_text("1\n")
    base = commit_all(work, "root")
    (work / "a.py").write_text("2\n")
    head = commit_all(work, "next")
    remote = make_remote(tmp_path, work)
    mirror = MirrorManager(str(tmp_path / "mirrors")).ensure(_push(remote, base, head))

    assert mirror.is_ancestor(base, head) is True
    assert mirror.is_ancestor(head, base) is False
    assert mirror.is_ancestor(head, head) is True  # equal counts as ancestor


def test_is_ancestor_raises_for_absent_commit(tmp_path: Path) -> None:
    work = make_work_repo(tmp_path)
    (work / "a.py").write_text("1\n")
    head = commit_all(work, "root")
    remote = make_remote(tmp_path, work)
    mirror = MirrorManager(str(tmp_path / "mirrors")).ensure(_push(remote, head, head))
    # An unknown commit is a real failure (exit 128), not a clean "not ancestor".
    with pytest.raises(MirrorError):
        mirror.is_ancestor("0" * 40, head)


def test_diff_range_spans_multiple_commits(tmp_path: Path) -> None:
    # base..last must include every commit in the range, not just the head —
    # a rollout's gold patch spans all its attributed commits.
    work = make_work_repo(tmp_path)
    (work / "README.md").write_text("# repo\n")
    base = commit_all(work, "root")
    (work / "a.py").write_text(FIB)
    commit_all(work, "add a")
    (work / "b.py").write_text(CART)
    last = commit_all(work, "add b")
    remote = make_remote(tmp_path, work)
    mirror = MirrorManager(str(tmp_path / "mirrors")).ensure(_push(remote, base, last))

    gold = mirror.diff_range(base, last)
    paths = {fd.file_path for fd in parse_unified_diff(gold)}
    assert paths == {"a.py", "b.py"}  # both commits' additions, README untouched


def _edited_rename_repo(tmp_path: Path) -> tuple[Path, str, str]:
    work = make_work_repo(tmp_path)
    old_dir = work / "old"
    old_dir.mkdir()
    for index in range(20):
        body = "".join(
            f"def function_{index}_{line}():\n    return {index + line}\n"
            for line in range(20)
        )
        (old_dir / f"file-{index:02}.py").write_text(body)
    source = commit_all(work, "add source files")

    new_dir = work / "new"
    new_dir.mkdir()
    for index in range(20):
        source_file = old_dir / f"file-{index:02}.py"
        destination = new_dir / f"renamed-{index:02}.py"
        source_file.rename(destination)
        destination.write_text(destination.read_text() + "# reviewed\n")
    old_dir.rmdir()
    head = commit_all(work, "rename source files")
    return make_remote(tmp_path, work), source, head


def test_resolve_path_follows_one_git_rename_and_reads_bounded_text(
    tmp_path: Path,
) -> None:
    work = make_work_repo(tmp_path)
    (work / "old.py").write_text("def query():\n    return 'kept'\n")
    source = commit_all(work, "add query")
    (work / "old.py").rename(work / "new.py")
    (work / "binary.py").write_bytes(b"abc\x00def")
    head = commit_all(work, "rename query")
    remote = make_remote(tmp_path, work)
    mirror = MirrorManager(str(tmp_path / "mirrors")).ensure(
        _push(remote, source, head)
    )

    assert mirror.resolve_path(source, head, "old.py") == "new.py"
    readable = mirror.read_file(head, "new.py", max_bytes=100)
    assert readable.status is FileReadStatus.READABLE
    assert readable.text == "def query():\n    return 'kept'\n"
    assert (
        mirror.read_file(head, "missing.py", max_bytes=100).status
        is FileReadStatus.ABSENT
    )
    assert (
        mirror.read_file(head, "new.py", max_bytes=4).status is FileReadStatus.OVERSIZED
    )
    assert (
        mirror.read_file(head, "binary.py", max_bytes=100).status
        is FileReadStatus.BINARY
    )


def test_resolve_path_ignores_developer_global_rename_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote, source, head = _edited_rename_repo(tmp_path)
    mirror = MirrorManager(str(tmp_path / "mirrors")).ensure(
        _push(remote, source, head)
    )

    global_config = tmp_path / "global-gitconfig"
    global_config.write_text("[diff]\n\trenameLimit = 1\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))

    assert mirror.resolve_path(source, head, "old/file-00.py") == "new/renamed-00.py"


def test_fetch_commit_diff_ignores_developer_global_rename_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote, source, head = _edited_rename_repo(tmp_path)
    push = _push(remote, source, head)
    mirror = MirrorManager(str(tmp_path / "mirrors")).ensure(push)

    global_config = tmp_path / "global-gitconfig"
    global_config.write_text("[diff]\n\trenameLimit = 1\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))

    parsed = parse_unified_diff(mirror.fetch_commit_diff(push.repo, head))
    assert {file_diff.file_path: file_diff.added_lines for file_diff in parsed} == {
        f"new/renamed-{index:02}.py": "# reviewed" for index in range(20)
    }


def test_diff_range_ignores_developer_global_rename_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote, source, head = _edited_rename_repo(tmp_path)
    push = _push(remote, source, head)
    mirror = MirrorManager(str(tmp_path / "mirrors")).ensure(push)

    global_config = tmp_path / "global-gitconfig"
    global_config.write_text("[diff]\n\trenameLimit = 1\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))

    parsed = parse_unified_diff(mirror.diff_range(source, head))
    assert {file_diff.file_path: file_diff.added_lines for file_diff in parsed} == {
        f"new/renamed-{index:02}.py": "# reviewed" for index in range(20)
    }


def test_diff_range_ignores_developer_global_diff_config(
    tmp_path: Path,
    fixture_remote: dict,
    fixture_push: Push,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # diff_range is porcelain `git diff` (unlike the plumbing `diff-tree`
    # fetch_commit_diff uses), so it must pin diff.noprefix/srcPrefix/dstPrefix
    # itself — otherwise a contributor's global git config reshapes the gold
    # patch: no a/b prefixes means the unified-diff parser's `+++ b/` match
    # misses, and a consumer's `git apply` on the "reference_patch" fails.
    global_config = tmp_path / "global-gitconfig"
    global_config.write_text("[diff]\n\tnoprefix = true\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))

    mirror = MirrorManager(str(tmp_path / "mirrors")).ensure(fixture_push)
    gold = mirror.diff_range(fixture_remote["base"], fixture_remote["head"])

    assert "\n+++ b/app/math_utils.py" in gold
    assert "\n--- a/app/math_utils.py" in gold
    parsed = parse_unified_diff(gold)
    assert {fd.file_path for fd in parsed} == {"app/math_utils.py", "README.md"}


def test_mirror_contains_notes_pr_and_branch_refs(
    tmp_path: Path, fixture_push: Push
) -> None:
    # The notes-attribution enabler: after a push the mirror holds the branch head,
    # refs/notes/sediment, and PR head refs.
    mirror = MirrorManager(str(tmp_path / "mirrors")).ensure(fixture_push)

    refs = run_git(mirror.path, "for-each-ref", "--format=%(refname)").split()
    assert "refs/heads/main" in refs
    assert "refs/notes/sediment" in refs
    assert "refs/pull/1/head" in refs


def test_mirror_config_has_unconditional_refspecs(
    tmp_path: Path, fixture_push: Push
) -> None:
    mirror = MirrorManager(str(tmp_path / "mirrors")).ensure(fixture_push)

    configured = run_git(
        mirror.path, "config", "--get-all", "remote.origin.fetch"
    ).splitlines()
    assert configured == list(FETCH_REFSPECS)


def test_fetch_succeeds_when_remote_has_no_notes_or_pr_refs(tmp_path: Path) -> None:
    # Regression guard for the glob refspec choice: an exact refspec for a
    # missing ref makes the whole fetch fail, so a repo that predates the
    # attribution stamper would never mirror at all.
    work = make_work_repo(tmp_path)
    (work / "a.py").write_text("x = 1\n")
    head = commit_all(work, "init")
    remote = tmp_path / "bare-remote.git"
    run_git(tmp_path, "init", "-q", "--bare", str(remote))
    run_git(work, "push", "-q", str(remote), "refs/heads/*:refs/heads/*")

    push = _push(remote, "0" * 40, head)
    mirror = MirrorManager(str(tmp_path / "mirrors")).ensure(push)
    assert run_git(mirror.path, "rev-parse", "refs/heads/main").strip() == head


def test_redelivered_push_refresh_is_idempotent(
    tmp_path: Path, fixture_push: Push
) -> None:
    # ensure() twice (create+fetch, then refetch) leaves the same refs and
    # no corruption.
    manager = MirrorManager(str(tmp_path / "mirrors"))
    first = run_git(manager.ensure(fixture_push).path, "for-each-ref")
    second = run_git(manager.ensure(fixture_push).path, "for-each-ref")
    assert first == second
    assert "refs/notes/sediment" in second


def test_open_returns_existing_mirror_without_fetching(
    tmp_path: Path, fixture_remote: dict, fixture_push: Push
) -> None:
    # The read-only accessor every derivation uses: after ensure(), open()
    # hands back the same mirror — even when the remote is gone, i.e. it never
    # touches the network.
    manager = MirrorManager(str(tmp_path / "mirrors"))
    ensured = manager.ensure(fixture_push)

    shutil.rmtree(fixture_remote["remote"])  # a fetch would now fail loudly

    opened = manager.open(fixture_push.org_id, fixture_push.repo)
    assert opened is not None
    assert opened.path == ensured.path
    diff = opened.fetch_commit_diff(fixture_push.repo, fixture_remote["head"])
    assert "def fibonacci" in diff


def test_open_unmirrored_repo_returns_none(tmp_path: Path) -> None:
    manager = MirrorManager(str(tmp_path / "mirrors"))
    assert manager.open("acme-corp", "acme-corp/never-seen") is None
    # And it must not create anything on disk — open() is read-only.
    assert not (tmp_path / "mirrors").exists()


def test_multi_commit_enumeration_and_cap(tmp_path: Path) -> None:
    work = make_work_repo(tmp_path)
    (work / "README.md").write_text("# repo\n")
    c0 = commit_all(work, "root")
    (work / "a.py").write_text(FIB)
    c1 = commit_all(work, "add a")
    (work / "b.py").write_text(CART)
    c2 = commit_all(work, "add b")
    remote = make_remote(tmp_path, work)
    manager = MirrorManager(str(tmp_path / "mirrors"))

    push = _push(remote, c0, c2)
    mirror = manager.ensure(push)
    assert mirror.list_push_commits(push, max_commits=20) == [c1, c2]
    # The cap keeps the newest commits — the head is what attribution
    # targets first, so it must never be the one dropped.
    assert mirror.list_push_commits(push, max_commits=1) == [c2]

    forced = _push(remote, c0, c2, forced=True)
    assert mirror.list_push_commits(forced, max_commits=20) == [c2]

    created = _push(remote, "0" * 40, c2)
    assert mirror.list_push_commits(created, max_commits=20) == [c2]

    unknown_before = _push(remote, "f" * 40, c2)
    assert mirror.list_push_commits(unknown_before, max_commits=20) == [c2]


def test_max_commits_zero_or_negative_degrades_to_head(tmp_path: Path) -> None:
    # A 0/negative cap (policy typo) must keep the head, never attribute a
    # stray middle subset. With a 3-commit range, an un-clamped -1 slice
    # would yield the two newest.
    work = make_work_repo(tmp_path)
    (work / "README.md").write_text("# repo\n")
    c0 = commit_all(work, "root")
    (work / "a.py").write_text(FIB)
    commit_all(work, "add a")
    (work / "b.py").write_text(CART)
    commit_all(work, "add b")
    (work / "c.py").write_text("value = 1\n")
    c3 = commit_all(work, "add c")
    remote = make_remote(tmp_path, work)

    push = _push(remote, c0, c3)
    mirror = MirrorManager(str(tmp_path / "mirrors")).ensure(push)
    assert mirror.list_push_commits(push, max_commits=0) == [c3]
    assert mirror.list_push_commits(push, max_commits=-1) == [c3]


def test_native_commit_cap_matches_merge_graph_with_clock_skew(tmp_path, monkeypatch):
    from sediment_derive import mirror as mirror_module

    work = make_work_repo(tmp_path)

    def date(value):
        monkeypatch.setenv("GIT_AUTHOR_DATE", value)
        monkeypatch.setenv("GIT_COMMITTER_DATE", value)

    date("2020-01-01T00:00:00+00:00")
    (work / "README.md").write_text("root\n")
    base = commit_all(work, "root")
    run_git(work, "checkout", "-q", "-b", "feature")
    date("2022-01-01T00:00:00+00:00")
    (work / "feature.py").write_text(FIB)
    commit_all(work, "feature")
    run_git(work, "checkout", "-q", "main")
    date("2021-01-01T00:00:00+00:00")
    (work / "main.py").write_text(CART)
    commit_all(work, "main")
    date("2023-01-01T00:00:00+00:00")
    run_git(work, "merge", "-q", "--no-ff", "-m", "merge", "feature")
    date("2020-06-01T00:00:00+00:00")
    (work / "skew.py").write_text("clock = 'earlier'\n")
    head = commit_all(work, "clock skew")
    remote = make_remote(tmp_path, work)
    push = _push(remote, base, head)
    mirror = MirrorManager(str(tmp_path / "mirrors")).ensure(push)
    complete = run_git(
        mirror.path, "rev-list", "--end-of-options", f"{base}..{head}"
    ).split()
    calls = []
    original = mirror_module._git

    def recording_git(path, *args):
        output = original(path, *args)
        calls.append((args, len(output.split())))
        return output

    monkeypatch.setattr(mirror_module, "_git", recording_git)
    for cap in (1, 2, 3, 10):
        assert mirror.list_push_commits(push, max_commits=cap) == list(
            reversed(complete[:cap])
        )
        assert f"--max-count={cap}" in calls[-1][0]
        assert calls[-1][1] <= cap


def test_fetch_prunes_refs_deleted_on_remote(tmp_path: Path) -> None:
    # A branch deleted on the remote must disappear from the mirror on the
    # next push fetch — a stale ref would mislead squash aliasing.
    work = make_work_repo(tmp_path)
    (work / "README.md").write_text("# repo\n")
    c0 = commit_all(work, "root")
    run_git(work, "checkout", "-q", "-b", "feature")
    (work / "a.py").write_text(FIB)
    commit_all(work, "add a")
    run_git(work, "checkout", "-q", "main")
    remote = make_remote(tmp_path, work)  # pushes both main and feature
    manager = MirrorManager(str(tmp_path / "mirrors"))

    mirror = manager.ensure(_push(remote, c0, c0))
    refs = run_git(mirror.path, "for-each-ref", "--format=%(refname)").split()
    assert "refs/heads/feature" in refs

    run_git(remote, "update-ref", "-d", "refs/heads/feature")
    mirror = manager.ensure(_push(remote, c0, c0))
    refs = run_git(mirror.path, "for-each-ref", "--format=%(refname)").split()
    assert "refs/heads/feature" not in refs  # pruned
    assert "refs/heads/main" in refs  # still present


def test_ensure_heals_mirror_missing_origin(
    tmp_path: Path, fixture_remote: dict
) -> None:
    # Simulate a crash after `git init --bare` but before origin was configured:
    # HEAD exists, origin does not. ensure() must reconcile the remote and fetch
    # rather than wedge forever on `git remote set-url` ("No such remote").
    base = tmp_path / "mirrors"
    push = _push(fixture_remote["remote"], "0" * 40, fixture_remote["head"])
    half = base / quote(f"{push.org_id}/{push.repo}", safe="")
    half.mkdir(parents=True)
    run_git(half, "init", "--bare", "--quiet", ".")  # HEAD present, no origin

    mirror = MirrorManager(str(base)).ensure(push)  # must not raise
    assert mirror.path == half
    assert (
        run_git(half, "rev-parse", "refs/heads/main").strip() == fixture_remote["head"]
    )


def test_lock_file_never_collides_with_a_mirror_dir(tmp_path: Path) -> None:
    # A repo literally named "….lock" must not collide with another repo's lock
    # file. Pre-fix the lock was `base/{dirname}.lock`, byte-identical to the
    # mirror dir of a "….lock"-named repo.
    work = make_work_repo(tmp_path)
    (work / "a.py").write_text(FIB)
    commit_all(work, "init")
    remote = make_remote(tmp_path, work)
    head = run_git(work, "rev-parse", "HEAD").strip()
    manager = MirrorManager(str(tmp_path / "mirrors"))

    m1 = manager.ensure(_push(remote, "0" * 40, head, repo="acme-corp/x"))
    m2 = manager.ensure(_push(remote, "0" * 40, head, repo="acme-corp/x.lock"))
    assert m1.path != m2.path
    assert (m1.path / "HEAD").exists()
    assert (m2.path / "HEAD").exists()


def test_ext_transport_clone_url_is_blocked(tmp_path: Path) -> None:
    # A webhook-controlled `ext::` clone URL must not run its command — git is
    # confined to ordinary transports via GIT_ALLOW_PROTOCOL.
    sentinel = tmp_path / "PWNED"
    push = Push(
        org_id="acme-corp",
        provider=ForgeProvider.GITHUB,
        repo="acme-corp/evil",
        clone_url=f"ext::sh -c 'touch {sentinel}'",
        ref="refs/heads/main",
        before_sha="0" * 40,
        after_sha="a" * 40,
    )
    with pytest.raises(MirrorError):
        MirrorManager(str(tmp_path / "mirrors")).ensure(push)
    assert not sentinel.exists()


def test_default_policy_accepts_file_scheme_remote(
    tmp_path: Path, fixture_remote: dict, fixture_push: Push
) -> None:
    # Dev/test posture: the default MirrorPolicy is permissive, so an
    # explicit file:// remote — the fixture transport — still mirrors.
    push = fixture_push.model_copy(
        update={"clone_url": f"file://{fixture_remote['remote']}"}
    )
    mirror = MirrorManager(str(tmp_path / "mirrors")).ensure(push)
    assert (
        run_git(mirror.path, "rev-parse", "refs/heads/main").strip()
        == fixture_remote["head"]
    )


@pytest.mark.parametrize(
    "clone_url",
    [
        "file:///srv/other-org/repo.git",  # server-local repo read
        "/srv/other-org/repo.git",  # scheme-less local path
        "git@github.com:org/repo.git",  # scp-style ssh: no scheme, no parseable host
        "http://127.0.0.1:8080/internal.git",  # loopback
        "http://[::1]/internal.git",  # IPv6 loopback
        "https://localhost/repo.git",
        "http://169.254.169.254/latest/meta-data",  # cloud metadata (link-local)
        "http://10.0.0.7/repo.git",  # RFC1918
        "http://172.16.0.1/repo.git",
        "http://192.168.1.10/repo.git",
        # Non-canonical IP encodings that ipaddress.ip_address rejects but
        # getaddrinfo (like git) resolves back to an internal address. These
        # are the forms a literal-only filter misses.
        "http://2130706433/internal.git",  # decimal 127.0.0.1
        "http://0x7f000001/internal.git",  # hex 127.0.0.1
        "http://127.1/internal.git",  # short-form 127.0.0.1
        "http://2852039166/latest/meta-data",  # decimal 169.254.169.254
        "https://github.com@2130706433/repo.git",  # userinfo masks decimal loopback
    ],
)
def test_enforced_policy_rejects_before_any_git_runs(
    tmp_path: Path, clone_url: str
) -> None:
    # Production posture: a hostile clone_url raises MirrorError before
    # any git subprocess or directory creation — no fetch, no trace.
    base = tmp_path / "mirrors"
    manager = MirrorManager(str(base), policy=MirrorPolicy(enforce=True))
    push = Push(
        org_id="acme-corp",
        provider=ForgeProvider.GITHUB,
        repo="acme-corp/evil",
        clone_url=clone_url,
        ref="refs/heads/main",
        before_sha="0" * 40,
        after_sha="a" * 40,
    )
    with pytest.raises(MirrorError, match="clone_url rejected"):
        manager.ensure(push)
    assert not base.exists()  # rejected before init/lock — nothing created


def test_enforced_policy_allowlist() -> None:
    # Non-empty allowlist: only the named hosts pass; file: is refused even
    # for an allowlisted deployment; a private-range host CAN be allowlisted
    # (an intranet forge is a deliberate operator choice).
    policy = MirrorPolicy(enforce=True, allowed_hosts=frozenset({"GitHub.com"}))
    assert policy.rejection_reason("https://github.com/org/repo.git") is None
    assert policy.rejection_reason("https://evil.example.com/repo.git") is not None
    assert policy.rejection_reason("file:///srv/repo.git") is not None
    assert policy.rejection_reason("http://10.0.0.7/repo.git") is not None

    intranet = MirrorPolicy(enforce=True, allowed_hosts=frozenset({"10.0.0.5"}))
    assert intranet.rejection_reason("ssh://git@10.0.0.5/repo.git") is None


def test_enforced_policy_empty_allowlist_accepts_public_host() -> None:
    # Empty allowlist = any public host (the internal ranges above still
    # refused) — mirror mode must work out of the box against a SaaS forge.
    assert (
        MirrorPolicy(enforce=True).rejection_reason("https://github.com/org/repo.git")
        is None
    )
    # Don't over-block: a public IP literal must still pass (the getaddrinfo
    # check only rejects addresses that resolve to an internal range).
    assert (
        MirrorPolicy(enforce=True).rejection_reason("http://8.8.8.8/repo.git") is None
    )


def test_rejection_raises_mirror_policy_error(tmp_path: Path) -> None:
    # A policy rejection is a MirrorPolicyError (a MirrorError subclass), so the
    # orchestration layer can skip a deterministic rejection cleanly instead of
    # 500-and-redeliver-forever.
    manager = MirrorManager(str(tmp_path / "m"), policy=MirrorPolicy(enforce=True))
    push = Push(
        org_id="acme-corp",
        provider=ForgeProvider.GITHUB,
        repo="acme-corp/evil",
        clone_url="http://2130706433/internal.git",  # decimal loopback
        ref="refs/heads/main",
        before_sha="0" * 40,
        after_sha="a" * 40,
    )
    with pytest.raises(MirrorPolicyError):
        manager.ensure(push)


def test_option_injection_sha_is_not_run_as_git_flag(
    tmp_path: Path, fixture_remote: dict, fixture_push: Push
) -> None:
    # A crafted after_sha like "--output=<path>" must be read as a (bad)
    # revision, never a git option that writes a file.
    sentinel = tmp_path / "PWNED"
    mirror = MirrorManager(str(tmp_path / "mirrors")).ensure(fixture_push)
    with pytest.raises(MirrorError):
        mirror.fetch_commit_diff(fixture_push.repo, f"--output={sentinel}")
    assert not sentinel.exists()


def test_non_ascii_filename_diff_is_parseable(tmp_path: Path) -> None:
    # The mirror emits readable non-ASCII names. The shared parser also accepts
    # Git's quoted form, covered by test_diff.py's real-Git quoting matrix.
    work = make_work_repo(tmp_path)
    (work / "café.py").write_text(FIB)
    head = commit_all(work, "add café")
    remote = make_remote(tmp_path, work)

    push = _push(remote, "0" * 40, head)
    mirror = MirrorManager(str(tmp_path / "mirrors")).ensure(push)
    parsed = parse_unified_diff(mirror.fetch_commit_diff(push.repo, head))
    assert [fd.file_path for fd in parsed] == ["café.py"]
    assert "def fibonacci" in parsed[0].added_lines


def test_merge_commit_diffs_against_first_parent(tmp_path: Path) -> None:
    # GitHub's .diff for a merge commit is the diff vs the first parent; the
    # mirror must match, not produce git's (usually empty) combined diff.
    work = make_work_repo(tmp_path)
    (work / "README.md").write_text("# repo\n")
    commit_all(work, "root")
    run_git(work, "checkout", "-q", "-b", "feature")
    (work / "feature.py").write_text(FIB)
    commit_all(work, "add feature")
    run_git(work, "checkout", "-q", "main")
    run_git(work, "merge", "-q", "--no-ff", "-m", "merge feature", "feature")
    merge_sha = run_git(work, "rev-parse", "HEAD").strip()
    remote = make_remote(tmp_path, work)

    push = _push(remote, "0" * 40, merge_sha)
    mirror = MirrorManager(str(tmp_path / "mirrors")).ensure(push)
    parsed = parse_unified_diff(mirror.fetch_commit_diff(push.repo, merge_sha))
    assert [fd.file_path for fd in parsed] == ["feature.py"]
    assert "def fibonacci" in parsed[0].added_lines


def _bare_remote_with_commit(tmp_path: Path, name: str) -> tuple[Path, str]:
    # Each call gets its own subtree: make_remote hardcodes "remote.git"
    # relative to the root it's given, so two remotes in one test need two
    # distinct roots.
    root = tmp_path / name
    root.mkdir()
    work = make_work_repo(root)
    (work / "a.py").write_text(FIB)
    head = commit_all(work, "init")
    remote = make_remote(root, work)
    return remote, head


def test_list_mirrored_repos_recovers_org_and_repo_from_directory_names(
    tmp_path: Path,
) -> None:
    # GC enumerates mirrors from disk, never a fact read — recovered via
    # unquote on the directory names this manager itself created.
    manager = MirrorManager(str(tmp_path / "mirrors"))
    remote_a, head_a = _bare_remote_with_commit(tmp_path, "a")
    remote_b, head_b = _bare_remote_with_commit(tmp_path, "b")
    # owner/repo slugs also exercise the quoting round-trip harder:
    # the "/" must survive quote → directory name → unquote.
    manager.ensure(_push(remote_a, "0" * 40, head_a, repo="acme/svc-a"))
    manager.ensure(_push(remote_b, "0" * 40, head_b, repo="acme/svc-b"))
    manager.ensure(
        _push(remote_b, "0" * 40, head_b, repo="acme/svc-b", org_id="other-org")
    )

    assert sorted(manager.list_mirrored_repos("acme-corp")) == [
        "acme/svc-a",
        "acme/svc-b",
    ]
    assert manager.list_mirrored_repos("other-org") == ["acme/svc-b"]
    assert manager.list_mirrored_repos("no-such-org") == []


def test_list_mirrored_repos_empty_when_base_missing(tmp_path: Path) -> None:
    manager = MirrorManager(str(tmp_path / "never-created"))
    assert manager.list_mirrored_repos("acme-corp") == []


def test_remove_deletes_mirror_directory(tmp_path: Path) -> None:
    manager = MirrorManager(str(tmp_path / "mirrors"))
    remote, head = _bare_remote_with_commit(tmp_path, "a")
    push = _push(remote, "0" * 40, head, repo="acme-corp/svc-a")
    mirror = manager.ensure(push)
    assert mirror.path.exists()

    assert manager.remove("acme-corp", "acme-corp/svc-a") is True
    assert not mirror.path.exists()
    assert manager.open("acme-corp", "acme-corp/svc-a") is None


def test_remove_returns_false_when_no_mirror_exists(tmp_path: Path) -> None:
    manager = MirrorManager(str(tmp_path / "mirrors"))
    assert manager.remove("acme-corp", "acme-corp/never-seen") is False


def test_rename_moves_mirror_and_preserves_objects(tmp_path: Path) -> None:
    manager = MirrorManager(str(tmp_path / "mirrors"))
    remote, head = _bare_remote_with_commit(tmp_path, "a")
    push = _push(remote, "0" * 40, head, repo="acme-corp/old-name")
    manager.ensure(push)

    assert (
        manager.rename("acme-corp", "acme-corp/old-name", "acme-corp/new-name") is True
    )
    assert manager.open("acme-corp", "acme-corp/old-name") is None
    renamed = manager.open("acme-corp", "acme-corp/new-name")
    assert renamed is not None
    assert renamed.commit_exists(head) is True


def test_rename_is_noop_when_no_existing_mirror(tmp_path: Path) -> None:
    manager = MirrorManager(str(tmp_path / "mirrors"))
    assert manager.rename("acme-corp", "acme-corp/ghost", "acme-corp/new-name") is False
    assert manager.open("acme-corp", "acme-corp/new-name") is None


def test_rename_is_noop_when_destination_already_exists(tmp_path: Path) -> None:
    manager = MirrorManager(str(tmp_path / "mirrors"))
    remote_a, head_a = _bare_remote_with_commit(tmp_path, "a")
    remote_b, head_b = _bare_remote_with_commit(tmp_path, "b")
    manager.ensure(_push(remote_a, "0" * 40, head_a, repo="acme-corp/old-name"))
    manager.ensure(_push(remote_b, "0" * 40, head_b, repo="acme-corp/new-name"))

    assert (
        manager.rename("acme-corp", "acme-corp/old-name", "acme-corp/new-name") is False
    )
    # Neither mirror is disturbed by the refused rename.
    assert manager.open("acme-corp", "acme-corp/old-name") is not None
    dest = manager.open("acme-corp", "acme-corp/new-name")
    assert dest is not None
    assert dest.commit_exists(head_b) is True


def test_rename_same_name_is_noop(tmp_path: Path) -> None:
    # Guards against a same-file double-lock deadlock (sorted lock names
    # collapse to one entry when old_repo == new_repo).
    manager = MirrorManager(str(tmp_path / "mirrors"))
    remote, head = _bare_remote_with_commit(tmp_path, "a")
    manager.ensure(_push(remote, "0" * 40, head, repo="acme-corp/svc-a"))
    assert manager.rename("acme-corp", "acme-corp/svc-a", "acme-corp/svc-a") is False
