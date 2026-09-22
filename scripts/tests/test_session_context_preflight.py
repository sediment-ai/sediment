# SPDX-License-Identifier: AGPL-3.0-or-later
"""Native preflight evidence is distinct from continuation benefit."""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys

import httpx
import pytest

SCRIPT = Path(__file__).parents[1] / "session_context_retrieval_eval.py"


def load():
    spec = importlib.util.spec_from_file_location("native_preflight", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def cycle(names=("read", "edit", "bash")):
    events, histories, prior = [], [], []
    for index, name in enumerate(names):
        identifier = f"call-{index}"
        arguments = {
            "sediment_retrieve_context": {"query": "previous task"},
            "read": {"path": "service.json"},
            "edit": {
                "path": "service.json",
                "edits": [{"oldText": "false", "newText": "true"}],
            },
            "bash": {"command": "python -c 'assert True'"},
        }[name]
        result = f"result-{index}"
        part = {
            "type": "tool_call",
            "id": identifier,
            "name": name,
            "arguments": arguments,
        }
        events.extend(
            [
                {
                    "type": "tool_execution_start",
                    "toolName": name,
                    "toolCallId": identifier,
                    "args": arguments,
                },
                {
                    "type": "tool_execution_end",
                    "toolCallId": identifier,
                    "isError": False,
                    "result": {"content": [{"type": "text", "text": result}]},
                },
            ]
        )
        histories.append(
            {
                "input_messages": deepcopy(prior),
                "output_messages": [{"role": "assistant", "parts": [part]}],
            }
        )
        prior.extend(
            [
                {"role": "assistant", "parts": [part]},
                {
                    "role": "tool",
                    "parts": [
                        {
                            "type": "tool_call_response",
                            "id": identifier,
                            "result": result,
                        }
                    ],
                },
            ]
        )
    histories.append(
        {
            "input_messages": prior,
            "output_messages": [
                {
                    "role": "assistant",
                    "parts": [{"type": "text", "content": "finished"}],
                }
            ],
        }
    )
    return events, histories


def test_native_cycle_requires_matched_calls_and_consumed_results():
    mod = load()
    events, histories = cycle()
    result = mod.preflight_tool_cycles(events, histories, ("read", "edit", "bash"))
    assert [item["name"] for item in result] == ["read", "edit", "bash"]
    assert all(item["consumed_at"] > item["called_at"] for item in result)


def test_json_read_result_preserves_exact_value_and_raw_whitespace():
    mod = load()
    events, histories = cycle(("read",))
    raw = '{ "value": 9007199254740993, "enabled": true }\n'
    events[1]["result"]["content"][0]["text"] = raw
    part = histories[1]["input_messages"][-1]["parts"][0]
    part["result"] = json.loads(raw)
    assert mod.preflight_tool_cycles(events, histories, ("read",))[0]["text"] == raw
    part["result"]["enabled"] = 1
    with pytest.raises(mod.EvaluationError, match="native_cycle_unverified"):
        mod.preflight_tool_cycles(events, histories, ("read",))


@pytest.mark.parametrize(
    "defect",
    [
        "printed",
        "error",
        "missing_result",
        "altered_result",
        "wrong_args",
        "same_turn",
        "reordered",
        "duplicate_id",
    ],
)
def test_native_cycle_refuses_incomplete_or_fabricated_trajectory(defect):
    mod = load()
    events, histories = cycle()
    if defect == "printed":
        events = []
        histories = [
            {
                "input_messages": [],
                "output_messages": [
                    {
                        "role": "assistant",
                        "parts": [
                            {
                                "type": "text",
                                "content": 'read[ARGS]{"path":"service.json"}',
                            }
                        ],
                    }
                ],
            }
        ]
    elif defect == "error":
        events[1]["isError"] = True
    elif defect == "missing_result":
        histories[-1]["input_messages"][-1]["parts"] = []
    elif defect == "altered_result":
        histories[-1]["input_messages"][-1]["parts"][0]["result"] += " truncated"
    elif defect == "wrong_args":
        events[0]["args"] = {"path": "elsewhere"}
    elif defect == "same_turn":
        histories[0]["output_messages"].extend(histories[1]["output_messages"])
        histories[1]["output_messages"] = []
    elif defect == "reordered":
        events, histories = cycle(("edit", "read", "bash"))
    else:
        events[2]["toolCallId"] = events[0]["toolCallId"]
    with pytest.raises(mod.EvaluationError, match="native_cycle_unverified"):
        mod.preflight_tool_cycles(events, histories, ("read", "edit", "bash"))


def test_independent_json_check_requires_exact_canary_and_regular_bounded_file(
    tmp_path,
):
    mod = load()
    path = tmp_path / "service.json"
    expected = {"enabled": True, "canary": "keep-this"}
    path.write_text(json.dumps(expected))
    assert mod.preflight_workspace_valid(tmp_path, "keep-this")
    path.write_text(json.dumps({**expected, "enabled": 1}))
    assert not mod.preflight_workspace_valid(tmp_path, "keep-this")
    path.write_text(json.dumps({**expected, "canary": "changed"}))
    assert not mod.preflight_workspace_valid(tmp_path, "keep-this")
    path.unlink()
    elsewhere = tmp_path / "other.json"
    elsewhere.write_text(json.dumps(expected))
    path.symlink_to(elsewhere)
    assert not mod.preflight_workspace_valid(tmp_path, "keep-this")
    path.unlink()
    path.write_text(" " * 17000)
    assert not mod.preflight_workspace_valid(tmp_path, "keep-this")


def selection():
    return {
        "schema_version": 1,
        "policy_version": 1,
        "source_session_id": "real-source",
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
                        "inference_call_id": "fact",
                        "side": "input",
                        "message_index": 0,
                        "part_index": 0,
                    },
                    "observed_at": "2026-09-21T12:00:00Z",
                    "role": "user",
                    "finish_reason": None,
                    "part": {"type": "text", "content": "prior task context"},
                },
            }
        ],
    }


