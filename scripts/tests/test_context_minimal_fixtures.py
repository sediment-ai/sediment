# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase 2 fixtures: validators, visible checks, and where labels appear."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).parent / "fixtures/context_minimal"
FAMILIES = ROOT / "families"
LABELS = json.loads((ROOT / "evaluation/labels.json").read_bytes())
PROFILES = ("missing", "redundant", "correction")

REFERENCE = {
    "ledger-balance": """def balances(rows):
    result = {}
    for row in rows:
        if row["status"] == "pending":
            continue
        account = row["account"].strip()
        result[account] = result.get(account, 0) + row["amount_cents"]
    return result
""",
    "tag-normalize": """def _norm(tag):
    return "-".join(tag.split()).lower()

def normalize(tags):
    removed = {_norm(t.strip()[1:]) for t in tags if t.strip().startswith("~")}
    result = []
    for tag in tags:
        if tag.strip().startswith("~"):
            continue
        value = _norm(tag)
        if value and value not in removed and value not in result:
            result.append(value)
    return result
""",
    "sensor-window": """def moving_average(readings, size):
    values = [r for r in readings if r is not None]
    return [
        round(sum(values[i:i + size]) / size, 2)
        for i in range(len(values) - size + 1)
    ]
""",
}
# (old, new, behavior_pass, constraint_pass, visible_checks_pass)
MUTATIONS = {
    "ledger-balance": {
        "no_rule": (
            '        if row["status"] == "pending":\n            continue\n',
            "",
            True,
            False,
            True,
        ),
        "obsolete_rule": (
            '        if row["status"] == "pending":\n            continue\n',
            '        if row["status"] == "pending":\n'
            '            row = {**row, "amount_cents": int(row["amount_cents"] / 2)}\n',
            True,
            False,
            True,
        ),
        "ordinary_behavior": (
            'row["account"].strip()',
            'row["account"]',
            False,
            True,
            False,
        ),
    },
    "tag-normalize": {
        "no_rule": ("value not in removed and ", "", True, False, True),
        "obsolete_rule": (
            '        if tag.strip().startswith("~"):\n            continue\n'
            "        value = _norm(tag)\n"
            "        if value and value not in removed and ",
            "        value = _norm(tag)\n        if value and ",
            True,
            False,
            True,
        ),
        "ordinary_behavior": (
            '"-".join(tag.split())',
            "tag.strip()",
            False,
            False,
            False,
        ),
    },
    "sensor-window": {
        "no_rule": (
            "[r for r in readings if r is not None]",
            "list(readings)",
            True,
            False,
            True,
        ),
        "obsolete_rule": (
            "[r for r in readings if r is not None]",
            "[0.0 if r is None else r for r in readings]",
            True,
            False,
            True,
        ),
        "ordinary_behavior": (
            "round(sum(values[i:i + size]) / size, 2)",
            "sum(values[i:i + size]) / size",
            False,
            True,
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
    for path in (FAMILIES / family / "workspace").iterdir():
        text = path.read_text()
        assert terms["rule"] not in text and terms["obsolete"] not in text
