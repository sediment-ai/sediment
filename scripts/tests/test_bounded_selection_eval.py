# SPDX-License-Identifier: AGPL-3.0-or-later
"""Checks for the separate bounded JEV selection runner (K versus J1)."""

from __future__ import annotations

from collections import Counter
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import sys
import threading

import httpx
import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import session_context_retrieval_eval as legacy  # noqa: E402

REAL_CLIENT = httpx.Client


def module():
    path = SCRIPTS / "bounded_selection_eval.py"
    spec = importlib.util.spec_from_file_location("bounded_selection_eval", path)
    result = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = result
    spec.loader.exec_module(result)
    return result


def test_heldout_order_is_balanced_and_alternates_within_each_task():
    run = module()
    order = run.run_order("heldout")
    assert len(order) == 24 and [s["slot"] for s in order] == list(range(1, 25))
    keys = Counter(
        (s["family"], s["profile"], s["arm"], s["repetition"]) for s in order
    )
    assert set(keys.values()) == {1} and len(keys) == 24
    pairs = [order[i : i + 2] for i in range(0, 24, 2)]
    assert all(
        a["family"] == b["family"] and a["profile"] == b["profile"] for a, b in pairs
    )
    assert Counter(a["arm"] for a, _ in pairs) == {"K": 6, "J1": 6}
    first = {}
    for a, _ in pairs:
        first.setdefault((a["family"], a["profile"]), []).append(a["arm"])
    assert all(len(set(arms)) == 2 for arms in first.values())
    assert len(run.run_order("development")) == 6
    assert run.run_order("heldout") == order


@pytest.mark.parametrize(
    "change,valid",
    [
        ({}, True),
        ({"max_tokens": 1}, True),
        ({"max_tokens": 1500}, True),
        ({"temperature": 0.0}, True),
        ({"max_tokens": 0}, False),
        ({"max_tokens": 2049}, False),
        ({"max_tokens": 2048.0}, False),
        ({"max_tokens": True}, False),
        ({"temperature": False}, False),
        ({"temperature": 0.1}, False),
        ({"stream": False}, False),
        ({"model": "other"}, False),
    ],
)
def test_generation_contract_accepts_the_recorded_adaptive_allowance(change, valid):
    run = module()
    value = {
        "model": "coding-model",
        "temperature": 0,
        "max_tokens": 2048,
        "stream": True,
        "messages": [],
        "tools": [],
        "seed": 7,
    } | change
    if valid:
        settings = run.generation_settings(value, "coding-model")
        assert settings["max_tokens"] == value["max_tokens"]
        assert "messages" not in settings["parameters"]
    else:
        with pytest.raises(legacy.EvaluationError, match="settings_mismatch"):
            run.generation_settings(value, "coding-model")


class Upstream(BaseHTTPRequestHandler):
    status = 200

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.server.requests.append(dict(self.headers))
        body = (
            b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
            b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":2}}'
            b"\n\ndata: [DONE]\n\n"
        )
        self.send_response(self.server.status)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def gate(tmp_path):
    run = module()
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    upstream.requests, upstream.status = [], 200
    config = {
        "gateway_url": f"http://127.0.0.1:{upstream.server_address[1]}/v1",
        "gateway_token": "upstream-secret",
        "model": "coding-model",
        "agent_token": "agent-token",
    }
    records = tmp_path / "records"
    records.mkdir()
    server = run.make_gate_server(config, records, port=0)
    threads = [
        threading.Thread(target=s.serve_forever, daemon=True)
        for s in (upstream, server)
    ]
    for thread in threads:
        thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}"

    def post(**change):
        body = {
            "model": "coding-model",
            "temperature": 0,
            "max_tokens": 2048,
            "stream": True,
            "messages": [{"role": "user", "content": "x"}],
        } | change
        return REAL_CLIENT(trust_env=False).post(
            url + "/v1/chat/completions",
            content=json.dumps(body),
            headers={
                "Authorization": "Bearer agent-token",
                "x-sediment-session": "session-1",
                "Content-Type": "application/json",
            },
        )

    yield run, server, upstream, records, post
    server.shutdown()
    upstream.shutdown()


