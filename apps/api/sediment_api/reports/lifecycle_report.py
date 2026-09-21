# SPDX-License-Identifier: AGPL-3.0-or-later
"""JSON-only accepted-work lifecycle report."""

from __future__ import annotations

import argparse
import os
import sys

from sediment_core import normalize_org_id
from sediment_derive import MirrorManager

from ..database import add_database_url_argument, one_shot_fact_store
from ..services.operational_reports import (
    LifecycleReportRequest,
    generate_lifecycle_report,
)

_DEFAULT_MIRROR_PATH = "./mirrors"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lifecycle_report",
        description="Report accepted-work progression and operational evidence.",
    )
    parser.add_argument(
        "--org",
        default=os.environ.get("SEDIMENT_ORG_ID"),
        required=not os.environ.get("SEDIMENT_ORG_ID"),
        help="org id (default: $SEDIMENT_ORG_ID)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        required=True,
        help="emit the canonical JSON artifact (required)",
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
    try:
        with one_shot_fact_store(
            args.database_url, operation="generate accepted-work lifecycle report"
        ) as store:
            envelope = generate_lifecycle_report(
                store,
                MirrorManager(args.mirror_path),
                LifecycleReportRequest(org_id),
            )
    except Exception as exc:
        print(f"error: couldn't generate lifecycle report: {exc}", file=sys.stderr)
        return 1
    print(envelope.to_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
