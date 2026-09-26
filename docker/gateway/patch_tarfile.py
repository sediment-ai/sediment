# SPDX-License-Identifier: AGPL-3.0-or-later
"""Backport CPython's CVE-2026-82049 fix to the released Wolfi runtime."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

# CPython 3.13 backport: b8f23e307097552eaea2604383a12ab280520d0d.
# The complete released 3.13.15-r8 source must match on both architectures.
SOURCE_SHA256 = "9fedddf7e814c226cb7e1ac0aa603092eda40047367ec00ad740a81484a17d01"
PATCHED_SHA256 = "9600de643ae7efed27009ee6c86aee60cebe335c0e732797c06db74dc719cefd"


def patch_tarfile(path: Path) -> None:
    source = path.read_bytes()
    if hashlib.sha256(source).hexdigest() != SOURCE_SHA256:
        raise ValueError("Unexpected released CPython tarfile source")
    old = b"os.link(tarinfo._link_target, targetpath)"
    new = b"os.link(os.path.realpath(tarinfo._link_target), targetpath)"
    updated = source.replace(old, new)
    if source.count(old) != 1 or hashlib.sha256(updated).hexdigest() != PATCHED_SHA256:
        raise ValueError("Unexpected CPython tarfile patch site")
    path.write_bytes(updated)
    for bytecode in (path.parent / "__pycache__").glob("tarfile.*.pyc"):
        bytecode.unlink()


if __name__ == "__main__":
    patch_tarfile(Path(sys.argv[1]))