def retrieval_records(mod, directory, value):
    raw = mod.encoded(value).decode()
    events, histories = cycle(("sediment_retrieve_context",))
    events[1]["result"]["content"][0]["text"] = raw
    histories[1]["input_messages"][-1]["parts"][0]["result"] = value
    gate = mod.private_directory(directory / "gate")
    mod.write_bytes(gate / "request.response", raw.encode())
    mod.write_json(
        gate / "request.request.json",
        {"schema_version": 1, "query": "previous task", "max_bytes": 16384},
    )
    mod.write_json(
        gate / "request.meta.json",
        {
            "route": "retrieval",
            "id": "request",
            "complete": True,
            "status": 200,
            "started_unix": 0,
        },
    )
    return events, histories


def test_retrieval_preflight_checks_exact_returned_references(tmp_path, monkeypatch):
    mod = load()
    value = selection()
    events, histories = retrieval_records(mod, tmp_path, value)
    calls = mod.preflight_tool_cycles(events, histories, ("sediment_retrieve_context",))
    read = {
        "schema_version": 1,
        "session_id": "real-source",
        "quarantine_revision": 0,
        "items": [value["items"][0]["evidence"]],
    }
    requests = []

    def operator(_config, route, **kwargs):
        requests.append((route, kwargs))
        return read

    monkeypatch.setattr(mod, "operator_read", operator)
    refs = mod.preflight_retrieval_evidence({}, tmp_path, calls, "real-source")
    assert refs == [value["items"][0]["evidence"]["reference"]]
    assert requests[0][1]["body"]["session_id"] == "real-source"
    read["items"][0] = {
        **read["items"][0],
        "part": {"type": "text", "content": "changed"},
    }
    with pytest.raises(mod.EvaluationError, match="retrieval_evidence_unverified"):
        mod.preflight_retrieval_evidence({}, tmp_path, calls, "real-source")


