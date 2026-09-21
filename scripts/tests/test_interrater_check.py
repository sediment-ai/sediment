# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for scripts/interrater_check.py.

``scripts/`` is not a package, so the module is loaded by path.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "interrater_check.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("interrater_check", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


main = _load_module().main


def test_csv_input_prints_kappa_metrics(tmp_path, capsys) -> None:
    path = tmp_path / "labels.csv"
    path.write_text(
        "rater_a,rater_b\n"
        "yes,yes\n"
        "yes,yes\n"
        "yes,yes\n"
        "yes,yes\n"
        "yes,no\n"
        "no,yes\n"
        "no,no\n"
        "no,no\n"
        "no,no\n"
        "no,no\n"
    )

    code = main([str(path)])

    assert code == 0
    out = capsys.readouterr().out
    assert "rows: 10" in out
    assert "skipped_missing_labels: 0" in out
    assert "percent_agreement: 0.800000" in out
    assert "expected_agreement: 0.500000" in out
    assert "cohens_kappa: 0.600000" in out
    assert "degenerate: false" in out


def test_csv_custom_columns_and_missing_labels_are_counted(tmp_path, capsys) -> None:
    path = tmp_path / "labels.csv"
    path.write_text("alice,bob\naccept,accept\nreject,\n,reject\nreject,reject\n")

    code = main([str(path), "--rater-a", "alice", "--rater-b", "bob"])

    assert code == 0
    out = capsys.readouterr().out
    assert "rows: 2" in out
    assert "skipped_missing_labels: 2" in out
    assert "percent_agreement: 1.000000" in out
    assert "cohens_kappa: 1.000000" in out


def test_jsonl_input_accepts_objects_and_tuple_arrays(tmp_path, capsys) -> None:
    path = tmp_path / "labels.jsonl"
    rows = [
        {"rater_a": "yes", "rater_b": "yes"},
        ["yes", "no"],
        [False, False],
        {"rater_a": 1, "rater_b": 1},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")

    code = main([str(path)])

    assert code == 0
    out = capsys.readouterr().out
    assert "rows: 4" in out
    assert "percent_agreement: 0.750000" in out


def test_bad_input_is_a_clean_error_not_a_traceback(tmp_path, capsys) -> None:
    path = tmp_path / "labels.csv"
    path.write_text("alice,bob\nyes,yes\n")

    code = main([str(path)])

    assert code == 2
    err = capsys.readouterr().err
    assert "error:" in err
    assert "missing required column" in err
