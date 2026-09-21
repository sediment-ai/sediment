# SPDX-License-Identifier: AGPL-3.0-or-later
"""Recovery-pair yield report: failed CI runs, emitted pairs, skips.

Prints ``sediment_export.generate_recovery_yield_report`` for one org: a
compact stdout summary by default, or ``--json`` for machines.

    sediment report recovery-yield --org acme-corp
    sediment report recovery-yield --org acme-corp --json

Storage is read the same way ``model_report.py`` reads it:
``--database-url``/``SEDIMENT_DATABASE_URL`` and ``--mirror-path``/
``SEDIMENT_MIRROR_PATH``. A missing mirror path is not fatal; unmirrored repos
are counted in the recovery derivation's ``mirror_absent`` skip tally.
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
    DiffSizeStats,
    RecoveryYieldReport,
    generate_recovery_yield_report,
)

from ..database import add_database_url_argument, one_shot_fact_store

_DEFAULT_MIRROR_PATH = "./mirrors"


def _print_table(row: RecoveryYieldReport) -> None:
    print(f"failed_ci_runs: {row.total_failed_ci_runs}")
    print(f"recovery_pairs: {row.recovery_pairs}")
    print(f"recovery_yield: {row.recovery_yield_rate:.1%}")
    print("skipped:")
    if not row.skipped:
        print("  none")
    else:
        for reason, count in sorted(row.skipped.items()):
            print(f"  {reason}: {count}")
    distribution = row.diff_size_distribution
    print("diff_size_distribution:")
    print(f"  max_recovery_diff_lines: {distribution.max_recovery_diff_lines}")
    print("  kept:")
    _print_diff_size_stats(distribution.kept)
    print("  dropped:")
    _print_diff_size_stats(distribution.dropped)


def _print_diff_size_stats(stats: DiffSizeStats) -> None:
    print(f"    count: {stats.count}")
    print(f"    min: {stats.min}")
    print(f"    median: {stats.median}")
    print(f"    p90: {stats.p90}")
    print(f"    max: {stats.max}")
    print("    histogram:")
    if not stats.histogram:
        print("      none")
        return
    for bucket, count in stats.histogram.items():
        print(f"      {bucket}: {count}")


def _row_dict(row: RecoveryYieldReport) -> dict:
    return {
        "total_failed_ci_runs": row.total_failed_ci_runs,
        "recovery_pairs": row.recovery_pairs,
        "recovery_yield_rate": row.recovery_yield_rate,
        "skipped": dict(row.skipped),
        "diff_size_distribution": asdict(row.diff_size_distribution),
    }


def build_parser() -> argparse.ArgumentParser:
    """The argv contract for this report, extracted so the generated
    CLI reference can walk it."""
    parser = argparse.ArgumentParser(
        prog="recovery_yield_report", description=__doc__.splitlines()[0]
    )
    parser.add_argument(
        "--org",
        default=os.environ.get("SEDIMENT_ORG_ID"),
        required=not os.environ.get("SEDIMENT_ORG_ID"),
        help="org id (default: $SEDIMENT_ORG_ID)",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit a JSON row instead of a table"
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
        args.database_url, operation="generate recovery-yield report"
    ) as store:
        row = generate_recovery_yield_report(
            store,
            MirrorManager(args.mirror_path),
            org_id,
        )

    if args.json:
        print(json.dumps(_row_dict(row), indent=2))
    else:
        print(f"org: {org_id}")
        _print_table(row)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
