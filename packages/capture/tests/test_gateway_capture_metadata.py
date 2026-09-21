# SPDX-License-Identifier: AGPL-3.0-or-later
"""Prepared capture metadata enters the canonical Inference call once."""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from sediment_capture import LiteLLMAdapter

BOUNDARY = datetime(2026, 9, 11, tzinfo=UTC)
CAPTURE_ID = UUID("7754fe46-50db-45f9-93ef-5b3e99ab70b3")


def _normalize(**capture):
    return LiteLLMAdapter().normalize(
        {
            "litellm_call_id": "provider-call",
            "response": {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "native-tool",
                        "name": "Write",
                        "arguments": '{"file_path":"a.py"}',
                    }
                ]
            },
        },
        session_id="session",
        user_id=None,
        org_id=capture.pop("org_id", "acme"),
        **capture,
    )


def test_capture_metadata_preserves_source_ids_and_normalized_org_identity():
    first = _normalize(capture_id=CAPTURE_ID, observed_at=BOUNDARY)
    same = _normalize(capture_id=CAPTURE_ID, observed_at=BOUNDARY, org_id="ACME")
    assert first == same
    # Published capture identity must survive implementation upgrades.
    assert first.inference_call_id == "4f97fcfd-d833-59e0-8b67-a4348771aa25"
    assert first.inference_call_id != str(CAPTURE_ID)
    assert UUID(first.inference_call_id).version == 5
    assert first.observed_at == BOUNDARY
    assert first.session_id == "session"
    assert first.model_call_id == "provider-call"
    assert first.output_messages[0].parts[0].id == "native-tool"


def test_stamped_capture_preserves_response_id_fallback():
    call = LiteLLMAdapter().normalize(
        {"response": {"id": "native-response"}},
        session_id="session",
        user_id=None,
        org_id="acme",
        capture_id=CAPTURE_ID,
        observed_at=BOUNDARY,
    )
    assert call.model_call_id == "native-response"


@pytest.mark.parametrize(
    "capture",
    [
        {"capture_id": CAPTURE_ID},
        {"observed_at": BOUNDARY},
        {"capture_id": "bad", "observed_at": BOUNDARY},
        {"capture_id": CAPTURE_ID, "observed_at": BOUNDARY.replace(tzinfo=None)},
    ],
)
def test_adapter_rejects_incomplete_or_invalid_capture_metadata(capture):
    with pytest.raises(ValueError):
        _normalize(**capture)


def test_unstamped_adapter_call_preserves_receiver_time_and_generated_identity():
    before = datetime.now(UTC)
    first, second = _normalize(), _normalize()
    assert before <= first.observed_at <= second.observed_at <= datetime.now(UTC)
    assert first.inference_call_id != second.inference_call_id
