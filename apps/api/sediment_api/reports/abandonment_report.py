# SPDX-License-Identifier: AGPL-3.0-or-later
"""Accepted Session status from captured Session-to-commit observations.

Missing observations remain unknown and count under session_commit_unobserved.
Legacy horizon and mirror options remain accepted for command compatibility.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict

from sediment_core import normalize_org_id
from sediment_derive import AbandonmentPolicy, MirrorManager, derive_abandonment

from ..database import add_database_url_argument, one_shot_fact_store

_DEFAULT_MIRROR_PATH = "./mirrors"


def _print_table(result, policy: AbandonmentPolicy) -> None:
    print(f"grace_horizon_days: {policy.grace_horizon_days}")
    print(f"as_of: {result.as_of.isoformat() if result.as_of else 'none'}")
    print(f"abandoned_sessions: {len(result.sessions)}")
    print(f"unshipped_accepts: {sum(s.accepted_decisions for s in result.sessions)}")
    print(
        f"observed_committed_sessions: {sum(item.status == 'committed' for item in result.outcomes)}"
    )
    print(
        f"unknown_sessions: {sum(item.status == 'attribution_unavailable' for item in result.outcomes)}"
    )
    print("skipped:")
    if not result.skipped:
        print("  none")
    else:
        for reason, count in sorted(result.skipped.items()):
            print(f"  {reason}: {count}")
    if result.sessions:
        print("sessions:")
        for session in result.sessions:
            print(
                f"  {session.session_id}  accepts={session.accepted_decisions}"
                f"  last_decision_at={session.last_decision_at.isoformat()}"
            )


def build_parser() -> argparse.ArgumentParser:
    """The argv contract for this report, extracted so the generated CLI
    reference can walk it; ``main`` is unchanged."""
    parser = argparse.ArgumentParser(
        prog="abandonment_report", description=__doc__.splitlines()[0]
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
    parser.add_argument(
        "--grace-horizon-days",
        type=int,
        default=AbandonmentPolicy().grace_horizon_days,
        help="legacy horizon for policy compatibility; missing observations remain unknown "
        f"(default: {AbandonmentPolicy().grace_horizon_days})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        org_id = normalize_org_id(args.org)
        policy = AbandonmentPolicy(grace_horizon_days=args.grace_horizon_days)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    with one_shot_fact_store(
        args.database_url, operation="generate abandonment report"
    ) as store:
        result = derive_abandonment(
            store, MirrorManager(args.mirror_path), org_id, policy
        )

    if args.json:
        print(
            json.dumps(
                {
                    "org_id": org_id,
                    "grace_horizon_days": policy.grace_horizon_days,
                    "policy_version": policy.policy_version,
                    "as_of": result.as_of.isoformat() if result.as_of else None,
                    "abandoned_sessions": [asdict(s) for s in result.sessions],
                    "accepted_session_outcomes": [
                        asdict(item) for item in result.outcomes
                    ],
                    "skipped": dict(sorted(result.skipped.items())),
                },
                indent=2,
                default=str,
            )
        )
    else:
        _print_table(result, policy)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
