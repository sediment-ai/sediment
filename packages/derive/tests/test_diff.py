# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the unified diff parser and the code-file filter, against the
same wire-captured GitHub .diff fixture the mirror tests compare to."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from gitfixtures import commit_all, make_work_repo, run_git
from sediment_derive import diff as diff_module

from sediment_derive.diff import is_code_file, parse_unified_diff

FIXTURE = Path(__file__).parent / "fixtures" / "github_diff.json"


def test_parse_real_diff_extracts_added_code_lines() -> None:
    file_diffs = parse_unified_diff(json.loads(FIXTURE.read_text())["diff"])
    by_path = {fd.file_path: fd for fd in file_diffs}

    assert "app/math_utils.py" in by_path
    added = by_path["app/math_utils.py"].added_lines
    assert "def fibonacci(n: int) -> int:" in added
    assert "return fibonacci(n - 1) + fibonacci(n - 2)" in added


def test_readme_is_filtered_as_non_code() -> None:
    assert is_code_file("app/math_utils.py") is True
    assert is_code_file("README.md") is False


def test_lockfiles_are_skipped_even_with_code_extension() -> None:
    assert is_code_file("app/bundle.min.js") is False
    assert is_code_file("uv.lock") is False


@pytest.mark.parametrize("quote_path", ["true", "false"])
def test_real_git_paths_and_hunk_additions_remain_exact(tmp_path: Path, quote_path):
    work = make_work_repo(tmp_path)
    files = {
        "plain.py": "++counter;\n+++header_like;\n++ b/not-a-file.py\n",
        "space file.py": "space_name = 1\n",
        'quote"file.py': "quoted_name = 2\n",
        "tab\tfile.py": "tab_name = 3\n",
        "slash\\file.py": "slash_name = 4\n",
        "line\nfile.py": "newline_name = 5\n",
        "café.py": "unicode_name = 6\n",
        "space b/part.py": "ambiguous_header = 7\n",
        "empty.py": "",
        "no-newline.py": "without_newline = 8",
        "control.py": "literal = 'a\vb\fc'\n",
    }
    for name, content in files.items():
        path = work / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(content)
    head = commit_all(work, "path grammar")
    raw = run_git(
        work,
        "-c",
        f"core.quotePath={quote_path}",
        "diff-tree",
        "--root",
        "-p",
        "--no-commit-id",
        head,
    )

    parsed = parse_unified_diff(raw)

    assert {item.file_path: item.added_lines for item in parsed} == {
        name: content.removesuffix("\n") for name, content in files.items()
    }
    result = diff_module.parse_diff_sections(raw)
    assert result.skipped == {}
    assert "".join(section.patch for section in result.sections) == raw
    assert [section.file_path for section in result.sections] == sorted(files)


def test_real_git_rename_deletion_empty_and_mode_sections(tmp_path: Path):
    work = make_work_repo(tmp_path)
    old = 'old "name.py'
    renamed = "renamed\tname.py"
    (work / old).write_text("rename_identity = 1\n" * 12)
    (work / "deleted.py").write_text("delete_identity = 2\n")
    (work / "mode.py").write_text("mode_identity = 3\n")
    base = commit_all(work, "before")
    (work / old).rename(work / renamed)
    (work / "deleted.py").unlink()
    (work / "empty.py").touch()
    (work / "mode.py").chmod(0o755)
    head = commit_all(work, "after")
    raw = run_git(work, "diff", "-M", base, head)

    result = diff_module.parse_diff_sections(raw)

    assert result.skipped == {}
    assert {section.file_path: section.added_lines for section in result.sections} == {
        renamed: "",
        "deleted.py": "",
        "empty.py": "",
        "mode.py": "",
    }
    assert "".join(section.patch for section in result.sections) == raw
    assert "+++ /dev/null" in raw
    assert "rename from" in raw


def _root_patch(tmp_path: Path, name: str, text: str) -> str:
    tmp_path.mkdir()
    work = make_work_repo(tmp_path)
    (work / name).write_text(text)
    head = commit_all(work, "root")
    return run_git(work, "diff-tree", "--root", "-p", "--no-commit-id", head)


