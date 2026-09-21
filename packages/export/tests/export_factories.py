# SPDX-License-Identifier: AGPL-3.0-or-later
"""Canonical inference-call factories shared by export tests."""

from __future__ import annotations

from datetime import datetime

from sediment_core import (
    GatewayProvider,
    InferenceCall,
    InferenceMessage,
    TextPart,
    ToolCallPart,
)


def message(role: str, content: str) -> InferenceMessage:
    return InferenceMessage(role=role, parts=[TextPart(content=content)])


def tool_call(call_id: str, name: str, arguments: dict) -> ToolCallPart:
    return ToolCallPart(id=call_id, name=name, arguments=arguments)


def inference_call(
    inference_call_id: str | None,
    *,
    org_id: str,
    session_id: str,
    user_id: str | None = "dev",
    model: str | None = "claude-sonnet-5",
    input_messages: list[InferenceMessage] | None = None,
    output: str = "done",
    tool_calls: list[ToolCallPart] | None = None,
    model_call_id: str | None = None,
    observed_at: datetime | None = None,
    gateway_provider: GatewayProvider = GatewayProvider.LITELLM,
) -> InferenceCall:
    values = dict(
        org_id=org_id,
        session_id=session_id,
        user_id=user_id,
        gateway_provider=gateway_provider,
        model=model,
        input_messages=input_messages or [],
        output_messages=[
            InferenceMessage(
                role="assistant",
                parts=[TextPart(content=output), *(tool_calls or [])],
            )
        ],
        input_tokens=10,
        output_tokens=20,
        duration_ms=50,
        model_call_id=model_call_id,
    )
    if inference_call_id is not None:
        values["inference_call_id"] = inference_call_id
    if observed_at is not None:
        values["observed_at"] = observed_at
    return InferenceCall(**values)
