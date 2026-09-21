# SPDX-License-Identifier: AGPL-3.0-or-later
"""Golden --help tests.

One test walks the argparse tree, invokes every installed ``sediment --help``
path, and diffs it against a checked-in golden in
``cli/tests/testdata/help/``. Help drift becomes
a red diff in review (coder's pattern, for 155 commands — this is the same
mechanism, scoped to ours).  Regenerate with ``--update-goldens``.

Every golden therefore pins console entry-point and pre-parser dispatch behavior,
not only the underlying Python parser.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import sys

import pytest

from sediment_cli.cli import _REPORTS, build_parser, main

GOLDEN_DIR = Path(__file__).parent / "testdata" / "help"
_DISPATCH_ONLY = {"report", "mirror-gc", "install", "uninstall", "doctor", "delivery"}
_DISPATCH_PATHS = [
    ("report",),
    ("mirror-gc",),
    ("install",),
    ("uninstall",),
    ("doctor",),
    ("delivery",),
    *(("delivery", name) for name in ("enqueue", "status", "replay")),
    *(("report", name) for name in _REPORTS),
]


def _subparsers_action(parser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _iter_parsers(parser, prefix=()):
    """Yield (command-path, parser) for the top parser and every subcommand,
    depth-first.  Leaf parsers carry no ``_SubParsersAction``."""
    yield prefix, parser
    action = _subparsers_action(parser)
    if action is None:
        return
    for name, sub in action.choices.items():
        yield from _iter_parsers(sub, prefix + (name,))


def _golden_name(path) -> str:
    return "-".join(path) if path else "sediment"


def _installed_help(path) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [str(Path(sys.executable).with_name("sediment")), *path, "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert result.stderr == ""
    return result


def test_help_matches_goldens(request) -> None:
    top = build_parser()
    update = request.config.getoption("--update-goldens")
    failures: list[str] = []
    seen: set[str] = set()

    for path, _parser in _iter_parsers(top):
        if path and path[0] in _DISPATCH_ONLY:
            continue
        name = _golden_name(path)
        assert name not in seen, f"duplicate golden name {name}"
        seen.add(name)
        actual = _installed_help(path).stdout
        golden = GOLDEN_DIR / f"{name}.txt"
        if update:
            golden.parent.mkdir(parents=True, exist_ok=True)
            golden.write_text(actual, encoding="utf-8")
            continue
        if not golden.exists():
            failures.append(f"{name}: golden missing — run pytest --update-goldens")
            continue
        expected = golden.read_text(encoding="utf-8")
        if actual != expected:
            failures.append(f"{name}: help drifted — run pytest --update-goldens")

    for path in _DISPATCH_PATHS:
        name = _golden_name(path)
        assert name not in seen, f"duplicate golden name {name}"
        seen.add(name)
        actual = _installed_help(path).stdout
        golden = GOLDEN_DIR / f"{name}.txt"
        if update:
            golden.parent.mkdir(parents=True, exist_ok=True)
            golden.write_text(actual, encoding="utf-8")
            continue
        if not golden.exists():
            failures.append(f"{name}: golden missing — run pytest --update-goldens")
            continue
        expected = golden.read_text(encoding="utf-8")
        if actual != expected:
            failures.append(f"{name}: help drifted — run pytest --update-goldens")

    if failures:
        pytest.fail("\n".join(failures))


def _invoke(argv, capsys):
    """Run ``main`` through an argparse error and return (exit code, output).
    Missing/invalid subcommands raise ``SystemExit(2)`` from ``error()``."""
    with pytest.raises(SystemExit) as exc:
        main(argv)
    captured = capsys.readouterr()
    return exc.value.code, captured


@pytest.mark.parametrize(
    "path",
    _DISPATCH_PATHS[1:],
)
def test_dispatched_runtime_help_uses_public_command_path(path) -> None:
    captured = _installed_help(path)

    assert captured.stdout.startswith("USAGE:\n")
    assert f"sediment {' '.join(path)}" in captured.stdout.splitlines()[1]


def test_report_registry_uses_attribution_vocabulary() -> None:
    captured = _installed_help(("report",))

    assert "per-model attribution/CI/acceptance" in captured.stdout
    assert "per-model survival/CI/acceptance" not in captured.stdout


def test_bare_invocation_prints_full_help_to_stderr(capsys) -> None:
    code, captured = _invoke([], capsys)
    assert code == 2
    assert captured.out == ""
    assert "COMMANDS:" in captured.err
    assert (
        "sediment: error: the following arguments are required: <command>"
        in captured.err
    )


def test_invalid_subcommand_prints_help_and_names_it(capsys) -> None:
    code, captured = _invoke(["nope"], capsys)
    assert code == 2
    assert captured.out == ""
    assert "COMMANDS:" in captured.err
    assert "invalid command 'nope'" in captured.err
    # The COMMANDS listing replaces argparse's choices parenthetical.
    assert "(choose from" not in captured.err


def test_bare_export_prints_formats_listing(capsys) -> None:
    code, captured = _invoke(["export"], capsys)
    assert code == 2
    assert "FORMATS:" in captured.err
    assert (
        "sediment export: error: the following arguments are required: <format>"
        in captured.err
    )


def test_invalid_format_is_named_a_format_not_a_command(capsys) -> None:
    # The diagnostic takes its noun from the level's own metavar, so the
    # listing above it and the word below it agree.
    code, captured = _invoke(["export", "nope"], capsys)
    assert code == 2
    assert "FORMATS:" in captured.err
    assert "invalid format 'nope'" in captured.err


def test_help_on_stderr_is_plain_when_stderr_is_redirected(monkeypatch, capsys) -> None:
    """`sediment nope 2>err.log` from a terminal must not write escape codes
    into that file. The color pass gates on the destination stream, so a TTY
    stdout must not style what goes to a non-TTY stderr."""

    class _Tty:
        def isatty(self):
            return True

        def write(self, text):
            return len(text)

        def flush(self):
            return None

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(sys, "stdout", _Tty())
    with pytest.raises(SystemExit):
        main(["nope"])
    monkeypatch.undo()
    assert "\x1b[" not in capsys.readouterr().err


def test_export_bad_flag_keeps_terse_error(capsys) -> None:
    code, captured = _invoke(["export", "rlvr", "--nope"], capsys)
    assert code == 2
    assert "FORMATS:" not in captured.err
    assert "error:" in captured.err


def test_export_rlvr_requires_explicit_target_and_points_to_guide(capsys) -> None:
    code, captured = _invoke(["export", "rlvr", "--out", "/tmp/out"], capsys)
    assert code == 2
    assert "--target" in captured.err
    assert "sediment" in captured.err
    assert "swe-bench" in captured.err
    assert "nemo-gym" in captured.err
    assert "docs/exports/rlvr-export.md" in captured.err


def test_bare_export_rlvr_target_omission_points_to_guide(capsys) -> None:
    code, captured = _invoke(["export", "rlvr"], capsys)
    assert code == 2
    assert "--out" in captured.err
    assert "--target" in captured.err
    assert "sediment" in captured.err
    assert "swe-bench" in captured.err
    assert "nemo-gym" in captured.err
    assert "docs/exports/rlvr-export.md" in captured.err


def test_export_recipe_defaults_are_conservative_and_opt_ins_are_explicit() -> None:
    parser = build_parser()

    assert parser.parse_args(["export", "dpo", "--out", "out"]).recipe == ("dpo_human")
    assert (
        parser.parse_args(
            ["export", "dpo", "--out", "out", "--recipe", "dpo_outcome"]
        ).recipe
        == "dpo_outcome"
    )
    assert parser.parse_args(["export", "sft", "--out", "out"]).recipe == (
        "sft_curated"
    )
    assert (
        parser.parse_args(
            ["export", "diff-sft", "--out", "out", "--recipe", "sft_verified"]
        ).recipe
        == "sft_verified"
    )
    assert parser.parse_args(["export", "recovery", "--out", "out"]).recipe == (
        "recovery_ci"
    )
