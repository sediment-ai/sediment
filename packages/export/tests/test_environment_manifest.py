# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Taskset-level ``environment.yaml`` manifest tests.

Two layers: pure ``ExportRow``-in tests for the manifest-building logic
(reward convention, workflow-reference agreement, splits, operator settings —
real dataclasses, never mocked, per AGENTS.md), and a real-git end-to-end test
wired through ``export_rlvr``, matching ``test_rlvr.py``'s discipline.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

from sediment_core import (
    CIResult,
    ForgeProvider,
    Push,
)
from sediment_derive import MirrorManager
from export_factories import inference_call, message

from sediment_export import (
    ExportRow,
    NemoGymRuntimeSettings,
    OpenEnvRuntimeSettings,
    build_manifest,
    export_rlvr,
    write_manifest,
)

ORG = "acme-corp"
REPO = "acme-corp/backend-service"
OTHER_REPO = "acme-corp/frontend"
WORKFLOW_NAME = "CI"
WORKFLOW_PATH = ".github/workflows/ci.yml"


def _task_row(
    *,
    split: str = "train",
    repo: str = REPO,
    workflow_name: str | None = WORKFLOW_NAME,
    workflow_path: str | None = WORKFLOW_PATH,
    recorded_result: str = "passed",
) -> ExportRow:
    """A minimal ``tasks.jsonl``-shaped row: only the fields ``build_manifest``
    reads (``repo``, ``verifier_results.workflow_name``/``workflow_path``) plus
    enough to look like a real row. Mirrors the Sediment task shape
    (``rlvr.py``)."""
    return ExportRow(
        split=split,
        body={
            "instance_id": f"{ORG}-sess-{repo}",
            "repo": repo,
            "verifier_results": [
                {
                    "workflow_name": workflow_name,
                    "workflow_path": workflow_path,
                    "result": recorded_result,
                }
            ],
        },
    )


def _settings(monkeypatch, *, image: str | None = None, package: str | None = None):
    monkeypatch.delenv("SEDIMENT_OPENENV_IMAGE", raising=False)
    monkeypatch.delenv("SEDIMENT_OPENENV_PACKAGE", raising=False)
    if image is not None:
        monkeypatch.setenv("SEDIMENT_OPENENV_IMAGE", image)
    if package is not None:
        monkeypatch.setenv("SEDIMENT_OPENENV_PACKAGE", package)
    return OpenEnvRuntimeSettings(_env_file=None)


def _nemo_settings(
    monkeypatch, *, resources_server: str | None = None, config: str | None = None
):
    monkeypatch.delenv("SEDIMENT_NEMO_GYM_RESOURCES_SERVER", raising=False)
    monkeypatch.delenv("SEDIMENT_NEMO_GYM_CONFIG", raising=False)
    if resources_server is not None:
        monkeypatch.setenv("SEDIMENT_NEMO_GYM_RESOURCES_SERVER", resources_server)
    if config is not None:
        monkeypatch.setenv("SEDIMENT_NEMO_GYM_CONFIG", config)
    return NemoGymRuntimeSettings(_env_file=None)


def test_no_rows_yields_no_manifest(monkeypatch) -> None:
    # Matches jsonl.py's empty-input convention: zero task rows -> no manifest,
    # never an empty stub.
    manifest = build_manifest([], _settings(monkeypatch), split_enabled=False)
    assert manifest is None


def test_reward_convention_matches_documented_verifiers_mapping(monkeypatch) -> None:
    manifest = build_manifest(
        [_task_row()], _settings(monkeypatch), split_enabled=False
    )
    assert manifest is not None
    env = manifest["environments"][0]
    # CI passed -> 1.0 / failed -> 0.0, the same convention docs/exports/rlvr-export.md
    # documents for the Verifiers adapter mapping.
    assert env["reward"] == {"kind": "ci", "pass_value": 1.0, "fail_value": 0.0}
    assert manifest["spec_version"] == "hf-rl-env-0.1"
    assert manifest["experimental"] is True


