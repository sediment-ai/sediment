# SPDX-License-Identifier: AGPL-3.0-or-later
"""Process interruption at the bundle directory publication boundary."""

from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import stat
import subprocess
import sys

import pytest

from sediment_export import (
    read_derived_bundle,
    validate_derived_bundle,
    write_derived_bundle,
)
from test_derived_bundle_io import _bundle, _tree_bytes


def _publication_worker(source: Path, destination: Path, boundary: str) -> None:
    bundle = read_derived_bundle(source)
    original_rename = Path.rename

    def pause(staging: Path) -> None:
        print(json.dumps({"boundary": boundary, "staging": str(staging)}), flush=True)
        sys.stdin.read(1)
        raise AssertionError("the parent must interrupt the paused worker")

    def rename(staging: Path, target: Path) -> Path:
        assert target == destination
        if boundary == "before":
            pause(staging)
        result = original_rename(staging, target)
        if boundary == "after":
            pause(staging)
        return result

    if boundary != "replay":
        Path.rename = rename
    try:
        write_derived_bundle(bundle, destination)
    except FileExistsError:
        print(json.dumps({"status": "existing_destination"}), flush=True)
    else:
        print(json.dumps({"status": "published"}), flush=True)
    finally:
        Path.rename = original_rename


def _assert_complete_private_bundle(path: Path, expected: dict[str, bytes]) -> None:
    assert not path.is_symlink()
    assert stat.S_IMODE(path.stat().st_mode) == 0o700
    assert _tree_bytes(path) == expected
    for item in path.iterdir():
        assert item.is_file() and not item.is_symlink()
        assert stat.S_IMODE(item.stat().st_mode) == 0o600
    restored = read_derived_bundle(path)
    validate_derived_bundle(restored)
    assert restored == _bundle()
    manifest = json.loads((path / "manifest.json").read_bytes())
    assert manifest["counts"] == {
        "attributed_completions": 1,
        "rollouts": 1,
        "inference_calls": 1,
        "inference_call_identities": 1,
        "repository_identities": 0,
        "repository_renames": 0,
    }


@pytest.mark.parametrize("boundary", ["before", "after"])
def test_bundle_process_interruption_preserves_publication_and_replay(
    tmp_path: Path, boundary: str
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "published"
    write_derived_bundle(_bundle(), source)
    expected = _tree_bytes(source)
    # A sibling with a similar name must survive cleanup of this worker's stage.
    unrelated = tmp_path / ".published.other-owner"
    unrelated.mkdir()
    (unrelated / "sentinel").write_bytes(b"preserve")
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        str(source),
        str(destination),
    ]
    env = os.environ.copy()
    staging = None
    try:
        with subprocess.Popen(
            [*command, boundary],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as child:
            try:
                with selectors.DefaultSelector() as ready:
                    ready.register(child.stdout, selectors.EVENT_READ)
                    assert ready.select(timeout=15), "worker never reached rename"
                receipt = json.loads(child.stdout.readline())
                assert receipt["boundary"] == boundary
                announced = Path(receipt["staging"])
                assert announced.parent == destination.parent
                assert announced.name.startswith(f".{destination.name}.")
                assert announced != unrelated
                staging = announced
                if boundary == "before":
                    assert not destination.exists()
                    _assert_complete_private_bundle(staging, expected)
                else:
                    assert not staging.exists()
                    _assert_complete_private_bundle(destination, expected)
                child.kill()
                output, errors = child.communicate(timeout=10)
                assert child.returncode == -signal.SIGKILL
                assert output == errors == ""
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=10)

        if boundary == "before":
            assert not destination.exists()
            # SIGKILL prevents Python cleanup; the prepared directory stays private.
            _assert_complete_private_bundle(staging, expected)
        else:
            _assert_complete_private_bundle(destination, expected)
        replay = subprocess.run(
            [*command, "replay"],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
        assert json.loads(replay.stdout) == {
            "status": "published" if boundary == "before" else "existing_destination"
        }
        assert replay.stderr == ""
        _assert_complete_private_bundle(destination, expected)
        assert _tree_bytes(source) == expected
    finally:
        if staging is not None and staging.exists():
            assert not staging.is_symlink()
            shutil.rmtree(staging)
    assert staging is not None and not staging.exists()
    assert _tree_bytes(unrelated) == {"sentinel": b"preserve"}
    assert set(tmp_path.iterdir()) == {source, destination, unrelated}


if __name__ == "__main__":
    _publication_worker(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3])
