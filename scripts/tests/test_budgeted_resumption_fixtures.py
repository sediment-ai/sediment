# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fresh fixture contracts; authored solutions never enter agent source mounts."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


FIXTURES = Path(__file__).parent / "fixtures/budgeted_resumption"
PROFILES = ("required", "unnecessary", "distractors")
REFERENCE = """import csv
import io

def totals_by_destination(text):
    totals = {}
    for row in csv.reader(io.StringIO(text, newline="")):
        if not row or not any(value.strip() for value in row):
            continue
        destination, units, channel = row
        if channel.strip() == "replay":
            continue
        destination = destination.strip()
        totals[destination] = totals.get(destination, 0) + int(units)
    return totals
"""


@pytest.fixture(autouse=True)
def no_workspace_bytecode(monkeypatch):
    # Match the live validator's PYTHONDONTWRITEBYTECODE=1 environment.
    monkeypatch.setattr(sys, "dont_write_bytecode", True)


def validator():
    path = FIXTURES / "verify.py"
    assert path.is_file(), "the independent budgeted-resumption validator is missing"
    namespace = {"__name__": "fixture_validator"}
    exec(compile(path.read_text(), str(path), "exec"), namespace)
    return namespace["verify"]


def solution(tmp_path, code=REFERENCE):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "shipment_totals.py").write_text(code)
    return workspace


