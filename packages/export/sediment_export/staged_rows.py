# SPDX-License-Identifier: AGPL-3.0-or-later
"""Repeatable private ExportRow storage for bounded training orchestration."""

from pathlib import Path

from ._record_storage import RecordStore, FileRecords
from .derived_bundle import (
    BundleCapacityError,
    BundleLimits,
    BundleValidationError,
    _private_json_chunks,
    _decode_private_record,
    _require_keys,
    _validate_split,
)
from .jsonl import ExportRow


def _export_chunks(row):
    # Trainer bytes use projector dictionary order, unlike sorted canonical JSON.
    value = {"split": row.split, "body": row.body}
    yield from _private_json_chunks(value)


def _restore_export_row(value):
    _require_keys(value, {"split", "body"}, "staged ExportRow")
    _validate_split(value["split"])
    if not isinstance(value["body"], dict):
        raise BundleValidationError("staged ExportRow body must be an object")
    return ExportRow(value["split"], value["body"])


class ExportRowStore(RecordStore):
    """Stage independent trainer populations under one private byte quota.

    Use as a context manager. Named populations support append/extend, seal,
    repeated iteration, indexing, and encoded-byte inspection. Access expires
    on exit. These files are unpublished execution resources.
    """

    def __init__(
        self,
        *,
        limits: BundleLimits | None = None,
        temporary_parent: Path | None = None,
    ):
        super().__init__(
            limits=limits or BundleLimits(),
            capacity_error=BundleCapacityError,
            validation_error=BundleValidationError,
            temporary_parent=temporary_parent,
            _prefix=".sediment-training-",
        )

    def records(self, name: str) -> FileRecords:
        return super().records(
            name,
            encoder=_export_chunks,
            decoder=_decode_private_record,
            restore=_restore_export_row,
        )