def test_gate_forwards_recorded_allowance_and_stops_at_the_call_limit(gate):
    run, server, upstream, records, post = gate
    assert post(max_tokens=1500).status_code == 200
    for _ in range(legacy.MODEL_CALL_LIMIT - 1):
        assert post().status_code == 200
    refused = post()
    assert refused.status_code == 429
    assert server.state.stopped == "budget_exhausted"
    assert server.state.budget.snapshot()["model"] == {"forwarded": 12, "declined": 1}
    traffic = legacy.gate_records(records)
    assert len(traffic) == 12 and all(r["complete"] for r in traffic)
    assert sorted(r["sampling"]["max_tokens"] for r in traffic)[0] == 1500
    usage = legacy.observed_usage(traffic, records)
    assert usage["input"] == 120 and usage["unknown_requests"] == 0
    assert all(
        h["Authorization"] == "Bearer upstream-secret" for h in upstream.requests
    )
    summary = run._gate_summary(traffic, records)
    assert summary["clamped_requests"] == 1 and summary["coding_complete"] == 12


@pytest.mark.parametrize(
    "change,reason",
    [({"max_tokens": 4096}, "generation_settings_mismatch"), ({}, "upstream_failure")],
)
def test_gate_rejects_contract_violations_and_upstream_failures(gate, change, reason):
    run, server, upstream, records, post = gate
    if not change:
        upstream.status = 500
    response = post(**change)
    assert response.status_code == 502
    assert server.state.stopped == reason
    assert post().status_code == 429


def test_gate_refuses_other_routes_and_agent_credentials(gate):
    run, server, upstream, records, post = gate
    url = f"http://127.0.0.1:{server.server_address[1]}"
    client = REAL_CLIENT(trust_env=False)
    assert client.post(url + "/query/context", content=b"{}").status_code == 404
    bad = client.post(
        url + "/v1/chat/completions",
        content=b"{}",
        headers={"Authorization": "Bearer upstream-secret"},
    )
    assert bad.status_code == 403 and not upstream.requests


def config(tmp_path):
    runtime = tmp_path / "runtime-identity.json"
    runtime.write_text('{"backend_model_sha256":"first"}')
    return {
        "model": legacy.MODEL,
        "agent_image": "sha256:" + "a" * 64,
        "gate_image": "sha256:" + "b" * 64,
        "gateway_url": "http://host.docker.internal:4011/v1",
        "api_url": "http://127.0.0.1:8011",
        "operator_api_url": "http://127.0.0.1:8011",
        "gateway_token": "g" * 32,
        "operator_token": "o" * 32,
        "retrieval_token": "r" * 32,
        "runtime_identity_path": runtime,
    }


def test_protocol_binds_policy_and_inputs_but_never_credentials(tmp_path, monkeypatch):
    run = module()
    value = config(tmp_path)
    protocol = run.protocol_identity(value, "heldout", "direct")
    text = json.dumps(protocol)
    for secret in ("g" * 32, "o" * 32, "r" * 32):
        assert secret not in text
    assert protocol["policy"]["relevance_min"] == 0.6
    assert protocol["generation"]["max_tokens_ceiling"] == 2048
    assert len(protocol["run_order"]) == 24
    assert "evaluation/verify.py" in protocol["fixture_hashes"]
    assert run.protocol_identity(value, "heldout", "loopback_proxy") != protocol
    assert run.protocol_identity(value, "development", "direct") != protocol
    Path(value["runtime_identity_path"]).write_text('{"backend_model_sha256":"x"}')
    assert run.protocol_identity(value, "heldout", "direct") != protocol


def row(slot, **change):
    base = {
        **slot,
        "status": "settled",
        "session_id": f"session-{slot['slot']}",
        "measured": True,
        "instrument_failure": False,
        "measurement_complete": True,
        "usage_complete": True,
        "behavior_pass": True,
        "constraint_pass": True,
        "coding_usage": {
            "input": 1000,
            "output": 100,
            "cache_read": 0,
            "cache_write": None,
            "unknown_requests": 0,
        },
        "selector_usage": {"input_tokens": 0, "output_tokens": 0},
        "selection": {"decision": "keyword", "context_bytes": 4000, "added": []},
        "latency": {"total_seconds": 60.0},
        "rule_source": "keyword",
    }
    if slot["arm"] == "J1":
        base["coding_usage"] = dict(base["coding_usage"], input=700)
        base["selector_usage"] = {"input_tokens": 150, "output_tokens": 12}
        base["selection"] = {"decision": "jev", "context_bytes": 2500, "added": ["c1"]}
        base["rule_source"] = "jev"
    return copy.deepcopy(base | change)


