# SPDX-License-Identifier: AGPL-3.0-or-later
"""Check a commit subject against this repo's Conventional Commits dialect.

Two callers share one vocabulary:

- ``scripts/hooks/commit-msg`` (opt-in, per developer) checks the message
  file git hands it, so a bad subject never reaches a push;
- CI checks the **pull request title**. The repo squash-merges, so the PR
  title is the subject that lands on main — intermediate commits do not.

The dialect is `Conventional Commits v1.0.0
<https://www.conventionalcommits.org/en/v1.0.0/>`_ with the scope
vocabulary closed to the AGENTS.md package map. Closed, because an open
scope list is one an agent invents a new member of every commit.

Usage:
    uv run python scripts/check_commit_msg.py .git/COMMIT_EDITMSG
    uv run python scripts/check_commit_msg.py --title "feat(core): ..."
    ... | uv run python scripts/check_commit_msg.py
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

TYPES = (
    "build",
    "chore",
    "ci",
    "docs",
    "feat",
    "fix",
    "perf",
    "refactor",
    "revert",
    "style",
    "test",
)

# The package map (AGENTS.md), plus the three surfaces that are not packages
# but do own commits: scripts/, sim/, and dependency bumps.
SCOPES = (
    "api",
    "capture",
    "cli",
    "core",
    "deps",
    "derive",
    "export",
    "scripts",
    "shims",
    "sim",
)

# Subjects git or GitHub wrote rather than a person. Checking these would
# fail messages nobody typed and cannot edit.
_GENERATED = re.compile(r'^(Merge |Revert "|fixup! |squash! |amend! )')

_HEADER = re.compile(
    r"^(?P<type>[a-z]+)"
    r"(?:\((?P<scope>[a-z0-9-]+)\))?"
    r"(?P<breaking>!)?"
    r": (?P<description>.+)$"
)

# GitHub appends the PR number when it squashes. That suffix is not part of
# the description anyone wrote, so it does not count against the cap below.
_PR_SUFFIX = re.compile(r"\s*\(#\d+\)$")

# Soft: long subjects are a readability drift, not a defect worth blocking a
# contributor over. Warned, never failed.
SUBJECT_SOFT_CAP = 72

_FORMAT = "type(scope): description  — scope and the ! breaking marker optional"


def check(message: str) -> tuple[list[str], list[str]]:
    """Return (failures, warnings) for a commit message or a bare subject."""
    lines = [line for line in message.splitlines() if not line.startswith("#")]
    subject = lines[0].strip() if lines else ""
    if not subject or _GENERATED.match(subject):
        # An empty message is git's to reject; a generated one is nobody's
        # to fix.
        return [], []

    failures: list[str] = []
    warnings: list[str] = []

    # A body run onto line 2 is read by git as part of the subject, so the
    # blank line is a correctness rule, not a style one.
    if len(lines) > 1 and lines[1].strip():
        failures.append("a blank line must separate the subject from the body")

    header = _HEADER.match(subject)
    if header is None:
        failures.append(f"not a conventional commit subject: {subject!r}")
        failures.append(f"expected: {_FORMAT}")
        failures.append(f"types: {', '.join(TYPES)}")
        return failures, warnings

    if header["type"] not in TYPES:
        failures.append(f"unknown type {header['type']!r} — one of: {', '.join(TYPES)}")
    if header["scope"] is not None and header["scope"] not in SCOPES:
        failures.append(
            f"unknown scope {header['scope']!r} — one of: {', '.join(SCOPES)}. "
            "Widen SCOPES in scripts/check_commit_msg.py only alongside a real "
            "new surface."
        )

    description = _PR_SUFFIX.sub("", header["description"]).strip()
    if not description:
        failures.append("description is empty")
    elif description.endswith("."):
        failures.append("description must not end with a period")

    trimmed = _PR_SUFFIX.sub("", subject)
    if len(trimmed) > SUBJECT_SOFT_CAP:
        warnings.append(
            f"subject is {len(trimmed)} chars; {SUBJECT_SOFT_CAP} keeps it "
            "readable in `git log --oneline`"
        )

    return failures, warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "path", nargs="?", type=Path, help="commit message file (git passes this)"
    )
    parser.add_argument("--title", help="check this string — CI passes the PR title")
    args = parser.parse_args(argv)

    if args.title is not None:
        # An empty subject is git's to reject when it owns the message file,
        # so check() stays silent on one. A caller passing --title has no
        # such backstop: silence there is a check that ran and enforced
        # nothing, which is worse than no check at all.
        if not args.title.strip():
            print("FAIL: --title is empty")
            return 1
        message = args.title
    elif args.path is not None:
        message = args.path.read_text()
    else:
        message = sys.stdin.read()

    failures, warnings = check(message)
    for warning in warnings:
        print(f"WARN: {warning}")
    for failure in failures:
        print(f"FAIL: {failure}")
    if failures:
        print("See CONTRIBUTING.md §Commits.")
        return 1
    print("commit subject OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
