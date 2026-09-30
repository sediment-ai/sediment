# SPDX-License-Identifier: AGPL-3.0-or-later
"""Replay recorded Anthropic Messages request bodies through erode's ``prune``.

E1 of the gateway pruning spec
(``docs/superpowers/specs/2026-09-28-gateway-context-pruning-design.md``)
records real agent Sessions through the erode proxy with pruning off, then
estimates offline how much ``prune`` would remove. This tool is that replay.

Input is a directory. Every directory in it that directly holds ``*.json``
files is one recorded run, and each file is one request body, as the recorder
saved it. File names must sort in send order, for example a zero-padded
sequence number. Output is JSON with counts only: no prompt, path, command, or
file content from the recordings.

Grouping follows the spec. Requests with the same system prompt and tool names
belong to one agent. The main agent is the one that can launch subagents (a
``Task`` or ``Agent`` tool); if none can, the agent with the most requests is
used, and the output says so. Within an agent, a request joins the thread
whose last request its ``messages`` extend. The main agent's threads are the
segments of one Session: a new segment whose history is shorter than the
previous one's is counted as a compaction. Every other agent's thread is a
subagent or side conversation, reported separately and outside the gate.
The gate stays unevaluated until at least three main Sessions have 20 requests.
OpenAI chat requests count as skipped. Provider-managed Messages requests
retain their input cost and report the ``context_management`` pruning bypass.

Usage:
    uv run python scripts/erode_replay.py RECORDINGS_DIR [--output report.json]
"""

from __future__ import annotations

import argparse
import copy
import dataclasses
import hashlib
import json
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "contrib/erode/src"))
from erode import core  # noqa: E402

LONG_SESSION_REQUESTS = 20
MIN_LONG_SESSIONS = 3
GATE_RATIO = 0.20
SUBAGENT_TOOLS = frozenset({"Task", "Agent"})
POLICIES = {
    "default": core.PrunePolicy(),
    "no_batch_threshold": core.PrunePolicy(min_new_bytes=0),
    "no_thresholds_no_protected_turns": core.PrunePolicy(
        min_result_bytes=0, min_new_bytes=0, protected_turns=0
    ),
}
DROP_OUTS = (
    "context_management",
    "unrecognized_tool",
    "edit_or_write",
    "not_prunable_shape",
    "error_result",
    "under_min_bytes",
    "never_superseded",
    "protected_turns",
    "held_back_by_threshold",
    "stubbed",
)


def size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def _system_text(system: Any) -> str:
    if isinstance(system, list):
        return "".join(b.get("text", "") for b in system if isinstance(b, dict))
    return system if isinstance(system, str) else ""


def signature(body: dict) -> str:
    """Identify an agent by its system prompt and tool names, not content."""
    tools = sorted(
        str(t.get("name")) for t in body.get("tools") or [] if isinstance(t, dict)
    )
    key = json.dumps([_system_text(body.get("system")), tools])
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def tool_names(body: dict) -> set[str]:
    return {str(t.get("name")) for t in body.get("tools") or [] if isinstance(t, dict)}


def load_runs(root: Path) -> dict[str, list[tuple[str, dict | None]]]:
    runs: dict[str, list[tuple[str, dict | None]]] = {}
    for directory in sorted({p.parent for p in root.rglob("*.json")}):
        name = directory.relative_to(root).as_posix() or "."
        items = []
        for path in sorted(directory.glob("*.json")):
            try:
                body = json.loads(path.read_text())
            except (OSError, ValueError):
                body = None
            items.append((path.name, body if isinstance(body, dict) else None))
        runs[name] = items
    return runs


def without_cache_control(value: Any) -> Any:
    """Drop ``cache_control`` markers, which agents move to the newest turn."""
    if isinstance(value, dict):
        return {
            k: without_cache_control(v)
            for k, v in value.items()
            if k != "cache_control"
        }
    if isinstance(value, list):
        return [without_cache_control(v) for v in value]
    return value


def extends(earlier: list, later: list) -> bool:
    if len(later) < len(earlier):
        return False
    return without_cache_control(later[: len(earlier)]) == without_cache_control(
        earlier
    )


