# SPDX-License-Identifier: AGPL-3.0-or-later
"""The supersession core: rules, pairing, the 4 KB threshold, and purity."""

from __future__ import annotations

import copy
import json
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sediment_context import STUB_PREFIX, PrunePolicy, prune  # noqa: E402

POLICY = PrunePolicy()
BIG = "x" * 5000  # alone crosses the 4 KB threshold
MID = "y" * 3000  # needs a second superseded result to cross it
SMALL = "z" * 400  # under the 512-byte floor


def openai(turns: list[list[tuple]]) -> list[dict]:
    """OpenAI chat (pi): one assistant message per turn, then role tool results."""
    messages: list[dict] = [
        {"role": "system", "content": "You are a coding agent."},
        {"role": "user", "content": "Fix the failing test in src/app.py."},
    ]
    step = 0
    for turn in turns:
        tool_calls, results = [], []
        for name, arguments, result in turn:
            step += 1
            tool_calls.append(
                {
                    "id": f"call_{step}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            )
            results.append(
                {"role": "tool", "tool_call_id": f"call_{step}", "content": result}
            )
        messages.append(
            {"role": "assistant", "content": f"Step {step}.", "tool_calls": tool_calls}
        )
        messages += results
    return messages


def anthropic(turns: list[list[tuple]]) -> list[dict]:
    """Anthropic Messages (Claude Code): tool_use blocks, then tool_result blocks.

    A result given as ``(text, True)`` is an error result.
    """
    messages: list[dict] = [
        {"role": "user", "content": [{"type": "text", "text": "Fix src/app.py."}]}
    ]
    step = 0
    for turn in turns:
        uses, results = [{"type": "text", "text": f"Turn after {step}."}], []
        for name, arguments, result in turn:
            step += 1
            text, error = result if isinstance(result, tuple) else (result, False)
            uses.append(
                {
                    "type": "tool_use",
                    "id": f"toolu_{step}",
                    "name": name,
                    "input": arguments,
                }
            )
            block = {"type": "tool_result", "tool_use_id": f"toolu_{step}"}
            block["content"] = text
            if error:
                block["is_error"] = True
            results.append(block)
        messages.append({"role": "assistant", "content": uses})
        messages.append({"role": "user", "content": results})
    return messages


def filler(n: int, fmt: str = "openai") -> list[list[tuple]]:
    name = "bash" if fmt == "openai" else "Bash"
    return [[(name, {"command": f"echo {i}"}, "ok")] for i in range(n)]


def stubbed(messages: list[dict]) -> list[str]:
    """Tool-call ids whose results are stubs, in either format."""
    ids = []
    for message in messages:
        if message.get("role") == "tool" and str(message["content"]).startswith(
            STUB_PREFIX
        ):
            ids.append(message["tool_call_id"])
        if message.get("role") == "user" and isinstance(message["content"], list):
            for block in message["content"]:
                content = block.get("content")
                if block.get("type") == "tool_result" and str(content).startswith(
                    STUB_PREFIX
                ):
                    ids.append(block["tool_use_id"])
    return ids


def test_read_superseded_by_later_edit_openai() -> None:
    messages = openai(
        [
            [("read", {"path": "src/app.py"}, BIG)],
            [("edit", {"path": "src/app.py", "oldText": "a", "newText": "b"}, "ok")],
            *filler(2),
        ]
    )
    pruned, report = prune(messages, POLICY)
    assert stubbed(pruned) == ["call_1"]
    assert pruned[3]["content"] == (
        "[sediment: superseded by step 2 (edit of src/app.py); "
        "read it again if you need the current content]"
    )
    assert report == {
        "policy_version": "1",
        "stubbed_results": 1,
        "bytes_removed": len(BIG) - len(pruned[3]["content"]),
    }


def test_read_superseded_by_later_read_and_write_anthropic() -> None:
    messages = anthropic(
        [
            [("Read", {"file_path": "/r/a.py"}, BIG)],
            [("Read", {"file_path": "/r/a.py"}, BIG)],
            [("Write", {"file_path": "/r/a.py", "content": "new"}, "ok")],
            *filler(2, "anthropic"),
        ]
    )
    pruned, report = prune(messages, POLICY)
    assert stubbed(pruned) == ["toolu_1", "toolu_2"]
    assert "(read of /r/a.py)" in pruned[2]["content"][0]["content"]
    assert "(write of /r/a.py)" in pruned[4]["content"][0]["content"]
    assert report["stubbed_results"] == 2


def test_run_superseded_only_by_identical_command() -> None:
    messages = openai(
        [
            [("bash", {"command": "pytest -q"}, BIG)],
            [("bash", {"command": "pytest -q tests/a.py"}, BIG)],
            [("bash", {"command": "pytest -q"}, BIG)],
            *filler(2),
        ]
    )
    pruned, _ = prune(messages, POLICY)
    assert stubbed(pruned) == ["call_1"]
    assert "(bash of pytest -q); run it again" in pruned[3]["content"]


def test_edit_does_not_supersede_a_command() -> None:
    messages = anthropic(
        [
            [("Bash", {"command": "cat src/app.py"}, BIG)],
            [("Edit", {"file_path": "src/app.py", "old_string": "a"}, "ok")],
            *filler(2, "anthropic"),
        ]
    )
    assert stubbed(prune(messages, POLICY)[0]) == []


def test_partial_read_supersedes_only_the_same_range() -> None:
    partial = {"path": "a.py", "offset": 10, "limit": 5}
    kept = openai(
        [
            [("read", {"path": "a.py"}, BIG)],
            [("read", partial, SMALL)],
            *filler(2),
        ]
    )
    assert stubbed(prune(kept, POLICY)[0]) == []
    superseded = openai(
        [
            [("read", partial, BIG)],
            [("read", partial, SMALL)],
            [("read", {"path": "a.py"}, SMALL)],
            *filler(2),
        ]
    )
    assert stubbed(prune(superseded, POLICY)[0]) == ["call_1"]


def test_last_two_turns_are_protected() -> None:
    turns = [
        *filler(1),
        [("read", {"path": "a.py"}, BIG)],
        [("read", {"path": "a.py"}, BIG)],
    ]
    # call_2 is in the second-to-last turn.
    assert stubbed(prune(openai(turns), POLICY)[0]) == []
    assert stubbed(prune(openai([*turns, *filler(1)]), POLICY)[0]) == ["call_2"]


def test_small_and_error_results_are_never_stubbed() -> None:
    messages = anthropic(
        [
            [("Read", {"file_path": "a.py"}, SMALL)],
            [("Bash", {"command": "make"}, (BIG, True))],
            [("Read", {"file_path": "a.py"}, SMALL)],
            [("Bash", {"command": "make"}, BIG)],
            *filler(2, "anthropic"),
        ]
    )
    assert stubbed(prune(messages, POLICY)[0]) == []


def test_errored_edit_does_not_supersede() -> None:
    messages = anthropic(
        [
            [("Read", {"file_path": "a.py"}, BIG)],
            [("Edit", {"file_path": "a.py"}, ("String not found", True))],
            *filler(2, "anthropic"),
        ]
    )
    assert stubbed(prune(messages, POLICY)[0]) == []


def test_unknown_tools_pass_through() -> None:
    oa = openai(
        [
            [("grep", {"path": "src", "pattern": "x"}, BIG)],
            [("grep", {"path": "src", "pattern": "x"}, BIG)],
            [("Read", {"file_path": "a.py"}, BIG)],
            [("Read", {"file_path": "a.py"}, BIG)],
            *filler(2),
        ]
    )
    assert prune(oa, POLICY)[0] == oa
    an = anthropic(
        [
            [("Glob", {"pattern": "**/*.py"}, BIG)],
            [("Glob", {"pattern": "**/*.py"}, BIG)],
            [("read", {"path": "a.py"}, BIG)],
            [("read", {"path": "a.py"}, BIG)],
            [("NotebookEdit", {"notebook_path": "n.ipynb"}, BIG)],
            [("NotebookEdit", {"notebook_path": "n.ipynb"}, BIG)],
            *filler(2, "anthropic"),
        ]
    )
    assert prune(an, POLICY)[0] == an


def test_threshold_batches_new_stubs() -> None:
    one = [
        [("read", {"path": "a.py"}, MID)],
        [("read", {"path": "a.py"}, SMALL)],
        *filler(2),
    ]
    # 3000 superseded bytes stay below the 4 KB threshold.
    assert stubbed(prune(openai(one), POLICY)[0]) == []
    two = [
        [("read", {"path": "a.py"}, MID)],
        [("read", {"path": "a.py"}, SMALL)],
        [("read", {"path": "b.py"}, MID)],
        [("read", {"path": "b.py"}, SMALL)],
        *filler(2),
    ]
    # The second 3000 bytes cross it, and both stubs land in one batch.
    assert stubbed(prune(openai(two), POLICY)[0]) == ["call_1", "call_3"]
    # A lower threshold applies each stub as soon as it's eligible.
    eager = PrunePolicy(policy_version="test", min_new_bytes=0)
    assert stubbed(prune(openai(one), eager)[0]) == ["call_1"]


def test_pairing_and_text_are_preserved_in_both_formats() -> None:
    oa = openai(
        [
            [("read", {"path": "a.py"}, BIG), ("bash", {"command": "ls"}, BIG)],
            [("read", {"path": "a.py"}, BIG), ("bash", {"command": "ls"}, MID)],
            *filler(2),
        ]
    )
    pruned, _ = prune(oa, POLICY)
    assert stubbed(pruned) == ["call_1", "call_2"]
    assert len(pruned) == len(oa)
    for before, after in zip(oa, pruned, strict=True):
        if before["role"] == "tool":
            assert after.keys() == before.keys()
            assert after["tool_call_id"] == before["tool_call_id"]
        else:
            assert after == before  # system, user, assistant text and tool calls

    an = anthropic(
        [
            [("Read", {"file_path": "a.py"}, BIG)],
            [("Read", {"file_path": "a.py"}, BIG)],
            *filler(2, "anthropic"),
        ]
    )
    an[2]["content"][0]["cache_control"] = {"type": "ephemeral"}
    an[2]["content"].append({"type": "text", "text": "User note beside a result."})
    pruned, _ = prune(an, POLICY)
    assert stubbed(pruned) == ["toolu_1"]
    for before, after in zip(an, pruned, strict=True):
        if before["role"] == "assistant":
            assert after == before
            continue
        assert len(after["content"]) == len(before["content"])
        for old, new in zip(before["content"], after["content"], strict=True):
            if old["type"] == "tool_result":
                assert {k: v for k, v in new.items() if k != "content"} == {
                    k: v for k, v in old.items() if k != "content"
                }
            else:
                assert new == old
    assert pruned[2]["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_list_content_keeps_its_shape() -> None:
    messages = anthropic(
        [
            [("Read", {"file_path": "a.py"}, [{"type": "text", "text": BIG}])],
            [("Read", {"file_path": "a.py"}, BIG)],
            *filler(2, "anthropic"),
        ]
    )
    content = prune(messages, POLICY)[0][2]["content"][0]["content"]
    assert isinstance(content, list) and content[0]["text"].startswith(STUB_PREFIX)
    # A result with an image part isn't a prunable shape.
    image = [{"type": "image", "source": {"type": "base64", "data": BIG}}]
    messages[2]["content"][0]["content"] = image
    assert stubbed(prune(messages, POLICY)[0]) == []


@pytest.mark.parametrize(
    "messages",
    [
        None,
        "not a list",
        [],
        [None, 3, {"role": "assistant", "tool_calls": "x"}],
        [
            {"role": "assistant", "tool_calls": [{"function": {"arguments": "{"}}]},
            {"role": "tool", "tool_call_id": None, "content": BIG},
            {"role": "assistant", "content": [{"type": "tool_use", "input": 7}]},
            {"role": "user", "content": [{"type": "tool_result", "content": 5}]},
        ],
    ],
)
def test_unknown_shapes_pass_through(messages) -> None:
    before = copy.deepcopy(messages)
    pruned, report = prune(messages, POLICY)
    assert pruned == before
    assert report["stubbed_results"] == 0


# Property checks over seeded synthetic Sessions in both formats.

PATHS = ["a.py", "b.py", "c.py"]
COMMANDS = ["pytest -q", "git status"]
SIZES = [100, 700, 2500, 6000]


def session(seed: int, fmt: str, turns: int = 30) -> list[dict]:
    rng = random.Random(seed)
    names = (
        {"read": "read", "edit": "edit", "write": "write", "run": "bash"}
        if fmt == "openai"
        else {"read": "Read", "edit": "Edit", "write": "Write", "run": "Bash"}
    )
    key = "path" if fmt == "openai" else "file_path"
    plan = []
    for _ in range(turns):
        turn = []
        for _ in range(rng.choice([1, 1, 2])):
            kind = rng.choice(["read", "read", "edit", "write", "run", "unknown"])
            text = f"{rng.random()}" + "q" * rng.choice(SIZES)
            if fmt == "anthropic" and rng.random() < 0.1:
                text = (text, True)
            if kind == "run":
                turn.append((names[kind], {"command": rng.choice(COMMANDS)}, text))
            elif kind == "unknown":
                turn.append(("find", {"path": rng.choice(PATHS)}, text))
            else:
                turn.append((names[kind], {key: rng.choice(PATHS)}, text))
        plan.append(turn)
    return (openai if fmt == "openai" else anthropic)(plan)


CASES = [(seed, fmt) for seed in range(12) for fmt in ("openai", "anthropic")]


@pytest.mark.parametrize(("seed", "fmt"), CASES)
def test_determinism_and_no_input_mutation(seed: int, fmt: str) -> None:
    messages = session(seed, fmt)
    before = copy.deepcopy(messages)
    first = prune(messages, POLICY)
    assert messages == before
    assert prune(copy.deepcopy(before), POLICY) == first


@pytest.mark.parametrize(("seed", "fmt"), CASES)
def test_idempotence(seed: int, fmt: str) -> None:
    once, _ = prune(session(seed, fmt), POLICY)
    twice, report = prune(once, POLICY)
    assert twice == once
    assert report["stubbed_results"] == 0


@pytest.mark.parametrize(("seed", "fmt"), CASES)
def test_monotonic_prefix_across_appended_turns(seed: int, fmt: str) -> None:
    messages = session(seed, fmt)
    ends = [i for i, m in enumerate(messages) if m["role"] == "assistant"]
    ends = [*ends[1:], len(messages)]  # each request ends before the next turn
    fired = 0
    previous: list[dict] | None = None
    for end in ends:
        pruned, _ = prune(messages[:end], POLICY)
        if previous is not None:
            earlier = set(stubbed(previous))
            now = set(stubbed(pruned[: len(previous)]))
            assert earlier <= now, "an applied stub disappeared"
            if now == earlier:
                assert pruned[: len(previous)] == previous, "cached prefix broke"
            else:
                fired += 1
                new_bytes = sum(
                    len(_result(messages, call_id)) for call_id in now - earlier
                )
                assert new_bytes >= POLICY.min_new_bytes
        previous = pruned
    assert fired, "the synthetic Session never exercised the threshold"


def _result(messages: list[dict], call_id: str) -> str:
    for message in messages:
        if message.get("tool_call_id") == call_id:
            return message["content"]
        if message["role"] == "user" and isinstance(message["content"], list):
            for block in message["content"]:
                if block.get("tool_use_id") == call_id:
                    return block["content"]
    raise KeyError(call_id)


def test_prune_request_leaves_other_fields_and_provider_managed_context() -> None:
    from sediment_context import prune_request

    messages = anthropic(
        [
            [("Read", {"file_path": "a.py"}, BIG)],
            [("Read", {"file_path": "a.py"}, BIG)],
            *filler(2, "anthropic"),
        ]
    )
    request = {"model": "m", "system": "S", "messages": messages, "extra": [1]}
    pruned, report = prune_request(request, POLICY)
    assert report["stubbed_results"] == 1
    assert {k: v for k, v in pruned.items() if k != "messages"} == {
        k: v for k, v in request.items() if k != "messages"
    }
    assert request["messages"] is messages  # the input isn't mutated
    # Provider-side context editing or compaction: never pruned.
    managed = {**request, "context_management": {"edits": [{"type": "x"}]}}
    assert prune_request(managed, POLICY) == (managed, None)
    # Nothing to stub: the same request object comes back.
    quiet = {"model": "m", "messages": messages[:3]}
    assert prune_request(quiet, POLICY)[0] is quiet
    assert prune_request({"model": "m"}, POLICY) == ({"model": "m"}, None)
