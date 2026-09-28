# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bounded JEV selection uses real evidence contracts and controlled responses.

Controlled JEV responses test the policy only. They are not live model results.
"""

from datetime import UTC, datetime
import importlib.util
import json
from pathlib import Path
import random
import sys

import httpx
import pytest
from sediment_core import (
    EvidenceCallMetadata,
    EvidenceMessageSource,
    EvidenceReference,
    InferenceCall,
    InferenceMessage,
    ReasoningPart,
    TextPart,
    ToolCallPart,
    ToolCallResponsePart,
    encode_evidence_json,
    project_evidence_inventory,
    project_evidence_manifest,
    project_evidence_read,
)

SCRIPT = Path(__file__).parents[1] / "bounded_evidence_selection.py"
REAL_CLIENT = httpx.Client
QUERY = "alpha beta gamma delta epsilon zeta"


def load():
    spec = importlib.util.spec_from_file_location("bounded_selection", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def default_parts():
    """Keyword scores: 6, 5, 4, 3, 2, 1, 1 (tool result), 0, reasoning."""
    return [
        TextPart(content="alpha beta gamma delta epsilon zeta: the visible task"),
        TextPart(content="alpha beta gamma delta epsilon, café ☃ detail"),
        TextPart(content="alpha beta gamma delta: requirement R"),
        TextPart(content="alpha beta gamma: correction C"),
        TextPart(content="alpha beta: redundant restatement"),
        TextPart(content="alpha: archived distractor"),
        TextPart(content="unrelated optional linter output"),
        ReasoningPart(content="alpha beta gamma delta epsilon zeta reasoning"),
    ]


def corpus(parts=None, tool=True):
    parts = default_parts() if parts is None else parts
    output = []
    if tool:
        output = [
            InferenceMessage(
                role="assistant",
                parts=[
                    ToolCallPart(
                        id="call-000000000000000000000000000000000001",
                        name="bash",
                        arguments={"command": "python3 check.py"},
                    )
                ],
            ),
            InferenceMessage(
                role="tool",
                parts=[
                    ToolCallResponsePart(
                        id="call-000000000000000000000000000000000001",
                        result="zeta failure",
                    )
                ],
            ),
        ]
    fact = InferenceCall(
        inference_call_id="final",
        org_id="acme",
        session_id="source",
        gateway_provider="litellm",
        observed_at=datetime(2026, 9, 26, tzinfo=UTC),
        input_messages=[InferenceMessage(role="user", parts=parts)] if parts else [],
        output_messages=output,
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
    return inventory, manifest, sources


def payload(value):
    return json.loads(encode_evidence_json(value, type(value)))


def response(body, status=200):
    return httpx.Response(
        status,
        content=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )


def answers_from(scores):
    """scores: label -> (relevant, new_information, conflict); default low."""

    def build(questions):
        result = {}
        for key in questions:
            label, name = key.split("_", 1)
            values = scores.get(label, (0.1, 0.1, 0.1))
            index = ("relevant", "new_information", "conflict").index(name)
            result[key] = {"type": "noul", "noul": values[index]}
        return result

    return build


def setup(monkeypatch, *, scores=None, transform=None, parts=None, tool=True):
    mod = load()
    inventory, manifest, sources = corpus(parts, tool)
    calls, clients = [], []
    build = answers_from(scores or {})

    def handle(request):
        calls.append(request)
        if request.url.host == "api.typesafe.ai":
            data = json.loads(request.content)
            body = {
                "model": "jev-1.13.0",
                "answers": build(data["questions"]),
                "usage": {"input_tokens": 900, "output_tokens": 12},
            }
        elif request.url.path == "/v1/me":
            body = {
                "org_id": "acme",
                "version": "0.1.0",
                "authority": "retrieval",
                "client_id": "retrieval",
                "source_session_ids": ["other", "source"],
            }
        elif request.url.path.endswith("/manifest"):
            body = payload(manifest)
        elif request.url.path.endswith("/read"):
            refs = [
                EvidenceReference(**r)
                for r in json.loads(request.content)["references"]
            ]
            body = payload(project_evidence_read("source", 0, refs, sources))
        else:
            body = payload(inventory)
        original = response(body)
        return transform(request, body, original) if transform else original

    def client(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        clients.append(kwargs)
        return REAL_CLIENT(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(mod.httpx, "Client", client)
    refs = [p.reference for m in manifest.messages for p in m.parts]
    read = payload(project_evidence_read("source", 0, refs, sources))
    history = mod.base.assemble_history(payload(manifest), read["items"])
    config = {
        "api_url": "http://127.0.0.1:8765",
        "retrieval_token": "private-retrieval",
    }
    digest = mod.base.digest(mod.base.encoded(history))
    return mod, config, digest, calls, clients


def run(mod, config, digest, path, arm="J1", query=QUERY, **kwargs):
    path.mkdir(parents=True, exist_ok=True)
    kwargs.setdefault("jev_api_key", "private-jev-key" if arm == "J1" else None)
    return mod.select_evidence(
        config,
        "source",
        "final",
        query,
        arm,
        path / "records",
        expected_history_sha256=digest,
        **kwargs,
    )


def record(path):
    return json.loads((path / "records/selection.json").read_bytes())


def contents(selection):
    return [item["part"].get("content") for item in selection.items]


def jev_calls(calls):
    return [c for c in calls if c.url.host == "api.typesafe.ai"]


def test_keyword_arm_matches_the_earlier_keyword_selection(monkeypatch, tmp_path):
    mod, config, digest, calls, _ = setup(monkeypatch)
    k = run(mod, config, digest, tmp_path / "k", "K")
    earlier = mod.base.select_context(
        config,
        "source",
        "final",
        QUERY,
        "C",
        tmp_path / "earlier",
        expected_history_sha256=digest,
    )
    assert k.context_text == earlier.context_text and k.status == "selected"
    assert not jev_calls(calls)
    metrics = record(tmp_path / "k")["metrics"]
    assert metrics["selection"]["decision"] == "keyword"
    assert metrics["usage"]["input_tokens"] == 0
    assert metrics["skipped"]["reasoning_part"] == 1


def test_low_scores_keep_initial_evidence_without_a_global_choice(
    monkeypatch, tmp_path
):
    mod, config, digest, calls, _ = setup(monkeypatch)
    result = run(mod, config, digest, tmp_path)
    assert contents(result) == [
        "alpha beta gamma delta epsilon zeta: the visible task",
        "alpha beta gamma delta epsilon, café ☃ detail",
    ]
    (request,) = jev_calls(calls)
    body = json.loads(request.content)
    assert "history_mode" not in body["questions"]
    assert all(q["type"] == "noul" for q in body["questions"].values())
    selection = record(tmp_path)["metrics"]["selection"]
    assert selection["decision"] == "jev" and selection["qualifying"] == []
    assert [c["id"] for c in selection["candidates"]] == ["c1", "c2", "c3", "c4"]
    assert record(tmp_path)["metrics"]["skipped"]["below_threshold"] == 4


@pytest.mark.parametrize(
    "scores,added",
    [
        ({"c1": (0.9, 0.9, 0.0)}, ["requirement R"]),
        ({"c2": (0.6, 0.1, 0.6)}, ["correction C"]),
        ({"c1": (0.5999, 0.99, 0.99)}, []),
        ({"c1": (0.99, 0.5999, 0.5999)}, []),
        # Relevance orders additions, then new information, then identity.
        (
            {"c1": (0.7, 0.9, 0.0), "c2": (0.9, 0.7, 0.0), "c3": (0.9, 0.8, 0.0)},
            ["redundant restatement", "correction C", "requirement R"],
        ),
        ({"c3": (0.8, 0.8, 0.0), "c4": (0.8, 0.8, 0.0)}, ["redundant", "archived"]),
    ],
)
def test_qualification_thresholds_and_order(monkeypatch, tmp_path, scores, added):
    mod, config, digest, _, _ = setup(monkeypatch, scores=scores)
    result = run(mod, config, digest, tmp_path)
    delivered = contents(result)[2:]
    assert len(delivered) == len(added)
    assert all(tail in text for tail, text in zip(added, delivered, strict=True))


def test_each_question_names_its_candidate_and_state_fields(monkeypatch, tmp_path):
    mod, config, digest, calls, _ = setup(monkeypatch)
    run(mod, config, digest, tmp_path)
    body = json.loads(jev_calls(calls)[0].content)
    state, questions = body["state"], body["questions"]
    assert set(state) == {"task", "history_note", "initial_evidence", "candidates"}
    assert state["task"] == QUERY
    assert [c["id"] for c in state["candidates"]] == ["c1", "c2", "c3", "c4"]
    assert len(questions) == 12
    for index, candidate in enumerate(state["candidates"]):
        label = candidate["id"]
        for name in ("relevant", "new_information", "conflict"):
            question = questions[f"{label}_{name}"]
            assert f"`candidates[{index}]` (id {label})" in question["instructions"]
            assert "`task`" in question["instructions"]
            assert set(question["criteria"]) == {"true", "false"}
            for other in state["candidates"]:
                if other["id"] != label:
                    assert f"id {other['id']})" not in question["instructions"]
            # No question may depend on another question's answer.
            assert "answer" not in question["instructions"].lower()
        assert (
            "`initial_evidence`"
            in questions[f"{label}_new_information"]["instructions"]
        )
    raw = jev_calls(calls)[0].content.decode()
    for private in ("inference_call_id", "reference", "call-0000", "observed_at"):
        assert private not in raw
    assert state["candidates"][0]["part"] == {
        "type": "text",
        "content": "alpha beta gamma delta: requirement R",
    }


def test_tool_results_carry_the_tool_name_not_the_call_identifier(
    monkeypatch, tmp_path
):
    parts = [
        TextPart(content="zeta alpha beta gamma delta epsilon"),
        TextPart(content="zeta alpha beta gamma delta"),
    ]
    mod, config, digest, calls, _ = setup(monkeypatch, parts=parts)
    run(mod, config, digest, tmp_path)
    candidates = json.loads(jev_calls(calls)[0].content)["state"]["candidates"]
    assert candidates == [
        {
            "id": "c1",
            "order": 3,
            "role": "tool",
            "part": {
                "type": "tool_call_response",
                "result": "zeta failure",
                "tool": "bash",
            },
        }
    ]


def test_request_budget_boundary_omits_whole_candidates(monkeypatch, tmp_path):
    mod, config, digest, calls, _ = setup(monkeypatch)
    run(mod, config, digest, tmp_path / "full")
    size = len(jev_calls(calls)[0].content)
    monkeypatch.setattr(mod, "JEV_REQUEST_BYTES", size)
    run(mod, config, digest, tmp_path / "exact")
    assert len(jev_calls(calls)[1].content) == size
    assert record(tmp_path / "exact")["metrics"]["skipped"]["request_budget"] == 0
    monkeypatch.setattr(mod, "JEV_REQUEST_BYTES", size - 1)
    run(mod, config, digest, tmp_path / "over")
    over = record(tmp_path / "over")["metrics"]
    assert over["skipped"]["request_budget"] >= 1
    assert len(jev_calls(calls)[2].content) <= size - 1
    assert [c["request_included"] for c in over["selection"]["candidates"]].count(
        False
    ) == over["skipped"]["request_budget"]


def test_minimum_request_overflow_uses_recorded_keyword_fallback(monkeypatch, tmp_path):
    mod, config, digest, calls, _ = setup(monkeypatch)
    keyword = run(mod, config, digest, tmp_path / "k", "K")
    monkeypatch.setattr(mod, "JEV_REQUEST_BYTES", 100)
    result = run(mod, config, digest, tmp_path / "j1")
    assert result.context_text == keyword.context_text
    metrics = record(tmp_path / "j1")["metrics"]
    assert metrics["selection"]["decision"] == "fallback"
    assert metrics["selection"]["decision_reason"] == "controller_request_budget"
    assert metrics["jev"]["attempted_calls"] == 0 and not jev_calls(calls)
    assert metrics["usage"]["input_tokens"] == 0


def test_no_additional_candidates_skips_jev_with_zero_usage(monkeypatch, tmp_path):
    parts = [TextPart(content="alpha beta"), TextPart(content="gamma")]
    mod, config, digest, calls, _ = setup(monkeypatch, parts=parts, tool=False)
    result = run(mod, config, digest, tmp_path)
    assert contents(result) == ["alpha beta", "gamma"]
    metrics = record(tmp_path)["metrics"]
    assert metrics["selection"]["decision"] == "skipped"
    assert metrics["selection"]["decision_reason"] == "no_additional_candidates"
    assert not jev_calls(calls) and metrics["jev"]["attempted_calls"] == 0
    assert metrics["usage"] == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read": 0,
        "cache_write": 0,
    }


def test_irrelevant_corpus_is_distinct_from_size_refusal(monkeypatch, tmp_path):
    parts = [TextPart(content="unrelated words only")]
    mod, config, digest, calls, _ = setup(monkeypatch, parts=parts, tool=False)
    for arm in ("K", "J1"):
        result = run(mod, config, digest, tmp_path / arm, arm)
        assert result.status == "no_initial_evidence" and result.context_text == ""
        selection = record(tmp_path / arm)["metrics"]["selection"]
        assert selection["evidence_gap"] == "no_keyword_match"
    huge = [TextPart(content="alpha " * 1500)]
    mod, config, digest, calls, _ = setup(monkeypatch, parts=huge, tool=False)
    result = run(mod, config, digest, tmp_path / "huge")
    assert result.status == "no_initial_evidence"
    metrics = record(tmp_path / "huge")["metrics"]
    assert metrics["selection"]["evidence_gap"] == "initial_budget"
    assert metrics["skipped"]["initial_budget"] == 1


def test_empty_authorized_corpus_is_a_distinct_refusal(monkeypatch, tmp_path):
    parts = [TextPart(content="x")]

    def empty(request, body, original):
        if request.url.path.endswith("/manifest"):
            body["messages"] = []
            return response(body)
        return original

    mod, config, digest, calls, _ = setup(
        monkeypatch, parts=parts, tool=False, transform=empty
    )
    with pytest.raises(mod.BoundedSelectionError, match="empty_corpus"):
        run(mod, config, digest, tmp_path / "empty")
    oversized = [TextPart(content="alpha " * 6000)]
    mod, config, digest, calls, _ = setup(monkeypatch, parts=oversized, tool=False)
    with pytest.raises(mod.BoundedSelectionError, match="catalog_limit"):
        run(mod, config, digest, tmp_path / "oversized")


@pytest.mark.parametrize(
    "failure,reason",
    [
        ("status", "jev_http_error"),
        ("transport", "jev_transport_error"),
        ("malformed", "jev_response_invalid"),
        ("oversized", "jev_response_limit"),
        ("model", "jev_response_invalid"),
        ("missing_answer", "jev_response_invalid"),
        ("invented_answer", "jev_response_invalid"),
        ("wrong_type", "jev_response_invalid"),
        ("nonfinite", "jev_response_invalid"),
        ("boolean", "jev_response_invalid"),
        ("out_of_range", "jev_response_invalid"),
        ("missing_usage", "jev_response_invalid"),
        ("redirect", "jev_http_error"),
    ],
)
def test_provider_failures_fall_back_once_and_keep_usage_visible(
    monkeypatch, tmp_path, failure, reason
):
    def transform(request, body, original):
        if request.url.host != "api.typesafe.ai":
            return original
        first = next(iter(body["answers"]))
        if failure == "status":
            return response({"detail": "private provider message"}, 503)
        if failure == "transport":
            raise httpx.ReadTimeout("private provider diagnostic", request=request)
        if failure == "malformed":
            return httpx.Response(
                200, content=b"{", headers={"Content-Type": "application/json"}
            )
        if failure == "oversized":
            return httpx.Response(
                200, content=b" " * 65537, headers={"Content-Type": "application/json"}
            )
        if failure == "redirect":
            return httpx.Response(302, headers={"Location": "https://never.invalid"})
        if failure == "model":
            body["model"] = "jev-latest"
        elif failure == "missing_answer":
            body["answers"].pop(first)
        elif failure == "invented_answer":
            body["answers"]["c9_relevant"] = {"type": "noul", "noul": 1.0}
        elif failure == "wrong_type":
            body["answers"][first] = {"type": "choice", "noul": 0.9}
        elif failure == "nonfinite":
            return httpx.Response(
                200,
                content=json.dumps(body).replace("0.1", "1e999", 1).encode(),
                headers={"Content-Type": "application/json"},
            )
        elif failure == "boolean":
            body["answers"][first]["noul"] = True
        elif failure == "out_of_range":
            body["answers"][first]["noul"] = 1.5
        elif failure == "missing_usage":
            body.pop("usage")
        return response(body)

    mod, config, digest, calls, _ = setup(monkeypatch, transform=transform)
    keyword = run(mod, config, digest, tmp_path / "k", "K")
    result = run(mod, config, digest, tmp_path / "j1")
    assert result.context_text == keyword.context_text
    metrics = record(tmp_path / "j1")["metrics"]
    assert metrics["selection"]["decision"] == "fallback"
    assert metrics["selection"]["decision_reason"] == reason
    assert metrics["jev"]["attempted_calls"] == 1 and len(jev_calls(calls)) == 1
    # Usage reported with an unpinned model stays unknown, as in the earlier
    # selector; it is never credited to the pinned model.
    if failure in {"missing_answer", "invented_answer", "wrong_type"} or (
        failure in {"boolean", "out_of_range"}
    ):
        assert metrics["usage"]["input_tokens"] == 900
    else:
        assert metrics["usage"]["input_tokens"] is None
    assert metrics["usage"]["cache_read"] is None
    for path in (tmp_path / "j1/records").iterdir():
        assert b"private-jev-key" not in path.read_bytes()
        assert b"private-retrieval" not in path.read_bytes()


@pytest.mark.parametrize(
    "phase,reason",
    [
        ("grant", "source_grant_invalid"),
        ("quarantine", "source_changed"),
        ("foreign", "source_mismatch"),
        ("final_changed", "source_mismatch"),
        ("final_quarantine", "source_changed"),
        ("final_refused", "evidence_http_error"),
    ],
)
def test_trust_boundary_failures_refuse_without_fallback(
    monkeypatch, tmp_path, phase, reason
):
    reads = 0

    def transform(request, body, original):
        nonlocal reads
        path = request.url.path
        if path.endswith("/read"):
            reads += 1
        if phase == "grant" and path == "/v1/me":
            body["source_session_ids"] = ["other"]
        elif phase == "quarantine" and path == "/query/context/evidence":
            body["quarantine_revision"] = 1
        elif phase == "foreign" and path.endswith("/read") and reads == 1:
            body["items"][0]["reference"]["inference_call_id"] = "foreign"
        elif phase == "final_changed" and path.endswith("/read") and reads == 2:
            body["items"][0]["part"]["content"] = "changed content"
        elif phase == "final_quarantine" and path.endswith("/read") and reads == 2:
            body["quarantine_revision"] = 1
        elif phase == "final_refused" and path.endswith("/read") and reads == 2:
            return response({"detail": "private"}, 403)
        else:
            return original
        return response(body)

    mod, config, digest, calls, _ = setup(
        monkeypatch, scores={"c1": (0.9, 0.9, 0.0)}, transform=transform
    )
    with pytest.raises(mod.BoundedSelectionError, match=reason):
        run(mod, config, digest, tmp_path)
    saved = record(tmp_path)
    assert saved["status"] == reason
    assert not (tmp_path / "records/context.json").exists()
    if phase in {"grant", "quarantine", "foreign"}:
        assert saved["metrics"]["jev"]["attempted_calls"] == 0


def test_missing_key_refuses_before_any_selector_dispatch(monkeypatch, tmp_path):
    mod, config, digest, calls, _ = setup(monkeypatch)
    with pytest.raises(mod.BoundedSelectionError, match="jev_credentials_missing"):
        run(mod, config, digest, tmp_path, jev_api_key=None)
    metrics = record(tmp_path)["metrics"]
    assert not jev_calls(calls) and metrics["usage"]["input_tokens"] == 0


@pytest.mark.parametrize(
    "environ,outcome",
    [
        ({"JEV_API_KEY": "tsk-valid_KEY.0123456789"}, "tsk-valid_KEY.0123456789"),
        ({}, "jev_credentials_missing"),
        ({"JEV_API_KEY": ""}, "jev_credentials_missing"),
        ({"TYPESAFE_API_KEY": "tsk-other"}, "jev_credentials_missing"),
        ({"JEV_API_KEY": "tsk invalid"}, "jev_credentials_invalid"),
        ({"JEV_API_KEY": "tsk-invalid\n"}, "jev_credentials_invalid"),
        ({"JEV_API_KEY": "x" * 4097}, "jev_credentials_invalid"),
    ],
)
def test_jev_api_key_loader(environ, outcome):
    mod = load()
    if outcome.startswith("jev_credentials"):
        with pytest.raises(mod.BoundedSelectionError) as caught:
            mod.load_jev_api_key(environ)
        assert caught.value.reason == outcome
        assert "tsk" not in str(caught.value)
    else:
        assert mod.load_jev_api_key(environ) == outcome


@pytest.mark.parametrize(
    "transport,valid",
    [
        (None, True),
        ({"proxy": "http://127.0.0.1:45605"}, True),
        ({"proxy": "http://localhost:3128", "ca_bundle": "/etc/hosts"}, True),
        ({"proxy": "http://proxy.example:3128"}, False),
        ({"proxy": "http://user:pw@127.0.0.1:3128"}, False),
        ({"proxy": "http://127.0.0.1"}, False),
        ({"proxy": "https://127.0.0.1:3128"}, False),
        ({"ca_bundle": "relative.crt"}, False),
        ({"ca_bundle": "/nonexistent/bundle.crt"}, False),
        ({"proxy": None, "extra": 1}, False),
    ],
)
def test_jev_transport_is_explicit_and_loopback_only(transport, valid):
    mod = load()
    if valid:
        assert set(mod.jev_transport(transport)) == {"proxy", "ca_bundle"}
    else:
        with pytest.raises(mod.BoundedSelectionError, match="invalid_transport"):
            mod.jev_transport(transport)


def test_selector_client_uses_only_the_configured_proxy(monkeypatch, tmp_path):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    mod, config, digest, _, clients = setup(monkeypatch)
    run(
        mod,
        config,
        digest,
        tmp_path,
        jev_transport_config={"proxy": "http://127.0.0.1:45605"},
    )
    proxies = [kwargs.get("proxy") for kwargs in clients]
    assert proxies == [None, "http://127.0.0.1:45605"]
    assert mod.transport_identity({"proxy": "http://127.0.0.1:45605"}) == (
        "loopback_proxy"
    )


def test_identical_facts_and_shuffled_enumeration_give_identical_requests(
    monkeypatch, tmp_path
):
    mod, config, digest, calls, _ = setup(monkeypatch, scores={"c2": (0.9, 0.9, 0.0)})
    first = run(mod, config, digest, tmp_path / "a")
    second = run(mod, config, digest, tmp_path / "b")
    requests = [c.content for c in jev_calls(calls)]
    assert requests[0] == requests[1] and first.context_text == second.context_text
    catalog = json.loads((tmp_path / "a/records/catalog.json").read_bytes())
    skipped = mod._metrics("J1")["skipped"]
    ranked = mod.base._keyword(catalog, QUERY, dict(skipped))
    anchors, candidates = mod._initial(catalog, ranked, dict(skipped))
    expected = mod.build_request(catalog, QUERY, anchors, candidates, dict(skipped))
    for seed in range(5):
        shuffled = dict(catalog)
        shuffled["candidates"] = list(catalog["candidates"])
        random.Random(seed).shuffle(shuffled["candidates"])
        ranked = mod.base._keyword(shuffled, QUERY, dict(skipped))
        anchors, candidates = mod._initial(shuffled, ranked, dict(skipped))
        assert (
            mod.build_request(shuffled, QUERY, anchors, candidates, dict(skipped))[0]
            == expected[0]
        )
    assert expected[0] == requests[0]


def test_final_envelope_boundary_counts_multibyte_escapes(monkeypatch, tmp_path):
    scores = {"c1": (0.9, 0.9, 0.0)}
    mod, config, digest, _, _ = setup(monkeypatch, scores=scores)
    full = run(mod, config, digest, tmp_path / "full")
    size = full.metrics["context_bytes"]
    assert size == len(full.context_text.encode("utf-8"))
    assert "\\u00e9" in full.context_text and "\\u2603" in full.context_text
    monkeypatch.setattr(mod, "CONTEXT_BYTES_LIMIT", size)
    exact = run(mod, config, digest, tmp_path / "exact")
    assert exact.context_text == full.context_text
    monkeypatch.setattr(mod, "CONTEXT_BYTES_LIMIT", size - 1)
    smaller = run(mod, config, digest, tmp_path / "smaller")
    assert len(smaller.items) == len(full.items) - 1
    selection = record(tmp_path / "smaller")["metrics"]["selection"]
    assert selection["response_omitted"] == ["c1"]
    assert selection["unresolved_conflicts"] == 0


def test_omitted_conflicting_evidence_is_reported_incomplete(monkeypatch, tmp_path):
    mod, config, digest, _, _ = setup(monkeypatch, scores={"c2": (0.9, 0.1, 0.9)})
    monkeypatch.setattr(mod, "CONTEXT_ITEM_LIMIT", 2)
    result = run(mod, config, digest, tmp_path)
    assert len(result.items) == 2
    selection = record(tmp_path)["metrics"]["selection"]
    assert selection["response_omitted"] == ["c2"]
    assert selection["unresolved_conflicts"] == 1


def test_records_are_private_and_never_overwritten(monkeypatch, tmp_path):
    mod, config, digest, calls, _ = setup(monkeypatch)
    run(mod, config, digest, tmp_path)
    prior = (tmp_path / "records/selection.json").read_bytes()
    for path in (tmp_path / "records").iterdir():
        assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(mod.BoundedSelectionError, match="private_records_required"):
        run(mod, config, digest, tmp_path)
    assert (tmp_path / "records/selection.json").read_bytes() == prior


def test_development_probe_uses_the_same_request_formulation(monkeypatch, tmp_path):
    mod, _, _, calls, _ = setup(monkeypatch, scores={"c1": (0.9, 0.9, 0.0)})
    catalog = mod.synthetic_catalog(
        [("user", part.content) for part in default_parts()[:6]]
    )
    result = mod.probe(catalog, QUERY, "private-jev-key", tmp_path / "probe")
    assert result["status"] == "passed"
    assert result["metrics"]["selection"]["qualifying"] == ["c1"]
    assert result["metrics"]["usage"]["input_tokens"] == 900
    (request,) = jev_calls(calls)
    body = json.loads(request.content)
    assert len(body["questions"]) == 12 and body["model"] == "jev-1.13.0"
    assert result["metrics"]["evidence"]["attempted_calls"] == 0


@pytest.mark.parametrize(
    "models,available",
    [
        ([{"name": "jev-1.13.0"}, {"name": "jev-latest"}], True),
        ([{"name": "jev-latest"}], False),
        (None, None),
    ],
)
def test_model_availability_check(monkeypatch, tmp_path, models, available):
    mod = load()

    def handle(request):
        assert request.url.path == "/v1/models" and request.method == "GET"
        return response({"models": models} if models is not None else {"x": 1})

    monkeypatch.setattr(
        mod.httpx,
        "Client",
        lambda **kwargs: REAL_CLIENT(transport=httpx.MockTransport(handle), **kwargs),
    )
    if available is None:
        with pytest.raises(mod.BoundedSelectionError, match="jev_models_invalid"):
            mod.list_models("private-key", tmp_path / "models")
    else:
        assert mod.list_models("private-key", tmp_path / "models")["available"] is (
            available
        )


def test_full_arm_delivers_every_non_reasoning_part_without_jev(monkeypatch, tmp_path):
    mod, config, digest, calls, _ = setup(monkeypatch)
    result = run(mod, config, digest, tmp_path / "full", "FULL")
    texts = [p.content for p in default_parts() if p.type == "text"]
    assert contents(result)[: len(texts)] == texts
    assert len(result.items) == len(texts) + 2  # the tool call and its result
    assert record(tmp_path / "full")["metrics"]["selection"]["decision"] == "full"
    assert not jev_calls(calls)


def test_j2_judges_text_only_and_qualifies_on_new_or_conflict(monkeypatch, tmp_path):
    # c3 is new but scored irrelevant; c4 conflicts; c1 is relevant but not new.
    scores = {"c1": (0.9, 0.1, 0.1), "c3": (0.2, 0.9, 0.1), "c4": (0.1, 0.1, 0.8)}
    mod, config, digest, calls, _ = setup(monkeypatch, scores=scores)
    result = run(mod, config, digest, tmp_path / "j2", "J2", jev_api_key="k" * 24)
    (request,) = jev_calls(calls)
    state = json.loads(request.content)["state"]
    assert state["initial_evidence"] == []
    assert {c["part"]["type"] for c in state["candidates"]} == {"text"}
    assert len(state["candidates"]) == 7
    assert contents(result) == [
        "alpha beta gamma delta: requirement R",
        "alpha beta gamma: correction C",
    ]
    selection = record(tmp_path / "j2")["metrics"]["selection"]
    assert selection["decision"] == "jev" and selection["policy_version"] == 2
    assert selection["qualifying"] == ["c3", "c4"]


@pytest.mark.parametrize(
    "failures,decision,attempts", [(1, "jev", 2), (2, "fallback", 2)]
)
def test_j2_retries_once_when_no_valid_answer_arrives(
    monkeypatch, tmp_path, failures, decision, attempts
):
    seen = []

    def transform(request, body, original):
        if request.url.host != "api.typesafe.ai":
            return original
        seen.append(request)
        if len(seen) <= failures:
            return response({"detail": "upstream connect error"}, 503)
        return original

    scores = {"c3": (0.2, 0.9, 0.1)}
    mod, config, digest, calls, _ = setup(
        monkeypatch, scores=scores, transform=transform
    )
    keyword = run(mod, config, digest, tmp_path / "k", "K")
    result = run(mod, config, digest, tmp_path / "j2", "J2", jev_api_key="k" * 24)
    metrics = record(tmp_path / "j2")["metrics"]
    assert metrics["selection"]["decision"] == decision
    assert metrics["selection"]["first_attempt_error"] == "jev_http_error"
    assert metrics["jev"]["attempted_calls"] == attempts == len(seen)
    if decision == "jev":
        assert contents(result) == ["alpha beta gamma delta: requirement R"]
        assert metrics["usage"]["input_tokens"] == 900
    else:
        assert result.context_text == keyword.context_text