@pytest.mark.parametrize(
    "defect", ["wrong_source", "empty", "wrong_wire", "failed_wire", "absent_wire"]
)
def test_retrieval_preflight_refuses_missing_or_changed_evidence(
    tmp_path, monkeypatch, defect
):
    mod = load()
    value = selection()
    if defect == "wrong_source":
        value["source_session_id"] = "other-source"
    if defect == "empty":
        value["items"] = []
    events, histories = retrieval_records(mod, tmp_path, value)
    calls = mod.preflight_tool_cycles(events, histories, ("sediment_retrieve_context",))
    if defect == "wrong_wire":
        (tmp_path / "gate/request.response").write_text("{}")
    if defect == "failed_wire":
        p = tmp_path / "gate/request.meta.json"
        r = json.loads(p.read_text())
        r["complete"] = False
        p.write_text(json.dumps(r))
    if defect == "absent_wire":
        (tmp_path / "gate/request.meta.json").unlink()
    monkeypatch.setattr(
        mod,
        "operator_read",
        lambda *a, **kw: pytest.fail("invalid evidence reached operator read"),
    )
    with pytest.raises(mod.EvaluationError, match="retrieval_evidence_unverified"):
        mod.preflight_retrieval_evidence({}, tmp_path, calls, "real-source")


def test_preflight_verdict_never_equates_coding_only_with_full_pass():
    mod = load()
    runs = [{"status": "passed", "session_id": str(i)} for i in range(3)]
    result = mod.preflight_verdict(runs, None)
    assert result["status"] == "coding_verified" and not result["passed"]
    assert result["coding_verified"] and result["retrieval_verified"] is None
    fourth = {"status": "passed", "session_id": "retrieval"}
    assert mod.preflight_verdict(runs, fourth)["passed"]
    fourth["session_id"] = "0"
    assert not mod.preflight_verdict(runs, fourth)["passed"]
    runs[0]["status"] = "gate_health_unavailable"
    assert mod.preflight_verdict(runs, None)["status"] == "failed"


def test_wire_request_must_contain_complete_result_bytes(tmp_path):
    mod = load()
    gate = mod.private_directory(tmp_path / "gate")
    rows = [{"id": "model0"}, {"id": "model1"}]
    cycles = [{"id": "tool", "text": '{"value":9007199254740993}', "consumed_at": 1}]
    request = {
        "messages": [
            {"role": "tool", "tool_call_id": "tool", "content": cycles[0]["text"]}
        ]
    }
    path = gate / "model1.request.json"
    mod.write_json(path, request)
    mod.preflight_request_results(tmp_path, rows, cycles)
    request["messages"][0]["content"] = '{"value":9007199254740992}'
    path.write_text(json.dumps(request))
    with pytest.raises(mod.EvaluationError, match="native_result_not_forwarded"):
        mod.preflight_request_results(tmp_path, rows, cycles)


@pytest.mark.parametrize(
    "defect",
    [None, "missing_health", "missing_record", "failed_request", "session_change"],
)
def test_preflight_traffic_requires_final_health_and_complete_requests(
    tmp_path, defect
):
    mod = load()
    gate = mod.private_directory(tmp_path / "gate")
    budget = mod.AttemptBudget()
    budget.reserve("model")
    if defect != "missing_health":
        mod.write_json(
            tmp_path / "gate-health.json",
            {"ready": True, "stopped": None, "budget": budget.snapshot()},
        )
    if defect != "missing_record":
        mod.write_json(
            gate / "model.meta.json",
            {
                "id": "model",
                "route": "model",
                "started_unix": 0,
                "complete": defect != "failed_request",
                "status": 200,
                "session_id": "other" if defect == "session_change" else "actual",
            },
        )
    if defect:
        with pytest.raises(mod.EvaluationError):
            mod.preflight_traffic(tmp_path, "actual")
    else:
        assert len(mod.preflight_traffic(tmp_path, "actual")) == 1


