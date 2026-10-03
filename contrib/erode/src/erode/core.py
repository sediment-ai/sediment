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

OpenAI chat requests pass through unpruned, and the report counts them as
skipped. A role ``tool`` message carries no error flag, so a failed read or
edit looks like a successful one, and a failed edit would supersede a read the
model still needs.

``prune`` is a pure function of the request. A harness resends its
conversation with new turns appended, so each request reproduces the stubs of
the request before it, and the provider's cached prefix breaks only where a
stub first appears. New stubs are applied in batches of at least
``min_new_bytes`` of superseded output to keep those breaks rare. The batches
are replayed from the request itself: boundary m is the request that holds the
first m assistant messages.

``prune_input`` does the same for an OpenAI Responses ``input`` list, with the
adapter for Codex CLI's ``exec`` calls: JavaScript that the adapter parses with
a strict line grammar and never evaluates. A call outside that grammar is
opaque, so a missed read costs savings, never correctness.

``prune_request`` is the entry point for a whole request body. A request whose
Anthropic ``context_management`` asks the provider to clear tool results or
compact hands that work to the provider, so it passes through untouched. Only
thinking-block clearing (``clear_thinking_*`` edits), which Claude Code sends
on every request, leaves tool results to erode. A client-side compaction request,
such as Claude Code's, is an ordinary Messages request with a summarization
prompt; nothing in its wire shape identifies it reliably, so it is pruned like
any other request.
"""

from __future__ import annotations

import html
import json
import posixpath
import re
import shlex
from dataclasses import dataclass
from typing import Any

STUB_PREFIX = "[erode: superseded"
READ, WRITE, RUN = "read", "write", "run"

# Anthropic Messages adapter: tool name -> kind. An unlisted tool is never pruned.
ANTHROPIC_TOOLS = {
    # Claude Code.
    "Read": READ,
    "Edit": WRITE,
    "MultiEdit": WRITE,
    "Write": WRITE,
    "Bash": RUN,
    # pi, whose Anthropic Messages results carry is_error.
    "read": READ,
    "edit": WRITE,
    "write": WRITE,
    "bash": RUN,
}
_TARGET_CHARS = 160  # keeps every stub far below min_result_bytes

# Codex CLI (Responses API): every tool call is a custom_tool_call named exec,
# one statement per line. Anything else on a line makes the call opaque.
# Statements are parsed with string operations and JSON decoding, never a
# backtracking pattern: the input is agent-controlled.
_STATEMENT_HEADS = {
    "exec_command": "text(await tools.exec_command(",
    "apply_patch": "text(await tools.apply_patch(",
}
_STATEMENT_TAIL = "));"
_PATCH_PATH = re.compile(r"^\*\*\* (?:(?:Add|Update|Delete) File|Move to): (.+)$", re.M)
_CWD = re.compile(r"<cwd>([^<]*)</cwd>")
_SHELL_META = frozenset("|&;<>`$()*?[\n")
_SED_RANGE = re.compile(r"\d+(?:,\d+)?p")


@dataclass(frozen=True)
class PrunePolicy:
    policy_version: str = "5"
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
    span: tuple = (None, None)  # which part of the target a read covers
    where: tuple | None = None  # result (message, block, part)
    text: str | None = None  # result text, when its shape is prunable
    error: bool = False
    shown: str | None = None  # the target as a stub names it, if not target


def _text(content: Any) -> str | None:
    """Result text for a string or all-text-parts content; None otherwise.

    A part with any field besides ``type`` and ``text``, such as
    ``cache_control``, isn't prunable: a stub can't carry that field.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list) and content:
        parts = [
            part.get("text")
            for part in content
            if isinstance(part, dict)
            and part.keys() == {"type", "text"}
            and part["type"] == "text"
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
    """Normalize Anthropic Messages into tool calls with result positions."""
    calls: list[_Call] = []
    by_id: dict[str, _Call] = {}
    turn = -1
    for i, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        role, content = message.get("role"), message.get("content")
        if role == "assistant":
            turn += 1
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
        elif role == "user" and isinstance(content, list):
            for j, block in enumerate(content):
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                call = by_id.get(str(block.get("tool_use_id")))
                if call is not None and call.where is None:
                    call.where = (i, j, None)
                    call.text = _text(block.get("content"))
                    call.error = block.get("is_error") is True
    return calls, turn + 1


def _statements(source: Any) -> list[tuple[str, Any]] | None:
    """Parse a Codex exec call's JavaScript, or None when it's opaque."""
    if not isinstance(source, str):
        return None
    statements: list[tuple[str, Any]] = []
    for line in source.splitlines():
        line = line.strip()
        if not line:
            continue
        found = _statement(line)
        if found is None:
            return None
        tool, argument = found
        try:
            if tool == "apply_patch":
                value = json.loads(argument)
                if not isinstance(value, str):
                    return None
            else:
                value = _arguments(argument)
                if value is None or not isinstance(value.get("cmd"), str):
                    return None
        except ValueError:  # a JavaScript-only escape, for example
            return None
        statements.append((tool, value))
    return statements or None


def _statement(line: str) -> tuple[str, str] | None:
    """(tool, argument source) for one recognized statement line, else None."""
    if not line.endswith(_STATEMENT_TAIL):
        return None
    for tool, head in _STATEMENT_HEADS.items():
        if line.startswith(head):
            return tool, line[len(head) : -len(_STATEMENT_TAIL)]
    return None


def _arguments(source: str) -> dict | None:
    """``{key:value, ...}`` with identifier keys and JSON scalar values."""
    source = source.strip()
    if not (source.startswith("{") and source.endswith("}")):
        return None
    body, arguments, position = source[1:-1], {}, 0
    decoder = json.JSONDecoder(parse_constant=_reject)
    while True:
        colon = body.find(":", position)
        if colon < 0:
            return None if body[position:].strip() else arguments
        key = body[position:colon].strip()
        if not (key.isascii() and key.replace("$", "_").isidentifier()):
            return None
        if key in arguments:
            return None
        start = colon + 1
        while start < len(body) and body[start].isspace():
            start += 1
        value, end = decoder.raw_decode(body, start)
        if isinstance(value, (dict, list)) or value is None:
            return None
        arguments[key] = value
        while end < len(body) and body[end].isspace():
            end += 1
        if end == len(body):
            return arguments
        if body[end] != ",":
            return None
        position = end + 1


def _reject(constant: str) -> None:
    raise ValueError(constant)  # NaN and Infinity aren't JSON


def _codex_path(path: str, cwd: str | None) -> str:
    """Join a relative path to cwd and normalize it, lexically only."""
    if cwd is not None and not path.startswith("/"):
        path = posixpath.join(cwd, path)
    return posixpath.normpath(path) if path.startswith("/") else path


def _codex_read(cmd: str, cwd: str | None) -> tuple[str, str] | None:
    """(path, "full" or "partial") for a recognized read form, else None."""
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return None
    if len(tokens) == 4 and tokens[:2] == ["sed", "-n"]:
        if not _SED_RANGE.fullmatch(tokens[2]):
            return None
        form, path = "partial", tokens[3]
    elif len(tokens) == 2 and tokens[0] in ("head", "tail"):
        form, path = "partial", tokens[1]  # the first or last lines only
    elif len(tokens) == 2 and tokens[0] == "cat":
        form, path = "full", tokens[1]
    elif len(tokens) == 3 and tokens[:2] == ["nl", "-ba"]:
        form, path = "full", tokens[2]
    else:
        return None
    if _SHELL_META & set(cmd) or path.startswith("-"):
        return None
    return _codex_path(path, cwd), form


def _codex_calls(
    step: int, turn: int, statements: list[tuple[str, Any]], cwd: str | None
) -> list[list[_Call]]:
    """Normalized calls per statement; a patch yields one call per path."""
    result = []
    for tool, value in statements:
        step += 1
        if tool == "apply_patch":
            paths = [
                _codex_path(p.rstrip("\r"), cwd) for p in _PATCH_PATH.findall(value)
            ]
            result.append([_Call(step, turn, tool, WRITE, p) for p in paths])
            continue
        cmd = value["cmd"]
        others = json.dumps(
            {k: v for k, v in value.items() if k != "cmd"}, sort_keys=True
        )
        # A call's workdir, absolute or relative to cwd, is where it runs.
        workdir = value.get("workdir", cwd)
        if "workdir" in value and (not isinstance(workdir, str) or not workdir):
            read = None  # an unreadable workdir: treat the command as a run
        else:
            base = workdir if cwd is None else posixpath.join(cwd, workdir)
            read = _codex_read(cmd, base)
        if read is None:
            call = _Call(step, turn, tool, RUN, cmd + "\x00" + others, shown=cmd)
        else:
            path, form = read
            span = ("full", others) if form == "full" else ("partial", cmd, others)
            call = _Call(step, turn, tool, READ, path, span)
        result.append([call])
    return result


def _part_result(call: _Call, text: str) -> None:
    """Record one output part as a call's result, with its error state."""
    call.text = text
    if text.startswith(STUB_PREFIX):
        return  # stubbed by an earlier pass: it was a valid result
    if call.kind == WRITE:
        call.error = text != "{}"
        return
    try:
        result = json.loads(text)
    except ValueError:
        result = None
    code = result.get("exit_code") if isinstance(result, dict) else None
    call.error = (
        not isinstance(code, int)
        or isinstance(code, bool)
        or (call.kind == READ and code != 0)
    )


def _input_side(item: dict) -> bool:
    kind = item.get("type")
    if kind == "message":
        return item.get("role") != "assistant"
    return kind == "additional_tools" or str(kind).endswith("_output")


def _responses_calls(items: list) -> tuple[list[_Call], int]:
    """Normalize a Responses input list, as Codex CLI sends it."""
    calls: list[_Call] = []
    pending: dict[str, list[list[_Call]]] = {}
    steps, turn, after_input, cwd = 0, -1, True, None
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        side = _input_side(item)
        if not side and after_input:
            turn += 1  # a turn is one model response's run of output items
        after_input = side
        kind = item.get("type")
        if kind == "message" and item.get("role") == "user":
            for part in item.get("content") or []:
                text = part.get("text") if isinstance(part, dict) else None
                if isinstance(text, str) and "<environment_context>" in text:
                    found = _CWD.search(text)
                    if found is not None:
                        cwd = html.unescape(found.group(1))
        elif kind == "custom_tool_call" and item.get("name") == "exec":
            statements = _statements(item.get("input"))
            call_id = str(item.get("call_id"))
            if statements is None or call_id in pending:
                continue
            grouped = _codex_calls(steps, turn, statements, cwd)
            steps += len(grouped)
            pending[call_id] = grouped
        elif kind == "custom_tool_call_output":
            grouped = pending.pop(str(item.get("call_id")), None)
            parts = item.get("output")
            if grouped is None or not isinstance(parts, list):
                continue
            texts = [p.get("text") if isinstance(p, dict) else None for p in parts]
            if len(parts) != len(grouped) + 1 or not all(
                isinstance(t, str) for t in texts
            ):
                continue  # opaque: the parts don't map onto the statements
            for k, group in enumerate(grouped, start=1):
                for call in group:
                    call.where = (i, None, k)
                    _part_result(call, texts[k])
                calls += group
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
    target = by.shown or by.target or ""
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


def _report(policy: PrunePolicy) -> dict:
    return {
        "policy_version": policy.policy_version,
        "stubbed_results": 0,
        "bytes_removed": 0,
    }


def _openai_chat(messages: list) -> bool:
    return any(
        isinstance(m, dict) and (m.get("role") == "tool" or bool(m.get("tool_calls")))
        for m in messages
    )


def prune(messages: list[dict], policy: PrunePolicy) -> tuple[list[dict], dict]:
    """Return the messages the model sees and a count-only report."""
    if not isinstance(messages, list):
        return messages, _report(policy)
    if _openai_chat(messages):
        # No error flag on OpenAI chat tool results: nothing here is provably
        # superseded, but it's counted.
        return messages, {**_report(policy), "skipped": "openai_chat"}
    return _prune(messages, *_calls(messages), policy)


def prune_input(items: list[dict], policy: PrunePolicy) -> tuple[list[dict], dict]:
    """Return the Responses ``input`` items the model sees and a report."""
    if not isinstance(items, list):
        return items, _report(policy)
    return _prune(items, *_responses_calls(items), policy)


def _prune(
    messages: list, calls: list[_Call], turns: int, policy: PrunePolicy
) -> tuple[list, dict]:
    report = _report(policy)

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
        i, j, k = call.where
        message = dict(result[i])
        if k is not None:
            parts = list(message["output"])
            parts[k] = dict(parts[k], text=stub)
            message["output"] = parts
        elif j is None:
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


def provider_managed(request: dict) -> bool:
    """Whether ``context_management`` hands tool results to the provider.

    Edits that clear only thinking blocks leave tool results alone; any other
    edit, or a shape this doesn't recognize, counts as provider-managed.
    """
    if "context_management" not in request:
        return False
    managed = request["context_management"]
    edits = managed.get("edits") if isinstance(managed, dict) else None
    if not isinstance(edits, list) or set(managed) != {"edits"}:
        return True
    return not all(
        isinstance(edit, dict)
        and str(edit.get("type", "")).startswith("clear_thinking_")
        for edit in edits
    )


def prune_request(request: Any, policy: PrunePolicy) -> tuple[Any, dict | None]:
    """Prune a request body; the report is None when it isn't prunable.

    Chat and Messages requests carry ``messages``; Responses requests carry an
    ``input`` list. Returns the request itself when nothing changes, else a
    shallow copy with the new list. Every other field, ``cache_control``
    included, is untouched.
    """
    if not isinstance(request, dict) or provider_managed(request):
        return request, None  # the provider manages this request's context
    if isinstance(request.get("messages"), list):
        key = "messages"
        pruned, report = prune(request[key], policy)
    elif isinstance(request.get("input"), list):
        if request.get("previous_response_id") is not None:
            # Server-side history: nothing here to prune, but it's counted.
            return request, {**_report(policy), "skipped": "previous_response_id"}
        key = "input"
        pruned, report = prune_input(request[key], policy)
    else:
        return request, None
    if not report["stubbed_results"]:
        return request, report
    return {**request, key: pruned}, report
