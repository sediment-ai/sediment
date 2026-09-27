# SPDX-License-Identifier: AGPL-3.0-or-later
"""The doc-freshness checker fails on planted violations and passes clean."""

import json
import re
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "check_docs.py"
ROOT = SCRIPT.parents[1]

sys.path.insert(0, str(SCRIPT.parent))
import check_docs  # noqa: E402


def _mini_repo(tmp_path: Path) -> Path:
    """A minimal doc tree with one violation of each enforced rule."""
    (tmp_path / "docs" / "agents").mkdir(parents=True)
    for base in ("packages", "apps", "scripts"):
        (tmp_path / base).mkdir()
    (tmp_path / "packages" / "mod.py").write_text("def real_symbol():\n    pass\n")
    (tmp_path / "AGENTS.md").write_text(
        "Routes: `docs/agents/derivations.md` and `docs/ghost.md`.\n"
        "A broken [link](docs/nowhere.md#anchor) and `docs/absent.md`.\n"
    )
    (tmp_path / "CONTEXT.md").write_text("Vocabulary.\n")
    # Deep links: one anchor that names a real heading below, one that does
    # not. A dead anchor rides on a path that resolves, so nothing else here
    # would catch it.
    (tmp_path / "README.md").write_text(
        "Readme.\n"
        "[live](docs/agents/derivations.md#module-map) and "
        "[dead](docs/agents/derivations.md#no-such-heading).\n"
    )
    # Orphan: exists but never routed by exact path (substring bait included
    # via "derivations.md" being routed — "ations.md" must not save it).
    (tmp_path / "docs" / "ations.md").write_text("orphan\n")
    # Registered playbook violating: fence, line-cite, dead + fileless
    # citations, over its (tiny, real-registry) cap is not simulated — cap
    # violations are covered by the registry check on the real tree.
    (tmp_path / "docs" / "agents" / "derivations.md").write_text(
        "## Module map\n"
        "```\nfence\n```\nsee store.py:42 and `mod.py::gone_symbol` "
        "and `phantom.py::real_symbol`\n"
        "covered_module is documented here.\n"
    )
    # Unregistered agent doc.
    (tmp_path / "docs" / "agents" / "rogue.md").write_text("unregistered\n")
    (tmp_path / "sim" / "README.md").parent.mkdir()
    (tmp_path / "sim" / "README.md").write_text(
        "[deleted runbook](../docs/deleted-runbook.md)\n"
    )
    (tmp_path / "apps" / "runtime.py").write_text(
        'HELP = "see docs/deleted-runtime-guide.md"\n'
    )
    (tmp_path / "pyproject.toml").write_text(
        'guide = "docs/deleted-package-guide.md"\n'
    )
    # Module coverage: one undocumented, one covered (named in the playbook
    # text above via "covered_module"), one exempt, one private.
    pkg = tmp_path / "packages" / "pkg" / "sediment_pkg"
    pkg.mkdir(parents=True)
    (pkg / "ghost_module.py").write_text("def f():\n    pass\n")
    (pkg / "covered_module.py").write_text("def f():\n    pass\n")
    (pkg / "hidden.py").write_text("# docs-exempt: internal plumbing\n")
    (pkg / "_private.py").write_text("def f():\n    pass\n")
    return tmp_path


def test_checker_fails_on_each_planted_violation(tmp_path):
    problems: list[str] = []
    root = _mini_repo(tmp_path)
    source = check_docs._source_files(root)
    check_docs.check_router(problems, root)
    check_docs.check_agent_docs(problems, root, source)
    check_docs.check_navigation(problems, root)
    check_docs.check_module_coverage(problems, root)
    text = "\n".join(problems)
    assert "undocumented module: packages/pkg/sediment_pkg/ghost_module.py" in text
    assert "covered_module" not in text  # named in a routed doc
    assert "hidden.py" not in text  # docs-exempt marker honored
    assert "_private.py" not in text  # underscore-prefixed excluded
    assert "orphan doc: docs/ations.md" in text  # substring match must not save it
    assert "dead route in AGENTS.md: docs/ghost.md" in text
    assert "unregistered agent doc: docs/agents/rogue.md" in text
    assert "code fence" in text
    assert "line-number citation" in text
    assert "dead symbol citation mod.py::gone_symbol" in text
    assert "citation names no source file: phantom.py" in text
    # A broken path reports once; its anchor is not also blamed.
    assert "broken link: docs/nowhere.md" in text
    assert "dead anchor #anchor" not in text
    assert "dead anchor #no-such-heading in docs/agents/derivations.md" in text
    assert "#module-map" not in text  # a live anchor stays quiet
    assert "backticked path does not exist: docs/absent.md" in text
    assert "sim/README.md: broken link: ../docs/deleted-runbook.md" in text
    assert (
        "apps/runtime.py: referenced doc does not exist: "
        "docs/deleted-runtime-guide.md" in text
    )
    assert (
        "pyproject.toml: referenced doc does not exist: "
        "docs/deleted-package-guide.md" in text
    )
    # Registered-but-missing docs from the real registry also fire here.
    assert "registered agent doc missing" in text
    assert check_docs.main(root) == 1


