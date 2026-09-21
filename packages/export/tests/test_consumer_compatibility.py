# SPDX-License-Identifier: AGPL-3.0-or-later
"""Consumer mappings preserve real projected evidence and fail before publication."""

from dataclasses import replace
from datetime import UTC, datetime
import copy
import json

import pytest

from sediment_core import (
    CIOutcome,
    CIProvider,
    CIResult,
    InferenceMessage,
    ReasoningPart,
)
from sediment_derive import AttributionSource, Provenance
from sediment_export import (
    AttributedCompletion,
    SFTPolicy,
    project_sft,
    sft_to_export_rows,
)
from export_factories import inference_call, message, tool_call


def _sft_rows():
    call = inference_call(
        "call",
        org_id="acme",
        session_id="session",
        input_messages=[
            message("user", "inspect"),
            message("assistant", "Earlier answer"),
            message("user", "finish"),
        ],
        observed_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    call = call.model_copy(
        update={
            "output_messages": [
                InferenceMessage(
                    role="assistant",
                    parts=[
                        ReasoningPart(content="reasoning sentinel"),
                        tool_call("t", "inspect", {"key": [1, None]}),
                    ],
                )
            ]
        }
    )
    outcome = CIOutcome(
        org_id="acme",
        provider=CIProvider.GITHUB_ACTIONS,
        run_id="run",
        repo="acme/repo",
        commit_sha="a" * 40,
        result=CIResult.PASSED,
        branch="main",
    )
    attributed = AttributedCompletion(
        org_id="acme",
        session_id="session",
        inference_call_id="call",
        repo="acme/repo",
        commit_sha="a" * 40,
        file_path="a.py",
        similarity_score=1,
        attribution_source=AttributionSource.GIT_NOTES,
        decisions=[],
        ci_outcomes=[outcome],
        provenance=Provenance(policy_version="5", quarantine_revision=0),
        split="train",
    )
    projected = project_sft(
        [attributed], {"call": call}, SFTPolicy(recipe_id="sft_verified")
    )
    assert len(projected.rows) == 1
    return sft_to_export_rows(projected.rows)


@pytest.mark.parametrize("name", ["hf-trl-sft-v1", "fireworks-sft-v1"])
def test_sft_profile_preserves_reasoning_tools_and_evidence(name):
    from sediment_export.compatibility import adapt_training_rows

    rows = _sft_rows()
    before = copy.deepcopy(rows)
    adapted = adapt_training_rows(rows, name)
    assert len(adapted.rows) == 1
    body = adapted.rows[0].body
    output = body["completion"] if name.startswith("hf") else body["messages"][-1:]
    assert output[0]["reasoning_content"] == "reasoning sentinel"
    assert "thinking" not in output[0]
    args = output[0]["tool_calls"][0]["function"]["arguments"]
    assert (args if name.startswith("hf") else json.loads(args)) == {"key": [1, None]}
    assert "metadata" not in body
    assert "weight" not in body
    assert adapted.evidence[0].body["metadata"] == rows[0].body["metadata"]
    assert rows == before


def test_fireworks_sft_masks_only_prior_assistant_turns():
    from sediment_export.compatibility import adapt_training_rows

    body = adapt_training_rows(_sft_rows(), "fireworks-sft-v1").rows[0].body
    assert [m.get("weight") for m in body["messages"]] == [None, 0, None, 1]


def test_profile_validates_canonical_schema_before_removing_metadata():
    from sediment_export.compatibility import adapt_training_rows, CompatibilityError

    [row] = _sft_rows()
    for changed in ({"unknown": 1}, {"metadata": {"schema_version": 999}}):
        with pytest.raises(CompatibilityError, match="canonical"):
            adapt_training_rows(
                [replace(row, body=row.body | changed)], "hf-trl-sft-v1"
            )


def test_profile_identity_and_objective_are_closed():
    from sediment_export.compatibility import get_profile, CompatibilityError

    with pytest.raises(CompatibilityError, match="unsupported profile"):
        get_profile("hf-trl-sft-v99")
    with pytest.raises(CompatibilityError, match="objective"):
        get_profile("hf-trl-sft-v1", objective="dpo")


def test_adaptation_is_deterministic_under_input_reordering():
    from sediment_export.compatibility import adapt_training_rows

    [first] = _sft_rows()
    second = replace(first, split="eval", body=copy.deepcopy(first.body))
    second.body["metadata"]["split"] = "eval"
    second.body["metadata"]["completion_id"] = "another"
    assert adapt_training_rows([first, second], "hf-trl-sft-v1") == adapt_training_rows(
        [second, first], "hf-trl-sft-v1"
    )


def test_explicit_profile_failure_leaves_no_artifacts(tmp_path):
    from sediment_export.compatibility import (
        write_compatible_export,
        CompatibilityError,
    )

    destination = tmp_path / "absent"
    with pytest.raises(CompatibilityError, match="unsupported profile"):
        write_compatible_export(
            _sft_rows(), destination, "hf-trl-sft-v99", split_enabled=True
        )
    assert not destination.exists()


def test_split_profile_rejects_exact_prompt_leakage_before_publication(tmp_path):
    from sediment_export.compatibility import (
        CompatibilityError,
        write_compatible_export,
    )

    [train] = _sft_rows()
    evaluation = replace(train, split="eval", body=copy.deepcopy(train.body))
    evaluation.body["metadata"]["split"] = "eval"
    with pytest.raises(CompatibilityError, match="exact prompts"):
        write_compatible_export(
            [train, evaluation],
            tmp_path / "absent",
            "fireworks-sft-v1",
            split_enabled=True,
        )
    assert not (tmp_path / "absent").exists()


def test_private_publication_is_deterministic_and_reports_empty_holdout(tmp_path):
    from sediment_export.compatibility import write_compatible_export

    rows = _sft_rows()
    second = replace(rows[0], body=copy.deepcopy(rows[0].body))
    second.body["metadata"]["completion_id"] = "second"
    second.body["prompt"][0]["content"] = "another prompt"
    rows.append(second)
    first = tmp_path / "first"
    repeated = tmp_path / "repeated"
    summary = write_compatible_export(
        rows, first, "fireworks-sft-v1", split_enabled=True
    )
    write_compatible_export(
        reversed(rows), repeated, "fireworks-sft-v1", split_enabled=True
    )
    assert {path.name: path.read_bytes() for path in first.iterdir()} == {
        path.name: path.read_bytes() for path in repeated.iterdir()
    }
    assert summary["diagnostics"]["rows_by_split"] == {"train": 2}
    assert "canonical_skipped" not in summary
    assert "fragmented" not in summary
    assert not (first / "data.eval.jsonl").exists()
    assert first.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in first.iterdir())


