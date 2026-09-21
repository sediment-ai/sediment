# SPDX-License-Identifier: AGPL-3.0-or-later
"""Canonical inference-call to trainer-conversation contract tests."""

from __future__ import annotations

import json

import pytest
from pathlib import Path
from typing import Never, get_args, get_type_hints

from sediment_capture import LiteLLMAdapter
from sediment_core import TextPart

from sediment_export.trainer import map_inference_call
from sediment_export import diff_sft, dpo, sft, trainer

RESPONSES_FIXTURE = (
    Path(__file__).parents[2]
    / "capture/tests/fixtures/litellm_responses_standard_logging_object.json"
)


def test_training_rows_use_closed_trainer_message_contracts() -> None:
    trainer_message = getattr(trainer, "TrainerMessage", None)
    trainer_assistant_message = getattr(trainer, "TrainerAssistantMessage", None)

    assert trainer_message is not None
    assert trainer_assistant_message is not None

    contracts = (
        (trainer.TrainerConversation, "prompt", trainer_message),
        (trainer.TrainerConversation, "completion", trainer_assistant_message),
        (dpo.DPOPair, "prompt", trainer_message),
        (dpo.DPOPair, "chosen", trainer_assistant_message),
        (dpo.DPOPair, "rejected", trainer_assistant_message),
        (sft.SFTSample, "prompt", trainer_message),
        (sft.SFTSample, "completion", trainer_assistant_message),
        (diff_sft.DiffSFTSample, "prompt", trainer_message),
        (diff_sft.DiffSFTSample, "completion", trainer_assistant_message),
    )
    for row_type, field_name, item_type in contracts:
        annotation = get_type_hints(row_type)[field_name]
        assert get_args(annotation) == (item_type,)

    for row_type in (
        trainer.TrainerConversation,
        dpo.DPOPair,
        sft.SFTSample,
        diff_sft.DiffSFTSample,
    ):
        annotation = get_type_hints(row_type)["tools"]
        assert get_args(annotation) == (Never,)


def test_responses_fixture_preserves_developer_prompt_role() -> None:
    payload = json.loads(RESPONSES_FIXTURE.read_text())
    inference_call = LiteLLMAdapter().normalize(
        payload, session_id="session-1", user_id="developer-1", org_id="acme"
    )
    source_message = inference_call.input_messages[0]
    assert source_message.role == "developer"
    assert all(isinstance(part, TextPart) for part in source_message.parts)

    conversation = map_inference_call(inference_call)

    assert conversation.prompt[0] == {
        "role": "developer",
        "content": "".join(part.content for part in source_message.parts),
    }
    assert conversation.completion


@pytest.mark.parametrize(
    "value,reason",
    [
        (float("nan"), "non_finite_number"),
        (float("inf"), "non_finite_number"),
        (-float("inf"), "non_finite_number"),
        ("bad\ud800", "unrepresentable_unicode"),
        ({"\udfff": "key"}, "unrepresentable_unicode"),
    ],
)
def test_mapping_declines_nested_unrepresentable_values(value, reason):
    from export_factories import inference_call, message, tool_call

    call = inference_call(
        "call",
        org_id="acme",
        session_id="session",
        input_messages=[message("user", "valid")],
        tool_calls=[tool_call("tool", "run", {"nested": [value]})],
    )
    with pytest.raises(trainer.TrainerMappingError) as error:
        map_inference_call(call)
    assert error.value.reason == reason


def test_mapping_preserves_finite_unicode_nul_and_omits_raw():
    from export_factories import inference_call, message, tool_call

    call = inference_call(
        "call",
        org_id="acme",
        session_id="session",
        input_messages=[message("user", "é\x00🪨")],
        tool_calls=[tool_call("tool", "run", {"nested": [0, -1, 1.5, None, ""]})],
    )
    call = call.model_copy(update={"raw": {"unused": float("nan"), "\ud800": ""}})
    mapped = map_inference_call(call)
    assert mapped.prompt[0]["content"] == "é\x00🪨"
    assert mapped.completion[0]["tool_calls"][0]["function"]["arguments"] == {
        "nested": [0, -1, 1.5, None, ""]
    }


