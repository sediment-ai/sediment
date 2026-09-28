# SPDX-License-Identifier: MIT
"""The Responses adapter for Codex CLI's exec calls.

Recorded fixtures come first: three Codex CLI 0.158.0 Sessions in
``fixtures/codex``, frozen request bodies in the order Codex sent them.
Hand-written items, built by ``handwritten_session``, cover only the near-miss
and edge cases that the recordings don't contain.
"""

from __future__ import annotations

import copy
import http.client
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from erode import litellm_hook
from erode.core import (
    STUB_PREFIX,
    PrunePolicy,
    _statements,
    prune_input,
    prune_request,
)
from erode.proxy import make_server

RECORDED = Path(__file__).resolve().parent / "fixtures" / "codex"
SESSIONS = (1, 2, 3)
EAGER = PrunePolicy(policy_version="test", min_new_bytes=0, min_result_bytes=0)
BIG = 5000


def recorded(session: int) -> list[bytes]:
    folder = RECORDED / f"session-{session}"
    return [path.read_bytes() for path in sorted(folder.glob("*_v1_responses.json"))]


def stubbed(items: list[dict]) -> list[tuple[str, int]]:
    """(call_id, part index) of every stubbed output part."""
    return [
        (item["call_id"], k)
        for item in items
        if item.get("type") == "custom_tool_call_output"
        and isinstance(item.get("output"), list)
        for k, part in enumerate(item["output"])
        if str(part.get("text")).startswith(STUB_PREFIX)
    ]


