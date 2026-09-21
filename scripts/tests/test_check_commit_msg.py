# SPDX-License-Identifier: AGPL-3.0-or-later
"""The commit-subject checker (CONTRIBUTING.md §Commits).

The failure this pins: the checker and the documented dialect drift apart,
so CI either rejects a subject the contributing guide told someone to write
or accepts one it did not. The type and scope vocabularies are the contract
— a PR title is checked against them and nothing else.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from check_commit_msg import SCOPES, TYPES, check  # noqa: E402

REPO_ROOT = Path(__file__).parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_commit_msg.py"


def failures(message: str) -> list[str]:
    return check(message)[0]


def warnings(message: str) -> list[str]:
    return check(message)[1]


def test_accepts_the_documented_shapes():
    for subject in (
        "feat(core): validate example record inputs",
        "docs: clarify setup steps",
        "fix(capture)!: reject invalid example payloads",
        "chore(deps): update an example dependency",
        # GitHub appends the PR number when it squashes.
        "feat(cli): add an example command (#123)",
    ):
        assert failures(subject) == [], subject


def test_every_declared_scope_is_accepted():
    for scope in SCOPES:
        assert failures(f"fix({scope}): something") == [], scope


def test_every_declared_type_is_accepted():
    for type_ in TYPES:
        assert failures(f"{type_}: something") == [], type_


def test_rejects_a_bare_subject():
    assert failures("Align logo text to the left in README")


def test_rejects_a_package_name_used_as_a_type():
    # The drift the convention replaces: `capture:` is a scope, not a type.
    problems = failures("capture: refused edits ship the declined text")
    assert any("unknown type" in p for p in problems)


def test_rejects_an_invented_scope():
    problems = failures("feat(notifications): add a webhook")
    assert any("unknown scope" in p for p in problems)


def test_rejects_a_trailing_period_and_an_empty_description():
    assert any("period" in p for p in failures("docs: fix the thing."))
    assert failures("docs: ")


def test_rejects_a_body_run_onto_the_second_line():
    problems = failures("feat(core): add a field\nthe body starts here")
    assert any("blank line" in p for p in problems)
    assert failures("feat(core): add a field\n\nthe body starts here") == []


def test_skips_what_git_and_github_write():
    for subject in (
        "Merge pull request #123 from example-org/example-branch",
        'Revert "docs: rewrite the client capture contract"',
        "fixup! feat(core): add a field",
        "",
    ):
        assert failures(subject) == [], subject


def test_comment_lines_are_ignored():
    # What git hands the hook: the subject, then its own commentary.
    message = "docs: rewrite the contract\n# Please enter the commit message"
    assert failures(message) == []


def test_long_subject_warns_but_does_not_fail():
    long = "feat(core): " + "x" * 80
    assert failures(long) == []
    assert warnings(long)
    # The squash suffix is GitHub's, so it does not count against the cap.
    assert warnings("docs: " + "x" * 60 + " (#1234)") == []


def test_an_empty_title_fails_rather_than_passing_silently():
    # check() stays silent on an empty message because git rejects one
    # itself. --title has no such backstop, so the CLI fails instead — a
    # check that silently enforces nothing is worse than no check.
    assert check("")[0] == []
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--title", "  "],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "empty" in result.stdout


def test_cli_reads_a_title_a_file_and_stdin(tmp_path):
    def run(args: list[str], stdin: str = "") -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            input=stdin,
            capture_output=True,
            text=True,
        )

    assert run(["--title", "docs: rewrite the contract"]).returncode == 0
    assert run(["--title", "rewrite the contract"]).returncode == 1

    message = tmp_path / "COMMIT_EDITMSG"
    message.write_text("feat(api): add a route\n")
    assert run([str(message)]).returncode == 0

    assert run([], stdin="docs: rewrite the contract").returncode == 0
