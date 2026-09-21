# SPDX-License-Identifier: AGPL-3.0-or-later
"""Versioned, read-only evidence projections over canonical Inference calls."""

from __future__ import annotations

import json
import math
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, ConfigDict, Field, TypeAdapter

from .models import (
    AwareDatetime,
    InferenceMessage,
    InferenceMessagePart,
    ModelName,
    NonEmptyId,
    ScalarIdentity,
)

EVIDENCE_INVENTORY_LIMIT = 1_000
EVIDENCE_REFERENCE_LIMIT = 32
EVIDENCE_REQUEST_BYTES_LIMIT = 64 * 1024
EVIDENCE_SOURCE_BYTES_LIMIT = 8 * 1024 * 1024
EVIDENCE_RESPONSE_BYTES_LIMIT = 1024 * 1024
CONTEXT_SOURCE_PART_LIMIT = 2_048

EvidenceSide = Literal["input", "output"]
EvidenceIndex = Annotated[int, Field(strict=True, ge=0)]


def _version_one(value: object) -> object:
    if type(value) is not int or value != 1:
        raise ValueError("unsupported evidence schema version")
    return value


EvidenceSchemaVersion = Annotated[Literal[1], BeforeValidator(_version_one)]
_ID = TypeAdapter(NonEmptyId)


@dataclass(frozen=True)
class EvidenceReference:
    """One immutable occurrence, independent of tool or provider call aliases."""

    __pydantic_config__ = ConfigDict(extra="forbid")

    inference_call_id: NonEmptyId
    side: EvidenceSide
    message_index: EvidenceIndex
    part_index: EvidenceIndex

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "inference_call_id", _ID.validate_python(self.inference_call_id)
        )
        if self.side not in ("input", "output"):
            raise ValueError("invalid evidence side")
        if any(
            type(index) is not int or index < 0
            for index in (self.message_index, self.part_index)
        ):
            raise ValueError("evidence indices must be nonnegative integers")


@dataclass(frozen=True)
class EvidenceCallMetadata:
    __pydantic_config__ = ConfigDict(extra="forbid")

    inference_call_id: NonEmptyId
    observed_at: AwareDatetime
    model_provider: ScalarIdentity | None
    model: ModelName | None


@dataclass(frozen=True, kw_only=True)
class EvidenceInventory:
    __pydantic_config__ = ConfigDict(extra="forbid")

    session_id: NonEmptyId
    quarantine_revision: EvidenceIndex
    found: Annotated[bool, Field(strict=True)]
    visible_inference_calls: EvidenceIndex
    quarantined_inference_calls: EvidenceIndex
    calls: tuple[EvidenceCallMetadata, ...]
    capture_completeness: Literal["unknown"]
    schema_version: EvidenceSchemaVersion


@dataclass(frozen=True)
class EvidenceManifestPart:
    __pydantic_config__ = ConfigDict(extra="forbid")

    type: Literal["text", "reasoning", "tool_call", "tool_call_response"]
    reference: EvidenceReference


@dataclass(frozen=True)
class EvidenceManifestMessage:
    __pydantic_config__ = ConfigDict(extra="forbid")

    side: EvidenceSide
    message_index: EvidenceIndex
    role: str
    finish_reason: str | None
    parts: tuple[EvidenceManifestPart, ...]


@dataclass(frozen=True, kw_only=True)
class EvidenceManifest:
    __pydantic_config__ = ConfigDict(extra="forbid")

    session_id: NonEmptyId
    quarantine_revision: EvidenceIndex
    call: EvidenceCallMetadata
    messages: tuple[EvidenceManifestMessage, ...]
    schema_version: EvidenceSchemaVersion


@dataclass(frozen=True)
class EvidenceReadItem:
    __pydantic_config__ = ConfigDict(extra="forbid")

    reference: EvidenceReference
    observed_at: AwareDatetime
    role: str
    finish_reason: str | None
    part: InferenceMessagePart


