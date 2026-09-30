# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for scripts/erode_replay.py on a synthetic Anthropic Messages run.

``scripts/`` is not a package, so the module is loaded by path.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "erode_replay.py"
MARKER = "SYNTHETIC-CONTENT-MARKER"


def _load_module():
    spec = importlib.util.spec_from_file_location("erode_replay", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


replay = _load_module()

MAIN_TOOLS = [
    {"name": n, "input_schema": {}} for n in ("Task", "Read", "Bash", "Edit", "Grep")
]
SUB_TOOLS = [{"name": n, "input_schema": {}} for n in ("Read", "Bash")]


def _text(n: int) -> str:
    return (MARKER + " ") * (n // (len(MARKER) + 1))


def _step(name, args, result, error=False):
    return dict(name=name, input=args, result=result, error=error)


# One tool call per assistant turn. Expected fates at the final request are
# worked out from erode's rules (512-byte minimum, 4 KB batches, 2 protected
# turns) in the comments.
MAIN_STEPS = (
    [
        _step("Read", {"file_path": "a.py"}, _text(3000)),  # 1 stubbed
        _step("Bash", {"command": "pytest -q"}, _text(2000)),  # 2 stubbed
        _step("Edit", {"file_path": "a.py"}, "ok"),  # 3 edit_or_write
        _step("Read", {"file_path": "a.py"}, _text(3000)),  # 4 held back
        _step(
            "Bash", {"command": "pytest -q"}, [{"type": "text", "text": _text(2000)}]
        ),
        _step("Read", {"file_path": "b.py"}, _text(1000)),  # 6 never (edit errored)
        _step("Grep", {"pattern": "x"}, _text(1500)),  # 7 unrecognized
        _step("Edit", {"file_path": "b.py"}, "no match", error=True),  # 8
    ]
    + [_step("Bash", {"command": f"ls {i}"}, "x") for i in range(9, 12)]
    + [_step("Read", {"file_path": "a.py"}, _text(3000))]  # 12 re-read of a stub
    + [_step("Bash", {"command": f"ls {i}"}, "x") for i in range(13, 23)]
    + [
        _step("Read", {"file_path": "c.py"}, _text(800)),  # 23 protected turns
        _step("Read", {"file_path": "c.py"}, _text(800)),  # 24 never superseded
    ]
)


def _messages(task: str, steps: list[dict]) -> list[dict]:
    messages = [{"role": "user", "content": task}]
    for n, step in enumerate(steps, 1):
        messages.append(
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": f"t{n}",
                        "name": step["name"],
                        "input": step["input"],
                    }
                ],
            }
        )
        block = {
            "type": "tool_result",
            "tool_use_id": f"t{n}",
            "content": step["result"],
        }
        if step["error"]:
            block["is_error"] = True
        messages.append({"role": "user", "content": [block]})
    return messages


def _requests(system, tools, task, steps):
    return [
        {
            "model": "m",
            "system": system,
            "tools": tools,
            "messages": _messages(task, steps[:k]),
        }
        for k in range(len(steps) + 1)
    ]


def _write_run(root: Path) -> None:
    run = root / "instance-a"
    run.mkdir(parents=True)
    main = _requests("main agent", MAIN_TOOLS, "fix the bug", MAIN_STEPS)
    compacted_steps = [_step("Bash", {"command": f"ls z{i}"}, "x") for i in range(2)]
    compacted = _requests(
        "main agent", MAIN_TOOLS, "summary of earlier work", compacted_steps
    )
    sub = _requests(
        "subagent",
        SUB_TOOLS,
        "explore",
        [_step("Read", {"file_path": "a.py"}, "x")] * 2,
    )
    side = [
        {
            "model": "small",
            "system": "title",
            "messages": [{"role": "user", "content": "t"}],
        }
    ]
    # Interleave the subagent and a side request with the main conversation,
    # as one proxy sees them.
    bodies = main[:5] + sub[:2] + side + main[5:] + sub[2:] + compacted
    for n, body in enumerate(bodies, 1):
        (run / f"{n:04d}.json").write_text(json.dumps(body))
    (run / f"{len(bodies) + 1:04d}.json").write_text(json.dumps({"input": []}))
    (run / f"{len(bodies) + 2:04d}.json").write_text("not json")


