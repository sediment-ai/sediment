# SPDX-License-Identifier: AGPL-3.0-or-later
"""Exercise conservative workflow routing against real Git histories."""

from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def preflight():
    path = ROOT / "scripts/ci_preflight.py"
    assert path.is_file(), "CI needs one shared, conservative preflight"
    spec = importlib.util.spec_from_file_location("ci_preflight", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def repository(tmp_path):
    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    for name in (
        "CONTEXT.md",
        "README.md",
        "docs/explanation/example.md",
        "docs/quickstart.md",
        "scripts/example.py",
    ):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("original\n")
    git("add", ".")
    git("commit", "-qm", "base")
    return tmp_path, git, git("rev-parse", "HEAD")


@pytest.mark.parametrize("event", ["push", "pull_request"])
@pytest.mark.parametrize(
    "name",
    [
        "CONTEXT.md",
        "CHANGELOG.md",
        "README.md",
        "docs/explanation/example.md",
        "docs/agents/writing-style.md",
        "docs/adr/0020-example.md",
    ],
)
def test_prose_only_changes_avoid_full_validation(repository, event, name):
    root, git, base = repository
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("A prose correction.\n")
    git("add", ".")
    git("commit", "-qm", "prose")
    assert not preflight().requires_full_validation(root, event, base)


@pytest.mark.parametrize(
    "name",
    [
        "docs/README.md",
        "docs/quickstart.md",
        "docs/operate/deploy.md",
        "docs/capture/local-capture.md",
        "docs/exports/rlvr-export.md",
        "docs/reference/schema.md",
        "docs/published-pages.json",
        "scripts/example.py",
        "uv.lock",
        ".github/workflows/ci.yml",
        "docs/explanation/extra.py",
        "docs/explanation/name\nwith-newline.py",
    ],
)
def test_unknown_or_executable_surfaces_always_get_full_validation(repository, name):
    root, git, base = repository
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("changed\n")
    git("add", ".")
    git("commit", "-qm", "full")
    assert preflight().requires_full_validation(root, "pull_request", base)


@pytest.mark.parametrize(
    "mutation", ["mixed", "deleted", "rename", "symlink", "executable"]
)
@pytest.mark.parametrize("name", ["CONTEXT.md", "README.md"])
def test_prose_path_cannot_hide_other_changes(repository, mutation, name):
    root, git, base = repository
    path = root / name
    path.write_text("prose\n")
    if mutation == "mixed":
        (root / "scripts/example.py").write_text("changed\n")
    elif mutation == "deleted":
        path.unlink()
    elif mutation == "rename":
        git("mv", "scripts/example.py", "docs/explanation/moved.md")
    elif mutation == "executable":
        path.chmod(0o755)
    else:
        path.unlink()
        path.symlink_to("scripts/example.py")
    git("add", "-A")
    git("commit", "-qm", "changes")
    assert preflight().requires_full_validation(root, "push", base)


@pytest.mark.parametrize(
    "event,base",
    [
        ("workflow_dispatch", "valid"),
        ("release", "valid"),
        ("workflow_call", "valid"),
        ("push", ""),
        ("pull_request", "0" * 40),
        ("push", "--output=/tmp/unsafe"),
    ],
)
@pytest.mark.parametrize("name", ["CONTEXT.md", "README.md"])
def test_manual_release_or_missing_base_defaults_to_full(repository, event, base, name):
    root, git, original = repository
    (root / name).write_text("prose\n")
    git("add", ".")
    git("commit", "-qm", "prose")
    assert preflight().requires_full_validation(
        root, event, original if base == "valid" else base
    )


def test_empty_diff_defaults_to_full(repository):
    root, _, base = repository
    assert preflight().requires_full_validation(root, "push", base)


def workflow(name):
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text())


def test_quality_checks_precede_database_setup_and_prose_keeps_contracts():
    steps = workflow("ci.yml")["jobs"]["test"]["steps"]
    commands = [step.get("run", "") for step in steps]
    database = next(
        i for i, text in enumerate(commands) if "docker build --pull" in text
    )
    for command in ("uv run ruff check .", "uv run ruff format --check ."):
        assert commands.index(command) < database
    assert any("ci_preflight.py scope" in command for command in commands)
    for step in steps:
        command = step.get("run", "")
        if any(
            text in command
            for text in (
                "docker build --pull",
                "sediment db upgrade",
                "pytest -q --durations=30",
                "scripts/release_rehearsal.py",
            )
        ):
            assert step["if"] == "steps.scope.outputs.full == 'true'"
    prose = next(step for step in steps if step.get("name") == "Check prose contracts")
    assert prose["if"] == "steps.scope.outputs.full == 'false'"
    assert "scripts/tests/test_contributor_contract.py" in prose["run"]
    assert "scripts/tests/test_check_docs.py" in prose["run"]


