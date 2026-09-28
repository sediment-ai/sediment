# SPDX-License-Identifier: AGPL-3.0-or-later
"""Offline check of J1's JEV judgment: does it keep a required requirement note?

Twelve synthetic families, two cases each (24 JEV calls, no coding agent):
``missing`` omits a hidden rule from the task, so the rule note is required;
``redundant`` states the rule in the task, so the note isn't required. Each
request uses the exact J1 state, questions, and qualification rule from
``bounded_evidence_selection`` (policy version 1). Candidates follow session
order: source read, check output, unrelated diagnostic, requirement note.

Declared before running: J1's judgment is usable as a filter for historical
requirements if it adds the note in at least 11 of 12 ``missing`` cases and in
at most 2 of 12 ``redundant`` cases. Synthetic text only; results are private.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics

import bounded_evidence_selection as sel

FIXTURE = Path(__file__).resolve().parent / "tests/fixtures/jev_judgment/families.json"
KINDS = ("source", "check", "distractor", "note")


def _text(order: int, content: str) -> dict:
    return {
        "order": order,
        "role": "user",
        "part": {"type": "text", "content": content},
    }


def _tool(order: int, tool: str, result: str) -> dict:
    part = {"type": "tool_call_response", "tool": tool, "result": result}
    return {"order": order, "role": "tool", "part": part}


def cases(families: list[dict]) -> list[dict]:
    """Two labeled cases per family; only the note in ``missing`` is required."""
    result = []
    for family in families:
        opening = (
            f"{family['task']} First read {family['module']}, then run its check. "
            "Do not edit files yet."
        )
        parts = {
            "source": _tool(1, "read", family["source"]),
            "check": _tool(2, "bash", family["check"]),
            "distractor": _tool(3, "bash", family["distractor"]),
            "note": _text(4, family["rule"]),
        }
        for profile in ("missing", "redundant"):
            task = f"Continue the task: {family['task']}"
            if profile == "redundant":
                task += " Requirement: " + family["rule"].split(": ", 1)[1]
            result.append(
                {
                    "family": family["name"],
                    "profile": profile,
                    "task": task,
                    "initial": [{"id": "e1", **_text(0, opening)}],
                    "candidates": [
                        {"id": f"c{i + 1}", **parts[kind]}
                        for i, kind in enumerate(KINDS)
                    ],
                    "required": {
                        kind: kind == "note" and profile == "missing" for kind in KINDS
                    },
                }
            )
    return result


def judge(case: dict, key: str, transport: dict | None, records: Path) -> dict:
    """One JEV request; qualification is policy version 1, unchanged."""
    body = sel._request_body(case["task"], case["initial"], case["candidates"])
    # qualify() orders ties by evidence identity; the conversation order stands in.
    included = [
        (
            view["id"],
            {
                "evidence": {
                    "reference": {
                        "inference_call_id": "offline",
                        "side": "input",
                        "message_index": view["order"],
                        "part_index": 0,
                    }
                }
            },
        )
        for view in case["candidates"]
    ]
    metrics = sel._metrics("J1")
    with sel._jev_client(transport) as client:
        scores = sel._decide(client, body, included, key, metrics, records)
    added = {label for label, _ in sel.qualify(scores, included)}
    labels = dict(zip(KINDS, (c["id"] for c in case["candidates"]), strict=True))
    return {
        "family": case["family"],
        "profile": case["profile"],
        "scores": {kind: scores[labels[kind]] for kind in KINDS},
        "added": {kind: labels[kind] in added for kind in KINDS},
        "usage": metrics["jev"]["usage"],
    }


def summarize(results: list[dict]) -> dict:
    """Failed calls are counted, not retried; any failure fails the pass rule."""
    rows = [r for r in results if "error" not in r]

    def note_added(profile):
        return sum(r["added"]["note"] for r in rows if r["profile"] == profile)

    medians = {
        f"{profile}/{kind}": {
            name: round(
                statistics.median(
                    r["scores"][kind][name] for r in rows if r["profile"] == profile
                ),
                2,
            )
            for name in sel.PROPOSITIONS
        }
        for profile in ("missing", "redundant")
        for kind in KINDS
    }
    missing, redundant = note_added("missing"), note_added("redundant")
    return {
        "cases": len(results),
        "failed_calls": sorted(r["error"] for r in results if "error" in r),
        "note_added_missing": missing,
        "note_added_redundant": redundant,
        "other_parts_added": {
            kind: sum(r["added"][kind] for r in rows) for kind in KINDS[:3]
        },
        "median_scores": medians,
        "input_tokens": sum(r["usage"].get("input_tokens") or 0 for r in rows),
        "usable_as_filter": missing >= 11 and redundant <= 2 and rows == results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--jev-proxy")
    parser.add_argument("--jev-ca-bundle")
    args = parser.parse_args()
    key = sel.load_jev_api_key()
    transport = {"proxy": args.jev_proxy, "ca_bundle": args.jev_ca_bundle}
    transport = {k: v for k, v in transport.items() if v} or None
    output = sel.base.private_directory(args.output)  # refuses an existing run
    families = json.loads(FIXTURE.read_bytes())["families"]
    rows = []
    for index, case in enumerate(cases(families)):
        records = sel.base.private_directory(output / f"{index:02d}")
        try:
            rows.append(judge(case, key, transport, records))
        except sel.BoundedSelectionError as exc:
            rows.append(
                {**{k: case[k] for k in ("family", "profile")}, "error": exc.reason}
            )
        sel.base.write_json(records / "result.json", rows[-1])
    summary = summarize(rows)
    sel.base.write_json(output / "summary.json", summary)
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
