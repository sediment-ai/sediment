# SPDX-License-Identifier: AGPL-3.0-or-later
"""Native consumer projections use full captured outputs and explicit task inputs."""

from datetime import UTC, datetime, timedelta
import copy

import pytest

from sediment_core import (
    InferenceMessage,
    ReasoningPart,
    ToolCallResponsePart,
    CIOutcome,
    CIProvider,
    CIResult,
)
from sediment_derive import AttributionSource, Provenance, Rollout, CommitRef
from sediment_derive.rollout import project_session_turns
from sediment_export import VerifierCommands
from sediment_export.rlvr import project_nemo_gym_rollouts
from export_factories import inference_call, message, tool_call


def _trajectory(*, reward=True, prefix=""):
    first = inference_call(
        f"{prefix}c1",
        org_id="acme",
        session_id=f"{prefix}session",
        input_messages=[message("user", "inspect")],
        observed_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    first = first.model_copy(
        update={
            "output_messages": [
                InferenceMessage(
                    role="assistant",
                    parts=[
                        ReasoningPart(content="verbatim reasoning"),
                        tool_call("t", "inspect", {"path": "a.py"}),
                    ],
                )
            ]
        }
    )
    second = inference_call(
        f"{prefix}c2",
        org_id="acme",
        session_id=f"{prefix}session",
        input_messages=[
            *first.input_messages,
            *first.output_messages,
            InferenceMessage(
                role="user", parts=[ToolCallResponsePart(id="t", result={"ok": True})]
            ),
            message("user", "steering request"),
        ],
        output="visible answer",
        observed_at=first.observed_at + timedelta(seconds=1),
    )
    segments, _, _ = project_session_turns([second, first], [])
    outcomes = (
        [
            CIOutcome(
                org_id="acme",
                provider=CIProvider.GITHUB_ACTIONS,
                run_id="run",
                repo="acme/repo",
                commit_sha="a" * 40,
                branch="main",
                result=CIResult.PASSED,
            )
        ]
        if reward
        else []
    )
    rollout = Rollout(
        org_id="acme",
        session_id=f"{prefix}session",
        segments=segments,
        commits=[CommitRef(repo="acme/repo", commit_sha="a" * 40)] if reward else [],
        terminal_outcomes=outcomes,
        attribution_source=AttributionSource.JACCARD,
        split="train",
        provenance=Provenance(policy_version="4", quarantine_revision=0),
    )
    return project_nemo_gym_rollouts([rollout], VerifierCommands.empty()).rows, {
        first.inference_call_id: first,
        second.inference_call_id: second,
    }


def test_nemo_uses_native_output_and_preserves_order_without_scoring_text():
    from sediment_export.consumer_rlvr import adapt_nemo_rows, ConsumerSettings

    rows, calls = _trajectory()
    before = copy.deepcopy(rows)
    config = ConsumerSettings.model_validate(
        {
            "schema_version": 1,
            "nemo": {
                "parallel_tool_calls": False,
                "tool_choice": "auto",
                "tools": [],
            },
        }
    )
    adapted = adapt_nemo_rows(rows, calls, config)
    [row] = adapted.rows
    assert [item["type"] for item in row.body["response"]["output"]] == [
        "reasoning",
        "function_call",
        "function_call_output",
        "message",
        "message",
    ]
    assert (
        row.body["response"]["output"][0]["summary"][0]["text"] == "verbatim reasoning"
    )
    assert row.body["response"]["output"][-1]["content"][0]["text"] == "visible answer"
    assert row.body["response"]["output"][2]["output"] == '{"ok":true}'
    assert row.body["reward"] == 1.0
    assert "status" not in row.body["response"]
    assert row.body["response"]["created_at"] == calls["c1"].observed_at.timestamp()
    assert adapted.evidence[0].body["response_configuration_source"] == "operator"
    assert adapted.evidence[0].body["inference_call_ids"] == ["c1", "c2"]
    assert rows == before


def test_nemo_missing_configuration_fails_without_fabrication():
    from sediment_export.consumer_rlvr import adapt_nemo_rows, ConsumerSettings
    from sediment_export.compatibility import CompatibilityError

    rows, calls = _trajectory()
    with pytest.raises(CompatibilityError, match="response configuration"):
        adapt_nemo_rows(rows, calls, ConsumerSettings())


def test_nemo_missing_call_and_model_conflict_are_counted():
    from sediment_export.consumer_rlvr import adapt_nemo_rows, ConsumerSettings

    rows, calls = _trajectory()
    config = ConsumerSettings.model_validate(
        {"nemo": {"parallel_tool_calls": False, "tool_choice": "auto", "tools": []}}
    )
    assert adapt_nemo_rows(rows, {"c1": calls["c1"]}, config).skipped == {
        "inference_call_absent": 1
    }
    changed = calls | {"c2": calls["c2"].model_copy(update={"model": "different"})}
    assert adapt_nemo_rows(rows, changed, config).skipped == {"model_conflict": 1}


def test_nemo_shuffled_call_lookup_does_not_change_bytes():
    from sediment_export.consumer_rlvr import adapt_nemo_rows, ConsumerSettings

    rows, calls = _trajectory()
    config = ConsumerSettings.model_validate(
        {"nemo": {"parallel_tool_calls": False, "tool_choice": "auto", "tools": []}}
    )
    assert adapt_nemo_rows(rows, calls, config) == adapt_nemo_rows(
        rows, dict(reversed(list(calls.items()))), config
    )


def test_task_config_rejects_guessed_or_incomplete_runtime():
    from sediment_export.consumer_rlvr import ConsumerSettings
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ConsumerSettings.model_validate(
            {"tasks": {"instance": {"problem_statement": "repair"}}}
        )
    with pytest.raises(ValidationError):
        ConsumerSettings.model_validate(
            {
                "nemo": {
                    "parallel_tool_calls": False,
                    "tool_choice": "auto",
                    "tools": [],
                    "unknown": True,
                }
            }
        )


@pytest.mark.parametrize("version", [True, 1.0, "1"])
def test_consumer_config_requires_exact_integer_version(version):
    import json
    from pydantic import ValidationError
    from sediment_export.consumer_rlvr import ConsumerSettings

    with pytest.raises(ValidationError):
        ConsumerSettings.model_validate({"schema_version": version})
    with pytest.raises(ValidationError):
        ConsumerSettings.model_validate_json(json.dumps({"schema_version": version}))


def test_offline_nemo_profile_counts_absent_reward_without_making_a_label():
    from sediment_export.consumer_rlvr import adapt_nemo_rows, ConsumerSettings

    rows, calls = _trajectory(reward=False)
    config = ConsumerSettings.model_validate(
        {"nemo": {"parallel_tool_calls": False, "tool_choice": "auto", "tools": []}}
    )
    result = adapt_nemo_rows(rows, calls, config)
    assert result.rows == []
    assert result.skipped == {"missing_reward": 1}


@pytest.mark.parametrize(
    "part,reason",
    [
        (
            ToolCallResponsePart(id="never-requested", result="orphan"),
            "unresolved_tool_call",
        ),
        (tool_call("t", "duplicate", {}), "duplicate_tool_call_id"),
    ],
)
def test_nemo_enforces_ordered_tool_references(part, reason):
    from sediment_export.consumer_rlvr import adapt_nemo_rows, ConsumerSettings

    rows, calls = _trajectory()
    prefix = InferenceMessage(
        role="user" if isinstance(part, ToolCallResponsePart) else "assistant",
        parts=[part],
    )
    calls = {
        name: call.model_copy(update={"input_messages": [prefix, *call.input_messages]})
        for name, call in calls.items()
    }
    config = ConsumerSettings.model_validate(
        {"nemo": {"parallel_tool_calls": False, "tool_choice": "auto", "tools": []}}
    )
    assert adapt_nemo_rows(rows, calls, config).skipped == {reason: 1}