def test_summary_reports_target_quality_and_subgroups():
    run = module()
    rows = [row(s) for s in run.run_order()]
    result = run.summarize(rows)
    assert result["experiment_complete"] is True
    k, j = result["arms"]["K"], result["arms"]["J1"]
    assert k["total_tokens"] == 12 * 1100 and j["total_tokens"] == 12 * (800 + 162)
    assert result["acceptance"]["token_reduction"] == pytest.approx(
        1 - (12 * 962) / (12 * 1100)
    )
    assert result["acceptance"]["savings_claim_supported"] is True
    assert result["j1_subgroups"]["jev"]["runs"] == 12
    assert result["jev_added_parts"] == {"runs": 12, "total": 12}
    assert all(p["difference"] == -138 for p in result["paired"])
    assert k["coding_cache_write_tokens"] is None


def test_fallbacks_and_quality_failures_block_a_savings_claim():
    run = module()
    rows = [row(s) for s in run.run_order()]
    j1 = [r for r in rows if r["arm"] == "J1"]
    j1[0]["selection"] = {"decision": "fallback", "decision_reason": "jev_http_error"}
    j1[1]["constraint_pass"] = False
    result = run.summarize(rows)
    assert result["j1_subgroups"]["fallback"] == {
        "runs": 1,
        "both_checks": 1,
        "reasons": {"jev_http_error": 1},
    }
    assert result["acceptance"]["j1_passes_all"] is False
    assert result["acceptance"]["token_target_met"] is False
    assert result["acceptance"]["savings_claim_supported"] is False


def test_unknown_usage_or_missing_slots_leave_the_experiment_incomplete():
    run = module()
    rows = [row(s) for s in run.run_order()]
    rows[3]["coding_usage"]["input"] = None
    rows[3]["measurement_complete"] = False
    result = run.summarize(rows)
    assert result["experiment_complete"] is False
    assert result["acceptance"]["token_reduction"] is None
    assert result["failures"] == [{"slot": 4, "status": "settled"}]
    assert run.summarize(rows[:-1])["not_run"] == 1


def fake_matrix(run, monkeypatch, tmp_path, outcomes):
    value = config(tmp_path)
    protocol = run.protocol_identity(value, "heldout", "direct")
    preflight = tmp_path / "preflight"
    preflight.mkdir()
    (preflight / "preflight.json").write_text(
        json.dumps({"passed": True, "protocol": protocol})
    )
    monkeypatch.setattr(
        run,
        "load_sources",
        lambda source, p: {
            f"{f}/{p_}": {"source_session_id": f"src-{f}-{p_}"}
            for f, p_ in run.tasks("heldout")
        },
    )
    monkeypatch.setattr(run, "verify_grant", lambda config, sources: [])
    calls = []

    def fake(config, source, manifest, slot, records, key, transport):
        calls.append(slot["slot"])
        outcome = outcomes.get(slot["slot"], "ok")
        if outcome == "crash":
            raise KeyboardInterrupt
        result = row(slot)
        if outcome == "instrument":
            result.update(
                status="capture_incomplete",
                measured=False,
                instrument_failure=True,
                measurement_complete=False,
            )
        legacy.write_json(records / "run.json", result)
        return result

    monkeypatch.setattr(run, "continuation", fake)
    return value, preflight, calls


