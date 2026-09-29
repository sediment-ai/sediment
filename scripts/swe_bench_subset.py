# SPDX-License-Identifier: AGPL-3.0-or-later
"""Draw the SWE-bench Verified instances for evaluations E1 and E2.

The gateway pruning spec
(``docs/superpowers/specs/2026-09-28-gateway-context-pruning-design.md``)
runs E2 on a fixed-seed random 50-instance subset of SWE-bench Verified, and
records E1's Sessions on instances outside that subset, so E2 stays held out.
This draws both from the sorted instance IDs with one seeded generator: E2's
50 first, then an ordered list of E1 candidates from the rest. E1 uses the
candidates in order until it has enough long Sessions.

The committed output, ``scripts/erode_eval_instances.json``, is the source of
truth; this script only shows how it was made.

Usage:
    uv run python scripts/swe_bench_subset.py --output scripts/erode_eval_instances.json
"""

from __future__ import annotations

import argparse
import json
import random
import urllib.request
from pathlib import Path

DATASET = "princeton-nlp/SWE-bench_Verified"
ROWS_URL = (
    "https://datasets-server.huggingface.co/rows?dataset={dataset}"
    "&config=default&split=test&offset={offset}&length={length}"
)
SEED = 20260929
E2_SIZE = 50
E1_CANDIDATES = 10


def draw(ids: list[str], seed: int = SEED) -> dict:
    """E2's subset, then E1's ordered candidates, from one seeded generator."""
    ordered = sorted(set(ids))
    if len(ordered) != len(ids):
        raise ValueError("instance IDs are not unique")
    rng = random.Random(seed)
    e2 = sorted(rng.sample(ordered, E2_SIZE))
    held_out = set(e2)
    rest = [i for i in ordered if i not in held_out]
    return {"e2": e2, "e1_candidates": rng.sample(rest, E1_CANDIDATES)}


def fetch_ids() -> list[str]:
    ids: list[str] = []
    while True:
        url = ROWS_URL.format(
            dataset=DATASET.replace("/", "%2F"), offset=len(ids), length=100
        )
        with urllib.request.urlopen(url, timeout=60) as response:
            page = json.load(response)
        ids += [row["row"]["instance_id"] for row in page["rows"]]
        if not page["rows"] or len(ids) >= page["num_rows_total"]:
            return ids


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    ids = fetch_ids()
    result = {
        "dataset": DATASET,
        "split": "test",
        "instances": len(ids),
        "seed": SEED,
        **draw(ids),
    }
    args.output.write_text(json.dumps(result, indent=1) + "\n")
    print(f"{len(ids)} instances; wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
