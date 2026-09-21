# SPDX-License-Identifier: AGPL-3.0-or-later
"""Versioned consumer boundaries, evidence sidecars, and private publication."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import tempfile
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field, fields
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import get_type_hints

from pydantic import TypeAdapter, ValidationError

from ._record_storage import FileRecords
from .derived_bundle import BundleCapacityError, BundleLimits, _json_chunks
from .dpo import DPOPair
from .jsonl import ExportRow, write_jsonl
from .schema_identity import (
    DPO_PAIR_SCHEMA_ID,
    SFT_SAMPLE_SCHEMA_ID,
    NEMO_GYM_ROLLOUT_ROW_SCHEMA_ID,
    SWE_BENCH_TASK_ROW_SCHEMA_ID,
)
from .sft import SFTSample
from .trainer import validate_training_representation


class CompatibilityError(ValueError):
    """An explicit consumer request cannot be honored without changing meaning."""


@dataclass(frozen=True)
class CompatibilityProfile:
    """One immutable mapping claim, independent of recipe and canonical versions."""

    id: str
    consumer: str
    objective: str
    profile_version: int
    canonical_schema: str
    dependencies: tuple[tuple[str, str], ...]
    validation: str
    support: str


_HF_DEPENDENCIES = (
    ("datasets", "5.0.1"),
    ("trl", "1.13.0"),
    ("transformers", "5.17.0"),
)
PROFILES = tuple(
    CompatibilityProfile(
        id=f"{consumer}-{objective}-v{profile_version}",
        consumer=consumer,
        objective=objective,
        profile_version=profile_version,
        canonical_schema=schema,
        dependencies=_HF_DEPENDENCIES if consumer == "hf-trl" else (),
        validation="datasets.Dataset.from_list; trl.is_conversational"
        if consumer == "hf-trl"
        else "Fireworks documented JSONL contract, 2026-09-13",
        support="loader"
        if consumer == "hf-trl"
        else "format; hosted acceptance not tested",
    )
    for consumer in ("hf-trl", "fireworks")
    for objective, schema, profile_version in (
        ("sft", SFT_SAMPLE_SCHEMA_ID, 1),
        ("dpo", DPO_PAIR_SCHEMA_ID, 2),
    )
) + (
    CompatibilityProfile(
        "swe-bench-tasks-v1",
        "swe-bench",
        "rlvr",
        1,
        SWE_BENCH_TASK_ROW_SCHEMA_ID,
        (("swebench", "5.0.1"), ("datasets", "5.0.1")),
        "swebench.harness.utils.load_swebench_dataset; make_test_spec",
        "loader and runtime parser; repository tests not run",
    ),
    CompatibilityProfile(
        "nemo-gym-rollouts-v1",
        "nemo-gym",
        "rlvr",
        1,
        NEMO_GYM_ROLLOUT_ROW_SCHEMA_ID,
        (("nemo-gym", "0.2.1"), ("openai", "2.6.1")),
        "nemo_gym.base_resources_server.BaseVerifyResponse",
        "native parser; optimizer not tested",
    ),
)


def get_profile(name: str, *, objective: str | None = None) -> CompatibilityProfile:
    if name in {"hf-trl-dpo-v1", "fireworks-dpo-v1"}:
        successor = name.removesuffix("v1") + "v2"
        raise CompatibilityError(
            f"profile {name} is retired; use {successor} and re-export "
            "canonical evidence with DPO recipe v2/schema v4"
        )
    for profile in PROFILES:
        if profile.id == name:
            if objective is not None and profile.objective != objective:
                raise CompatibilityError(
                    f"profile {name} does not support objective {objective}"
                )
            return profile
    raise CompatibilityError(f"unsupported profile: {name}")


def require_dependencies(profile: CompatibilityProfile) -> None:
    """Reject drift or absence; optional consumers never enter base dependencies."""
    for package, expected in profile.dependencies:
        try:
            actual = version(package)
        except PackageNotFoundError as exc:
            raise CompatibilityError(
                f"{profile.id} requires optional {package}=={expected}"
            ) from exc
        if actual != expected:
            raise CompatibilityError(
                f"{profile.id} requires {package}=={expected}; found {actual}"
            )


def _json(value: object) -> str:
    validate_training_representation(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def row_digest(body: dict) -> str:
    return hashlib.sha256(_json(body).encode()).hexdigest()


@dataclass(frozen=True)
class CompatibleRows:
    """Ordered consumer rows and one evidence record per consumer row."""

    rows: list[ExportRow]
    evidence: list[ExportRow]
    skipped: dict[str, int] = field(default_factory=dict)


_PROFILE_LIMITS = BundleLimits()


class _ProfileBudget:
    """Count encoded values before retaining a complete consumer population."""

    def __init__(self):
        self.remaining = _PROFILE_LIMITS.max_materialized_bytes

    def reserve(self, size):
        self.remaining -= size
        if self.remaining < 0:
            raise BundleCapacityError(
                "consumer profile materialization exceeds byte budget "
                f"({_PROFILE_LIMITS.max_materialized_bytes} bytes)"
            )

    def add(self, value):
        for chunk in _json_chunks(value):
            self.reserve(len(chunk))

    def population(self, values):
        if isinstance(values, FileRecords):
            self.reserve(values.encoded_bytes)
        else:
            for value in values:
                self.add(value)


def _profile_rows(rows):
    # Refuse a staged population before decoding its first record. A caller's
    # iterable has no stored byte count, so count each value before retaining it.
    if isinstance(rows, FileRecords):
        _ProfileBudget().population(rows)
    budget = _ProfileBudget()
    for row in rows:
        budget.add(row)
        yield row


def aligned_rows(
    pairs: Iterable[tuple[ExportRow, dict]], *, skipped=None
) -> CompatibleRows:
    budget = _ProfileBudget()

    def checked_pairs():
        for row, evidence in pairs:
            budget.add((row, {"row_sha256": row_digest(row.body), **evidence}))
            yield row, evidence

    ordered = sorted(
        checked_pairs(),
        key=lambda pair: (pair[0].split, row_digest(pair[0].body), _json(pair[1])),
    )
    return CompatibleRows(
        rows=[row for row, _ in ordered],
        evidence=[
            ExportRow(row.split, {"row_sha256": row_digest(row.body), **evidence})
            for row, evidence in ordered
        ],
        skipped=dict(sorted((skipped or {}).items())),
    )


def _validate_canonical(row: ExportRow, profile: CompatibilityProfile) -> None:
    contract = SFTSample if profile.objective == "sft" else DPOPair
    annotations = get_type_hints(contract)
    try:
        if set(row.body) != {item.name for item in fields(contract)}:
            raise ValueError("unknown or missing fields")
        restored = {}
        for name, value in row.body.items():
            # Pydantic cannot generate a schema for list[Never]. The canonical
            # contract means exactly an empty list, not unknown tool schemas.
            if name == "tools":
                if value != []:
                    raise ValueError("canonical tools must be empty")
                restored[name] = []
                continue
            adapter = TypeAdapter(annotations[name])
            source = adapter.validate_json(_json(value), strict=True)
            restored[name] = adapter.dump_python(source, mode="json")
    except (ValidationError, ValueError, TypeError) as exc:
        raise CompatibilityError(f"{profile.id}: invalid canonical row") from exc
    if _json(restored) != _json(row.body) or row.body["metadata"]["split"] != row.split:
        raise CompatibilityError(
            f"{profile.id}: canonical row has unknown or altered fields"
        )


def _messages(
    messages: list[dict], *, fireworks: bool, weight: int | None = None
) -> list[dict]:
    result = copy.deepcopy(messages)
    for index, message in enumerate(result):
        if "thinking" in message:
            message["reasoning_content"] = message.pop("thinking")
        if fireworks:
            if message["role"] == "developer":
                raise CompatibilityError(
                    "Fireworks profiles do not support developer messages"
                )
            if message["role"] == "system" and index != 0:
                raise CompatibilityError(
                    "Fireworks profiles require the system message to be first"
                )
            for call in message.get("tool_calls", []):
                call["function"]["arguments"] = _json(call["function"]["arguments"])
            if weight is not None and message["role"] == "assistant":
                message["weight"] = weight
    return result


def adapt_training_rows(rows: Iterable[ExportRow], name: str) -> CompatibleRows:
    """Map admitted SFT/DPO rows without changing labels or training boundaries."""
    profile = get_profile(name)
    if profile.objective not in {"sft", "dpo"}:
        raise CompatibilityError(f"{name} requires the RLVR adapter")

    def pairs():
        for row in _profile_rows(rows):
            _validate_canonical(row, profile)
            source = row.body
            fireworks = profile.consumer == "fireworks"
            prompt = _messages(
                source["prompt"],
                fireworks=fireworks,
                weight=0 if profile.objective == "sft" else None,
            )
            if profile.objective == "sft":
                completion = _messages(
                    source["completion"], fireworks=fireworks, weight=1
                )
                body = (
                    {"messages": prompt + completion}
                    if fireworks
                    else {"prompt": prompt, "completion": completion, "tools": []}
                )
            else:
                chosen = _messages(source["chosen"], fireworks=fireworks)
                rejected = _messages(source["rejected"], fireworks=fireworks)
                if fireworks:
                    if len(chosen) != 1 or len(rejected) != 1:
                        raise CompatibilityError(
                            "Fireworks DPO requires one final assistant message per output"
                        )
                    if any(
                        set(message) != {"role", "content"}
                        or message["role"] not in {"system", "user", "assistant"}
                        or not isinstance(message["content"], str)
                        for message in prompt + chosen + rejected
                    ):
                        raise CompatibilityError(
                            "Fireworks DPO v2 supports documented text messages only"
                        )
                    body = {
                        "input": {"messages": prompt, "tools": []},
                        "preferred_output": chosen,
                        "non_preferred_output": rejected,
                    }
                else:
                    body = {
                        "prompt": prompt,
                        "chosen": chosen,
                        "rejected": rejected,
                        "tools": [],
                    }
            yield (
                ExportRow(row.split, body),
                {
                    "canonical_schema": profile.canonical_schema,
                    "source_row_sha256": row_digest(source),
                    "prompt_sha256": row_digest({"prompt": source["prompt"]}),
                    "metadata": copy.deepcopy(source["metadata"]),
                    "tool_definitions": "absent",
                },
            )

    return aligned_rows(pairs())


def hf_dataset(rows: Iterable[dict], name: str):
    """Load heterogeneous conversations with explicit Arrow JSON features.

    Use this loader for the emitted JSONL instead of inferring nested structs
    from the first batch. Json preserves arbitrary tool keys and scalar types.
    """
    profile = get_profile(name)
    if profile.consumer != "hf-trl":
        raise CompatibilityError(f"{name} is not a Hugging Face profile")
    require_dependencies(profile)
    from datasets import Dataset, Features, Json, List
    from trl import is_conversational

    columns = (
        ("prompt", "completion", "tools")
        if profile.objective == "sft"
        else ("prompt", "chosen", "rejected", "tools")
    )
    values = list(_profile_rows(rows))
    for row in values:
        if set(row) != set(columns) or not is_conversational(row):
            raise CompatibilityError(f"{name}: invalid conversational fields")
        for column in columns:
            if not isinstance(row[column], list):
                raise CompatibilityError(f"{name}: {column} must be a list")
        for column in columns[:-1]:
            for message in row[column]:
                if not isinstance(message, dict) or message.get("role") not in {
                    "system",
                    "developer",
                    "user",
                    "assistant",
                    "tool",
                }:
                    raise CompatibilityError(f"{name}: invalid message")
    features = Features({name: List(Json()) for name in columns})
    dataset = (
        Dataset.from_list(values, features=features)
        if values
        else Dataset.from_dict({name: [] for name in columns}, features=features)
    )
    if list(dataset) != values:
        raise CompatibilityError(f"{name}: downstream loader changed values")
    return dataset


def load_hf_dataset(path: str | Path, name: str):
    """Read an emitted profile JSONL through its pinned Hugging Face loader."""
    budget = _ProfileBudget()
    with Path(path).open("rb") as handle:

        def values():
            while line := handle.readline(budget.remaining + 1):
                budget.reserve(len(line))
                if line.strip():
                    yield json.loads(line)

        return hf_dataset(values(), name)


def _validate_adapted(rows: CompatibleRows, profile: CompatibilityProfile) -> None:
    require_dependencies(profile)
    if len(rows.rows) != len(rows.evidence) or any(
        row.split != evidence.split
        or evidence.body.get("row_sha256") != row_digest(row.body)
        for row, evidence in zip(rows.rows, rows.evidence, strict=True)
    ):
        raise CompatibilityError("consumer rows and evidence are not aligned")
    for row in rows.rows:
        validate_training_representation(row.body)
    if profile.consumer == "hf-trl":
        hf_dataset((row.body for row in rows.rows), profile.id)
    elif profile.objective == "rlvr":
        from .consumer_rlvr import validate_rlvr_consumer_rows

        validate_rlvr_consumer_rows(rows.rows, profile.consumer)


def publish_compatible_rows(
    rows: CompatibleRows,
    destination: Path,
    profile: CompatibilityProfile,
    *,
    split_enabled: bool,
    canonical_skipped: Mapping[str, int] | None = None,
    fragmented: Mapping[str, int] | None = None,
) -> dict:
    """Validate all rows, then publish a complete private directory exactly once."""
    budget = _ProfileBudget()
    budget.population(rows.rows)
    budget.population(rows.evidence)
    _validate_adapted(rows, profile)
    source_counts = {
        name: dict(sorted(counts.items()))
        for name, counts in (
            ("canonical_skipped", canonical_skipped),
            ("fragmented", fragmented),
        )
        if counts is not None
    }
    prompts = {
        split: {
            row.body["prompt_sha256"]
            for row in rows.evidence
            if row.split == split and "prompt_sha256" in row.body
        }
        for split in ("train", "eval")
    }
    overlap = len(prompts["train"] & prompts["eval"])
    if split_enabled and overlap:
        raise CompatibilityError(
            f"consumer export has {overlap} exact prompts in both train and eval; select a coherent cohort"
        )
    diagnostics = {
        "rows_by_split": dict(sorted(Counter(row.split for row in rows.rows).items())),
        "reward_counts": dict(
            sorted(
                Counter(
                    str(row.body["reward"]) for row in rows.rows if "reward" in row.body
                ).items()
            )
        ),
        "exact_prompt_overlap": overlap,
        "semantic_task_overlap": "not_assessed",
    }
    if destination.exists():
        raise CompatibilityError(f"consumer destination already exists: {destination}")
    if not rows.rows:
        return {
            "profile": profile.id,
            "rows": 0,
            "skipped": rows.skipped,
            **source_counts,
            "written": {},
            "diagnostics": diagnostics,
        }
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent)
    )
    try:
        data = write_jsonl(
            rows.rows,
            staging / "data.jsonl",
            split_enabled=split_enabled,
            max_bytes=_PROFILE_LIMITS.max_materialized_bytes,
        )
        remaining = _PROFILE_LIMITS.max_materialized_bytes - sum(
            Path(path).stat().st_size for path in data.written
        )
        evidence = write_jsonl(
            rows.evidence,
            staging / "evidence.jsonl",
            split_enabled=split_enabled,
            max_bytes=remaining,
        )
        files = {}
        for path in sorted(staging.iterdir()):
            with path.open("rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            files[path.name] = {
                "sha256": digest,
                "bytes": path.stat().st_size,
            }
        manifest = {
            "profile": asdict(profile),
            "rows": len(rows.rows),
            "skipped": rows.skipped,
            **source_counts,
            "files": files,
            "training_execution": "not_run",
            "tool_definitions": "not_inferred",
            "diagnostics": diagnostics,
        }
        (staging / "compatibility.json").write_text(
            _json(manifest) + "\n", encoding="utf-8"
        )
        os.chmod(staging / "compatibility.json", 0o600)
        os.rename(staging, destination)
        written = {
            str(destination / Path(path).name): count
            for path, count in (data.written | evidence.written).items()
        }
        return {
            "profile": profile.id,
            "rows": len(rows.rows),
            "skipped": rows.skipped,
            **source_counts,
            "written": written,
            "diagnostics": diagnostics,
        }
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def write_compatible_export(
    rows: Iterable[ExportRow],
    destination: str | Path,
    name: str,
    *,
    split_enabled: bool,
    canonical_skipped: Mapping[str, int] | None = None,
) -> dict:
    profile = get_profile(name)
    require_dependencies(profile)
    adapted = adapt_training_rows(rows, name)
    return publish_compatible_rows(
        adapted,
        Path(destination),
        profile,
        split_enabled=split_enabled,
        canonical_skipped=canonical_skipped,
    )