def compact(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


# Recorded Sessions.


@pytest.mark.parametrize("session", SESSIONS)
def test_recorded_requests_pass_through_below_the_threshold(session) -> None:
    for body in recorded(session):
        request = json.loads(body)
        pruned, report = prune_request(request, PrunePolicy())
        assert pruned is request
        assert report["stubbed_results"] == 0


@pytest.mark.parametrize("session", SESSIONS)
def test_recorded_pruning_changes_only_stubbed_part_text(session) -> None:
    body = recorded(session)[-1]
    request = json.loads(body)
    pruned, report = prune_request(request, EAGER)
    assert report["stubbed_results"] == len(stubbed(pruned["input"])) >= 3
    # Every byte outside the stubbed part texts is Codex's own.
    replaced = body
    for old, new in zip(request["input"], pruned["input"], strict=True):
        if old is new:
            continue
        assert old["type"] == "custom_tool_call_output"
        assert {k: v for k, v in old.items() if k != "output"} == {
            k: v for k, v in new.items() if k != "output"
        }
        for a, b in zip(old["output"], new["output"], strict=True):
            assert a.keys() == b.keys() and a["type"] == b["type"]
            if a["text"] != b["text"]:
                assert b["text"].startswith(STUB_PREFIX)
                replaced = replaced.replace(compact(a["text"]), compact(b["text"]))
    assert compact(pruned) == replaced
    # Calls, messages, and encrypted reasoning are untouched.
    for old, new in zip(request["input"], pruned["input"], strict=True):
        if old["type"] != "custom_tool_call_output":
            assert new is old


def test_recorded_stubs_name_their_superseders() -> None:
    request = json.loads(recorded(1)[-1])
    pruned, _ = prune_request(request, EAGER)
    texts = [
        part["text"]
        for item in pruned["input"]
        if item["type"] == "custom_tool_call_output"
        for part in item["output"]
        if part["text"].startswith(STUB_PREFIX)
    ]
    root = "/tmp/erode-codex-recording/demo/parcel"
    assert f"(apply_patch of {root}/billing.py); read it again" in texts[0]
    assert f"(apply_patch of {root}/labels.py); read it again" in texts[1]
    assert all("(exec_command of python -m pytest -q)" in t for t in texts[2:])


@pytest.mark.parametrize("session", SESSIONS)
@pytest.mark.parametrize("policy", [EAGER, PrunePolicy("test", 0, 1500)])
def test_recorded_determinism_idempotence_and_monotonic_prefix(session, policy) -> None:
    previous: list[dict] | None = None
    for body in recorded(session):
        items = json.loads(body)["input"]
        before = copy.deepcopy(items)
        pruned, _ = prune_input(items, policy)
        assert items == before
        assert prune_input(copy.deepcopy(before), policy)[0] == pruned
        assert prune_input(pruned, policy)[0] == pruned
        if previous is not None:
            earlier, now = set(stubbed(previous)), set(stubbed(pruned))
            assert earlier <= now
            if now == earlier:
                assert pruned[: len(previous)] == previous
        previous = pruned


def test_recorded_opaque_calls_are_never_stubbed() -> None:
    # Session 1's second call uses Promise.allSettled.
    request = json.loads(recorded(1)[-1])
    calls = [i for i in request["input"] if i["type"] == "custom_tool_call"]
    assert "Promise.allSettled" in calls[1]["input"]
    pruned, _ = prune_request(request, EAGER)
    assert calls[1]["call_id"] not in {
        call_id for call_id, _ in stubbed(pruned["input"])
    }


def test_previous_response_id_passes_through_and_is_counted() -> None:
    request = {**json.loads(recorded(1)[-1]), "previous_response_id": "resp_1"}
    pruned, report = prune_request(request, EAGER)
    assert pruned is request
    assert report["skipped"] == "previous_response_id"
    assert report["stubbed_results"] == 0


# Hand-written edge cases.


def cmd(command: str, *, exit_code: int = 0, size: int = BIG, **extra):
    arguments = ",".join(
        [f"cmd:{json.dumps(command)}"]
        + [f"{k}:{json.dumps(v)}" for k, v in extra.items()]
    )
    result = json.dumps({"exit_code": exit_code, "output": "x" * size})
    return f"text(await tools.exec_command({{{arguments}}}));", result


def patch(*headers: str, ok: bool = True):
    body = "*** Begin Patch\n" + "\n".join(headers) + "\n@@\n-a\n+b\n*** End Patch"
    result = "{}" if ok else "apply_patch verification failed"
    return f"text(await tools.apply_patch({json.dumps(body)}));", result


def handwritten_session(turns: list, cwd: str | None = "/work") -> list[dict]:
    """Codex-shaped items: one exec call per turn, then two filler turns."""
    context = "<environment_context>\n  <cwd>%s</cwd>\n</environment_context>" % cwd
    items: list[dict] = []
    if cwd is not None:
        items.append(
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": context}],
            }
        )
    items.append(
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "Fix it."}],
        }
    )
    filler = [[cmd(f"echo {n}", size=10)] for n in range(2)]
    for n, statements in enumerate([*turns, *filler]):
        source = statements if isinstance(statements, str) else None
        if source is None:
            source = "\n".join(line for line, _ in statements) + "\n"
            results = [result for _, result in statements]
        else:
            results = [json.dumps({"exit_code": 0, "output": "x" * BIG})]
        items.append(
            {
                "type": "custom_tool_call",
                "status": "completed",
                "call_id": f"call_{n}",
                "name": "exec",
                "input": source,
            }
        )
        header = {"type": "input_text", "text": "Script completed\nOutput:\n"}
        parts = [{"type": "input_text", "text": r} for r in results]
        items.append(
            {
                "type": "custom_tool_call_output",
                "call_id": f"call_{n}",
                "output": [header, *parts],
            }
        )
    return items


def stubbed_calls(turns: list, cwd: str | None = "/work") -> list[str]:
    pruned, _ = prune_input(handwritten_session(turns, cwd), PrunePolicy())
    return sorted({call_id for call_id, _ in stubbed(pruned)})


@pytest.mark.parametrize(
    "form",
    [
        "cat src/a.py",
        "nl -ba src/a.py",
        "head src/a.py",
        "tail src/a.py",
        "cat /work/src/a.py",
    ],
)
def test_handwritten_read_forms_are_superseded_by_a_patch(form) -> None:
    turns = [[cmd(form)], [patch("*** Update File: /work/src/a.py")]]
    assert stubbed_calls(turns) == ["call_0"]