def test_preflight_runs_three_distinct_cycles_and_optional_real_source(
    tmp_path, monkeypatch
):
    mod = load()
    monkeypatch.setattr(mod, "freeze", lambda config: {"model": mod.MODEL})
    called = []

    def run(config, records, source_session=None):
        called.append(source_session)
        return {"status": "passed", "session_id": str(len(called))}

    monkeypatch.setattr(mod, "preflight_cycle", run)
    coding = mod.run_preflight({}, mod.private_directory(tmp_path / "coding"))
    assert called == [None, None, None]
    assert coding["status"] == "coding_verified"
    monkeypatch.setattr(mod, "preflight_source", lambda config, source: "actual-source")
    called.clear()
    full = mod.run_preflight(
        {}, mod.private_directory(tmp_path / "full"), tmp_path / "accepted"
    )
    assert called == [None, None, None, "actual-source"]
    assert full["passed"]


@pytest.mark.parametrize(
    "defect", [None, "failed_source", "changed_history", "binding"]
)
def test_preflight_source_requires_accepted_files_and_actual_retrieval_binding(
    tmp_path, monkeypatch, defect
):
    mod = load()
    history, gold = {"input_messages": [], "output_messages": []}, {"constraint": []}
    mod.write_json(tmp_path / "full-history.json", history)
    mod.write_json(tmp_path / "gold.json", gold)
    mod.write_json(
        tmp_path / "source.json",
        {
            "source_session_id": "accepted-source",
            "status": "failed" if defect == "failed_source" else "captured",
            "history_sha256": mod.digest(mod.encoded(history)),
            "gold_sha256": mod.digest(mod.encoded(gold)),
        },
    )
    if defect == "changed_history":
        (tmp_path / "full-history.json").write_text("{}")
    requests = []

    def identity(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "authority": "retrieval",
                "source_session_id": "other"
                if defect == "binding"
                else "accepted-source",
            },
        )

    client = httpx.Client
    monkeypatch.setattr(
        mod.httpx,
        "Client",
        lambda **kwargs: client(transport=httpx.MockTransport(identity), **kwargs),
    )
    config = {
        "operator_api_url": "http://127.0.0.1:8011",
        "retrieval_token": "test-value",
    }
    if defect:
        with pytest.raises(mod.EvaluationError, match="preflight_source_unverified"):
            mod.preflight_source(config, tmp_path)
        assert len(requests) == (1 if defect == "binding" else 0)
    else:
        assert mod.preflight_source(config, tmp_path) == "accepted-source"
        assert requests[0].url.path == "/v1/me"
        assert requests[0].headers["Authorization"] == "Bearer test-value"


def test_preflight_failure_is_retained_without_retrying_cycles(tmp_path, monkeypatch):
    mod = load()
    monkeypatch.setattr(mod, "freeze", lambda config: {"model": mod.MODEL})
    calls = []

    def failed_cycle(config, records, source_session=None):
        calls.append(records)
        return {"status": "native_cycle_unverified", "session_id": str(len(calls))}

    monkeypatch.setattr(mod, "preflight_cycle", failed_cycle)
    result = mod.run_preflight({}, mod.private_directory(tmp_path / "failed"))
    assert len(calls) == 3
    assert not result["passed"]
    assert result["status"] == "failed"
    assert all(r["status"] == "native_cycle_unverified" for r in result["coding_runs"])
    assert json.loads((tmp_path / "failed/preflight.json").read_bytes()) == result


@pytest.mark.parametrize(
    "status,expected", [("coding_verified", 0), ("passed", 0), ("failed", 1)]
)
def test_preflight_cli_reports_honest_exit_status(
    tmp_path, monkeypatch, status, expected
):
    mod = load()
    monkeypatch.setattr(mod, "load_config", lambda _: {})
    monkeypatch.setattr(
        mod,
        "run_preflight",
        lambda *args: {"status": status, "passed": status == "passed"},
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "preflight",
            "--config",
            "private.json",
            "--output",
            str(tmp_path / "result"),
        ],
    )
    assert mod.main() == expected
