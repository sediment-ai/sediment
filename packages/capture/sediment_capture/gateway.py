# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Gateway adapters: provider payloads into structured inference-call facts.

Pure payload→fact translation — no I/O, no storage, no attribution at
ingest (ADR 0001). Identity (session/user/org) arrives as parameters: the
ingest route binds ``org_id`` to the credential and the callback
supplies real session/user ids (ADR 0002 — no placeholders here). The
``ADAPTERS`` registry lives in the same module as the adapters it registers.

Fail-soft posture throughout (the same contract as ``github.py``): a
malformed payload must degrade, never raise — the ingest route would turn
an exception into a 500 and the inference call would be lost. The original
payload always survives on ``raw``.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime
from typing import Any
from uuid import UUID, uuid5

from sediment_core import (
    GatewayProvider,
    InferenceCall,
    InferenceMessage,
    ReasoningPart,
    TextPart,
    ToolCallPart,
    ToolCallResponsePart,
    normalize_org_id,
)

logger = logging.getLogger("sediment.capture.gateway")

# PostgreSQL BIGINT is a signed 64-bit integer. Larger values fail at INSERT,
# and int(NaN)/int(inf) raise here in the adapter — bound once so
# a crafted numeric degrades instead of crashing (same guard as github.py's
# pr_number).
_INT64_MAX = 2**63 - 1

# Dedicated namespace for callback-prepared capture identities (ADR 0017).
_CAPTURE_NAMESPACE = UUID("f8bfd6e0-f182-4bcd-a1f8-85a987a8c6e2")


def _as_dict(value: Any) -> dict[str, Any]:
    """Gateway payloads are untrusted — coerce a missing/non-dict field to {}."""
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    """Untrusted sibling of ``_as_dict`` for list-shaped fields."""
    return value if isinstance(value, list) else []


def _int_in_bounds(value: Any) -> int | None:
    """Coerce a numeric to the fact store's int64 range; fail soft otherwise.

    Negatives are out of bounds too: every consumer here is a count or a
    latency, ge=0 at the schema — a crafted negative must degrade to the
    fallback, not raise at InferenceCall construction.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    n = int(value)
    if not 0 <= n <= _INT64_MAX:
        return None
    return n


def _first_int(*values: Any) -> int | None:
    """Return the first storable int value, else ``None``."""
    for value in values:
        n = _int_in_bounds(value)
        if n is not None:
            return n
    return None


def _text(value: Any) -> str:
    """Best-effort text for a message field. Strings pass
    through; non-string shapes (e.g. multimodal content parts) degrade to
    their JSON form rather than raising during message-part validation."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return str(value)


def _tool_call_input(arguments: Any) -> dict[str, Any] | None:
    """The parsed argument object, or None when malformed.

    OpenAI-shape ``arguments`` is a JSON string; Anthropic-shape ``input`` is
    already an object. Anything that fails to parse as a JSON *object* —
    invalid JSON, or valid JSON that isn't a dict — is malformed: the caller
    keeps the tool-call part with ``arguments={}`` (the id is join-critical
    and must survive a cosmetic defect) and counts the occurrence. Never
    wrap the raw string into input — the unparsed original is already on
    ``InferenceCall.raw`` for storage-seam Basic redaction; normalized fields
    carry normalized data or nothing.
    """
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (TypeError, ValueError):
            return None
    return arguments if isinstance(arguments, dict) else None


def _usable_id(value: Any) -> bool:
    """A tool-call id that can join something: a non-blank string."""
    return isinstance(value, str) and bool(value.strip())


def _optional_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _decoded_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _tool_part(
    call_id: Any,
    name: Any,
    arguments: Any,
    degraded: dict[str, int],
) -> ToolCallPart | None:
    if not _usable_id(call_id):
        degraded["missing_id"] += 1
        return None
    parsed = _tool_call_input(arguments)
    if parsed is None:
        degraded["malformed_arguments"] += 1
        parsed = {}
    return ToolCallPart(id=call_id, name=_text(name), arguments=parsed)


def _reasoning_part(content: Any, degraded: dict[str, int]) -> ReasoningPart | None:
    if isinstance(content, str) and content.strip():
        return ReasoningPart(content=content)
    degraded["opaque_reasoning"] += 1
    return None