def test_runtime_reference_present_when_every_row_agrees(monkeypatch) -> None:
    rows = [_task_row(split="train"), _task_row(split="eval")]
    manifest = build_manifest(rows, _settings(monkeypatch), split_enabled=True)
    assert manifest is not None
    ref = manifest["environments"][0]["runtime_reference"]
    assert ref == {
        "kind": "ci_workflow",
        "repo": REPO,
        "workflow_name": WORKFLOW_NAME,
        "workflow_path": WORKFLOW_PATH,
    }


def test_runtime_reference_accepts_repeated_attempts_of_one_workflow(
    monkeypatch,
) -> None:
    row = _task_row()
    row.body["verifier_results"].append(
        {
            "workflow_name": WORKFLOW_NAME,
            "workflow_path": WORKFLOW_PATH,
            "result": "passed",
        }
    )

    manifest = build_manifest([row], _settings(monkeypatch), split_enabled=False)

    assert manifest is not None
    assert manifest["environments"][0]["runtime_reference"]["workflow_path"] == (
        WORKFLOW_PATH
    )


def test_runtime_reference_absent_for_multiple_workflows_in_one_resolution(
    monkeypatch,
) -> None:
    row = _task_row()
    row.body["verifier_results"].append(
        {
            "workflow_name": "Lint",
            "workflow_path": ".github/workflows/lint.yml",
            "result": "passed",
        }
    )

    manifest = build_manifest([row], _settings(monkeypatch), split_enabled=False)

    assert manifest is not None
    assert "runtime_reference" not in manifest["environments"][0]


def test_runtime_reference_absent_when_workflow_paths_diverge(monkeypatch) -> None:
    # A taskset spanning two repos/workflows has no single truthful runtime
    # reference — absent, never guessed (the verification-command discipline).
    rows = [
        _task_row(repo=REPO),
        _task_row(repo=OTHER_REPO, workflow_path=".gh/other.yml"),
    ]
    manifest = build_manifest(rows, _settings(monkeypatch), split_enabled=False)
    assert manifest is not None
    assert "runtime_reference" not in manifest["environments"][0]


def test_runtime_reference_absent_when_no_workflow_path_resolves(monkeypatch) -> None:
    rows = [_task_row(workflow_path=None)]
    manifest = build_manifest(rows, _settings(monkeypatch), split_enabled=False)
    assert manifest is not None
    assert "runtime_reference" not in manifest["environments"][0]


def test_runtime_reference_absent_when_any_row_lacks_a_workflow(monkeypatch) -> None:
    # "Every emitted task row agrees" must mean EVERY row: a workflow-less
    # row (CIOutcome.workflow_path is Optional) is a disagreement, not a
    # bystander — otherwise a mixed taskset gets blanketed by whichever repo
    # happens to carry a workflow path.
    rows = [
        _task_row(repo=REPO),
        _task_row(repo=OTHER_REPO, workflow_name="", workflow_path=None),
    ]
    manifest = build_manifest(rows, _settings(monkeypatch), split_enabled=False)
    assert manifest is not None
    assert "runtime_reference" not in manifest["environments"][0]


def test_blank_openenv_config_counts_as_unconfigured(monkeypatch) -> None:
    # A whitespace-only env var must not become a garbage "runnable" target
    # in the RFC-actionable slot (VerifierCommands.load's own posture).
    assert _settings(monkeypatch, image="   ").resolve() is None
    assert _settings(monkeypatch, package=" \t").resolve() is None


def test_frameworks_openenv_absent_when_unconfigured(monkeypatch) -> None:
    manifest = build_manifest(
        [_task_row()], _settings(monkeypatch), split_enabled=False
    )
    assert manifest is not None
    assert "frameworks" not in manifest["environments"][0]