def test_interrupted_slot_is_recorded_once_and_never_rerun(tmp_path, monkeypatch):
    run = module()
    value, preflight, calls = fake_matrix(run, monkeypatch, tmp_path, {3: "crash"})
    output = tmp_path / "matrix"
    arguments = (value, tmp_path, output, "key", preflight, None, "direct")
    with pytest.raises(KeyboardInterrupt):
        run.run_matrix(*arguments, task_set="heldout", resume=False)
    assert calls == [1, 2, 3]
    with pytest.raises(FileExistsError):
        run.run_matrix(*arguments, task_set="heldout", resume=False)
    result = run.run_matrix(*arguments, task_set="heldout", resume=True)
    assert calls == [1, 2, 3, *range(4, 25)]
    interrupted = json.loads(
        (output / run.slot_name(run.run_order()[2]) / "run.json").read_bytes()
    )
    assert interrupted["status"] == "interrupted"
    assert result["recorded_runs"] == 24 and result["experiment_complete"] is False
    assert result["failures"] == [{"slot": 3, "status": "interrupted"}]
    again = run.run_matrix(*arguments, task_set="heldout", resume=True)
    assert calls == [1, 2, 3, *range(4, 25)] and again["recorded_runs"] == 24


def test_instrument_failure_stops_the_experiment_and_blocks_resume(
    tmp_path, monkeypatch
):
    run = module()
    value, preflight, calls = fake_matrix(run, monkeypatch, tmp_path, {5: "instrument"})
    output = tmp_path / "matrix"
    arguments = (value, tmp_path, output, "key", preflight, None, "direct")
    result = run.run_matrix(*arguments, task_set="heldout", resume=False)
    assert calls == [1, 2, 3, 4, 5]
    assert result["not_run"] == 19 and result["experiment_complete"] is False
    assert json.loads((output / "stopped.json").read_bytes()) == {
        "slot": 5,
        "status": "capture_incomplete",
    }
    with pytest.raises(legacy.EvaluationError, match="experiment_stopped"):
        run.run_matrix(*arguments, task_set="heldout", resume=True)


def test_changed_protocol_cannot_resume(tmp_path, monkeypatch):
    run = module()
    value, preflight, calls = fake_matrix(run, monkeypatch, tmp_path, {2: "crash"})
    output = tmp_path / "matrix"
    arguments = (value, tmp_path, output, "key", preflight, None, "direct")
    with pytest.raises(KeyboardInterrupt):
        run.run_matrix(*arguments, task_set="heldout", resume=False)
    Path(value["runtime_identity_path"]).write_text('{"changed":true}')
    with pytest.raises(legacy.EvaluationError):
        run.run_matrix(*arguments, task_set="heldout", resume=True)


def test_selection_refusal_never_launches_coding(tmp_path, monkeypatch):
    run = module()
    import bounded_evidence_selection as selector

    source = tmp_path / "source"
    source.mkdir()
    legacy.initialize_task(
        source / "snapshot", run.FIXTURES / "families/event-rollup/workspace"
    )
    records = tmp_path / "records"
    records.mkdir()

    def refuse(*args, **kwargs):
        output = args[5]
        output.mkdir()
        (output / "selection.json").write_text(
            json.dumps(
                {
                    "status": "source_changed",
                    "metrics": {
                        "usage": {"input_tokens": 0, "output_tokens": 0},
                        "evidence": {"attempted_calls": 2},
                        "jev": {"attempted_calls": 0},
                        "selection": {"decision": None},
                    },
                }
            )
        )
        raise selector.BoundedSelectionError("source_changed")

    def no_coding(*args, **kwargs):
        pytest.fail("a refused selection must not launch the coding agent")

    monkeypatch.setattr(selector, "select_evidence", refuse)
    monkeypatch.setattr(run, "isolated_agent", no_coding)
    manifest = {
        "source_session_id": "source",
        "final_call_id": "call",
        "history_sha256": "a" * 64,
        "quarantine_revision": 0,
        "workspace": legacy.workspace_identity(source / "snapshot"),
    }
    slot = {
        "slot": 2,
        "family": "event-rollup",
        "profile": "missing",
        "arm": "J1",
        "repetition": 1,
    }
    result = run.continuation({}, source, manifest, slot, records, "key", None)
    assert result["status"] == "source_changed"
    assert result["coding_launched"] is False and result["instrument_failure"] is True
    assert result["requests"]["evidence_calls"] == 2
    assert json.loads((records / "run.json").read_bytes()) == result


