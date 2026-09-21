# SPDX-License-Identifier: AGPL-3.0-or-later
"""Glossary-gap detector: flag load-bearing code entities missing from CONTEXT.md.

Diffs the local graphify code-graph's node inventory against CONTEXT.md `###`
headings and reports domain classes (optionally functions) that are high-degree
in the graph but have no glossary entry, turning vocabulary drift into a
mechanical, advisory flag.

Fully local: consumes only graphify's tree-sitter AST extraction (`--code-only`,
no LLM, no key, no network). It never touches graphify's `query` retrieval layer.
With no `--graph`, it shells out to `graphify extract --code-only` itself.

Advisory only — always exits 0. Ranking is by graph degree; the tail below a few
tens of edges is report/DTO internals, not glossary material, so lower
`--min-degree` for recall and read top-down.

    uv run python scripts/glossary_gap.py                       # run graphify, report
    uv run python scripts/glossary_gap.py --path packages/core  # scope to persisted entities
    uv run python scripts/glossary_gap.py --graph out/graph.json --include-functions
    uv run python scripts/glossary_gap.py --stale --json
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path


def _norm(text: str) -> str:
    """Lowercase, alnum-only — the comparison key for identifiers and terms."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


@dataclass(frozen=True)
class DefinedTerms:
    """The vocabulary CONTEXT.md defines, parsed from its `###` headings."""

    exact: frozenset[str]  # normalized backtick tokens without a glob
    globs: tuple[str, ...]  # raw backtick tokens containing `*` (e.g. `*Policy`)
    titles: tuple[str, ...]  # normalized heading title (text before first `(`)
    camel_backticks: tuple[
        str, ...
    ]  # CamelCase backtick tokens (should be real classes)


def parse_defined_terms(context_text: str) -> DefinedTerms:
    exact: set[str] = set()
    globs: list[str] = []
    titles: list[str] = []
    camel: list[str] = []
    for line in context_text.splitlines():
        if not line.startswith("### "):
            continue
        heading = line[4:].strip()
        titles.append(_norm(heading.split("(", 1)[0]))
        for token in re.findall(r"`([^`]+)`", heading):
            for part in re.split(r"[,\s]+", token):
                part = part.strip()
                if not part:
                    continue
                if "*" in part:
                    globs.append(part)
                    continue
                exact.add(_norm(part))
                if re.fullmatch(r"[A-Z][A-Za-z0-9]*", part):
                    camel.append(part)
    return DefinedTerms(frozenset(exact), tuple(globs), tuple(titles), tuple(camel))


def _glob_match(name_norm: str, globs: tuple[str, ...]) -> bool:
    for raw in globs:
        # Normalize each literal segment separately: `_norm` strips any
        # non-alnum placeholder, so the `*` must be handled by splitting,
        # never by marker substitution inside the normalized string.
        pattern = "^" + ".*".join(re.escape(_norm(p)) for p in raw.split("*")) + "$"
        if re.match(pattern, name_norm):
            return True
    return False


def is_covered(name: str, terms: DefinedTerms) -> bool:
    """True if `name` maps to a CONTEXT.md heading.

    Three tiers, cheapest first: exact backtick token, glob backtick token
    (`*Policy` covers LabelConfidencePolicy), then two-way substring against a heading
    title (so `FactStore` is covered by the bare `### Fact` heading).
    """
    key = _norm(name)
    if not key:
        return False
    if key in terms.exact or _glob_match(key, terms.globs):
        return True
    return any(title and (title in key or key in title) for title in terms.titles)


@dataclass(frozen=True)
class CodeEntity:
    name: str
    kind: str  # "class" or "func"
    degree: int
    source_file: str


_CLASS_RE = re.compile(r"[A-Z][A-Za-z0-9_]*")


def code_entities(graph: dict, *, include_functions: bool) -> list[CodeEntity]:
    """First-party classes (and optionally functions) from a graphify graph.json.

    Excludes tests, the sim harness, and private/nested labels — none are
    glossary candidates. Degree is undirected edge count on the node id.
    """
    degree: Counter[str] = Counter()
    for edge in graph.get("links", []):
        degree[edge["source"]] += 1
        degree[edge["target"]] += 1

    out: list[CodeEntity] = []
    for node in graph.get("nodes", []):
        if node.get("file_type") != "code":
            continue
        label = node.get("label", "")
        source_file = node.get("source_file") or ""
        if not source_file:  # external / stdlib (e.g. Path)
            continue
        if "/tests/" in source_file or source_file.startswith("sim/"):
            continue
        if label.startswith(("test_", "scenario_", ".", "_")):
            continue
        is_func = label.endswith("()")
        ident = label[:-2] if is_func else label
        if _CLASS_RE.fullmatch(label):
            kind = "class"
        elif (
            is_func
            and include_functions
            and (_CLASS_RE.match(ident) or re.fullmatch(r"[a-z][A-Za-z0-9_]*", ident))
        ):
            kind = "func"
        else:
            continue
        out.append(CodeEntity(ident, kind, degree[node["id"]], source_file))
    return out


