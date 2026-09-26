# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bounded-selection fixtures: independent validators and input isolation."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).parent / "fixtures/bounded_selection"
FAMILIES = ROOT / "families"
EVALUATION = ROOT / "evaluation"
LABELS = json.loads((EVALUATION / "labels.json").read_bytes())
PROFILES = ("missing", "redundant", "correction")
SELECTOR = Path(__file__).parents[1] / "bounded_evidence_selection.py"

REFERENCE = {
    "event-rollup": """import json

def action_counts(text):
    latest = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        latest[event["id"]] = event["action"].strip()
    counts = {}
    for action in latest.values():
        counts[action] = counts.get(action, 0) + 1
    return counts
""",
    "layered-config": """def resolve(layers):
    result = {}
    for layer in layers:
        for line in layer.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if value:
                result[key] = value
            else:
                result.pop(key, None)
    return result
""",
    "duration-parse": """import re

def to_seconds(text):
    text = text.strip()
    if text.isdigit():
        return int(text) * 60
    units = {"h": 3600, "m": 60, "s": 1}
    return sum(int(n) * units[u] for n, u in re.findall(r"(\\d+)([hms])", text))
""",
}
# (old, new, behavior_pass, constraint_pass, visible_checks_pass)
MUTATIONS = {
    "event-rollup": {
        "no_rule": (
            'latest[event["id"]] = event["action"].strip()',
            'latest[len(latest)] = event["action"].strip()',
            True,
            False,
            True,
        ),
        "obsolete_rule": (
            'latest[event["id"]] = event["action"].strip()',
            'latest.setdefault(event["id"], event["action"].strip())',
            True,
            False,
            True,
        ),
        "ordinary_behavior": (
            'event["action"].strip()',
            'event["action"]',
            False,
            True,
            False,
        ),
        "unrelated_diagnostic": (
            'event["action"].strip()',
            'event["action"].strip().upper()',
            False,
            False,
            False,
        ),
    },
    "layered-config": {
        "no_rule": (
            "if value:",
            "if True:",
            True,
            False,
            True,
        ),
        "obsolete_rule": (
            "result.pop(key, None)",
            "pass",
            True,
            False,
            True,
        ),
        "ordinary_behavior": (
            'line.split("=", 1)',
            'line.split("=", 1)[::-1]',
            False,
            False,
            False,
        ),
        "unrelated_diagnostic": (
            "key, value = key.strip(), value.strip()",
            "key, value = key.strip().lower(), value.strip()",
            False,
            False,
            False,
        ),
    },
    "duration-parse": {
        "no_rule": (
            "return int(text) * 60",
            "raise ValueError(text)",
            True,
            False,
            True,
        ),
        "obsolete_rule": (
            "return int(text) * 60",
            "return int(text)",
            True,
            False,
            True,
        ),
        "ordinary_behavior": (
            '"s": 1',
            '"s": 0',
            False,
            True,
            False,
        ),
        "unrelated_diagnostic": (
            "return sum(",
            "return 3600 * round(1 / 3600 * sum(",
            False,
            False,
            False,
        ),
    },
}
MODULES = {
    name: (value["module"], "check_" + value["module"])
    for name, value in LABELS["families"].items()
}


@pytest.fixture(autouse=True)
def no_workspace_bytecode(monkeypatch):
    monkeypatch.setattr(sys, "dont_write_bytecode", True)


def validator():
    namespace = {"__name__": "bounded_fixture_validator"}
    path = EVALUATION / "verify.py"
    exec(compile(path.read_text(), str(path), "exec"), namespace)
    return namespace["verify"]


def workspace(tmp_path, family, code):
    directory = tmp_path / family
    shutil.copytree(FAMILIES / family / "workspace", directory)
    (directory / MODULES[family][0]).write_text(code)
    return directory


