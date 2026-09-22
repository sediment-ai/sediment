# SPDX-License-Identifier: AGPL-3.0-or-later
"""Instructed retrieval ordering is an additional, task-specific acceptance gate."""

from copy import deepcopy
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
    spec = importlib.util.spec_from_file_location("required_context_lookup", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def lookup_events():
    selection = {
        "schema_version": 1,
        "policy_version": 1,
        "source_session_id": "actual-source",
        "quarantine_revision": 0,
        "status": "matched",
        "capture_completeness": "unknown",
        "coverage": {
            "visible_inference_calls": 1,
            "quarantined_inference_calls": 0,
            "scanned_parts": 1,
            "complete_visible_scan": True,
        },
        "skipped": {
            key: 0
            for key in (
                "reasoning_part",
                "non_finite_number",
                "no_match",
                "repeated_content",
                "item_limit",
                "response_budget",
            )
        },
        "items": [
            {
                "score": 1,
                "evidence": {
                    "reference": {
                        "inference_call_id": "actual-fact",
                        "side": "input",
                        "message_index": 0,
                        "part_index": 0,
                    },
                    "observed_at": "2026-09-22T12:00:00Z",
                    "role": "user",
                    "finish_reason": None,
                    "part": {"type": "text", "content": "historical evidence"},
                },
            }
        ],
    }
    return [
        {
            "type": "tool_execution_start",
            "toolName": "sediment_retrieve_context",
            "toolCallId": "lookup",
            "args": {"query": "prior constraints and failure"},
        },
        {
            "type": "tool_execution_end",
            "toolCallId": "lookup",
            "isError": False,
            "result": {"content": [{"type": "text", "text": json.dumps(selection)}]},
        },
    ]


def work(name="edit"):
    return {"type": "tool_execution_start", "toolName": name, "toolCallId": "work"}


@pytest.mark.parametrize("name", ["edit", "write", "bash"])
def test_successful_lookup_must_precede_first_work_start_even_if_work_fails(name):
    mod = load()
    lookup = lookup_events()
    assert mod.lookup_before_work([work("read"), *lookup, work(name)], "actual-source")
    assert not mod.lookup_before_work(
        [work(name), *lookup, work(name)], "actual-source"
    )
    assert not mod.lookup_before_work(
        [
            work(name),
            {"type": "tool_execution_end", "toolCallId": "work", "isError": True},
            *lookup,
            work(name),
        ],
        "actual-source",
    )


@pytest.mark.parametrize(
    "defect",
    [
        "absent",
        "error",
        "empty",
        "wrong_source",
        "malformed",
        "unmatched_end",
        "unfinished",
        "not_matched",
        "missing_reference",
        "missing_error_flag",
    ],
)
def test_unsuccessful_or_unverified_lookup_does_not_satisfy_ordering(defect):
    mod = load()
    lookup = lookup_events()
    selection = json.loads(lookup[-1]["result"]["content"][0]["text"])
    if defect == "absent":
        lookup = []
    elif defect == "error":
        lookup[-1]["isError"] = True
    elif defect == "unmatched_end":
        lookup[-1]["toolCallId"] = "unmatched"
    elif defect == "unfinished":
        lookup = lookup[:1]
    elif defect == "missing_error_flag":
        lookup[-1].pop("isError")
    else:
        if defect == "empty":
            selection["items"] = []
        elif defect == "wrong_source":
            selection["source_session_id"] = "other-source"
        elif defect == "not_matched":
            selection["status"] = "no_match"
        elif defect == "missing_reference":
            del selection["items"][0]["evidence"]["reference"]
        lookup[-1]["result"]["content"][0]["text"] = (
            "unparseable" if defect == "malformed" else json.dumps(selection)
        )
    assert not mod.lookup_before_work([*lookup, work()], "actual-source")


def test_lookup_must_finish_before_work_and_without_work_does_not_prove_ordering():
    mod = load()
    start, end = lookup_events()
    assert not mod.lookup_before_work([start, work(), end], "actual-source")
    assert not mod.lookup_before_work([start, end], "actual-source")
    failed = deepcopy(end)
    failed["isError"] = True
    assert mod.lookup_before_work([start, failed, start, end, work()], "actual-source")


def records():
    return [
        {
            "arm": arm,
            "repetition": repetition,
            "session_id": f"{arm}-{repetition}",
            "status": "settled",
            "behavior_pass": True,
            "constraint_pass": arm != "A",
            "verification_error": None,
            "retrieval_calls": int(arm == "C"),
            "retrieved_constraint": arm == "C",
            "retrieved_failure": arm == "C",
            "trajectory_verified": arm == "C",
            "lookup_before_work": arm == "C",
        }
        for repetition in range(1, 4)
        for arm in "ABC"
    ]


def test_shipment_acceptance_adds_ordering_without_relaxing_existing_criteria():
    mod = load()
    runs = records()
    assert mod.evaluate(runs, "shipment-totals")["benefit_demonstrated"]
    for key in (
        "lookup_before_work",
        "retrieved_constraint",
        "retrieved_failure",
        "trajectory_verified",
        "constraint_pass",
        "behavior_pass",
    ):
        changed = deepcopy(runs)
        changed[-1][key] = False
        assert not mod.evaluate(changed, "shipment-totals")["benefit_demonstrated"]
    runs[-1].pop("lookup_before_work")
    assert not mod.evaluate(runs, "shipment-totals")["benefit_demonstrated"]
    assert mod.evaluate(runs, "invoice")["benefit_demonstrated"]
    assert mod.evaluate(runs, "env-profile")["benefit_demonstrated"]


def execute(script, workspace):
    return subprocess.run(
        [sys.executable, "-B", str(script)]
        + ([str(workspace)] if script.name == "verify.py" else []),
        cwd=workspace,
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        timeout=10,
    )


@pytest.mark.parametrize(
    "rule,passes",
    [("none", False), ("casefold", False), ("untrimmed", False), ("correct", True)],
)
def test_private_shipment_validator_checks_rule_beyond_public_goal(
    tmp_path, rule, passes
):
    fixture = load().task_fixture("shipment-totals")
    workspace = tmp_path / "workspace"
    shutil.copytree(fixture / "workspace", workspace)
    exclusion = {
        "none": "False",
        "casefold": "channel.strip().lower() == 'replay'",
        "untrimmed": "channel == 'replay'",
        "correct": "channel.strip() == 'replay'",
    }[rule]
    (workspace / "shipment_totals.py").write_text(
        "import csv, io\n"
        "def totals_by_destination(text):\n"
        "    totals = {}\n"
        "    for row in csv.reader(io.StringIO(text)):\n"
        "        if not row or not any(value.strip() for value in row):\n"
        "            continue\n"
        "        destination, units, channel = row\n"
        f"        if {exclusion}:\n"
        "            continue\n"
        "        destination = destination.strip()\n"
        "        totals[destination] = totals.get(destination, 0) + int(units)\n"
        "    return totals\n"
    )
    public = execute(workspace / "check_shipment_totals.py", workspace)
    assert public.returncode == 0, public.stderr
    result = execute(fixture / "verify.py", workspace)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "behavior_pass": True,
        "constraint_pass": passes,
    }
    (workspace / "check_shipment_totals.py").write_text("raise SystemExit(0)\n")
    assert (
        json.loads(execute(fixture / "verify.py", workspace).stdout)["constraint_pass"]
        is passes
    )


