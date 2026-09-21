# SPDX-License-Identifier: AGPL-3.0-or-later
"""Marker races across real processes, with explicit Git-boundary barriers."""

from __future__ import annotations

import json
import io
import fcntl
import os
import select
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

import test_attribution as h


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("SEDIMENT_ATTRIBUTION_LOG", str(tmp_path / "test.log"))
    monkeypatch.setenv("SEDIMENT_ATTRIBUTION_CONFIG", str(tmp_path / "absent.json"))


_CLIENT = """
import importlib.util, json, os, subprocess, sys
spec = importlib.util.spec_from_file_location('attribution', sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod._now_iso = lambda: '2020-01-01T00:00:00+00:00'
settings = json.loads(sys.argv[2])
if settings:
    original = mod.subprocess.run
    def paused(args, *a, **kw):
        matches = args[:2] == ['git', 'notes'] and settings['operation'] in args
        if matches and settings['phase'] == 'before':
            os.write(settings['ready'], b'1')
            os.read(settings['resume'], 1)
        result = original(args, *a, **kw)
        if matches and settings['phase'] == 'after':
            os.write(settings['ready'], b'1')
            os.read(settings['resume'], 1)
        return result
    mod.subprocess.run = paused
raise SystemExit(mod.main(sys.argv[3:]))
"""


def client(repo, *args, stdin=""):
    return subprocess.run(
        [sys.executable, "-c", _CLIENT, str(h.SCRIPT), "{}", *args],
        cwd=repo,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "SEDIMENT_ATTRIBUTION_LOG": str(h.attribution_log(repo))},
    )


def mark(repo, session="first"):
    result = client(
        repo,
        "mark",
        "--tool",
        "codex",
        stdin=json.dumps({"session_id": session, "cwd": str(repo)}),
    )
    assert result.returncode == 0, result.stderr


@contextmanager
def paused_client(repo, *args, operation="add", phase="before"):
    ready_read, ready_write = os.pipe()
    resume_read, resume_write = os.pipe()
    settings = {
        "operation": operation,
        "phase": phase,
        "ready": ready_write,
        "resume": resume_read,
    }
    process = subprocess.Popen(
        [sys.executable, "-c", _CLIENT, str(h.SCRIPT), json.dumps(settings), *args],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        pass_fds=(ready_write, resume_read),
        env={**os.environ, "SEDIMENT_ATTRIBUTION_LOG": str(h.attribution_log(repo))},
    )
    os.close(ready_write)
    os.close(resume_read)
    try:
        assert select.select([ready_read], [], [], 10)[0], "Git barrier not reached"
        assert os.read(ready_read, 1) == b"1", process.communicate(timeout=1)
        yield process, resume_write
    finally:
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=5)
        os.close(ready_read)
        os.close(resume_write)


def finish(process, resume):
    os.write(resume, b"1")
    stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == 0, (stdout, stderr)


def markers(repo):
    path = h.marker_file(repo)
    return (
        [json.loads(line) for line in path.read_text().splitlines()]
        if path.exists()
        else []
    )


@pytest.mark.parametrize("refresh", [False, True], ids=["C1-added", "C2-refreshed"])
def test_stamp_retains_marks_after_snapshot(tmp_path: Path, refresh: bool):
    repo = h.make_repo(tmp_path / "repo")
    mark(repo)
    initial = markers(repo)[0]
    original_sha = h.git(repo, "rev-parse", "HEAD")
    with paused_client(repo, "stamp") as (process, resume):
        mark(repo, "first" if refresh else "later")
        concurrent = markers(repo)
        finish(process, resume)
    note = h.note_on(repo, original_sha)
    assert {s["session_id"] for s in note["sessions"]} == {"first"}
    remaining = markers(repo)
    assert [m["session_id"] for m in remaining] == ["first" if refresh else "later"]
    if refresh:
        assert remaining[0]["stamped_at"] == initial["stamped_at"]
        assert remaining[0]["generation"] != initial["generation"]
    assert remaining[0] == concurrent[-1]
    h.git(repo, "commit", "--allow-empty", "-qm", "next")
    assert client(repo, "stamp").returncode == 0
    assert {s["session_id"] for s in h.note_on(repo)["sessions"]} == {
        "first" if refresh else "later"
    }
    assert h.note_on(repo, original_sha) == note
    assert not markers(repo)