def test_security_drafts_wait_for_ready_and_release_remains_full():
    security = workflow("security.yml")
    events = security.get("on", security.get(True))
    assert "ready_for_review" in events["pull_request"]["types"]
    jobs = security["jobs"]
    assert jobs["source"]["if"] == workflow("ci.yml")["jobs"]["test"]["if"]
    for name in ("client", "pi", "images"):
        assert jobs[name]["needs"] == "source"
        assert jobs[name]["if"] == "needs.source.outputs.full == 'true'"
    source = jobs["source"]
    assert source["outputs"]["full"] == "${{ steps.scope.outputs.full }}"
    commands = [step.get("run", "") for step in source["steps"]]
    for expected in (
        "ruff check .",
        "ruff format --check .",
        "ci_preflight.py reviews",
    ):
        assert any(expected in command for command in commands)
    assert "workflow_call" in events
    assert "workflow_dispatch" in events
    scope = next(step for step in source["steps"] if step.get("id") == "scope")
    assert scope["env"]["CI_EVENT"] == (
        "${{ inputs.wheels-artifact != '' && 'workflow_call' || github.event_name }}"
    )
    reviews = next(
        step
        for step in source["steps"]
        if "ci_preflight.py reviews" in step.get("run", "")
    )
    assert "github.event_name != 'workflow_dispatch'" in reviews["if"]
    assert "inputs.wheels-artifact == ''" in reviews["if"]
    # Explicit bash enables pipefail on Actions; tee must not mask rejection.
    assert reviews["shell"] == "bash"


@pytest.mark.parametrize(
    "source,full,images,client,pi,expected",
    [
        ("success", "true", "success", "success", "success", 0),
        ("success", "false", "skipped", "skipped", "skipped", 0),
        ("failure", "", "skipped", "skipped", "skipped", 1),
        ("cancelled", "", "skipped", "skipped", "skipped", 1),
        ("success", "true", "failure", "success", "success", 1),
        ("success", "true", "skipped", "success", "success", 1),
        ("success", "", "skipped", "skipped", "skipped", 1),
    ],
)
def test_security_summary_cannot_hide_failed_or_missing_checks(
    source, full, images, client, pi, expected
):
    summary = workflow("security.yml")["jobs"].get("security")
    assert summary is not None, "A required summary must reject upstream failures"
    assert (
        summary["if"]
        == "always() && (github.event_name != 'pull_request' || github.event.pull_request.draft == false)"
    )
    step = summary["steps"][0]
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", step["run"]],
        env={
            **os.environ,
            "SOURCE_RESULT": source,
            "FULL": full,
            "IMAGES_RESULT": images,
            "CLIENT_RESULT": client,
            "PI_RESULT": pi,
        },
        capture_output=True,
    )
    assert result.returncode == expected


def test_routine_dependency_updates_are_grouped_without_changing_daily_checks():
    config = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text())
    for update in config["updates"]:
        assert update["schedule"]["interval"] == "daily"
        groups = update.get("groups", {})
        assert groups, update["package-ecosystem"]
        assert any(
            group["update-types"] == ["minor", "patch"] for group in groups.values()
        )
        assert update["commit-message"] == {"prefix": "chore(deps)"}


@pytest.mark.parametrize(
    "name,required",
    [
        ("shims/pi/index.ts", True),
        ("shims/pi/name\nwith-newline.ts", True),
        ("cli/sediment_cli/delivery.py", True),
        ("cli/sediment_cli/cli.py", True),
        ("cli/pyproject.toml", True),
        ("pyproject.toml", True),
        ("uv.lock", True),
        (".github/workflows/shims.yml", True),
        ("scripts/ci_preflight.py", True),
        ("scripts/tests/test_ci_preflight.py", True),
        ("README.md", False),
        ("docs/capture/agents/pi.md", False),
        ("cli/hatch_build.py", True),
        ("cli/sediment_cli/attribution.py", True),
        ("packages/export/example.py", False),
    ],
)
def test_shim_scope_uses_actual_changed_paths(repository, name, required):
    root, git, base = repository
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("changed\n")
    git("add", ".")
    git("commit", "-qm", "change")
    assert preflight().requires_shim_validation(root, "pull_request", base) is required