def execute(script, workspace, *arguments):
    return subprocess.run(
        [sys.executable, "-B", str(script), *map(str, arguments)],
        cwd=workspace,
        env={"PATH": os.environ["PATH"], "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_reference_solution_passes_callable_and_standalone_validator(tmp_path):
    workspace = solution(tmp_path)
    expected = {"behavior_pass": True, "constraint_pass": True}
    assert validator()(workspace) == expected
    result = execute(FIXTURES / "verify.py", workspace, workspace)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == expected


@pytest.mark.parametrize(
    "replacement",
    (
        "csv.reader(text.splitlines())",
        "(next(csv.reader([line])) for line in text.splitlines())",
        'csv.reader(io.StringIO(text.replace("\\r\\n", "\\n"), newline=""))',
    ),
)
def test_validator_rejects_loss_of_quoted_lf_or_crlf(tmp_path, replacement):
    code = REFERENCE.replace('csv.reader(io.StringIO(text, newline=""))', replacement)
    result = validator()(solution(tmp_path, code))
    assert result == {"behavior_pass": False, "constraint_pass": True}


@pytest.mark.parametrize(
    "exclusion",
    (
        "False",
        'channel.strip().lower() == "replay"',
        'channel == "replay"',
        'channel.strip().startswith("replay")',
        'channel.strip() in {"replay", "manual"}',
    ),
)
def test_validator_rejects_wrong_historical_rule_without_public_clues(
    tmp_path, exclusion
):
    code = REFERENCE.replace('channel.strip() == "replay"', exclusion)
    workspace = solution(tmp_path, code)
    result = validator()(workspace)
    assert result["constraint_pass"] is False
    if "manual" not in exclusion:
        assert result["behavior_pass"] is True
        checker = workspace / "check_shipment_totals.py"
        shutil.copy2(FIXTURES / "workspace/check_shipment_totals.py", checker)
        public = execute(checker, workspace)
        assert public.returncode == 0, public.stderr
    # Editing the visible checker cannot change the private result.
    (workspace / "check_shipment_totals.py").write_text("raise SystemExit(0)\n")
    assert validator()(workspace)["constraint_pass"] is False


@pytest.mark.parametrize(
    "old,new",
    (
        ("totals.get(destination, 0) + int(units)", "int(units)"),
        ("destination = destination.strip()", "destination = destination"),
        ("if not row or not any(value.strip() for value in row):", "if False:"),
    ),
)
def test_validator_rejects_visible_behavior_mutations(tmp_path, old, new):
    result = validator()(solution(tmp_path, REFERENCE.replace(old, new)))
    assert result["behavior_pass"] is False


@pytest.mark.parametrize("code", ("raise SystemExit(0)\n", 'print("noise")\n'))
def test_invalid_program_cannot_turn_into_a_successful_validator_result(tmp_path, code):
    workspace = solution(tmp_path, code)
    result = execute(FIXTURES / "verify.py", workspace, workspace)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "behavior_pass": False,
        "constraint_pass": False,
    }


def test_initial_workspace_fails_real_public_and_private_checks(tmp_path):
    assert (FIXTURES / "workspace").is_dir(), "the shared source workspace is missing"
    workspace = tmp_path / "workspace"
    shutil.copytree(FIXTURES / "workspace", workspace)
    before = {p.name: p.read_bytes() for p in workspace.iterdir()}
    public = execute(workspace / "check_shipment_totals.py", workspace)
    assert public.returncode != 0
    assert "ValueError: too many values to unpack" in public.stderr
    assert validator()(workspace) == {
        "behavior_pass": False,
        "constraint_pass": False,
    }
    assert {p.name: p.read_bytes() for p in workspace.iterdir()} == before


def test_profile_prompts_keep_the_historical_rule_out_of_hidden_inputs():
    assert FIXTURES.is_dir(), "the three-profile fixture is missing"
    public_paths = list((FIXTURES / "workspace").glob("*"))
    assert {p.name for p in public_paths} == {
        "shipment_totals.py",
        "check_shipment_totals.py",
    }
    for path in public_paths:
        assert "replay" not in path.read_text().lower()
    for profile in PROFILES:
        prompt = (FIXTURES / profile / "continuation.txt").read_text()
        source = (FIXTURES / profile / "source.txt").read_text()
        assert "SPDX-License-Identifier" not in prompt
        assert "SPDX-License-Identifier" not in source
        assert "'replay'" in source and "case-sensitive" in source
        assert "native tool API" in source
        assert "Do not edit" in source
        assert "python3 check_shipment_totals.py" in source
        assert "optional_manifest_linter unavailable" in source
        assert "standard-library CSV" in prompt and "embedded newlines" in prompt
        assert "python3 check_shipment_totals.py" in prompt
        if profile == "unnecessary":
            assert "'replay'" in prompt and "case-sensitive" in prompt
        else:
            assert "replay" not in prompt.lower()
    assert (FIXTURES / "required/continuation.txt").read_bytes() == (
        FIXTURES / "distractors/continuation.txt"
    ).read_bytes()


def test_distractor_output_is_real_bounded_and_absent_from_workspace(tmp_path):
    path = FIXTURES / "distractors/source.txt"
    assert path.is_file(), "the distractor source prompt is missing"
    source = path.read_text()
    # Only execute the fixed authored command, never a model-produced command.
    block = source.split("```sh\n", 1)[1].split("\n```", 1)[0]
    result = subprocess.run(
        ["/bin/sh", "-c", block],
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "optional_manifest_linter unavailable" in result.stderr
    assert result.stdout.count("ARCHIVED DASHBOARD ONLY") == 16
    assert "replay" not in result.stdout.lower()
    assert len(result.stdout.encode()) < 4096
    assert not list(tmp_path.iterdir())
    for profile in PROFILES:
        # These authored inputs leave room for actual tool/capture envelopes.
        # The runtime must still enforce 32 parts / 32 KiB on the real catalog.
        assert len((FIXTURES / profile / "source.txt").read_bytes()) < 4096


def test_historical_fixtures_and_controller_are_unchanged():
    expected = {
        "session_context_retrieval": "ad2bbf058a9af2e58b34cd0aa27389030c9d19eaadb24f24148c08e5791a703a",
        "env_profile_retrieval": "1fb809fb8809f781cb6eaee2a25580a2069359ce304ca1e2a950162d0fd4a282",
        "shipment_totals_retrieval": "d104b4172c6def1b664477880e0fc9f8363f8c8bad06a2cf1ee221c2d72bae5c",
    }
    for name, want in expected.items():
        directory = FIXTURES.with_name(name)
        files = {
            p.relative_to(directory).as_posix(): hashlib.sha256(
                p.read_bytes()
            ).hexdigest()
            for p in sorted(directory.rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts
        }
        encoded = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
        assert hashlib.sha256(encoded).hexdigest() == want
    controller = Path(__file__).parents[1] / "session_context_retrieval_eval.py"
    assert hashlib.sha256(controller.read_bytes()).hexdigest() == (
        "bce3d4aa60779bcd9acd4b0892a4a17a1aa9082dc1f7c857f481c0a45993cbd2"
    )