def test_navigation_checks_nested_guides_and_changelog(tmp_path):
    root = _mini_repo(tmp_path)
    nested = root / "docs" / "topics" / "guide.md"
    nested.parent.mkdir()
    nested.write_text("[guide](../missing-guide.md)\n")
    (root / "CHANGELOG.md").write_text("[migration](docs/missing-migration.md)\n")
    problems: list[str] = []

    check_docs.check_navigation(problems, root)

    assert "docs/topics/guide.md: broken link: ../missing-guide.md" in problems
    assert "CHANGELOG.md: broken link: docs/missing-migration.md" in problems


def test_navigation_ignores_remote_urls_and_checks_adjacent_local_docs(tmp_path):
    root = _mini_repo(tmp_path)
    (root / "security").mkdir()
    (root / "security" / "maintenance.json").write_text(
        json.dumps(
            {
                "evidence": "https://github.com/vendor/project/blob/rev/docs/release.md",
                "source": "http://example.com/docs/upstream.md#support",
                "guide": "docs/missing-local.md",
            }
        )
    )
    problems: list[str] = []

    check_docs.check_navigation(problems, root)

    assert [p for p in problems if p.startswith("security/maintenance.json:")] == [
        "security/maintenance.json: referenced doc does not exist: "
        "docs/missing-local.md"
    ]


def test_router_ignores_upstream_doc_urls(tmp_path):
    root = _mini_repo(tmp_path)
    with (root / "AGENTS.md").open("a") as agents:
        agents.write(
            "[upstream](https://github.com/vendor/project/blob/rev/docs/upstream.md) "
            "and `docs/missing-local.md`.\n"
        )
    problems: list[str] = []

    check_docs.check_router(problems, root)

    assert "dead route in AGENTS.md: docs/upstream.md" not in problems
    assert "dead route in AGENTS.md: docs/missing-local.md" in problems


