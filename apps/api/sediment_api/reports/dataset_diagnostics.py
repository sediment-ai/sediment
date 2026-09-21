# SPDX-License-Identifier: AGPL-3.0-or-later
"""DPO/SFT dataset diagnostics.

Prints ``sediment_export.generate_dataset_diagnostics`` for one org: compact
tables on stdout by default, or ``--json`` for machines.

    sediment report dataset-diagnostics --org acme-corp
    sediment report dataset-diagnostics --org acme-corp --json
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
    DEFAULT_DPO_NEAR_DUPLICATE_THRESHOLD,
    DPOPolicy,
    DatasetDiagnostics,
    SFTPolicy,
    generate_dataset_diagnostics,
)

from ..database import add_database_url_argument, one_shot_fact_store

_DEFAULT_MIRROR_PATH = "./mirrors"


def _recipe_label(recipe_id: str, recipe_version: int) -> str:
    return f"{recipe_id} v{recipe_version}"


def _source_label(
    eligibility_source: str | None,
    chosen_label_source: str | None,
    rejected_label_source: str | None,
) -> str:
    if eligibility_source is not None:
        return eligibility_source
    return f"{chosen_label_source}/{rejected_label_source}"


def _print_balance(report: DatasetDiagnostics) -> None:
    print("class/model balance")
    if not report.model_balance:
        print("  no data")
        return
    print(f"{'dataset':<8} {'recipe':<16} {'source':<32} {'model':<28} {'rows':>8}")
    for row in report.model_balance:
        source = _source_label(
            row.eligibility_source,
            row.chosen_label_source,
            row.rejected_label_source,
        )
        print(
            f"{row.dataset:<8} {_recipe_label(row.recipe_id, row.recipe_version):<16} "
            f"{source:<32} {row.model:<28} {row.rows:>8}"
        )


def _print_distributions(report: DatasetDiagnostics) -> None:
    print("confidence distribution")
    if not report.confidence_distributions:
        print("  no data")
        return
    print(
        f"{'dataset':<8} {'recipe':<16} {'source':<32} {'metric':<12} "
        f"{'model':<28} {'count':>7} {'mean':>8} {'median':>8} {'min':>8} "
        f"{'max':>8} {'stdev':>8}"
    )
    for row in report.confidence_distributions:
        stats = row.stats
        source = _source_label(
            row.eligibility_source,
            row.chosen_label_source,
            row.rejected_label_source,
        )
        print(
            f"{row.dataset:<8} {_recipe_label(row.recipe_id, row.recipe_version):<16} "
            f"{source:<32} {row.metric:<12} {row.model:<28} "
            f"{stats.count:>7} {stats.mean:>8.3f} {stats.median:>8.3f} "
            f"{stats.min:>8.3f} {stats.max:>8.3f} {stats.stdev:>8.3f}"
        )


def _print_duplicates(report: DatasetDiagnostics) -> None:
    print("cross-split prompt duplicates")
    for row in report.cross_split_duplicates:
        source = _source_label(
            row.eligibility_source,
            row.chosen_label_source,
            row.rejected_label_source,
        )
        print(
            f"{row.dataset} {_recipe_label(row.recipe_id, row.recipe_version)} "
            f"{source}: {row.duplicate_prompt_count}"
        )
        for example in row.examples:
            print(
                f"  train={example.train_count} eval={example.eval_count} "
                f"prompt={example.prompt}"
            )


def _print_floor_exclusions(report: DatasetDiagnostics) -> None:
    print("confidence-floor exclusions")
    if not report.confidence_floor_exclusions:
        print("  no data")
        return
    print(
        f"{'dataset':<8} {'recipe':<16} {'source':<32} {'model':<28} "
        f"{'eligible':>10} {'excluded':>10} {'rate':>8}"
    )
    for row in report.confidence_floor_exclusions:
        print(
            f"{row.dataset:<8} {_recipe_label(row.recipe_id, row.recipe_version):<16} "
            f"{row.eligibility_source:<32} {row.model:<28} "
            f"{row.otherwise_eligible_completions:>10} "
            f"{row.excluded_by_floor:>10} {row.exclusion_rate:>8.3f}"
        )


def _print_dpo_near_duplicates(report: DatasetDiagnostics) -> None:
    print("dpo near-duplicate prompt buckets")
    if not report.dpo_near_duplicate_buckets:
        print("  no data")
        return
    for row in report.dpo_near_duplicate_buckets:
        print(
            f"  recipe={row.recipe_id} v{row.recipe_version} "
            f"sources={row.chosen_label_source}/{row.rejected_label_source} "
            f"buckets={row.bucket_count} compared={row.comparable_pair_count} "
            f"threshold={row.threshold:.3f} near_misses={row.near_miss_pair_count}"
        )
        for example in row.examples:
            print(
                f"    org={example.org_id} model={example.model} "
                f"similarity={example.similarity:.3f}"
            )


def _print_dpo_bucket_sparsity(report: DatasetDiagnostics) -> None:
    print("dpo prompt-bucket sparsity")
    if not report.dpo_bucket_sparsity:
        print("  no data")
        return
    for sparsity in report.dpo_bucket_sparsity:
        print(
            f"  recipe={sparsity.recipe_id} v{sparsity.recipe_version} "
            f"sources={sparsity.chosen_label_source}/{sparsity.rejected_label_source}"
        )
        print(f"  buckets: {sparsity.total_buckets}")
        print(f"  candidates: {sparsity.total_candidates}")
        print(
            "  singleton candidates: "
            f"{sparsity.singleton_candidates} "
            f"({sparsity.singleton_candidate_fraction:.1%})"
        )
        print(f"  cap-hit buckets: {sparsity.cap_hit_buckets}")


def _format_counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{key}={value}" for key, value in counts.items()) or "-"


def _print_fate_diagnostic(report: DatasetDiagnostics) -> None:
    print("Fate diagnostic")
    print(f"  fates: {_format_counts(report.fate.fates)}")
    print(
        "  human-explicit accept fates: "
        f"{_format_counts(report.fate.explicit_accept_fates)}"
    )
    print(
        "  fates with external changes: "
        f"{_format_counts(report.fate.fates_with_external_changes)}"
    )
    print(f"  derivation skipped: {_format_counts(report.fate.skipped)}")
    print(f"  provenance: {report.fate.provenance or '-'}")


def _print_table(report: DatasetDiagnostics) -> None:
    _print_balance(report)
    print()
    _print_distributions(report)
    print()
    _print_dpo_bucket_sparsity(report)
    print()
    _print_duplicates(report)
    print()
    _print_floor_exclusions(report)
    print()
    _print_dpo_near_duplicates(report)
    print()
    _print_fate_diagnostic(report)
    print()
    print("abandonment")
    print(f"  abandoned sessions: {report.abandonment.abandoned_sessions}")
    print(f"  grade-eligible sessions: {report.abandonment.grade_eligible_sessions}")
    print(f"  implicit-only sessions: {report.abandonment.implicit_only_sessions}")
    print(f"  negative completions: {report.abandonment.negative_completions}")
    print(
        f"  explicit accepts unjoined: {report.abandonment.explicit_accepts_unjoined}"
    )
    skipped = ", ".join(
        f"{reason}={count}"
        for reason, count in report.abandonment.derivation_skipped.items()
    )
    print(f"  derivation skipped: {skipped or '-'}")
    print(f"  provenance: {report.abandonment.provenance or '-'}")


def build_parser() -> argparse.ArgumentParser:
    """The argv contract for this report, extracted so the generated
    CLI reference can walk it."""
    parser = argparse.ArgumentParser(
        prog="dataset_diagnostics", description=__doc__.splitlines()[0]
    )
    parser.add_argument(
        "--org",
        default=os.environ.get("SEDIMENT_ORG_ID"),
        required=not os.environ.get("SEDIMENT_ORG_ID"),
        help="org id (default: $SEDIMENT_ORG_ID)",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit JSON report instead of tables"
    )
    parser.add_argument(
        "--dpo-recipe",
        choices=("dpo_human", "dpo_outcome"),
        default="dpo_human",
        help="DPO evidence recipe (default: dpo_human)",
    )
    parser.add_argument(
        "--sft-recipe",
        choices=("sft_curated", "sft_verified"),
        default="sft_curated",
        help="SFT evidence recipe (default: sft_curated)",
    )
    parser.add_argument(
        "--max-examples",
        type=int,
        default=5,
        help="maximum examples per diagnostics section (default: 5)",
    )
    parser.add_argument(
        "--dpo-near-duplicate-threshold",
        type=float,
        default=DEFAULT_DPO_NEAR_DUPLICATE_THRESHOLD,
        help=(
            "minimum token Jaccard similarity for DPO near-duplicate prompt "
            f"bucket flags (default: {DEFAULT_DPO_NEAR_DUPLICATE_THRESHOLD})"
        ),
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

    if args.max_examples < 0:
        print("error: --max-examples must be >= 0", file=sys.stderr)
        return 2
    if not 0.0 <= args.dpo_near_duplicate_threshold <= 1.0:
        print(
            "error: --dpo-near-duplicate-threshold must be between 0.0 and 1.0",
            file=sys.stderr,
        )
        return 2

    try:
        org_id = normalize_org_id(args.org)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    with one_shot_fact_store(
        args.database_url, operation="generate dataset diagnostics"
    ) as store:
        report = generate_dataset_diagnostics(
            store,
            MirrorManager(args.mirror_path),
            org_id,
            dpo_policy=DPOPolicy(recipe_id=args.dpo_recipe),
            sft_policy=SFTPolicy(recipe_id=args.sft_recipe),
            max_duplicate_examples=args.max_examples,
            dpo_near_duplicate_threshold=args.dpo_near_duplicate_threshold,
            max_near_duplicate_examples=args.max_examples,
        )

    if args.json:
        print(json.dumps(asdict(report), indent=2))
    else:
        print(f"org: {org_id}")
        _print_table(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
