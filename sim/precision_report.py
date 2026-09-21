# SPDX-License-Identifier: AGPL-3.0-or-later
"""Notes/jaccard precision-recall against the sim ground-truth manifest.

Runs the Tier A scenarios (``scenarios.py``) into a workdir, derives
attributions over the resulting facts and mirrors, and scores the derivation
against the manifest's labeled rows — notes and jaccard separately at the
default 0.7 threshold, plus a jaccard threshold sweep. Exits nonzero when a
default-threshold metric drops below its pinned floor.

Two scoring axes, matching the manifest's two labels:

- Behavior axis (default policy vs ``expected_attribution``): the pipeline
  regression pin. The manifest records what the pipeline is EXPECTED to do —
  including attributing the retyped-after-reject bait via jaccard — so the
  honest value here is 1.0/1.0 and any deviation is a behavior change worth
  a red build (a tokenizer tweak that flips the 30% gradient row, say).
- Ground-truth axis (``true_link``): how well jaccard finds REAL links on
  unstamped commits, scored at the default threshold and swept across
  thresholds. The bait is a false positive at EVERY threshold and the
  hand-edit gradient rows are true links the 0.7 threshold misses — so this
  axis is below 1.0 BY DESIGN; pinning 1.0 would be a lie.

Floors are pinned at the first honest run minus a small margin — never
aspirational. Sim precision is an upper bound / mechanics check, not a claim
about real-repo performance: the sim is the harness, not the exam, and
labeling real repos is separate work.

Usage:
    python sim/precision_report.py [--workdir DIR]
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sediment_core import FactStore
from sediment_core.postgres_engine import create_postgres_engine
from sediment_derive import (
    Attribution,
    AttributionPolicy,
    MirrorManager,
    SimilarityPolicy,
    derive_attributions,
)

HERE = Path(__file__).resolve().parent


def _load_sibling(name: str):
    if name in sys.modules:  # one shared instance across harness modules
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # before exec: dataclasses needs it registered
    spec.loader.exec_module(module)
    return module


scenarios = _load_sibling("scenarios")

# Pinned floors — honest run minus a margin of about one manifest row.
# Update deliberately, together with the manifest, never to green a red run.
# Honest values over the full Groups 1-6 catalog: behavior axis notes
# 1.0/1.0 (6 rows) and jaccard 1.0/1.0 (16 rows — the bait's jaccard match
# is EXPECTED behavior); ground-truth axis at 0.7: P=0.9375 (tp=15, fp=1 —
# the bait, a designed FP at every threshold), R=0.8824 (fn=2: the 30%/60%
# hand-edit rows sit below the 0.7 threshold by design). The margins are
# sized so one extra false positive breaches the precision floor and one
# lost true row breaches the recall floor. (Below ~0.3 the sweep also
# surfaces the rlvr buggy-vs-fix weak matches — visible in the table, not
# floor-gated: the default-threshold floors are the contract.)
FLOORS = {
    ("git_notes", "precision"): 0.99,
    ("git_notes", "recall"): 0.99,
    ("jaccard", "precision"): 0.99,
    ("jaccard", "recall"): 0.99,
    ("jaccard_truth", "precision"): 0.90,
    ("jaccard_truth", "recall"): 0.85,
}
SWEEP_THRESHOLDS = tuple(round(0.1 * i, 1) for i in range(1, 10))

_Key = tuple[str, str, str]  # (inference_call_id, commit_sha, file_path)


@dataclass(frozen=True)
class Metrics:
    tp: int
    fp: int
    fn: int

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else 1.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else 1.0


def _predicted(attributions: list[Attribution], source: str) -> set[_Key]:
    return {
        (c.inference_call_id, c.commit_sha, c.file_path)
        for c in attributions
        if c.attribution_source.value == source
    }


def _row_key(row: dict[str, Any]) -> _Key:
    return (row["inference_call_id"], row["expected_commit"], row["expected_file"])


def _score(predicted: set[_Key], expected: set[_Key]) -> Metrics:
    tp = len(predicted & expected)
    return Metrics(tp=tp, fp=len(predicted) - tp, fn=len(expected) - tp)


def compute_report(run) -> dict[str, Any]:
    """Score one scenario run: per-source metrics at the default policy plus
    the jaccard sweep. ``run`` is a ``scenarios.SimRun`` (duck-typed)."""
    mirrors = MirrorManager(run.mirror_path)
    engine = create_postgres_engine(run.database_url)
    try:
        store = FactStore(engine)
        default = derive_attributions(store, mirrors, scenarios.ORG)
        by_source = {
            source: _score(
                _predicted(default, source),
                {
                    _row_key(row)
                    for row in run.rows
                    if row["expected_attribution"] == source
                },
            )
            for source in ("git_notes", "jaccard")
        }

        # Sweep axis: jaccard predictions vs TRUE links on unstamped commits
        # (notes-stamped rows are never jaccard's to find — the note
        # supersedes before jaccard is consulted).
        sweep_truth = {
            _row_key(row)
            for row in run.rows
            if row["true_link"] and row["expected_attribution"] != "git_notes"
        }
        sweep = {}
        for threshold in SWEEP_THRESHOLDS:
            # The default policy differs from the sweep only in the swept
            # knob, so its derivation IS that threshold's point — deriving
            # the same attributions twice is pure cost.
            attributions = (
                default
                if threshold == AttributionPolicy().jaccard.min_similarity
                else derive_attributions(
                    store,
                    mirrors,
                    scenarios.ORG,
                    AttributionPolicy(
                        jaccard=SimilarityPolicy(
                            min_similarity=threshold,
                            lookback_window_minutes=(
                                AttributionPolicy().jaccard.lookback_window_minutes
                            ),
                        )
                    ),
                )
            )
            sweep[threshold] = _score(_predicted(attributions, "jaccard"), sweep_truth)
    finally:
        engine.dispose()

    # The ground-truth axis at the default threshold IS the sweep's 0.7 point.
    gated = {**by_source, "jaccard_truth": sweep[0.7]}
    failures = [
        (source, metric, floor, getattr(gated[source], metric))
        for (source, metric), floor in FLOORS.items()
        if getattr(gated[source], metric) < floor
    ]
    return {"default": by_source, "sweep": sweep, "failures": failures}


def print_report(report: dict[str, Any]) -> None:
    print("behavior axis — default policy vs expected_attribution:")
    for source, m in report["default"].items():
        print(
            f"  {source:<8} precision={m.precision:.4f} recall={m.recall:.4f} "
            f"(tp={m.tp} fp={m.fp} fn={m.fn})"
        )
    print("ground-truth axis — jaccard sweep vs true_link (unstamped commits):")
    for threshold, m in sorted(report["sweep"].items()):
        print(
            f"  t={threshold:.1f}  precision={m.precision:.4f} "
            f"recall={m.recall:.4f} (tp={m.tp} fp={m.fp} fn={m.fn})"
        )
    for source, metric, floor, value in report["failures"]:
        print(f"FLOOR BREACH: {source} {metric} {value:.4f} < {floor}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sim precision/recall vs the ground-truth manifest"
    )
    parser.add_argument(
        "--workdir", help="working directory (default: a fresh temp dir)"
    )
    args = parser.parse_args(argv)
    workdir = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp("sim"))

    run = scenarios.run_all(workdir)
    print(f"scenarios green: {len(run.rows)} manifest rows ({run.manifest_path})")
    report = compute_report(run)
    print_report(report)
    return 1 if report["failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