def _git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def test_changelog_touch_diff_mode(tmp_path):
    root = _mini_repo(tmp_path)
    _git(root, "init", "-q", "-b", "main")
    (root / "CHANGELOG.md").write_text("# Changelog\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    # New public module + new script flag, no CHANGELOG touch → one FAIL
    # naming both triggers; a flag in scripts/tests/ must not trigger.
    pkg = root / "packages" / "pkg" / "sediment_pkg"
    (pkg / "new_module.py").write_text("def f():\n    pass\n")
    (root / "scripts" / "tool.py").write_text('parser.add_argument("--new-flag")\n')
    (root / "scripts" / "tests").mkdir()
    (root / "scripts" / "tests" / "helper.py").write_text(
        'parser.add_argument("--test-only")\n'
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "feature")
    problems: list[str] = []
    triggers = check_docs.check_changelog_touch(problems, root, "HEAD~1")
    [problem] = problems
    assert "CHANGELOG.md untouched" in problem
    assert "new module packages/pkg/sediment_pkg/new_module.py" in problem
    assert "new flag in scripts/tool.py" in problem
    assert "tests" not in problem
    assert triggers == 2
    # Touching the CHANGELOG in the same span clears it.
    (root / "CHANGELOG.md").write_text("# Changelog\n- entry\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "changelog")
    problems = []
    check_docs.check_changelog_touch(problems, root, "HEAD~2")
    assert problems == []
    # A sim/ flag in a non-test file trips the gate.
    (root / "sim").mkdir(exist_ok=True)
    (root / "sim" / "tool.py").write_text('parser.add_argument("--deploy-flag")\n')
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "deploy-flag")
    problems = []
    triggers = check_docs.check_changelog_touch(problems, root, "HEAD~1")
    assert len(problems) == 1
    assert "CHANGELOG.md untouched" in problems[0]
    assert "new flag in sim/tool.py" in problems[0]
    assert triggers == 1
    # A sim/ flag under sim/.../tests/ must not trigger.
    (root / "sim" / "tests").mkdir()
    (root / "sim" / "tests" / "helper.py").write_text(
        'parser.add_argument("--test-only")\n'
    )
    (root / "CHANGELOG.md").write_text("# Changelog\n- entry\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "deploy-test-helper")
    problems = []
    triggers = check_docs.check_changelog_touch(problems, root, "HEAD~1")
    # CHANGELOG was touched (cleared the earlier trigger), and the
    # tests/ path must not contribute a trigger.
    assert problems == []
    assert triggers == 0
    # An exempt new module alone does not demand a CHANGELOG entry.
    (pkg / "internal.py").write_text("# docs-exempt: internal\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "internal")
    problems = []
    assert check_docs.check_changelog_touch(problems, root, "HEAD~1") == 0
    assert problems == []
    # A broken base ref fails loudly instead of silently skipping.
    problems = []
    check_docs.check_changelog_touch(problems, root, "no-such-ref")
    assert problems and "diff mode unusable" in problems[0]


def _published_tree(tmp_path: Path) -> tuple[Path, list[dict[str, str]]]:
    pages: list[dict[str, str]] = []
    for relative, destination in (
        ("docs/quickstart.md", "quickstart"),
        ("docs/capture/local.md", "capture/local"),
        ("docs/explanation/system.md", "system"),
        ("docs/exports/training.md", "exports/training"),
        ("docs/operate/deploy.md", "operate/deploy"),
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {path.stem}\n", encoding="utf-8")
        pages.append(
            {
                "source": relative,
                "destination": destination,
                "description": f"Published {path.stem} documentation.",
            }
        )
    (tmp_path / "docs" / "published-pages.json").write_text(
        json.dumps({"pages": pages}),
        encoding="utf-8",
    )
    return tmp_path, pages


def test_published_page_manifest_covers_every_publishable_document(tmp_path):
    root, pages = _published_tree(tmp_path)
    problems: list[str] = []
    assert check_docs.check_published_pages(problems, root) == len(pages)
    assert problems == []

    unlisted = root / "docs" / "operate" / "rehearse-release.md"
    unlisted.write_text("# Rehearse a release\n", encoding="utf-8")
    problems = []
    check_docs.check_published_pages(problems, root)
    assert problems == [
        "unmapped published page: docs/operate/rehearse-release.md is absent "
        "from docs/published-pages.json"
    ]


def test_published_page_manifest_rejects_invalid_entries(tmp_path):
    root, pages = _published_tree(tmp_path)
    pages[0]["source"] = "docs/absent.md"
    pages[1]["destination"] = pages[2]["destination"]
    pages[3]["description"] = ""
    pages[4]["source"] = "docs/operate/../../outside.md"
    pages.append(
        {
            "source": "docs/operate/Upper.md",
            "destination": "operate/upper",
            "description": "Invalid uppercase source path.",
        }
    )
    (root / "docs" / "published-pages.json").write_text(
        json.dumps({"pages": pages}),
        encoding="utf-8",
    )

    problems: list[str] = []
    check_docs.check_published_pages(problems, root)
    text = "\n".join(problems)
    assert "published page source does not exist: docs/absent.md" in text
    assert "duplicate published destination: system" in text
    assert "published page has no description: docs/exports/training.md" in text
    assert "invalid published source: docs/operate/../../outside.md" in text
    assert "invalid published source: docs/operate/Upper.md" in text
    assert "required published page missing: docs/quickstart.md" in text


def test_checker_passes_on_the_real_tree_with_honest_floors():
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    match = re.search(
        r"docs OK: (\d+) distinct routes, (\d+) symbol citations, "
        r"(\d+) navigation targets, (\d+) modules covered, "
        r"(\d+) published pages",
        result.stdout,
    )
    assert match, result.stdout
    routes, citations, navigation, modules, published = (int(g) for g in match.groups())
    # Ratchet floors near current values: a regression that silently drops
    # a whole check class must trip these.
    assert routes >= 18
    assert citations >= 10
    assert navigation >= 150
    assert modules >= 25
    assert published >= 12