@pytest.mark.parametrize("operation", ["delete", "rename"])
def test_shim_scope_keeps_removed_or_moved_dependencies(repository, operation):
    root, git, _ = repository
    path = root / "shims/pi/index.ts"
    path.parent.mkdir(parents=True)
    path.write_text("original\n")
    git("add", ".")
    git("commit", "-qm", "shim")
    base = git("rev-parse", "HEAD")
    if operation == "delete":
        path.unlink()
    else:
        git("mv", "shims/pi/index.ts", "docs/explanation/moved.md")
    git("add", "-A")
    git("commit", "-qm", "remove shim")
    assert preflight().requires_shim_validation(root, "pull_request", base)


@pytest.mark.parametrize(
    "event,base",
    [
        ("workflow_dispatch", "valid"),
        ("push", "valid"),
        ("pull_request", ""),
        ("pull_request", "0" * 40),
        ("pull_request", "--output=/tmp/unsafe"),
    ],
)
def test_shim_scope_unknown_history_and_non_pr_runs_select_tests(
    repository, event, base
):
    root, _, original = repository
    assert preflight().requires_shim_validation(
        root, event, original if base == "valid" else base
    )


def test_empty_shim_diff_selects_tests(repository):
    root, _, base = repository
    assert preflight().requires_shim_validation(root, "pull_request", base)


def test_shim_workflow_always_reports_and_gates_installed_tests():
    config = workflow("shims.yml")
    events = config.get("on", config.get(True))
    assert not {"paths", "paths-ignore"}.intersection(events["pull_request"])
    assert "ready_for_review" in events["pull_request"]["types"]
    steps = config["jobs"]["shims"]["steps"]
    checkout = next(
        s for s in steps if s.get("uses", "").startswith("actions/checkout@")
    )
    assert checkout["with"]["fetch-depth"] == 0
    scope = next(s for s in steps if s.get("id") == "scope")
    assert "ci_preflight.py shims" in scope["run"]
    assert scope["env"]["CI_BASE_SHA"] == "${{ github.event.pull_request.base.sha }}"
    for step in steps:
        if step.get("id") in {"install", "typecheck", "tests"}:
            assert step["if"] == "steps.scope.outputs.shims == 'true'"
    assert "SEDIMENT_PI_TEST_INSTALLED_BIN" in next(
        s["run"] for s in steps if s.get("id") == "install"
    )
    gate = steps[-1]
    assert gate["if"] == "always()"
    assert gate["env"] == {
        "SCOPE_RESULT": "${{ steps.scope.outcome }}",
        "SHIMS": "${{ steps.scope.outputs.shims }}",
        "INSTALL_RESULT": "${{ steps.install.outcome }}",
        "TYPECHECK_RESULT": "${{ steps.typecheck.outcome }}",
        "TEST_RESULT": "${{ steps.tests.outcome }}",
    }


@pytest.mark.parametrize(
    "scope,selected,install,typecheck,tests,expected",
    [
        ("success", "true", "success", "success", "success", 0),
        ("success", "false", "skipped", "skipped", "skipped", 0),
        ("failure", "false", "skipped", "skipped", "skipped", 1),
        ("cancelled", "", "skipped", "skipped", "skipped", 1),
        ("success", "", "skipped", "skipped", "skipped", 1),
        ("success", "true", "skipped", "success", "success", 1),
        ("success", "true", "failure", "skipped", "skipped", 1),
        ("success", "true", "success", "failure", "skipped", 1),
        ("success", "true", "success", "skipped", "success", 1),
        ("success", "true", "success", "success", "failure", 1),
        ("success", "true", "success", "success", "cancelled", 1),
        ("success", "true", "success", "success", "skipped", 1),
    ],
)
def test_shim_gate_rejects_failed_or_missing_selected_work(
    scope, selected, install, typecheck, tests, expected
):
    step = workflow("shims.yml")["jobs"]["shims"]["steps"][-1]
    assert step.get("name") == "Require selected shim checks"
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", step["run"]],
        env={
            **os.environ,
            "SCOPE_RESULT": scope,
            "SHIMS": selected,
            "INSTALL_RESULT": install,
            "TYPECHECK_RESULT": typecheck,
            "TEST_RESULT": tests,
        },
        capture_output=True,
    )
    assert result.returncode == expected, result.stdout + result.stderr
