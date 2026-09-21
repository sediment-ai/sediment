# SPDX-License-Identifier: AGPL-3.0-or-later
"""Byte capacity applies before output growth across every JSONL partition."""

import json

import pytest
from sediment_core import OperationalReportLimitExceeded
from sediment_export.jsonl import ExportRow, write_jsonl


def test_jsonl_byte_capacity_counts_encoded_output_and_all_partitions(tmp_path):
    rows = [
        ExportRow("train", {"text": "\u00e9"}),
        ExportRow("eval", {"text": "\u00e9"}),
    ]
    per_row = len((json.dumps(rows[0].body, ensure_ascii=True) + "\n").encode("utf-8"))
    train = tmp_path / "rows.train.jsonl"
    evaluation = tmp_path / "rows.eval.jsonl"
    train.write_text("prior train\n")
    evaluation.write_text("prior eval\n")
    with pytest.raises(OperationalReportLimitExceeded, match="encoded bytes"):
        write_jsonl(
            iter(rows),
            tmp_path / "rows.jsonl",
            split_enabled=True,
            max_bytes=per_row * 2 - 1,
        )
    assert train.read_text() == "prior train\n"
    assert evaluation.read_text() == "prior eval\n"
    assert set(tmp_path.iterdir()) == {train, evaluation}
    result = write_jsonl(
        iter(rows), tmp_path / "rows.jsonl", split_enabled=True, max_bytes=per_row * 2
    )
    assert sum(result.written.values()) == 2
    assert train.stat().st_size + evaluation.stat().st_size == per_row * 2


def test_jsonl_zero_budget_preserves_empty_destinations(tmp_path):
    path = tmp_path / "rows.jsonl"
    path.write_text("prior\n")
    result = write_jsonl(iter(()), path, split_enabled=False, max_bytes=0)
    assert result.written == {}
    assert path.read_text() == "prior\n"


@pytest.mark.parametrize("budget", [-1, True, 1.5])
def test_jsonl_refuses_invalid_budget_before_filesystem_work(tmp_path, budget):
    with pytest.raises(ValueError, match="nonnegative integer"):
        write_jsonl([], tmp_path / "rows.jsonl", split_enabled=False, max_bytes=budget)
    assert list(tmp_path.iterdir()) == []
