# SPDX-License-Identifier: AGPL-3.0-or-later
"""Compute report: Session completion time by final Compute allocation.

sediment report compute --org acme-corp
sediment report compute --org acme-corp --json
sediment report compute --org acme-corp --rows-out sessions.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from sediment_core import normalize_org_id
from sediment_derive import ComputeArm
from sediment_export import (
    ComputeReport,
    DurationDistribution,
    compute_report_to_json,
    compute_timings_to_export_rows,
    generate_compute_report_result,
    write_jsonl,
)
from sediment_export.significance import MedianDifference

from ..database import add_database_url_argument, one_shot_fact_store


def _arm(arm: ComputeArm) -> str:
    cpu = "unknown" if arm.cpu_millicores is None else f"{arm.cpu_millicores}m"
    memory = (
        "unknown" if arm.memory_bytes is None else f"{arm.memory_bytes / 2**30:.2f}GiB"
    )
    return f"{arm.allocation_source.value} cpu={cpu} memory={memory}"


def _seconds(milliseconds: float | None) -> str:
    return "n/a" if milliseconds is None else f"{milliseconds / 1000:.1f}s"


def _distribution(name: str, distribution: DurationDistribution) -> str:
    return (
        f"{name} median={_seconds(distribution.median)} "
        f"p25={_seconds(distribution.p25)} p75={_seconds(distribution.p75)}"
    )


def _difference(name: str, difference: MedianDifference) -> str:
    sizes = f"n={difference.n_a}/{difference.n_b}"
    if difference.insufficient_data:
        return f"    {name}: insufficient data ({sizes})"
    flag = " small-n" if difference.small_n else ""
    level = round(difference.confidence * 100)
    return (
        f"    {name}: {_seconds(difference.difference)} "
        f"[{level}% CI {_seconds(difference.ci_low)}, "
        f"{_seconds(difference.ci_high)}] {sizes}{flag}"
    )


def _print_table(report: ComputeReport) -> None:
    print(f"Sessions with a Compute allocation: {report.sessions_with_allocation}")
    print(f"timed Sessions: {report.timed_sessions}")
    skipped = ", ".join(f"{key}={value}" for key, value in report.skipped.items())
    print(f"skipped Sessions: {skipped}")
    print(
        "randomized Sessions reassigned to another source: "
        f"{report.randomized_reassigned_sessions} "
        "(excluded from randomized comparisons)"
    )
    if not report.arms:
        print("arms: no data")
    for summary in report.arms:
        print(
            f"{_arm(summary.arm)}: n={summary.sessions} "
            f"(allocation changed in {summary.allocation_changed_sessions})"
        )
        for name in ("elapsed_ms", "model_ms", "between_calls_ms"):
            print(f"  {_distribution(name, getattr(summary, name))}")
    if report.comparisons:
        print("comparisons (arm B minus arm A; negative means arm B finished faster):")
    for comparison in report.comparisons:
        reading = "randomized" if comparison.randomized else "observational"
        print(f"  {_arm(comparison.arm_a)} -> {_arm(comparison.arm_b)} ({reading})")
        print(_difference("elapsed_ms", comparison.elapsed_ms))
        print(_difference("between_calls_ms", comparison.between_calls_ms))
    print(f"provenance: {report.provenance}")


def build_parser() -> argparse.ArgumentParser:
    """Return the argv contract consumed by the generated CLI reference."""
    parser = argparse.ArgumentParser(
        prog="compute_report",
        description=(
            "Compare Session completion time across final Compute allocations."
        ),
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
        help="write one Session compute timing per line to FILE as JSONL",
    )
    add_database_url_argument(parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        org_id = normalize_org_id(args.org)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    with one_shot_fact_store(
        args.database_url, operation="generate compute report"
    ) as store:
        result = generate_compute_report_result(store, org_id)
    if args.json:
        print(json.dumps(compute_report_to_json(result.report), indent=2))
    else:
        _print_table(result.report)
    if args.rows_out:
        try:
            write_result = write_jsonl(
                compute_timings_to_export_rows(result.timings),
                args.rows_out,
                split_enabled=False,
            )
        except (OSError, TypeError, ValueError) as exc:
            print(f"error: couldn't write compute timing rows: {exc}", file=sys.stderr)
            return 1
        if write_result.written:
            print(
                f"wrote {len(result.timings)} compute timing rows to {args.rows_out}",
                file=sys.stderr,
            )
        else:
            print(
                f"wrote 0 compute timing rows; left {args.rows_out} unchanged",
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