def test_all_projectors_compose_representation_skip_reasons():
    from sediment_export import recovery, rlvr

    for vocabulary in (
        dpo.DPO_SKIP_REASONS,
        sft.SFT_SKIP_REASONS,
        diff_sft.DIFF_SFT_SKIP_REASONS,
        recovery.RECOVERY_PROJECTION_SKIP_REASONS,
        rlvr.SEDIMENT_TASK_SKIP_REASONS,
        rlvr.SWE_BENCH_SKIP_REASONS,
        rlvr.ROLLOUT_SKIP_REASONS,
    ):
        assert {"non_finite_number", "unrepresentable_unicode"} <= set(vocabulary)
        assert len(vocabulary) == len(set(vocabulary))


@pytest.mark.parametrize("role", ["user", "tool"])
@pytest.mark.parametrize(
    "result", ["verbatim", {"b": [1, False, None], "a": "é"}, [], None]
)
def test_native_tool_results_preserve_role_identity_and_json_values(role, result):
    from export_factories import inference_call, message, tool_call
    from sediment_core import InferenceMessage, ToolCallResponsePart

    call = inference_call(
        "call",
        org_id="acme",
        session_id="session",
        input_messages=[
            message("user", "inspect"),
            InferenceMessage(role="assistant", parts=[tool_call("t", "inspect", {})]),
            InferenceMessage(
                role=role, parts=[ToolCallResponsePart(id="t", result=result)]
            ),
        ],
    )
    before = call.model_dump()
    result_message = map_inference_call(call).prompt[-1]
    assert result_message["role"] == "tool"
    assert result_message["tool_call_id"] == "t"
    assert result_message["name"] == "inspect"
    if isinstance(result, str):
        assert result_message["content"] == result
    else:
        assert json.loads(result_message["content"]) == result
    assert call.model_dump() == before


def test_user_text_and_tool_results_keep_part_order():
    from export_factories import inference_call, tool_call
    from sediment_core import InferenceMessage, ToolCallResponsePart

    call = inference_call(
        "call",
        org_id="acme",
        session_id="session",
        input_messages=[
            InferenceMessage(role="assistant", parts=[tool_call("t", "inspect", {})]),
            InferenceMessage(
                role="user",
                parts=[
                    TextPart(content="before"),
                    ToolCallResponsePart(id="t", result="result"),
                    TextPart(content="after"),
                    TextPart(content=" end"),
                ],
            ),
        ],
    )
    assert map_inference_call(call).prompt[1:] == [
        {"role": "user", "content": "before"},
        {"role": "tool", "name": "inspect", "tool_call_id": "t", "content": "result"},
        {"role": "user", "content": "after end"},
    ]


@pytest.mark.parametrize(
    "result,reason",
    [(float("nan"), "non_finite_number"), ("\ud800", "unrepresentable_unicode")],
)
def test_result_serialization_never_hides_exceptional_scalars(result, reason):
    from export_factories import inference_call, tool_call
    from sediment_core import InferenceMessage, ToolCallResponsePart

    call = inference_call(
        "call",
        org_id="acme",
        session_id="session",
        input_messages=[
            InferenceMessage(role="assistant", parts=[tool_call("t", "inspect", {})]),
            InferenceMessage(
                role="user",
                parts=[ToolCallResponsePart(id="t", result={"value": result})],
            ),
        ],
    )
    with pytest.raises(trainer.TrainerMappingError, match=reason):
        map_inference_call(call)


@pytest.mark.parametrize("result", [{1: "coerced key"}, ("coerced tuple",)])
def test_tool_result_rejects_non_json_values_instead_of_coercing(result):
    from sediment_export.trainer import TrainerMappingError, tool_result_text

    with pytest.raises(TrainerMappingError, match="non_string_tool_result"):
        tool_result_text(result)