def test_evidence_hits_and_rule_attribution():
    run = module()
    rule = run.labels()["families"]["event-rollup"]["rule"]
    ref = {"inference_call_id": "c", "side": "input", "message_index": 0}
    items = [
        {
            "reference": ref | {"part_index": 0},
            "part": {"type": "text", "content": "x"},
        },
        {
            "reference": ref | {"part_index": 1},
            "part": {"type": "text", "content": "Note: the " + rule},
        },
    ]
    assert run.evidence_hits(items, "event-rollup") == {
        "rule": True,
        "obsolete": False,
        "distractor": False,
    }
    metrics = {"selection": {"decision": "jev", "initial": [ref | {"part_index": 0}]}}
    assert run.rule_source(metrics, items, "event-rollup") == "jev"
    metrics["selection"]["initial"].append(ref | {"part_index": 1})
    assert run.rule_source(metrics, items, "event-rollup") == "initial"
    metrics["selection"]["decision"] = "fallback"
    assert run.rule_source(metrics, items, "event-rollup") == "fallback"
    assert run.rule_source(metrics, items[:1], "event-rollup") is None


def test_capture_reads_are_counted_with_exact_bytes(monkeypatch):
    run = module()

    def handle(request):
        return httpx.Response(200, stream=httpx.ByteStream(b'{"ok":true}'))

    monkeypatch.setattr(
        run.httpx,
        "Client",
        lambda **kwargs: REAL_CLIENT(transport=httpx.MockTransport(handle), **kwargs),
    )
    counter = {"requests": 0, "request_bytes": 0, "response_bytes": 0}
    value = {"operator_api_url": "http://127.0.0.1:1", "operator_token": "t" * 32}
    with run.counted_capture_reads(counter):
        assert legacy.operator_read(value, "/x", body={"a": 1}) == {"ok": True}
        legacy.operator_read(value, "/query/evidence", params={"session_id": "s"})
    assert counter["requests"] == 2 and counter["response_bytes"] == 22
    assert counter["request_bytes"] == len(b'{"a":1}') + len(b"/x") + len(
        b"/query/evidence?session_id=s"
    )
    assert legacy.operator_read is not None


def test_development_probes_are_capped_by_a_persistent_ledger(tmp_path, monkeypatch):
    run = module()
    import bounded_evidence_selection as selector

    dispatched = []

    def fake_probe(catalog, query, key, records, transport=None):
        dispatched.append(records)
        return {
            "status": "passed",
            "metrics": {
                "usage": {"input_tokens": 5, "output_tokens": 1},
                "selection": {"candidates": [], "qualifying": [], "scores": {}},
            },
        }

    monkeypatch.setattr(selector, "probe", fake_probe)
    ledger = tmp_path / "ledger.json"
    for index in range(5):
        out = tmp_path / f"probe-{index}"
        out.mkdir()
        run.development_probes("key", out, ledger, None)
    assert len(dispatched) == run.DEVELOPMENT_PROBE_LIMIT == 12
    entries = json.loads(ledger.read_bytes())
    assert len(entries) == 12 and ledger.stat().st_mode & 0o777 == 0o600


def test_probe_scoring_uses_development_labels_only():
    run = module()
    catalog, query = run.development_catalog("missing")
    assert "legacy scheduler value measured in minutes" not in query
    rule_id = next(
        c["id"]
        for c in catalog["candidates"]
        if "measured in minutes" in c["evidence"]["part"]["content"]
    )
    result = {
        "metrics": {
            "selection": {
                "candidates": [{"id": "c1", "catalog_id": rule_id}],
                "qualifying": ["c1"],
                "scores": {"c1": {"relevant": 0.9}},
            }
        }
    }
    assert run.score_probe(result, catalog, "missing")["agrees"] is True
    assert run.score_probe(result, catalog, "redundant")["agrees"] is False
    result["metrics"]["selection"]["candidates"] = []
    assert run.score_probe(result, catalog, "missing")["agrees"] is None


def test_missing_key_fails_closed_without_printing_secrets(
    tmp_path, monkeypatch, capsys
):
    run = module()
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    monkeypatch.setenv("TYPESAFE_API_KEY", "tsk-should-not-be-used")
    monkeypatch.setattr(
        sys, "argv", ["x", "jev-check", "--output", str(tmp_path / "out")]
    )
    assert run.main() == 1
    printed = capsys.readouterr().out
    assert json.loads(printed) == {
        "status": "failed",
        "reason": "jev_credentials_missing",
    }
    assert "tsk-" not in printed
    monkeypatch.setenv("JEV_API_KEY", "bad key")
    assert run.main() == 1
    assert "jev_credentials_invalid" in capsys.readouterr().out


