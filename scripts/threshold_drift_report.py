# SPDX-License-Identifier: AGPL-3.0-or-later
"""Precision-harness report for labelled attribution cases.

Loads the labelled-case JSON shape consumed by
``sediment_derive.precision_harness.load_labelled_cases`` and reports the
current Jaccard baseline at a threshold. With ``--check-drift``, it also
compares the historical threshold against the current F1-optimal threshold
selected by the existing threshold sweep.

This script is deliberately operator-facing only: every status
(``ok``/``insufficient_data``, materiality true or false) exits 0. Only a
load/parse error exits 2. It is not wired into any CI gate, and
``material: true`` is a signal to look, not a build failure — the labelled
corpus this repo bundles is a small synthetic fixture set, nowhere near the
~97-case floor a ten-point-margin Wilson interval needs to be meaningful (see ``MIN_DRIFT_CASES``), so a real drift *gate* would need a
real labelled corpus first.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sediment_derive import (
    MIN_DRIFT_CASES,
    JaccardScorer,
    ThresholdDriftReport,
    evaluate_scorer,
    load_labelled_cases,
    threshold_drift_report,
)


def _result_dict(result) -> dict:
    return {
        "scorer_version": result.scorer_version,
        "threshold": result.threshold,
        "precision": result.precision,
        "recall": result.recall,
        "f1": result.f1,
        "true_positives": result.true_positives,
        "false_positives": result.false_positives,
        "false_negatives": result.false_negatives,
        "true_negatives": result.true_negatives,
    }


def _drift_dict(report: ThresholdDriftReport) -> dict:
    return {
        "scorer_version": report.scorer_version,
        "case_count": report.case_count,
        "positive_count": report.positive_count,
        "negative_count": report.negative_count,
        "historical_threshold": report.historical_threshold,
        "optimal_threshold": report.optimal_threshold,
        "historical_result": (
            _result_dict(report.historical_result)
            if report.historical_result is not None
            else None
        ),
        "optimal_result": (
            _result_dict(report.optimal_result)
            if report.optimal_result is not None
            else None
        ),
        "threshold_delta": report.threshold_delta,
        "f1_delta": report.f1_delta,
        "precision_outside_ci": report.precision_outside_ci,
        "recall_outside_ci": report.recall_outside_ci,
        "material": report.material,
        "status": report.status,
        "reason": report.reason,
    }


def _print_drift(report: ThresholdDriftReport) -> None:
    print(f"scorer: {report.scorer_version}")
    print(f"cases: {report.case_count}")
    print(f"labels: positive={report.positive_count} negative={report.negative_count}")
    print(f"status: {report.status}")
    if report.reason is not None:
        print(f"reason: {report.reason}")
        return
    print(f"historical_threshold: {report.historical_threshold:.2f}")
    print(f"optimal_threshold: {report.optimal_threshold:.2f}")
    print(f"threshold_delta: {report.threshold_delta:.2f}")
    print(f"historical_f1: {report.historical_result.f1:.4f}")
    print(f"optimal_f1: {report.optimal_result.f1:.4f}")
    print(f"f1_delta: {report.f1_delta:.4f}")
    print(f"precision_outside_ci: {str(report.precision_outside_ci).lower()}")
    print(f"recall_outside_ci: {str(report.recall_outside_ci).lower()}")
    print(f"material: {str(report.material).lower()}")


def _print_baseline(result) -> None:
    print(f"scorer: {result.scorer_version}")
    print(f"threshold: {result.threshold:.2f}")
    print(f"precision: {result.precision:.4f}")
    print(f"recall: {result.recall:.4f}")
    print(f"f1: {result.f1:.4f}")
    print(
        "confusion: "
        f"tp={result.true_positives} "
        f"fp={result.false_positives} "
        f"fn={result.false_negatives} "
        f"tn={result.true_negatives}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="threshold_drift_report",
        description="Evaluate labelled attribution cases with the precision harness.",
    )
    parser.add_argument(
        "manifest",
        type=Path,
        help="path to labelled ground-truth JSON manifest",
    )
    parser.add_argument(
        "--historical-threshold",
        type=float,
        default=0.7,
        help="historical attribution threshold (default: 0.7)",
    )
    parser.add_argument(
        "--check-drift",
        action="store_true",
        help="compare the historical threshold with the current F1 optimum",
    )
    parser.add_argument(
        "--min-cases",
        type=int,
        default=MIN_DRIFT_CASES,
        help="minimum labelled cases before a drift verdict is trusted "
        f"(default: {MIN_DRIFT_CASES}, derived from the corpus-sizing "
        "planner for a ten-point Wilson-interval margin at worst-case "
        "p=0.5 -- lower only for a deliberately small/test corpus)",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit JSON instead of a text report"
    )
    args = parser.parse_args(argv)

    try:
        cases = load_labelled_cases(args.manifest)
        scorer = JaccardScorer()
        if args.check_drift:
            drift = threshold_drift_report(
                args.historical_threshold,
                cases,
                scorer=scorer,
                min_cases=args.min_cases,
            )
            if args.json:
                print(json.dumps(_drift_dict(drift), indent=2))
            else:
                _print_drift(drift)
        else:
            result = evaluate_scorer(
                scorer,
                cases,
                threshold=args.historical_threshold,
            )
            if args.json:
                print(json.dumps(_result_dict(result), indent=2))
            else:
                _print_baseline(result)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
