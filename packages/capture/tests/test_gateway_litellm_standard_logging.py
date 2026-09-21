# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Contract test: LiteLLMAdapter against a REAL LiteLLM StandardLoggingPayload.

The fixture was captured from a live LiteLLM proxy via
litellm/sediment_callback.py. This guards the adapter against payload-shape
drift between LiteLLM versions, including token and latency fields. Capture
additional fixtures when a supported LiteLLM upgrade changes its wire shape
(SEDIMENT_CAPTURE_DIR in the callback).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sediment_capture import LiteLLMAdapter
from sediment_core import (
    GatewayProvider,
    InferenceCall,
    TextPart,
    ToolCallPart,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _payload() -> dict[str, Any]:
    return json.loads((FIXTURES / "litellm_standard_logging_object.json").read_text())


def test_standard_logging_object_normalizes_fully() -> None:
    fact = LiteLLMAdapter().normalize(
        _payload(),
        session_id="sess-litellm",
        user_id="dev-1",
        org_id="acme-corp",
    )
    assert isinstance(fact, InferenceCall)
    assert fact.gateway_provider is GatewayProvider.LITELLM
    assert fact.model == "gpt-4o"
    assert fact.output_messages[0].parts[0].content.startswith("def fibonacci")
    assert fact.input_messages[0].role == "user"
    # Token and latency fields must survive translation.
    assert fact.input_tokens == 10
    assert fact.output_tokens == 20
    assert fact.duration_ms is not None and fact.duration_ms > 0


def test_tokens_resolve_from_response_usage_when_top_level_absent() -> None:
    payload = _payload()
    payload.pop("prompt_tokens", None)
    payload.pop("completion_tokens", None)
    # response.usage still carries them.
    fact = LiteLLMAdapter().normalize(payload, session_id="s", user_id="u", org_id="o")
    assert fact.input_tokens == 10
    assert fact.output_tokens == 20


def test_malformed_top_level_usage_falls_back_to_response_usage() -> None:
    payload = _payload()
    payload["usage"] = "lots"  # truthy non-dict must not mask response.usage
    payload.pop("prompt_tokens", None)
    payload.pop("completion_tokens", None)
    fact = LiteLLMAdapter().normalize(payload, session_id="s", user_id="u", org_id="o")
    assert fact.input_tokens == 10
    assert fact.output_tokens == 20


def test_latency_falls_back_to_start_end_time() -> None:
    payload = _payload()
    payload.pop("response_time", None)
    payload.pop("response_time_ms", None)
    # startTime/endTime remain → latency derived from their delta. Strictly
    # positive: 0 is _latency_ms's failure default, so >= 0 would let a
    # broken fallback pass silently.
    fact = LiteLLMAdapter().normalize(payload, session_id="s", user_id="u", org_id="o")
    assert fact.duration_ms is not None and fact.duration_ms > 0
    assert fact.raw["call_type"] == "acompletion"


def test_non_numeric_response_time_ms_falls_through() -> None:
    # A numeric-string response_time_ms must not zero the latency while the
    # other timing shapes are present — the silent-zeroing failure mode this
    # contract test exists to prevent.
    payload = _payload()
    payload["response_time_ms"] = "412"
    fact = LiteLLMAdapter().normalize(payload, session_id="s", user_id="u", org_id="o")
    assert fact.duration_ms is not None and fact.duration_ms > 0


def test_call_id_from_litellm_call_id() -> None:
    # The proxy call id is the idempotency identity for a retried callback POST.
    fact = LiteLLMAdapter().normalize(
        _payload(), session_id="s", user_id="u", org_id="acme-corp"
    )
    assert fact.model_call_id == "200e6862-acd4-40fb-ae57-715310f5019a"


def test_call_id_falls_back_to_response_id() -> None:
    payload = _payload()
    del payload["litellm_call_id"]
    fact = LiteLLMAdapter().normalize(
        payload, session_id="s", user_id="u", org_id="acme-corp"
    )
    assert fact.model_call_id == "chatcmpl-7abe6be1-e75b-4721-8f4f-05f10724854e"


def test_no_call_id_yields_keyless_inference_call() -> None:
    payload = _payload()
    del payload["litellm_call_id"]
    del payload["response"]["id"]
    fact = LiteLLMAdapter().normalize(
        payload, session_id="s", user_id="u", org_id="acme-corp"
    )
    assert fact.model_call_id is None


def test_keyed_redelivery_collapses_and_keyless_does_not(postgres_store) -> None:
    # UNIQUE indexes enforce deduplication (ADR 0003); test the store boundary.
    store = postgres_store
    adapter = LiteLLMAdapter()

    first = adapter.normalize(_payload(), session_id="s", user_id="u", org_id="acme")
    redelivered = adapter.normalize(
        _payload(), session_id="s", user_id="u", org_id="acme"
    )
    assert store.store_inference_call(first) is True
    assert store.store_inference_call(redelivered) is False

    # The dedup key is provider-scoped: the same call_id under another
    # provider is a different call, never a "redelivery".
    other_provider = redelivered.model_copy(
        update={"gateway_provider": GatewayProvider.HELICONE}
    )
    assert store.store_inference_call(other_provider) is True

    keyless_payload = _payload()
    del keyless_payload["litellm_call_id"]
    del keyless_payload["response"]["id"]
    for _ in range(2):
        keyless = adapter.normalize(
            keyless_payload, session_id="s", user_id="u", org_id="acme"
        )
        # No model_call_id → no dedup key: both inserts land, by design.
        assert store.store_inference_call(keyless) is True


# The OpenAI Responses-API SLO (Codex CLI through the gateway) fixture was
# captured from a live LiteLLM 1.91.0 proxy (codex-cli 0.142.5,
# /v1/responses) via litellm/sediment_callback.py; bulk constants
# (instructions, tool schemas, prompt text) truncated, response.output and
# every key/shape byte-real.


def _responses_payload() -> dict[str, Any]:
    return json.loads(
        (FIXTURES / "litellm_responses_standard_logging_object.json").read_text()
    )


def test_responses_slo_preserves_message_text_and_tool_call_args() -> None:
    fact = LiteLLMAdapter().normalize(
        _responses_payload(), session_id="s", user_id="u", org_id="acme"
    )
    assert (
        fact.output_messages[0]
        .parts[0]
        .content.startswith("Confirmed. Now I'll fix `slugify`")
    )
    assert (
        "cat > slugger/__init__.py"
        in (fact.output_messages[1].parts[0].arguments["cmd"])
    )
    assert fact.input_tokens == 39414
    assert fact.output_tokens == 296
    assert fact.input_messages[0].role == "developer"


def test_responses_slo_undecodable_function_call_args_stay_only_in_raw() -> None:
    payload = _responses_payload()
    calls = [
        i for i in payload["response"]["output"] if i.get("type") == "function_call"
    ]
    calls[0]["arguments"] = "{not json"
    fact = LiteLLMAdapter().normalize(
        payload, session_id="s", user_id="u", org_id="acme"
    )
    assert fact.output_messages[1].parts[0].arguments == {}
    assert fact.raw["response"]["output"][1]["arguments"] == "{not json"


def test_responses_history_tool_items_stay_structured() -> None:
    payload = _responses_payload()
    fact = LiteLLMAdapter().normalize(
        payload, session_id="s", user_id="u", org_id="acme"
    )
    assert len(fact.input_messages) == len(payload["messages"])
    tool_messages = [
        message
        for message in fact.input_messages
        if message.parts and isinstance(message.parts[0], ToolCallPart)
    ]
    assert tool_messages
    assert tool_messages[0].role == "assistant"
    assert tool_messages[0].parts[0].arguments == {
        "cmd": 'cat test_slugger.py; echo "-----"; cat slugger/__init__.py'
    }


def test_responses_history_tool_item_rewrite_is_visible_in_stored_view() -> None:
    # The negative control, as a regression test: a history rewrite that
    # touches ONLY a function_call item must produce different stored
    # messages, so the rollout prefix check splits instead of stitching
    # (never-stitch). Pre-fix both projected to ("", "") and matched.
    a = _responses_payload()
    b = _responses_payload()
    rewritten = next(
        i for i, m in enumerate(b["messages"]) if m.get("type") == "function_call"
    )
    b["messages"][rewritten]["arguments"] = json.dumps({"cmd": "something else"})
    adapter = LiteLLMAdapter()
    fact_a = adapter.normalize(a, session_id="s", user_id="u", org_id="acme")
    fact_b = adapter.normalize(b, session_id="s", user_id="u", org_id="acme")
    assert fact_a.input_messages[rewritten] != fact_b.input_messages[rewritten]
    unchanged = [i for i in range(len(fact_a.input_messages)) if i != rewritten]
    for i in unchanged:
        assert fact_a.input_messages[i] == fact_b.input_messages[i]


# For response-side tool-call extraction: litellm_slo_tool_call.json is the
# wire-verified chat-shape SLO (Anthropic /v1/messages traffic,
# translated by LiteLLM into chat tool_calls); the Responses SLO above carries
# a real function_call item plus request-side echoes of two earlier calls —
# the exact shape the echo exclusion must NOT extract.


def _tool_call_payload() -> dict[str, Any]:
    return json.loads((FIXTURES / "litellm_slo_tool_call.json").read_text())


def test_chat_slo_normalizes_canonical_structured_inference_call() -> None:
    fact = LiteLLMAdapter().normalize(
        _tool_call_payload(), session_id="s", user_id="u", org_id="acme"
    )

    assert isinstance(fact, InferenceCall)
    assert fact.schema_version == 1
    assert fact.gateway_provider is GatewayProvider.LITELLM
    assert fact.model_provider == "anthropic"
    assert fact.model == "anthropic/claude-haiku-4-5-20251001"
    assert fact.input_tokens == 10
    assert fact.output_tokens == 20
    assert fact.duration_ms == 14
    assert fact.model_call_id == "e7dbf70d-4da7-4c58-ac3d-cb6608081c4e"
    assert isinstance(fact.input_messages[0].parts[0], TextPart)
    assert fact.input_messages[0].parts[0].content == "edit the file"
    assert fact.output_messages[0].role == "assistant"
    assert fact.output_messages[0].finish_reason == "tool_calls"
    assert fact.output_messages[0].parts == [
        ToolCallPart(
            id="toolu_mock_00",
            name="Edit",
            arguments={
                "file_path": "/home/dev/project/app/math_utils.py",
                "old_string": "a",
                "new_string": "b",
            },
        )
    ]
    assert fact.model_call_id != fact.output_messages[0].parts[0].id


def test_chat_slo_normalizes_response_tool_calls() -> None:
    fact = LiteLLMAdapter().normalize(
        _tool_call_payload(), session_id="s", user_id="u", org_id="acme"
    )
    assert fact.output_messages[0].parts == [
        ToolCallPart(
            id="toolu_mock_00",
            name="Edit",
            arguments={
                "file_path": "/home/dev/project/app/math_utils.py",
                "old_string": "a",
                "new_string": "b",
            },
        )
    ]
    # The gateway call_id is a
    # LiteLLM UUID, the decision joins on the agent's toolu_ id — disjoint
    # namespaces until tool_calls carries the latter.
    assert fact.model_call_id != fact.output_messages[0].parts[0].id


def test_responses_slo_extracts_response_side_tool_calls_only() -> None:
    payload = _responses_payload()
    fact = LiteLLMAdapter().normalize(
        payload, session_id="s", user_id="u", org_id="acme"
    )
    tool_parts = [
        part
        for message in fact.output_messages
        for part in message.parts
        if isinstance(part, ToolCallPart)
    ]
    assert [part.id for part in tool_parts] == ["toolu_01CirjUmykuyzEVCHQLKgrcx"]
    assert tool_parts[0].name == "exec_command"
    assert "cat > slugger/__init__.py" in tool_parts[0].arguments["cmd"]
    # The request history echoes earlier tool calls back as function_call
    # items under payload["messages"]; those ids must never be extracted —
    # a follow-up request would otherwise re-index every prior call.
    echoed = {
        m["call_id"]
        for m in payload["messages"]
        if isinstance(m, dict) and m.get("type") == "function_call"
    }
    assert echoed  # the fixture must keep carrying echoes
    assert echoed.isdisjoint({part.id for part in tool_parts})


def test_slo_without_tool_calls_yields_empty_list() -> None:
    fact = LiteLLMAdapter().normalize(
        _payload(), session_id="s", user_id="u", org_id="acme"
    )
    assert not any(
        isinstance(part, ToolCallPart)
        for message in fact.output_messages
        for part in message.parts
    )


def test_no_response_body_yields_empty_tool_calls() -> None:
    # Provider with body logging disabled: no response dict at all.
    payload = _tool_call_payload()
    payload["response"] = None
    fact = LiteLLMAdapter().normalize(
        payload, session_id="s", user_id="u", org_id="acme"
    )
    assert fact.output_messages == []


def test_malformed_arguments_keep_the_call_with_empty_input(caplog) -> None:
    # The id is the join-critical field and must survive a malformed-arguments
    # edge; the raw string is never wrapped into input (it stays on raw until
    # storage-seam Basic redaction); the occurrence is counted, not silent.
    payload = _tool_call_payload()
    call = payload["response"]["choices"][0]["message"]["tool_calls"][0]
    call["function"]["arguments"] = "{not json"
    with caplog.at_level("WARNING", logger="sediment.capture.gateway"):
        fact = LiteLLMAdapter().normalize(
            payload, session_id="s", user_id="u", org_id="acme"
        )
    assert fact.output_messages[0].parts == [
        ToolCallPart(id="toolu_mock_00", name="Edit", arguments={})
    ]
    assert "malformed_arguments=1" in caplog.text


def test_non_object_json_arguments_also_degrade_to_empty_input() -> None:
    # Valid JSON that isn't an object (a bare array) is malformed too:
    # input carries a parsed argument object or nothing.
    payload = _tool_call_payload()
    call = payload["response"]["choices"][0]["message"]["tool_calls"][0]
    call["function"]["arguments"] = "[1, 2]"
    fact = LiteLLMAdapter().normalize(
        payload, session_id="s", user_id="u", org_id="acme"
    )
    assert fact.output_messages[0].parts[0].arguments == {}


def test_non_string_call_id_falls_back_to_usable_item_id() -> None:
    # The Responses-shape fallback must key on usability, not truthiness — a
    # non-string call_id (crafted or drifted payload) must not mask a valid
    # string id.
    payload = _responses_payload()
    call = next(
        i for i in payload["response"]["output"] if i.get("type") == "function_call"
    )
    call["call_id"] = 12345
    fact = LiteLLMAdapter().normalize(
        payload, session_id="s", user_id="u", org_id="acme"
    )
    tool_parts = [
        part
        for message in fact.output_messages
        for part in message.parts
        if isinstance(part, ToolCallPart)
    ]
    assert [part.id for part in tool_parts] == [call["id"]]


def test_tool_call_without_id_is_skipped_and_counted(caplog) -> None:
    # A call with no usable id can never join a decision — kept out of the
    # normalized list (absent, never guessed), counted in the log line.
    payload = _tool_call_payload()
    call = payload["response"]["choices"][0]["message"]["tool_calls"][0]
    del call["id"]
    with caplog.at_level("WARNING", logger="sediment.capture.gateway"):
        fact = LiteLLMAdapter().normalize(
            payload, session_id="s", user_id="u", org_id="acme"
        )
    assert not any(
        isinstance(part, ToolCallPart)
        for message in fact.output_messages
        for part in message.parts
    )
    assert "missing_id=1" in caplog.text


def test_tool_calls_survive_the_store_round_trip(postgres_store) -> None:
    store = postgres_store
    fact = LiteLLMAdapter().normalize(
        _tool_call_payload(), session_id="s", user_id="u", org_id="acme"
    )
    assert store.store_inference_call(fact) is True
    assert store.read_inference_calls("acme")[0] == fact
