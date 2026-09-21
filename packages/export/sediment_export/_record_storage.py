# SPDX-License-Identifier: AGPL-3.0-or-later
"""Private offset-indexed execution files shared by canonical and trainer rows."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Iterable, Sequence
from pathlib import Path

try:
    import fcntl
except ImportError:  # no POSIX locks: stages still work, abandoned ones are kept
    fcntl = None

logger = logging.getLogger("sediment.export.record_storage")

# The lock sits beside its stage, so stage contents stay exactly the records.
_STAGE_LOCK_SUFFIX = ".sediment-stage-lock"


class RecordStore:
    """Own private disposable records for one run, within a shared byte quota.

    Use as a context manager. Append to ``records(name)``, then seal each
    population before constructing a DerivedBundle. All access ends on exit.
    Encoding and restoration belong to the two concrete canonical/trainer owners.
    """

    def __init__(
        self,
        *,
        limits,
        capacity_error,
        validation_error,
        temporary_parent: Path | None = None,
        _prefix: str = ".sediment-bundle-",
    ):
        self.limits = limits
        self._capacity_error = capacity_error
        self._validation_error = validation_error
        # A custom prefix marks the bundle publication stage. It is renamed whole
        # into its destination, and an interrupted one stays for the operator.
        claims = _prefix.startswith(".sediment-")
        if claims:
            # Sweep first: an abandoned stage can be what filled the volume.
            _remove_abandoned_stages(Path(temporary_parent or tempfile.gettempdir()))
        self._temporary = tempfile.TemporaryDirectory(
            prefix=_prefix, dir=temporary_parent
        )
        self.directory = Path(self._temporary.name)
        os.chmod(self.directory, 0o700)
        self._lock = _claim_stage(self.directory) if claims else None
        self._records: dict[str, FileRecords] = {}
        self._bytes = 0
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exception):
        self.close()

    def close(self):
        self.closed = True
        self._temporary.cleanup()
        if self._lock is not None:
            _stage_lock(self.directory).unlink(missing_ok=True)
            os.close(self._lock)
            self._lock = None

    def records(self, name: str, *, encoder, decoder, restore) -> FileRecords:
        self._check_open()
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError("invalid private record name")
        if name not in self._records:
            self._records[name] = FileRecords(self, name, encoder, decoder, restore)
        return self._records[name]

    def _check_open(self):
        if self.closed:
            raise ValueError("bundle record store is closed")

    def _reserve(self, size):
        if self._bytes + size > self.limits.max_staging_bytes:
            raise self._capacity_error("bundle staging exceeds byte budget")
        self._bytes += size


class FileRecords(Sequence):
    """Repeated record access with compact file offsets only.

    ``seal`` ends appends. Indexing and iteration reconstruct records without
    caching decoded content. The owning RecordStore controls lifetime.
    """

    def __init__(self, owner: RecordStore, name: str, encoder, decoder, restore):
        self._owner, self.name = owner, name
        self._encoder, self._decoder, self._restore = encoder, decoder, restore
        self.path = owner.directory / f"{name}.jsonl"
        self._offsets: list[tuple[int, int]] = []
        self._digest = hashlib.sha256()
        self._bytes = 0
        self._sealed = False
        _write_private(self.path, b"")

    def __len__(self):
        self._owner._check_open()
        return len(self._offsets)

    def __getitem__(self, index):
        self._owner._check_open()
        if not self._sealed:
            raise ValueError("bundle records must be sealed before reading")
        if isinstance(index, slice):
            return tuple(self[item] for item in range(*index.indices(len(self))))
        offset, size = self._offsets[index]
        with self.path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read(size)
        if len(data) != size:
            raise self._owner._validation_error("private bundle record is truncated")
        value = self._decoder(data, f"{self.name} row {index + 1}")
        del data
        return self._restore(value)

    def __iter__(self):
        for index in range(len(self)):
            yield self[index]

    def record_bytes(self, index: int) -> int:
        """Return the encoded record size without reading or decoding content."""
        self._owner._check_open()
        return self._offsets[index][1]

    @property
    def encoded_bytes(self) -> int:
        """Return the complete member's encoded size without materializing it."""
        self._owner._check_open()
        return self._bytes

    def append(self, row):
        self._owner._check_open()
        if self._sealed:
            raise ValueError("bundle records are sealed")
        start, digest = self._bytes, self._digest.copy()
        try:
            with self.path.open("ab") as handle:
                for chunk in self._encoder(row):
                    if (
                        self._bytes - start + len(chunk)
                        > self._owner.limits.max_record_bytes
                    ):
                        raise self._owner._capacity_error(
                            "bundle record exceeds byte budget"
                        )
                    self._owner._reserve(len(chunk))
                    self._bytes += len(chunk)
                    handle.write(chunk)
                    self._digest.update(chunk)
        except BaseException:
            with self.path.open("r+b") as handle:
                handle.truncate(start)
            self._owner._bytes -= self._bytes - start
            self._bytes, self._digest = start, digest
            raise
        self._offsets.append((start, self._bytes - start))

    def extend(self, rows: Iterable):
        for row in rows:
            self.append(row)

    def seal(self):
        self._owner._check_open()
        with self.path.open("rb") as handle:
            os.fsync(handle.fileno())
        self._sealed = True
        return self

    def _capture(self, source: Path, metadata: dict):
        """Copy and verify a member before exposing any canonical records."""
        if source.is_symlink() or not source.is_file():
            raise self._owner._validation_error(f"{source.name} is not a regular file")
        if source.stat().st_size != metadata["bytes"]:
            raise self._owner._validation_error(
                f"{source.name} byte count does not match"
            )
        # Copy in fixed buffers. Scan bounded lines only after checksum success.
        with source.open("rb") as original, self.path.open("wb") as target:
            while chunk := original.read(64 * 1024):
                self._owner._reserve(len(chunk))
                target.write(chunk)
                self._digest.update(chunk)
                self._bytes += len(chunk)
        if self._bytes != metadata["bytes"]:
            raise self._owner._validation_error(
                f"{source.name} byte count does not match"
            )
        if self._digest.hexdigest() != metadata["sha256"]:
            raise self._owner._validation_error(f"{source.name} SHA-256 does not match")
        with self.path.open("rb") as handle:
            while True:
                start = handle.tell()
                line = handle.readline(self._owner.limits.max_record_bytes + 1)
                if not line:
                    break
                if len(line) > self._owner.limits.max_record_bytes:
                    raise self._owner._capacity_error(
                        "bundle record exceeds byte budget"
                    )
                self._offsets.append((start, len(line)))
        if len(self._offsets) != metadata["rows"]:
            raise self._owner._validation_error(
                f"{source.name} row count does not match"
            )
        self.seal()


