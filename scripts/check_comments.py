# SPDX-License-Identifier: AGPL-3.0-or-later
"""Enforce the minimal-comments policy.

Fails when:

- a non-docstring comment matches an excuse pattern (deferred work,
  workaround language, or a skip/swallow alibi);
- the repository's non-docstring comment count exceeds ``COMMENTS_BUDGET``.

Ruff FIX/TD/ERA/PGH cover Python TODO/FIXME/HACK, commented-out code, and
blanket suppressions. This script covers every code-like file and the
phrases agents use when they annotate a problem instead of solving it.

Allowed comments still count toward the budget: SPDX headers, ``# ponytail:``
ceilings that name an upgrade path, precise ``# noqa: CODE`` / ``# type:
ignore[code]`` / ``# pragma: no cover`` markers, and comments that carry
information the code cannot (upstream bug links, external constraints).

Usage:
    uv run python scripts/check_comments.py
    uv run python scripts/check_comments.py --budget 5800
"""

from __future__ import annotations

import argparse
import io
import re
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Frozen after the audit cleanup. Raise only when a change that earns a
# comment (external constraint, measured ceiling) also deletes enough
# restating comments to stay under the budget — or when the audit's
# remaining inventory is intentionally reduced and the budget follows.
COMMENTS_BUDGET = 5700

SKIP_DIR_NAMES = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    "security",
    "docs",
    ".skills",
}

CODE_SUFFIXES = {".py", ".sh", ".yml", ".yaml", ".toml", ".ts", ".js"}

# Phrases that mark deferred or excused work. ``ponytail:`` is the one
# sanctioned simplification marker and is checked separately.
EXCUSE_RE = re.compile(
    r"(?i)(?:"
    r"\b(?:TODO|FIXME|HACK|XXX)\b|"
    r"\bworkaround\b|"
    r"\bfor now\b|"
    r"\bknown limitation\b|"
    r"\bnot supported yet\b|"
    r"\bwhen someone (?:hits|needs)\b|"
    r"\badd it when\b|"
    r"\bskip(?:ping)? (?:them |it )?(?:for now|until)\b|"
    r"\bbest[- ]effort:\b"  # inline alibi on pass/except; API prose is OK
    r")"
)

# A ponytail that only defers work without naming a ceiling / upgrade path.
PONYTAIL_DEFER_RE = re.compile(
    r"(?i)ponytail:.*(?:\buntil\b|\bwhen someone\b|\bfor now\b|\badd it when\b)"
)

SPDX_RE = re.compile(r"SPDX-License-Identifier:")


def _is_code_file(path: Path) -> bool:
    if path.name.startswith("Dockerfile") or path.name == "install.sh":
        return True
    if path.name == "openapi.yaml":
        return False
    return path.suffix in CODE_SUFFIXES


def iter_code_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIR_NAMES for part in path.parts):
            continue
        if _is_code_file(path):
            files.append(path)
    return sorted(files)


def _python_comments(path: Path) -> list[tuple[int, str]]:
    source = path.read_text(encoding="utf-8", errors="replace")
    comments: list[tuple[int, str]] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                comments.append((tok.start[0], tok.string))
    except tokenize.TokenError as exc:
        raise SystemExit(f"FAIL {path.relative_to(ROOT)}: tokenize error: {exc}") from (
            exc
        )
    return comments


def _hash_line_comments(path: Path) -> list[tuple[int, str]]:
    comments: list[tuple[int, str]] = []
    for lineno, line in enumerate(
        path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
    ):
        stripped = line.lstrip()
        if stripped.startswith("#!"):
            continue
        if path.suffix in {".ts", ".js"}:
            if "//" not in line:
                continue
            # Strings with // are rare in this repo's TypeScript; accept the
            # crude split — false positives still have to look like excuses.
            comment = line[line.index("//") :].rstrip()
        else:
            if not stripped.startswith("#"):
                continue
            comment = stripped.rstrip()
        comments.append((lineno, comment))
    return comments


def collect_comments(path: Path) -> list[tuple[int, str]]:
    if path.suffix == ".py":
        return _python_comments(path)
    return _hash_line_comments(path)


def is_excuse(text: str) -> bool:
    body = text.lstrip("#/ \t")
    if SPDX_RE.search(body):
        return False
    if PONYTAIL_DEFER_RE.search(body):
        return True
    if body.lower().startswith("ponytail:"):
        return False
    return bool(EXCUSE_RE.search(body))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--budget",
        type=int,
        default=COMMENTS_BUDGET,
        help=f"max non-docstring comments (default {COMMENTS_BUDGET})",
    )
    args = parser.parse_args(argv)

    failures: list[str] = []
    total = 0
    for path in iter_code_files():
        rel = path.relative_to(ROOT).as_posix()
        for lineno, text in collect_comments(path):
            total += 1
            if is_excuse(text):
                failures.append(f"FAIL {rel}:{lineno}: excuse comment: {text.strip()}")

    if total > args.budget:
        failures.append(
            f"FAIL comment density: {total} non-docstring comments exceed "
            f"budget {args.budget}; delete restating comments or raise the "
            "budget in scripts/check_comments.py only when a real constraint "
            "earns the room"
        )

    for line in failures:
        print(line, file=sys.stderr)
    if failures:
        print(
            f"{len(failures)} comment-policy failure(s); scanned {total} comments",
            file=sys.stderr,
        )
        return 1
    print(f"OK: {total} comments within budget {args.budget}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