def test_replay_groups_sessions_and_reports_counts(tmp_path):
    _write_run(tmp_path / "rec")
    result = replay.report(tmp_path / "rec")

    assert result["requests_not_replayed"] == {"not_messages": 1, "unreadable": 1}
    main = [s for s in result["sessions"] if s["role"] == "main"]
    others = [s for s in result["sessions"] if s["role"] == "other"]
    assert len(main) == 1 and len(others) == 2
    session = main[0]
    assert session["requests"] == 25 + 3
    assert session["long"] and not session["main_by_request_count"]
    assert (
        session["segments"],
        session["compactions"],
        session["raw_prefix_breaks"],
    ) == (2, 1, 1)
    assert not any(s["long"] for s in others)

    checks = session["checks"]
    assert checks["requests"] == 28
    assert checks["deterministic"] == checks["pairing_preserved"] == 28
    assert checks["stub_regressions"] == 0
    assert checks["pruned_prefix_breaks_without_new_stub"] == 0

    funnel = session["drop_outs"]
    assert funnel["results"] == {
        "context_management": 0,
        "unrecognized_tool": 1,
        "edit_or_write": 2,
        "not_prunable_shape": 0,
        "error_result": 0,
        "under_min_bytes": 13 + 2,
        "never_superseded": 4,
        "protected_turns": 1,
        "held_back_by_threshold": 1,
        "stubbed": 2,
    }
    assert funnel["stubs_from_errored_superseder"] == 0
    assert funnel["superseders_skipped_as_errored"] == 1

    # Steps 1 and 2 are stubbed together once 5 KB is pending, at the request
    # that holds five assistant turns.
    assert [
        (b["segment"], b["request"], b["new_stubs"]) for b in session["cache_breaks"]
    ] == [(1, 6, 2)]
    assert session["stubbed_targets_read_again"] == 1

    removed = sum(r["bytes_removed"] for r in session["per_request"])
    sent = sum(r["bytes_before"] for r in session["per_request"])
    assert session["ratio"] == removed / sent > 0
    assert all(v >= session["ratio"] for v in session["sensitivity"].values())
    assert set(session["tool_result_bytes_by_tool"]) == {"Read", "Bash", "Edit", "Grep"}
    assert abs(sum(session["share_by_part"].values()) - 1) < 1e-9

    gate = result["gate"]
    assert gate["long_sessions"] == 1 and gate["long_session_ratios"] == [
        session["ratio"]
    ]
    assert gate["passed"] is None
    assert MARKER not in json.dumps(result)
    assert "a.py" not in json.dumps(result) and "pytest" not in json.dumps(result)


def test_main_agent_falls_back_to_most_requests(tmp_path):
    run = tmp_path / "rec" / "r"
    run.mkdir(parents=True)
    bodies = _requests("a", SUB_TOOLS, "t", [_step("Bash", {"command": "ls"}, "x")] * 2)
    bodies.append(
        {"model": "m", "system": "b", "messages": [{"role": "user", "content": "t"}]}
    )
    for n, body in enumerate(bodies, 1):
        (run / f"{n:04d}.json").write_text(json.dumps(body))
    sessions = replay.report(tmp_path / "rec")["sessions"]
    main = [s for s in sessions if s["role"] == "main"]
    assert (
        len(main) == 1 and main[0]["requests"] == 3 and main[0]["main_by_request_count"]
    )
    assert replay.report(tmp_path / "rec")["gate"]["passed"] is None


