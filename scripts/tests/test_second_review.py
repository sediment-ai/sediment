# SPDX-License-Identifier: AGPL-3.0-or-later
"""second_review fails closed: missing/non-executable codex, bad base,
empty diff — all exit 2 with a message, never a traceback."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "second_review.py"
ROOT = SCRIPT.parents[1]


def _run(*args, cwd=ROOT):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=cwd,
    )


def test_missing_codex_binary_exits_2_with_message():
    result = _run("--codex-bin", "definitely-not-codex-xyz")
    assert result.returncode == 2
    assert "codex CLI not found" in result.stderr


def test_non_executable_codex_bin_exits_2_not_traceback():
    # A real file that is not executable must fail closed, not FileNotFoundError.
    result = _run("--codex-bin", "pyproject.toml")
    assert result.returncode == 2
    assert "not found or not executable" in result.stderr


def test_bad_base_ref_exits_2_with_gits_explanation():
    # sys.executable passes the codex-resolution step; git then fails loudly.
    result = _run("--codex-bin", sys.executable, "--base", "no-such-ref-xyz")
    assert result.returncode == 2
    assert "git diff against" in result.stderr


def test_empty_diff_exits_2():
    result = _run("--codex-bin", sys.executable, "--base", "HEAD")
    assert result.returncode == 2
    assert "empty diff" in result.stderr


def test_help_runs_without_codex_or_git():
    result = _run("--help")
    assert result.returncode == 0
    assert "--base" in result.stdout


@pytest.mark.parametrize(
    "final,code,expected",
    [
        (None, 0, 2),
        ("", 0, 2),
        ("invalid_request_error", 0, 2),
        ('{"findings": []}', 0, 0),
        (
            '{"findings": [{"location": "store.py:10", "problem": "Loses a Fact on retry"}]}',
            0,
            0,
        ),
        ('{"findings": [{}]}', 0, 2),
        ('{"findings": []}', 7, 7),
    ],
)
def test_review_requires_a_completed_result(tmp_path, final, code, expected):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for version in ("before", "after"):
        (repo / "sample").write_text(version)
        subprocess.run(["git", "add", "sample"], cwd=repo, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-qm",
                version,
            ],
            cwd=repo,
            check=True,
        )
    stub = tmp_path / "codex-stub"
    stub.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, sys\n"
        f"final = {final!r}\n"
        "if final is not None and '--output-last-message' in sys.argv:\n"
        "    pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text(final)\n"
        "print('invalid_request_error: unsupported model' if final is None else 'finished')\n"
        f"sys.exit({code})\n"
    )
    stub.chmod(0o700)
    result = _run(
        "--base", "HEAD~1", "--codex-bin", str(stub), "--model", "test-model", cwd=repo
    )
    assert result.returncode == expected
    if expected == 0:
        assert "model=test-model" in result.stdout
        if not json.loads(final)["findings"]:
            assert "no blocking findings" in result.stdout
    elif code == 0:
        assert "completed review result" in result.stderr
