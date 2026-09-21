# SPDX-License-Identifier: AGPL-3.0-or-later
"""Malformed source tags decline only their own record or content part."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sediment_capture import (
    LiteLLMAdapter,
    parse_otlp_decisions,
    parse_pull_request_revision,
)
from sediment_core import ReasoningPart, TextPart

ABSENT = object()
INVALID_TAGS = [ABSENT, None, "unsupported", 12, True, [], {}]


def _decision_record(source, call_id, field=None, value=None):
    attrs = {
        "tool_name": "Edit" if source == "claude-code" else "apply_patch",
        "decision": "accept" if source == "claude-code" else "approved",
        "source": "user_temporary" if source == "claude-code" else "User",
        "session.id": "session-1",
        "conversation.id": "session-1",
        "tool_use_id": call_id,
        "call_id": call_id,
        "event.name": "codex.tool_decision",
    }
    if field is not None:
        if value is ABSENT:
            del attrs[field]
        else:
            attrs[field] = value
    return {
        "timeUnixNano": "1783542925478460000",
        "body": {
            "stringValue": "claude_code.tool_decision"
            if source == "claude-code"
            else ""
        },
        # Deliberately malformed AnyValue strings preserve the wrong source type.
        "attributes": [
            {"key": key, "value": {"stringValue": item}} for key, item in attrs.items()
        ],
    }


@pytest.mark.parametrize("value", INVALID_TAGS)
@pytest.mark.parametrize(
    "source,field",
    [
        ("claude-code", "tool_name"),
        ("claude-code", "decision"),
        ("claude-code", "source"),
        ("codex", "tool_name"),
        ("codex", "decision"),
    ],
)
def test_otlp_discriminator_preserves_valid_sibling(source, field, value, caplog):
    payload = {
        "resourceLogs": [
            {
                "scopeLogs": [
                    {
                        "logRecords": [
                            _decision_record(source, "bad", field, value),
                            _decision_record(source, "good"),
                        ]
                    }
                ]
            }
        ]
    }
    caplog.set_level("INFO")

    decisions = parse_otlp_decisions(payload, org_id="acme")

    by_call = {decision.call_id: decision for decision in decisions}
    assert by_call["good"].accepted is True
    assert by_call["good"].explicit is True
    if field == "source":
        assert set(by_call) == {"bad", "good"}
        assert by_call["bad"].explicit is False
    else:
        assert set(by_call) == {"good"}
    expected = (
        "unsupported_discriminator"
        if isinstance(value, str)
        else "malformed_discriminator"
    )
    assert any(
        getattr(record, "reason", None) == expected
        and getattr(record, "source", None) == source
        and getattr(record, "record_position", None) == 0
        for record in caplog.records
    )


@pytest.mark.parametrize("value", INVALID_TAGS)
@pytest.mark.parametrize(
    "container", ["content", "thinking_blocks", "response_item", "tool_calls"]
)
def test_gateway_discriminator_preserves_supported_content(value, container, caplog):
    invalid = {"type": value} if value is not ABSENT else {}
    message = {"role": "assistant", "content": [{"type": "text", "text": "kept"}]}
    payload = {"messages": [], "response": {"choices": [{"message": message}]}}
    if container == "content":
        message["content"].insert(0, invalid)
    elif container == "thinking_blocks":
        message["thinking_blocks"] = [
            invalid,
            {"type": "thinking", "thinking": "kept reasoning"},
        ]
    elif container == "tool_calls":
        invalid.update(id="tool", function={"name": "Edit", "arguments": {}})
        message["tool_calls"] = [invalid]
    else:
        # Explicit invalid tags can't silently become ordinary chat messages.
        invalid.update(role="assistant", content="unsupported item")
        payload["response"] = {"output": [invalid, message]}
    caplog.set_level("INFO")

    fact = LiteLLMAdapter().normalize(
        payload, org_id="acme", session_id="s", user_id=None
    )

    if container == "response_item" and value in (ABSENT, None):
        assert len(fact.output_messages) == 2  # Chat messages don't require a type tag.
        return
    if container == "tool_calls" and value in (ABSENT, None):
        assert len(fact.output_messages[0].parts) == 2
        assert fact.output_messages[0].parts[1].id == "tool"
        return
    assert len(fact.output_messages) == 1
    expected_parts = [TextPart(content="kept")]
    if container == "thinking_blocks":
        expected_parts.insert(0, ReasoningPart(content="kept reasoning"))
    assert fact.output_messages[0].parts == expected_parts
    expected = (
        "unsupported_discriminator"
        if isinstance(value, str)
        else "malformed_discriminator"
    )
    assert any(
        getattr(record, "reason", None) == expected
        and getattr(record, "source", None) == "litellm"
        and getattr(record, "record_position", None) == 0
        for record in caplog.records
    )


@pytest.mark.parametrize("value", INVALID_TAGS)
def test_github_action_discriminator_declines_without_raising(value, caplog):
    payload = {} if value is ABSENT else {"action": value}
    caplog.set_level("INFO")

    fact, reason = parse_pull_request_revision(payload, org_id="acme")

    assert fact is None
    assert reason.value == "unsupported_pull_request_action"
    expected = (
        "unsupported_discriminator"
        if isinstance(value, str)
        else "malformed_discriminator"
    )
    assert any(
        getattr(record, "reason", None) == expected
        and getattr(record, "source", None) == "github"
        for record in caplog.records
    )


def test_github_supported_action_still_captures_revision():
    path = Path(__file__).parent / "fixtures/github_pull_request.json"
    payload = json.loads(path.read_text())
    payload["action"] = "opened"

    fact, reason = parse_pull_request_revision(payload, org_id="acme")

    assert reason is None
    assert fact is not None
    assert fact.head_sha == payload["pull_request"]["head"]["sha"]


@pytest.mark.parametrize(
    "parser,filename,field",
    [
        ("push", "github_push.json", "ref"),
        ("workflow_run", "github_workflow_run.json", "id"),
    ],
)
@pytest.mark.parametrize("invalid", ["\x00", "\ud800"])
def test_github_invalid_fact_identity_declines_before_storage(
    parser, filename, field, invalid, caplog
):
    from sediment_capture import parse_push, parse_workflow_run

    payload = json.loads((Path(__file__).parent / "fixtures" / filename).read_text())
    if parser == "push":
        payload[field] = "refs/heads/" + invalid
        translate = parse_push
    else:
        payload["workflow_run"][field] = invalid
        translate = parse_workflow_run
    caplog.set_level("INFO")

    assert translate(payload, org_id="acme") is None
    assert any(
        getattr(record, "reason", None) == "invalid_fact"
        and getattr(record, "source", None) == "github"
        for record in caplog.records
    )
