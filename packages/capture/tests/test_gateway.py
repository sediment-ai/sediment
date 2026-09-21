# SPDX-License-Identifier: AGPL-3.0-or-later
"""LiteLLM gateway adapter against the hand-shaped unit fixture."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sediment_capture import ADAPTERS, LiteLLMAdapter
from sediment_core import (
    GatewayProvider,
    InferenceCall,
    InferenceMessage,
    ReasoningPart,
    TextPart,
    ToolCallPart,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _payload() -> dict[str, Any]:
    return json.loads((FIXTURES / "litellm_payload.json").read_text())


def test_normalize_produces_valid_inference_call() -> None:
    adapter = LiteLLMAdapter()
    fact = adapter.normalize(
        _payload(),
        session_id="sess-1",
        user_id="user-1",
        org_id="acme-corp",
    )
    assert isinstance(fact, InferenceCall)
    assert fact.gateway_provider is GatewayProvider.LITELLM
    assert fact.model == "gpt-4o"
    assert "def fibonacci" in fact.output_messages[0].parts[0].content
    assert fact.input_tokens == 28
    assert fact.output_tokens == 34
    assert fact.duration_ms == 412


def test_messages_parsed_into_message_objects() -> None:
    adapter = LiteLLMAdapter()
    fact = adapter.normalize(_payload(), session_id="s", user_id="u", org_id="o")
    assert len(fact.input_messages) == 2
    assert all(isinstance(m, InferenceMessage) for m in fact.input_messages)
    assert fact.input_messages[0].role == "system"
    assert fact.input_messages[1].role == "user"


def test_missing_usage_stays_absent() -> None:
    adapter = LiteLLMAdapter()
    payload = _payload()
    del payload["usage"]
    fact = adapter.normalize(payload, session_id="s", user_id="u", org_id="o")
    assert fact.input_tokens is None
    assert fact.output_tokens is None


def test_unknown_fields_preserved_in_raw() -> None:
    adapter = LiteLLMAdapter()
    payload = _payload()
    payload["custom_trace_id"] = "trace-xyz"
    fact = adapter.normalize(payload, session_id="s", user_id="u", org_id="o")
    assert fact.raw["custom_trace_id"] == "trace-xyz"


def test_text_content_part_stays_structured() -> None:
    adapter = LiteLLMAdapter()
    payload = _payload()
    parts = [{"type": "text", "text": "what is in this image?"}]
    payload["messages"] = [{"role": "user", "content": parts}]
    fact = adapter.normalize(payload, session_id="s", user_id="u", org_id="o")
    assert fact.input_messages[0].parts == [TextPart(content="what is in this image?")]
    assert fact.raw["messages"][0]["content"] == parts


def test_readable_reasoning_blocks_preserve_order_and_opaque_blocks_count(
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = _payload()
    payload["messages"] = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "thinking",
                    "thinking": "Inspect every caller.",
                    "signature": "provider-state",
                },
                {"type": "text", "text": "I found the seam."},
                {"type": "reasoning", "content": "Patch it once."},
                {"type": "redacted_thinking", "data": "opaque-provider-state"},
            ],
        }
    ]

    with caplog.at_level("INFO", logger="sediment.capture.gateway"):
        fact = LiteLLMAdapter().normalize(
            payload, session_id="s", user_id="u", org_id="o"
        )

    assert fact.input_messages[0].parts == [
        ReasoningPart(content="Inspect every caller."),
        TextPart(content="I found the seam."),
        ReasoningPart(content="Patch it once."),
    ]
    record = next(
        record
        for record in caplog.records
        if "inference_messages_degraded" in record.message
    )
    assert record.levelname == "INFO"
    assert "opaque_reasoning=1" in record.message
    assert "unsupported_content=0" in record.message
    assert fact.raw["messages"][0]["content"][0]["signature"] == "provider-state"


def test_unknown_message_part_still_warns(caplog: pytest.LogCaptureFixture) -> None:
    payload = _payload()
    payload["messages"] = [{"role": "assistant", "content": [{"type": "hologram"}]}]

    with caplog.at_level("INFO", logger="sediment.capture.gateway"):
        LiteLLMAdapter().normalize(payload, session_id="s", user_id="u", org_id="o")

    record = next(
        record
        for record in caplog.records
        if "inference_messages_degraded" in record.message
    )
    assert record.levelname == "WARNING"
    assert "unsupported_content=1" in record.message


def test_non_reasoning_thinking_block_warns_and_uses_readable_fallback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = _payload()
    message = payload["response"]["choices"][0]["message"]
    message["thinking_blocks"] = [{"type": "text", "text": "wrong container"}]
    message["reasoning_content"] = "Readable fallback."

    with caplog.at_level("INFO", logger="sediment.capture.gateway"):
        fact = LiteLLMAdapter().normalize(
            payload, session_id="s", user_id="u", org_id="o"
        )

    assert fact.output_messages[0].parts[0] == ReasoningPart(
        content="Readable fallback."
    )
    record = next(
        record
        for record in caplog.records
        if "inference_messages_degraded" in record.message
    )
    assert record.levelname == "WARNING"
    assert "unsupported_content=1" in record.message


def test_malformed_thinking_blocks_container_warns_and_uses_fallback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = _payload()
    message = payload["response"]["choices"][0]["message"]
    message["thinking_blocks"] = {
        "type": "thinking",
        "thinking": "wrong container",
    }
    message["reasoning_content"] = "Readable fallback."

    with caplog.at_level("INFO", logger="sediment.capture.gateway"):
        fact = LiteLLMAdapter().normalize(
            payload, session_id="s", user_id="u", org_id="o"
        )

    assert fact.output_messages[0].parts[0] == ReasoningPart(
        content="Readable fallback."
    )
    record = next(
        record
        for record in caplog.records
        if "inference_messages_degraded" in record.message
    )
    assert record.levelname == "WARNING"
    assert "unsupported_content=1" in record.message


def _tool_call_payload(arguments: Any, content: Any = None) -> dict[str, Any]:
    """A turn whose output is a tool call — what a coding agent actually
    emits when it writes a file."""
    payload = _payload()
    payload["response"]["choices"][0]["message"] = {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "Write", "arguments": arguments},
            }
        ],
    }
    return payload


def test_tool_call_output_is_captured_as_structured_arguments() -> None:
    code = "def token_overlap(a, b):\n    return len(a & b) / len(a | b)\n"
    fact = LiteLLMAdapter().normalize(
        _tool_call_payload(json.dumps({"file_path": "probe.py", "content": code})),
        session_id="s",
        user_id="u",
        org_id="o",
    )
    assert fact.output_messages[0].parts == [
        ToolCallPart(
            id="call_1",
            name="Write",
            arguments={"file_path": "probe.py", "content": code},
        )
    ]


def test_spoken_content_and_tool_call_are_both_captured() -> None:
    # A turn can narrate *and* write. Both are the model's output.
    fact = LiteLLMAdapter().normalize(
        _tool_call_payload(
            json.dumps({"content": "def probe(): ..."}),
            content="I'll write the probe.",
        ),
        session_id="s",
        user_id="u",
        org_id="o",
    )
    assert fact.output_messages[0].parts == [
        TextPart(content="I'll write the probe."),
        ToolCallPart(
            id="call_1",
            name="Write",
            arguments={"content": "def probe(): ..."},
        ),
    ]


def test_output_structured_reasoning_precedes_text_and_flattened_fallback() -> None:
    payload = _tool_call_payload(
        json.dumps({"content": "def probe(): ..."}),
        content="I'll write the probe.",
    )
    message = payload["response"]["choices"][0]["message"]
    message["reasoning_content"] = "flattened duplicate"
    message["provider_specific_fields"] = {
        "thinking_blocks": [
            {
                "type": "thinking",
                "thinking": "Use the smallest patch.",
                "signature": "provider-state",
            }
        ]
    }

    fact = LiteLLMAdapter().normalize(payload, session_id="s", user_id="u", org_id="o")

    assert fact.output_messages[0].parts == [
        ReasoningPart(content="Use the smallest patch."),
        TextPart(content="I'll write the probe."),
        ToolCallPart(
            id="call_1",
            name="Write",
            arguments={"content": "def probe(): ..."},
        ),
    ]


def test_output_reasoning_content_is_the_readable_fallback() -> None:
    payload = _payload()
    message = payload["response"]["choices"][0]["message"]
    message["reasoning_content"] = "Check the invariant first."

    fact = LiteLLMAdapter().normalize(payload, session_id="s", user_id="u", org_id="o")

    assert fact.output_messages[0].parts[:2] == [
        ReasoningPart(content="Check the invariant first."),
        TextPart(content=message["content"]),
    ]


def test_empty_structured_reasoning_uses_readable_fallback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = _payload()
    message = payload["response"]["choices"][0]["message"]
    message["thinking_blocks"] = [
        {"type": "thinking", "thinking": "", "signature": "provider-state"}
    ]
    message["reasoning_content"] = "Readable fallback."

    with caplog.at_level("INFO", logger="sediment.capture.gateway"):
        fact = LiteLLMAdapter().normalize(
            payload, session_id="s", user_id="u", org_id="o"
        )

    assert fact.output_messages[0].parts[:2] == [
        ReasoningPart(content="Readable fallback."),
        TextPart(content=message["content"]),
    ]
    assert "opaque_reasoning=1" in caplog.text


@pytest.mark.parametrize("empty_content", ["", " \t"])
def test_empty_flattened_reasoning_counts_opaque_without_a_part(
    empty_content: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = _payload()
    message = payload["response"]["choices"][0]["message"]
    message["reasoning_content"] = empty_content

    with caplog.at_level("INFO", logger="sediment.capture.gateway"):
        fact = LiteLLMAdapter().normalize(
            payload, session_id="s", user_id="u", org_id="o"
        )

    assert fact.output_messages[0].parts == [TextPart(content=message["content"])]
    assert "opaque_reasoning=1" in caplog.text


def test_output_content_reasoning_block_prevents_flattened_duplication(
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = _payload()
    message = payload["response"]["choices"][0]["message"]
    message["content"] = [
        {"type": "reasoning", "content": "Structured reasoning."},
        {"type": "text", "text": "Visible answer."},
    ]
    message["reasoning_content"] = "Flattened duplicate."
    message["thinking_blocks"] = [
        {"type": "redacted_thinking", "data": "opaque-provider-state"}
    ]

    with caplog.at_level("INFO", logger="sediment.capture.gateway"):
        fact = LiteLLMAdapter().normalize(
            payload, session_id="s", user_id="u", org_id="o"
        )

    assert fact.output_messages[0].parts == [
        ReasoningPart(content="Structured reasoning."),
        TextPart(content="Visible answer."),
    ]
    assert "opaque_reasoning=1" in caplog.text


@pytest.mark.parametrize(
    "arguments",
    [
        "{not json at all",  # truncated/streamed arguments
        "",
        None,
        {"already": "decoded"},
        [1, 2, 3],
    ],
)
def test_malformed_tool_call_arguments_degrade_never_raise(arguments: Any) -> None:
    fact = LiteLLMAdapter().normalize(
        _tool_call_payload(arguments), session_id="s", user_id="u", org_id="o"
    )
    assert isinstance(fact, InferenceCall)


@pytest.mark.parametrize(
    "mutation",
    [
        {"messages": [{"content": "no role"}, "not-a-dict", None]},
        {"messages": "bogus"},
        {"response": {"choices": {"0": {}}}},
        {"response": {"choices": [None]}},
        {"response": {"choices": [{"message": "bare string"}]}},
        {"response": {"choices": [{"message": {"tool_calls": "bogus"}}]}},
        {"response": {"choices": [{"message": {"tool_calls": [None, {}, 7]}}]}},
        {"response": 17},
        {"usage": "lots"},
        {"model": None},
        {"response_time_ms": "412"},
    ],
)
def test_malformed_fields_degrade_never_raise(mutation: dict[str, Any]) -> None:
    # Fail-soft contract (same as github.py): a crafted payload degrades,
    # never raises — an exception would 500 the ingest route and lose the
    # completion.
    payload = {**_payload(), **mutation}
    fact = LiteLLMAdapter().normalize(payload, session_id="s", user_id="u", org_id="o")
    assert isinstance(fact, InferenceCall)


@pytest.mark.parametrize(
    "mutation",
    [
        {"usage": {"prompt_tokens": 10**30, "completion_tokens": 2**63}},
        {"usage": {"prompt_tokens": float("nan"), "completion_tokens": float("inf")}},
        {"response_time_ms": float("nan")},
        {"response_time_ms": None, "response_time": float("inf")},
        {"response_time_ms": None, "response_time": 1e308},
        {"response_time_ms": None, "startTime": -1e308, "endTime": 1e308},
        # Negatives are out of bounds too: the schema is ge=0, so the adapter
        # must degrade them before construction.
        {"usage": {"prompt_tokens": -5, "completion_tokens": -9}},
        {"response_time_ms": -1},
    ],
)
def test_hostile_numerics_degrade_to_absent(mutation: dict[str, Any]) -> None:
    # int(NaN) raises here in the adapter; a > int64 value raises
    # an error later at the BIGINT insert. Both must degrade to 0 —
    # an exception would 500 the ingest route (json.loads accepts NaN,
    # Infinity, and arbitrary-precision ints, so all are wire-reachable).
    payload = {**_payload(), **mutation}
    fact = LiteLLMAdapter().normalize(payload, session_id="s", user_id="u", org_id="o")
    assert isinstance(fact, InferenceCall)
    assert all(
        value is None or 0 <= value < 2**63
        for value in (fact.input_tokens, fact.output_tokens, fact.duration_ms)
    )


def test_junk_call_id_degrades_to_fallback_or_keyless() -> None:
    # call_id IS the dedup key (uq_completions_call): a truthy non-string
    # (True → "True") or a blank string would be one shared key collapsing
    # every later completion as a "redelivery" — _usable_id gates it the
    # same way it already gated tool-call ids.
    payload = _payload()
    payload["litellm_call_id"] = True
    payload.setdefault("response", {})["id"] = "resp-fallback"
    fact = LiteLLMAdapter().normalize(payload, session_id="s", user_id="u", org_id="o")
    assert fact.model_call_id == "resp-fallback"

    payload = _payload()
    payload["litellm_call_id"] = "   "
    payload.setdefault("response", {})["id"] = 123  # non-string: unusable too
    fact = LiteLLMAdapter().normalize(payload, session_id="s", user_id="u", org_id="o")
    assert fact.model_call_id is None


def test_out_of_range_tokens_and_latency_are_absent() -> None:
    payload = {
        **_payload(),
        "usage": {"prompt_tokens": 10**30, "completion_tokens": float("nan")},
        "response_time_ms": float("inf"),
        "response_time": None,
        "startTime": None,
        "endTime": None,
    }
    fact = LiteLLMAdapter().normalize(payload, session_id="s", user_id="u", org_id="o")
    assert fact.input_tokens is None
    assert fact.output_tokens is None
    assert fact.duration_ms is None


def test_boolean_timing_fields_never_fabricate_latency() -> None:
    # bool is an int subclass; True/False timing values must fall through to
    # 0, not compute a fake delta.
    payload = _payload()
    del payload["response_time_ms"]
    payload["startTime"], payload["endTime"] = False, True
    fact = LiteLLMAdapter().normalize(payload, session_id="s", user_id="u", org_id="o")
    assert fact.duration_ms is None


def test_null_model_stays_absent() -> None:
    payload = _payload()
    payload["model"] = None
    fact = LiteLLMAdapter().normalize(payload, session_id="s", user_id="u", org_id="o")
    assert fact.model is None


def test_registry_holds_only_implemented_adapters() -> None:
    # Unimplemented providers stay unregistered so the API can 400 cleanly.
    assert set(ADAPTERS) == {GatewayProvider.LITELLM}
    assert isinstance(ADAPTERS[GatewayProvider.LITELLM], LiteLLMAdapter)


def test_whitespace_model_stays_absent() -> None:
    # A whitespace-only wire model is truthy, so the plain `or "unknown"`
    # missed it and ModelName raised at construction — dropping the fact
    # where the adapter contract says degrade.
    payload = {**_payload(), "model": "   "}
    fact = LiteLLMAdapter().normalize(payload, session_id="s", user_id="u", org_id="o")
    assert fact.model is None
