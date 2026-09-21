# SPDX-License-Identifier: AGPL-3.0-or-later
"""Print inter-rater agreement metrics for two label columns.

Input is the standalone shape consumed by ``sediment_export.interrater``:
two parallel label lists over the same items. CSV input must have two label
columns, defaulting to ``rater_a`` and ``rater_b``. JSONL input may contain
objects with those keys or two-item arrays in tuple order.

Blank or null labels are absent judgments, not a category: the CLI skips
those rows and reports the skip count rather than fabricating labels.

    uv run python scripts/interrater_check.py labels.csv
    uv run python scripts/interrater_check.py labels.jsonl --rater-a alice --rater-b bob
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from sediment_export import Label, cohens_kappa

_CSV_SUFFIXES = {".csv"}
_JSONL_SUFFIXES = {".jsonl", ".ndjson"}


class LabelRows:
    def __init__(self) -> None:
        self.rater_a: list[Label] = []
        self.rater_b: list[Label] = []
        self.skipped_missing_labels = 0


def _parse_label(value: object) -> Label | None:
    if value is None:
        return None
    if isinstance(value, bool | int):
        return value
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return None
        return normalized
    raise ValueError(f"labels must be strings, ints, bools, or null: {value!r}")


def _append_row(rows: LabelRows, raw_a: object, raw_b: object) -> None:
    label_a = _parse_label(raw_a)
    label_b = _parse_label(raw_b)
    if label_a is None or label_b is None:
        rows.skipped_missing_labels += 1
        return
    rows.rater_a.append(label_a)
    rows.rater_b.append(label_b)


def _read_csv(path: Path, rater_a_column: str, rater_b_column: str) -> LabelRows:
    rows = LabelRows()
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            return rows
        missing = sorted({rater_a_column, rater_b_column} - set(reader.fieldnames))
        if missing:
            raise ValueError(f"CSV missing required column(s): {', '.join(missing)}")
        for row in reader:
            _append_row(rows, row[rater_a_column], row[rater_b_column])
    return rows


def _read_jsonl(path: Path, rater_a_column: str, rater_b_column: str) -> LabelRows:
    rows = LabelRows()
    with path.open() as f:
        for line_number, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    _append_row(rows, value[rater_a_column], value[rater_b_column])
                elif isinstance(value, list) and len(value) == 2:
                    _append_row(rows, value[0], value[1])
                else:
                    raise ValueError(
                        "JSONL rows must be objects or two-item arrays in tuple order"
                    )
            except (KeyError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(f"{path}:{line_number}: {exc}") from exc
    return rows


def _read_rows(
    path: Path, file_format: str, rater_a_column: str, rater_b_column: str
) -> LabelRows:
    if file_format == "auto":
        suffix = path.suffix.lower()
        if suffix in _CSV_SUFFIXES:
            file_format = "csv"
        elif suffix in _JSONL_SUFFIXES:
            file_format = "jsonl"
        else:
            raise ValueError(
                "could not infer input format from suffix; pass --format csv or jsonl"
            )
    if file_format == "csv":
        return _read_csv(path, rater_a_column, rater_b_column)
    if file_format == "jsonl":
        return _read_jsonl(path, rater_a_column, rater_b_column)
    raise ValueError(f"unsupported input format: {file_format}")


def _print_metrics(rows: LabelRows) -> None:
    result = cohens_kappa(rows.rater_a, rows.rater_b)
    print(f"rows: {result.n_items}")
    print(f"skipped_missing_labels: {rows.skipped_missing_labels}")
    print(f"percent_agreement: {result.observed_agreement:.6f}")
    print(f"expected_agreement: {result.expected_agreement:.6f}")
    print(f"cohens_kappa: {result.kappa:.6f}")
    print(f"degenerate: {str(result.degenerate).lower()}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="interrater_check", description=__doc__.splitlines()[0]
    )
    parser.add_argument("path", type=Path, help="CSV or JSONL labels file")
    parser.add_argument(
        "--format",
        choices=["auto", "csv", "jsonl"],
        default="auto",
        help="input format; default infers from .csv/.jsonl/.ndjson suffix",
    )
    parser.add_argument(
        "--rater-a",
        default="rater_a",
        help="first rater label column/key (default: rater_a)",
    )
    parser.add_argument(
        "--rater-b",
        default="rater_b",
        help="second rater label column/key (default: rater_b)",
    )
    args = parser.parse_args(argv)

    try:
        rows = _read_rows(args.path, args.format, args.rater_a, args.rater_b)
        _print_metrics(rows)
    except (OSError, TypeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
