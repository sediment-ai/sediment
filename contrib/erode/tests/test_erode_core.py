# SPDX-License-Identifier: MIT
"""The supersession core: rules, pairing, the 4 KB threshold, and purity."""

from __future__ import annotations

import copy
import json
import random

import pytest

from erode.core import STUB_PREFIX, PrunePolicy, prune, prune_request

POLICY = PrunePolicy()
BIG = "x" * 5000  # alone crosses the 4 KB threshold
MID = "y" * 3000  # needs a second superseded result to cross it
SMALL = "z" * 400  # under the 512-byte floor


def openai(turns: list[list[tuple]]) -> list[dict]:
    """OpenAI chat (pi): one assistant message per turn, then role tool results.

    erode skips this format; the builder exists to prove it.
    """
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


def filler(n: int) -> list[list[tuple]]:
    return [[("Bash", {"command": f"echo {i}"}, "ok")] for i in range(n)]


def stubbed(messages: list[dict]) -> list[str]:
    """Tool-use ids whose results are stubs."""
    ids = []
    for message in messages:
        if message.get("role") == "user" and isinstance(message["content"], list):
            for block in message["content"]:
                content = block.get("content")
                if block.get("type") == "tool_result" and str(content).startswith(
                    STUB_PREFIX
                ):
                    ids.append(block["tool_use_id"])
    return ids


def test_read_superseded_by_later_edit() -> None:
    messages = anthropic(
        [
            [("Read", {"file_path": "src/app.py"}, BIG)],
            [("Edit", {"file_path": "src/app.py", "old_string": "a"}, "ok")],
            *filler(2),
        ]
    )
    pruned, report = prune(messages, POLICY)
    assert stubbed(pruned) == ["toolu_1"]
    stub = pruned[2]["content"][0]["content"]
    assert stub == (
        "[erode: superseded by step 2 (edit of src/app.py); "
        "read it again if you need the current content]"
    )
    assert report == {
        "policy_version": "4",
        "stubbed_results": 1,
        "bytes_removed": len(BIG) - len(stub),
    }


def test_read_superseded_by_later_read_and_write_anthropic() -> None:
    messages = anthropic(
        [
            [("Read", {"file_path": "/r/a.py"}, BIG)],
            [("Read", {"file_path": "/r/a.py"}, BIG)],
            [("Write", {"file_path": "/r/a.py", "content": "new"}, "ok")],
            *filler(2),
        ]
    )
    pruned, report = prune(messages, POLICY)
    assert stubbed(pruned) == ["toolu_1", "toolu_2"]
    assert "(read of /r/a.py)" in pruned[2]["content"][0]["content"]
    assert "(write of /r/a.py)" in pruned[4]["content"][0]["content"]
    assert report["stubbed_results"] == 2


def test_run_superseded_only_by_identical_command() -> None:
    messages = anthropic(
        [
            [("Bash", {"command": "pytest -q"}, BIG)],
            [("Bash", {"command": "pytest -q tests/a.py"}, BIG)],
            [("Bash", {"command": "pytest -q"}, BIG)],
            *filler(2),
        ]
    )
    pruned, _ = prune(messages, POLICY)
    assert stubbed(pruned) == ["toolu_1"]
    assert "(bash of pytest -q); run it again" in pruned[2]["content"][0]["content"]


def test_edit_does_not_supersede_a_command() -> None:
    messages = anthropic(
        [
            [("Bash", {"command": "cat src/app.py"}, BIG)],
            [("Edit", {"file_path": "src/app.py", "old_string": "a"}, "ok")],
            *filler(2),
        ]
    )
    assert stubbed(prune(messages, POLICY)[0]) == []


def test_partial_read_supersedes_only_the_same_range() -> None:
    partial = {"file_path": "a.py", "offset": 10, "limit": 5}
    kept = anthropic(
        [
            [("Read", {"file_path": "a.py"}, BIG)],
            [("Read", partial, SMALL)],
            *filler(2),
        ]
    )
    assert stubbed(prune(kept, POLICY)[0]) == []
    superseded = anthropic(
        [
            [("Read", partial, BIG)],
            [("Read", partial, SMALL)],
            [("Read", {"file_path": "a.py"}, SMALL)],
            *filler(2),
        ]
    )
    assert stubbed(prune(superseded, POLICY)[0]) == ["toolu_1"]


