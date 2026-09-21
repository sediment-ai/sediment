# SPDX-License-Identifier: AGPL-3.0-or-later
"""Versioned, read-only evidence projections over canonical Inference calls."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC
from typing import Annotated, Literal

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
                "evidence_part_absent", reference_index=index
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
