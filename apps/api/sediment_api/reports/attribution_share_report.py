# SPDX-License-Identifier: AGPL-3.0-or-later
"""Attribution-share metric + decline-alert operator report.

Derives one org's per-repo notes-attribution share for the current
``--window-days`` window, derives the same metric again for the trailing
``--baseline-window-days`` window immediately preceding it, and reports any
``check_attribution_share_alerts`` verdict between them.

``--target-margin`` plans ``min_cases_for_decline_verdict`` via
``scripts/corpus_sizing.py``'s worst-case-p=0.5 planner
(``required_labelled_examples_for_proportion_margin``) -- the same derivation
``MIN_DRIFT_CASES`` uses for ``threshold_drift_report``, so the gate is a
computed corpus-size floor, never a hand-picked literal.

Like ``threshold_drift_report.py``, this script is deliberately
operator-facing only: every outcome -- an alert, or none -- exits 0. Only a
bad org id or an invalid ``--target-margin``/``--window-days`` exits 2. It is
not wired into any CI gate.

    sediment report attribution-share --org acme-corp \
        --target-margin 0.1
    sediment report attribution-share --org acme-corp \
        --target-margin 0.1 --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta

from sediment_core import FactStore, Push, normalize_org_id
from sediment_derive import (
    AttributionShareAlert,
    AttributionSharePolicy,
    MirrorManager,
    RepoAttributionShare,
    check_attribution_share_alerts,
    derive_attribution_share,
    read_repository_context,
    RepositoryContext,
    RepositoryIdentity,
    IdentifiedRepositoryKey,
    LegacyRepositoryKey,
)
from sediment_derive.precision_harness import (
    required_labelled_examples_for_proportion_margin,
)

from ..database import add_database_url_argument, one_shot_fact_store

_DEFAULT_MIRROR_PATH = "./mirrors"
_WORST_CASE_ASSUMED_RATE = 0.5


def _baseline_rows(
    store: FactStore,
    mirrors: MirrorManager,
    org_id: str,
    policy: AttributionSharePolicy,
    current: list[RepoAttributionShare],
    *,
    repository_context: RepositoryContext | None = None,
) -> list[RepoAttributionShare]:
    """The trailing baseline window per repo in ``current``: the
    ``baseline_window_days`` immediately preceding that repo's current
    ``window_start``. Bounds are enforced here (not left to the derive
    function's latest-push anchor) so a cadence gap before the current
    window cannot re-anchor the baseline at a stale pre-current push --
    matching the contiguous-bounds discipline the model-report path
    (``derive_model_report_attribution_share``) already follows. The
    ``_window`` reuse lives in ``derive_attribution_share`` itself; this is
    only the bounded-selection + bounds-forcing wiring the interim CLI owns.

    Both bounds stay data-driven (ADR 0001 -- never wall-clock):
    ``current.window_start`` is ``latest_push.captured_at - window_days``
    (derived from push facts by ``derive_attribution_share``), and the
    baseline lower bound is arithmetic on that value, never ``now()``."""
    if repository_context is None:
        with store.read_snapshot() as snapshot:
            context = read_repository_context(snapshot, org_id, as_of=datetime.now(UTC))
            return _baseline_rows(
                snapshot, mirrors, org_id, policy, current, repository_context=context
            )

    def row_key(row: RepoAttributionShare):
        return (
            IdentifiedRepositoryKey(row.org_id, row.repository_identity)
            if row.repository_identity is not None
            else LegacyRepositoryKey(row.org_id, row.repo)
        )

    current_window_start = {row_key(row): row.window_start for row in current}
    if not current_window_start:
        return []
    baseline_start = {
        repo: ws - timedelta(days=policy.baseline_window_days)
        for repo, ws in current_window_start.items()
    }
    older_pushes: list[Push] = [
        push
        for push in store.read_pushes(org_id, captured_through=repository_context.as_of)
        if (key := repository_context.resolve_fact(push).key) in current_window_start
        and baseline_start[key] <= push.captured_at < current_window_start[key]
    ]
    if not older_pushes:
        return []
    baseline_policy = replace(policy, window_days=policy.baseline_window_days)
    derived = derive_attribution_share(
        store,
        mirrors,
        org_id,
        baseline_policy,
        pushes=older_pushes,
        repository_context=repository_context,
        as_of=repository_context.as_of,
    )
    return [
        replace(
            row,
            window_start=baseline_start[row_key(row)],
            window_end=current_window_start[row_key(row)],
        )
        for row in derived
    ]


def attribution_share_row_payload(row: RepoAttributionShare) -> dict[str, object]:
    payload = asdict(row)
    payload["window_start"] = row.window_start.isoformat()
    payload["window_end"] = row.window_end.isoformat()
    return payload


def attribution_share_alert_payload(
    alert: AttributionShareAlert,
) -> dict[str, object]:
    return {
        "org_id": alert.org_id,
        "repo": alert.repo,
        "repository_identity": asdict(alert.repository_identity)
        if alert.repository_identity
        else None,
        "kind": alert.kind.value,
        "current": attribution_share_row_payload(alert.current),
        "baseline": (
            attribution_share_row_payload(alert.baseline)
            if alert.baseline is not None
            else None
        ),
        "reason": alert.reason,
    }


def format_repository_label(repo: str, identity: RepositoryIdentity | None) -> str:
    """Show the repository lifetime beside its representative display label."""
    return (
        repo
        if identity is None
        else f"{repo} [{identity.provider}/{identity.host}/{identity.repository_id}]"
    )


def _print_row(row: RepoAttributionShare) -> None:
    label = format_repository_label(row.repo, row.repository_identity)
    print(
        f"  {label}: git_notes_share={row.git_notes_share:.4f} "
        f"ci=({row.git_notes_share_ci[0]:.4f}, {row.git_notes_share_ci[1]:.4f}) "
        f"agent_plausible_commits={row.agent_plausible_commits} "
        f"git_notes={row.git_notes_attributed} jaccard={row.jaccard_attributed} "
        f"unattributed={row.unattributed}"
    )


def build_parser() -> argparse.ArgumentParser:
    """The argv contract for this report, extracted so the generated
    CLI reference can walk it."""
    parser = argparse.ArgumentParser(
        prog="attribution_share_report",
        description=(
            "Report notes-attribution share per repo and flag a material "
            "decline against a trailing baseline window."
        ),
    )
    parser.add_argument(
        "--org",
        default=os.environ.get("SEDIMENT_ORG_ID"),
        required=not os.environ.get("SEDIMENT_ORG_ID"),
        help="org id (default: $SEDIMENT_ORG_ID)",
    )
    parser.add_argument(
        "--target-margin",
        type=float,
        required=True,
        help=(
            "target CI half-width in raw proportion points for the decline "
            "verdict's min-cases gate, e.g. 0.1 for +/-10pp (worst-case "
            "p=0.5 planner -- see scripts/corpus_sizing.py)"
        ),
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=7,
        help="current window width in days (default: 7)",
    )
    parser.add_argument(
        "--baseline-window-days",
        type=int,
        default=28,
        help="trailing baseline window width in days (default: 28)",
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

    try:
        org_id = normalize_org_id(args.org)
        min_cases = required_labelled_examples_for_proportion_margin(
            _WORST_CASE_ASSUMED_RATE, args.target_margin
        )
        policy = AttributionSharePolicy(
            window_days=args.window_days,
            baseline_window_days=args.baseline_window_days,
            min_cases_for_decline_verdict=min_cases,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    with one_shot_fact_store(
        args.database_url, operation="generate attribution-share report"
    ) as store:
        mirrors = MirrorManager(args.mirror_path)
        with store.read_snapshot() as snapshot:
            context = read_repository_context(snapshot, org_id, as_of=datetime.now(UTC))
            current = derive_attribution_share(
                snapshot,
                mirrors,
                org_id,
                policy,
                repository_context=context,
                as_of=context.as_of,
            )
            baseline = _baseline_rows(
                snapshot, mirrors, org_id, policy, current, repository_context=context
            )
            alerts = check_attribution_share_alerts(current, baseline, policy)

    if args.json:
        print(
            json.dumps(
                {
                    "min_cases_for_decline_verdict": min_cases,
                    "current": [attribution_share_row_payload(row) for row in current],
                    "baseline": [
                        attribution_share_row_payload(row) for row in baseline
                    ],
                    "alerts": [
                        attribution_share_alert_payload(alert) for alert in alerts
                    ],
                },
                indent=2,
            )
        )
    else:
        print(f"min_cases_for_decline_verdict: {min_cases}")
        print("current:")
        for row in current:
            _print_row(row)
        print("baseline:")
        for row in baseline:
            _print_row(row)
        print("alerts:")
        for alert in alerts:
            print(
                f"  {format_repository_label(alert.repo, alert.repository_identity)}: {alert.kind.value} -- {alert.reason}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