@pytest.mark.parametrize(
    "damage",
    [
        "missing_header",
        "wrong_count",
        "broken_hunk",
        "bad_quote",
        "extra_addition",
        "wrong_path",
        "overlapping_hunk",
        "shifted_hunk",
    ],
)
def test_malformed_section_cannot_borrow_neighbor_path_or_partial_additions(
    tmp_path: Path, damage: str, caplog
):
    before = _root_patch(tmp_path / "before", "before.py", "before = 1\n")
    bad = _root_patch(tmp_path / "bad", 'bad"name.py', "private_authored_text = 2\n")
    after = _root_patch(tmp_path / "after", "after.py", "after = 3\n")
    if damage == "missing_header":
        bad = "\n".join(line for line in bad.split("\n") if not line.startswith("+++ "))
    elif damage == "wrong_count":
        bad = bad.replace("+1 @@", "+1,2 @@")
    elif damage == "broken_hunk":
        bad = bad.replace("@@ -0,0 +1 @@", "@@ invalid @@")
    elif damage == "bad_quote":
        bad = bad.replace('\\"name.py', "\\qname.py")
    elif damage == "extra_addition":
        bad += "+unexpected = 4\n"
    elif damage == "overlapping_hunk":
        bad += bad[bad.index("@@") :]
    elif damage == "shifted_hunk":
        bad = bad.replace("+1 @@", "+2 @@")
    else:
        bad = bad.replace('+++ "b/bad\\"name.py"', "+++ b/unrelated.py")

    result = diff_module.parse_diff_sections(before + bad + after)

    assert [
        (section.file_path, section.added_lines) for section in result.sections
    ] == [("before.py", "before = 1"), ("after.py", "after = 3")]
    assert result.skipped == {"malformed_diff_section": 1}
    assert "malformed_diff_section" in caplog.text
    assert "private_authored_text" not in caplog.text


def test_real_git_binary_section_is_counted_without_losing_valid_neighbors(tmp_path):
    work = make_work_repo(tmp_path)
    (work / "a.py").write_text("first = 1\n")
    (work / "binary.py").write_bytes(b"binary\x00data")
    (work / "z.py").write_text("last = 2\n")
    head = commit_all(work, "binary and text")
    for binary_flag in ([], ["--binary"]):
        raw = run_git(work, "show", "--format=", *binary_flag, head)
        result = diff_module.parse_diff_sections(raw)
        assert [(item.file_path, item.added_lines) for item in result.sections] == [
            ("a.py", "first = 1"),
            ("z.py", "last = 2"),
        ]
        assert result.skipped == {"unsupported_diff_section": 1}


def test_real_git_combined_merge_section_is_unsupported(tmp_path):
    import subprocess

    work = make_work_repo(tmp_path)
    (work / "merge.py").write_text("base\n")
    base = commit_all(work, "base")
    run_git(work, "checkout", "-qb", "left")
    (work / "merge.py").write_text("left\n")
    commit_all(work, "left")
    run_git(work, "checkout", "-qb", "right", base)
    (work / "merge.py").write_text("right\n")
    commit_all(work, "right")
    subprocess.run(["git", "merge", "left"], cwd=work, capture_output=True, check=False)
    (work / "merge.py").write_text("both\n")
    merge = commit_all(work, "resolve merge")
    raw = run_git(work, "show", "--cc", "--format=", merge)
    assert raw.startswith("diff --cc ")

    result = diff_module.parse_diff_sections(raw)

    assert result.sections == []
    assert result.skipped == {"unsupported_diff_section": 1}


def test_real_git_renamed_file_with_multiple_hunks_preserves_authored_prefixes(
    tmp_path,
):
    work = make_work_repo(tmp_path)
    original = [f"unchanged_{index} = {index}\n" for index in range(40)]
    (work / "old name.py").write_text("".join(original))
    base = commit_all(work, "base")
    (work / "old name.py").unlink()
    original[0] = "++counter;\n"
    original[-1] = "--authored_text"
    target = 'new"name.py'
    (work / target).write_text("".join(original))
    head = commit_all(work, "rename with edits")
    raw = run_git(work, "diff", "-M", "-U1", base, head)
    assert "rename from" in raw
    assert raw.count("@@ -") == 2
    result = diff_module.parse_diff_sections(raw)
    assert result.skipped == {}
    assert [
        (item.file_path, item.added_lines, item.patch) for item in result.sections
    ] == [(target, "++counter;\n--authored_text", raw)]
