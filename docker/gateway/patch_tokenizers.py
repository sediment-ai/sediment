# SPDX-License-Identifier: AGPL-3.0-or-later
"""Declare the tested Hub 2 compatibility of the pinned Tokenizers release."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import sys
from pathlib import Path

SOURCE_SHA256 = "67226b8d55c78feec1f100f38cd5b04ad7385af8484696b7fd332c1d8cec4345"
PATCHED_SHA256 = "1790601ecfac9a88825ef4d0b31dc47e5703abae979e62a740c4cfe2374b66e0"


def record_hash(content: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest())
    return "sha256=" + digest.rstrip(b"=").decode("ascii")


def patch_tokenizers(root: Path) -> None:
    metadata = root / "METADATA"
    source = metadata.read_bytes()
    if hashlib.sha256(source).hexdigest() != SOURCE_SHA256:
        raise ValueError("Unexpected Tokenizers metadata")
    old = b"Requires-Dist: huggingface-hub>=0.16.4,<2.0\n"
    updated = source.replace(old, b"Requires-Dist: huggingface-hub==2.0.0\n")
    if source.count(old) != 1 or hashlib.sha256(updated).hexdigest() != PATCHED_SHA256:
        raise ValueError("Unexpected Tokenizers dependency patch site")
    record = root / "RECORD"
    rows = list(csv.reader(io.StringIO(record.read_text())))
    matches = [row for row in rows if row[0] == f"{root.name}/METADATA"]
    if len(matches) != 1 or matches[0][1:] != [record_hash(source), str(len(source))]:
        raise ValueError("Unexpected Tokenizers metadata record")
    matches[0][1:] = [record_hash(updated), str(len(updated))]
    metadata.write_bytes(updated)
    with record.open("w", newline="") as output:
        csv.writer(output, lineterminator="\n").writerows(rows)


if __name__ == "__main__":
    patch_tokenizers(Path(sys.argv[1]))
