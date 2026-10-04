# SPDX-License-Identifier: AGPL-3.0-or-later
"""Declare the tested Hub 2 compatibility of the pinned Hub-bounded releases."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import sys
from pathlib import Path

# dist-info directory: (source METADATA sha256, patched sha256, original bound)
PATCHES = {
    "tokenizers-0.23.1.dist-info": (
        "67226b8d55c78feec1f100f38cd5b04ad7385af8484696b7fd332c1d8cec4345",
        "50ca680702d20ef3a3c47f857ae45b8879ccbda92a0eda41dd162ad8554134ca",
        b"Requires-Dist: huggingface-hub>=0.16.4,<2.0\n",
    ),
    "litellm-1.104.0.dist-info": (
        "193be9cdc4da95ceb32602f1e9084aaa161cd4426d6a51fb951bc60191d4c906",
        "d6e69d5a13b8c81371d67d7dd1190ed51e4fc3be66c16e3af98120970145c52d",
        b"Requires-Dist: huggingface-hub>=0.34.0,<2.0\n",
    ),
}


def record_hash(content: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest())
    return "sha256=" + digest.rstrip(b"=").decode("ascii")


def patch_hub_metadata(root: Path) -> None:
    if root.name not in PATCHES:
        raise ValueError(f"Unexpected Hub-bounded distribution: {root.name}")
    source_sha256, patched_sha256, old = PATCHES[root.name]
    metadata = root / "METADATA"
    source = metadata.read_bytes()
    if hashlib.sha256(source).hexdigest() != source_sha256:
        raise ValueError(f"Unexpected {root.name} metadata")
    updated = source.replace(old, b"Requires-Dist: huggingface-hub==2.1.1\n")
    if source.count(old) != 1 or hashlib.sha256(updated).hexdigest() != patched_sha256:
        raise ValueError(f"Unexpected {root.name} dependency patch site")
    record = root / "RECORD"
    rows = list(csv.reader(io.StringIO(record.read_text())))
    matches = [row for row in rows if row[0] == f"{root.name}/METADATA"]
    if len(matches) != 1 or matches[0][1:] != [record_hash(source), str(len(source))]:
        raise ValueError(f"Unexpected {root.name} metadata record")
    matches[0][1:] = [record_hash(updated), str(len(updated))]
    metadata.write_bytes(updated)
    with record.open("w", newline="") as output:
        csv.writer(output, lineterminator="\n").writerows(rows)


if __name__ == "__main__":
    for path in sys.argv[1:]:
        patch_hub_metadata(Path(path))
