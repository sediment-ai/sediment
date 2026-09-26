# SPDX-License-Identifier: AGPL-3.0-or-later
"""Evaluation checks do not substitute for the recorded live comparison."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).parents[1] / "session_context_retrieval_eval.py"


def load():
    assert SCRIPT.exists(), "the evaluation controller is missing"
    spec = importlib.util.spec_from_file_location("context_eval", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_freeze_accepts_locked_evaluation_harness_and_rejects_other_versions(
    monkeypatch,
):
    mod = load()
    lock = json.loads((SCRIPT.parents[1] / "shims/pi/package-lock.json").read_text())
    version = lock["packages"]["node_modules/@earendil-works/pi-coding-agent"][
        "version"
    ]
    config = {"agent_image": "agent", "gate_image": "gate"}
    monkeypatch.setattr(
        mod,
        "command",
        lambda *a, **kw: f"{version}\nv24.21.0\nPython 3.12.14\n".encode(),
    )
    assert mod.freeze(config)["harness"] == version
    monkeypatch.setattr(mod, "command", lambda *a, **kw: b"0.0.0\n")
    with pytest.raises(mod.EvaluationError, match="unsupported_harness"):
        mod.freeze(config)


def test_admission_reserves_attempts_atomically_including_failed_forwards():
    mod = load()
    budget = mod.AttemptBudget()
    with ThreadPoolExecutor(max_workers=16) as pool:
        admitted = list(pool.map(lambda _: budget.reserve("retrieval"), range(40)))
    assert sum(admitted) == 4
    assert budget.snapshot()["retrieval"] == {"forwarded": 4, "declined": 36}
    # A failed upstream request never refunds an admitted attempt.
    assert all(budget.reserve("model") for _ in range(12))
    assert not budget.reserve("model")
    with pytest.raises(mod.EvaluationError, match="unknown_route"):
        budget.reserve("operator")


def test_rpc_framing_preserves_unicode_separators_and_bounds_partial_lines():
    mod = load()
    decoder = mod.JsonLines(limit=64)
    assert decoder.feed(b'{"text":"hello') == []
    assert decoder.feed('\u2028world"}\n'.encode()) == [{"text": "hello\u2028world"}]
    with pytest.raises(mod.EvaluationError, match="rpc_record_limit"):
        decoder.feed(b"x" * 65)


def test_private_records_refuse_existing_paths_and_symlinks(tmp_path):
    mod = load()
    private = mod.private_directory(tmp_path / "records")
    path = private / "result.json"
    mod.write_json(path, {"status": "failed"})
    assert path.stat().st_mode & 0o777 == 0o600
    assert private.stat().st_mode & 0o777 == 0o700
    with pytest.raises(FileExistsError):
        mod.write_json(path, {})
    link = private / "link"
    link.symlink_to(path)
    with pytest.raises(FileExistsError):
        mod.write_json(link, {})
    with pytest.raises(FileExistsError):
        mod.private_directory(private)


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True
    ).stdout


def test_snapshot_keeps_index_worktree_and_untracked_bytes(tmp_path):
    mod = load()
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@example.test")
    (repo / "code.py").write_text("committed\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "fixture")
    (repo / "code.py").write_text("staged\n")
    git(repo, "add", ".")
    (repo / "code.py").write_text("working\n")
    (repo / "untracked.bin").write_bytes(b"\x00\xff")
    (repo / "untracked.bin").chmod(0o755)
    identity = mod.copy_workspace(repo, tmp_path / "copy")
    assert mod.workspace_identity(repo) == identity
    assert mod.workspace_identity(tmp_path / "copy") == identity
    assert git(tmp_path / "copy", "show", ":code.py") == b"staged\n"
    assert (tmp_path / "copy" / "code.py").read_text() == "working\n"
    assert (tmp_path / "copy" / "untracked.bin").read_bytes() == b"\x00\xff"
    (repo / "external").symlink_to(tmp_path / "secret")
    with pytest.raises(mod.EvaluationError, match="workspace_link"):
        mod.copy_workspace(repo, tmp_path / "unsafe")


def manifest_and_items():
    reference = {
        "inference_call_id": "fact",
        "side": "input",
        "message_index": 0,
        "part_index": 0,
    }
    manifest = {
        "messages": [
            {
                "side": "input",
                "message_index": 0,
                "role": "user",
                "finish_reason": None,
                "parts": [{"type": "text", "reference": reference}],
            },
            {
                "side": "output",
                "message_index": 0,
                "role": "assistant",
                "finish_reason": "stop",
                "parts": [],
            },
        ]
    }
    items = [
        {
            "reference": reference,
            "role": "user",
            "finish_reason": None,
            "part": {"type": "text", "content": "constraint"},
        }
    ]
    return manifest, items


def test_history_keeps_empty_messages_and_refuses_missing_or_duplicate_parts():
    mod = load()
    manifest, items = manifest_and_items()
    history = mod.assemble_history(manifest, items)
    assert history["input_messages"][0]["parts"] == [items[0]["part"]]
    assert history["output_messages"] == [
        {"role": "assistant", "finish_reason": "stop", "parts": []}
    ]
    with pytest.raises(mod.EvaluationError, match="incomplete_history"):
        mod.assemble_history(manifest, [])
    with pytest.raises(mod.EvaluationError, match="incomplete_history"):
        mod.assemble_history(manifest, items * 2)


def run_record(arm, repetition):
    return {
        "arm": arm,
        "repetition": repetition,
        "session_id": f"{arm}-{repetition}",
        "status": "settled",
        "behavior_pass": True,
        "constraint_pass": arm != "A",
        "verification_error": None,
        "retrieval_calls": 1 if arm == "C" else 0,
        "retrieved_constraint": arm == "C",
        "retrieved_failure": arm == "C",
        "trajectory_verified": arm == "C",
        "elapsed_seconds": 1,
        "usage": {"input": 10, "output": 5, "cache_read": None, "cache_write": None},
    }


def test_verdict_requires_all_nine_runs_and_all_three_c_trajectories():
    mod = load()
    records = [run_record(arm, rep) for rep in range(1, 4) for arm in "ABC"]
    assert mod.evaluate(records)["benefit_demonstrated"]
    assert len(mod.evaluate(records)["comparisons"]) == 3
    assert not mod.evaluate(records[:-1])["complete"]
    records[-1]["retrieved_failure"] = False
    assert not mod.evaluate(records)["benefit_demonstrated"]
    records[-1]["retrieved_failure"] = True
    records[-1]["status"] = "budget_exhausted"
    assert not mod.evaluate(records)["benefit_demonstrated"]


def test_equal_a_success_proves_no_benefit_and_duplicate_sessions_refuse_completion():
    mod = load()
    records = [run_record(arm, rep) for rep in range(1, 4) for arm in "ABC"]
    for record in records:
        record["constraint_pass"] = True
    assert mod.evaluate(records)["retrieval_feasible"]
    assert not mod.evaluate(records)["benefit_demonstrated"]
    records[1]["session_id"] = records[0]["session_id"]
    assert not mod.evaluate(records)["complete"]


def test_validator_detects_round_at_end_and_tampered_visible_tests(tmp_path):
    mod = load()
    task = tmp_path / "task"
    task.mkdir()
    (task / "invoice_csv.py").write_text(
        "import csv, io\nfrom decimal import Decimal\n"
        "def invoice_total(text):\n"
        "    rows = csv.DictReader(io.StringIO(text))\n"
        '    return format(sum((Decimal(r["amount"]) for r in rows), Decimal(0)), ".2f")\n'
    )
    check = subprocess.run(
        [sys.executable, str(mod.FIXTURES / "verify.py"), str(task)],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"]},
    )
    result = json.loads(check.stdout)
    assert result["behavior_pass"]
    assert not result["constraint_pass"]
    (task / "test_invoice_csv.py").write_text("# agent removed visible assertions\n")
    check = subprocess.run(
        [sys.executable, str(mod.FIXTURES / "verify.py"), str(task)],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"]},
    )
    assert not json.loads(check.stdout)["constraint_pass"]


def test_gate_forwards_exact_bytes_limits_calls_and_never_exposes_broad_credentials(
    tmp_path,
):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    import httpx

    mod = load()
    received = []
    exact = b'{"integer":9007199254740993,"string":"\\ud800\\u0000"}'

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((self.path, self.headers.get("Authorization"), body))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(exact)))
            self.end_headers()
            self.wfile.write(exact)

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{upstream.server_port}"
    config = {
        "gateway_url": base + "/v1",
        "api_url": base,
        "gateway_token": "upstream-only",
        "retrieval_token": "scoped-only",
        "agent_token": "run-only",
        "model": mod.MODEL,
        "arm": "C",
    }
    records = mod.private_directory(tmp_path / "gate")
    gate = mod.make_gate_server(config, records, port=0)
    threading.Thread(target=gate.serve_forever, daemon=True).start()
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{gate.server_port}", trust_env=False
        ) as client:
            request = {"schema_version": 1, "query": "invoice constraint"}
            for _ in range(4):
                result = client.post(
                    "/query/context",
                    json=request,
                    headers={"Authorization": "Bearer run-only"},
                )
                assert result.content == exact
            result = client.post(
                "/query/context",
                json=request,
                headers={"Authorization": "Bearer run-only"},
            )
            assert result.status_code == 429
            assert len(received) == 4
            result = client.post(
                "/v1/facts", json={}, headers={"Authorization": "Bearer run-only"}
            )
            assert result.status_code == 404
            assert all(auth == "Bearer scoped-only" for _, auth, _ in received)
            assert b"upstream-only" not in result.content
    finally:
        gate.shutdown()
        gate.server_close()
        upstream.shutdown()
        upstream.server_close()


def test_gate_disabled_retrieval_cannot_use_model_credential(tmp_path):
    import threading
    import httpx

    mod = load()
    config = {
        "gateway_url": "http://127.0.0.1:1/v1",
        "api_url": "http://127.0.0.1:1",
        "gateway_token": "upstream-only",
        "retrieval_token": "scoped-only",
        "agent_token": "run-only",
        "model": mod.MODEL,
        "arm": "A",
    }
    gate = mod.make_gate_server(
        config, mod.private_directory(tmp_path / "gate"), port=0
    )
    threading.Thread(target=gate.serve_forever, daemon=True).start()
    try:
        result = httpx.post(
            f"http://127.0.0.1:{gate.server_port}/query/context",
            json={},
            headers={"Authorization": "Bearer run-only"},
        )
        assert result.status_code == 403
        assert gate.state.budget.snapshot()["retrieval"]["forwarded"] == 0
    finally:
        gate.shutdown()
        gate.server_close()


def test_rpc_waits_for_settled_and_closes_normally(tmp_path):
    mod = load()
    fake = tmp_path / "fake_rpc.py"
    fake.write_text("""import json,sys,time