def test_shipment_source_fails_for_quoted_csv_and_keeps_rule_out_of_public_files(
    tmp_path,
):
    mod = load()
    fixture = mod.task_fixture("shipment-totals")
    workspace = tmp_path / "workspace"
    shutil.copytree(fixture / "workspace", workspace)
    result = execute(workspace / "check_shipment_totals.py", workspace)
    assert result.returncode != 0
    assert "ValueError: too many values to unpack" in result.stderr
    for path in [fixture / "continuation.txt", *(fixture / "workspace").glob("*.py")]:
        assert "replay" not in path.read_text().lower()
    source = (fixture / "source.txt").read_text()
    assert "'replay'" in source and "must NOT contribute" in source
    gold = mod.source_gold(
        [
            {"part": {"type": "text", "content": source}},
            {
                "part": {
                    "type": "tool_call_response",
                    "id": "checker",
                    "result": result.stderr,
                }
            },
            {
                "part": {
                    "type": "tool_call_response",
                    "id": "distractor",
                    "result": "RuntimeError: optional_manifest_linter unavailable",
                }
            },
        ],
        "shipment-totals",
    )
    assert all(len(items) == 1 for items in gold.values())


def test_shipment_freeze_rejects_other_task_source(tmp_path, monkeypatch):
    mod = load()
    monkeypatch.setattr(
        mod, "command", lambda *a, **kw: b"0.84.1\nv24.14.0\nPython 3.12.14\n"
    )
    config = {"agent_image": "agent", "gate_image": "gate"}
    frozen = mod.freeze(config, "shipment-totals")
    assert frozen["task"] == "shipment-totals"
    assert frozen["fixture_hashes"]["workspace/shipment_totals.py"]
    source = mod.private_directory(tmp_path / "source")
    mod.write_json(source / "freeze.json", mod.freeze(config, "env-profile"))
    with pytest.raises(mod.EvaluationError, match="evaluation_changed_after_source"):
        mod.run_comparison(config, source, tmp_path / "unused", "shipment-totals")


def test_original_profile_fixture_bytes_remain_unchanged():
    mod = load()
    fixture = mod.task_fixture("env-profile")
    assert {
        p.relative_to(fixture).as_posix(): mod.digest(p.read_bytes())
        for p in sorted(fixture.rglob("*"))
        if p.is_file()
    } == {
        "continuation.txt": "ecf5bc4a4cfe3f988241e5d1c7eec164803381e4363640cafffd01e9ac1a1540",
        "source.txt": "2b63c9b9307aabeac55579193b10947a654580d6b786900ed9888d71619633df",
        "verify.py": "daf220c37e5860b49ffd89523fb4b12ca26e77a766631beebff344898da721ab",
        "workspace/check_env_profile.py": "1b16614aa2c34b33b5d948319b7c4a17617568191b8fd3fce226a61fa59400a2",
        "workspace/env_profile.py": "cc1458e73036c59dc998a5294c5cba3cce3f7b6c73ee77dd2a5775242bbd4b0f",
    }