def test_empty_profile_retains_supplied_exclusions_without_writing(tmp_path):
    from sediment_export.compatibility import write_compatible_export

    summary = write_compatible_export(
        [],
        tmp_path / "absent",
        "fireworks-sft-v1",
        split_enabled=True,
        canonical_skipped={"explicit_reject": 1},
    )
    assert summary["canonical_skipped"] == {"explicit_reject": 1}
    assert summary["skipped"] == {}
    assert "fragmented" not in summary
    assert not (tmp_path / "absent").exists()


def test_missing_or_drifted_dependency_fails_before_artifact_writes(
    tmp_path, monkeypatch
):
    import sediment_export.compatibility as compatibility
    from importlib.metadata import PackageNotFoundError

    for actual in (None, "999"):

        def version(name):
            if actual is None:
                raise PackageNotFoundError(name)
            return actual

        monkeypatch.setattr(compatibility, "version", version)
        with pytest.raises(compatibility.CompatibilityError, match="requires"):
            compatibility.write_compatible_export(
                _sft_rows(), tmp_path / "absent", "hf-trl-sft-v1", split_enabled=True
            )
        assert not (tmp_path / "absent").exists()


def _dpo_rows():
    from sediment_export import dpo_to_export_rows, project_dpo
    from test_dpo import _contrast_inputs

    members, calls = _contrast_inputs(
        [
            message(
                "assistant", "def fib(n): return n if n < 2 else fib(n-1) + fib(n-2)"
            )
        ],
        [message("assistant", "def fib(n): return 0")],
    )
    result = project_dpo(members, calls)
    assert len(result.rows) == 1
    return dpo_to_export_rows(result.rows)


@pytest.mark.parametrize("consumer", ["hf-trl", "fireworks"])
def test_dpo_successors_keep_exact_responses_and_v2_evidence(consumer):
    from sediment_export.compatibility import adapt_training_rows, get_profile

    name = f"{consumer}-dpo-v2"
    canonical = _dpo_rows()
    profile = get_profile(name)
    assert profile.profile_version == 2
    assert (
        profile.canonical_schema
        == "https://sediment.so/schemas/training-rows/dpo-pair/v4.json"
    )
    result = adapt_training_rows(canonical, name)
    [row] = result.rows
    chosen = row.body["chosen" if consumer == "hf-trl" else "preferred_output"]
    rejected = row.body["rejected" if consumer == "hf-trl" else "non_preferred_output"]
    assert chosen == [
        {
            "role": "assistant",
            "content": "def fib(n): return n if n < 2 else fib(n-1) + fib(n-2)",
        }
    ]
    assert rejected == [{"role": "assistant", "content": "def fib(n): return 0"}]
    [evidence] = result.evidence
    assert evidence.body["metadata"] == canonical[0].body["metadata"]
    assert evidence.body["metadata"]["recipe_version"] == 2
    assert evidence.body["metadata"]["schema_version"] == 4
    assert evidence.body["canonical_schema"] == profile.canonical_schema


@pytest.mark.parametrize("consumer", ["hf-trl", "fireworks"])
def test_retired_dpo_profiles_refuse_with_reexport_guidance(consumer, tmp_path):
    from sediment_export.compatibility import (
        CompatibilityError,
        write_compatible_export,
    )

    with pytest.raises(
        CompatibilityError, match=f"retired.*{consumer}-dpo-v2.*re-export"
    ):
        write_compatible_export(
            _dpo_rows(), tmp_path / "absent", f"{consumer}-dpo-v1", split_enabled=False
        )
    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize("name", ["hf-trl-dpo-v2", "fireworks-dpo-v2"])
def test_dpo_successors_refuse_historical_v1_rows_without_coercion(name):
    from pathlib import Path
    from jsonschema import Draft202012Validator
    from sediment_export.compatibility import CompatibilityError, adapt_training_rows

    [row] = _dpo_rows()
    old = copy.deepcopy(row.body)
    old["metadata"].update(
        recipe_version=1,
        schema_version=3,
        schema_id="https://sediment.so/schemas/training-rows/dpo-pair/v3.json",
    )
    schema = (
        Path(__file__).resolve().parents[3] / "schemas/training-rows/dpo-pair/v3.json"
    )
    Draft202012Validator(json.loads(schema.read_text())).validate(
        json.loads(json.dumps(old))
    )
    before = copy.deepcopy(old)
    with pytest.raises(CompatibilityError, match="canonical"):
        adapt_training_rows([replace(row, body=old)], name)
    assert old == before
