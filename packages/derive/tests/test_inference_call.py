# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure views over canonical inference-call facts."""

from sediment_core import (
    GatewayProvider,
    InferenceCall,
    InferenceMessage,
    ReasoningPart,
    TextPart,
    ToolCallPart,
    ToolCallResponsePart,
)
from sediment_derive import inference_prompt_key, model_call_ids, render_scoring_text


def test_scoring_text_renders_model_output_without_persisted_flattening() -> None:
    call = InferenceCall(
        org_id="acme",
        session_id="session-1",
        gateway_provider=GatewayProvider.LITELLM,
        input_messages=[],
        output_messages=[
            InferenceMessage(
                role="assistant",
                parts=[
                    ReasoningPart(content="Plan mentions secret_environment_output."),
                    TextPart(content="Applying the change."),
                    ToolCallPart(
                        id="tool-1",
                        name="apply_patch",
                        arguments={
                            "patch": "def add(a, b):\n    return a + b",
                            "path": "app.py",
                        },
                    ),
                ],
            ),
            InferenceMessage(
                role="tool",
                parts=[
                    ToolCallResponsePart(
                        id="tool-1", result={"secret_environment_output": "omit"}
                    )
                ],
            ),
        ],
        model_call_id="model-1",
    )

    assert render_scoring_text(call) == (
        "Applying the change.\ndef add(a, b):\n    return a + b\napp.py"
    )
    assert model_call_ids(call) == {"model-1", "tool-1"}
    assert "completion" not in call.model_dump()


def test_reasoning_changes_structural_prompt_identity() -> None:
    def call(reasoning: str) -> InferenceCall:
        return InferenceCall(
            org_id="acme",
            session_id="session-1",
            gateway_provider=GatewayProvider.LITELLM,
            input_messages=[
                InferenceMessage(
                    role="assistant", parts=[ReasoningPart(content=reasoning)]
                )
            ],
            output_messages=[],
        )

    assert inference_prompt_key(call("Inspect callers.")) != inference_prompt_key(
        call("Inspect only this route.")
    )


def test_prompt_view_preserves_exceptional_canonical_values():
    import math
    from sediment_derive.inference_call import inference_prompt

    call = InferenceCall(
        org_id="acme",
        session_id="s",
        gateway_provider=GatewayProvider.LITELLM,
        input_messages=[
            InferenceMessage(
                role="assistant",
                parts=[
                    TextPart(content="\ud800"),
                    ToolCallPart(id="t", name="run", arguments={"n": float("nan")}),
                ],
            )
        ],
        output_messages=[],
    )
    first = inference_prompt(call)
    assert first[0]["parts"][0]["content"] == "\ud800"
    assert math.isnan(first[0]["parts"][1]["arguments"]["n"])
    assert inference_prompt(call) == first


def test_prompt_keys_preserve_types_and_totally_order_native_values():
    import math

    def key(value):
        call = InferenceCall(
            org_id="acme",
            session_id="s",
            gateway_provider=GatewayProvider.LITELLM,
            input_messages=[
                InferenceMessage(
                    role="assistant",
                    parts=[
                        ToolCallPart(id="t", name="run", arguments={"value": value})
                    ],
                )
            ],
            output_messages=[],
        )
        return inference_prompt_key(call)

    values = [
        None,
        False,
        0,
        0.0,
        -0.0,
        "0",
        [],
        {},
        [1],
        {"a": 1},
        float("nan"),
        float("inf"),
        -float("inf"),
        "\ud800",
        "🪨",
        "\ud83e\udea8",
    ]
    keys = [key(value) for value in values]
    assert len(set(keys)) == len(values)
    assert sorted(keys) == sorted(reversed(keys))
    assert key(float("nan")) == key(math.nan)
    assert key({"a": 1, "b": 2}) == key({"b": 2, "a": 1})
