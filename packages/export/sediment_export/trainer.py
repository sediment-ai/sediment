# SPDX-License-Identifier: AGPL-3.0-or-later
"""Map canonical inference-call messages to trainer-facing conversations."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, fields, is_dataclass
from typing import Any, Literal, Never, Required, TypedDict, cast

from pydantic import BaseModel
from sediment_core import (
    InferenceCall,
    InferenceMessage,
    ReasoningPart,
    TextPart,
    ToolCallPart,
    ToolCallResponsePart,
)

TrainingRepresentationSkipReason = Literal[
    "non_finite_number", "unrepresentable_unicode"
]
TRAINING_REPRESENTATION_SKIP_REASONS: tuple[TrainingRepresentationSkipReason, ...] = (
    "non_finite_number",
    "unrepresentable_unicode",
)

TrainerSkipReason = (
    TrainingRepresentationSkipReason
    | Literal[
        "completionless",
        "duplicate_tool_call_id",
        "empty_message",
        "non_string_tool_result",
        "unrepresentable_part_order",
        "unresolved_tool_call",
        "unsupported_completion_role",
        "unsupported_message_role",
        "unsupported_role_part",
    ]
)

TRAINER_SKIP_REASONS: tuple[TrainerSkipReason, ...] = (
    *TRAINING_REPRESENTATION_SKIP_REASONS,
    "completionless",
    "duplicate_tool_call_id",
    "empty_message",
    "non_string_tool_result",
    "unrepresentable_part_order",
    "unresolved_tool_call",
    "unsupported_completion_role",
    "unsupported_message_role",
    "unsupported_role_part",
)


class TrainerMappingError(ValueError):
    """A canonical message can't be represented without changing its meaning."""

    def __init__(self, reason: TrainerSkipReason) -> None:
        self.reason = reason
        super().__init__(reason)