for line in sys.stdin:
    c=json.loads(line)
    print(json.dumps({"type":"response","id":c["id"],"success":True,"data":{"sessionId":"actual-session"}}),flush=True)
    if c["type"]=="prompt":
        print(json.dumps({"type":"agent_end"}),flush=True)
        time.sleep(.05)
        print(json.dumps({"type":"agent_settled"}),flush=True)
""")
    record = mod.private_directory(tmp_path / "record")
    with mod.RpcProcess([sys.executable, str(fake)], record, timeout=5) as rpc:
        assert rpc.request("get_state")["sessionId"] == "actual-session"
        rpc.request("prompt", message="task")
        assert rpc.wait_settled()["type"] == "agent_settled"
    assert rpc.process.returncode == 0
    assert b"agent_settled" in (record / "rpc.jsonl").read_bytes()


def test_rpc_timeout_leaves_explicit_failure_and_no_live_process(tmp_path):
    mod = load()
    record = mod.private_directory(tmp_path / "record")
    with pytest.raises(mod.EvaluationError, match="run_deadline"):
        with mod.RpcProcess(
            [sys.executable, "-c", "import time;time.sleep(10)"], record, timeout=0.1
        ) as rpc:
            rpc.request("get_state")
    assert rpc.process.poll() is not None


def test_agent_plan_mounts_only_workspace_and_fresh_home_without_operator_secret(
    tmp_path,
):
    mod = load()
    config = {
        "agent_image": "sha256:" + "a" * 64,
        "model": mod.MODEL,
        "operator_token": "must-not-reach-agent",
    }
    args = mod.agent_command(
        config,
        "gate-owned",
        "agent-owned",
        tmp_path / "workspace",
        tmp_path / "home",
        "C",
    )
    text = " ".join(args)
    assert "must-not-reach-agent" not in text
    assert "container:gate-owned" in text
    mounts = [args[i + 1] for i, value in enumerate(args[:-1]) if value == "--mount"]
    assert len(mounts) == 2
    assert all("workspace" in item or "home" in item for item in mounts)
    assert "docker.sock" not in text
    assert "--read-only" in args


def test_config_rejects_cloud_routes_and_mutable_image_tags(tmp_path):
    mod = load()
    config = {
        "schema_version": 1,
        "agent_image": "sha256:" + "a" * 64,
        "gate_image": "sha256:" + "b" * 64,
        "gateway_url": "http://host.docker.internal:4011/v1",
        "api_url": "http://host.docker.internal:8011",
        "operator_api_url": "http://127.0.0.1:8011",
        "gateway_token": "g" * 32,
        "operator_token": "o" * 32,
        "retrieval_token": "r" * 32,
        "model": mod.MODEL,
    }
    path = tmp_path / "config.json"
    mod.write_json(path, config)
    assert mod.load_config(path)["model"] == mod.MODEL
    config["agent_image"] = "agent:latest"
    path.write_bytes(mod.encoded(config))
    with pytest.raises(mod.EvaluationError, match="unpinned_image"):
        mod.load_config(path)
    config["agent_image"] = "sha256:" + "a" * 64
    config["gateway_url"] = "https://api.example.com/v1"
    path.write_bytes(mod.encoded(config))
    with pytest.raises(mod.EvaluationError, match="nonlocal_endpoint"):
        mod.load_config(path)


def test_complete_prefix_refuses_missing_earlier_messages_without_multiplying_history():
    mod = load()
    user = {
        "role": "user",
        "parts": [{"type": "text", "content": "goal"}],
        "finish_reason": None,
    }
    assistant = {
        "role": "assistant",
        "parts": [{"type": "text", "content": "inspected"}],
        "finish_reason": "stop",
    }
    one = {"input_messages": [user], "output_messages": [assistant]}
    two = {
        "input_messages": [user, {**assistant, "finish_reason": None}],
        "output_messages": [assistant],
    }
    assert mod.verify_prefix([one, two]) == two
    with pytest.raises(mod.EvaluationError, match="capture_prefix_incomplete"):
        mod.verify_prefix([one, {**two, "input_messages": [user]}])


def test_usage_uses_observed_gateway_fields_and_keeps_missing_cache_unknown(tmp_path):
    mod = load()
    records = mod.private_directory(tmp_path / "gate")
    for number, payload in enumerate(
        [
            {"usage": {"prompt_tokens": 11, "completion_tokens": 3}},
            {"usage": {"prompt_tokens": 19, "completion_tokens": 4}},
        ]
    ):
        mod.write_bytes(
            records / f"{number}.response",
            b"data: " + mod.encoded(payload) + b"\n\ndata: [DONE]\n\n",
        )
        mod.write_json(
            records / f"{number}.meta.json",
            {
                "route": "model",
                "id": str(number),
                "complete": True,
                "status": 200,
                "started_unix": number,
            },
        )
    usage = mod.observed_usage(mod.gate_records(records), records)
    assert usage["input"] == 30
    assert usage["output"] == 7
    assert usage["cache_read"] is None and usage["cache_write"] is None
    mod.write_json(
        records / "failed.meta.json",
        {"route": "model", "id": "failed", "complete": False, "started_unix": 3},
    )
    usage = mod.observed_usage(mod.gate_records(records), records)
    assert usage["unknown_requests"] == 1
    assert usage["input"] is None


def test_no_history_transport_failure_does_not_count_as_retrieval_benefit():
    mod = load()
    records = [run_record(arm, rep) for rep in range(1, 4) for arm in "ABC"]
    for record in records:
        if record["arm"] == "A":
            record["status"] = "upstream_failure"
    assert not mod.evaluate(records)["benefit_demonstrated"]


def test_validator_failure_is_not_evidence_of_a_failed_constraint():
    mod = load()
    records = [run_record(arm, rep) for rep in range(1, 4) for arm in "ABC"]
    for record in records:
        record["verification_error"] = (
            "verification_failed" if record["arm"] == "A" else None
        )
    assert mod.evaluate(records)["retrieval_feasible"]
    assert not mod.evaluate(records)["benefit_demonstrated"]


def test_generated_home_never_contains_upstream_credentials(tmp_path):
    mod = load()
    config = {
        "retrieval_token": "upstream-retrieval-never-agent",
        "gateway_token": "upstream-gateway-never-agent",
        "operator_token": "operator-never-agent",
    }
    home = tmp_path / "home"
    mod.prepare_home(home, config, "run-ingress-only", "C")
    contents = b"".join(p.read_bytes() for p in home.rglob("*") if p.is_file())
    assert b"run-ingress-only" in contents
    assert all(secret.encode() not in contents for secret in config.values())


def test_source_gold_requires_captured_tool_results_not_commands():
    mod = load()
    parts = [
        {"type": "text", "content": "quantize each amount with ROUND_DOWN"},
        {
            "type": "tool_call_response",
            "id": "failure",
            "result": "ValueError: too many values to unpack",
        },
        {
            "type": "tool_call_response",
            "id": "other",
            "result": "RuntimeError: optional_formatter unavailable",
        },
    ]
    assert mod.source_gold([{"part": part} for part in parts])["failure"]
    parts[1]["type"] = "tool_call"
    with pytest.raises(mod.EvaluationError, match="source_evidence_missing"):
        mod.source_gold([{"part": part} for part in parts])


@pytest.mark.parametrize("work_tool", ["edit", "write", "bash"])
def test_c_trajectory_requires_real_tool_result_then_subsequent_task_work(work_tool):
    mod = load()
    c = {"type": "text", "content": "quantize each amount with ROUND_DOWN"}
    f = {
        "type": "tool_call_response",
        "id": "old-fail",
        "result": "ValueError: too many values to unpack",
    }
    gold = {"constraint": [{"part": c}], "failure": [{"part": f}]}
    selection = {
        "schema_version": 1,
        "source_session_id": "source",
        "items": [
            {"evidence": {"part": p, "reference": {"inference_call_id": "source-call"}}}
            for p in (c, f)
        ],
    }
    retrieve = {
        "type": "tool_call",
        "id": "retrieve",
        "name": "sediment_retrieve_context",
        "arguments": {"query": "invoice constraints"},
    }
    response = {"type": "tool_call_response", "id": "retrieve", "result": selection}
    edit = {
        "type": "tool_call",
        "id": "edit",
        "name": work_tool,
        "arguments": {"path": "invoice_csv.py"},
    }
    history = {
        "input_messages": [{"parts": [retrieve, response, edit]}],
        "output_messages": [],
    }
    assert mod.captured_trajectory(history, gold, "source")
    history["input_messages"][0]["parts"] = [retrieve, edit, response]
    assert not mod.captured_trajectory(history, gold, "source")
    history["input_messages"][0]["parts"] = [
        retrieve,
        {"type": "text", "content": mod.encoded(selection).decode()},
        edit,
    ]
    assert not mod.captured_trajectory(history, gold, "source")
    events = [
        {
            "type": "tool_execution_start",
            "toolName": "sediment_retrieve_context",
            "toolCallId": "r",
            "args": {"query": "invoice constraints"},
        },
        {"type": "tool_execution_start", "toolName": work_tool, "toolCallId": "w"},
        {
            "type": "tool_execution_end",
            "toolCallId": "r",
            "result": {
                "content": [{"type": "text", "text": mod.encoded(selection).decode()}]
            },
        },
    ]
    assert not mod.retrieval_observations(events, gold, "source")["later_native_work"]
    events.append(
        {"type": "tool_execution_start", "toolName": work_tool, "toolCallId": "e"}
    )
    assert mod.retrieval_observations(events, gold, "source")["later_native_work"]


def test_final_health_requires_explicit_available_evidence(tmp_path):
    mod = load()
    with pytest.raises(mod.EvaluationError, match="gate_health_unavailable"):
        mod.final_gate_health(tmp_path)
    path = tmp_path / "gate-health.json"
    for value in (
        {"ready": False, "stopped": "gate_health_unavailable", "budget": None},
        {"ready": True, "stopped": None},
        {"ready": True, "stopped": None, "budget": {}},
        {"ready": True, "budget": mod.AttemptBudget().snapshot()},
        {"ready": True, "stopped": None, "budget": {"model": {}, "retrieval": {}}},
        [],
    ):
        path.write_bytes(mod.encoded(value))
        with pytest.raises(mod.EvaluationError, match="gate_health_unavailable"):
            mod.final_gate_health(tmp_path)
    health = {"ready": True, "stopped": None, "budget": mod.AttemptBudget().snapshot()}
    path.write_bytes(mod.encoded(health))
    assert mod.final_gate_health(tmp_path) == health


def test_cleanup_records_closed_health_failure_and_preserves_original_error(
    tmp_path, monkeypatch
):
    from contextlib import nullcontext

    mod = load()
    records = mod.private_directory(tmp_path / "records")
    checks = 0

    def health(_name):
        nonlocal checks
        checks += 1
        if checks == 1:
            return {"ready": True}
        raise subprocess.TimeoutExpired("private command details", 5)

    removed = []
    monkeypatch.setattr(mod, "gate_health", health)
    monkeypatch.setattr(mod, "command", lambda *args, **kwargs: b"")
    monkeypatch.setattr(mod, "RpcProcess", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(
        mod.subprocess, "run", lambda args, **kwargs: removed.append(args)
    )
    config = dict.fromkeys(
        (
            "agent_image",
            "gate_image",
            "gateway_url",
            "gateway_token",
            "api_url",
            "retrieval_token",
            "model",
        ),
        "unused",
    )
    with pytest.raises(mod.EvaluationError, match="source_evidence_missing"):
        with mod.isolated_agent(config, records, tmp_path / "workspace", "source"):
            raise mod.EvaluationError("source_evidence_missing")
    raw = (records / "gate-health.json").read_bytes()
    value = json.loads(raw)
    assert value == {
        "ready": False,
        "stopped": "gate_health_unavailable",
        "budget": None,
        "error": "timeout",
    }
    assert b"private command details" not in raw
    assert len(removed) == 2