def _discriminator_declined(value: Any, *, position: int, field: str) -> None:
    logger.warning(
        "gateway_discriminator_declined",
        extra={
            "source": "litellm",
            "record_position": position,
            "field": field,
            "reason": "unsupported_discriminator"
            if isinstance(value, str)
            else "malformed_discriminator",
        },
    )


def _message_part(
    block: Any,
    degraded: dict[str, int],
    *,
    position: int,
):
    if block is None:
        return None
    if isinstance(block, str):
        return TextPart(content=block)
    if not isinstance(block, dict):
        degraded["unsupported_content"] += 1
        return None
    part_type = block.get("type")
    if not isinstance(part_type, str):
        _discriminator_declined(part_type, position=position, field="content.type")
        degraded["unsupported_content"] += 1
        return None
    if part_type in {"thinking", "reasoning"}:
        content = block.get("thinking", block.get("text", block.get("content")))
        return _reasoning_part(content, degraded)
    if part_type == "redacted_thinking":
        degraded["opaque_reasoning"] += 1
        return None
    if part_type in {"text", "input_text", "output_text"}:
        text = block.get("text", block.get("content"))
        if isinstance(text, str):
            return TextPart(content=text)
        degraded["unsupported_content"] += 1
        return None
    elif part_type in {"tool_use", "tool_call"}:
        return _tool_part(
            block.get("id"),
            block.get("name"),
            block.get("input", block.get("arguments")),
            degraded,
        )
    elif part_type in {"tool_result", "tool_call_response"}:
        call_id = block.get("tool_use_id", block.get("id"))
        if not _usable_id(call_id):
            degraded["missing_id"] += 1
            return None
        result = block.get("result", block.get("content"))
        return ToolCallResponsePart(id=call_id, result=_decoded_json(result))
    _discriminator_declined(part_type, position=position, field="content.type")
    degraded["unsupported_content"] += 1
    return None


def _message_reasoning_parts(
    item: dict[str, Any], degraded: dict[str, int], *, position: int
) -> list[ReasoningPart]:
    thinking_blocks = item.get("thinking_blocks")
    if thinking_blocks is not None and not isinstance(thinking_blocks, list):
        degraded["unsupported_content"] += 1
        thinking_blocks = None
    if thinking_blocks is None:
        provider_thinking_blocks = _as_dict(item.get("provider_specific_fields")).get(
            "thinking_blocks"
        )
        if provider_thinking_blocks is not None and not isinstance(
            provider_thinking_blocks, list
        ):
            degraded["unsupported_content"] += 1
        else:
            thinking_blocks = provider_thinking_blocks

    parts = []
    if isinstance(thinking_blocks, list):
        for block in thinking_blocks:
            if not isinstance(block, dict):
                degraded["unsupported_content"] += 1
                continue
            part_type = block.get("type")
            if not isinstance(part_type, str) or part_type not in {
                "thinking",
                "reasoning",
                "redacted_thinking",
            }:
                _discriminator_declined(
                    part_type, position=position, field="thinking_blocks.type"
                )
                degraded["unsupported_content"] += 1
                continue
            part = _message_part(block, degraded, position=position)
            if isinstance(part, ReasoningPart):
                parts.append(part)
    if parts:
        return parts

    reasoning_content = item.get("reasoning_content")
    if reasoning_content is not None:
        part = _reasoning_part(reasoning_content, degraded)
        return [part] if part is not None else []
    return []


