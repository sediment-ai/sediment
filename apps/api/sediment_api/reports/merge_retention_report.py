# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pull request merge-retention diagnostic report.

sediment report merge-retention --org acme-corp
sediment report merge-retention --org acme-corp --json
sediment report merge-retention --org acme-corp --rows-out review.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict

from sediment_core import normalize_org_id
from sediment_derive import MirrorManager
from sediment_export import (
    MergeRetentionReport,
    RetentionScoreSummary,
    generate_merge_retention_report_result,
    merge_retention_to_export_rows,
    write_jsonl,
)

from ..database import add_database_url_argument, one_shot_fact_store

_DEFAULT_MIRROR_PATH = "./mirrors"


def _format_counts(counts: dict[str, int]) -> str:
    return (
        ", ".join(f"{key}={value}" for key, value in sorted(counts.items())) or "none"
    )


def _print_score(name: str, summary: RetentionScoreSummary) -> None:
    distribution = summary.distribution
    assert distribution.mean is not None
    assert distribution.median is not None
    assert distribution.p10 is not None
    assert distribution.p90 is not None
    print(
        f"{name}: n={distribution.count} mean={distribution.mean:.3f} "
        f"median={distribution.median:.3f} p10={distribution.p10:.3f} "
        f"p90={distribution.p90:.3f}"
    )
    for threshold in summary.thresholds:
        rate = "no data" if threshold.rate is None else f"{threshold.rate:.3f}"
        print(
            f"  below {threshold.threshold:.1f}: {threshold.below}/"
            f"{threshold.total} ({rate})"
        )


def _print_table(report: MergeRetentionReport) -> None:
    print(f"attributed file candidates: {report.attributed_file_candidates}")
    print(f"candidates joined to merge: {report.candidates_joined_to_merge}")
    print(f"scored rows: {report.scored_rows}")
    print(f"pull requests with joined candidates: {report.joined_pull_requests}")
    print(f"pull requests with scored rows: {report.scored_pull_requests}")
    print(f"membership: {_format_counts(report.membership)}")
    print(f"scoring skips: {_format_counts(report.scoring_skips)}")
    for unit, counts in sorted(report.repository_skipped.items()):
        print(f"repository skips ({unit}): {_format_counts(counts)}")
    print(f"attribution skips: {_format_counts(report.attribution_skips)}")
    print(
        f"decision attachment skips: {_format_counts(report.decision_attachment_skips)}"
    )
    if report.scored_rows == 0:
        print("scores: no data")
    else:
        _print_score("final head", report.head)
        _print_score("merged commit", report.merge)
    print(f"explicit accepted rows: {report.explicit_accept_rows}")
    for name, thresholds in (
        ("explicit final head", report.explicit_accept_head_thresholds),
        ("explicit merged commit", report.explicit_accept_merge_thresholds),
    ):
        for threshold in thresholds:
            rate = "no data" if threshold.rate is None else f"{threshold.rate:.3f}"
            print(
                f"{name} below {threshold.threshold:.1f}: {threshold.below}/"
                f"{threshold.total} ({rate})"
            )
    print(f"attribution provenance: {report.attribution_provenance}")
    print(f"merge retention provenance: {report.merge_retention_provenance}")


def build_parser() -> argparse.ArgumentParser:
    """Return the argv contract consumed by the generated CLI reference."""
    parser = argparse.ArgumentParser(
        prog="merge_retention_report",
        description="Measure attributed file changes at pull request merge boundaries.",
    )
    parser.add_argument(
        "--org",
        default=os.environ.get("SEDIMENT_ORG_ID"),
        required=not os.environ.get("SEDIMENT_ORG_ID"),
        help="org id (default: $SEDIMENT_ORG_ID)",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit JSON instead of a text report"
    )
    parser.add_argument(
        "--rows-out",
        metavar="FILE",
        help="write canonical merge-retention rows to FILE as JSONL",
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
    try:
        org_id = normalize_org_id(args.org)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    with one_shot_fact_store(
        args.database_url, operation="generate merge-retention report"
    ) as store:
        result = generate_merge_retention_report_result(
            store,
            MirrorManager(args.mirror_path),
            org_id,
        )
    report = result.report
    if args.json:
        print(json.dumps(asdict(report), indent=2))
    else:
        _print_table(report)
    if args.rows_out:
        try:
            write_result = write_jsonl(
                merge_retention_to_export_rows(result.rows),
                args.rows_out,
                split_enabled=False,
            )
        except (OSError, TypeError, ValueError) as exc:
            print(f"error: couldn't write merge-retention rows: {exc}", file=sys.stderr)
            return 1
        if write_result.written:
            print(
                f"wrote {len(result.rows)} merge-retention rows to {args.rows_out}",
                file=sys.stderr,
            )
        else:
            print(
                f"wrote 0 merge-retention rows; left {args.rows_out} unchanged",
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