@dataclass(frozen=True, kw_only=True)
class EvidenceRead:
    __pydantic_config__ = ConfigDict(extra="forbid")

    session_id: NonEmptyId
    quarantine_revision: EvidenceIndex
    items: tuple[EvidenceReadItem, ...]
    schema_version: EvidenceSchemaVersion


@dataclass(frozen=True)
class EvidenceMessageSource:
    """Selected source column used during a single read; never persisted."""

    observed_at: AwareDatetime
    messages: tuple[InferenceMessage, ...]


@dataclass(frozen=True, kw_only=True)
class EvidenceContextSource:
    """Complete bounded visible Session content, owned by one read snapshot."""

    __pydantic_config__ = ConfigDict(extra="forbid")

    session_id: NonEmptyId
    quarantine_revision: EvidenceIndex
    visible_inference_calls: EvidenceIndex
    quarantined_inference_calls: EvidenceIndex
    items: tuple[EvidenceReadItem, ...]


class EvidenceReadError(ValueError):
    """Content-free complete-response refusal, mapped to HTTP 409 by the API."""

    def __init__(
        self,
        reason: Literal[
            "evidence_inventory_limit",
            "evidence_source_limit",
            "evidence_response_limit",
            "evidence_unavailable",
            "evidence_part_absent",
            "retrieval_part_limit",
            "non_finite_number",
        ],
        **details: int,
    ) -> None:
        self.detail = {"reason": reason, **details}
        super().__init__(reason)


def validate_evidence_references(
    references: Sequence[EvidenceReference],
) -> tuple[EvidenceReference, ...]:
    """Validate the complete selection before a caller issues SQL or HTTP."""
    if not 1 <= len(references) <= EVIDENCE_REFERENCE_LIMIT:
        raise ValueError("evidence selection must contain between 1 and 32 references")
    result = tuple(references)
    if any(not isinstance(reference, EvidenceReference) for reference in result):
        raise ValueError("invalid evidence reference")
    if len(set(result)) != len(result):
        raise ValueError("duplicate evidence reference")
    return result


def project_evidence_inventory(
    session_id: NonEmptyId,
    quarantine_revision: int,
    *,
    found: bool,
    calls: Sequence[EvidenceCallMetadata],
    quarantined_inference_calls: int,
) -> EvidenceInventory:
    """Order a complete visible population independently of insertion order."""
    ordered = tuple(
        sorted(
            calls,
            key=lambda call: (call.observed_at.astimezone(UTC), call.inference_call_id),
        )
    )
    return EvidenceInventory(
        schema_version=1,
        capture_completeness="unknown",
        session_id=session_id,
        quarantine_revision=quarantine_revision,
        found=found,
        visible_inference_calls=len(ordered),
        quarantined_inference_calls=quarantined_inference_calls,
        calls=ordered,
    )


def project_evidence_manifest(
    session_id: NonEmptyId,
    quarantine_revision: int,
    call: EvidenceCallMetadata,
    input_messages: Sequence[InferenceMessage],
    output_messages: Sequence[InferenceMessage],
) -> EvidenceManifest:
    """Describe canonical occurrences without emitting part content."""
    messages = []
    for side, source in (("input", input_messages), ("output", output_messages)):
        for message_index, message in enumerate(source):
            messages.append(
                EvidenceManifestMessage(
                    side=side,
                    message_index=message_index,
                    role=message.role,
                    finish_reason=message.finish_reason,
                    parts=tuple(
                        EvidenceManifestPart(
                            type=part.type,
                            reference=EvidenceReference(
                                call.inference_call_id, side, message_index, part_index
                            ),
                        )
                        for part_index, part in enumerate(message.parts)
                    ),
                )
            )
    return EvidenceManifest(
        schema_version=1,
        session_id=session_id,
        quarantine_revision=quarantine_revision,
        call=call,
        messages=tuple(messages),
    )