def test_stamp_busy_does_not_block_markers(tmp_path):
    repo = h.make_repo(tmp_path / "repo")
    mark(repo)
    with paused_client(repo, "stamp") as (process, resume):
        start = time.monotonic()
        mark(repo, "later")
        competing = client(repo, "stamp")
        assert time.monotonic() - start < 5
        assert competing.returncode == 0
        assert "stamp_busy" in competing.stderr
        assert {m["session_id"] for m in markers(repo)} == {"first", "later"}
        assert h.note_on(repo) is None
        finish(process, resume)
    assert [m["session_id"] for m in markers(repo)] == ["later"]


def test_stamp_pins_head_and_diagnostic(tmp_path):
    repo = h.make_repo(tmp_path / "repo")
    mark(repo)
    original = h.git(repo, "rev-parse", "HEAD")
    with paused_client(repo, "stamp") as (process, resume):
        h.git(repo, "commit", "--allow-empty", "-qm", "head moves")
        finish(process, resume)
    assert h.note_on(repo, original) is not None
    assert h.note_on(repo) is None
    created = [e for e in h.log_entries(repo) if e["event"] == "note-created"]
    assert created[-1]["sha"] == original


def test_repeated_stamp_preserves_existing_sessions(tmp_path):
    repo = h.make_repo(tmp_path / "repo")
    mark(repo)
    assert client(repo, "stamp").returncode == 0
    original = h.note_on(repo)["sessions"][0]
    mark(repo, "second")
    assert client(repo, "stamp").returncode == 0
    note = h.note_on(repo)
    assert note["v"] == 1
    assert {s["session_id"] for s in note["sessions"]} == {"first", "second"}
    assert original in note["sessions"]
    assert all(set(s) == {"tool", "session_id", "stamped_at"} for s in note["sessions"])


@pytest.mark.parametrize(
    "body",
    [
        '{"v":1,"sessions":[]} trailing',
        '{"v":2,"sessions":[]}',
        '{"v":true,"sessions":[]}',
        '{"v":1,"sessions":[{"tool":"codex","session_id":"old"}]}',
        '{"v":1,"sessions":[],"private":"secret"}',
    ],
)
def test_stamp_refuses_unknown_or_malformed_note(tmp_path, body):
    repo = h.make_repo(tmp_path / "repo")
    mark(repo)
    h.git(repo, "notes", f"--ref={h.NOTES_REF}", "add", "-m", body)
    result = client(repo, "stamp")
    assert result.returncode == 0
    assert "note_invalid" in result.stderr
    assert "secret" not in result.stderr
    assert h.git(repo, "notes", f"--ref={h.NOTES_REF}", "show") == body
    assert [m["session_id"] for m in markers(repo)] == ["first"]


@pytest.mark.parametrize("phase", ["before", "after"])
def test_interrupted_stamp_releases_locks_without_partial_marker_file(tmp_path, phase):
    repo = h.make_repo(tmp_path / "repo")
    mark(repo)
    before = markers(repo)
    with paused_client(repo, "stamp", phase=phase) as (process, _):
        process.terminate()
        process.communicate(timeout=5)
    started = [e for e in h.log_entries(repo) if e["event"] == "stamp-started"]
    assert [e["sha"] for e in started] == [h.git(repo, "rev-parse", "HEAD")]
    assert markers(repo) == before
    assert (h.note_on(repo) is not None) == (phase == "after")
    mark(repo, "later")
    assert client(repo, "stamp").returncode == 0
    assert {s["session_id"] for s in h.note_on(repo)["sessions"]} == {"first", "later"}
    assert not markers(repo)


