# SPDX-License-Identifier: AGPL-3.0-or-later
"""Native RLVR consumer adapters with explicit operator configuration."""

from __future__ import annotations

import hashlib
import logging
import tempfile
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator
from sediment_core import (
    CommitSha,
    InferenceCall,
    InferenceMessage,
    NonEmptyId,
    ReasoningPart,
    RepoSlug,
    TextPart,
    ToolCallPart,
    ToolCallResponsePart,
)

from .compatibility import (
    CompatibilityError,
    CompatibleRows,
    _json,
    _ProfileBudget,
    _profile_rows,
    aligned_rows,
    row_digest,
)
from .jsonl import ExportRow
from .staged_rows import ExportRowStore
from .trainer import (
    TRAINER_SKIP_REASONS,
    TrainerMappingError,
    tool_result_text,
    validate_training_representation,
)

logger = logging.getLogger("sediment.export.consumer_rlvr")

NEMO_PROFILE_SKIP_REASONS = (
    *TRAINER_SKIP_REASONS,
    "missing_reward",
    "inference_call_absent",
    "inference_call_identity_mismatch",
    "model_absent",
    "model_conflict",
)


class NemoResponseSettings(BaseModel):
    """Operator-declared adapter parameters, never reconstructed request Facts."""

    model_config = ConfigDict(extra="forbid", strict=True)
    parallel_tool_calls: bool
    tool_choice: Literal["none", "auto", "required"]
    tools: list[dict[str, Any]]


class SWETaskSettings(BaseModel):
    """One operator-supplied task and runtime bound to an exact source patch."""

    model_config = ConfigDict(extra="forbid", strict=True)
    repo: RepoSlug
    base_commit: CommitSha
    problem_statement: str = Field(min_length=1)
    test_patch: str
    hints_text: str
    created_at: AwareDatetime
    version: NonEmptyId
    environment_setup_commit: CommitSha
    FAIL_TO_PASS: list[NonEmptyId] = Field(min_length=1)
    PASS_TO_PASS: list[NonEmptyId]
    image: NonEmptyId
    eval_script: str = Field(min_length=1)
    log_parser: NonEmptyId
    eval_type: Literal["pass_and_fail", "fail_only"]


class ConsumerSettings(BaseModel):
    """Closed JSON settings for task specifications and native response configuration."""

    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1] = 1
    nemo: NemoResponseSettings | None = None
    tasks: dict[NonEmptyId, SWETaskSettings] = Field(default_factory=dict)

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema_version must be an integer")
        return value

    @property
    def digest(self) -> str:
        return hashlib.sha256(_json(self.model_dump(mode="json")).encode()).hexdigest()


def load_consumer_settings(path: str | Path | None) -> ConsumerSettings:
    if path is None:
        return ConsumerSettings()
    budget = _ProfileBudget()
    with Path(path).open("rb") as source:
        content = source.read(budget.remaining + 1)
    budget.reserve(len(content))
    settings = ConsumerSettings.model_validate_json(content)
    _ProfileBudget().add(settings)
    return settings


def _native_items(
    messages: list[InferenceMessage], prefix: str, tool_names: dict[str, str]
) -> list[dict]:
    """Map ordered parts to Responses items; readable reasoning stays verbatim."""
    items = []
    for index, message in enumerate(messages):
        if not message.parts:
            raise TrainerMappingError("empty_message")
        for part_index, part in enumerate(message.parts):
            item_id = f"{prefix}-{index}-{part_index}"
            if isinstance(part, TextPart):
                if message.role == "assistant":
                    items.append(
                        {
                            "type": "message",
                            "id": item_id,
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": part.content,
                                    "annotations": [],
                                }
                            ],
                        }
                    )
                elif message.role in {"system", "developer", "user"}:
                    items.append(
                        {
                            "type": "message",
                            "role": message.role,
                            "content": part.content,
                        }
                    )
                else:
                    raise TrainerMappingError("unsupported_message_role")
            elif isinstance(part, ReasoningPart) and message.role == "assistant":
                # Gym 0.2.1's readable reasoning slot is summary_text. The
                # adapter copies the full text; it performs no summarization.
                items.append(
                    {
                        "type": "reasoning",
                        "id": item_id,
                        "summary": [{"type": "summary_text", "text": part.content}],
                    }
                )
            elif isinstance(part, ToolCallPart) and message.role == "assistant":
                if part.id in tool_names:
                    raise TrainerMappingError("duplicate_tool_call_id")
                tool_names[part.id] = part.name
                items.append(
                    {
                        "type": "function_call",
                        "id": item_id,
                        "call_id": part.id,
                        "name": part.name,
                        "arguments": _json(part.arguments),
                    }
                )
            elif isinstance(part, ToolCallResponsePart) and message.role in {
                "user",
                "tool",
            }:
                if part.id not in tool_names:
                    raise TrainerMappingError("unresolved_tool_call")
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": part.id,
                        "output": tool_result_text(part.result),
                    }
                )
            else:
                raise TrainerMappingError("unsupported_role_part")
    return items


