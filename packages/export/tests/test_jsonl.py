# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Local JSONL destination tests: the empty-input no-truncate guard and
split-aware file naming. Real files on disk, real round-trips (per AGENTS.md).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sediment_export import ExportRow, write_jsonl


def _rows(*row_definitions: tuple[str, dict]) -> list[ExportRow]:
    return [ExportRow(split=split, body=body) for split, body in row_definitions]


def _read_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_single_file_when_split_disabled(tmp_path: Path) -> None:
    out = tmp_path / "tasks.jsonl"
    rows = _rows(("train", {"instance_id": "a"}), ("train", {"instance_id": "b"}))
    result = write_jsonl(rows, out, split_enabled=False)

    assert out.exists()
    assert _read_lines(out) == [{"instance_id": "a"}, {"instance_id": "b"}]
    # No split files when disabled.
    assert not (tmp_path / "tasks.train.jsonl").exists()
    assert not (tmp_path / "tasks.eval.jsonl").exists()
    assert result.written == {str(out): 2}


def test_split_partitions_into_train_and_eval_files(tmp_path: Path) -> None:
    out = tmp_path / "tasks.jsonl"
    rows = _rows(
        ("train", {"instance_id": "t1"}),
        ("eval", {"instance_id": "e1"}),
        ("train", {"instance_id": "t2"}),
    )
    write_jsonl(rows, out, split_enabled=True)

    train = tmp_path / "tasks.train.jsonl"
    eval_ = tmp_path / "tasks.eval.jsonl"
    assert not out.exists()  # the unsplit path is never written in split mode
    assert [r["instance_id"] for r in _read_lines(train)] == ["t1", "t2"]
    assert [r["instance_id"] for r in _read_lines(eval_)] == ["e1"]


def test_split_partition_is_a_total_no_overlap_cover(tmp_path: Path) -> None:
    # train + eval == all rows, disjoint — the holdout invariant.
    out = tmp_path / "rollouts.jsonl"
    rows = _rows(
        ("train", {"id": 1}),
        ("eval", {"id": 2}),
        ("eval", {"id": 3}),
        ("train", {"id": 4}),
    )
    write_jsonl(rows, out, split_enabled=True)
    train_ids = {r["id"] for r in _read_lines(tmp_path / "rollouts.train.jsonl")}
    eval_ids = {r["id"] for r in _read_lines(tmp_path / "rollouts.eval.jsonl")}
    assert train_ids.isdisjoint(eval_ids)
    assert train_ids | eval_ids == {1, 2, 3, 4}


def test_empty_input_writes_nothing_and_leaves_existing_file_untouched(
    tmp_path: Path,
) -> None:
    # The no-truncate guard: a re-run over transiently-empty input must not
    # truncate a good dataset from a prior run.
    out = tmp_path / "tasks.jsonl"
    prior = '{"instance_id": "kept"}\n'
    out.write_text(prior)

    result = write_jsonl([], out, split_enabled=False)

    assert out.read_text() == prior  # byte-for-byte untouched
    assert result.written == {}
    assert result.skipped_empty == [str(out)]


def test_empty_eval_partition_does_not_truncate_prior_eval_file(
    tmp_path: Path,
) -> None:
    # Per-file composition of the guard: an all-train run leaves a previously
    # written eval file alone rather than truncating it to empty.
    eval_ = tmp_path / "tasks.eval.jsonl"
    prior = '{"instance_id": "old-eval"}\n'
    eval_.write_text(prior)

    write_jsonl(
        _rows(("train", {"instance_id": "t1"})),
        tmp_path / "tasks.jsonl",
        split_enabled=True,
    )

    assert (tmp_path / "tasks.train.jsonl").exists()
    assert eval_.read_text() == prior  # untouched, not truncated


def test_creates_parent_directory_on_write(tmp_path: Path) -> None:
    out = tmp_path / "nested" / "deeper" / "tasks.jsonl"
    write_jsonl(_rows(("train", {"x": 1})), out, split_enabled=False)
    assert out.exists()


def test_empty_input_does_not_create_parent_directory(tmp_path: Path) -> None:
    out = tmp_path / "nested" / "tasks.jsonl"
    write_jsonl([], out, split_enabled=False)
    assert not (tmp_path / "nested").exists()  # nothing created for empty input


def test_unicode_content_round_trips(tmp_path: Path) -> None:
    out = tmp_path / "tasks.jsonl"
    body = {"reference_patch": "+++ b/café.py\n+données = 1\n", "emoji": "✅"}
    write_jsonl(_rows(("train", body)), out, split_enabled=False)
    assert _read_lines(out) == [body]


def test_rewrite_replaces_prior_contents(tmp_path: Path) -> None:
    # A non-empty re-run atomically replaces (not appends to) the file.
    out = tmp_path / "tasks.jsonl"
    write_jsonl(
        _rows(("train", {"id": 1}), ("train", {"id": 2})), out, split_enabled=False
    )
    write_jsonl(_rows(("train", {"id": 9})), out, split_enabled=False)
    assert _read_lines(out) == [{"id": 9}]


def test_no_temp_files_left_behind(tmp_path: Path) -> None:
    out = tmp_path / "tasks.jsonl"
    write_jsonl(_rows(("train", {"id": 1})), out, split_enabled=False)
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "tasks.jsonl"]
    assert leftovers == []


