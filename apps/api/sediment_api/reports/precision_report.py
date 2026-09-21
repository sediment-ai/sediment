# SPDX-License-Identifier: AGPL-3.0-or-later
"""Attribution ground-truth precision report.

Loads a JSONL ground-truth manifest, derives one org's attributions, and
prints notes-attributed and jaccard-attributed precision/recall separately.
The reusable schema and scoring logic live in
``sediment_derive.precision_report`` beside the scorer precision harness; this
script is only the operator-facing stdout wrapper.

    sediment report precision --org acme-corp \
        --manifest packages/derive/tests/fixtures/ground_truth/illustrative_manifest.jsonl
    sediment report precision --org acme-corp --manifest gt.jsonl --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict

from sediment_core import normalize_org_id
from sediment_derive import MirrorManager
from sediment_derive.precision_report import (
    DEFAULT_SWEEP_THRESHOLDS,
    DEFAULT_THRESHOLD,
    AttributionPrecisionReport,
    generate_precision_report_for_org,
    load_ground_truth_manifest,
)

from ..database import add_database_url_argument, one_shot_fact_store

_DEFAULT_MIRROR_PATH = "./mirrors"


def _parse_thresholds(raw: str) -> list[float]:
    try:
        thresholds = [float(part.strip()) for part in raw.split(",") if part.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "thresholds must be comma-separated numbers"
        ) from exc
    if not thresholds:
        raise argparse.ArgumentTypeError("at least one threshold is required")
    for threshold in thresholds:
        if threshold < 0.0 or threshold > 1.0:
            raise argparse.ArgumentTypeError("thresholds must be between 0.0 and 1.0")
    return thresholds


def _result_dict(report: AttributionPrecisionReport) -> dict[str, object]:
    return {
        "threshold": report.threshold,
        "by_source": {
            source.value: _source_dict(source_report)
            for source, source_report in report.by_source.items()
        },
    }


def _source_dict(report: AttributionPrecisionReport) -> dict[str, object]:
    return {
        "default": asdict(report.default),
        "sweep": [asdict(row) for row in report.sweep],
        "skipped_unlabelled_predictions": report.skipped_unlabelled_predictions,
        "skipped_repository_ground_truth": report.skipped_repository_ground_truth,
        "skipped_repository_predictions": report.skipped_repository_predictions,
    }


def _print_table(report: AttributionPrecisionReport) -> None:
    print(f"default_threshold: {report.threshold:.2f}")
    for source, source_report in report.by_source.items():
        row = source_report.default
        print(f"{source.value}:")
        print(
            "  "
            f"precision={row.precision:.4f} recall={row.recall:.4f} "
            f"tp={row.true_positives} fp={row.false_positives} "
            f"fn={row.false_negatives} tn={row.true_negatives}"
        )
        print(
            "  "
            f"skipped_unlabelled_predictions="
            f"{source_report.skipped_unlabelled_predictions}"
        )
        print(
            f"  skipped_repository_ground_truth={source_report.skipped_repository_ground_truth} "
            f"skipped_repository_predictions={source_report.skipped_repository_predictions}"
        )
        print("  sweep:")
        for sweep_row in source_report.sweep:
            print(
                "    "
                f"threshold={sweep_row.threshold:.2f} "
                f"precision={sweep_row.precision:.4f} "
                f"recall={sweep_row.recall:.4f} "
                f"tp={sweep_row.true_positives} "
                f"fp={sweep_row.false_positives} "
                f"fn={sweep_row.false_negatives} "
                f"tn={sweep_row.true_negatives}"
            )


def build_parser() -> argparse.ArgumentParser:
    """The argv contract for this report, extracted so the generated
    CLI reference can walk it."""
    parser = argparse.ArgumentParser(
        prog="precision_report",
        description="Score derived attributions against a ground-truth manifest.",
    )
    parser.add_argument(
        "--org",
        default=os.environ.get("SEDIMENT_ORG_ID"),
        required=not os.environ.get("SEDIMENT_ORG_ID"),
        help="org id (default: $SEDIMENT_ORG_ID)",
    )
    parser.add_argument("--manifest", required=True, help="JSONL ground-truth manifest")
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help=f"default scoring threshold (default: {DEFAULT_THRESHOLD})",
    )
    parser.add_argument(
        "--sweep-thresholds",
        type=_parse_thresholds,
        default=list(DEFAULT_SWEEP_THRESHOLDS),
        help="comma-separated sweep thresholds (default: 0.1,0.2,...,0.9)",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit JSON instead of a text report"
    )
    add_database_url_argument(parser)
    parser.add_argument(
        "--mirror-path",
        default=os.environ.get("SEDIMENT_MIRROR_PATH", _DEFAULT_MIRROR_PATH),
        help="base dir for git mirrors (default: $SEDIMENT_MIRROR_PATH or "
        f"{_DEFAULT_MIRROR_PATH!r})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not 0.0 <= args.threshold <= 1.0:
        print(
            "error: --threshold must be between 0.0 and 1.0",
            file=sys.stderr,
        )
        return 2
    try:
        org_id = normalize_org_id(args.org)
        manifest = load_ground_truth_manifest(args.manifest)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    with one_shot_fact_store(
        args.database_url, operation="generate precision report"
    ) as store:
        report = generate_precision_report_for_org(
            store,
            MirrorManager(args.mirror_path),
            org_id,
            manifest,
            threshold=args.threshold,
            sweep_threshold_values=args.sweep_thresholds,
        )

    if args.json:
        print(json.dumps(_result_dict(report), indent=2))
    else:
        _print_table(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