def test_linked_worktrees_and_reconcile_share_notes_mutex(tmp_path):
    repo = h.make_repo(tmp_path / "repo")
    linked = tmp_path / "linked"
    h.git(repo, "worktree", "add", "--detach", str(linked), "HEAD")
    remote = tmp_path / "remote.git"
    h.git(repo, "init", "--bare", str(remote))
    h.git(repo, "remote", "add", "origin", str(remote))
    mark(repo, "first")
    mark(linked, "second")
    with paused_client(repo, "stamp") as (process, resume):
        competing = client(linked, "stamp")
        assert "stamp_busy" in competing.stderr
        assert competing.returncode == 0
        reconciliation = client(linked, "repair-notes", "origin")
        assert "notes_reconcile_busy" in reconciliation.stderr
        assert reconciliation.returncode == 1
        assert [m["session_id"] for m in markers(linked)] == ["second"]
        finish(process, resume)
    assert client(linked, "stamp").returncode == 0
    assert {s["session_id"] for s in h.note_on(repo)["sessions"]} == {"first", "second"}
    assert client(linked, "repair-notes", "origin").returncode == 0
    assert {s["session_id"] for s in h.note_on(repo)["sessions"]} == {"first", "second"}


def test_doctor_does_not_create_marker_state(tmp_path):
    repo = h.make_repo(tmp_path / "repo")
    before = set(h.git_dir(repo).iterdir())
    mod = h._load_module()
    findings = []
    mod._doctor_markers(findings, "repo", repo)
    assert findings == [(mod.DOCTOR_OK, "markers[repo]", "no unconsumed markers")]
    assert set(h.git_dir(repo).iterdir()) == before


def test_doctor_reports_refreshed_generation_as_pending(tmp_path):
    repo = h.make_repo(tmp_path / "repo")
    mark(repo)
    with paused_client(repo, "stamp") as (process, resume):
        mark(repo)
        finish(process, resume)
    before = h.marker_file(repo).read_bytes()
    mod = h._load_module()
    findings = []
    mod._doctor_markers(findings, "repo", repo)
    assert findings[0][0] == mod.DOCTOR_INFO
    assert "pending generation" in findings[0][2]
    assert "commit note" in findings[0][2]
    assert h.marker_file(repo).read_bytes() == before


def test_concurrent_marks_and_squash_preserve_live_entries(tmp_path):
    repo = h.make_repo(tmp_path / "repo")
    source = {
        "v": 1,
        "sessions": [
            {
                "tool": "codex",
                "session_id": session,
                "stamped_at": "2019-01-01T00:00:00+00:00",
            }
            for session in ("first", "squashed")
        ],
    }
    h.git(repo, "notes", f"--ref={h.NOTES_REF}", "add", "-m", json.dumps(source))
    (h.git_dir(repo) / "SQUASH_MSG").write_text(
        f"commit {h.git(repo, 'rev-parse', 'HEAD')}\n"
    )
    mark(repo)
    with paused_client(
        repo, "union-squash-notes", "unused", "message", operation="show", phase="after"
    ) as (process, resume):
        publishers = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    _CLIENT,
                    str(h.SCRIPT),
                    "{}",
                    "mark",
                    "--tool",
                    "codex",
                ],
                cwd=repo,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(8)
        ]
        for index, publisher in enumerate(publishers):
            publisher.stdin.write(
                json.dumps({"session_id": f"parallel-{index}", "cwd": str(repo)})
            )
            publisher.stdin.close()
            publisher.stdin = None
        for publisher in publishers:
            stdout, stderr = publisher.communicate(timeout=10)
            assert publisher.returncode == 0, (stdout, stderr)
            assert not stderr
        mark(repo)
        live = {m["session_id"]: m for m in markers(repo)}
        finish(process, resume)
    after = {m["session_id"]: m for m in markers(repo)}
    assert len(markers(repo)) == 10
    assert set(after) == {"first", "squashed", *(f"parallel-{i}" for i in range(8))}
    assert all(after[key] == entry for key, entry in live.items())
    assert all(m["generation"] for m in after.values())