def test_frameworks_openenv_image_from_operator_setting(monkeypatch) -> None:
    manifest = build_manifest(
        [_task_row()],
        _settings(monkeypatch, image="ghcr.io/acme/coding-env:1.0"),
        split_enabled=False,
    )
    assert manifest is not None
    assert manifest["environments"][0]["frameworks"] == {
        "openenv": {"image": "ghcr.io/acme/coding-env:1.0"}
    }


def test_frameworks_openenv_package_from_operator_setting(monkeypatch) -> None:
    manifest = build_manifest(
        [_task_row()],
        _settings(monkeypatch, package="acme-coding-env>=1.0"),
        split_enabled=False,
    )
    assert manifest is not None
    assert manifest["environments"][0]["frameworks"] == {
        "openenv": {"package": "acme-coding-env>=1.0"}
    }


def test_frameworks_nemo_gym_from_operator_setting(monkeypatch) -> None:
    manifest = build_manifest(
        [_task_row()],
        _settings(monkeypatch),
        split_enabled=False,
        nemo_gym_settings=_nemo_settings(monkeypatch, resources_server="swe_gym"),
    )
    assert manifest is not None
    assert manifest["environments"][0]["frameworks"] == {
        "nemo_gym": {"resources_server": "swe_gym"}
    }


def test_frameworks_nemo_gym_config_rides_along(monkeypatch) -> None:
    manifest = build_manifest(
        [_task_row()],
        _settings(monkeypatch),
        split_enabled=False,
        nemo_gym_settings=_nemo_settings(
            monkeypatch,
            resources_server="swe_gym",
            config="resources_servers/swe_gym/configs/swe_gym.yaml",
        ),
    )
    assert manifest is not None
    assert manifest["environments"][0]["frameworks"]["nemo_gym"] == {
        "resources_server": "swe_gym",
        "config": "resources_servers/swe_gym/configs/swe_gym.yaml",
    }


def test_frameworks_openenv_and_nemo_gym_coexist(monkeypatch) -> None:
    # Alternative runtimes for the same taskset, not alternatives to each
    # other — both blocks present, fixed openenv-then-nemo_gym key order so
    # repeated exports stay diffable.
    manifest = build_manifest(
        [_task_row()],
        _settings(monkeypatch, image="ghcr.io/acme/coding-env:1.0"),
        split_enabled=False,
        nemo_gym_settings=_nemo_settings(monkeypatch, resources_server="swe_gym"),
    )
    assert manifest is not None
    frameworks = manifest["environments"][0]["frameworks"]
    assert frameworks == {
        "openenv": {"image": "ghcr.io/acme/coding-env:1.0"},
        "nemo_gym": {"resources_server": "swe_gym"},
    }
    assert list(frameworks) == ["openenv", "nemo_gym"]


def test_frameworks_absent_when_neither_framework_configured(monkeypatch) -> None:
    manifest = build_manifest(
        [_task_row()],
        _settings(monkeypatch),
        split_enabled=False,
        nemo_gym_settings=_nemo_settings(monkeypatch),
    )
    assert manifest is not None
    assert "frameworks" not in manifest["environments"][0]


def test_blank_nemo_gym_config_counts_as_unconfigured(monkeypatch) -> None:
    assert _nemo_settings(monkeypatch, resources_server="   ").resolve() is None


def test_nemo_gym_config_without_server_is_unconfigured(monkeypatch, caplog) -> None:
    # A config path alone is not a launchable reference (gym env start takes
    # the resources-server name) — logged and treated as unconfigured, never
    # emitted as a garbage entry in the actionable slot.
    import logging

    settings = _nemo_settings(monkeypatch, config="resources_servers/x/configs/x.yaml")
    with caplog.at_level(
        logging.WARNING, logger="sediment.export.environment_manifest"
    ):
        resolved = settings.resolve()
    assert resolved is None
    assert any(
        r.message == "nemo_gym_config_without_resources_server" for r in caplog.records
    )