@pytest.mark.parametrize(
    "form",
    [
        "cat src/a.py | head",
        "cat src/a.py > out.txt",
        "cat src/a.py && ls",
        "cat src/a.py src/b.py",
        "cat src/*.py",
        "cat -n src/a.py",
        "cat $FILE",
    ],
)
def test_handwritten_near_miss_forms_stay_runs(form) -> None:
    turns = [[cmd(form)], [patch("*** Update File: /work/src/a.py")]]
    assert stubbed_calls(turns) == []
    # As runs, they are superseded only by the identical command.
    assert stubbed_calls([[cmd(form)], [cmd(form)]]) == ["call_0"]


@pytest.mark.parametrize(
    "source",
    [
        'const r = await tools.exec_command({cmd:"cat src/a.py"});\ntext(r);\n',
        "text(await tools.exec_command({cmd:`cat src/a.py`}));\n",
        "text(await tools.exec_command({cmd:'cat src/a.py'}));\n",
        'text(await tools.exec_command({cmd:"cat src/\\x61.py"}));\n',
        'text(await tools.exec_command({cmd:"cat src/a.py",cmd:"ls"}));\n',
        "text(await tools.exec_command({max_output_tokens:4000}));\n",
        'text(await tools.web_search({q:"x"}));\n',
    ],
)
def test_handwritten_grammar_failures_make_a_call_opaque(source) -> None:
    turns = [source, [patch("*** Update File: /work/src/a.py")]]
    assert stubbed_calls(turns) == []


def test_handwritten_part_count_mismatch_makes_a_call_opaque() -> None:
    items = handwritten_session(
        [[cmd("cat a.py")], [patch("*** Update File: /work/a.py")]]
    )
    items[3]["output"].append({"type": "input_text", "text": "extra"})
    assert stubbed(prune_input(items, PrunePolicy())[0]) == []


@pytest.mark.parametrize(
    "header",
    [
        "*** Add File: /work/a.py",
        "*** Update File: /work/a.py",
        "*** Delete File: /work/a.py",
        "*** Update File: /work/old.py\n*** Move to: /work/a.py",
        "*** Update File: a.py",
    ],
)
def test_handwritten_patch_paths_supersede_reads(header) -> None:
    assert stubbed_calls([[cmd("cat a.py")], [patch(header)]]) == ["call_0"]


def test_handwritten_failed_patch_supersedes_nothing() -> None:
    turns = [[cmd("cat a.py")], [patch("*** Update File: /work/a.py", ok=False)]]
    assert stubbed_calls(turns) == []


def test_handwritten_relative_paths_need_a_cwd() -> None:
    turns = [[cmd("cat a.py")], [patch("*** Update File: /work/a.py")]]
    assert stubbed_calls(turns, cwd=None) == []
    relative = [[cmd("cat a.py")], [patch("*** Update File: a.py")]]
    assert stubbed_calls(relative, cwd=None) == ["call_0"]


def test_handwritten_failed_read_is_never_stubbed_and_never_supersedes() -> None:
    failed = [[cmd("cat a.py", exit_code=1)], [patch("*** Update File: /work/a.py")]]
    assert stubbed_calls(failed) == []
    assert stubbed_calls([[cmd("cat a.py")], [cmd("cat a.py", exit_code=1)]]) == []
    # A failing test run is an ordinary result, superseded by the same run.
    runs = [[cmd("pytest -q", exit_code=1)], [cmd("pytest -q", exit_code=1)]]
    assert stubbed_calls(runs) == ["call_0"]


def test_handwritten_partial_reads_and_output_limits() -> None:
    sed = "sed -n '1,40p' a.py"
    assert stubbed_calls([[cmd(sed)], [cmd("sed -n '41,80p' a.py")]]) == []
    assert stubbed_calls([[cmd(sed)], [cmd("cat a.py")]]) == []
    assert stubbed_calls([[cmd(sed)], [cmd(sed)]]) == ["call_0"]
    assert stubbed_calls([[cmd("cat a.py")], [cmd("nl -ba a.py")]]) == ["call_0"]
    limited = [[cmd("cat a.py", max_output_tokens=4000)], [cmd("cat a.py")]]
    assert stubbed_calls(limited) == []


