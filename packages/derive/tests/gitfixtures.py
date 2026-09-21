# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared real-git fixture helpers for the derive test suite (per AGENTS.md:
real fixtures, never mocked). One copy for test_mirror.py and
test_derive_attribution.py, so a fixture fix (e.g. a new git default, a CI-runner
identity quirk) can't land in one file and silently miss the other.

Every fixture repo sets a local user.name/user.email before committing: CI
runners have no global git identity, so a fixture that skips this fails
only in CI.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

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


def run_git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def commit_all(work: Path, message: str) -> str:
    run_git(work, "add", "-A")
    run_git(work, "commit", "-q", "-m", message)
    return run_git(work, "rev-parse", "HEAD").strip()


def make_work_repo(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    run_git(work, "init", "-q", "-b", "main")
    run_git(work, "config", "user.email", "dev@example.com")
    run_git(work, "config", "user.name", "Dev")
    return work


def make_remote(tmp_path: Path, work: Path) -> Path:
    """A bare remote holding everything the work repo has — branches, notes
    (when stamped; the glob push refspec is a no-op otherwise), and a
    synthetic PR head ref, like a forge would serve."""
    remote = tmp_path / "remote.git"
    run_git(tmp_path, "init", "-q", "--bare", str(remote))
    run_git(work, "push", "-q", str(remote), "refs/heads/*:refs/heads/*")
    run_git(work, "push", "-q", str(remote), "refs/notes/*:refs/notes/*")
    head = run_git(work, "rev-parse", "HEAD").strip()
    run_git(remote, "update-ref", "refs/pull/1/head", head)
    return remote
