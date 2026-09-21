# SPDX-License-Identifier: AGPL-3.0-or-later
"""Print calibration and discrimination metrics for labeled confidence rows.

Each input row must carry ``predicted_confidence``, ``actual_outcome``,
``recipe_id``, ``recipe_version``, and exactly one source mode: a
``chosen_label_source``/``rejected_label_source`` pair, ``label_source``, or
``eligibility_source``. The command computes each recipe and source stratum
separately. It never pools heterogeneous training evidence.

CSV input uses those headers. JSONL input uses objects with the same fields.

    uv run python scripts/calibration_check.py labels.csv
    uv run python scripts/calibration_check.py labels.jsonl --bins 20
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from sediment_export import (
    CalibrationPair,
    auroc,
    brier_score,
    bucket_inversions,
    compare_calibration_policies,
    expected_calibration_error,
    reliability_diagram_data,
)
from sediment_export.calibration import CalibrationRecord, stratify_calibration

_CSV_SUFFIXES = {".csv"}
_JSONL_SUFFIXES = {".jsonl", ".ndjson"}
_TRUE_VALUES = {"1", "true", "t", "yes", "y"}
_FALSE_VALUES = {"0", "false", "f", "no", "n"}


def _parse_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUE_VALUES:
            return True
        if normalized in _FALSE_VALUES:
            return False
    raise ValueError(f"invalid actual_outcome value: {value!r}")


def _parse_pair(predicted: object, actual: object) -> CalibrationPair:
    try:
        confidence = float(predicted)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid predicted_confidence value: {predicted!r}") from exc
    return (confidence, _parse_bool(actual))


def _parse_record(row: dict[str, object]) -> CalibrationRecord:
    confidence, actual = _parse_pair(row["predicted_confidence"], row["actual_outcome"])
    try:
        recipe_version = int(row["recipe_version"])
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"invalid recipe_version value: {row['recipe_version']!r}"
        ) from exc
    return CalibrationRecord(
        predicted_confidence=confidence,
        actual_outcome=actual,
        recipe_id=str(row["recipe_id"]),
        recipe_version=recipe_version,
        chosen_label_source=(
            str(row["chosen_label_source"]) if row.get("chosen_label_source") else None
        ),
        rejected_label_source=(
            str(row["rejected_label_source"])
            if row.get("rejected_label_source")
            else None
        ),
        label_source=(str(row["label_source"]) if row.get("label_source") else None),
        eligibility_source=(
            str(row["eligibility_source"]) if row.get("eligibility_source") else None
        ),
    )


def _read_csv(path: Path) -> list[CalibrationRecord]:
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            return []
        missing = sorted(
            {
                "predicted_confidence",
                "actual_outcome",
                "recipe_id",
                "recipe_version",
            }
            - set(reader.fieldnames)
        )
        if missing:
            raise ValueError(f"CSV missing required column(s): {', '.join(missing)}")
        source_fields = set(reader.fieldnames)
        if not (
            {"label_source", "eligibility_source"} & source_fields
            or {"chosen_label_source", "rejected_label_source"} <= source_fields
        ):
            raise ValueError(
                "CSV requires label_source, eligibility_source, or chosen and "
                "rejected label-source metadata"
            )
        return [_parse_record(row) for row in reader]


def _read_jsonl(path: Path) -> list[CalibrationRecord]:
    records: list[CalibrationRecord] = []
    with path.open() as f:
        for line_number, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    records.append(_parse_record(value))
                else:
                    raise ValueError(
                        "JSONL rows must be objects with recipe and source metadata"
                    )
            except (KeyError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(f"{path}:{line_number}: {exc}") from exc
    return records


def _read_records(path: Path, file_format: str) -> list[CalibrationRecord]:
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
        return _read_csv(path)
    if file_format == "jsonl":
        return _read_jsonl(path)
    raise ValueError(f"unsupported input format: {file_format}")


def _resolved_format(path: Path, file_format: str) -> str:
    if file_format != "auto":
        return file_format
    if path.suffix.lower() in _CSV_SUFFIXES:
        return "csv"
    if path.suffix.lower() in _JSONL_SUFFIXES:
        return "jsonl"
    raise ValueError(
        "could not infer input format from suffix; pass --format csv or jsonl"
    )


def _read_policy_v3_pairs(path: Path, file_format: str) -> list[CalibrationPair] | None:
    """Read the optional inspection-export policy-v3 comparison column."""

    file_format = _resolved_format(path, file_format)
    if file_format == "csv":
        with path.open(newline="") as f:
            reader = csv.DictReader(f)
            if (
                reader.fieldnames is None
                or "policy_v3_confidence" not in reader.fieldnames
            ):
                return None
            return [
                _parse_pair(row["policy_v3_confidence"], row["actual_outcome"])
                for row in reader
            ]
    rows: list[tuple[int, object]] = []
    with path.open() as f:
        for line_number, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
                rows.append((line_number, value))
            except (KeyError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(f"{path}:{line_number}: {exc}") from exc
    found = any(
        isinstance(value, dict) and "policy_v3_confidence" in value for _, value in rows
    )
    if not found:
        return None
    pairs: list[CalibrationPair] = []
    for line_number, value in rows:
        if not isinstance(value, dict) or "policy_v3_confidence" not in value:
            raise ValueError(
                f"{path}:{line_number}: policy_v3_confidence must be present "
                "on every JSONL row"
            )
        try:
            pairs.append(
                _parse_pair(value["policy_v3_confidence"], value["actual_outcome"])
            )
        except (KeyError, ValueError) as exc:
            raise ValueError(f"{path}:{line_number}: {exc}") from exc
    return pairs


def _bucket_bounds(mean_predicted: float, n_bins: int) -> tuple[float, float]:
    index = n_bins - 1 if mean_predicted == 1.0 else int(mean_predicted * n_bins)
    return (index / n_bins, (index + 1) / n_bins)


def _print_table(pairs: list[CalibrationPair], n_bins: int) -> None:
    print(f"rows: {len(pairs)}")
    print(f"brier_score: {brier_score(pairs):.6f}")
    print(f"ece: {expected_calibration_error(pairs, n_bins=n_bins):.6f}")
    print(f"auroc: {auroc(pairs):.6f}")
    inversions = bucket_inversions(pairs, n_bins=n_bins)
    print(f"bucket_inversions: {len(inversions)}")
    print()
    if inversions:
        print(
            f"{'lower_bucket':>12} {'higher_bucket':>13} "
            f"{'lower_accuracy':>15} {'higher_accuracy':>16}"
        )
        for inversion in inversions:
            print(
                f"{inversion.lower_bucket:>12} {inversion.higher_bucket:>13} "
                f"{inversion.lower_empirical_accuracy:>15.6f} "
                f"{inversion.higher_empirical_accuracy:>16.6f}"
            )
        print()
    print(
        f"{'bucket':<15} {'mean_predicted':>15} {'empirical_accuracy':>20} {'count':>7}"
    )
    for mean_predicted, empirical_accuracy, count in reliability_diagram_data(
        pairs, n_bins=n_bins
    ):
        low, high = _bucket_bounds(mean_predicted, n_bins)
        right = "]" if high == 1.0 else ")"
        print(
            f"[{low:.3f}, {high:.3f}{right:<2} {mean_predicted:>15.6f} "
            f"{empirical_accuracy:>20.6f} {count:>7}"
        )


def _print_policy_comparison(
    pairs: list[CalibrationPair], policy_v3_pairs: list[CalibrationPair], n_bins: int
) -> None:
    comparison = compare_calibration_policies(pairs, policy_v3_pairs, n_bins=n_bins)
    print()
    print("policy comparison: v4 - v3")
    print(f"brier_score_delta: {comparison.brier_score_delta:.6f}")
    print(f"ece_delta: {comparison.expected_calibration_error_delta:.6f}")
    print(f"auroc_delta: {comparison.auroc_delta:.6f}")


def _record_key(record: CalibrationRecord) -> tuple[str, int, str, str, str, str]:
    return (
        record.recipe_id,
        record.recipe_version,
        record.chosen_label_source or "",
        record.rejected_label_source or "",
        record.label_source or "",
        record.eligibility_source or "",
    )


def _print_recipe_strata(records: list[CalibrationRecord], n_bins: int) -> None:
    strata = stratify_calibration(records, n_bins=n_bins)
    by_key: dict[tuple[str, int, str, str, str, str], list[CalibrationPair]] = {}
    for record in records:
        by_key.setdefault(_record_key(record), []).append(
            (record.predicted_confidence, record.actual_outcome)
        )
    for index, stratum in enumerate(strata):
        if index:
            print()
        if stratum.chosen_label_source is not None:
            source_name = (
                f"chosen_label_source={stratum.chosen_label_source} "
                f"rejected_label_source={stratum.rejected_label_source}"
            )
        elif stratum.label_source is not None:
            source_name = f"label_source={stratum.label_source}"
        else:
            source_name = f"eligibility_source={stratum.eligibility_source}"
        print(f"recipe: {stratum.recipe_id} v{stratum.recipe_version} {source_name}")
        key = (
            stratum.recipe_id,
            stratum.recipe_version,
            stratum.chosen_label_source or "",
            stratum.rejected_label_source or "",
            stratum.label_source or "",
            stratum.eligibility_source or "",
        )
        _print_table(by_key[key], n_bins)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="calibration_check", description=__doc__.splitlines()[0]
    )
    parser.add_argument("path", type=Path, help="CSV or JSONL labels file")
    parser.add_argument(
        "--format",
        choices=["auto", "csv", "jsonl"],
        default="auto",
        help="input format; default infers from .csv/.jsonl/.ndjson suffix",
    )
    parser.add_argument(
        "--bins",
        type=int,
        default=10,
        help="number of equal-width ECE buckets (default: 10)",
    )
    args = parser.parse_args(argv)

    try:
        records = _read_records(args.path, args.format)
        _print_recipe_strata(records, args.bins)
        policy_v3_pairs = _read_policy_v3_pairs(args.path, args.format)
        if policy_v3_pairs is not None:
            if len({_record_key(record) for record in records}) != 1:
                raise ValueError(
                    "policy_v3_confidence comparison requires one recipe/source stratum"
                )
            current_pairs = [
                (record.predicted_confidence, record.actual_outcome)
                for record in records
            ]
            _print_policy_comparison(current_pairs, policy_v3_pairs, args.bins)
    except (OSError, TypeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
