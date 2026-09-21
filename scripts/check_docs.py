# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic doc-freshness checks.

Enforces the mechanical half of doc freshness in CI — the invariants the
agent docs (AGENTS.md router + docs/agents/) declare about themselves:

- no-orphan router rule: every doc under docs/ (the DOC_DIRS sections,
  including docs/agents/) is routed from AGENTS.md by exact path;
- every docs/ path AGENTS.md routes to exists;
- every docs/agents/*.md file is registered (playbook or adapter) with a
  line cap; playbooks additionally forbid code fences;
- no line-number citations anywhere in docs/agents/;
- every ``path::symbol`` citation resolves: the file must exist in source
  and contain the symbol (dotted attributes checked by last component);
- repo-root-anchored backticked paths and markdown links resolve across
  first-party Markdown files, and literal ``docs/*.md`` references resolve
  in first-party source and package metadata — a link's ``#anchor`` names a
  real heading in the file it points at, so a renamed heading cannot leave
  a valid path with a dangling fragment;
- module coverage: every public ``packages/*/sediment_*/*.py``
  module's name appears in at least one routed doc, unless the module
  carries a ``docs-exempt`` marker;
- diff mode (``--base REF``): a diff that adds a new public module
  or a new ``add_argument`` flag in scripts/ or sim/ must touch CHANGELOG.md.

What this deliberately cannot check (the semantic half, handled by
docs/agents/doc-sync.md at authoring time): concrete values quoted in
prose, prose "§" section references (unlike markdown ``#anchors``, they
are not links and name nothing resolvable), CONTEXT.md glossary claims
about bare class names, and CHANGELOG.md *currency* (diff mode checks
touch, not truth). Exit 0 when clean, 1 with FAIL lines otherwise.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The router grows with the docs tree it routes: every doc under DOC_DIRS
# needs a row (the no-orphan rule below), so a cap that never moves is a cap
# that eventually gets paid for in shaved prose rather than in brevity.
# Raise the cap only when added documentation needs routing space.
AGENTS_CAP = 326

# Every docs/agents/*.md must appear in exactly one registry. Playbooks
# forbid code fences; adapters (skill config, checklists) may carry them.
# An unregistered file fails the run — that is the reminder.
#
# The caps bound sprawl, not sentence length. Plain-language prose costs
# lines: splitting a semicolon into two sentences, or naming the actor a
# clause had elided, both add words. A cap tight enough to forbid that is a
# cap that buys density by making the page harder to read, which is the
# wrong trade. Raise a cap when a rewrite earns the lines; do not raise one
# to make room for a section that should have been a link.
PLAYBOOK_CAPS = {
    "docs/agents/derivations.md": 250,
    "docs/agents/exports-and-stats.md": 260,
    "docs/agents/capture-translators.md": 175,
    "docs/agents/fact-store.md": 140,
    "docs/agents/postgresql.md": 70,
    "docs/agents/api-and-operations.md": 180,
    "docs/agents/statistics.md": 215,
}
ADAPTER_CAPS = {
    "docs/agents/domain.md": 65,
    "docs/agents/issue-tracker.md": 85,
    "docs/agents/doc-sync.md": 90,
    "docs/agents/review.md": 110,
    "docs/agents/doc-style.md": 145,
    "docs/agents/writing-style.md": 150,
    "docs/agents/capture-clients.md": 215,
    "docs/agents/mermaid-diagrams.md": 115,
}

# Routed doc directories under docs/ — flat sections plus the agent docs.
# docs/adr/ is deliberately absent: ADRs are routed as a directory, not
# per-file.
DOC_DIRS = (
    "",
    "agents",
    "explanation",
    "operate",
    "capture",
    "capture/agents",
    "exports",
    "history",
    "reference",
)

PUBLISHED_MANIFEST = "docs/published-pages.json"
PUBLISHED_DOC_DIRS = ("capture", "capture/agents", "explanation", "exports", "operate")
REQUIRED_PUBLISHED_PAGES = frozenset({"docs/quickstart.md"})
_PUBLISHED_DESTINATION = re.compile(
    r"^[a-z0-9]+(?:-[a-z0-9]+)*(?:/[a-z0-9]+(?:-[a-z0-9]+)*)*$"
)
_PUBLISHED_SOURCE = re.compile(r"^docs/[a-z0-9_./-]+\.md$")


def _doc_files(root: Path) -> list[Path]:
    docs: list[Path] = []
    for sub in DOC_DIRS:
        docs += sorted((root / "docs" / sub).glob("*.md"))
    return docs


PATH_ANCHORS = (
    "docs/",
    "packages/",
    "apps/",
    "scripts/",
    "litellm/",
    "sim/",
    ".claude/",
    ".github/",
)

_REQUIRED_NAVIGATION_FILES = ("AGENTS.md", "CLAUDE.md", "CONTEXT.md", "README.md")

# Public package modules subject to the coverage check: not
# underscore-prefixed, not __init__ (excluded by the [!_] glob below).
DOCS_EXEMPT = "docs-exempt"
_MODULE_RE = re.compile(r"^packages/[^/]+/sediment_[^/]+/[^_/][A-Za-z0-9_]*\.py$")

_SYMBOL_CITE = re.compile(r"([A-Za-z0-9_./-]+\.py)::([A-Za-z_][A-Za-z0-9_.]*)")
_LINE_CITE = re.compile(r"\.(?:py|md|toml|ya?ml|json)(?::L?|#L)\d")
_BACKTICK = re.compile(r"`([^`\s]+)`")
_MD_LINK = re.compile(r"\]\(<?([^)\s]+?)>?(?:\s+\"[^\"]*\")?\)")
_ROUTE = re.compile(r"docs/[A-Za-z0-9_./-]+\.md")
_HEADING = re.compile(r"^#{1,6}\s+(.*)$")
_LINE_ANCHOR = re.compile(r"^L\d+(?:-L?\d+)?$")

_REFERENCE_SUFFIXES = frozenset(
    {".py", ".ts", ".json", ".toml", ".yaml", ".yml", ".sh"}
)
_REFERENCE_FILENAMES = frozenset({"Dockerfile"})
_REFERENCE_SKIP_PARTS = frozenset({".git", ".venv", ".pytest_cache", "node_modules"})
_REFERENCE_TEST_DIRS = frozenset({"test", "tests"})
# These files contain paths inside synthetic upstream repositories, not
# references to this repository's docs tree.
_REFERENCE_FIXTURE_FILES = frozenset({"sim/scenarios.py"})


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _is_reference_file(path: Path, root: Path) -> bool:
    return not any(
        part in _REFERENCE_SKIP_PARTS for part in path.relative_to(root).parts
    )


def _markdown_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.md") if _is_reference_file(path, root))


def _runtime_reference_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or (
            path.suffix not in _REFERENCE_SUFFIXES
            and path.name not in _REFERENCE_FILENAMES
        ):
            continue
        rel = path.relative_to(root).as_posix()
        relative_parts = path.relative_to(root).parts
        if (
            rel in _REFERENCE_FIXTURE_FILES
            or any(part in _REFERENCE_TEST_DIRS for part in relative_parts)
            or not _is_reference_file(path, root)
        ):
            continue
        files.append(path)
    return sorted(files)


def _heading_anchors(path: Path) -> set[str]:
    """The fragment ids GitHub derives from a markdown file's headings.

    GitHub lowercases, drops everything that is not a word character,
    hyphen, or space, then hyphenates the spaces — so an em dash between
    two words leaves the two spaces around it and becomes a double hyphen.
    Duplicate headings get a ``-1`` suffix upstream; that is not modelled,
    which can pass a link this check should fail but never fails a good one.
    """
    out: set[str] = set()
    fenced = False
    for line in _read(path).splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        elif not fenced and (match := _HEADING.match(line)):
            slug = re.sub(r"[^\w\- ]", "", match.group(1).strip().lower())
            out.add(slug.replace(" ", "-"))
    return out


def _source_files(root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for base in ("packages", "apps", "scripts"):
        for py in (root / base).rglob("*.py"):
            if "__pycache__" in py.parts:
                continue
            files[py.relative_to(root).as_posix()] = _read(py)
    return files


def check_router(problems: list[str], root: Path) -> int:
    """No-orphan rule by exact routed path; every route resolves."""
    agents = root / "AGENTS.md"
    if not agents.exists():
        problems.append("AGENTS.md missing")
        return 0
    # Claude Code auto-loads CLAUDE.md, not AGENTS.md — the pointer file is
    # what routes a Claude agent into the router.
    claude = root / "CLAUDE.md"
    if not claude.exists():
        problems.append("CLAUDE.md missing — Claude Code has no route to AGENTS.md")
    elif "AGENTS.md" not in _read(claude):
        problems.append("CLAUDE.md does not point at AGENTS.md")
    routes = set(_ROUTE.findall(_read(agents)))
    for doc in _doc_files(root):
        rel = doc.relative_to(root).as_posix()
        if rel not in routes:
            problems.append(f"orphan doc: {rel} not routed by exact path in AGENTS.md")
    for route in sorted(routes):
        if not (root / route).exists():
            problems.append(f"dead route in AGENTS.md: {route}")
    return len(routes)


def _check_citations(
    problems: list[str], rel: str, text: str, source: dict[str, str]
) -> int:
    count = 0
    for file_part, symbol in _SYMBOL_CITE.findall(text):
        count += 1
        matches = [
            body
            for path, body in source.items()
            if path.endswith(file_part) or path.endswith("/" + file_part)
        ]
        if not matches:
            problems.append(f"{rel}: citation names no source file: {file_part}")
            continue
        leaf = symbol.split(".")[-1]
        head = symbol.split(".")[0]
        if not any(head in body and leaf in body for body in matches):
            problems.append(f"{rel}: dead symbol citation {file_part}::{symbol}")
    return count


def check_agent_docs(problems: list[str], root: Path, source: dict[str, str]) -> int:
    citations = 0
    registered = PLAYBOOK_CAPS | ADAPTER_CAPS
    for path in sorted((root / "docs" / "agents").glob("*.md")):
        rel = path.relative_to(root).as_posix()
        cap = registered.get(rel)
        if cap is None:
            problems.append(
                f"unregistered agent doc: {rel} — add it to PLAYBOOK_CAPS or "
                "ADAPTER_CAPS in scripts/check_docs.py"
            )
            continue
        text = _read(path)
        lines = len(text.splitlines())
        if lines > cap:
            problems.append(f"{rel}: {lines} lines exceeds cap {cap}")
        if rel in PLAYBOOK_CAPS and "```" in text:
            problems.append(f"{rel}: code fence (playbooks are prose/tables only)")
        for match in _LINE_CITE.finditer(text):
            problems.append(f"{rel}: line-number citation {match.group(0)!r}")
        citations += _check_citations(problems, rel, text, source)
    for rel in registered:
        if not (root / rel).exists():
            problems.append(f"registered agent doc missing: {rel}")
    for nav in ("AGENTS.md", "CONTEXT.md"):
        citations += _check_citations(problems, nav, _read(root / nav), source)
    agents_lines = len(_read(root / "AGENTS.md").splitlines())
    if agents_lines > AGENTS_CAP:
        problems.append(f"AGENTS.md: {agents_lines} lines exceeds cap {AGENTS_CAP}")
    return citations


def check_navigation(problems: list[str], root: Path) -> int:
    checked = 0
    for rel in _REQUIRED_NAVIGATION_FILES:
        if not (root / rel).exists():
            problems.append(f"navigation file missing: {rel}")
    for path in _markdown_files(root):
        rel = path.relative_to(root).as_posix()
        text = _read(path)
        for token in _BACKTICK.findall(text):
            token = token.split("#", 1)[0].rstrip(",.;:)")
            if (
                not token.startswith(PATH_ANCHORS)
                or "::" in token
                or any(c in token for c in "*?<[")
            ):
                continue
            checked += 1
            if not (root / token.rstrip("/")).exists():
                problems.append(f"{rel}: backticked path does not exist: {token}")
        for link in _MD_LINK.findall(text):
            if link.startswith(("http://", "https://", "mailto:")):
                continue
            link, _, anchor = link.partition("#")
            if not link and not anchor:
                continue
            base = root if link.startswith("/") else path.parent
            target = (base / link.lstrip("/")).resolve() if link else path
            if link:
                checked += 1
                if not target.exists():
                    problems.append(f"{rel}: broken link: {link}")
                    continue
            # Deep links rot silently: renaming a heading leaves the path
            # valid and the anchor dangling, which no other check here sees.
            if anchor and target.suffix == ".md" and not _LINE_ANCHOR.match(anchor):
                checked += 1
                if anchor not in _heading_anchors(target):
                    problems.append(
                        f"{rel}: dead anchor #{anchor} in "
                        f"{target.relative_to(root).as_posix()}"
                    )
    for path in _runtime_reference_files(root):
        rel = path.relative_to(root).as_posix()
        for doc in sorted(set(_ROUTE.findall(_read(path)))):
            checked += 1
            if not (root / doc).exists():
                problems.append(f"{rel}: referenced doc does not exist: {doc}")
    return checked


def check_module_coverage(problems: list[str], root: Path) -> int:
    """Every public package module's name appears in some routed doc.

    Grep-shaped by design: a stem mention anywhere in AGENTS.md, docs/*.md,
    or docs/agents/*.md counts. A low bar — the point is catching a new
    module with zero documentation anywhere, not judging documentation
    quality. ``# docs-exempt: <why>`` in the module opts out.
    """
    corpus_paths = [root / "AGENTS.md", *_doc_files(root)]
    corpus = "\n".join(_read(p) for p in corpus_paths if p.exists())
    checked = 0
    for py in sorted(root.glob("packages/*/sediment_*/[!_]*.py")):
        if DOCS_EXEMPT in _read(py):
            continue
        checked += 1
        if py.stem not in corpus:
            rel = py.relative_to(root).as_posix()
            problems.append(
                f"undocumented module: {rel} — '{py.stem}' appears in no "
                "routed doc; add it to AGENTS.md or a playbook, or mark the "
                f"module '# {DOCS_EXEMPT}: <why>'"
            )
    return checked