def threads(bodies: list[dict]) -> list[list[dict]]:
    """Split one agent's requests into threads of extending ``messages``."""
    out: list[list[dict]] = []
    for body in bodies:
        messages = body["messages"]
        matches = [t for t in out if extends(t[-1]["messages"], messages)]
        if matches:
            max(matches, key=lambda t: len(t[-1]["messages"])).append(body)
        else:
            out.append([body])
    return out


def group(run: list[tuple[str, dict | None]]) -> tuple[list[dict], Counter]:
    """Return the run's conversations and counts of requests not replayed."""
    skipped: Counter = Counter()
    agents: dict[str, list[dict]] = {}
    for _, body in run:
        if body is None:
            skipped["unreadable"] += 1
        elif not isinstance(body.get("messages"), list):
            skipped["not_messages"] += 1
        elif core._openai_chat(body["messages"]):
            skipped["openai_chat"] += 1
        else:
            agents.setdefault(signature(body), []).append(body)
    main = [s for s, b in agents.items() if tool_names(b[0]) & SUBAGENT_TOOLS]
    by_count = not main and bool(agents)
    if by_count:
        main = [max(agents, key=lambda s: len(agents[s]))]
    conversations = []
    for sig, bodies in agents.items():
        split = threads(bodies)
        if sig in main:
            conversations.append(
                dict(role="main", agent=sig, segments=split, main_by_count=by_count)
            )
        else:
            for thread in split:
                conversations.append(dict(role="other", agent=sig, segments=[thread]))
    return conversations, skipped


def stub_positions(messages: list) -> set[tuple[int, int]]:
    out = set()
    for i, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if message.get("role") != "user" or not isinstance(content, list):
            continue
        for j, block in enumerate(content):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                text = core._text(block.get("content"))
                if text is not None and text.startswith(core.STUB_PREFIX):
                    out.add((i, j))
    return out


def pairing_ok(before: list, after: list) -> bool:
    """Only tool_result content may change; ids, flags, and order stay."""
    if len(before) != len(after):
        return False
    for b, a in zip(before, after):
        if b == a:
            continue
        if b.get("role") != "user" or {*b} != {*a}:
            return False
        bc, ac = b.get("content"), a.get("content")
        if not isinstance(bc, list) or not isinstance(ac, list) or len(bc) != len(ac):
            return False
        for x, y in zip(bc, ac):
            if x == y:
                continue
            if x.get("type") != "tool_result" or {
                k: v for k, v in x.items() if k != "content"
            } != {k: v for k, v in y.items() if k != "content"}:
                return False
    return True


def first_diff(a: list, b: list) -> int | None:
    for i, (x, y) in enumerate(zip(a, b)):
        if without_cache_control(x) != without_cache_control(y):
            return i
    return None


def parts(body: dict) -> Counter:
    """Bytes of one request by part, and tool-result bytes by tool name."""
    by_part: Counter = Counter()
    by_tool: Counter = Counter()
    names = {}
    for message in body["messages"]:
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                names[str(block.get("id"))] = str(block.get("name"))
    for message in body["messages"]:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if message.get("role") == "assistant":
            by_part["assistant"] += size(message)
            continue
        if isinstance(content, list):
            results = [
                b
                for b in content
                if isinstance(b, dict) and b.get("type") == "tool_result"
            ]
            for block in results:
                n = size(block)
                by_part["tool_results"] += n
                by_tool[names.get(str(block.get("tool_use_id")), "unknown")] += n
            by_part["user"] += size(message) - sum(size(b) for b in results)
        else:
            by_part["user"] += size(message)
    by_part["tool_schema"] = size(body.get("tools") or [])
    by_part["system"] = size(body.get("system") or "")
    by_part["other_fields"] = size(body) - sum(by_part.values())
    return Counter({**by_part, **{f"tool:{k}": v for k, v in by_tool.items()}})


def _stubbed_by(block: dict, calls: list) -> Any:
    """The superseding call a stub names by its step number."""
    text = core._text(block.get("content")) or ""
    step = int(text[len(core.STUB_PREFIX) :].split("by step ", 1)[1].split(" ", 1)[0])
    return calls[step - 1]


