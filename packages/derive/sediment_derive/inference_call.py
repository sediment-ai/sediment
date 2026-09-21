# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure views over canonical inference-call facts."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sediment_core import (
    GatewayProvider,
    InferenceCall,
    InferenceMessage,
    ReasoningPart,
    TextPart,
    ToolCallPart,
)

from sediment_core.store import InferenceCallIdentity


def inference_fact_id(call: InferenceCall | InferenceCallIdentity) -> str:
    """Return the immutable inference-call fact id."""
    return call.inference_call_id


def inference_observed_at(call: InferenceCall) -> datetime:
    """Return the source observation time."""
    return call.observed_at


def _string_leaves(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for key in sorted(value) for text in _string_leaves(value[key])]
    if isinstance(value, list):
        return [text for item in value for text in _string_leaves(item)]
    return []


def render_scoring_text(call: InferenceCall) -> str:
    """Render model-produced text for similarity scoring.

    Rendering includes assistant text and string leaves from tool
    arguments in message-part order. It excludes readable reasoning because a
    plan can restate code before the model writes it. It excludes tool-call
    responses because those are environment output, not model output. The
    rendered view is never persisted.
    """
    text: list[str] = []
    for message in call.output_messages:
        for part in message.parts:
            if isinstance(part, TextPart):
                text.append(part.content)
            elif isinstance(part, ReasoningPart):
                continue
            elif isinstance(part, ToolCallPart):
                text.extend(_string_leaves(part.arguments))
    return "\n".join(item for item in text if item)


def model_call_ids(call: InferenceCall | InferenceCallIdentity) -> set[str]:
    """Return explicit provider-call and response tool-call join ids."""
    if isinstance(call, InferenceCallIdentity):
        return set(call.call_ids)
    ids = {
        part.id
        for message in call.output_messages
        for part in message.parts
        if isinstance(part, ToolCallPart)
    }
    if call.model_call_id is not None:
        ids.add(call.model_call_id)
    return ids


def inference_input_messages(
    call: InferenceCall,
) -> list[InferenceMessage]:
    """Return the structured request history."""
    return list(call.input_messages)


def inference_tool_calls(call: InferenceCall) -> list[ToolCallPart]:
    """Return response-side tool calls without request-history echoes."""
    return [
        part
        for message in call.output_messages
        for part in message.parts
        if isinstance(part, ToolCallPart)
    ]


def inference_model(call: InferenceCall) -> str | None:
    """Return the recorded model name without inventing an absent value."""
    return call.model


def inference_gateway_provider(call: InferenceCall) -> GatewayProvider:
    """Return the gateway that observed the call."""
    return call.gateway_provider


def inference_user_id(call: InferenceCall) -> str | None:
    """Return the recorded user id without inventing an absent value."""
    return call.user_id


def inference_prompt(call: InferenceCall) -> list[dict[str, Any]]:
    """Return native prompt values without JSON-mode scalar coercion."""
    return [
        message.model_dump(mode="python", exclude_none=True)
        for message in call.input_messages
    ]


def inference_prompt_key(call: InferenceCall) -> tuple[str]:
    """Return a typed, totally ordered structural key for DPO membership.

    JSON syntax distinguishes containers, booleans, integers, and floats. Its
    numeric extensions identify equal non-finite categories before training
    eligibility declines the pair. This key stays in memory: unescaped Unicode
    distinguishes surrogate code points from an actual astral character.
    """
    return (
        json.dumps(
            inference_prompt(call), ensure_ascii=False, allow_nan=True, sort_keys=True
        ),
    )
