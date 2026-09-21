# SPDX-License-Identifier: AGPL-3.0-or-later
"""Operator-selected evidence reads and private packet publication."""

from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
from datetime import datetime
from pathlib import Path

from pydantic import TypeAdapter
from sediment_core import (
    EVIDENCE_INVENTORY_LIMIT,
    EVIDENCE_REQUEST_BYTES_LIMIT,
    EvidenceInventory,
    EvidenceManifest,
    EvidenceRead,
    EvidenceReference,
    EvidenceSchemaVersion,
    NonEmptyId,
    validate_evidence_references,
)

from .client import ClientError, read_evidence


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite JSON number")
    return result


def _reject_constant(value):
    raise ValueError("non-finite JSON number")


def _decode(content: bytes):
    return json.loads(
        content.decode("utf-8"),
        object_pairs_hook=_object,
        parse_float=_finite_float,
        parse_constant=_reject_constant,
    )


def _selection(path: Path) -> tuple[EvidenceReference, ...]:
    with path.open("rb") as source:
        content = source.read(EVIDENCE_REQUEST_BYTES_LIMIT + 1)
    if len(content) > EVIDENCE_REQUEST_BYTES_LIMIT:
        raise ClientError("evidence selection exceeds the 64 KiB limit")
    value = _decode(content)
    if not isinstance(value, dict) or set(value) != {"schema_version", "references"}:
        raise ValueError("invalid selection envelope")
    TypeAdapter(EvidenceSchemaVersion).validate_python(value["schema_version"])
    references = TypeAdapter(tuple[EvidenceReference, ...]).validate_python(
        value["references"]
    )
    return validate_evidence_references(references)


def _unchanged(source, canonical) -> bool:
    """Reject default-filled or normalized wire data without another part schema."""
    if isinstance(canonical, datetime):
        return isinstance(source, str) and datetime.fromisoformat(source) == canonical
    if isinstance(canonical, dict):
        return (
            isinstance(source, dict)
            and source.keys() == canonical.keys()
            and all(_unchanged(source[key], value) for key, value in canonical.items())
        )
    if isinstance(canonical, (list, tuple)):
        return (
            isinstance(source, list)
            and len(source) == len(canonical)
            and all(_unchanged(a, b) for a, b in zip(source, canonical, strict=True))
        )
    return type(source) is type(canonical) and source == canonical


def _response(content: bytes, shape):
    adapter = TypeAdapter(shape)
    source = _decode(content)
    result = adapter.validate_python(source)
    if not _unchanged(source, adapter.dump_python(result)):
        raise ValueError("noncanonical evidence response")
    return result


def _inventory(result: EvidenceInventory) -> None:
    calls = result.calls
    if (
        result.visible_inference_calls != len(calls)
        or len(calls) > EVIDENCE_INVENTORY_LIMIT
        or len({call.inference_call_id for call in calls}) != len(calls)
        or (not result.found and (calls or result.quarantined_inference_calls))
        or tuple(sorted(calls, key=lambda c: (c.observed_at, c.inference_call_id)))
        != calls
    ):
        raise ValueError("inconsistent evidence inventory")


def _manifest(result: EvidenceManifest, inference_call_id: NonEmptyId) -> None:
    if result.call.inference_call_id != inference_call_id:
        raise ValueError("Inference call mismatch")
    indices = {"input": 0, "output": 0}
    for message in result.messages:
        if message.message_index != indices[message.side] or (
            message.side == "input" and indices["output"]
        ):
            raise ValueError("inconsistent message order")
        indices[message.side] += 1
        for index, part in enumerate(message.parts):
            if part.reference != EvidenceReference(
                inference_call_id, message.side, message.message_index, index
            ):
                raise ValueError("inconsistent part reference")


def _publish(destination: Path, content: bytes) -> None:
    """Hard-link a complete private sibling file without replacing a destination."""
    fd, name = tempfile.mkstemp(prefix=".sediment-evidence-", dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, destination)
    finally:
        temporary.unlink()


def command(args: argparse.Namespace) -> int:
    """Run a remote evidence operation without opening the FactStore."""
    try:
        session_id = TypeAdapter(NonEmptyId).validate_python(args.session_id)
        params = {"session_id": session_id}
        if args.evidence_operation == "inventory":
            content = read_evidence("/query/evidence", params=params)
            result = _response(content, EvidenceInventory)
            _inventory(result)
        elif args.evidence_operation == "inspect":
            params["inference_call_id"] = TypeAdapter(NonEmptyId).validate_python(
                args.inference_call_id
            )
            content = read_evidence("/query/evidence/manifest", params=params)
            result = _response(content, EvidenceManifest)
            _manifest(result, params["inference_call_id"])
        else:
            destination = Path(args.output)
            if os.path.lexists(destination):
                raise ClientError("evidence output already exists")
            references = _selection(Path(args.references))
            body = json.dumps(
                {
                    "schema_version": 1,
                    "session_id": session_id,
                    "references": TypeAdapter(
                        tuple[EvidenceReference, ...]
                    ).dump_python(references),
                },
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("ascii")
            content = read_evidence("/query/evidence/read", body=body)
            result = _response(content, EvidenceRead)
            if tuple(item.reference for item in result.items) != references:
                raise ValueError("incomplete selection")
        if result.session_id != session_id:
            raise ValueError("Session mismatch")
        if args.evidence_operation == "fetch":
            _publish(destination, content)
            print(f"{destination}: {len(result.items)} items")
        else:
            print(content.decode("ascii"))
        return 0
    except ClientError:
        raise
    except (OSError, ValueError, TypeError, RecursionError):
        raise ClientError("evidence input, response, or output is invalid") from None