@pytest.mark.parametrize("failure", ["read", "write", "timeout"])
def test_note_command_failure_retains_markers(tmp_path, monkeypatch, capsys, failure):
    repo = h.make_repo(tmp_path / "repo")
    mark(repo)
    mod = h._load_module()
    monkeypatch.chdir(repo)
    run = mod.subprocess.run

    def fail(args, *a, **kwargs):
        operation = "show" if failure == "read" else "add"
        if args[:2] == ["git", "notes"] and operation in args:
            if failure == "timeout":
                raise subprocess.TimeoutExpired(args, 30, stderr="private payload")
            return subprocess.CompletedProcess(args, 1, "", "private payload")
        return run(args, *a, **kwargs)

    monkeypatch.setattr(mod.subprocess, "run", fail)
    assert mod.cmd_stamp() == 0
    expected = {
        "read": "note_read_failed",
        "write": "note_write_failed",
        "timeout": "note_timeout",
    }[failure]
    stderr = capsys.readouterr().err
    assert expected in stderr
    assert "private payload" not in stderr
    assert [m["session_id"] for m in markers(repo)] == ["first"]
    monkeypatch.setattr(mod.subprocess, "run", run)
    assert h.note_on(repo) is None
    assert mod.cmd_stamp() == 0
    findings = []
    mod._doctor_log(findings)
    assert all(f[0] == mod.DOCTOR_INFO for f in findings)
    assert expected in findings[0][2]
    live = []
    mod._doctor_markers(live, "repo", repo)
    assert live[0][0] == mod.DOCTOR_OK


def test_marker_replace_failure_preserves_population(tmp_path, monkeypatch, capsys):
    repo = h.make_repo(tmp_path / "repo")
    mark(repo)
    before = h.marker_file(repo).read_bytes()
    mod = h._load_module()

    def fail(*args, **kwargs):
        raise OSError("private payload")

    monkeypatch.setattr(mod.os, "replace", fail)
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps({"session_id": "later", "cwd": str(repo)}))
    )
    assert mod.cmd_mark("codex") == 0
    assert h.marker_file(repo).read_bytes() == before
    assert "marker_write_failed" in capsys.readouterr().err
    assert not list(h.git_dir(repo).glob(".sediment-sessions.*.tmp"))


def test_cleanup_failure_retains_successful_snapshot(tmp_path, monkeypatch, capsys):
    repo = h.make_repo(tmp_path / "repo")
    mark(repo)
    mod = h._load_module()
    monkeypatch.chdir(repo)
    unlink = Path.unlink

    def fail(path, *args, **kwargs):
        if path.name == "sediment-sessions":
            raise OSError("private payload")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail)
    assert mod.cmd_stamp() == 0
    assert [m["session_id"] for m in markers(repo)] == ["first"]
    assert h.note_on(repo)["sessions"][0]["session_id"] == "first"
    assert "stamp_cleanup_failed" in capsys.readouterr().err


def test_reconcile_merge_blocks_stamps_across_worktrees(tmp_path):
    repo = h.make_repo(tmp_path / "repo")
    remote = tmp_path / "remote.git"
    h.git(repo, "init", "--bare", str(remote))
    h.git(repo, "remote", "add", "origin", str(remote))
    h.git(repo, "push", "-q", "origin", "HEAD")
    other = tmp_path / "other"
    h.git(tmp_path, "clone", "-q", str(remote), str(other))
    h.git(other, "config", "user.email", "t@local")
    h.git(other, "config", "user.name", "t")
    mark(other, "remote")
    assert client(other, "stamp").returncode == 0
    h.git(other, "push", "-q", "origin", h.NOTES_REF)
    mark(repo, "local")
    assert client(repo, "stamp").returncode == 0
    linked = tmp_path / "linked"
    h.git(repo, "worktree", "add", "--detach", str(linked), "HEAD")
    with paused_client(repo, "repair-notes", "origin", operation="merge") as (
        process,
        resume,
    ):
        mark(linked, "later")
        result = client(linked, "stamp")
        assert result.returncode == 0
        assert "stamp_busy" in result.stderr
        assert [m["session_id"] for m in markers(linked)] == ["later"]
        finish(process, resume)
    assert client(linked, "stamp").returncode == 0
    assert {s["session_id"] for s in h.note_on(repo)["sessions"]} == {
        "local",
        "remote",
        "later",
    }