def execute(script, cwd, *arguments, isolated=True):
    flags = ["-I", "-B"] if isolated else ["-B"]
    return subprocess.run(
        [sys.executable, *flags, str(script), *map(str, arguments)],
        cwd=cwd,
        env={"PATH": os.environ["PATH"], "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=20,
    )


def visible(directory, family):
    # The agent runs `python3 check_*.py` from the workspace, as here.
    check = directory / MODULES[family][1]
    return execute(check, directory, isolated=False).returncode == 0


@pytest.mark.parametrize("family", sorted(REFERENCE))
def test_reference_passes_and_initial_program_fails(tmp_path, family):
    reference = workspace(tmp_path / "reference", family, REFERENCE[family])
    expected = {"behavior_pass": True, "constraint_pass": True}
    assert validator()(family, reference) == expected
    standalone = execute(EVALUATION / "verify.py", tmp_path, family, reference)
    assert standalone.returncode == 0, standalone.stderr
    assert json.loads(standalone.stdout) == expected
    assert visible(reference, family)
    initial = FAMILIES / family / "workspace"
    assert validator()(family, initial) == {
        "behavior_pass": False,
        "constraint_pass": False,
    }
    assert not visible(initial, family)


@pytest.mark.parametrize(
    "family,mutation",
    [(family, name) for family in MUTATIONS for name in MUTATIONS[family]],
)
def test_mutations_fail_their_intended_checks(tmp_path, family, mutation):
    old, new, behavior, constraint, public = MUTATIONS[family][mutation]
    assert REFERENCE[family].count(old) == 1
    directory = workspace(tmp_path, family, REFERENCE[family].replace(old, new))
    result = validator()(family, directory)
    assert result == {"behavior_pass": behavior, "constraint_pass": constraint}
    # Visible checks never reveal the historical rule.
    assert visible(directory, family) is public
    (directory / MODULES[family][1]).write_text("raise SystemExit(0)\n")
    assert validator()(family, directory) == result


@pytest.mark.parametrize("family", sorted(REFERENCE))
def test_generated_exit_is_a_failed_check_not_a_verdict(tmp_path, family):
    directory = workspace(tmp_path, family, "raise SystemExit(0)\n")
    assert validator()(family, directory) == {
        "behavior_pass": False,
        "constraint_pass": False,
    }


def model_inputs(family, profile):
    source = json.loads((FAMILIES / family / profile / "source.json").read_bytes())
    continuation = (FAMILIES / family / profile / "continuation.txt").read_text()
    return source["prompts"], continuation


@pytest.mark.parametrize("family", sorted(LABELS["families"]))
@pytest.mark.parametrize("profile", PROFILES)
def test_profiles_place_labels_only_where_designed(family, profile):
    labels = LABELS["families"][family]
    prompts, continuation = model_inputs(family, profile)
    history = "\n".join(prompts)
    for term in LABELS["profiles"][profile]["source_requires"]:
        assert labels[term] in history
    assert (labels["rule"] in continuation.lower()) is (profile == "redundant")
    assert labels["obsolete"] not in continuation
    assert (labels["obsolete"] in history) is (profile == "correction")
    if profile == "correction":
        correction = prompts[-1]
        assert correction.startswith("Correction from") and labels["rule"] in correction
        assert labels["rule"] not in prompts[1]
    # Private evaluation material never enters either model's input.
    for text in [*prompts, continuation]:
        for private in ("verify.py", "labels.json", "behavior_pass", "reference"):
            assert private not in text


def test_workspaces_hold_only_the_program_and_visible_checks():
    for family, (module, check) in MODULES.items():
        files = sorted(
            p.name for p in (FAMILIES / family / "workspace").iterdir() if p.is_file()
        )
        assert files == sorted([module, check])
    labels = json.dumps(LABELS)
    for path in FAMILIES.rglob("*"):
        if path.is_file():
            assert "shipment" not in path.read_text()
    assert "workspace" not in {p.name for p in EVALUATION.iterdir()}
    assert "rule" in labels


def load_selector():
    spec = importlib.util.spec_from_file_location("bounded_fixture_sel", SELECTOR)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# A stand-in for the harness system prompt with similar vocabulary and size.
SYSTEM = (
    "You are an expert coding assistant operating inside a coding agent harness. "
    "You help users by reading files, executing commands, editing code, and "
    "writing new files.\n\nAvailable tools:\n- read: Read file contents\n- bash: "
    "Execute bash commands\n- edit: Make precise edits to files\n- write: Create "
    "or overwrite files\n\nGuidelines:\n- Use bash for file operations like ls, "
    "rg, find\n- Be concise in your responses\n- Show file paths clearly when "
    "working with files\n" + "Harness documentation paths and topics. " * 40
)


def simulated_history(family, profile):
    """One plausible capture with fixed assistant text; real captures differ."""
    module, check = MODULES[family]
    base = FAMILIES / family / "workspace"
    prompts, _ = model_inputs(family, profile)
    output = execute(base / check, base, isolated=False).stdout
    messages = [
        ("system", SYSTEM),
        ("user", prompts[0]),
        ("assistant", f"I'll read {module} to understand the implementation."),
        ("tool", (base / module).read_text()),
        ("assistant", f"Next I'll run the visible checks with python3 {check}."),
        ("tool", output),
    ]
    if profile == "correction":
        messages += [("tool", "ARCHIVED ... only\nRuntimeError: " + "diagnostic")]
    messages.append(("assistant", "The checks fail as expected. Ready to continue."))
    for prompt in prompts[1:]:
        messages += [("user", prompt), ("assistant", "Recorded.")]
    return messages


@pytest.mark.parametrize("family", sorted(LABELS["families"]))
@pytest.mark.parametrize("profile", ("missing", "correction"))
def test_design_places_the_necessary_note_in_the_candidate_window(family, profile):
    """Fixture design, not a held-out claim: JEV's decision must matter."""
    sel = load_selector()
    _, continuation = model_inputs(family, profile)
    catalog = sel.synthetic_catalog(simulated_history(family, profile))
    skipped = sel._metrics("J1")["skipped"]
    ranked = sel.base._keyword(catalog, continuation.strip(), dict(skipped))
    anchors, candidates = sel._initial(catalog, ranked, dict(skipped))
    keyword = sel.base._pack(catalog, ranked, dict(skipped))
    rule = LABELS["families"][family]["rule"]

    def carries(item):
        return rule in item["part"]["content"]

    assert not any(carries(a["evidence"]) for a in anchors)
    assert any(carries(c["evidence"]) for c in candidates)
    assert any(carries(item) for item in keyword)