def test_nonloopback_proxy_is_refused_before_any_request(tmp_path, monkeypatch, capsys):
    run = module()
    monkeypatch.setenv("JEV_API_KEY", "tsk-valid")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "x",
            "jev-check",
            "--output",
            str(tmp_path / "out"),
            "--jev-proxy",
            "http://proxy.example:3128",
        ],
    )
    assert run.main() == 1
    assert "invalid_transport" in capsys.readouterr().out
    assert not (tmp_path / "out").exists()


def fake_continuation(run, monkeypatch, tmp_path, *, stopped=None, calls=3, fail=None):
    """Drive one continuation with recorded gate files and no containers."""
    import bounded_evidence_selection as selector
    import budgeted_resumption_eval as earlier
    from contextlib import contextmanager

    source = tmp_path / "source"
    source.mkdir()
    legacy.initialize_task(
        source / "snapshot", run.FIXTURES / "families/event-rollup/workspace"
    )
    records = tmp_path / "records"
    records.mkdir()
    validated = []

    def select(config, session, call, query, arm, output, **kwargs):
        Path(output).mkdir()
        metrics = selector._metrics(arm)
        metrics["selection"]["decision"] = "keyword"
        legacy.write_json(
            Path(output) / "selection.json", {"status": "selected", "metrics": metrics}
        )
        return selector.base.Selection("", (), metrics, "selected")

    class Rpc:
        deadline = float("inf")

        class process:
            returncode = 0

        def request(self, kind, **fields):
            return {"sessionId": "fresh", "model": {"id": legacy.MODEL}}

        def wait_settled(self):
            return {}

    @contextmanager
    def agent(config, records_, workspace):
        if fail:
            raise legacy.EvaluationError(fail)
        gate = legacy.private_directory(records_ / "gate")
        for index in range(calls):
            legacy.write_json(
                gate / f"{index}.meta.json",
                {
                    "route": "model",
                    "id": str(index),
                    "started_unix": index,
                    "complete": True,
                    "status": 200,
                    "request_bytes": 10,
                    "response_bytes": 20,
                    "sampling": {"max_tokens": 2048},
                },
            )
            (gate / f"{index}.response").write_bytes(
                b'data: {"usage":{"prompt_tokens":5,"completion_tokens":1}}\n'
            )
        yield Rpc()
        declined = 1 if stopped == "budget_exhausted" else 0
        legacy.write_json(
            records_ / "gate-health.json",
            {
                "ready": True,
                "stopped": stopped,
                "budget": {
                    "model": {"forwarded": calls, "declined": declined},
                    "retrieval": {"forwarded": 0, "declined": 0},
                },
            },
        )

    monkeypatch.setattr(selector, "select_evidence", select)
    monkeypatch.setattr(run, "isolated_agent", agent)
    monkeypatch.setattr(legacy, "initialize_rpc", lambda rpc: "fresh")
    monkeypatch.setattr(
        legacy, "read_captured_calls", lambda *a, **k: ([{"x": 1}], [[]])
    )
    monkeypatch.setattr(earlier, "verify_context_delivery", lambda *a: None)

    def validate(config, family, workspace, output):
        validated.append(family)
        return {
            "behavior_pass": True,
            "constraint_pass": False,
            "verification_error": None,
        }

    monkeypatch.setattr(run, "validate_workspace", validate)
    manifest = {
        "source_session_id": "source",
        "final_call_id": "call",
        "history_sha256": "a" * 64,
        "quarantine_revision": 0,
        "workspace": legacy.workspace_identity(source / "snapshot"),
    }
    slot = {
        "slot": 1,
        "family": "event-rollup",
        "profile": "missing",
        "arm": "K",
        "repetition": 1,
    }
    config = {"model": legacy.MODEL}
    row = run.continuation(config, source, manifest, slot, records, None, None)
    return row, validated


