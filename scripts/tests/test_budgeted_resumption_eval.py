# SPDX-License-Identifier: AGPL-3.0-or-later
"""Checks for the separate, frozen budgeted-resumption protocol."""

import copy
import importlib.util
import json
from pathlib import Path
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import session_context_retrieval_eval as legacy  # noqa: E402


def module():
    path = SCRIPTS / "budgeted_resumption_eval.py"
    assert path.exists(), "The separate budgeted resumption driver is missing"
    spec = importlib.util.spec_from_file_location("budgeted_resumption_eval", path)
    result = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = result
    spec.loader.exec_module(result)
    return result


def rows():
    return [
        {
            **slot,
            "session_id": f"fresh-{index}",
            "status": "settled",
            "measurement_valid": True,
            "behavior_pass": True,
            "constraint_pass": True,
            "context_bytes": 0 if slot["arm"] == "A" else 500,
            "coding_usage": {
                "input": 1000,
                "output": 100,
                "cache_read": 250,
                "cache_write": None,
                "unknown_requests": 0,
            },
            "selector_usage": {
                "input_tokens": 200 if slot["arm"] == "D" else 0,
                "output_tokens": 10 if slot["arm"] == "D" else 0,
            },
        }
        for index, slot in enumerate(module().run_order())
    ]


def test_frozen_matrix_has_each_profile_arm_and_repetition_once():
    run = module()
    order = run.run_order()
    assert len(order) == 36
    assert {(r["profile"], r["arm"], r["repetition"]) for r in order} == {
        (profile, arm, repetition)
        for profile in run.PROFILES
        for arm in "ABCD"
        for repetition in (1, 2, 3)
    }
    assert run.run_order() == order
    assert [r["arm"] for r in order[:4]] != [r["arm"] for r in order[4:8]]


def test_summary_counts_selector_and_keeps_cache_out_of_total_input():
    result = module().summarize(rows())
    assert result["experiment_complete"] is True
    assert result["arms"]["D"]["total_input_tokens"] == 10800
    assert result["arms"]["D"]["total_output_tokens"] == 990
    assert result["arms"]["D"]["coding_cache_read_tokens"] == 2250
    assert result["arms"]["D"]["coding_cache_write_tokens"] is None
    assert result["observed_jev_input_reduction"] is False
    assert result["jev_token_savings_supported"] is False
    assert result["total_cost_usd"] is None


def test_unknown_usage_prevents_savings_claim_without_discarding_run():
    records = rows()
    next(r for r in records if r["arm"] == "D")["selector_usage"]["input_tokens"] = None
    result = module().summarize(records)
    assert result["arms"]["D"]["runs"] == 9
    assert result["arms"]["D"]["total_input_tokens"] is None
    assert result["observed_jev_input_reduction"] is None
    assert result["experiment_complete"] is False


def test_incomplete_matrix_and_duplicate_sessions_cannot_complete():
    run = module()
    records = rows()
    assert run.summarize(records[:-1])["experiment_complete"] is False
    records[1]["session_id"] = records[0]["session_id"]
    assert run.summarize(records)["experiment_complete"] is False
    assert run.summarize(rows() + [rows()[0]])["experiment_complete"] is False


def test_clean_negative_quality_result_is_a_completed_experiment():
    records = rows()
    next(r for r in records if r["arm"] == "D")["constraint_pass"] = False
    result = module().summarize(records)
    assert result["experiment_complete"] is True
    assert result["jev_preserves_quality"] is False


def test_lower_input_without_correctness_or_output_accounting_is_not_savings():
    records = rows()
    for row in records:
        if row["arm"] == "D":
            row["coding_usage"]["input"] = 100
    records[-1]["constraint_pass"] = False
    result = module().summarize(records)
    assert result["observed_jev_input_reduction"] is True
    assert result["jev_token_savings_supported"] is False
    next(r for r in records if r["arm"] == "D")["coding_usage"]["output"] = None
    result = module().summarize(records)
    assert result["experiment_complete"] is False
    assert result["jev_token_savings_supported"] is None


def test_lower_input_with_higher_total_tokens_is_not_savings():
    records = rows()
    for row in records:
        if row["arm"] == "D":
            row["coding_usage"].update(input=100, output=5000)
    result = module().summarize(records)
    assert result["observed_jev_input_reduction"] is True
    assert result["jev_token_savings_supported"] is False


def test_failed_measurement_is_retained_and_invalidates_completion():
    records = rows()
    row = next(r for r in records if r["arm"] == "D")
    row.update(status="jev_http_error", measurement_valid=False)
    result = module().summarize(records)
    assert result["arms"]["D"]["runs"] == 9
    assert result["arms"]["D"]["invalid_measurements"] == 1
    assert result["experiment_complete"] is False