def test_workflow_reference_and_operator_frameworks_coexist(monkeypatch) -> None:
    # The informational CI-workflow reference and the actionable operator
    # runtime are independent: both can be present at once.
    manifest = build_manifest(
        [_task_row()],
        _settings(monkeypatch, image="ghcr.io/acme/coding-env:1.0"),
        split_enabled=False,
    )
    assert manifest is not None
    env = manifest["environments"][0]
    assert env["runtime_reference"]["kind"] == "ci_workflow"
    assert env["frameworks"]["openenv"] == {"image": "ghcr.io/acme/coding-env:1.0"}


def test_splits_listed_only_when_split_enabled(monkeypatch) -> None:
    rows = [_task_row(split="train"), _task_row(split="eval")]
    disabled = build_manifest(rows, _settings(monkeypatch), split_enabled=False)
    enabled = build_manifest(rows, _settings(monkeypatch), split_enabled=True)
    assert disabled is not None and "splits" not in disabled["environments"][0]
    assert enabled is not None
    assert enabled["environments"][0]["splits"] == ["train", "eval"]


def test_write_manifest_none_writes_nothing_and_leaves_prior_file(
    tmp_path: Path,
) -> None:
    prior = tmp_path / "environment.yaml"
    prior.write_text("kept: true\n")

    result = write_manifest(None, tmp_path)

    assert result is None
    assert prior.read_text() == "kept: true\n"  # byte-for-byte untouched


def test_write_manifest_writes_valid_yaml(tmp_path: Path, monkeypatch) -> None:
    manifest = build_manifest(
        [_task_row()], _settings(monkeypatch), split_enabled=False
    )
    path = write_manifest(manifest, tmp_path)

    assert path == tmp_path / "environment.yaml"
    assert path.exists()
    loaded = yaml.safe_load(path.read_text())
    assert loaded == manifest


def test_write_manifest_creates_parent_directory(tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / "nested" / "out"
    manifest = build_manifest(
        [_task_row()], _settings(monkeypatch), split_enabled=False
    )
    write_manifest(manifest, out)
    assert (out / "environment.yaml").exists()


def test_write_manifest_no_temp_files_left_behind(tmp_path: Path, monkeypatch) -> None:
    manifest = build_manifest(
        [_task_row()], _settings(monkeypatch), split_enabled=False
    )
    write_manifest(manifest, tmp_path)
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "environment.yaml"]
    assert leftovers == []


def test_settings_unconfigured_resolves_to_none(monkeypatch) -> None:
    assert _settings(monkeypatch).resolve() is None


def test_settings_image_wins_when_both_configured(monkeypatch, caplog) -> None:
    settings = _settings(
        monkeypatch, image="ghcr.io/acme/env:1.0", package="acme-env>=1.0"
    )
    import logging

    with caplog.at_level(
        logging.WARNING, logger="sediment.export.environment_manifest"
    ):
        resolved = settings.resolve()
    assert resolved == {"image": "ghcr.io/acme/env:1.0"}
    assert any(r.message == "openenv_runtime_ambiguous_config" for r in caplog.records)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def _work_repo(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "dev@example.com")
    _git(work, "config", "user.name", "Dev")
    return work


def _commit(work: Path, message: str) -> str:
    _git(work, "add", "-A")
    subprocess.run(
        ["git", "commit", "-q", "-m", message],
        cwd=work,
        check=True,
        capture_output=True,
        text=True,
        env=dict(os.environ),
    )
    return _git(work, "rev-parse", "HEAD").strip()


def _make_remote(tmp_path: Path, work: Path) -> Path:
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    _git(work, "push", "-q", str(remote), "refs/heads/*:refs/heads/*")
    _git(work, "push", "-q", str(remote), "refs/notes/*:refs/notes/*")
    return remote