@pytest.mark.parametrize("cleanup", [False, True])
def test_directory_sync_failure_reports_unconfirmed_complete_state(
    tmp_path, monkeypatch, capsys, cleanup
):
    repo = h.make_repo(tmp_path / "repo")
    mark(repo)
    mod = h._load_module()

    def fail(directory):
        raise OSError("private payload")

    monkeypatch.setattr(mod, "_sync_directory", fail)
    monkeypatch.chdir(repo)
    if cleanup:
        assert mod.cmd_stamp() == 0
        assert not h.marker_file(repo).exists()
        assert h.note_on(repo) is not None
        expected = "stamp_cleanup_unconfirmed"
    else:
        monkeypatch.setattr(
            sys,
            "stdin",
            io.StringIO(json.dumps({"session_id": "later", "cwd": str(repo)})),
        )
        assert mod.cmd_mark("codex") == 0
        assert {m["session_id"] for m in markers(repo)} == {"first", "later"}
        expected = "marker_durability_unconfirmed"
    assert expected in capsys.readouterr().err


def test_real_git_ref_failure_retains_marker(tmp_path):
    repo = h.make_repo(tmp_path / "repo")
    mark(repo)
    notes_dir = h.git_dir(repo) / "refs" / "notes"
    notes_dir.mkdir(parents=True)
    locked = notes_dir / "sediment.lock"
    locked.write_text("external writer\n")
    result = client(repo, "stamp")
    assert result.returncode == 0
    assert "note_write_failed" in result.stderr
    assert [m["session_id"] for m in markers(repo)] == ["first"]
    assert h.note_on(repo) is None
    locked.unlink()
    assert client(repo, "stamp").returncode == 0
    assert not markers(repo)


def test_legacy_duplicate_snapshot_is_persisted_before_git(tmp_path):
    repo = h.make_repo(tmp_path / "repo")
    entry = {
        "tool": "codex",
        "session_id": "legacy",
        "stamped_at": "2019-01-01T00:00:00+00:00",
    }
    h.marker_file(repo).write_text((json.dumps(entry) + "\n") * 2)
    with paused_client(repo, "stamp") as (process, resume):
        [normalized] = markers(repo)
        assert normalized == {**entry, "generation": normalized["generation"]}
        mark(repo, "legacy")
        refreshed = markers(repo)
        assert refreshed[0]["generation"] != normalized["generation"]
        finish(process, resume)
    assert markers(repo) == refreshed
    assert set(h.note_on(repo)["sessions"][0]) == {"tool", "session_id", "stamped_at"}


def test_marker_lock_deadline_and_stable_inode(tmp_path):
    repo = h.make_repo(tmp_path / "repo")
    mark(repo)
    before = markers(repo)
    lock_path = h.git_dir(repo) / "sediment-sessions.lock"
    inode = lock_path.stat().st_ino
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        start = time.monotonic()
        result = client(
            repo,
            "mark",
            "--tool",
            "codex",
            stdin=json.dumps({"session_id": "later", "cwd": str(repo)}),
        )
        assert time.monotonic() - start < 3
        assert result.returncode == 0
        assert "marker_busy" in result.stderr
        assert markers(repo) == before
    assert client(repo, "stamp").returncode == 0
    assert not h.marker_file(repo).exists()
    mark(repo, "later")
    assert lock_path.stat().st_ino == inode


