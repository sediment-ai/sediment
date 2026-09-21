# SPDX-License-Identifier: AGPL-3.0-or-later
"""Label-confidence inspection — a stratified sample of attributed_completions with
``resolve_confidence``'s factor breakdown, so an operator can eyeball what
the label-confidence policy actually produced without writing Python. NVIDIA's
agentic-RL guidance treats "run the reward function against 50-100 outputs
and manually inspect the scores" as mandatory pre-training hygiene; this is
that hygiene, wired to ``sediment_export.generate_label_confidence_inspection``.

Prints an aligned table on stdout by default, or ``--json`` for machines.
An empty result (no data for the org) is a valid answer — it prints
"no data" and exits 0, not an error, same convention as
``model_report.py``.

    sediment report label-confidence-inspection --org acme-corp
    sediment report label-confidence-inspection --org acme-corp --n 100
    sediment report label-confidence-inspection --org acme-corp --latency-buckets 4
    sediment report label-confidence-inspection --org acme-corp --json
    sediment report label-confidence-inspection --org acme-corp --sensitivity
    sediment report label-confidence-inspection --org acme-corp --sensitivity \\
      --knob implicit_accept_multiplier --values 1.0,1.1,1.5

Every row carries a ``human_judgment`` field that is always empty/null — a
column for a human to fill in later against real data. This tool never
generates a judgment and labels it as if it were the human's (see
``sediment_export.label_confidence_inspection``'s module docstring).

Storage is read the same way ``model_report.py`` reads it —
``--database-url``/``SEDIMENT_DATABASE_URL`` and ``--mirror-path``/
``SEDIMENT_MIRROR_PATH`` — and, like that script, this one does NOT import
``sediment_api.config.settings`` (which requires ``SEDIMENT_ORG_ID`` and,
outside dev mode, real auth secrets just to construct): report any org a
deployment holds via ``--org``, without touching API auth config.
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
    DEFAULT_SWEEP_GRIDS,
    LABEL_CONFIDENCE_POLICY_KNOBS,
    DecisionLatencyReport,
    InspectionRow,
    LabelConfidenceSensitivityRow,
    SFTPolicy,
    generate_decision_latency_report,
    generate_label_confidence_inspection,
    generate_label_confidence_sensitivity,
)

from ..database import add_database_url_argument, one_shot_fact_store

_DEFAULT_MIRROR_PATH = "./mirrors"
_DEFAULT_N = 50
_DEFAULT_MIN_CONFIDENCE = 0.6


def _decisions_str(row: InspectionRow) -> str:
    if not row.decisions:
        return "-"
    return "; ".join(
        f"{d.agent_harness}/{'explicit' if d.explicit else 'implicit'}/"
        f"{'accept' if d.accepted else 'reject'}"
        for d in row.decisions
    )


def _ci_str(row: InspectionRow) -> str:
    if not row.ci_outcomes:
        return "-"
    return "; ".join(o.result for o in row.ci_outcomes)


def _confidence_str(row: InspectionRow) -> str:
    if row.confidence is None:
        return "None"
    b = row.confidence
    return (
        f"{b.final:.3f} "
        f"(decision={b.decision_factor:.3f} x ci={b.ci_factor:.3f} "
        f"x reliability={b.ci_reliability:.3f} "
        f"x sim={b.similarity_discount:.3f})"
    )


def _print_table(rows: list[InspectionRow]) -> None:
    if not rows:
        print("no data")
        return
    for row in rows:
        print(
            f"--- recipe {row.recipe_id} v{row.recipe_version} "
            f"eligibility_source={row.eligibility_source or 'ineligible'}"
        )
        if row.attribution_source == "abandonment":
            print(f"    abandonment {row.inference_call_id}")
        else:
            assert row.repo is not None
            assert row.commit_sha is not None
            assert row.file_path is not None
            print(f"--- {row.repo}@{row.commit_sha[:12]} {row.file_path}")
        print(f"    inference_call_id:   {row.inference_call_id}")
        print(f"    snippet:         {row.completion_snippet!r}")
        print(f"    decisions:       {_decisions_str(row)}")
        print(f"    ci_outcomes:     {_ci_str(row)}")
        if row.attribution_source == "abandonment":
            print("    evidence:        abandonment")
        else:
            assert row.similarity_score is not None
            print(
                f"    attribution:     {row.similarity_score:.3f} "
                f"({row.attribution_source})"
            )
        print(f"    decision_branch: {row.decision_branch}  ci_bucket: {row.ci_bucket}")
        print(f"    confidence:      {_confidence_str(row)}")
        print(
            "    policy_v3:       "
            f"{_float_str(row.policy_v3_confidence)} "
            f"(delta={_delta_str(row.confidence_delta_from_policy_v3)})"
        )
        if row.ci_resolution is not None:
            print(
                "    ci_resolution:  "
                f"verdict={row.ci_resolution.verdict} "
                f"reliability={_float_str(row.ci_resolution.reliability)} "
                f"suspected_flake={row.ci_resolution.suspected_flake}"
            )
        if row.ci_resolution_skips:
            print(f"    ci_skipped:      {row.ci_resolution_skips}")
        print(f"    human_judgment:  {row.human_judgment!r}  (fill in by hand)")
    print(f"{len(rows)} row(s)")


def _float_str(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.3f}"


def _delta_str(value: float | int | None) -> str:
    if value is None:
        return "-"
    if isinstance(value, int):
        return f"{value:+d}"
    return f"{value:+.3f}"


def _parse_values(raw: str) -> tuple[float, ...]:
    values: list[float] = []
    for part in raw.split(","):
        token = part.strip()
        if not token:
            raise ValueError("--values must be a comma-separated list of floats")
        values.append(float(token))
    return tuple(values)


def _print_sensitivity_table(rows: list[LabelConfidenceSensitivityRow]) -> None:
    if not rows:
        print("no data")
        return

    current_stratum: tuple[str, int, str, str] | None = None
    for row in rows:
        stratum = (
            row.recipe_id,
            row.recipe_version,
            row.eligibility_source or "ineligible",
            row.knob,
        )
        if stratum != current_stratum:
            current_stratum = stratum
            print(
                f"--- {row.recipe_id} v{row.recipe_version} "
                f"eligibility_source={row.eligibility_source} {row.knob}  "
                f"affected_attributed_completions: {row.affected_attributed_completions}"
            )
            print(
                "    value  sft_count  delta  sft_mean  sft_median  "
                "all_mean  all_median  v3_all_mean  delta_v3"
            )
        print(
            f"    {row.value:>5.3f}  "
            f"{row.sft_eligible_count:>9d}  "
            f"{_delta_str(row.sft_eligible_delta):>5}  "
            f"{_float_str(row.sft_confidences.mean):>8}  "
            f"{_float_str(row.sft_confidences.median):>10}  "
            f"{_float_str(row.all_confidences.mean):>8}  "
            f"{_float_str(row.all_confidences.median):>10}  "
            f"{_float_str(row.policy_v3_all_confidences.mean):>11}  "
            f"{_delta_str(row.all_mean_delta_from_policy_v3):>8}"
        )
    print(f"{len(rows)} sweep row(s)")


def _print_latency_report(report: DecisionLatencyReport) -> None:
    if not report.buckets:
        print("no data")
        if any(report.skipped_decisions.values()):
            print(f"skipped: {report.skipped_decisions}")
        return

    print(
        f"latency_buckets: requested={report.buckets_requested} "
        f"returned={report.buckets_returned}"
    )
    print(
        f"decisions: included={report.included_decisions} "
        f"total={report.total_decisions}"
    )
    print(f"skipped: {report.skipped_decisions}")
    if report.accept_rate_delta is not None:
        print(
            f"accept_rate_delta_slowest_minus_fastest: {report.accept_rate_delta:.3f}"
        )
    if report.mean_confidence_delta is not None:
        print(
            "mean_confidence_delta_slowest_minus_fastest: "
            f"{report.mean_confidence_delta:.3f}"
        )
    for bucket in report.buckets:
        confidence = (
            "None"
            if bucket.mean_confidence is None
            else f"{bucket.mean_confidence:.3f}"
        )
        print(
            f"--- {bucket.bucket} "
            f"{bucket.min_latency_ms:.0f}-{bucket.max_latency_ms:.0f} ms"
        )
        print(
            f"    decisions:       {bucket.decisions} "
            f"(accepts={bucket.accepts} rejects={bucket.rejects})"
        )
        print(f"    accept_rate:     {bucket.accept_rate:.3f}")
        print(
            f"    mean_latency_ms: {bucket.mean_latency_ms:.1f} "
            f"mean_confidence: {confidence} "
            f"(n={bucket.confidence_count})"
        )


def build_parser() -> argparse.ArgumentParser:
    """The argv contract for this report, extracted so the generated
    CLI reference can walk it."""
    parser = argparse.ArgumentParser(
        prog="label_confidence_inspection", description=__doc__.splitlines()[0]
    )
    parser.add_argument(
        "--org",
        default=os.environ.get("SEDIMENT_ORG_ID"),
        required=not os.environ.get("SEDIMENT_ORG_ID"),
        help="org id (default: $SEDIMENT_ORG_ID)",
    )
    parser.add_argument(
        "--n",
        type=int,
        default=None,
        help=f"sample size, stratified across the confidence ladder's real "
        f"branches (default: {_DEFAULT_N})",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit JSON rows instead of a table"
    )
    parser.add_argument(
        "--recipe",
        choices=("sft_curated", "sft_verified"),
        default=None,
        help="SFT evidence recipe (default: sft_curated)",
    )
    parser.add_argument(
        "--sensitivity",
        action="store_true",
        help="sweep LabelConfidencePolicy knobs over all assembled attributed_completions",
    )
    parser.add_argument(
        "--knob",
        action="append",
        choices=LABEL_CONFIDENCE_POLICY_KNOBS,
        help="LabelConfidencePolicy knob to sweep in sensitivity mode; may repeat",
    )
    parser.add_argument(
        "--values",
        help="comma-separated grid for a single --knob in sensitivity mode",
    )
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=None,
        help=f"SFT confidence floor for sensitivity mode "
        f"(default: {_DEFAULT_MIN_CONFIDENCE})",
    )
    parser.add_argument(
        "--latency-buckets",
        type=int,
        help="emit the decision-latency diagnostic using this many quantile "
        "buckets instead of the label-confidence-inspection sample",
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

    if args.sensitivity and args.latency_buckets is not None:
        print(
            "error: use --sensitivity or --latency-buckets, not both",
            file=sys.stderr,
        )
        return 2
    if args.knob and not args.sensitivity:
        print("error: --knob requires --sensitivity", file=sys.stderr)
        return 2
    if args.min_confidence is not None and not args.sensitivity:
        print("error: --min-confidence requires --sensitivity", file=sys.stderr)
        return 2
    if args.n is not None and args.sensitivity:
        print("error: --n does not apply to --sensitivity", file=sys.stderr)
        return 2
    if args.n is not None and args.latency_buckets is not None:
        print("error: --n does not apply to --latency-buckets", file=sys.stderr)
        return 2
    if args.recipe is not None and args.latency_buckets is not None:
        print("error: --recipe does not apply to --latency-buckets", file=sys.stderr)
        return 2
    n = args.n if args.n is not None else _DEFAULT_N
    recipe = args.recipe if args.recipe is not None else "sft_curated"
    min_confidence = (
        args.min_confidence
        if args.min_confidence is not None
        else _DEFAULT_MIN_CONFIDENCE
    )
    if n <= 0:
        print("error: --n must be a positive integer", file=sys.stderr)
        return 2
    if args.values and not args.sensitivity:
        print("error: --values requires --sensitivity", file=sys.stderr)
        return 2
    if args.values and (not args.knob or len(args.knob) != 1):
        print("error: --values requires exactly one --knob", file=sys.stderr)
        return 2
    if not 0.0 <= min_confidence <= 1.0:
        print("error: --min-confidence must be in [0.0, 1.0]", file=sys.stderr)
        return 2
    if args.latency_buckets is not None and args.latency_buckets <= 0:
        print("error: --latency-buckets must be a positive integer", file=sys.stderr)
        return 2

    try:
        org_id = normalize_org_id(args.org)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    with one_shot_fact_store(
        args.database_url, operation="inspect label confidence"
    ) as store:
        mirrors = MirrorManager(args.mirror_path)
        sft_policy = SFTPolicy(
            recipe_id=recipe,
            min_confidence=min_confidence,
        )
        if args.sensitivity:
            if args.knob:
                try:
                    values = _parse_values(args.values) if args.values else None
                except ValueError as exc:
                    print(f"error: {exc}", file=sys.stderr)
                    return 2
                grids = {
                    knob: values if values is not None else DEFAULT_SWEEP_GRIDS[knob]
                    for knob in args.knob
                }
            else:
                grids = DEFAULT_SWEEP_GRIDS
            rows = generate_label_confidence_sensitivity(
                store,
                mirrors,
                org_id,
                sft_min_confidence=min_confidence,
                sft_policy=sft_policy,
                grids=grids,
            )
            latency_report = None
        elif args.latency_buckets is not None:
            latency_report = generate_decision_latency_report(
                store,
                mirrors,
                org_id,
                latency_buckets=args.latency_buckets,
            )
            rows = None
        else:
            rows = generate_label_confidence_inspection(
                store,
                mirrors,
                org_id,
                n=n,
                sft_policy=sft_policy,
            )
            latency_report = None

    if args.json:
        if latency_report is not None:
            print(json.dumps(asdict(latency_report), indent=2))
        else:
            print(json.dumps([asdict(r) for r in rows or []], indent=2))
    else:
        if latency_report is not None:
            print(f"org: {org_id}  latency buckets: {args.latency_buckets}")
            _print_latency_report(latency_report)
        elif args.sensitivity:
            print(
                f"org: {org_id}  sensitivity sweep  "
                f"sft min_confidence: {min_confidence:.3f}"
            )
            _print_sensitivity_table(rows)
        else:
            print(f"org: {org_id}  sample size: {n}")
            _print_table(rows or [])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