def find_gaps(
    graph: dict,
    context_text: str,
    *,
    min_degree: int,
    include_functions: bool,
    path_filters: list[str] | None = None,
) -> list[CodeEntity]:
    terms = parse_defined_terms(context_text)
    gaps = [
        e
        for e in code_entities(graph, include_functions=include_functions)
        if e.degree >= min_degree
        and not is_covered(e.name, terms)
        and (not path_filters or any(p in e.source_file for p in path_filters))
    ]
    gaps.sort(key=lambda e: (-e.degree, e.name))
    return gaps


def find_stale(graph: dict, context_text: str) -> list[str]:
    """CamelCase glossary backtick classes with no matching code node — renamed/removed."""
    terms = parse_defined_terms(context_text)
    present = {_norm(e.name) for e in code_entities(graph, include_functions=True)}
    return [t for t in terms.camel_backticks if _norm(t) not in present]


def run_graphify(repo: Path) -> dict:
    """Extract a code-only graph.json via the graphify CLI (local, no LLM)."""
    with tempfile.TemporaryDirectory(prefix="glossary-gap-") as tmp:
        cmd = ["graphify", "extract", str(repo), "--code-only", "--output", tmp]
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        except FileNotFoundError:
            raise SystemExit(
                "graphify not found. Install it (`uv tool install graphify`) or pass "
                "a pre-built graph.json with --graph."
            )
        except subprocess.CalledProcessError as exc:
            raise SystemExit(f"graphify extract failed:\n{exc.stderr}")
        matches = list(Path(tmp).rglob("graph.json"))
        if not matches:
            raise SystemExit("graphify produced no graph.json")
        return json.loads(matches[0].read_text(encoding="utf-8"))


def _load_graph(args: argparse.Namespace) -> dict:
    if args.graph:
        return json.loads(Path(args.graph).read_text(encoding="utf-8"))
    return run_graphify(Path(args.repo))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--graph", help="path to a graphify graph.json (else run graphify)"
    )
    parser.add_argument(
        "--repo", default=".", help="repo root to extract / resolve CONTEXT.md"
    )
    parser.add_argument(
        "--context", help="path to CONTEXT.md (default: <repo>/CONTEXT.md)"
    )
    parser.add_argument(
        "--min-degree", type=int, default=10, help="degree floor (default 10)"
    )
    parser.add_argument(
        "--path",
        action="append",
        dest="paths",
        default=[],
        help="only entities whose source_file contains this substring (repeatable)",
    )
    parser.add_argument(
        "--include-functions",
        action="store_true",
        help="also consider functions, not just classes (noisier)",
    )
    parser.add_argument(
        "--stale",
        action="store_true",
        help="also list glossary classes with no code node",
    )
    parser.add_argument("--top", type=int, help="show at most this many gaps")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = parser.parse_args(argv)

    context_path = (
        Path(args.context) if args.context else Path(args.repo) / "CONTEXT.md"
    )
    context_text = context_path.read_text(encoding="utf-8")
    graph = _load_graph(args)

    gaps = find_gaps(
        graph,
        context_text,
        min_degree=args.min_degree,
        include_functions=args.include_functions,
        path_filters=args.paths,
    )
    if args.top is not None:
        gaps = gaps[: args.top]
    stale = find_stale(graph, context_text) if args.stale else []

    if args.json:
        print(json.dumps({"gaps": [asdict(g) for g in gaps], "stale": stale}, indent=2))
        return 0

    if not gaps:
        print(f"No glossary gaps at degree >= {args.min_degree}.")
    else:
        print(
            f"Glossary gaps — code entities (degree >= {args.min_degree}) with no CONTEXT.md entry:"
        )
        for g in gaps:
            print(f"  {g.degree:4d}  {g.kind:5s}  {g.name:34s}  {g.source_file}")
        print(
            f"\n{len(gaps)} candidate(s). Advisory — a hit is a term to define or a "
            "name to deliberately keep out of the glossary."
        )
    if args.stale:
        if stale:
            print("\nStale glossary classes (in CONTEXT.md, no matching code node):")
            for name in stale:
                print(f"  {name}")
        else:
            print("\nNo stale glossary classes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