def _stage_lock(directory: Path) -> Path:
    return directory.with_name(directory.name + _STAGE_LOCK_SUFFIX)


def _claim_stage(directory: Path) -> int | None:
    """Hold a lock for the stage's lifetime. The kernel releases it if the owner dies."""
    if fcntl is None:
        return None
    lock = _stage_lock(directory)
    pending = lock.with_name(lock.name + ".pending")
    descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    # Lock before the sweepable name exists, so no sweeper can win the claim.
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    os.rename(pending, lock)
    return descriptor


def _remove_abandoned_stages(parent: Path) -> None:
    """Remove this user's stages whose owner died without cleanup.

    SIGTERM and SIGKILL skip context exit, and a disk-backed parent keeps the
    raw payload. A free lock proves the owner is gone; a live one is skipped.
    """
    if fcntl is None:
        return
    try:
        names = [n for n in os.listdir(parent) if n.endswith(_STAGE_LOCK_SUFFIX)]
    except OSError:
        return
    for name in names:
        lock = parent / name
        try:
            descriptor = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            continue
        try:
            status = os.fstat(descriptor)
            if not stat.S_ISREG(status.st_mode) or status.st_uid != os.getuid():
                continue
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            stage = parent / name.removesuffix(_STAGE_LOCK_SUFFIX)
            if stage.is_dir() and not stage.is_symlink():
                shutil.rmtree(stage)
                logger.warning(
                    "abandoned_private_stage_removed", extra={"path": str(stage)}
                )
            lock.unlink(missing_ok=True)
        except OSError:
            continue  # a live owner holds the lock, or the stage is not removable
        finally:
            os.close(descriptor)


def _write_private(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
