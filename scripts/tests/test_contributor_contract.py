# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contributor-facing repository contracts."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml
import pytest


ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _frontmatter(relative: str) -> tuple[dict, str]:
    text = _read(relative)
    marker, frontmatter, body = text.split("---", 2)
    assert marker == ""
    parsed = yaml.safe_load(frontmatter)
    assert isinstance(parsed, dict)
    return parsed, body


def test_contributor_markdown_is_visible_in_a_clean_clone_and_status(tmp_path) -> None:
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "--quiet", "--no-local", str(ROOT), str(clone)],
        check=True,
    )
    guidance = Path("docs/onboarding.md")
    assert (clone / guidance).is_file()
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", guidance.as_posix()],
        cwd=clone,
        text=True,
        capture_output=True,
        check=False,
    )
    assert tracked.returncode == 0, tracked.stderr

    shutil.copyfile(ROOT / ".gitignore", clone / ".gitignore")
    probe = clone / "docs/contributor-contract-probe.md"
    probe.write_text("# Contributor contract probe\n", encoding="utf-8")
    status = subprocess.run(
        ["git", "status", "--short", "--untracked-files=all"],
        cwd=clone,
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    assert "?? docs/contributor-contract-probe.md" in status


def test_documentation_passes_from_a_complete_source_archive(tmp_path) -> None:
    public = tmp_path / "public"
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    for filename in tracked.split("\0"):
        if not filename:
            continue
        relative = Path(filename)
        destination = public / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination, follow_symlinks=False)

    assert not (public / ".git").exists()
    copied = {
        path.relative_to(public).as_posix()
        for path in public.rglob("*")
        if path.is_file()
    }
    assert copied == {filename for filename in tracked.split("\0") if filename}
    for required in (
        "README.md",
        "AGENTS.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
        "docs/onboarding.md",
        "docs/agents/issue-tracker.md",
    ):
        assert (public / required).is_file()

    result = subprocess.run(
        [sys.executable, str(public / "scripts/check_docs.py")],
        cwd=public,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_published_derivation_pages_explain_bundle_completeness() -> None:
    pages = {
        "docs/explanation/how-derivation-works.md": (
            "The bundle contract treats completeness as a producer assertion "
            "rather than a validation result."
        ),
        "docs/operate/run-derivations.md": (
            "Validation proves the bundle's internal relationships. It can't prove "
            "that an external producer supplied a complete or truthful Fact population."
        ),
    }
    for relative, expected in pages.items():
        body = " ".join(_read(relative).split())
        assert expected in body
        assert "0016-bundle-derivation-consistency" not in body


def test_active_contributor_surfaces_use_current_vocabulary_and_checks() -> None:
    onboarding = _read("docs/onboarding.md")
    pull_request = _read(".github/PULL_REQUEST_TEMPLATE.md")
    feature_request = _read(".github/ISSUE_TEMPLATE/feature_request.md")
    active = "\n".join((onboarding, pull_request, feature_request)).lower()
    for stale in (
        "four pillars",
        "labeled completion",
        "labelled completion",
        "pillar check",
        "which pillar",
    ):
        assert stale not in active
    assert "attributed completions" in onboarding.lower()
    assert "rollouts" in onboarding.lower()
    assert "attributed completion" in pull_request.lower()
    for adr in (
        "0010-canonical-ci-outcome-facts",
        "0011-training-objectives-own-evidence-interpretation",
        "0012-postgresql-fact-store",
    ):
        assert adr in onboarding
    for command in (
        "uv run pytest packages/core",
        "uv run pytest -q",
        "uv run python scripts/dump_openapi.py --check",
        "uv run python scripts/gen_cli_docs.py --check",
        "uv run python scripts/gen_api_docs.py --check",
        "uv run python scripts/gen_schema_docs.py --check",
        "uv run python scripts/release_rehearsal.py",
        "npm ci --no-audit --no-fund",
        "npm run typecheck",
        "npm test",
    ):
        assert command in onboarding
    assert "Python 3.12" in onboarding
    assert "Node 24" in onboarding
    assert "TruffleHog" in onboarding
    assert "CI-only" in onboarding


def test_external_pr_triage_requires_reviewed_enablement() -> None:
    guidance = _read("docs/agents/issue-tracker.md")
    assert "**PRs as a request surface: no.**" in guidance
    lower = guidance.lower()
    for requirement in (
        "community contributors",
        "maintainers",
        "audits fork workflows",
        "token scope",
        "merge permissions",
        "fork-based pull request",
        "contributor-visible failure path",
        "a maintainer approves a reviewed pull request",
    ):
        assert requirement in lower
    assert "contributors apply labels" not in lower
    assert "contributors assign milestones" not in lower


def test_github_templates_are_valid_and_bug_diagnostics_are_safe() -> None:
    for relative in (
        ".github/ISSUE_TEMPLATE/bug_report.md",
        ".github/ISSUE_TEMPLATE/feature_request.md",
    ):
        frontmatter, body = _frontmatter(relative)
        assert set(frontmatter) >= {"name", "about", "labels"}
        assert all(isinstance(frontmatter[key], str) for key in frontmatter)
        assert body.strip()

    config = yaml.safe_load(_read(".github/ISSUE_TEMPLATE/config.yml"))
    assert isinstance(config["blank_issues_enabled"], bool)
    assert config["contact_links"]
    for link in config["contact_links"]:
        assert set(link) == {"name", "url", "about"}

    pull_request = _read(".github/PULL_REQUEST_TEMPLATE.md")
    assert not pull_request.startswith("---")
    assert "## Summary" in pull_request
    assert "## Checklist" in pull_request

    bug = _read(".github/ISSUE_TEMPLATE/bug_report.md").lower()
    for diagnostic in (
        "sediment version",
        "python version",
        "operating system",
        "installation context",
        "deployment context",
        "failing stage",
        "sanitized commands",
        "minimal reproduction",
    ):
        assert diagnostic in bug
    for prohibited in (
        "raw transcripts",
        "prompts",
        "source archives",
        "environment dumps",
        "tokens",
        "webhook secrets",
        "database urls",
        "proprietary training rows",
    ):
        assert prohibited in bug


def test_pull_request_ci_scans_the_complete_tree_and_commit_history() -> None:
    workflow = yaml.safe_load(_read(".github/workflows/ci.yml"))
    steps = workflow["jobs"]["test"]["steps"]
    tree_scan = next(
        step
        for step in steps
        if step.get("name") == "Scan complete pull-request tree for secrets"
    )
    assert tree_scan["if"] == (
        "github.event_name == 'pull_request' || "
        "github.event_name == 'workflow_dispatch'"
    )
    assert "git archive HEAD" in tree_scan["run"]
    assert "filesystem /scan" in tree_scan["run"]
    assert re.fullmatch(
        r"ghcr\.io/trufflesecurity/trufflehog:3\.97\.5@sha256:[0-9a-f]{64}",
        tree_scan["env"]["TRUFFLEHOG_IMAGE"],
    )
    dependency_install = next(
        index
        for index, step in enumerate(steps)
        if step.get("run") == "uv sync --locked"
    )
    assert steps.index(tree_scan) < dependency_install
    assert any(
        step.get("uses")
        == "trufflesecurity/trufflehog@f714bf454f350590f4a24c3ddb1aef02c35bf5b6"
        and step["with"]["version"] == "3.97.5"
        for step in steps
    )


@pytest.mark.parametrize("proposal", [False, True])
def test_manual_secret_scan_resolves_only_proposed_history(tmp_path, proposal) -> None:
    workflow = yaml.safe_load(_read(".github/workflows/ci.yml"))
    steps = workflow["jobs"]["test"]["steps"]
    resolve = next(step for step in steps if step.get("id") == "secret-range")
    history = next(step for step in steps if "trufflesecurity/" in step.get("uses", ""))
    assert resolve["if"] == "github.event_name == 'workflow_dispatch'"
    assert resolve["env"]["DEFAULT_BRANCH"] == (
        "${{ github.event.repository.default_branch }}"
    )
    assert history["with"]["base"] == "${{ steps.secret-range.outputs.base || '' }}"
    assert history["with"]["head"] == (
        "${{ github.event_name == 'workflow_dispatch' && github.sha || '' }}"
    )
    assert history["if"] == (
        "github.event_name != 'workflow_dispatch' || "
        "steps.secret-range.outputs.base != github.sha"
    )

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    git("commit", "--allow-empty", "-qm", "base")
    base = git("rev-parse", "HEAD")
    git("update-ref", "refs/remotes/origin/main", base)
    if proposal:
        git("commit", "--allow-empty", "-qm", "proposal")
    output = tmp_path / "outputs"
    subprocess.run(
        ["bash", "-euo", "pipefail", "-c", resolve["run"]],
        cwd=tmp_path,
        env={**os.environ, "DEFAULT_BRANCH": "main", "GITHUB_OUTPUT": str(output)},
        check=True,
        capture_output=True,
        text=True,
    )
    assert output.read_text() == f"base={base}\n"
    assert (base != git("rev-parse", "HEAD")) == proposal

    git("update-ref", "-d", "refs/remotes/origin/main")
    output.unlink()
    missing_base = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", resolve["run"]],
        cwd=tmp_path,
        env={**os.environ, "DEFAULT_BRANCH": "main", "GITHUB_OUTPUT": str(output)},
        check=False,
        capture_output=True,
        text=True,
    )
    assert missing_base.returncode != 0
    assert not output.exists()