def validate_training_representation(value: object) -> None:
    """Decline exceptional scalars in an emitted row without changing values.

    Inspect Python values before Pydantic's JSON serialization can coerce them.
    Projectors pass their row, so omitted Fact content has no eligibility effect.
    This is an eligibility check, not an arbitrary-object serialization adapter.
    """
    if isinstance(value, float) and not math.isfinite(value):
        raise TrainerMappingError("non_finite_number")
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise TrainerMappingError("unrepresentable_unicode") from exc
    elif isinstance(value, BaseModel):
        validate_training_representation(value.model_dump(mode="python"))
    elif is_dataclass(value) and not isinstance(value, type):
        for item in fields(value):
            validate_training_representation(getattr(value, item.name))
    elif isinstance(value, dict):
        for key, item in value.items():
            validate_training_representation(key)
            validate_training_representation(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            validate_training_representation(item)


class TrainerFunctionCall(TypedDict):
    """The function payload nested under one trainer-facing tool call."""

    name: str
    arguments: dict[str, Any]


class TrainerToolCall(TypedDict):
    """One structured function call in an assistant message."""

    id: str
    type: Literal["function"]
    function: TrainerFunctionCall


class TrainerTextMessage(TypedDict):
    """One developer, system, or user message."""

    role: Literal["developer", "system", "user"]
    content: str


class TrainerAssistantContentMessage(TypedDict, total=False):
    """An assistant message with visible response text."""

    role: Required[Literal["assistant"]]
    content: Required[str]
    thinking: str
    tool_calls: list[TrainerToolCall]


class TrainerAssistantThinkingMessage(TypedDict, total=False):
    """An assistant message with readable reasoning."""

    role: Required[Literal["assistant"]]
    thinking: Required[str]
    content: str
    tool_calls: list[TrainerToolCall]


class TrainerAssistantToolCallMessage(TypedDict, total=False):
    """An assistant message with at least one structured tool call."""

    role: Required[Literal["assistant"]]
    tool_calls: Required[list[TrainerToolCall]]
    thinking: str
    content: str


type TrainerAssistantMessage = (
    TrainerAssistantContentMessage
    | TrainerAssistantThinkingMessage
    | TrainerAssistantToolCallMessage
)


class TrainerToolMessage(TypedDict):
    """One string result for an earlier trainer-facing tool call."""

    role: Literal["tool"]
    name: str
    tool_call_id: str
    content: str


type TrainerMessage = TrainerTextMessage | TrainerAssistantMessage | TrainerToolMessage


@dataclass(frozen=True)
class TrainerConversation:
    """One inference call split at the trainer's prompt/completion boundary."""

    prompt: list[TrainerMessage]
    completion: list[TrainerAssistantMessage]
    tools: list[Never]


def _tool_call(part: ToolCallPart) -> TrainerToolCall:
    return {
        "id": part.id,
        "type": "function",
        "function": {"name": part.name, "arguments": part.arguments},
    }


def tool_result_text(result: object) -> str:
    """Preserve strings verbatim and encode structured results as strict JSON.

    A JSON tree is tool output, not a collection of message parts to flatten.
    Validate before encoding so escapes cannot hide an ineligible scalar.
    """
    validate_training_representation(result)
    if isinstance(result, str):
        return result
    try:
        encoded = json.dumps(
            result,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if json.loads(encoded) != result:
            raise ValueError("tool result is not a JSON value")
        return encoded
    except (TypeError, ValueError) as exc:
        raise TrainerMappingError("non_string_tool_result") from exc


def _map_messages(
    messages: list[InferenceMessage],
    tool_names: dict[str, str],
    *,
    completion: bool,
) -> list[TrainerMessage]:
    mapped: list[TrainerMessage] = []
    for message in messages:
        if not message.parts:
            raise TrainerMappingError("empty_message")
        if completion and message.role != "assistant":
            raise TrainerMappingError("unsupported_completion_role")

        response_parts = [
            part for part in message.parts if isinstance(part, ToolCallResponsePart)
        ]
        if response_parts:
            if message.role not in {"tool", "user"}:
                raise TrainerMappingError("unsupported_role_part")
            text: list[str] = []
            for part in message.parts:
                if isinstance(part, TextPart) and message.role == "user":
                    text.append(part.content)
                    continue
                if not isinstance(part, ToolCallResponsePart):
                    raise TrainerMappingError("unrepresentable_part_order")
                if text:
                    mapped.append({"role": "user", "content": "".join(text)})
                    text.clear()
                name = tool_names.get(part.id)
                if name is None:
                    raise TrainerMappingError("unresolved_tool_call")
                mapped.append(
                    {
                        "role": "tool",
                        "name": name,
                        "tool_call_id": part.id,
                        "content": tool_result_text(part.result),
                    }
                )
            if text:
                mapped.append({"role": "user", "content": "".join(text)})
            continue

        if message.role not in {"developer", "system", "user", "assistant"}:
            raise TrainerMappingError("unsupported_message_role")

        if message.role != "assistant":
            if not all(isinstance(part, TextPart) for part in message.parts):
                raise TrainerMappingError("unsupported_role_part")
            mapped.append(
                {
                    "role": message.role,
                    "content": "".join(part.content for part in message.parts),
                }
            )
            continue

        phase = -1
        text: list[str] = []
        thinking: list[str] = []
        tool_calls: list[TrainerToolCall] = []
        for part in message.parts:
            if isinstance(part, ReasoningPart):
                part_phase = 0
                thinking.append(part.content)
            elif isinstance(part, TextPart):
                part_phase = 1
                text.append(part.content)
            elif isinstance(part, ToolCallPart):
                part_phase = 2
                if part.id in tool_names:
                    raise TrainerMappingError("duplicate_tool_call_id")
                tool_names[part.id] = part.name
                tool_calls.append(_tool_call(part))
            else:  # pragma: no cover - the closed Pydantic union owns this guard
                raise TrainerMappingError("unsupported_role_part")
            if part_phase < phase:
                raise TrainerMappingError("unrepresentable_part_order")
            phase = part_phase

        trainer_message: dict[str, object] = {"role": "assistant"}
        if thinking:
            trainer_message["thinking"] = "".join(thinking)
        if text:
            trainer_message["content"] = "".join(text)
        if tool_calls:
            trainer_message["tool_calls"] = tool_calls
        mapped.append(cast(TrainerAssistantMessage, trainer_message))
    return mapped


def map_inference_prompt(inference_call: InferenceCall) -> list[TrainerMessage]:
    """Map only the prompt when a recipe supplies its own completion."""
    prompt = _map_messages(inference_call.input_messages, {}, completion=False)
    validate_training_representation(prompt)
    return prompt


def map_inference_call(inference_call: InferenceCall) -> TrainerConversation:
    """Map one canonical call without flattening or fabricating message content."""
    tool_names: dict[str, str] = {}
    prompt = _map_messages(inference_call.input_messages, tool_names, completion=False)
    completion = cast(
        list[TrainerAssistantMessage],
        _map_messages(inference_call.output_messages, tool_names, completion=True),
    )
    if not completion:
        raise TrainerMappingError("completionless")
    # Canonical inference calls don't carry tool definitions. An empty list is
    # explicit and doesn't invent schemas from observed calls.
    conversation = TrainerConversation(prompt=prompt, completion=completion, tools=[])
    validate_training_representation(conversation)
    return conversation