def drop_outs(body: dict, pruned: dict, policy: core.PrunePolicy) -> dict:
    """Where each tool result in the final request falls out of the rules."""
    messages = body["messages"]
    calls, turns = core._calls(messages)
    stubbed = stub_positions(pruned["messages"])
    count: Counter = Counter()
    nbytes: Counter = Counter()
    errored_superseder_stubs = 0
    skipped_errored_superseders = 0
    for call in calls:
        if call.where is None:
            continue
        i, j, _ = call.where
        n = size(messages[i]["content"][j])
        if "context_management" in body:
            fate = "context_management"
        elif call.kind is None:
            fate = "unrecognized_tool"
        elif call.kind == core.WRITE:
            fate = "edit_or_write"
        elif call.text is None:
            fate = "not_prunable_shape"
        elif call.error:
            fate = "error_result"
        elif len(call.text.encode()) < policy.min_result_bytes:
            fate = "under_min_bytes"
        else:
            family = core.RUN if call.kind == core.RUN else "path"
            same = [
                c
                for c in calls
                if c.step > call.step
                and c.kind is not None
                and c.target == call.target
                and (core.RUN if c.kind == core.RUN else "path") == family
            ]
            by = next((c for c in same if core._supersedes(c, call)), None)
            if any(
                c.error and core._supersedes(dataclasses.replace(c, error=False), call)
                for c in same
            ):
                skipped_errored_superseders += 1
            if (i, j) in stubbed:
                fate = "stubbed"
                if _stubbed_by(pruned["messages"][i]["content"][j], calls).error:
                    errored_superseder_stubs += 1
            elif by is None:
                fate = "never_superseded"
            elif max(by.turn + 1, call.turn + policy.protected_turns + 1) > turns:
                fate = "protected_turns"
            else:
                fate = "held_back_by_threshold"
        count[fate] += 1
        nbytes[fate] += n
    return dict(
        results={k: count[k] for k in DROP_OUTS},
        bytes={k: nbytes[k] for k in DROP_OUTS},
        stubs_from_errored_superseder=errored_superseder_stubs,
        superseders_skipped_as_errored=skipped_errored_superseders,
    )


def rereads(segment: list[dict], first_seen: dict) -> int:
    """Stubbed targets the agent read or ran again after the stub appeared."""
    last = segment[-1]["messages"]
    calls, _ = core._calls(last)
    assistant = [i for i, m in enumerate(last) if m.get("role") == "assistant"]
    produced_by = {len(body["messages"]): k for k, body in enumerate(segment, 1)}
    by_position = {c.where[:2]: c for c in calls if c.where}
    count = 0
    for position, k in first_seen.items():
        call = by_position.get(position)
        if call is None:
            continue
        for c in calls:
            if (
                c.step <= call.step
                or c.target != call.target
                or c.turn >= len(assistant)
            ):
                continue
            same = c.kind == core.RUN if call.kind == core.RUN else c.kind == core.READ
            j = produced_by.get(assistant[c.turn])
            if same and j is not None and j >= k:
                count += 1
                break
    return count


CHECKS = (
    "requests",
    "deterministic",
    "pairing_preserved",
    "stub_regressions",
    "pruned_prefix_breaks_without_new_stub",
)


