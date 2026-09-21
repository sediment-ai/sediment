# SPDX-License-Identifier: AGPL-3.0-or-later
"""Mirror garbage collection operator CLI.

Thin stdout wrapper over ``sediment_derive.gc.gc_mirrors`` — the reusable
retention logic lives there; this script only reads args, supplies the one
legitimate wall-clock read (``now``), and prints. Dry-run by default: pass
``--apply`` to actually delete anything, since a removed mirror is not
undone by anything short of a fresh clone.

    sediment mirror-gc --org acme-corp
    sediment mirror-gc --org acme-corp --retention-days 30 --apply
    sediment mirror-gc --org acme-corp --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import UTC, datetime

from sediment_core import normalize_org_id
from sediment_derive import MirrorManager
from sediment_derive.gc import MirrorGCPolicy, MirrorGCResult, gc_mirrors

from .database import add_database_url_argument, one_shot_fact_store

_DEFAULT_MIRROR_PATH = "./mirrors"


def _result_dict(
    result: MirrorGCResult, *, dry_run: bool, policy: MirrorGCPolicy
) -> dict[str, object]:
    row = asdict(result)
    row["action"] = result.action.value
    if result.last_push_captured_at is not None:
        row["last_push_captured_at"] = result.last_push_captured_at.isoformat()
    # A dry run and an applied run both report action="removed" for an
    # eligible mirror -- without these, a --json consumer reading that
    # value from a dry run misreads it as actually deleted.
    row["dry_run"] = dry_run
    row["retention_days"] = policy.retention_days
    row["policy_version"] = policy.policy_version
    return row


def _print_table(results: list[MirrorGCResult], *, dry_run: bool) -> None:
    if not results:
        print("no mirrored repos found")
        return
    mode = "dry-run (pass --apply to delete)" if dry_run else "applied"
    print(f"mode: {mode}")
    for result in results:
        last_push = (
            result.last_push_captured_at.isoformat()
            if result.last_push_captured_at is not None
            else "never"
        )
        detail = f" ({result.skip_reason})" if result.skip_reason else ""
        repository = result.repo if result.repo is not None else "name absent"
        if result.repository_identity is not None:
            identity = result.repository_identity
            repository += (
                f" [{identity.provider}:{identity.host}/{identity.repository_id}]"
            )
        print(f"{repository}\t{result.action.value}{detail}\tlast_push={last_push}")


def build_parser() -> argparse.ArgumentParser:
    """The argv contract, extracted so the generated CLI reference can
    walk it."""
    parser = argparse.ArgumentParser(
        prog="mirror_gc",
        description="Reclaim bare mirrors for repos with no recent Push activity.",
    )
    parser.add_argument(
        "--org",
        default=os.environ.get("SEDIMENT_ORG_ID"),
        required=not os.environ.get("SEDIMENT_ORG_ID"),
        help="org id (default: $SEDIMENT_ORG_ID)",
    )
    parser.add_argument(
        "--retention-days",
        type=int,
        default=MirrorGCPolicy().retention_days,
        help=f"days of no Push before a mirror is eligible for removal "
        f"(default: {MirrorGCPolicy().retention_days})",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually delete eligible mirrors (default: dry-run, print only)",
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

    # Reject at the trust boundary, before FactStore or MirrorManager touch
    # anything: a zero/negative retention flips the cutoff into the future
    # and (with --apply) removes every mirror that has push history.
    if args.retention_days < 1:
        print(
            f"error: --retention-days must be >= 1, got {args.retention_days}",
            file=sys.stderr,
        )
        return 2

    try:
        org_id = normalize_org_id(args.org)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    policy = MirrorGCPolicy(retention_days=args.retention_days)
    dry_run = not args.apply
    with one_shot_fact_store(
        args.database_url, operation="garbage-collect mirrors"
    ) as store:
        results = gc_mirrors(
            store,
            MirrorManager(args.mirror_path),
            org_id,
            policy,
            now=datetime.now(UTC),
            dry_run=dry_run,
        )

    if args.json:
        rows = [_result_dict(r, dry_run=dry_run, policy=policy) for r in results]
        print(json.dumps(rows, indent=2))
    else:
        _print_table(results, dry_run=dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
