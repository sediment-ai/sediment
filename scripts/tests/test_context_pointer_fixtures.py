# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase 3 fixtures: validators, visible checks, and where labels appear."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).parent / "fixtures/context_pointer"
FAMILIES = ROOT / "families"
LABELS = json.loads((ROOT / "evaluation/labels.json").read_bytes())
PROFILES = ("missing", "redundant", "correction")

REFERENCE = {
    "invoice-subtotal": """def subtotal(lines):
    return sum(
        line["unit_cents"] * line["quantity"]
        for line in lines
        if not line["sku"].startswith("SAMPLE-")
    )
""",
    "username-check": """import re

def valid(name):
    if name.startswith(("admin", "root")):
        return False
    return re.fullmatch(r"[a-z][a-z0-9_]{2,15}", name) is not None
""",
    "threshold-alerts": """def alerts(readings, limit):
    return [i for i, r in enumerate(readings) if r >= limit]
""",
}
# (old, new, behavior_pass, constraint_pass, visible_checks_pass)
MUTATIONS = {
    "invoice-subtotal": {
        "no_rule": (
            '        if not line["sku"].startswith("SAMPLE-")\n',
            "",
            True,
            False,
            True,
        ),
        "obsolete_rule": (
            'line["unit_cents"] * line["quantity"]\n        for line in lines\n'
            '        if not line["sku"].startswith("SAMPLE-")\n',
            '(1 if line["sku"].startswith("SAMPLE-") else line["unit_cents"])'
            ' * line["quantity"]\n        for line in lines\n',
            True,
            False,
            True,
        ),
        "ordinary_behavior": (' * line["quantity"]', "", False, False, False),
    },
    "username-check": {
        "no_rule": (
            '    if name.startswith(("admin", "root")):\n        return False\n',
            "",
            True,
            False,
            True,
        ),
        "obsolete_rule": (
            'name.startswith(("admin", "root"))',
            'name in ("admin", "root")',
            True,
            False,
            True,
        ),
        "ordinary_behavior": (
            "[a-z][a-z0-9_]{2,15}",
            "[a-zA-Z0-9_]{3,16}",
            False,
            True,
            False,
        ),
    },
    "threshold-alerts": {
        "no_rule": ("r >= limit", "r > limit", True, False, True),
        "obsolete_rule": ("r >= limit", "r > limit - 1", True, False, True),
        "ordinary_behavior": (
            "enumerate(readings)",
            "enumerate(readings[1:])",
            False,
            False,
            False,
        ),
    },
}


def workspace(tmp_path, family, code):
    directory = tmp_path / family
    shutil.copytree(FAMILIES / family / "workspace", directory)
    module = LABELS["families"][family]["module"]
    (directory / module).write_text(code)
    return directory


def verify(family, directory):
    output = subprocess.run(
        [sys.executable, "-I", "-B", ROOT / "evaluation/verify.py", family, directory],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout
    return json.loads(output)


def visible(family, directory):
    module = LABELS["families"][family]["module"]
    return (
        subprocess.run(
            [sys.executable, "-B", f"check_{module}"],
            cwd=directory,
            capture_output=True,
            timeout=30,
        ).returncode
        == 0
    )


@pytest.mark.parametrize("family", sorted(REFERENCE))
def test_reference_passes_and_initial_program_fails(tmp_path, family):
    reference = workspace(tmp_path / "ref", family, REFERENCE[family])
    assert verify(family, reference) == {"behavior_pass": True, "constraint_pass": True}
    assert visible(family, reference)
    initial = FAMILIES / family / "workspace"
    assert verify(family, initial) == {"behavior_pass": False, "constraint_pass": False}
    assert not visible(family, initial)


@pytest.mark.parametrize(
    "family,mutation", [(f, m) for f in MUTATIONS for m in MUTATIONS[f]]
)
def test_mutations_fail_their_intended_checks(tmp_path, family, mutation):
    old, new, behavior, constraint, public = MUTATIONS[family][mutation]
    assert REFERENCE[family].count(old) == 1
    directory = workspace(tmp_path, family, REFERENCE[family].replace(old, new))
    assert verify(family, directory) == {
        "behavior_pass": behavior,
        "constraint_pass": constraint,
    }
    assert visible(family, directory) is public


@pytest.mark.parametrize("family", sorted(REFERENCE))
@pytest.mark.parametrize("profile", PROFILES)
def test_labels_appear_only_where_designed(family, profile):
    terms = LABELS["families"][family]
    source = "\n".join(
        json.loads((FAMILIES / family / profile / "source.json").read_bytes())[
            "prompts"
        ]
    )
    visible_task = (FAMILIES / family / profile / "continuation.txt").read_text()
    for name in LABELS["profiles"][profile]["source_requires"]:
        # The distractor term appears in the diagnostic command's text.
        assert terms[name] in source
    assert (terms["obsolete"] in source) is (profile == "correction")
    assert (terms["rule"] in visible_task) is (profile == "redundant")
    assert terms["obsolete"] not in visible_task
    for path in (FAMILIES / family / "workspace").rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text()
        assert terms["rule"] not in text and terms["obsolete"] not in text