def test_same_prompt_wrapper_for_every_nonempty_history():
    run = module()
    visible = "Finish the preserved task."
    for text in ('{"items":[]}', '{"input_messages":[]}'):
        prompt = run.continuation_prompt(visible, text)
        assert prompt.startswith(visible)
        assert prompt.endswith(text)
        assert "historical data" in prompt
    assert run.continuation_prompt(visible, "") == run.continuation_prompt(visible, "")


def test_private_key_file_and_environment_are_not_logged(tmp_path, monkeypatch):
    run = module()
    key = tmp_path / "key"
    key.write_text("fixture-token-for-tests-only\n")
    key.chmod(0o600)
    assert run.load_jev_key(key) == "fixture-token-for-tests-only"
    key.chmod(0o644)
    with pytest.raises(legacy.EvaluationError, match="private_jev_key_required"):
        run.load_jev_key(key)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with pytest.raises(legacy.EvaluationError, match="jev_key_unavailable"):
        run.load_jev_key(None)


def test_changed_source_history_and_workspace_are_rejected(tmp_path):
    run = module()
    protocol = {"fixture": "frozen"}
    (tmp_path / "freeze.json").write_bytes(legacy.encoded(protocol))
    for profile in run.PROFILES:
        directory = tmp_path / profile
        directory.mkdir()
        legacy.initialize_task(directory / "snapshot", run.FIXTURES / "workspace")
        history = {"input_messages": [], "output_messages": []}
        gold = {"constraint": [], "failure": [], "distractor": []}
        manifest = {
            "source_session_id": f"source-{profile}",
            "final_call_id": f"call-{profile}",
            "profile": profile,
            "workspace": legacy.workspace_identity(directory / "snapshot"),
            "history_sha256": legacy.digest(legacy.encoded(history)),
            "gold_sha256": legacy.digest(legacy.encoded(gold)),
            "status": "captured",
        }
        for name, value in (
            ("source.json", manifest),
            ("full-history.json", history),
            ("gold.json", gold),
        ):
            (directory / name).write_bytes(legacy.encoded(value))
    assert set(run.load_sources(tmp_path, protocol)) == set(run.PROFILES)
    (tmp_path / "required/full-history.json").write_text("{}")
    with pytest.raises(legacy.EvaluationError, match="source_changed"):
        run.load_sources(tmp_path, protocol)


def test_freeze_excludes_keys_but_detects_runtime_and_source_changes(
    monkeypatch, tmp_path
):
    run = module()
    tree = tmp_path / "fixtures"
    tree.mkdir()
    (tree / "case.txt").write_text("original")
    selector = tmp_path / "selector.py"
    selector.write_text("selector-v1")
    monkeypatch.setattr(run, "FIXTURES", tree)
    monkeypatch.setattr(run, "SELECTOR_PATH", selector)
    config = {
        "model": legacy.MODEL,
        "agent_image": "sha256:" + "a" * 64,
        "gate_image": "sha256:" + "b" * 64,
        "gateway_url": "http://localhost:4000/v1",
        "api_url": "http://localhost:8000",
        "operator_api_url": "http://localhost:8000",
        "gateway_token": "never-in-freeze",
        "operator_token": "never-in-freeze",
        "retrieval_token": "never-in-freeze",
    }
    runtime = tmp_path / "runtime.json"
    runtime.write_text("{}")
    config["runtime_identity_path"] = runtime
    first = run.protocol_identity(config)
    assert "never-in-freeze" not in json.dumps(first)
    changed = copy.deepcopy(config)
    changed["agent_image"] = "sha256:" + "c" * 64
    assert run.protocol_identity(changed) != first
    (tree / "case.txt").write_text("modified")
    assert run.protocol_identity(config) != first


def test_runtime_evidence_is_bound_without_copying_private_content(
    tmp_path, monkeypatch
):
    run = module()
    selector = tmp_path / "selector.py"
    selector.write_text("selector")
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    monkeypatch.setattr(run, "SELECTOR_PATH", selector)
    monkeypatch.setattr(run, "FIXTURES", fixtures)
    runtime = tmp_path / "runtime.json"
    runtime.write_text('{"backend_model_sha256":"first-identity"}')
    config = {
        name: name
        for name in (
            "model",
            "agent_image",
            "gate_image",
            "gateway_url",
            "api_url",
            "operator_api_url",
        )
    }
    config["runtime_identity_path"] = runtime
    before = run.protocol_identity(config)
    assert before["runtime_identity_sha256"] == legacy.digest(runtime.read_bytes())
    assert "first-identity" not in json.dumps(before)
    runtime.write_text('{"backend_model_sha256":"second-identity"}')
    assert run.protocol_identity(config) != before


