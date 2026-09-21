# SPDX-License-Identifier: AGPL-3.0-or-later
"""Plan hand-labelled corpus size for precision/recall estimates."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from sediment_derive.precision_harness import plan_labelled_corpus_size


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Estimate labelled examples needed for a target 95% CI half-width "
            "around one precision/recall proportion."
        )
    )
    parser.add_argument(
        "--target-margin",
        type=float,
        required=True,
        help="Target CI half-width in raw proportion points, e.g. 0.1 for +/-10pp.",
    )
    parser.add_argument(
        "--assumed-rate",
        type=float,
        default=None,
        help=(
            "Optional assumed true precision/recall rate. The p=0.5 worst-case "
            "answer is always printed too."
        ),
    )
    parser.add_argument(
        "--confidence",
        type=float,
        default=0.95,
        help="Confidence level for the planning formula, default: 0.95.",
    )
    args = parser.parse_args(argv)

    try:
        estimate = plan_labelled_corpus_size(
            args.target_margin,
            assumed_rate=args.assumed_rate,
            confidence=args.confidence,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"target_margin: {estimate.target_margin:.6g}")
    print(f"confidence: {estimate.confidence:.6g}")
    if estimate.assumed_rate is not None:
        print(f"assumed_rate: {estimate.assumed_rate:.6g}")
        print(f"assumed_rate_required_n: {estimate.assumed_rate_required_n}")
    print(f"worst_case_rate: {estimate.worst_case_rate:.6g}")
    print(f"worst_case_required_n: {estimate.worst_case_required_n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