def adapt_nemo_rows(
    rows: Iterable[ExportRow],
    calls: Mapping[str, InferenceCall],
    settings: ConsumerSettings,
) -> CompatibleRows:
    """Hydrate native outputs from trusted canonical Segment references."""
    if settings.nemo is None:
        raise CompatibilityError(
            "NeMo profile requires explicit response configuration in --consumer-config"
        )
    skipped = Counter()

    def pairs():
        for row in _profile_rows(rows):
            source = row.body
            if "reward" not in source:
                skipped["missing_reward"] += 1
                continue
            metadata = source["metadata"]
            turns = source["response"]["turns"]
            ids = [turn["inference_call_id"] for turn in turns]
            if not ids or any(call_id not in calls for call_id in ids):
                skipped["inference_call_absent"] += 1
                continue
            selected = [calls[call_id] for call_id in ids]
            if any(
                (call.org_id, call.session_id)
                != (metadata["org_id"], metadata["session_id"])
                for call in selected
            ):
                skipped["inference_call_identity_mismatch"] += 1
                continue
            if any(call.model is None for call in selected):
                skipped["model_absent"] += 1
                continue
            if len({call.model for call in selected}) != 1:
                skipped["model_conflict"] += 1
                continue
            first = selected[0]
            try:
                tool_names = {}
                inputs = _native_items(
                    first.input_messages, f"{first.inference_call_id}-input", tool_names
                )
                output = []
                previous = None
                for call in selected:
                    if previous is not None:
                        prefix_length = len(previous.input_messages) + len(
                            previous.output_messages
                        )
                        # The bundle validates continuity. Retain the same guard on
                        # this pure public adapter rather than silently slice drift.
                        from sediment_derive.rollout import project_session_turns

                        segments, _, _ = project_session_turns([previous, call], [])
                        if len(segments) != 1:
                            raise CompatibilityError(
                                "NeMo source Segment does not replay prior output"
                            )
                        output.extend(
                            _native_items(
                                call.input_messages[prefix_length:],
                                f"{call.inference_call_id}-input",
                                tool_names,
                            )
                        )
                    output.extend(
                        _native_items(
                            call.output_messages,
                            f"{call.inference_call_id}-output",
                            tool_names,
                        )
                    )
                    previous = call
                response_config = settings.nemo.model_dump(mode="json")
                body = {
                    "responses_create_params": {"input": inputs, **response_config},
                    "response": {
                        "id": metadata["instance_id"],
                        "created_at": first.observed_at.timestamp(),
                        "model": first.model,
                        "object": "response",
                        "output": output,
                        **response_config,
                    },
                }
                if "reward" in source:
                    body["reward"] = source["reward"]
                validate_training_representation(body)
            except TrainerMappingError as exc:
                skipped[exc.reason] += 1
                continue
            yield (
                ExportRow(row.split, body),
                {
                    "canonical_schema": metadata["schema_id"],
                    "metadata": metadata,
                    "source_row_sha256": row_digest(source),
                    "prompt_sha256": row_digest(
                        {
                            "prompt": [
                                message.model_dump(mode="json")
                                for message in first.input_messages
                            ]
                        }
                    ),
                    "inference_call_ids": ids,
                    "created_at_source_call_id": first.inference_call_id,
                    "response_configuration_source": "operator",
                    "configuration_digest": settings.digest,
                    "readable_reasoning_mapping": "verbatim summary_text; no summarization",
                    "native_output_messages": [
                        call.model_dump(mode="json")["output_messages"]
                        for call in selected
                    ],
                },
            )

    adapted = aligned_rows(pairs(), skipped=skipped)
    logger.info(
        "nemo_profile_projected",
        extra={"rows": len(adapted.rows), "skipped": dict(skipped)},
    )
    return adapted


def adapt_swe_rows(
    rows: Iterable[ExportRow], settings: ConsumerSettings
) -> CompatibleRows:
    """Bind explicit task/runtime inputs to the exact canonical passing patch."""

    def pairs():
        for row in _profile_rows(rows):
            source = row.body
            task = settings.tasks.get(source["instance_id"])
            if task is None:
                raise CompatibilityError(
                    f"SWE-bench task configuration missing for {source['instance_id']}"
                )
            if (task.repo, task.base_commit) != (source["repo"], source["base_commit"]):
                raise CompatibilityError(
                    f"SWE-bench task configuration does not match {source['instance_id']}"
                )
            if not task.problem_statement.strip():
                raise CompatibilityError("SWE-bench requires a repair request")
            if set(task.FAIL_TO_PASS) & set(task.PASS_TO_PASS):
                raise CompatibilityError("SWE-bench test transitions overlap")
            body = task.model_dump(mode="json") | {
                "instance_id": source["instance_id"],
                "patch": source["patch"],
            }
            yield (
                ExportRow(row.split, body),
                {
                    "canonical_schema": source["metadata"]["schema_id"],
                    "metadata": source["metadata"],
                    "source_row_sha256": row_digest(source),
                    "prompt_sha256": row_digest({"prompt": task.problem_statement}),
                    "task_configuration_source": "operator",
                    "configuration_digest": settings.digest,
                    "captured_first_user_message": source["problem_statement"],
                },
            )

    return aligned_rows(pairs())