def test_continuation_records_complete_measurement(tmp_path, monkeypatch):
    run = module()
    row, validated = fake_continuation(run, monkeypatch, tmp_path)
    assert row["status"] == "settled" and row["measurement_complete"] is True
    assert row["coding_usage"]["input"] == 15 and validated == ["event-rollup"]
    assert row["requests"]["coding_dispatches"] == 3
    assert row["traffic"]["coding_request_bytes"] == 30
    assert row["selector_usage"]["input_tokens"] == 0


def test_proven_call_limit_stop_is_a_measured_unsuccessful_continuation(
    tmp_path, monkeypatch
):
    run = module()
    row, validated = fake_continuation(
        run, monkeypatch, tmp_path, stopped="budget_exhausted", calls=12
    )
    assert row["status"] == "budget_exhausted" and row["measured"] is True
    assert row["instrument_failure"] is False and validated == ["event-rollup"]


def test_unproven_call_limit_stop_is_an_instrument_failure(tmp_path, monkeypatch):
    run = module()
    row, _ = fake_continuation(
        run, monkeypatch, tmp_path, stopped="budget_exhausted", calls=5
    )
    assert row["status"] == "budget_unproven" and row["instrument_failure"] is True


def test_harness_failure_after_launch_still_validates_workspace(tmp_path, monkeypatch):
    run = module()
    row, validated = fake_continuation(
        run, monkeypatch, tmp_path, fail="gate_start_failed"
    )
    assert row["status"] == "gate_start_failed" and row["instrument_failure"] is True
    assert validated == ["event-rollup"] and row["behavior_pass"] is True


def test_jev_check_counts_its_probe_and_verifies_the_answering_model(
    tmp_path, monkeypatch
):
    run = module()
    import bounded_evidence_selection as selector

    monkeypatch.setattr(
        selector,
        "list_models",
        lambda key, records, transport=None: {
            "available": False,
            "models": ["jev-latest", "jev-preview"],
        },
    )
    monkeypatch.setattr(
        selector,
        "probe",
        lambda catalog, query, key, records, transport=None: {
            "status": "passed",
            "metrics": {
                "usage": {"input_tokens": 9, "output_tokens": 1},
                "selection": {"candidates": [], "qualifying": [], "scores": {}},
            },
        },
    )
    ledger = tmp_path / "ledger.json"
    result = run.jev_check("key", tmp_path, None, ledger)
    assert result["status"] == "passed" and result["model_verified"] is True
    assert result["pinned_listed"] is False
    assert len(json.loads(ledger.read_bytes())) == 1
    ledger.write_text(json.dumps([{}] * run.DEVELOPMENT_PROBE_LIMIT))
    assert run.jev_check("key", tmp_path, None, ledger)["status"] == (
        "probe_limit_reached"
    )


@pytest.mark.parametrize(
    "effort,sent,valid",
    [
        (None, None, True),
        ("low", "low", True),
        (None, "low", False),
        ("low", None, False),
        ("low", "high", False),
    ],
)
def test_generation_contract_binds_the_reasoning_effort(effort, sent, valid):
    run = module()
    value = {"model": "m", "temperature": 0, "max_tokens": 2048, "stream": True}
    if sent is not None:
        value["reasoning_effort"] = sent
    if valid:
        settings = run.generation_settings(value, "m", effort)
        assert settings["reasoning_effort"] == sent
    else:
        with pytest.raises(legacy.EvaluationError, match="settings_mismatch"):
            run.generation_settings(value, "m", effort)


def test_reasoning_profile_enables_pi_reasoning_effort(tmp_path, monkeypatch):
    run = module()
    home = tmp_path / "home"
    legacy.prepare_home(home, {}, "token", "A")
    run.enable_reasoning(home / "config/models.json")
    models = json.loads((home / "config/models.json").read_bytes())
    (model,) = models["providers"]["sediment"]["models"]
    assert model["reasoning"] is True
    assert model["compat"]["supportsReasoningEffort"] is True
    assert model["maxTokens"] == 2048 and model["contextWindow"] == 16384
    monkeypatch.setattr(run, "REASONING_EFFORT", "low")
    contract = run.generation_contract()
    assert contract["reasoning_effort"] == "low"
    monkeypatch.setattr(run, "REASONING_EFFORT", None)
    assert run.generation_contract()["reasoning_effort"] is None