def replay(conversation: dict) -> dict:
    checks = dict.fromkeys(CHECKS, 0)
    prune_skips: Counter = Counter()
    totals = {name: [0, 0] for name in POLICIES}
    by_part: Counter = Counter()
    cache_breaks = []
    per_request = []
    funnel = None
    reread = 0
    compactions = 0
    previous_last = None
    for s, segment in enumerate(conversation["segments"]):
        if previous_last is not None and len(segment[0]["messages"]) < len(
            previous_last
        ):
            compactions += 1
        previous_last = segment[-1]["messages"]
        prev_pruned = None
        prev_stubs: set = set()
        first_seen: dict = {}
        pruned = None
        for k, body in enumerate(segment, 1):
            before = size(body)
            outputs = {}
            for name, policy in POLICIES.items():
                out, pruning = core.prune_request(copy.deepcopy(body), policy)
                if name == "default" and pruning is None:
                    prune_skips["context_management"] += 1
                outputs[name] = out
                totals[name][0] += before
                totals[name][1] += before - size(out)
            pruned = outputs["default"]
            again, _ = core.prune_request(copy.deepcopy(body), POLICIES["default"])
            checks["requests"] += 1
            checks["deterministic"] += again == pruned
            msgs = body["messages"]
            pmsgs = pruned["messages"] if isinstance(pruned, dict) else msgs
            checks["pairing_preserved"] += pairing_ok(msgs, pmsgs)
            stubs = stub_positions(pmsgs)
            new = sorted(stubs - prev_stubs)
            checks["stub_regressions"] += bool(prev_stubs - stubs)
            if prev_pruned is not None:
                d = first_diff(prev_pruned, pmsgs[: len(prev_pruned)])
                if d is not None and (not new or d < new[0][0]):
                    checks["pruned_prefix_breaks_without_new_stub"] += 1
            for position in new:
                first_seen.setdefault(position, k)
            if new:
                cache_breaks.append(
                    dict(
                        segment=s + 1,
                        request=k,
                        message=new[0][0],
                        new_stubs=len(new),
                        prefix_bytes_kept=size(msgs[: new[0][0]]),
                    )
                )
            by_part += parts(body)
            per_request.append(
                dict(
                    segment=s + 1,
                    request=k,
                    messages=len(msgs),
                    bytes_before=before,
                    bytes_removed=before - size(pruned),
                )
            )
            prev_pruned, prev_stubs = pmsgs, stubs
        seg_funnel = drop_outs(segment[-1], pruned, POLICIES["default"])
        if funnel is None:
            funnel = seg_funnel
        else:
            for key in ("results", "bytes"):
                for fate in DROP_OUTS:
                    funnel[key][fate] += seg_funnel[key][fate]
            for key in (
                "stubs_from_errored_superseder",
                "superseders_skipped_as_errored",
            ):
                funnel[key] += seg_funnel[key]
        reread += rereads(segment, first_seen)
    requests = sum(len(s) for s in conversation["segments"])
    ratios = {n: (r / b if b else 0.0) for n, (b, r) in totals.items()}
    total = sum(v for k, v in by_part.items() if not k.startswith("tool:"))
    return dict(
        role=conversation["role"],
        agent=conversation["agent"],
        main_by_request_count=conversation.get("main_by_count", False),
        requests=requests,
        long=conversation["role"] == "main" and requests >= LONG_SESSION_REQUESTS,
        segments=len(conversation["segments"]),
        compactions=compactions,
        raw_prefix_breaks=len(conversation["segments"]) - 1,
        input_bytes=totals["default"][0],
        bytes_removed=totals["default"][1],
        ratio=ratios["default"],
        sensitivity={n: ratios[n] for n in POLICIES if n != "default"},
        bytes_by_part={k: v for k, v in by_part.items() if not k.startswith("tool:")},
        share_by_part={
            k: (v / total if total else 0.0)
            for k, v in by_part.items()
            if not k.startswith("tool:")
        },
        tool_result_bytes_by_tool={
            k[5:]: v for k, v in sorted(by_part.items()) if k.startswith("tool:")
        },
        drop_outs=funnel,
        cache_breaks=cache_breaks,
        stubbed_targets_read_again=reread,
        checks=checks,
        prune_skips=dict(prune_skips),
        per_request=per_request,
    )


def report(root: Path) -> dict:
    sessions = []
    skipped: Counter = Counter()
    for name, run in load_runs(root).items():
        conversations, run_skipped = group(run)
        skipped += run_skipped
        for conversation in conversations:
            sessions.append(dict(run=name, **replay(conversation)))
    long = [s for s in sessions if s["long"]]
    ratios = [s["ratio"] for s in long]
    median = statistics.median(ratios) if ratios else None
    return dict(
        policy_version=core.PrunePolicy().policy_version,
        gate=dict(
            long_session_requests=LONG_SESSION_REQUESTS,
            minimum_long_sessions=MIN_LONG_SESSIONS,
            threshold=GATE_RATIO,
            long_sessions=len(long),
            long_session_ratios=ratios,
            median_ratio=median,
            passed=None if len(long) < MIN_LONG_SESSIONS else median >= GATE_RATIO,
        ),
        requests_not_replayed=dict(skipped),
        sessions=sessions,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("recordings", type=Path, help="directory of request bodies")
    parser.add_argument("--output", type=Path, help="write JSON here, not stdout")
    args = parser.parse_args(argv)
    if not args.recordings.is_dir():
        parser.error(f"not a directory: {args.recordings}")
    text = json.dumps(report(args.recordings), indent=1)
    if args.output:
        args.output.write_text(text + "\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