def check_published_pages(problems: list[str], root: Path) -> int:
    """Every user-facing source page has one valid site destination."""
    manifest_path = root / PUBLISHED_MANIFEST
    if not manifest_path.is_file():
        problems.append(f"published-page manifest missing: {PUBLISHED_MANIFEST}")
        return 0
    try:
        manifest = json.loads(_read(manifest_path))
    except json.JSONDecodeError as exc:
        problems.append(f"invalid {PUBLISHED_MANIFEST}: {exc.msg}")
        return 0
    pages = manifest.get("pages") if isinstance(manifest, dict) else None
    if not isinstance(pages, list):
        problems.append(f"invalid {PUBLISHED_MANIFEST}: 'pages' must be a list")
        return 0

    sources: set[str] = set()
    destinations: set[str] = set()
    for index, page in enumerate(pages):
        if not isinstance(page, dict):
            problems.append(
                f"invalid {PUBLISHED_MANIFEST}: pages[{index}] must be an object"
            )
            continue
        source = page.get("source")
        destination = page.get("destination")
        description = page.get("description")
        title = page.get("title")
        if not isinstance(source, str) or not source:
            problems.append(
                f"invalid {PUBLISHED_MANIFEST}: pages[{index}] has no source"
            )
            continue
        if not _PUBLISHED_SOURCE.fullmatch(source) or any(
            part in {"", ".", ".."} for part in source.split("/")
        ):
            problems.append(f"invalid published source: {source}")
            continue
        if source in sources:
            problems.append(f"duplicate published source: {source}")
        sources.add(source)
        if not (root / source).is_file():
            problems.append(f"published page source does not exist: {source}")
        if not isinstance(destination, str) or not _PUBLISHED_DESTINATION.fullmatch(
            destination
        ):
            problems.append(f"invalid published destination for {source}")
        elif destination in destinations:
            problems.append(f"duplicate published destination: {destination}")
        else:
            destinations.add(destination)
        if not isinstance(description, str) or not description.strip():
            problems.append(f"published page has no description: {source}")
        if title is not None and (not isinstance(title, str) or not title.strip()):
            problems.append(f"published page has an invalid title: {source}")

    publishable = set(REQUIRED_PUBLISHED_PAGES)
    for subdirectory in PUBLISHED_DOC_DIRS:
        publishable.update(
            path.relative_to(root).as_posix()
            for path in (root / "docs" / subdirectory).glob("*.md")
        )
    for source in sorted(publishable - sources):
        if source in REQUIRED_PUBLISHED_PAGES:
            problems.append(f"required published page missing: {source}")
        else:
            problems.append(
                f"unmapped published page: {source} is absent from {PUBLISHED_MANIFEST}"
            )
    for source in sorted(sources - publishable):
        if (root / source).is_file():
            problems.append(f"published page is outside the public doc tiers: {source}")
    return len(pages)


