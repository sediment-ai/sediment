# SPDX-License-Identifier: AGPL-3.0-or-later
"""Private trainer-row staging shares canonical execution resource ownership."""

from pathlib import Path

import pytest

from sediment_export.derived_bundle import BundleCapacityError, BundleLimits
from sediment_export.jsonl import ExportRow


def test_staged_export_rows_are_repeatable_and_preserve_values(tmp_path):
    from sediment_export.staged_rows import ExportRowStore

    row = ExportRow("train", {"prompt": "x\ud800", "n": 1, "nested": [False, None]})
    with ExportRowStore(temporary_parent=tmp_path) as stage:
        rows = stage.records("tasks")
        rows.append(row)
        rows.seal()
        assert list(rows) == [row]
        assert rows[0] == row
        assert rows.record_bytes(0) == rows.encoded_bytes
        directory = stage.directory
        assert directory.stat().st_mode & 0o777 == 0o700
    assert not directory.exists()
    with pytest.raises(ValueError, match="closed"):
        rows[0]


def test_staged_export_members_share_quota_and_cleanup(tmp_path):
    from sediment_export.staged_rows import ExportRowStore

    with pytest.raises(BundleCapacityError):
        with ExportRowStore(
            temporary_parent=tmp_path, limits=BundleLimits(max_staging_bytes=256)
        ) as stage:
            stage.records("tasks").append(ExportRow("train", {"text": "x" * 128}))
            stage.records("rollouts").append(ExportRow("train", {"text": "x" * 128}))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("name", ["../escape", "a/b", "/tmp/escape", "", "x.jsonl"])
def test_private_row_names_cannot_escape_owner(tmp_path, name):
    from sediment_export.staged_rows import ExportRowStore

    with ExportRowStore(temporary_parent=tmp_path) as stage:
        with pytest.raises(ValueError, match="name"):
            stage.records(name)


def test_staged_rows_preserve_projector_dictionary_order(tmp_path):
    from sediment_export.staged_rows import ExportRowStore
    from sediment_export.jsonl import write_jsonl

    row = ExportRow("train", {"z": {"y": 1, "a": 2}, "a": "last"})
    write_jsonl([row], tmp_path / "expected.jsonl", split_enabled=False)
    with ExportRowStore(temporary_parent=tmp_path) as stage:
        rows = stage.records("rows")
        rows.append(row)
        rows.seal()
        write_jsonl(rows, tmp_path / "actual.jsonl", split_enabled=False)
    assert (tmp_path / "actual.jsonl").read_bytes() == (
        tmp_path / "expected.jsonl"
    ).read_bytes()


def test_staged_rows_preserve_surrogate_pair_codepoints(tmp_path):
    from sediment_export.staged_rows import ExportRowStore

    row = ExportRow("train", {"text": "\ud83d\ude00"})
    with ExportRowStore(temporary_parent=tmp_path) as stage:
        rows = stage.records("rows")
        rows.append(row)
        rows.seal()
        assert rows[0].body["text"] == row.body["text"]


def test_killed_owner_stage_is_removed_by_next_store_and_live_stage_kept(tmp_path):
    import signal
    import subprocess
    import sys

    from sediment_export.staged_rows import ExportRowStore

    owner = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys, time\n"
            "from pathlib import Path\n"
            "from sediment_export.jsonl import ExportRow\n"
            "from sediment_export.staged_rows import ExportRowStore\n"
            "stage = ExportRowStore(temporary_parent=Path(sys.argv[1]))\n"
            "stage.records('tasks').append(ExportRow('train', {'secret': 'raw'}))\n"
            "print(stage.directory, flush=True)\n"
            "time.sleep(60)\n",
            str(tmp_path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        abandoned = Path(owner.stdout.readline().strip())
        with ExportRowStore(temporary_parent=tmp_path) as live:
            assert abandoned.is_dir()  # a running owner is never swept
            owner.send_signal(signal.SIGKILL)
            owner.wait(timeout=10)
            assert (abandoned / "tasks.jsonl").exists()
            with ExportRowStore(temporary_parent=tmp_path):
                assert not abandoned.exists()
                assert live.directory.is_dir()
        assert list(tmp_path.iterdir()) == []
    finally:
        owner.kill()
        owner.wait(timeout=10)