def _inference_message(
    item: dict[str, Any],
    degraded: dict[str, int],
    *,
    finish_reason: Any = None,
    position: int,
) -> InferenceMessage | None:
    item_type = item.get("type")
    if item_type is not None and (
        not isinstance(item_type, str)
        or item_type not in {"message", "function_call", "function_call_output"}
    ):
        _discriminator_declined(item_type, position=position, field="type")
        degraded["unsupported_content"] += 1
        return None
    if item_type == "function_call":
        call_id = item.get("call_id")
        if not _usable_id(call_id):
            call_id = item.get("id")
        part = _tool_part(call_id, item.get("name"), item.get("arguments"), degraded)
        return InferenceMessage(role="assistant", parts=[part]) if part else None
    if item_type == "function_call_output":
        call_id = item.get("call_id")
        if not _usable_id(call_id):
            degraded["missing_id"] += 1
            return None
        return InferenceMessage(
            role="tool",
            parts=[
                ToolCallResponsePart(
                    id=call_id, result=_decoded_json(item.get("output"))
                )
            ],
        )

    role = _optional_text(item.get("role"))
    if role is None:
        degraded["unsupported_content"] += 1
        return None

    content = item.get("content")
    if role == "tool" and _usable_id(item.get("tool_call_id")):
        parts = [
            ToolCallResponsePart(id=item["tool_call_id"], result=_decoded_json(content))
        ]
    else:
        blocks = content if isinstance(content, list) else [content]
        content_parts = [
            part
            for block in blocks
            if (part := _message_part(block, degraded, position=position)) is not None
        ]
        sibling_reasoning = (
            _message_reasoning_parts(item, degraded, position=position)
            if role == "assistant"
            else []
        )
        parts = (
            sibling_reasoning
            if not any(isinstance(part, ReasoningPart) for part in content_parts)
            else []
        )
        parts.extend(content_parts)
        for call in _as_list(item.get("tool_calls")):
            if not isinstance(call, dict):
                degraded["unsupported_content"] += 1
                continue
            call_type = call.get("type")
            if call_type is not None and (
                not isinstance(call_type, str) or call_type != "function"
            ):
                _discriminator_declined(
                    call_type, position=position, field="tool_calls.type"
                )
                degraded["unsupported_content"] += 1
                continue
            function = _as_dict(call.get("function"))
            part = _tool_part(
                call.get("id"),
                function.get("name"),
                function.get("arguments"),
                degraded,
            )
            if part is not None:
                parts.append(part)

    finish = _optional_text(finish_reason)
    return InferenceMessage(role=role, parts=parts, finish_reason=finish)


def _messages(items: list[Any], *, finish_reason: Any = None) -> list[InferenceMessage]:
    degraded = {
        "malformed_arguments": 0,
        "missing_id": 0,
        "opaque_reasoning": 0,
        "unsupported_content": 0,
    }
    messages = []
    for position, item in enumerate(items):
        if not isinstance(item, dict):
            degraded["unsupported_content"] += 1
            continue
        message = _inference_message(
            item, degraded, finish_reason=finish_reason, position=position
        )
        if message is not None:
            messages.append(message)
    if any(degraded.values()):
        level = (
            logging.WARNING
            if any(
                value for key, value in degraded.items() if key != "opaque_reasoning"
            )
            else logging.INFO
        )
        logger.log(
            level,
            "inference_messages_degraded malformed_arguments=%d missing_id=%d "
            "unsupported_content=%d opaque_reasoning=%d kept=%d",
            degraded["malformed_arguments"],
            degraded["missing_id"],
            degraded["unsupported_content"],
            degraded["opaque_reasoning"],
            len(messages),
        )
    return messages


