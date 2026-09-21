# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Local JSONL destination for the export projections, with two guards the
RLVR artifacts depend on: the **empty-input no-truncate guard** and
**split-aware file naming**.

**Empty-input no-truncate guard.** A projection that finds nothing to emit
(every rollout skipped, an org with no facts, a filter that matched no rows)
must leave the filesystem exactly as it found it: *nothing written, existing
files untouched, a warning logged*. The failure this prevents is a re-run over
a transiently-empty input silently truncating a good dataset from a prior run
to zero bytes. So an empty row list is never "write an empty file" — it is
"write no file". The guard composes per output file, so an empty eval partition
never truncates a previously-written ``<name>.eval.jsonl``.

**Split-aware naming.** The eval holdout rides on each row via the
rollout's ``split``. When the canonical policy's ``eval_fraction`` is above zero,
rows partition into ``<name>.train.jsonl`` / ``<name>.eval.jsonl``; when
disabled with ``0.0``, a single ``<name>.jsonl`` is written and
the output is byte-for-byte what a deployment that never heard of the split
would produce. The caller passes ``split_enabled`` explicitly because an
all-``train`` partition is ambiguous otherwise: it could mean "split disabled"
or "split enabled, nothing landed in eval", and those produce different file
layouts.

Writes are atomic (temp file in the destination dir, then ``os.replace``) so a
crash mid-write cannot leave a half-written line that breaks the round-trip.
All nonempty split partitions serialize before any destination is replaced;
publication remains atomic per file, not across the sequence of replacements.
Local files only — remote destinations (S3/HF) stay deferred until a real user
pulls one in (ADR 0005), and cloud SDK imports are barred everywhere but this
package anyway (AGENTS.md).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from sediment_core import OperationalReportLimitExceeded
from sediment_derive import Split

logger = logging.getLogger("sediment.export.jsonl")


@dataclass(frozen=True)
class ExportRow:
    """One projected row plus the split side it belongs on. Projections return
    these; the writer partitions on ``split`` and serializes ``body``. Keeping
    the split beside the body (rather than digging it back out of
    ``body["split"]``) means the writer never has to know a
    projection's row schema."""

    split: Split
    body: dict


@dataclass(frozen=True)
class WriteResult:
    """What :func:`write_jsonl` did — the files it wrote (row count each) and
    the destination paths it skipped under the no-truncate guard. Returned so a
    runner can log or assert on the outcome without re-reading the disk."""

    written: dict[str, int] = field(default_factory=dict)  # path -> rows written
    skipped_empty: list[str] = field(default_factory=list)  # paths left untouched


def _split_path(base: Path, side: Split) -> Path:
    """``out/tasks.jsonl`` + ``"train"`` -> ``out/tasks.train.jsonl``. Inserts
    the side before the final suffix so the ``.jsonl`` extension is preserved."""
    return base.with_name(f"{base.stem}.{side}{base.suffix}")


def write_jsonl(
    rows: Iterable[ExportRow],
    base_path: str | Path,
    *,
    split_enabled: bool,
    max_bytes: int | None = None,
) -> WriteResult:
    """Consume rows once, staging every partition before atomic replacements.

    Empty input preserves existing files. Serialization failure in any partition
    preserves every destination. Replacements remain atomic per file. If supplied,
    ``max_bytes`` bounds encoded output across all partitions before each write.
    """
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 0):
        raise ValueError("max_bytes must be a nonnegative integer")
    encoded_bytes = 0

    def write_chunk(handle, chunk):
        nonlocal encoded_bytes
        # ensure_ascii=True makes every emitted character one UTF-8 byte.
        size = len(chunk)
        if max_bytes is not None and encoded_bytes + size > max_bytes:
            raise OperationalReportLimitExceeded(
                f"JSONL output exceeds {max_bytes} encoded bytes"
            )
        handle.write(chunk)
        encoded_bytes += size

    base = Path(base_path)
    targets = (
        [_split_path(base, "train"), _split_path(base, "eval")]
        if split_enabled
        else [base]
    )
    result = WriteResult()
    prepared = {}
    try:
        for row in rows:
            path = _split_path(base, row.split) if split_enabled else base
            if path not in targets:
                continue
            if path not in prepared:
                path.parent.mkdir(parents=True, exist_ok=True)
                descriptor, temporary = tempfile.mkstemp(
                    dir=path.parent, prefix=path.name, suffix=".tmp"
                )
                prepared[path] = [
                    os.fdopen(descriptor, "w", encoding="utf-8"),
                    temporary,
                    0,
                ]
            handle, _, _ = prepared[path]
            for chunk in json.JSONEncoder(
                ensure_ascii=True, allow_nan=False
            ).iterencode(row.body):
                write_chunk(handle, chunk)
            write_chunk(handle, "\n")
            prepared[path][2] += 1
        for handle, _, _ in prepared.values():
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
        for path in targets:
            if path not in prepared:
                result.skipped_empty.append(str(path))
                logger.warning("jsonl_empty_input_no_write", extra={"path": str(path)})
                continue
            _, temporary, count = prepared[path]
            os.replace(temporary, path)
            result.written[str(path)] = count
            logger.info("jsonl_written", extra={"path": str(path), "rows": count})
    finally:
        for handle, temporary, _ in prepared.values():
            handle.close()
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    return result