def project_evidence_read(
    session_id: NonEmptyId,
    quarantine_revision: int,
    references: Sequence[EvidenceReference],
    sources: Mapping[tuple[NonEmptyId, EvidenceSide], EvidenceMessageSource],
) -> EvidenceRead:
    """Resolve the whole ordered selection or refuse without a partial packet."""
    items = []
    for index, reference in enumerate(validate_evidence_references(references)):
        source = sources.get((reference.inference_call_id, reference.side))
        if source is None:
            raise EvidenceReadError("evidence_unavailable", reference_index=index)
        try:
            message = source.messages[reference.message_index]
            part = message.parts[reference.part_index]
        except IndexError:
            raise EvidenceReadError(
                "evidence_part_absent",
                reference_index=index,
            ) from None
        items.append(
            EvidenceReadItem(
                reference=reference,
                observed_at=source.observed_at,
                role=message.role,
                finish_reason=message.finish_reason,
                part=part,
            )
        )
    return EvidenceRead(
        schema_version=1,
        session_id=session_id,
        quarantine_revision=quarantine_revision,
        items=tuple(items),
    )


def validate_evidence_numbers(value: Any) -> None:
    """Refuse non-finite JSON numbers without including captured values."""
    if isinstance(value, float) and not math.isfinite(value):
        raise EvidenceReadError("non_finite_number")
    if isinstance(value, dict):
        for key, item in value.items():
            validate_evidence_numbers(key)
            validate_evidence_numbers(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            validate_evidence_numbers(item)


def _json_chunks(value: Any) -> Iterator[str]:
    """Match compact ASCII JSON while bounding each escaped string fragment."""
    if isinstance(value, str):
        yield '"'
        # JSONEncoder.iterencode emits a whole scalar at once. Split before
        # escaping so an oversized source part cannot allocate its full encoding.
        for offset in range(0, len(value), 1024):
            yield json.dumps(value[offset : offset + 1024], ensure_ascii=True)[1:-1]
        yield '"'
    elif isinstance(value, (list, tuple)):
        yield "["
        for index, item in enumerate(value):
            if index:
                yield ","
            yield from _json_chunks(item)
        yield "]"
    elif isinstance(value, dict):
        yield "{"
        for index, (key, item) in enumerate(value.items()):
            if index:
                yield ","
            if not isinstance(key, str):
                if key is None or isinstance(key, (bool, int, float)):
                    key = json.dumps(key, allow_nan=False)
                else:
                    raise TypeError("unsupported query response object key")
            yield from _json_chunks(key)
            yield ":"
            yield from _json_chunks(item)
        yield "}"
    elif isinstance(value, datetime):
        yield from _json_chunks(TypeAdapter(datetime).dump_python(value, mode="json"))
    elif value is None or isinstance(value, (bool, int, float)):
        yield json.dumps(value, allow_nan=False, separators=(",", ":"))
    else:
        raise TypeError(f"unsupported query response value: {type(value).__name__}")


def encode_evidence_json(
    value: Any,
    contract: Any,
    *,
    exclude_none: bool = False,
    max_bytes: int | None = None,
) -> bytes:
    """Validate a Python contract, then emit exact bounded strict ASCII JSON.

    Python-mode serialization preserves descriptive surrogates for escaping.
    HTTP exception translation belongs to the API, not this shared encoder.
    """
    adapter = TypeAdapter(contract)
    validated = adapter.validate_python(asdict(value) if is_dataclass(value) else value)
    payload = adapter.dump_python(validated, mode="python", exclude_none=exclude_none)
    validate_evidence_numbers(payload)
    bounded = bytearray()
    for chunk in _json_chunks(payload):
        # All output is ASCII; characters and bytes have the same length.
        if max_bytes is not None and len(bounded) + len(chunk) > max_bytes:
            raise EvidenceReadError("evidence_response_limit", limit=max_bytes)
        bounded.extend(chunk.encode("ascii"))
    return bytes(bounded)
