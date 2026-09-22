# SPDX-License-Identifier: AGPL-3.0-or-later
"""Private selection uses real evidence contracts and bounded mock transport."""

from datetime import UTC, datetime
import importlib.util
import json
from pathlib import Path
import sys

import httpx
import pytest
from sediment_core import (
    EvidenceCallMetadata,
    EvidenceMessageSource,
    InferenceCall,
    InferenceMessage,
    TextPart,
    ToolCallResponsePart,
    project_evidence_inventory,
    project_evidence_manifest,
    project_evidence_read,
    encode_evidence_json,
)

SCRIPT = Path(__file__).parents[1] / "budgeted_context_selection.py"


def load():
    assert SCRIPT.exists(), "the budgeted selection consumer must exist"
    spec = importlib.util.spec_from_file_location("budget_selection", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def corpus():
    fact = InferenceCall(
        inference_call_id="final",
        org_id="acme",
        session_id="source",
        gateway_provider="litellm",
        observed_at=datetime(2026, 9, 22, tzinfo=UTC),
        input_messages=[
            InferenceMessage(
                role="user",
                parts=[
                    TextPart(
                        content="  parser constraint: preserve CASE and whitespace\n"
                    ),
                    TextPart(content="unrelated optional linter failure"),
                ],
            )
        ],
        output_messages=[
            InferenceMessage(
                role="tool",
                parts=[
                    ToolCallResponsePart(
                        id="tool",
                        result={"parser": 2**100, "zero": -0.0, "text": "\x00\ud800"},
                    ),
                ],
            )
        ],
    )
    meta = EvidenceCallMetadata(fact.inference_call_id, fact.observed_at, None, None)
    inventory = project_evidence_inventory(
        "source", 0, found=True, calls=[meta], quarantined_inference_calls=0
    )
    manifest = project_evidence_manifest(
        "source", 0, meta, fact.input_messages, fact.output_messages
    )
    sources = {
        (fact.inference_call_id, side): EvidenceMessageSource(
            fact.observed_at, tuple(messages)
        )
        for side, messages in (
            ("input", fact.input_messages),
            ("output", fact.output_messages),
        )
    }
    return fact, inventory, manifest, sources


def payload(value):
    return json.loads(encode_evidence_json(value, type(value)))


def setup(monkeypatch, transform=None):
    mod = load()
    fact, inventory, manifest, sources = corpus()
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.host == "api.typesafe.ai":
            data = json.loads(request.content)
            answers = {
                key: {"type": "noul", "noul": 0.9}
                for key in data["questions"]
                if key != "history_mode"
            }
            answers["history_mode"] = {
                "type": "choice",
                "choice": "read_history",
                "confidence": 0.8,
                "probabilities": {
                    "read_history": 0.9,
                    "no_history": 0.05,
                    "insufficient": 0.05,
                },
            }
            body = {
                "model": "jev-1.13.0",
                "answers": answers,
                "usage": {"input_tokens": 120, "output_tokens": 10},
            }
        elif request.url.path == "/v1/me":
            body = {
                "org_id": "acme",
                "version": "0.1.0",
                "authority": "retrieval",
                "client_id": "retrieval",
                "source_session_id": "source",
            }
        elif request.url.path.endswith("/manifest"):
            body = payload(manifest)
        elif request.url.path.endswith("/read"):
            from sediment_core import EvidenceReference

            refs = [
                EvidenceReference(**r)
                for r in json.loads(request.content)["references"]
            ]
            body = payload(project_evidence_read("source", 0, refs, sources))
        else:
            body = payload(inventory)
        response = httpx.Response(
            200, content=mod.encoded(body), headers={"Content-Type": "application/json"}
        )
        return transform(request, body, response) if transform else response

    original = httpx.Client

    def client(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return original(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(mod.httpx, "Client", client)
    refs = [p.reference for m in manifest.messages for p in m.parts]
    read = payload(project_evidence_read("source", 0, refs, sources))
    history = mod.assemble_history(payload(manifest), read["items"])
    config = {
        "api_url": "http://127.0.0.1:8765",
        "retrieval_token": "private-retrieval-key",
    }
    return mod, config, mod.digest(mod.encoded(history)), calls


def run(mod, config, digest, tmp_path, arm="C", **kwargs):
    tmp_path.mkdir(parents=True, exist_ok=True)
    return mod.select_context(
        config,
        "source",
        "final",
        "parser constraint",
        arm,
        tmp_path / "records",
        expected_history_sha256=digest,
        **kwargs,
    )


def test_no_history_arm_performs_no_io(tmp_path):
    mod = load()
    result = mod.select_context(
        {},
        "source",
        "final",
        "parser",
        "A",
        tmp_path / "absent",
        expected_history_sha256="a" * 64,
    )
    assert (
        result.status == "no_history"
        and result.items == ()
        and result.context_text == ""
    )
    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize("arm", ["B", "C", "D"])
def test_factual_corpus_same_for_arms_and_selected_reads_keep_exact_values(
    monkeypatch, tmp_path, arm
):
    mod, config, digest, calls = setup(monkeypatch)
    result = run(mod, config, digest, tmp_path, arm, jev_api_key="private-jev-key")
    assert result.status == ("full_history" if arm == "B" else "selected")
    assert "1267650600228229401496703205376" in result.context_text
    assert "\\ud800" in result.context_text and '"zero":-0.0' in result.context_text
    assert "  parser constraint" in result.context_text
    assert len(result.context_text.encode()) <= (32768 if arm == "B" else 8192)
    assert len(calls) == (4 if arm == "B" else 5 if arm == "C" else 6)
    assert len([c for c in calls if c.url.host == "api.typesafe.ai"]) == (arm == "D")
    assert result.metrics["evidence"]["attempted_calls"] == (4 if arm == "B" else 5)
    for path in (tmp_path / "records").iterdir():
        assert path.stat().st_mode & 0o777 == 0o600
        assert b"private-retrieval-key" not in path.read_bytes()
        assert b"private-jev-key" not in path.read_bytes()


def test_wrong_history_hash_leaves_failure_metrics(monkeypatch, tmp_path):
    mod, config, _, calls = setup(monkeypatch)
    with pytest.raises(mod.SelectionError, match="source_mismatch"):
        run(mod, config, "0" * 64, tmp_path)
    record = json.loads((tmp_path / "records/selection.json").read_bytes())
    assert record["status"] == "source_mismatch"
    assert record["metrics"]["evidence"]["attempted_calls"] == 4
    assert len(calls) == 4


@pytest.mark.parametrize(
    "change",
    ["model", "missing", "extra", "nan", "bool", "argmax", "normalization", "usage"],
)
def test_jev_rejects_invalid_contract_without_retry(monkeypatch, tmp_path, change):
    def transform(request, body, response):
        if request.url.host != "api.typesafe.ai":
            return response
        choice = body["answers"]["history_mode"]
        if change == "model":
            body["model"] = "jev-latest"
        elif change == "missing":
            body["answers"].pop(next(k for k in body["answers"] if k != "history_mode"))
        elif change == "extra":
            body["answers"]["unexpected"] = {"type": "noul", "noul": 0.5}
        elif change == "nan":
            choice["confidence"] = float("nan")
        elif change == "bool":
            choice["confidence"] = True
        elif change == "argmax":
            choice["choice"] = "no_history"
        elif change == "normalization":
            choice["probabilities"]["read_history"] = 0.1
        elif change == "usage":
            body["usage"]["input_tokens"] = True
        return httpx.Response(
            200,
            content=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )

    mod, config, digest, calls = setup(monkeypatch, transform)
    with pytest.raises(mod.SelectionError, match="jev_response_invalid"):
        run(mod, config, digest, tmp_path, "D", jev_api_key="private-jev-key")
    assert len(calls) == 5
    record = json.loads((tmp_path / "records/selection.json").read_bytes())
    assert record["metrics"]["jev"]["attempted_calls"] == 1


def test_missing_usage_is_unknown_and_refused(monkeypatch, tmp_path):
    def transform(request, body, response):
        if request.url.host == "api.typesafe.ai":
            body.pop("usage")
            return httpx.Response(200, json=body)
        return response

    mod, config, digest, _ = setup(monkeypatch, transform)
    with pytest.raises(mod.SelectionError, match="jev_response_invalid"):
        run(mod, config, digest, tmp_path, "D", jev_api_key="private-jev-key")
    metrics = json.loads((tmp_path / "records/selection.json").read_bytes())["metrics"]
    assert metrics["jev"]["usage"]["input_tokens"] is None


@pytest.mark.parametrize(
    "mode,confidence,status",
    [
        ("read_history", 0.59, "insufficient"),
        ("no_history", 0.8, "no_history"),
        ("insufficient", 0.8, "insufficient"),
    ],
)
def test_choice_abstention_is_distinct_and_does_not_fetch_selected(
    monkeypatch, tmp_path, mode, confidence, status
):
    def transform(request, body, response):
        if request.url.host == "api.typesafe.ai":
            answer = body["answers"]["history_mode"]
            answer.update(
                choice=mode,
                confidence=confidence,
                probabilities={
                    k: 0.9 if k == mode else 0.05 for k in answer["probabilities"]
                },
            )
            return httpx.Response(200, json=body)
        return response

    mod, config, digest, calls = setup(monkeypatch, transform)
    result = run(mod, config, digest, tmp_path, "D", jev_api_key="private-jev-key")
    assert result.status == status and result.context_text == ""
    assert len(calls) == 5


def test_selected_refetch_rejects_changed_quarantine(monkeypatch, tmp_path):
    reads = 0

    def transform(request, body, response):
        nonlocal reads
        if request.url.path.endswith("/read"):
            reads += 1
            if reads == 2:
                body["quarantine_revision"] = 1
                return httpx.Response(
                    200,
                    content=json.dumps(body).encode(),
                    headers={"Content-Type": "application/json"},
                )
        return response

    mod, config, digest, _ = setup(monkeypatch, transform)
    with pytest.raises(mod.SelectionError, match="source_changed"):
        run(mod, config, digest, tmp_path)


def response(body, status=200):
    return httpx.Response(
        status,
        content=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )


def test_read_timestamp_must_match_manifest(monkeypatch, tmp_path):
    def transform(request, body, original):
        if request.url.path.endswith("/read"):
            body["items"][0]["observed_at"] = "2026-09-23T00:00:00Z"
            return response(body)
        return original

    mod, config, digest, _ = setup(monkeypatch, transform)
    with pytest.raises(mod.SelectionError, match="source_mismatch"):
        run(mod, config, digest, tmp_path)


@pytest.mark.parametrize("phase", ["grant", "inventory", "manifest", "read"])
def test_factual_identity_failures_are_counted_before_selection(
    monkeypatch, tmp_path, phase
):
    def transform(request, body, original):
        if phase == "grant" and request.url.path == "/v1/me":
            body["authority"] = "operator"
        elif phase == "inventory" and request.url.path == "/query/context/evidence":
            body["session_id"] = "foreign"
        elif phase == "manifest" and request.url.path.endswith("/manifest"):
            body["call"]["inference_call_id"] = "foreign"
        elif phase == "read" and request.url.path.endswith("/read"):
            body["items"][0]["reference"]["inference_call_id"] = "foreign"
        else:
            return original
        return response(body)

    mod, config, digest, calls = setup(monkeypatch, transform)
    with pytest.raises(mod.SelectionError):
        run(mod, config, digest, tmp_path, "D", jev_api_key="private-key")
    metrics = json.loads((tmp_path / "records/selection.json").read_bytes())["metrics"]
    assert metrics["evidence"]["attempted_calls"] == len(calls)
    assert metrics["jev"]["attempted_calls"] == 0
    assert metrics["usage"]["input_tokens"] == 0


def test_usage_survives_a_refused_final_read(monkeypatch, tmp_path):
    reads = 0

    def transform(request, body, original):
        nonlocal reads
        if request.url.path.endswith("/read"):
            reads += 1
            if reads == 2:
                return response({"detail": "private error"}, 403)
        return original

    mod, config, digest, calls = setup(monkeypatch, transform)
    with pytest.raises(mod.SelectionError, match="evidence_http_error"):
        run(mod, config, digest, tmp_path, "D", jev_api_key="private-key")
    metrics = json.loads((tmp_path / "records/selection.json").read_bytes())["metrics"]
    assert len(calls) == 6
    assert metrics["usage"]["input_tokens"] == 120
    assert metrics["usage"]["output_tokens"] == 10
    assert metrics["usage"]["cache_read"] is None
    assert metrics["evidence"]["attempted_calls"] == 5


@pytest.mark.parametrize(
    "failure", ["redirect", "transport", "oversized", "malformed", "duplicate"]
)
def test_jev_transport_failure_is_bounded_and_never_retried(
    monkeypatch, tmp_path, failure
):
    def transform(request, body, original):
        if request.url.host != "api.typesafe.ai":
            return original
        if failure == "redirect":
            return httpx.Response(302, headers={"Location": "https://never.invalid"})
        if failure == "transport":
            raise httpx.ReadTimeout("private provider diagnostic", request=request)
        if failure == "oversized":
            return httpx.Response(
                200, content=b"x" * 65537, headers={"Content-Type": "application/json"}
            )
        if failure == "duplicate":
            return httpx.Response(
                200,
                content=b'{"model":"jev-1.13.0","model":"jev-1.13.0"}',
                headers={"Content-Type": "application/json"},
            )
        return httpx.Response(
            200, content=b"{", headers={"Content-Type": "application/json"}
        )

    mod, config, digest, calls = setup(monkeypatch, transform)
    with pytest.raises(mod.SelectionError) as caught:
        run(mod, config, digest, tmp_path, "D", jev_api_key="private-key")
    assert "private" not in str(caught.value)
    assert len(calls) == 5
    assert (tmp_path / "records/jev-01.response").stat().st_size <= 65536
    metrics = json.loads((tmp_path / "records/selection.json").read_bytes())["metrics"]
    assert metrics["usage"]["input_tokens"] is None
    assert metrics["jev"]["attempted_calls"] == 1


def test_catalog_and_context_exact_byte_boundaries(monkeypatch, tmp_path):
    mod, config, digest, _ = setup(monkeypatch)
    initial = run(mod, config, digest, tmp_path / "initial")
    catalog_size, context_size = (
        initial.metrics["catalog_bytes"],
        initial.metrics["context_bytes"],
    )
    monkeypatch.setattr(mod, "CATALOG_BYTES_LIMIT", catalog_size)
    monkeypatch.setattr(mod, "CONTEXT_BYTES_LIMIT", context_size)
    exact = run(mod, config, digest, tmp_path / "exact")
    assert exact.context_text == initial.context_text
    monkeypatch.setattr(mod, "CONTEXT_BYTES_LIMIT", context_size - 1)
    smaller = run(mod, config, digest, tmp_path / "smaller")
    assert len(smaller.items) == len(initial.items) - 1
    assert smaller.metrics["skipped"]["response_budget"] == 1
    monkeypatch.setattr(mod, "CATALOG_BYTES_LIMIT", catalog_size - 1)
    with pytest.raises(mod.SelectionError, match="catalog_limit"):
        run(mod, config, digest, tmp_path / "catalog-overflow")


def test_oversized_jev_request_never_dispatches(monkeypatch, tmp_path):
    mod, config, digest, calls = setup(monkeypatch)
    with pytest.raises(mod.SelectionError, match="jev_request_limit"):
        mod.select_context(
            config,
            "source",
            "final",
            "task " * 14000,
            "D",
            tmp_path / "records",
            expected_history_sha256=digest,
            jev_api_key="private-key",
        )
    assert len(calls) == 4
    metrics = json.loads((tmp_path / "records/selection.json").read_bytes())["metrics"]
    assert metrics["jev"]["attempted_calls"] == 0


def test_packing_keeps_occurrences_and_continues_after_whole_part_refusal():
    mod = load()
    _, _, manifest, sources = corpus()
    refs = [p.reference for m in manifest.messages for p in m.parts]
    items = payload(project_evidence_read("source", 0, refs, sources))["items"]
    huge = json.loads(json.dumps(items[0]))
    huge["part"]["content"] = "parser " * 2000
    candidates = [
        {"id": "huge", "evidence": huge},
        *[
            {
                "id": f"p{i}",
                "evidence": {
                    **items[0],
                    "reference": {**items[0]["reference"], "part_index": i + 1},
                },
            }
            for i in range(10)
        ],
    ]
    skipped = mod._metrics()["skipped"]
    selected = mod._pack(
        {"session_id": "source", "quarantine_revision": 0}, candidates, skipped
    )
    assert (
        len(selected) == 8
        and skipped["response_budget"] == 1
        and skipped["item_limit"] == 2
    )
    assert len({mod.encoded(item["reference"]) for item in selected}) == 8


def test_hard_deadline_records_attempt_and_cancels_slow_transport(
    monkeypatch, tmp_path
):
    import time

    def slow(request, body, original):
        time.sleep(0.1)
        return original

    mod, config, digest, calls = setup(monkeypatch, slow)
    monkeypatch.setattr(mod, "SELECTION_SECONDS", 0.02)
    started = time.monotonic()
    with pytest.raises(mod.SelectionError, match="selection_deadline"):
        run(mod, config, digest, tmp_path)
    assert time.monotonic() - started < 0.2
    assert len(calls) == 1
    metrics = json.loads((tmp_path / "records/selection.json").read_bytes())["metrics"]
    assert metrics["evidence"]["attempted_calls"] == 1


def test_preflight_uses_same_jev_contract_without_source_reads(monkeypatch, tmp_path):
    mod, _, _, calls = setup(monkeypatch)
    result = mod.jev_preflight("private-key", tmp_path / "records")
    assert result["status"] == "passed"
    assert len(calls) == 1 and calls[0].url.host == "api.typesafe.ai"
    assert result["metrics"]["evidence"]["attempted_calls"] == 0
    assert result["metrics"]["catalog_parts"] == 1
    body = json.loads(calls[0].content)
    assert body["model"] == "jev-1.13.0"
    assert "p00" in body["questions"]["p00"]["instructions"]


@pytest.mark.parametrize(
    "mode,confidence",
    [("read_history", 0.44), ("no_history", 0.8), ("insufficient", 0.8)],
)
def test_preflight_accepts_valid_abstention_without_hiding_decision(
    monkeypatch, tmp_path, mode, confidence
):
    def transform(request, body, original):
        choice = body["answers"]["history_mode"]
        choice.update(
            choice=mode,
            confidence=confidence,
            probabilities={
                name: 0.9 if name == mode else 0.05 for name in choice["probabilities"]
            },
        )
        return response(body)

    mod, _, _, calls = setup(monkeypatch, transform)
    result = mod.jev_preflight("private-key", tmp_path / "records")
    assert result["status"] == "passed"
    assert result["metrics"]["jev"]["effective_choice"] == (
        mode if confidence >= 0.6 else "insufficient"
    )
    assert len(calls) == 1
    assert result["metrics"]["usage"]["input_tokens"] == 120


def test_nonfinite_exponent_in_source_is_a_closed_refusal(monkeypatch, tmp_path):
    def transform(request, body, original):
        if request.url.path.endswith("/read"):
            return httpx.Response(
                200,
                content=json.dumps(body).replace("-0.0", "1e999").encode(),
                headers={"Content-Type": "application/json"},
            )
        return original

    mod, config, digest, _ = setup(monkeypatch, transform)
    with pytest.raises(mod.SelectionError, match="source_shape"):
        run(mod, config, digest, tmp_path)


def test_manifest_over_catalog_part_limit_stops_before_content(monkeypatch, tmp_path):
    def transform(request, body, original):
        if request.url.path.endswith("/manifest"):
            part = body["messages"][0]["parts"][0]
            body["messages"][0]["parts"] = [
                {**part, "reference": {**part["reference"], "part_index": index}}
                for index in range(33)
            ]
            return response(body)
        return original

    mod, config, digest, calls = setup(monkeypatch, transform)
    with pytest.raises(mod.SelectionError, match="catalog_limit"):
        run(mod, config, digest, tmp_path)
    assert len(calls) == 3


def test_evidence_attempt_limit_is_checked_before_forwarding(monkeypatch, tmp_path):
    mod, config, digest, calls = setup(monkeypatch)
    monkeypatch.setattr(mod, "EVIDENCE_CALL_LIMIT", 3)
    with pytest.raises(mod.SelectionError, match="evidence_attempt_limit"):
        run(mod, config, digest, tmp_path)
    assert len(calls) == 3


def test_private_artifacts_cannot_be_overwritten(monkeypatch, tmp_path):
    mod, config, digest, calls = setup(monkeypatch)
    run(mod, config, digest, tmp_path)
    prior = (tmp_path / "records/selection.json").read_bytes()
    with pytest.raises(mod.SelectionError, match="private_records_required"):
        run(mod, config, digest, tmp_path)
    assert (tmp_path / "records/selection.json").read_bytes() == prior
    assert len(calls) == 5


def test_missing_jev_key_cannot_dispatch_and_preserves_zero_usage(
    monkeypatch, tmp_path
):
    mod, config, digest, calls = setup(monkeypatch)
    with pytest.raises(mod.SelectionError, match="jev_credentials_missing"):
        run(mod, config, digest, tmp_path, "D")
    metrics = json.loads((tmp_path / "records/selection.json").read_bytes())["metrics"]
    assert len(calls) == 4
    assert metrics["usage"]["input_tokens"] == metrics["usage"]["output_tokens"] == 0