class LiteLLMAdapter:
    """Normalizes LiteLLM inference-call payloads.

    Handles the real proxy ``StandardLoggingPayload`` (what the custom
    callback forwards in production — see ``litellm/sediment_callback.py``)
    as well as the shaped payload the same callback's ``_fallback_payload``
    builds when no SLO is present (also the shape of the hand-written unit
    fixture). The two differ in where tokens and timing live:

    - tokens: top-level ``usage`` (shaped) vs ``response.usage`` / top-level
      ``prompt_tokens``/``completion_tokens`` (StandardLoggingPayload)
    - timing: ``response_time_ms`` (shaped) vs ``response_time`` seconds or
      ``endTime`` - ``startTime`` unix seconds (StandardLoggingPayload)

    https://docs.litellm.ai/docs/proxy/logging
    """

    def normalize(
        self,
        payload: dict[str, Any],
        *,
        session_id: str,
        user_id: str | None,
        org_id: str,
        capture_id: UUID | None = None,
        observed_at: datetime | None = None,
    ) -> InferenceCall:
        if (capture_id is None) != (observed_at is None):
            raise ValueError("capture_id and observed_at must be supplied together")
        capture_fields: dict[str, Any] = {}
        if capture_id is not None:
            capture_fields = {
                "inference_call_id": str(
                    uuid5(
                        _CAPTURE_NAMESPACE,
                        json.dumps(
                            [normalize_org_id(org_id), str(UUID(str(capture_id)))],
                            separators=(",", ":"),
                        ),
                    )
                ),
                "observed_at": observed_at,
            }
        raw_response = payload.get("response")
        response_text = raw_response if isinstance(raw_response, str) else ""
        response = _as_dict(raw_response)
        choices = _as_list(response.get("choices"))
        first_choice = _as_dict(choices[0]) if choices else {}
        message = _as_dict(first_choice.get("message"))

        input_messages = _messages(_as_list(payload.get("messages")))
        if message:
            output_messages = _messages(
                [message], finish_reason=first_choice.get("finish_reason")
            )
        elif isinstance(first_choice.get("text"), str):
            output_messages = [
                InferenceMessage(
                    role="assistant",
                    parts=[TextPart(content=first_choice["text"])],
                    finish_reason=_optional_text(first_choice.get("finish_reason")),
                )
            ]
        elif _as_list(response.get("output")):
            output_messages = _messages(_as_list(response.get("output")))
        elif response_text:
            output_messages = [
                InferenceMessage(
                    role="assistant", parts=[TextPart(content=response_text)]
                )
            ]
        else:
            output_messages = []

        # Tokens may be under a top-level ``usage`` (shaped) or under
        # ``response.usage`` / top-level keys (StandardLoggingPayload). A
        # malformed or empty top-level usage falls through to response.usage.
        usage = _as_dict(payload.get("usage")) or _as_dict(response.get("usage"))

        # The proxy-assigned call id — the idempotency identity for a
        # redelivered/retried callback POST. Fall back to the response's own
        # completion id; a payload with neither stays keyless — model_call_id
        # None means no dedup key under uq_inference_calls_model_call.
        # _usable_id, not truthiness: model_call_id is the dedup key, and a
        # truthy non-string (True → "True") or a blank string would be a
        # shared key that collapses every later call as a redelivery.
        model_call_id = payload.get("litellm_call_id")
        if not _usable_id(model_call_id):
            model_call_id = response.get("id")
        if not _usable_id(model_call_id):
            model_call_id = None

        model = _optional_text(payload.get("model")) or _optional_text(
            response.get("model")
        )
        return InferenceCall(
            session_id=session_id,
            user_id=user_id,
            org_id=org_id,
            gateway_provider=GatewayProvider.LITELLM,
            model_provider=_optional_text(payload.get("custom_llm_provider")),
            model=model,
            input_messages=input_messages,
            output_messages=output_messages,
            model_call_id=model_call_id,
            input_tokens=_first_int(
                usage.get("prompt_tokens"), payload.get("prompt_tokens")
            ),
            output_tokens=_first_int(
                usage.get("completion_tokens"), payload.get("completion_tokens")
            ),
            duration_ms=self._latency_ms(payload),
            raw=payload,
            **capture_fields,
        )

    @staticmethod
    def _latency_ms(payload: dict[str, Any]) -> int | None:
        """Resolve latency in ms across the shapes LiteLLM emits. Each
        branch is taken only when its value is a storable numeric, so a
        malformed field (non-numeric, NaN/inf, beyond int64) falls through
        to the next shape instead of zeroing the latency or raising."""
        ms = _int_in_bounds(payload.get("response_time_ms"))
        if ms is not None:
            return ms
        seconds = payload.get("response_time")
        if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
            ms = _int_in_bounds(seconds * 1000)
            if ms is not None:
                return ms
        start, end = payload.get("startTime"), payload.get("endTime")  # unix seconds
        if (
            isinstance(start, (int, float))
            and isinstance(end, (int, float))
            and not isinstance(start, bool)
            and not isinstance(end, bool)
        ):
            ms = _int_in_bounds((end - start) * 1000)
            if ms is not None:
                return ms
        return None


# Only fully-implemented adapters are registered: an enum provider with no
# entry here (portkey, helicone, unknown) gets a clean 400 from the ingest
# route. A new provider is one adapter class + an entry; the contract is the
# route's call shape — normalize(payload, *, session_id, user_id, org_id).
ADAPTERS = {
    GatewayProvider.LITELLM: LiteLLMAdapter(),
}