def test_last_two_turns_are_protected() -> None:
    turns = [
        *filler(1),
        [("Read", {"file_path": "a.py"}, BIG)],
        [("Read", {"file_path": "a.py"}, BIG)],
    ]
    # toolu_2 is in the second-to-last turn.
    assert stubbed(prune(anthropic(turns), POLICY)[0]) == []
    assert stubbed(prune(anthropic([*turns, *filler(1)]), POLICY)[0]) == ["toolu_2"]


def test_small_and_error_results_are_never_stubbed() -> None:
    messages = anthropic(
        [
            [("Read", {"file_path": "a.py"}, SMALL)],
            [("Bash", {"command": "make"}, (BIG, True))],
            [("Read", {"file_path": "a.py"}, SMALL)],
            [("Bash", {"command": "make"}, BIG)],
            *filler(2),
        ]
    )
    assert stubbed(prune(messages, POLICY)[0]) == []


def test_errored_edit_does_not_supersede() -> None:
    messages = anthropic(
        [
            [("Read", {"file_path": "a.py"}, BIG)],
            [("Edit", {"file_path": "a.py"}, ("String not found", True))],
            *filler(2),
        ]
    )
    assert stubbed(prune(messages, POLICY)[0]) == []


def test_unknown_tools_pass_through() -> None:
    an = anthropic(
        [
            [("Glob", {"pattern": "**/*.py"}, BIG)],
            [("Glob", {"pattern": "**/*.py"}, BIG)],
            [("Grep", {"path": "a.py", "pattern": "x"}, BIG)],
            [("Grep", {"path": "a.py", "pattern": "x"}, BIG)],
            [("NotebookEdit", {"notebook_path": "n.ipynb"}, BIG)],
            [("NotebookEdit", {"notebook_path": "n.ipynb"}, BIG)],
            *filler(2),
        ]
    )
    assert prune(an, POLICY)[0] == an


def test_threshold_batches_new_stubs() -> None:
    one = [
        [("Read", {"file_path": "a.py"}, MID)],
        [("Read", {"file_path": "a.py"}, SMALL)],
        *filler(2),
    ]
    # 3000 superseded bytes stay below the 4 KB threshold.
    assert stubbed(prune(anthropic(one), POLICY)[0]) == []
    two = [
        [("Read", {"file_path": "a.py"}, MID)],
        [("Read", {"file_path": "a.py"}, SMALL)],
        [("Read", {"file_path": "b.py"}, MID)],
        [("Read", {"file_path": "b.py"}, SMALL)],
        *filler(2),
    ]
    # The second 3000 bytes cross it, and both stubs land in one batch.
    assert stubbed(prune(anthropic(two), POLICY)[0]) == ["toolu_1", "toolu_3"]
    # A lower threshold applies each stub as soon as it's eligible.
    eager = PrunePolicy(policy_version="test", min_new_bytes=0)
    assert stubbed(prune(anthropic(one), eager)[0]) == ["toolu_1"]