def _added_flag_lines(diff_text: str) -> list[str]:
    """Operator-script files (tests excluded) whose diff adds an argparse flag."""
    flagged: list[str] = []
    current = ""
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
        elif (
            line.startswith("+")
            and "add_argument(" in line
            and current.startswith(("scripts/", "sim/"))
            and "/tests/" not in current
        ):
            flagged.append(current)
    return flagged


def check_changelog_touch(problems: list[str], root: Path, base: str) -> int:
    """Diff mode: new user-visible surface requires a CHANGELOG touch.

    Two grep-shaped triggers — an added public package module, or an added
    ``add_argument`` flag in scripts/ or sim/ — exactly the shapes that
    have shipped undocumented before. Checks touch, not truth: doc-sync.md
    still owns whether the entry says anything useful.
    """

    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "git failed")
        return result.stdout

    try:
        span = f"{base}...HEAD"
        changed = set(git("diff", "--name-only", span).splitlines())
        added = git("diff", "--name-only", "--diff-filter=A", span).splitlines()
        surface_diff = git("diff", span, "--", "scripts", "sim")
    except (OSError, RuntimeError) as exc:
        # A broken base ref must fail the gate loudly — silently skipping
        # would reopen the exact blind spot this check closes.
        problems.append(f"diff mode unusable against base {base!r}: {exc}")
        return 0
    triggers = [
        f"new module {path}"
        for path in added
        if _MODULE_RE.match(path)
        and (root / path).exists()
        and DOCS_EXEMPT not in _read(root / path)
    ]
    triggers += [
        f"new flag in {path}" for path in sorted(set(_added_flag_lines(surface_diff)))
    ]
    if triggers and "CHANGELOG.md" not in changed:
        problems.append(
            "CHANGELOG.md untouched but the diff adds user-visible surface: "
            + "; ".join(triggers)
        )
    return len(triggers)


def main(root: Path = ROOT, base: str | None = None) -> int:
    problems: list[str] = []
    source = _source_files(root)
    routes = check_router(problems, root)
    citations = check_agent_docs(problems, root, source)
    navigation = check_navigation(problems, root)
    modules = check_module_coverage(problems, root)
    published = check_published_pages(problems, root)
    triggers = check_changelog_touch(problems, root, base) if base else 0
    for problem in problems:
        print(f"FAIL: {problem}")
    if problems:
        print(f"{len(problems)} doc-freshness failure(s)")
        return 1
    print(
        f"docs OK: {routes} distinct routes, {citations} symbol citations, "
        f"{navigation} navigation targets, {modules} modules covered, "
        f"{published} published pages"
        + (f", {triggers} diff trigger(s) changelogged" if base else "")
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Deterministic doc-freshness checks")
    parser.add_argument(
        "--base",
        help="git ref to diff against; enables the CHANGELOG-touch check",
    )
    sys.exit(main(base=parser.parse_args().base))