def test_export_rlvr_writes_environment_manifest_beside_tasks(
    tmp_path: Path, postgres_store, monkeypatch
) -> None:
    # export_rlvr constructs its settings from the live process env, so the
    # very knobs this module introduces must be cleared for the "frameworks
    # not in env" assertion to be hermetic on a configured machine.
    monkeypatch.delenv("SEDIMENT_OPENENV_IMAGE", raising=False)
    monkeypatch.delenv("SEDIMENT_OPENENV_PACKAGE", raising=False)
    monkeypatch.delenv("SEDIMENT_NEMO_GYM_RESOURCES_SERVER", raising=False)
    monkeypatch.delenv("SEDIMENT_NEMO_GYM_CONFIG", raising=False)
    import json

    work = _work_repo(tmp_path)
    (work / "math_utils.py").write_text("")
    _commit(work, "scaffold")
    (work / "math_utils.py").write_text("def fib(n):\n    return n\n")
    head = _commit(work, "add fib")
    note = json.dumps(
        {
            "v": 1,
            "sessions": [
                {
                    "tool": "claude-code",
                    "session_id": "sess-1",
                    "stamped_at": "2026-07-15T00:00:00+00:00",
                }
            ],
        }
    )
    _git(work, "notes", "--ref=sediment", "add", "-m", note, head)

    store = postgres_store
    remote = _make_remote(tmp_path, work)
    mirrors = MirrorManager(str(tmp_path / "mirrors"))
    push = Push(
        org_id=ORG,
        provider=ForgeProvider.GITHUB,
        repo=REPO,
        clone_url=str(remote),
        ref="refs/heads/main",
        before_sha="0" * 40,
        after_sha=head,
    )
    mirrors.ensure(push)
    store.store_push(push)
    store.store_inference_call(
        inference_call(
            inference_call_id="inference-1",
            org_id=ORG,
            session_id="sess-1",
            model="claude-sonnet-5",
            input_messages=[message("user", "fix fib")],
            output="def fib(n):\n    return n\n",
        )
    )
    from sediment_core import CIOutcome, CIProvider

    store.store_ci_outcome(
        CIOutcome(
            org_id=ORG,
            provider=CIProvider.GITHUB_ACTIONS,
            run_id="run/1",
            repo=REPO,
            commit_sha=head,
            branch="main",
            result=CIResult.PASSED,
            workflow_name=WORKFLOW_NAME,
            workflow_path=WORKFLOW_PATH,
            run_url="run/1",
        )
    )

    out = tmp_path / "export"
    summary = export_rlvr(store, mirrors, ORG, out, target="sediment")

    assert summary["task_rows"] == 1
    manifest_path = out / "environment.yaml"
    assert manifest_path.exists()
    assert summary["environment_manifest"] == str(manifest_path)
    manifest = yaml.safe_load(manifest_path.read_text())
    env = manifest["environments"][0]
    assert env["reward"] == {"kind": "ci", "pass_value": 1.0, "fail_value": 0.0}
    assert env["runtime_reference"] == {
        "kind": "ci_workflow",
        "repo": REPO,
        "workflow_name": WORKFLOW_NAME,
        "workflow_path": WORKFLOW_PATH,
    }
    assert "frameworks" not in env  # no operator SEDIMENT_OPENENV_* configured


def test_export_rlvr_no_manifest_when_no_task_rows(
    tmp_path: Path, postgres_store
) -> None:
    # A session with a completion but no push/notes/attribution binding
    # produces a rollout row but no task row -> no manifest either.
    store = postgres_store
    store.store_inference_call(
        inference_call(
            inference_call_id="inference-1",
            org_id=ORG,
            session_id="sess-1",
            model="claude-sonnet-5",
            input_messages=[message("user", "hi")],
            output="unrelated output",
        )
    )
    mirrors = MirrorManager(str(tmp_path / "mirrors"))

    out = tmp_path / "export"
    summary = export_rlvr(store, mirrors, ORG, out, target="sediment")

    assert summary["task_rows"] == 0
    assert summary["environment_manifest"] is None
    assert not (out / "environment.yaml").exists()
