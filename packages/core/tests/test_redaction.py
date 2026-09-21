# SPDX-License-Identifier: AGPL-3.0-or-later
"""Basic credential redaction before facts reach PostgreSQL."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

import pytest
from sediment_core import (
    AgentHarness,
    CIOutcome,
    CIProvider,
    CIResult,
    EditObservation,
    GatewayProvider,
    InferenceCall,
    InferenceMessage,
    REDACTION_MARKER,
    RejectedEdit,
    RedactionReason,
    ReasoningPart,
    TextPart,
    ToolCallPart,
    ToolCallResponsePart,
    redact_fact,
)


def _inference_call(
    *,
    input_text: str = "hello",
    output_text: str = "world",
    raw: dict | None = None,
    **over,
) -> InferenceCall:
    base = dict(
        org_id="acme",
        session_id="sess-1",
        user_id="dev-1",
        gateway_provider=GatewayProvider.LITELLM,
        model="model-1",
        input_messages=[
            InferenceMessage(role="user", parts=[TextPart(content=input_text)])
        ],
        output_messages=[
            InferenceMessage(role="assistant", parts=[TextPart(content=output_text)])
        ],
        model_call_id="model-call-1",
        raw={} if raw is None else raw,
    )
    base.update(over)
    return InferenceCall(**base)


def test_redacts_nested_structured_and_text_credentials() -> None:
    authorization = "authorization-token-1234567890"
    api_key = "provider-key-1234567890123456"
    bare_bearer = "bare-bearer-token-1234567890"
    provider_key = "sk-proj-abcdefghijklmnopqrstuvwxyz"
    fact = _inference_call(
        raw={
            "headers": {"Authorization": f"Bearer {authorization}"},
            "nested": [
                {"provider_api_key": api_key},
                f"Bearer {bare_bearer}",
                provider_key,
            ],
        }
    )

    redacted, counts = redact_fact(fact)

    assert redacted.raw == {
        "headers": {"Authorization": f"Bearer {REDACTION_MARKER}"},
        "nested": [
            {"provider_api_key": REDACTION_MARKER},
            f"Bearer {REDACTION_MARKER}",
            REDACTION_MARKER,
        ],
    }
    assert counts == Counter(
        {
            RedactionReason.STRUCTURED_CREDENTIAL: 2,
            RedactionReason.BEARER_TOKEN: 1,
            RedactionReason.PROVIDER_KEY: 1,
        }
    )


def test_redacts_inference_parts_without_changing_identity() -> None:
    labeled = "labeled-api-key-1234567890"
    authorization = "authorization-value-1234567890"
    tool_token = "tool-bearer-token-1234567890"
    identity = "sk-proj-identitymustnotchange"
    fact = _inference_call(
        session_id=identity,
        user_id=identity,
        model=identity,
        model_call_id=identity,
        input_text=f"api_key = '{labeled}'",
        output_messages=[
            InferenceMessage(
                role="assistant",
                parts=[
                    TextPart(content=f"Authorization: Bearer {authorization}"),
                    ToolCallPart(
                        id=identity,
                        name="Write",
                        arguments={"content": f"Bearer {tool_token}"},
                    ),
                ],
            )
        ],
    )

    redacted, counts = redact_fact(fact)

    assert redacted.input_messages[0].parts[0].content == (
        f"api_key = '{REDACTION_MARKER}'"
    )
    assert redacted.output_messages[0].parts[0].content == (
        f"Authorization: Bearer {REDACTION_MARKER}"
    )
    assert redacted.output_messages[0].parts[1].arguments == {
        "content": f"Bearer {REDACTION_MARKER}"
    }
    assert redacted.session_id == identity
    assert redacted.user_id == identity
    assert redacted.model == identity
    assert redacted.model_call_id == identity
    assert redacted.output_messages[0].parts[1].id == identity
    assert counts == Counter(
        {
            RedactionReason.API_KEY_ASSIGNMENT: 1,
            RedactionReason.AUTHORIZATION_HEADER: 1,
            RedactionReason.BEARER_TOKEN: 1,
        }
    )


def test_redacts_readable_reasoning_without_dropping_the_part() -> None:
    credential = "reasoning-token-1234567890"
    fact = _inference_call(
        output_messages=[
            InferenceMessage(
                role="assistant",
                parts=[ReasoningPart(content=f"Authorization: Bearer {credential}")],
            )
        ]
    )

    redacted, counts = redact_fact(fact)

    assert redacted.output_messages[0].parts == [
        ReasoningPart(content=f"Authorization: Bearer {REDACTION_MARKER}")
    ]
    assert counts == Counter({RedactionReason.AUTHORIZATION_HEADER: 1})


def test_cross_part_credential_propagation_is_recursive() -> None:
    credential = "secret-from-tool"
    call = _inference_call(
        input_messages=[
            InferenceMessage(
                role="tool",
                parts=[
                    ToolCallResponsePart(
                        id="tool-1", result={"nested": [{"api_key": credential}]}
                    )
                ],
            )
        ],
        output_messages=[
            InferenceMessage(
                role="assistant",
                parts=[
                    TextPart(content=credential),
                    ToolCallPart(
                        id="tool-2",
                        name="write",
                        arguments={"nested": [credential]},
                    ),
                ],
            )
        ],
        raw={"echo": {"value": credential}},
    )

    redacted, counts = redact_fact(call)

    assert redacted.input_messages[0].parts[0].result == {
        "nested": [{"api_key": REDACTION_MARKER}]
    }
    assert redacted.output_messages[0].parts[0].content == REDACTION_MARKER
    assert redacted.output_messages[0].parts[1].arguments == {
        "nested": [REDACTION_MARKER]
    }
    assert redacted.raw == {"echo": {"value": REDACTION_MARKER}}
    assert counts == Counter({RedactionReason.STRUCTURED_CREDENTIAL: 4})


def test_redacts_applied_and_rejected_edit_text_without_changing_paths() -> None:
    at = datetime(2026, 8, 19, tzinfo=UTC)
    path = "sk-proj-pathmustnotchange"
    applied = EditObservation(
        org_id="acme",
        session_id="sess-1",
        user_id="dev-1",
        agent_harness=AgentHarness.CLAUDE_CODE,
        file_path=path,
        call_id="call-1",
        applied_text="api_key='original-key-1234567890'",
        observed_file_text="Bearer final-token-1234567890",
        occurred_at=at,
    )
    rejected = RejectedEdit(
        org_id="acme",
        session_id="sess-1",
        user_id="dev-1",
        agent_harness=AgentHarness.CLAUDE_CODE,
        file_path=path,
        call_id="call-2",
        proposed="sk-ant-api03-abcdefghijklmnopqrstuvwxyz",
        occurred_at=at,
    )

    redacted_applied, applied_counts = redact_fact(applied)
    redacted_rejected, rejected_counts = redact_fact(rejected)

    assert redacted_applied.applied_text == f"api_key='{REDACTION_MARKER}'"
    assert redacted_applied.observed_file_text == f"Bearer {REDACTION_MARKER}"
    assert redacted_applied.file_path == path
    assert applied_counts == Counter(
        {
            RedactionReason.API_KEY_ASSIGNMENT: 1,
            RedactionReason.BEARER_TOKEN: 1,
        }
    )
    assert redacted_rejected.proposed == REDACTION_MARKER
    assert redacted_rejected.file_path == path
    assert rejected_counts == Counter({RedactionReason.PROVIDER_KEY: 1})


def test_redacts_ci_reason_without_changing_run_identity() -> None:
    outcome = CIOutcome(
        org_id="acme",
        provider=CIProvider.JENKINS,
        run_id="sk-proj-runidentitymustnotchange",
        repo="acme/backend",
        commit_sha="a" * 40,
        branch="main",
        result=CIResult.ERROR,
        reason="Authorization: Bearer ci-reason-token-1234567890",
    )

    redacted, counts = redact_fact(outcome)

    assert redacted.reason == f"Authorization: Bearer {REDACTION_MARKER}"
    assert redacted.run_id == "sk-proj-runidentitymustnotchange"
    assert counts == Counter({RedactionReason.AUTHORIZATION_HEADER: 1})


def test_clamps_ci_reason_grown_past_max_length_by_redaction() -> None:
    reason = "a" * 4082 + '"api_key": "x"'
    assert len(reason) == 4096
    outcome = CIOutcome(
        org_id="acme",
        provider=CIProvider.GITHUB_ACTIONS,
        run_id="run-clamp",
        repo="acme/backend",
        commit_sha="a" * 40,
        branch="main",
        result=CIResult.FAILED,
        reason=reason,
    )

    redacted, counts = redact_fact(outcome)

    assert len(redacted.reason) <= 4096
    assert redacted.reason.endswith(REDACTION_MARKER)
    assert '"x"' not in redacted.reason
    assert counts == Counter({RedactionReason.STRUCTURED_CREDENTIAL: 1})
    assert redacted.run_id == "run-clamp"
    CIOutcome.model_validate(redacted.model_dump(mode="python"))
    re_redacted, second_counts = redact_fact(redacted)
    assert re_redacted.reason == redacted.reason
    assert second_counts == Counter()


def test_redaction_is_idempotent() -> None:
    fact = _inference_call(
        input_text="api_key='labeled-key-1234567890'",
        output_text="sk-proj-abcdefghijklmnopqrstuvwxyz",
        raw={
            "Authorization": "Bearer authorization-token-1234567890",
            "OPENAI-API-KEY": "provider-key-1234567890",
        },
    )

    redacted_once, first_counts = redact_fact(fact)
    redacted_twice, second_counts = redact_fact(redacted_once)

    assert redacted_twice == redacted_once
    assert first_counts == Counter(
        {
            RedactionReason.STRUCTURED_CREDENTIAL: 2,
            RedactionReason.PROVIDER_KEY: 1,
            RedactionReason.API_KEY_ASSIGNMENT: 1,
        }
    )
    assert second_counts == Counter()


def test_redaction_ignores_low_confidence_text() -> None:
    content = (
        "Bearer of bad news; Authorization: Bearer short; "
        "api_key='short'; sk-short; monkey=abcdefghijklmnopqrstuvwxyz; "
        'notapi_key=abcdefghijklmnopqrstuvwxyz; {"monkey":"short"}'
    )
    fact = _inference_call(
        input_text=content,
        output_text="abcdefghijklmnopqrstuvwxyz0123456789",
        raw={"turkey": "abcdefghijklmnopqrstuvwxyz0123456789"},
    )

    redacted, counts = redact_fact(fact)

    assert redacted == fact
    assert counts == Counter()


@pytest.mark.parametrize(
    ("content", "expected", "reason"),
    [
        (
            '{"api_key":"json-secret-1234567890"}',
            f'{{"api_key":"{REDACTION_MARKER}"}}',
            RedactionReason.STRUCTURED_CREDENTIAL,
        ),
        (
            '{"api_key":"short"}',
            f'{{"api_key":"{REDACTION_MARKER}"}}',
            RedactionReason.STRUCTURED_CREDENTIAL,
        ),
        (
            '"x-api-key": "header-secret-1234567890"',
            f'"x-api-key": "{REDACTION_MARKER}"',
            RedactionReason.STRUCTURED_CREDENTIAL,
        ),
        (
            "AZURE_OPENAI_API_KEY=azure-secret-1234567890",
            f"AZURE_OPENAI_API_KEY={REDACTION_MARKER}",
            RedactionReason.API_KEY_ASSIGNMENT,
        ),
        (
            "GOOGLE_GENERATIVE_AI_API_KEY='google-secret-1234567890'",
            f"GOOGLE_GENERATIVE_AI_API_KEY='{REDACTION_MARKER}'",
            RedactionReason.API_KEY_ASSIGNMENT,
        ),
    ],
)
def test_redacts_credential_assignments(
    content: str, expected: str, reason: RedactionReason
) -> None:
    redacted, counts = redact_fact(_inference_call(output_text=content))

    assert redacted.output_messages[0].parts[0].content == expected
    assert counts == Counter({reason: 1})


def test_redacts_quoted_json_authorization_with_spaces() -> None:
    fact = _inference_call(
        raw={
            "arguments": '{"authorization":"Digest username=\\"Mufasa\\", '
            'realm=\\"sediment-realm\\"",'
            '"proxy-authorization":"Digest abc 123"}'
        }
    )

    redacted, counts = redact_fact(fact)

    assert redacted.raw == {
        "arguments": f'{{"authorization":"{REDACTION_MARKER}",'
        f'"proxy-authorization":"{REDACTION_MARKER}"}}'
    }
    assert counts == Counter({RedactionReason.STRUCTURED_CREDENTIAL: 2})


def test_redacts_unterminated_quoted_values() -> None:
    fact = _inference_call(
        output_text="api_key='generic-secret-1234567890",
        raw={"arguments": '{"authorization":"Digest username=secret-value'},
    )

    redacted, counts = redact_fact(fact)
    redacted_twice, second_counts = redact_fact(redacted)

    assert redacted.output_messages[0].parts[0].content == (
        f"api_key='{REDACTION_MARKER}"
    )
    assert redacted.raw == {"arguments": f'{{"authorization":"{REDACTION_MARKER}'}
    assert counts == Counter(
        {
            RedactionReason.STRUCTURED_CREDENTIAL: 1,
            RedactionReason.API_KEY_ASSIGNMENT: 1,
        }
    )
    assert redacted_twice == redacted
    assert second_counts == Counter()