def test_interleaved_threads_of_one_agent_stay_separate():
    a = _requests("s", SUB_TOOLS, "one", [_step("Bash", {"command": "ls"}, "x")] * 2)
    b = _requests("s", SUB_TOOLS, "two", [_step("Bash", {"command": "ls"}, "y")] * 2)
    split = replay.threads([a[0], b[0], a[1], b[1], a[2], b[2]])
    assert [len(t) for t in split] == [3, 3]
    assert all(t[0]["messages"][0]["content"] in ("one", "two") for t in split)


def test_cli_writes_counts_only(tmp_path):
    _write_run(tmp_path / "rec")
    out = tmp_path / "report.json"
    assert replay.main([str(tmp_path / "rec"), "--output", str(out)]) == 0
    text = out.read_text()
    assert json.loads(text)["gate"]["long_sessions"] == 1
    assert MARKER not in text


def test_moving_cache_control_marker_keeps_one_thread():
    bodies = _requests("s", SUB_TOOLS, "t", [_step("Bash", {"command": "ls"}, "x")] * 3)
    for body in bodies:
        body["messages"][0]["content"] = [{"type": "text", "text": "t"}]
        # Claude Code marks only the newest message block for caching.
        body["messages"][-1]["content"][-1]["cache_control"] = {"type": "ephemeral"}
    assert [len(t) for t in replay.threads(bodies)] == [4]
    # Without the marker handling, every request would start a new thread.
    assert bodies[0]["messages"] != bodies[1]["messages"][:1]


@pytest.mark.parametrize("run_count", [0, 1, 2, 3])
@pytest.mark.parametrize("result_bytes", [10, 20_000])
def test_gate_requires_three_long_sessions(tmp_path, run_count, result_bytes):
    bodies = _requests(
        "main",
        MAIN_TOOLS,
        "task",
        [_step("Read", {"file_path": "a.py"}, "x" * result_bytes)] * 19,
    )
    for run in range(run_count):
        directory = tmp_path / str(run)
        directory.mkdir()
        for n, body in enumerate(bodies):
            (directory / f"{n:04d}.json").write_text(json.dumps(body))
    gate = replay.report(tmp_path)["gate"]
    assert gate["long_sessions"] == run_count
    assert len(gate["long_session_ratios"]) == run_count
    if run_count < 3:
        assert gate["passed"] is None
    else:
        assert gate["passed"] is (result_bytes == 20_000)


def test_openai_chat_is_counted_as_skipped(tmp_path):
    messages = [{"role": "user", "content": "task"}]
    for n in range(21):
        messages += [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": str(n),
                        "type": "function",
                        "function": {"name": "read", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": str(n), "content": "x" * 1000},
        ]
        body = {"model": "m", "messages": messages}
        (tmp_path / f"{n:04d}.json").write_text(json.dumps(body))
    result = replay.report(tmp_path)
    assert result["requests_not_replayed"] == {"openai_chat": 21}
    assert result["sessions"] == []
    assert result["gate"]["passed"] is None


def test_provider_managed_request_keeps_cost_and_reports_bypass(tmp_path):
    bodies = _requests(
        "main",
        MAIN_TOOLS,
        "task",
        [_step("Read", {"file_path": "a.py"}, "x" * 20_000)] * 20,
    )
    bodies[-1]["context_management"] = {"edits": []}
    for n, body in enumerate(bodies):
        (tmp_path / f"{n:04d}.json").write_text(json.dumps(body))
    session = replay.report(tmp_path)["sessions"][0]
    assert session["prune_skips"] == {"context_management": 1}
    assert session["requests"] == len(bodies)
    assert session["input_bytes"] == sum(replay.size(body) for body in bodies)
    assert session["per_request"][-1]["bytes_removed"] == 0
    assert session["bytes_removed"] > 0
    assert session["drop_outs"]["results"]["context_management"] == 20
    assert session["drop_outs"]["results"]["held_back_by_threshold"] == 0