def validate_rlvr_consumer_rows(rows: list[ExportRow], consumer: str) -> None:
    """Exercise the installed upstream parser and reject silent data loss."""
    try:
        if consumer == "nemo-gym":
            from nemo_gym.base_resources_server import BaseVerifyResponse

            for row in rows:
                parsed = BaseVerifyResponse.model_validate(row.body)
                if _json(parsed.model_dump(mode="json", exclude_unset=True)) != _json(
                    row.body
                ):
                    raise CompatibilityError("NeMo parser altered or dropped fields")
        elif consumer == "swe-bench":
            from swebench.harness.constants import START_TEST_OUTPUT, END_TEST_OUTPUT
            from swebench.harness.log_parsers import PARSER_REGISTRY
            from swebench.harness.utils import load_swebench_dataset, make_test_spec

            with tempfile.TemporaryDirectory(
                prefix="sediment-swe-loader-"
            ) as directory:
                path = Path(directory) / "tasks.jsonl"
                path.write_text("".join(_json(row.body) + "\n" for row in rows))
                loaded = load_swebench_dataset(str(path))
            if _json(loaded) != _json([row.body for row in rows]):
                raise CompatibilityError("SWE-bench loader altered or dropped fields")
            for row in loaded:
                spec = make_test_spec(row)
                if spec.log_parser not in PARSER_REGISTRY:
                    raise CompatibilityError("SWE-bench log_parser is not registered")
                if spec.eval_type not in {"pass_and_fail", "fail_only"}:
                    raise CompatibilityError("SWE-bench eval_type is unsupported")
                if any(
                    marker not in row["eval_script"]
                    for marker in (START_TEST_OUTPUT, END_TEST_OUTPUT)
                ):
                    raise CompatibilityError(
                        "SWE-bench eval_script requires test-output markers"
                    )
        else:
            raise CompatibilityError(f"unsupported RLVR consumer: {consumer}")
    except (ImportError, ValueError, TypeError, KeyError) as exc:
        if isinstance(exc, CompatibilityError):
            raise
        raise CompatibilityError(
            f"{consumer} loader rejected the export: {type(exc).__name__}"
        ) from exc


def export_rlvr_profile(
    bundle, mirrors, destination, name: str, settings: ConsumerSettings
) -> dict:
    """Validate the canonical bundle before hydrating a versioned consumer view."""
    from .compatibility import (
        get_profile,
        require_dependencies,
        publish_compatible_rows,
    )
    from .derived_bundle import validate_derived_bundle
    from .rlvr import project_nemo_gym_rollouts, project_swe_bench_tasks
    from .verifier_commands import VerifierCommandsSettings

    profile = get_profile(name, objective="rlvr")
    require_dependencies(profile)
    # These native consumers require complete materialized loader inputs. Apply
    # the same small-data envelope as explicit bundle materialization first.
    budget = _ProfileBudget()
    budget.population(bundle.rollouts)
    if profile.consumer == "nemo-gym":
        budget.population(bundle.inference_calls)
    _ProfileBudget().add(settings)
    context = validate_derived_bundle(bundle)
    commands = VerifierCommandsSettings().resolve()
    with ExportRowStore() as stage:
        rows = stage.records("consumer_source")
        projected_budget = _ProfileBudget()

        def collect(row):
            projected_budget.add(row)
            rows.append(row)

        if profile.consumer == "nemo-gym":
            projection = project_nemo_gym_rollouts(
                bundle.rollouts, commands, repository_context=context, row_sink=collect
            )
            rows.seal()
            calls = {call.inference_call_id: call for call in bundle.inference_calls}
            adapted = adapt_nemo_rows(rows, calls, settings)
        else:
            if mirrors is None:
                raise CompatibilityError("SWE-bench requires the source mirrors")
            projection = project_swe_bench_tasks(
                bundle.rollouts,
                mirrors,
                commands,
                repository_context=context,
                row_sink=collect,
            )
            rows.seal()
            adapted = adapt_swe_rows(rows, settings)
        return publish_compatible_rows(
            adapted,
            Path(destination),
            profile,
            split_enabled=bundle.policy.eval_fraction > 0,
            canonical_skipped=projection.skipped,
            fragmented=bundle.fragmented,
        )
