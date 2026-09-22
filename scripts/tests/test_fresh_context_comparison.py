# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fresh task selection preserves the frozen earlier comparison and its rules."""

import ast
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).parents[1] / "session_context_retrieval_eval.py"


def load():
    spec = importlib.util.spec_from_file_location("fresh_context_comparison", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_original_invoice_fixture_bytes_remain_unchanged():
    mod = load()
    assert {
        p.relative_to(mod.FIXTURES).as_posix(): mod.digest(p.read_bytes())
        for p in sorted(mod.FIXTURES.rglob("*"))
        if p.is_file()
    } == {
        "continuation.txt": "907267b8bffb3e65c356b4885b3b95b861c85d95b2704dec88f958b9be39bcfa",
        "source.txt": "98a1934cf8ff11ae66baa749fdb1bb9ac1eca325b41bc5c557329494e473926c",
        "verify.py": "64d5a6a86ac5f544abf4a5b78d0a598e3048417d6472dac78243e0bb34a05288",
        "workspace/check_invoice_csv.py": "bc1ca54ed37ee20b873e8a8f5afbda3338cbfc8d34f2e3ceb5af3c1dd249273e",
        "workspace/invoice_csv.py": "a21526bcbd240c6df0942e8a978af73ed06dbbe71f63cdc0b375e8492c255ac1",
    }


def test_selected_task_is_frozen_and_cross_task_source_is_refused(
    tmp_path, monkeypatch
):
    mod = load()
    monkeypatch.setattr(
        mod, "command", lambda *a, **kw: b"0.84.1\nv24.14.0\nPython 3.12.14\n"
    )
    config = {"agent_image": "agent", "gate_image": "gate"}
    original = mod.freeze(config)
    fresh = mod.freeze(config, "env-profile")
    assert original["task"] == "invoice" and fresh["task"] == "env-profile"
    assert original["fixture_hashes"] != fresh["fixture_hashes"]
    assert fresh["fixture_hashes"]["workspace/env_profile.py"]
    for key in ("temperature", "output_limit", "context_window", "run_order", "model"):
        assert original[key] == fresh[key]
    source = mod.private_directory(tmp_path / "source")
    mod.write_json(source / "freeze.json", original)
    with pytest.raises(mod.EvaluationError, match="evaluation_changed_after_source"):
        mod.run_comparison(config, source, tmp_path / "unused", "env-profile")
    assert not (tmp_path / "unused").exists()


def run_python(script, workspace):
    return subprocess.run(
        [sys.executable, "-B", str(script)]
        + ([str(workspace)] if script.name == "verify.py" else []),
        cwd=workspace,
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        timeout=10,
    )


def implementation(first_wins):
    assignment = (
        "result.setdefault(key, value)" if first_wins else "result[key] = value"
    )
    return (
        "def parse_profile(text):\n"
        "    result = {}\n"
        "    for line in text.splitlines():\n"
        "        line = line.strip()\n"
        "        if not line or line.startswith('#'):\n"
        "            continue\n"
        "        key, value = (piece.strip() for piece in line.split('=', 1))\n"
        f"        {assignment}\n"
        "    return result\n"
    )


@pytest.mark.parametrize("first_wins", [False, True])
def test_private_validator_separates_goal_from_historical_constraint(
    tmp_path, first_wins
):
    mod = load()
    fixture = mod.task_fixture("env-profile")
    workspace = tmp_path / "workspace"
    shutil.copytree(fixture / "workspace", workspace)
    (workspace / "env_profile.py").write_text(implementation(first_wins))
    visible = run_python(workspace / "check_env_profile.py", workspace)
    assert visible.returncode == 0, visible.stderr
    checked = run_python(fixture / "verify.py", workspace)
    assert checked.returncode == 0, checked.stderr
    assert json.loads(checked.stdout) == {
        "behavior_pass": True,
        "constraint_pass": first_wins,
    }
    (workspace / "check_env_profile.py").write_text("raise SystemExit(0)\n")
    assert (
        json.loads(run_python(fixture / "verify.py", workspace).stdout)[
            "constraint_pass"
        ]
        is first_wins
    )


def test_fresh_source_starts_with_actual_relevant_failure(tmp_path):
    mod = load()
    fixture = mod.task_fixture("env-profile")
    workspace = tmp_path / "workspace"
    shutil.copytree(fixture / "workspace", workspace)
    before = (workspace / "env_profile.py").read_bytes()
    checked = run_python(workspace / "check_env_profile.py", workspace)
    assert checked.returncode != 0
    assert "ValueError: too many values to unpack" in checked.stderr
    assert (workspace / "env_profile.py").read_bytes() == before
    assert json.loads(run_python(fixture / "verify.py", workspace).stdout) == {
        "behavior_pass": False,
        "constraint_pass": False,
    }


def test_historical_constraint_is_absent_from_public_checks_and_continuation():
    fixture = load().task_fixture("env-profile")
    source = (fixture / "source.txt").read_text()
    assert "repeated exact keys" in source and "FIRST observed value" in source
    public = (fixture / "workspace/check_env_profile.py").read_text()
    calls = [
        node
        for node in ast.walk(ast.parse(public))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "parse_profile"
    ]
    assert calls
    for call in calls:
        text = ast.literal_eval(call.args[0])
        keys = [
            line.split("=", 1)[0].strip()
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        assert len(keys) == len(set(keys))
    for path in [fixture / "continuation.txt", *(fixture / "workspace").glob("*.py")]:
        text = path.read_text().lower()
        assert all(
            term not in text for term in ("duplicate", "repeated", "first observed")
        )


def test_selected_validator_runs_outside_agent_with_private_fixture(
    tmp_path, monkeypatch
):
    mod = load()
    seen = []

    class Validator:
        def __init__(self, arguments, records, **kwargs):
            seen.extend(arguments)
            self.process = type("Process", (), {"returncode": 0})()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def _receive(self):
            return {"behavior_pass": True, "constraint_pass": True}

    monkeypatch.setattr(mod, "RpcProcess", Validator)
    checked = mod.validate_workspace(
        {"agent_image": "pinned-image"}, tmp_path, tmp_path, "env-profile"
    )
    assert checked == {
        "behavior_pass": True,
        "constraint_pass": True,
        "verification_error": None,
    }
    assert seen[seen.index("--network") + 1] == "none"
    assert (
        f"type=bind,src={mod.task_fixture('env-profile') / 'verify.py'},dst=/verify.py,readonly"
        in seen
    )
    assert not any("TOKEN" in argument for argument in seen)


def test_fresh_gold_uses_observed_parts_and_keeps_exact_references():
    mod = load()
    parts = [
        {
            "type": "text",
            "content": "For repeated exact keys, keep the FIRST observed value.",
        },
        {
            "type": "tool_call_response",
            "id": "failure",
            "result": "ValueError: too many values to unpack (expected 2)",
        },
        {
            "type": "tool_call_response",
            "id": "distractor",
            "result": "RuntimeError: optional_schema_linter unavailable",
        },
    ]
    items = [
        {
            "reference": {
                "inference_call_id": "actual-call",
                "side": "input",
                "message_index": i,
                "part_index": 0,
            },
            "part": part,
        }
        for i, part in enumerate(parts)
    ]
    assert mod.source_gold(items, "env-profile") == dict(
        zip(
            ("constraint", "failure", "distractor"),
            ([item] for item in items),
            strict=True,
        )
    )
    parts[1]["type"] = "tool_call"
    with pytest.raises(mod.EvaluationError, match="source_evidence_missing"):
        mod.source_gold(items, "env-profile")


@pytest.mark.parametrize("operation", ["source", "run"])
def test_cli_passes_selected_task_to_source_and_comparison(
    tmp_path, monkeypatch, operation
):
    mod = load()
    monkeypatch.setattr(mod, "load_config", lambda _: {})
    calls = []

    def selected(*args):
        calls.append(args)
        return {"status": "captured", "benefit_demonstrated": True}

    monkeypatch.setattr(mod, "source_run", selected)
    monkeypatch.setattr(mod, "run_comparison", selected)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            operation,
            "--config",
            "private.json",
            "--output",
            str(tmp_path / "run"),
            "--source",
            str(tmp_path / "source"),
            "--task",
            "env-profile",
        ],
    )
    assert mod.main() == 0
    assert calls[0][-1] == "env-profile"