@pytest.mark.parametrize("text_array", [False, True])
def test_delivery_checks_both_forwarded_request_and_captured_fact(tmp_path, text_array):
    run = module()
    gate = tmp_path / "gate"
    gate.mkdir()
    prompt = run.continuation_prompt("task", '{"items":[]}')
    (gate / "1.meta.json").write_text(
        json.dumps({"id": "1", "route": "model", "started_unix": 0})
    )
    content = [{"type": "text", "text": prompt}] if text_array else prompt
    request = {"messages": [{"role": "user", "content": content}]}
    (gate / "1.request.json").write_text(json.dumps(request))
    history = {
        "input_messages": [
            {"role": "user", "parts": [{"type": "text", "content": prompt}]}
        ],
        "output_messages": [],
    }
    run.verify_context_delivery(prompt, [history], tmp_path)
    if text_array:
        for invalid in (
            [{"type": "text", "text": prompt + "altered"}],
            [*content, {"type": "text", "text": "extra"}],
            [{"type": "text", "text": prompt, "extra": True}],
        ):
            request["messages"][0]["content"] = invalid
            (gate / "1.request.json").write_text(json.dumps(request))
            with pytest.raises(legacy.EvaluationError, match="context_not_forwarded"):
                run.verify_context_delivery(prompt, [history], tmp_path)
        request["messages"][0]["content"] = content
        (gate / "1.request.json").write_text(json.dumps(request))
    history["input_messages"][0]["parts"][0]["content"] = "counterfeit"
    with pytest.raises(legacy.EvaluationError, match="context_not_captured"):
        run.verify_context_delivery(prompt, [history], tmp_path)
    request["messages"][0]["content"] = "counterfeit"
    (gate / "1.request.json").write_text(json.dumps(request))
    with pytest.raises(legacy.EvaluationError, match="context_not_forwarded"):
        run.verify_context_delivery(prompt, [history], tmp_path)


def test_inference_failure_does_not_become_free_zero_usage():
    records = rows()
    record = next(r for r in records if r["arm"] == "D")
    record["coding_usage"] = {"input": None, "output": None, "unknown_requests": 1}
    record["measurement_valid"] = False
    result = module().summarize(records)
    assert result["arms"]["D"]["total_input_tokens"] is None
    assert result["observed_jev_input_reduction"] is None


def test_selector_failure_preserves_billed_usage_without_launching_coding(
    tmp_path, monkeypatch
):
    import budgeted_context_selection as selector

    run = module()
    source = tmp_path / "source"
    source.mkdir()
    legacy.initialize_task(source / "snapshot", run.FIXTURES / "workspace")
    records = tmp_path / "records"
    records.mkdir()
    metrics = {
        "usage": {"input_tokens": 123, "output_tokens": 0},
        "evidence": {"attempted_calls": 5},
        "jev": {"attempted_calls": 1},
    }

    def fail(*args, **kwargs):
        output = args[5]
        assert not output.exists()
        output.mkdir()
        (output / "selection.json").write_text(
            json.dumps({"status": "source_changed", "metrics": metrics})
        )
        raise selector.SelectionError("source_changed")

    def no_coding(*args, **kwargs):
        pytest.fail("A refused selection must not launch the coding agent")

    monkeypatch.setattr(selector, "select_context", fail)
    monkeypatch.setattr(legacy, "isolated_agent", no_coding)
    manifest = {
        "source_session_id": "source",
        "final_call_id": "call",
        "history_sha256": "a" * 64,
        "quarantine_revision": 0,
        "workspace": legacy.workspace_identity(source / "snapshot"),
    }
    row = run.continuation(
        {},
        source,
        manifest,
        {"profile": "required", "arm": "D", "repetition": 1},
        records,
        "test-key",
    )
    assert row["status"] == "source_changed"
    assert row["selector_usage"] == metrics["usage"]
    assert row["selection_metrics"] == metrics
    assert row["coding_model_calls"] == 0
    assert row["measurement_valid"] is False
    assert json.loads((records / "run.json").read_bytes()) == row


def test_runtime_record_contains_no_foreign_experiment_metadata(monkeypatch):
    run = module()
    probe = {
        "schema_version": 1,
        "model": legacy.MODEL,
        "harness": legacy.PI_VERSION,
        "runtime_versions": [legacy.PI_VERSION, "v24.21.0", "Python 3.12.14"],
        "agent_image": "sha256:a",
        "gate_image": "sha256:b",
        "temperature": 0,
        "output_limit": 2048,
        "context_window": 16384,
        "provider_seed": None,
        "task": "invoice",
        "fixture_hashes": {"invoice.py": "foreign-hash"},
        "controller_sha256": "legacy-controller",
        "run_order": [["A", "B", "C"]],
    }
    monkeypatch.setattr(legacy, "freeze", lambda config: probe)
    result = run.runtime_metadata({})
    assert result["runtime_versions"] == probe["runtime_versions"]
    assert result["agent_image"] == probe["agent_image"]
    assert (
        not {"task", "fixture_hashes", "controller_sha256", "run_order"} & result.keys()
    )
