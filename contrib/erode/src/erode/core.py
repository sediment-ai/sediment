# SPDX-License-Identifier: MIT
"""
Deterministic supersession pruning of tool output in an agent's model requests.

Standard library only. ``erode.proxy`` and ``erode.litellm_hook`` both wrap
``prune_request``; nothing here imports LiteLLM or any other dependency.

A tool result is superseded when later tool calls in the same request make it
out of date: a read of path P by a later read, edit, or write of P, and a run
of command C by a later run of the identical command. A superseded result
keeps its message and its tool-call pairing; its content becomes one stub
line. Everything else passes through untouched: system, user, and assistant
text, tool calls, the last two turns, small results, error results, and tools
the adapters don't know.

``prune`` is a pure function of the request. A harness resends its
conversation with new turns appended, so each request reproduces the stubs of
the request before it, and the provider's cached prefix breaks only where a
stub first appears. New stubs are applied in batches of at least
``min_new_bytes`` of superseded output to keep those breaks rare. The batches
are replayed from the request itself: boundary m is the request that holds the
first m assistant messages.

``prune_request`` is the entry point for a whole request body. A request that
carries Anthropic ``context_management`` hands context editing or compaction to
the provider, so it passes through untouched. A client-side compaction request,
such as Claude Code's, is an ordinary Messages request with a summarization
prompt; nothing in its wire shape identifies it reliably, so it is pruned like
any other request.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

STUB_PREFIX = "[erode: superseded"
READ, WRITE, RUN = "read", "write", "run"

# Wire format adapters: tool name -> kind. An unlisted tool is never pruned.
OPENAI_TOOLS = {"read": READ, "edit": WRITE, "write": WRITE, "bash": RUN}
ANTHROPIC_TOOLS = {
    "Read": READ,
    "Edit": WRITE,
    "MultiEdit": WRITE,
    "Write": WRITE,
    "Bash": RUN,
}
_TARGET_CHARS = 160  # keeps every stub far below min_result_bytes


@dataclass(frozen=True)
class PrunePolicy:
    policy_version: str = "1"
    min_result_bytes: int = 512
    min_new_bytes: int = 4096
    protected_turns: int = 2


@dataclass
class _Call:
    step: int  # 1-based position among all tool calls in the request
    turn: int  # 0-based index of the assistant message that issued it
    name: str
    kind: str | None
    target: str | None
    span: tuple[Any, Any] = (None, None)  # a read's (offset, limit)
    where: tuple[int, int | None] | None = None  # result (message, block)
    text: str | None = None  # result text, when its shape is prunable
    error: bool = False


def _text(content: Any) -> str | None:
    """Result text for a string or all-text-parts content; None otherwise."""
    if isinstance(content, str):
        return content
    if isinstance(content, list) and content:
        parts = [
            part.get("text")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        if len(parts) == len(content) and all(isinstance(p, str) for p in parts):
            return "".join(parts)
    return None


def _call(step: int, turn: int, name: Any, arguments: Any, tools: dict) -> _Call:
    kind = tools.get(name) if isinstance(name, str) else None
    args = arguments if isinstance(arguments, dict) else {}
    if kind == RUN:
        target = args.get("command")
    else:
        target = args.get("file_path", args.get("path"))
    if not isinstance(target, str) or not target:
        kind, target = None, None
    span = (args.get("offset"), args.get("limit")) if kind == READ else (None, None)
    return _Call(step, turn, str(name), kind, target, span)


def _calls(messages: list) -> tuple[list[_Call], int]:
    """Normalize both wire formats into tool calls with result positions."""
    calls: list[_Call] = []
    by_id: dict[str, _Call] = {}
    turn = -1
    for i, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        role, content = message.get("role"), message.get("content")
        if role == "assistant":
            turn += 1
            # OpenAI chat: tool_calls with JSON-string arguments.
            for tool_call in message.get("tool_calls") or []:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function") or {}
                try:
                    arguments = json.loads(function.get("arguments") or "")
                except (TypeError, ValueError):
                    arguments = None
                call = _call(
                    len(calls) + 1, turn, function.get("name"), arguments, OPENAI_TOOLS
                )
                calls.append(call)
                by_id.setdefault(str(tool_call.get("id")), call)
            # Anthropic Messages: tool_use blocks.
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    call = _call(
                        len(calls) + 1,
                        turn,
                        block.get("name"),
                        block.get("input"),
                        ANTHROPIC_TOOLS,
                    )
                    calls.append(call)
                    by_id.setdefault(str(block.get("id")), call)
        elif role == "tool":
            call = by_id.get(str(message.get("tool_call_id")))
            if call is not None and call.where is None:
                call.where, call.text = (i, None), _text(content)
        elif role == "user" and isinstance(content, list):
            for j, block in enumerate(content):
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                call = by_id.get(str(block.get("tool_use_id")))
                if call is not None and call.where is None:
                    call.where, call.text = (i, j), _text(block.get("content"))
                    call.error = block.get("is_error") is True
    return calls, turn + 1


def _supersedes(later: _Call, earlier: _Call) -> bool:
    # A superseder must have run: an errored edit left the file as it was.
    if later.where is None or later.error:
        return False
    if earlier.kind == RUN:
        return later.kind == RUN
    if later.kind == WRITE:
        return True
    # A partial read replaces only a read of the same range.
    return later.kind == READ and later.span in ((None, None), earlier.span)


def _stub(by: _Call) -> str:
    target = by.target or ""
    if len(target) > _TARGET_CHARS:
        target = target[: _TARGET_CHARS - 1] + "…"
    again = "run it again" if by.kind == RUN else "read it again"
    what = "output" if by.kind == RUN else "content"
    return (
        f"{STUB_PREFIX} by step {by.step} ({by.name.lower()} of {target}); "
        f"{again} if you need the current {what}]"
    )


def _replace(content: Any, stub: str) -> Any:
    return stub if isinstance(content, str) else [{"type": "text", "text": stub}]


def prune(messages: list[dict], policy: PrunePolicy) -> tuple[list[dict], dict]:
    """Return the messages the model sees and a count-only report."""
    report = {
        "policy_version": policy.policy_version,
        "stubbed_results": 0,
        "bytes_removed": 0,
    }
    if not isinstance(messages, list):
        return messages, report
    calls, turns = _calls(messages)

    by_target: dict[tuple[str, str], list[_Call]] = {}
    for call in calls:
        if call.kind is not None:
            family = RUN if call.kind == RUN else "path"
            by_target.setdefault((family, call.target), []).append(call)

    # Each candidate becomes eligible at the first boundary where it is both
    # superseded and outside the protected turns.
    candidates: list[tuple[int, int, _Call, _Call, int]] = []
    for call in calls:
        if call.kind not in (READ, RUN) or call.text is None or call.error:
            continue
        size = len(call.text.encode("utf-8"))
        if size < policy.min_result_bytes or call.text.startswith(STUB_PREFIX):
            continue
        family = RUN if call.kind == RUN else "path"
        later = (
            c
            for c in by_target[(family, call.target)]
            if c.step > call.step and _supersedes(c, call)
        )
        by = next(later, None)
        if by is None:
            continue
        eligible = max(by.turn + 1, call.turn + policy.protected_turns + 1)
        if eligible <= turns:
            candidates.append((eligible, call.step, call, by, size))
    candidates.sort(key=lambda item: (item[0], item[1]))

    # Replay the threshold boundary by boundary, as earlier requests saw it.
    applied: list[tuple[_Call, _Call, int]] = []
    pending: list[tuple[_Call, _Call, int]] = []
    pending_bytes = 0
    for index, (eligible, _, call, by, size) in enumerate(candidates):
        pending.append((call, by, size))
        pending_bytes += size
        boundary_done = (
            index + 1 == len(candidates) or candidates[index + 1][0] != eligible
        )
        if boundary_done and pending_bytes >= policy.min_new_bytes:
            applied += pending
            pending, pending_bytes = [], 0

    result = list(messages)
    for call, by, size in applied:
        stub = _stub(by)
        i, j = call.where
        message = dict(result[i])
        if j is None:
            message["content"] = _replace(message.get("content"), stub)
        else:
            blocks = list(message["content"])
            blocks[j] = dict(
                blocks[j], content=_replace(blocks[j].get("content"), stub)
            )
            message["content"] = blocks
        result[i] = message
        report["stubbed_results"] += 1
        report["bytes_removed"] += size - len(stub.encode("utf-8"))
    return result, report


def prune_request(request: Any, policy: PrunePolicy) -> tuple[Any, dict | None]:
    """Prune a request body's messages; None when the request isn't prunable.

    Returns the request itself when nothing changes, else a shallow copy with
    new messages. Every other field, ``cache_control`` included, is untouched.
    """
    if not isinstance(request, dict) or not isinstance(request.get("messages"), list):
        return request, None
    if "context_management" in request:
        return request, None  # the provider manages this request's context
    messages, report = prune(request["messages"], policy)
    if not report["stubbed_results"]:
        return request, report
    return {**request, "messages": messages}, report