def test_file_sync_failure_leaves_previous_complete_population(
    tmp_path, monkeypatch, capsys
):
    repo = h.make_repo(tmp_path / "repo")
    mark(repo)
    before = h.marker_file(repo).read_bytes()
    mod = h._load_module()

    def fail(fd):
        raise OSError("private payload")

    monkeypatch.setattr(mod.os, "fsync", fail)
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps({"session_id": "later", "cwd": str(repo)}))
    )
    assert mod.cmd_mark("codex") == 0
    assert h.marker_file(repo).read_bytes() == before
    assert "marker_write_failed" in capsys.readouterr().err
    assert not list(h.git_dir(repo).glob(".sediment-sessions.*.tmp"))


def test_unwritable_diagnostic_path_remains_fail_soft(tmp_path, monkeypatch, capsys):
    repo = h.make_repo(tmp_path / "repo")
    mark(repo)
    mod = h._load_module()
    monkeypatch.chdir(repo)
    monkeypatch.setenv("SEDIMENT_ATTRIBUTION_LOG", str(tmp_path))
    assert mod.cmd_stamp() == 0
    assert [m["session_id"] for m in markers(repo)] == ["first"]
    assert "stamp_log_failed" in capsys.readouterr().err


def test_malformed_marker_row_is_skipped_and_healed(tmp_path):
    repo = h.make_repo(tmp_path / "repo")
    good = {
        "tool": "codex",
        "session_id": "good",
        "stamped_at": "2019-01-01T00:00:00+00:00",
    }
    h.marker_file(repo).write_text(
        json.dumps(good) + "\n" + '{"tool":"codex","session_id":"trunc'
    )
    mark(repo, "later")
    assert {m["session_id"] for m in markers(repo)} == {"good", "later"}
    assert all(m["generation"] for m in markers(repo))
    assert any(e["event"] == "marker_rows_skipped" for e in h.log_entries(repo))
    assert client(repo, "stamp").returncode == 0
    assert {s["session_id"] for s in h.note_on(repo)["sessions"]} == {"good", "later"}
    assert not markers(repo)


def test_notes_lock_lives_in_common_dir_from_any_work_tree(tmp_path):
    repo = h.make_repo(tmp_path / "repo")
    linked = tmp_path / "linked"
    h.git(repo, "worktree", "add", "--detach", str(linked), "HEAD")
    (repo / "sub").mkdir()
    (linked / "sub").mkdir()
    mod = h._load_module()
    expected = (h.git_dir(repo) / "sediment-notes.lock").resolve()
    assert mod._notes_lock_path(repo / "sub").resolve() == expected
    assert mod._notes_lock_path(linked / "sub").resolve() == expected


@pytest.mark.parametrize("mark_before_stamp", [False, True])
def test_invalid_utf8_marker_rows_never_reach_notes(tmp_path, mark_before_stamp):
    repo = h.make_repo(tmp_path / "repo")
    healthy = {"tool": "codex", "session_id": "healthy-\ufffd-\u00e9"}
    h.marker_file(repo).write_bytes(
        b'{"tool":"codex","session_id":"damaged-\xff"}\r\n'
        + json.dumps(healthy, ensure_ascii=False).encode("utf-8")
        + b'\r\n{"tool":"damaged-\xc3","session_id":"other"}\r\n'
    )
    expected = {("codex", healthy["session_id"])}
    if mark_before_stamp:
        mark(repo, "later")
        expected.add(("codex", "later"))
    assert client(repo, "stamp").returncode == 0
    assert {
        (entry["tool"], entry["session_id"]) for entry in h.note_on(repo)["sessions"]
    } == expected
    receipts = [
        event
        for event in h.log_entries(repo)
        if event["event"] == "marker_rows_skipped"
    ]
    assert receipts
    assert all(event["count"] == "2" for event in receipts)
    assert not markers(repo)