def test_handwritten_bundled_call_stubs_only_superseded_parts() -> None:
    turns = [
        [cmd("cat a.py"), cmd("cat b.py"), patch("*** Update File: /work/c.py")],
        [patch("*** Update File: /work/a.py")],
    ]
    pruned, _ = prune_input(handwritten_session(turns), PrunePolicy())
    assert stubbed(pruned) == [("call_0", 1)]
    output = pruned[3]["output"]
    assert output[0]["text"].startswith("Script completed")
    assert json.loads(output[2]["text"])["exit_code"] == 0
    assert output[3]["text"] == "{}"


def test_hook_prunes_a_responses_request_in_place() -> None:
    turns = [[cmd("cat a.py")], [patch("*** Update File: /work/a.py")]]
    data = {"model": "m", "input": handwritten_session(turns)}
    report = litellm_hook.apply(data)
    assert report["stubbed_results"] == 1
    assert stubbed(data["input"]) == [("call_0", 1)]
    assert "messages" not in data


def test_handwritten_extra_line_makes_a_call_opaque() -> None:
    line, result = cmd("cat a.py")
    items = handwritten_session(
        [[(line, result)], [patch("*** Update File: /work/a.py")]]
    )
    # One recognized statement and one other line; parts still count one.
    items[2]["input"] = "const n = 1;\n" + items[2]["input"]
    assert stubbed(prune_input(items, PrunePolicy())[0]) == []


def test_handwritten_glob_characters_never_make_a_read() -> None:
    # The shell expands [a].py to a.py, so the text isn't the path it reads.
    turns = [[cmd("cat [a].py")], [patch("*** Update File: /work/[a].py")]]
    assert stubbed_calls(turns) == []


def test_proxy_prunes_the_responses_route_and_streams_events() -> None:
    events = [b"event: response.created\ndata: {}\n\n", b"event: done\ndata: {}\n\n"]
    received: list[bytes] = []

    class Upstream(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args) -> None:
            pass

        def do_POST(self) -> None:
            received.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response_only(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for event in events:
                self.wfile.write(b"%x\r\n%s\r\n" % (len(event), event))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    proxy = make_server(f"http://127.0.0.1:{upstream.server_address[1]}", port=0)
    for server in (upstream, proxy):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        turns = [[cmd("cat a.py")], [patch("*** Update File: /work/a.py")]]
        request = {"model": "m", "store": False, "input": handwritten_session(turns)}
        body = compact(request)
        agent = http.client.HTTPConnection(*proxy.server_address, timeout=10)
        agent.request("POST", "/v1/responses", body=body)
        response = agent.getresponse()
        assert response.read() == b"".join(events)
        expected, report = prune_request(request, PrunePolicy())
        assert report["stubbed_results"] == 1
        assert received == [compact(expected)]
        agent.close()
    finally:
        for server in (upstream, proxy):
            server.shutdown()
            server.server_close()


@pytest.mark.parametrize(
    "source",
    [
        "text(await tools.apply_patch(" + "text(await tools.apply_patch(a" * 40_000,
        "text(await tools.exec_command({a" + " " * 1_000_000 + "x}));",
        "text(await tools.exec_command({" + "$" * 1_000_000 + "}));",
        "text(await tools.exec_command({" + "a:1," * 250_000 + "}));",
    ],
    ids=["nested-heads", "spaces", "dollars", "many-keys"],
)
def test_handwritten_hostile_statements_parse_in_linear_time(source) -> None:
    # Agent-controlled input: parsing about 1 MB must stay far under a second.
    started = time.perf_counter()
    _statements(source)
    assert time.perf_counter() - started < 1.0