def test_midstream_serialization_error_leaves_destination_and_dir_untouched(
    tmp_path: Path,
) -> None:
    # Streaming rows one at a time must still keep the destination untouched on a
    # serialization error: a non-JSON-able body in a later row (here a set) makes
    # json.dumps raise after earlier rows are already in the temp file. The
    # os.replace never runs, so the prior file survives and no temp is left.
    out = tmp_path / "tasks.jsonl"
    prior = '{"instance_id": "kept"}\n'
    out.write_text(prior)

    rows = _rows(
        ("train", {"instance_id": "ok"}),
        ("train", {"instance_id": {1, 2}}),  # a set is not JSON-serializable
    )
    with pytest.raises(TypeError):
        write_jsonl(rows, out, split_enabled=False)

    assert out.read_text() == prior  # destination untouched, not truncated
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "tasks.jsonl"]
    assert leftovers == []  # no temp file behind


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -float("inf"), {1}])
def test_all_split_partitions_are_prepared_before_replacement(tmp_path, invalid):
    train = tmp_path / "tasks.train.jsonl"
    evaluation = tmp_path / "tasks.eval.jsonl"
    train.write_bytes(b"prior train\n")
    evaluation.write_bytes(b"prior eval\n")
    with pytest.raises((ValueError, TypeError)):
        write_jsonl(
            _rows(("train", {"valid": 1}), ("eval", {"nested": [invalid]})),
            tmp_path / "tasks.jsonl",
            split_enabled=True,
        )
    assert train.read_bytes() == b"prior train\n"
    assert evaluation.read_bytes() == b"prior eval\n"
    assert set(tmp_path.iterdir()) == {train, evaluation}


def test_jsonl_escapes_report_strings_without_training_eligibility(tmp_path):
    out = tmp_path / "report.jsonl"
    body = {"\ud800": ["\udfff", "é", "\x00"]}
    write_jsonl(_rows(("train", body)), out, split_enabled=False)
    assert out.read_bytes().isascii()
    assert _read_lines(out) == [body]


def _replay_rows():
    return _rows(
        ("train", {"generation": "next", "id": 1, "text": "exact\n\x00\ud800"}),
        ("eval", {"generation": "next", "id": 2}),
        ("train", {"generation": "next", "id": 3}),
        ("eval", {"generation": "next", "id": 4}),
    )


def _publication_worker(destination, mode):
    import os
    import sys
    from dataclasses import asdict

    original_replace = os.replace
    replacements = 0

    def replace(source, target):
        nonlocal replacements
        if mode == "failure" and replacements == 1:
            raise OSError("interrupted between split replacements")
        original_replace(source, target)
        replacements += 1
        if mode == "interrupt" and replacements == 1:
            print("first replacement complete", flush=True)
            sys.stdin.read(1)

    if mode != "replay":
        os.replace = replace
    result = write_jsonl(_replay_rows(), destination, split_enabled=True)
    print(json.dumps(asdict(result)), flush=True)


@pytest.mark.parametrize("mode", ["failure", "interrupt"])
def test_split_publication_process_failure_replays_to_exact_bytes(tmp_path, mode):
    import os
    import selectors
    import subprocess
    import sys

    out = tmp_path / "published" / "tasks.jsonl"
    old = _rows(("train", {"generation": "prior"}), ("eval", {"generation": "prior"}))
    write_jsonl(old, out, split_enabled=True)
    train = out.with_name("tasks.train.jsonl")
    evaluation = out.with_name("tasks.eval.jsonl")
    old_eval = evaluation.read_bytes()
    expected_out = tmp_path / "expected" / "tasks.jsonl"
    expected = write_jsonl(_replay_rows(), expected_out, split_enabled=True)
    expected_bytes = {
        Path(path).name: Path(path).read_bytes() for path in expected.written
    }
    env = os.environ.copy()
    command = [sys.executable, str(Path(__file__).resolve()), str(out)]

    with subprocess.Popen(
        [*command, mode],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as child:
        try:
            if mode == "interrupt":
                with selectors.DefaultSelector() as ready:
                    ready.register(child.stdout, selectors.EVENT_READ)
                    assert ready.select(timeout=15), "worker never reached replacement"
                assert child.stdout.readline() == "first replacement complete\n"
                child.kill()
                child.wait(timeout=10)
                assert child.returncode < 0
            else:
                _, errors = child.communicate(timeout=15)
                assert child.returncode != 0
                assert "interrupted between split replacements" in errors
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)

    # A split export may expose two complete files from different generations.
    assert train.read_bytes() == expected_bytes[train.name]
    assert evaluation.read_bytes() == old_eval
    assert len(_read_lines(train)) == 2
    assert _read_lines(evaluation) == [{"generation": "prior"}]
    for path in (train, evaluation):
        assert path.read_bytes().endswith(b"\n")
        assert all(isinstance(row, dict) for row in _read_lines(path))
    replay = subprocess.run(
        [*command, "replay"],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    receipt = json.loads(replay.stdout)
    assert receipt == {
        "written": {str(train): 2, str(evaluation): 2},
        "skipped_empty": [],
    }
    assert {
        path.name: path.read_bytes() for path in (train, evaluation)
    } == expected_bytes
    assert {path.name: len(_read_lines(path)) for path in (train, evaluation)} == {
        train.name: 2,
        evaluation.name: 2,
    }
    no_eval = write_jsonl(
        [row for row in _replay_rows() if row.split == "train"], out, split_enabled=True
    )
    assert no_eval.skipped_empty == [str(evaluation)]
    assert {
        path.name: path.read_bytes() for path in (train, evaluation)
    } == expected_bytes
    assert write_jsonl([], out, split_enabled=True).written == {}
    assert {
        path.name: path.read_bytes() for path in (train, evaluation)
    } == expected_bytes
    # SIGKILL can leave prepared siblings. Remove only this worker's scratch files.
    for temporary in out.parent.glob("*.tmp"):
        temporary.unlink()


if __name__ == "__main__":
    import sys

    _publication_worker(sys.argv[1], sys.argv[2])