def test_pairing_and_text_are_preserved() -> None:
    an = anthropic(
        [
            [("Read", {"file_path": "a.py"}, BIG), ("Bash", {"command": "ls"}, BIG)],
            [("Read", {"file_path": "a.py"}, BIG), ("Bash", {"command": "ls"}, MID)],
            *filler(2),
        ]
    )
    an[2]["content"][0]["cache_control"] = {"type": "ephemeral"}
    an[2]["content"].append({"type": "text", "text": "User note beside a result."})
    pruned, _ = prune(an, POLICY)
    assert stubbed(pruned) == ["toolu_1", "toolu_2"]
    assert len(pruned) == len(an)
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
            *filler(2),
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
        [None, 3, {"role": "assistant", "content": "x"}],
        [
            {"role": "assistant", "content": [{"type": "tool_use", "name": "Read"}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": 7}]},
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


# Property checks over seeded synthetic Sessions.

PATHS = ["a.py", "b.py", "c.py"]
COMMANDS = ["pytest -q", "git status"]
SIZES = [100, 700, 2500, 6000]


def session(seed: int, turns: int = 30) -> list[dict]:
    rng = random.Random(seed)
    names = {"read": "Read", "edit": "Edit", "write": "Write", "run": "Bash"}
    plan = []
    for _ in range(turns):
        turn = []
        for _ in range(rng.choice([1, 1, 2])):
            kind = rng.choice(["read", "read", "edit", "write", "run", "unknown"])
            text = f"{rng.random()}" + "q" * rng.choice(SIZES)
            if rng.random() < 0.1:
                text = (text, True)
            if kind == "run":
                turn.append((names[kind], {"command": rng.choice(COMMANDS)}, text))
            elif kind == "unknown":
                turn.append(("find", {"path": rng.choice(PATHS)}, text))
            else:
                turn.append((names[kind], {"file_path": rng.choice(PATHS)}, text))
        plan.append(turn)
    return anthropic(plan)


SEEDS = range(24)


@pytest.mark.parametrize("seed", SEEDS)
def test_determinism_and_no_input_mutation(seed: int) -> None:
    messages = session(seed)
    before = copy.deepcopy(messages)
    first = prune(messages, POLICY)
    assert messages == before
    assert prune(copy.deepcopy(before), POLICY) == first


@pytest.mark.parametrize("seed", SEEDS)
def test_idempotence(seed: int) -> None:
    once, _ = prune(session(seed), POLICY)
    twice, report = prune(once, POLICY)
    assert twice == once
    assert report["stubbed_results"] == 0


@pytest.mark.parametrize("seed", SEEDS)
def test_monotonic_prefix_across_appended_turns(seed: int) -> None:
    messages = session(seed)
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
        if message["role"] == "user" and isinstance(message["content"], list):
            for block in message["content"]:
                if block.get("tool_use_id") == call_id:
                    return block["content"]
    raise KeyError(call_id)


def test_prune_request_leaves_other_fields_and_provider_managed_context() -> None:
    messages = anthropic(
        [
            [("Read", {"file_path": "a.py"}, BIG)],
            [("Read", {"file_path": "a.py"}, BIG)],
            *filler(2),
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


def test_results_with_part_metadata_pass_through() -> None:
    # A part's cache_control or unknown fields can't survive a rebuilt stub.
    marked = [{"type": "text", "text": BIG, "cache_control": {"type": "ephemeral"}}]
    messages = anthropic(
        [
            [("Read", {"file_path": "a.py"}, marked)],
            [("Read", {"file_path": "a.py"}, BIG)],
            *filler(2),
        ]
    )
    pruned, report = prune(messages, POLICY)
    assert pruned[2] == messages[2]
    assert report["stubbed_results"] == 0


def test_openai_chat_requests_are_skipped_and_counted() -> None:
    # pi's tool messages carry no error flag: this "edit" failed, but nothing on
    # the wire says so, and stubbing the read would hide content the model needs.
    messages = openai(
        [
            [("read", {"path": "src/app.py"}, BIG)],
            [("edit", {"path": "src/app.py"}, "Error: oldText not found")],
            [("read", {"path": "src/app.py"}, BIG)],
            *[[("bash", {"command": f"echo {i}"}, "ok")] for i in range(2)],
        ]
    )
    before = copy.deepcopy(messages)
    pruned, report = prune(messages, POLICY)
    assert pruned is messages and messages == before
    assert report == {
        "policy_version": "4",
        "stubbed_results": 0,
        "bytes_removed": 0,
        "skipped": "openai_chat",
    }
    request = {"model": "m", "messages": messages}
    assert prune_request(request, POLICY) == (request, report)


def test_pi_over_anthropic_messages_is_pruned() -> None:
    # pi's provider in the gateway docs uses anthropic-messages, which carries
    # is_error, so its lowercase tools get the same rules as Claude Code's.
    messages = anthropic(
        [
            [("read", {"path": "src/app.py"}, BIG)],
            [("edit", {"path": "src/app.py"}, ("oldText not found", True))],
            [("bash", {"command": "pytest -q"}, BIG)],
            [("bash", {"command": "pytest -q"}, BIG)],
            *filler(2),
        ]
    )
    pruned, report = prune(messages, POLICY)
    # The errored edit supersedes nothing; the repeated run is stubbed.
    assert stubbed(pruned) == ["toolu_3"]
    assert "(bash of pytest -q)" in pruned[6]["content"][0]["content"]
    assert "skipped" not in report
