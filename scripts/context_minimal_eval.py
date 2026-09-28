# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase 2: can a decision model keep a resuming agent's context minimal?

Compares three ways to hand captured history to a continuing coding agent:
FULL (the whole captured catalog), K (keyword selection, Phase 1), and J2 (JEV
judges the recorded user and assistant text; policy version 2). Two held-out
families, three profiles, three arms, two repetitions: 36 continuations. This
driver reuses ``bounded_selection_eval`` unchanged in operation, pointing it at
the Phase 2 fixtures, arms, schedule, and summary. Commands and flags are the
same (``preflight``, ``source``, ``run``, ``summarize``).

The targets below were fixed before any development or held-out run.
"""

from __future__ import annotations

import statistics
from pathlib import Path

import bounded_selection_eval as run

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "tests/fixtures/context_minimal"
ARMS = ("FULL", "K", "J2")
SETS = {
    "heldout": {"families": ("ledger-balance", "tag-normalize"), "repetitions": 2},
    "development": {"families": ("sensor-window",), "repetitions": 1},
}
TARGETS = {
    "validity": "every slot recorded, measured, and complete; no instrument failure",
    "rule_delivery": "J2 delivers the rule in every missing and correction run",
    "quality": "J2 both-check passes >= FULL both-check passes - 1",
    "tokens": "J2 combined tokens <= 0.60 x FULL over complete triples",
    "supported": "all four targets hold",
}


def run_order(task_set: str = "heldout") -> list[dict]:
    """Repetition-major; the three arms rotate across tasks and repetitions."""
    order = []
    for repetition in range(1, SETS[task_set]["repetitions"] + 1):
        for index, (family, profile) in enumerate(run.tasks(task_set)):
            shift = (index + repetition) % len(ARMS)
            for arm in ARMS[shift:] + ARMS[:shift]:
                order.append(
                    {
                        "slot": len(order) + 1,
                        "family": family,
                        "profile": profile,
                        "arm": arm,
                        "repetition": repetition,
                    }
                )
    return order


def _tokens(row: dict) -> int | None:
    coding, selector = row.get("coding_usage") or {}, row.get("selector_usage") or {}
    values = [
        coding.get("input"),
        coding.get("output"),
        selector.get("input_tokens", 0),
        selector.get("output_tokens", 0),
    ]
    if not row.get("measurement_complete") or not all(
        type(v) is int and v >= 0 for v in values
    ):
        return None
    return sum(values)


def _both(row: dict) -> bool:
    return row.get("behavior_pass") is True and row.get("constraint_pass") is True


def summarize(records: list[dict], task_set: str = "heldout") -> dict:
    schedule = run_order(task_set)
    arms = {}
    for arm in ARMS:
        rows = [r for r in records if r.get("arm") == arm]
        tokens = [_tokens(r) for r in rows]
        known = [t for t in tokens if t is not None]
        context = [
            (r.get("selection") or {}).get("context_bytes")
            for r in rows
            if type((r.get("selection") or {}).get("context_bytes")) is int
        ]
        arms[arm] = {
            "runs": len(rows),
            "measurement_complete": sum(
                bool(r.get("measurement_complete")) for r in rows
            ),
            "both_checks": sum(_both(r) for r in rows),
            "behavior_pass": sum(r.get("behavior_pass") is True for r in rows),
            "constraint_pass": sum(r.get("constraint_pass") is True for r in rows),
            "tokens_total": sum(known) if len(known) == len(rows) else None,
            "tokens_median": statistics.median(known) if known else None,
            "context_bytes_median": statistics.median(context) if context else None,
            "rule_delivered": sum(
                bool((r.get("evidence_hits") or {}).get("rule")) for r in rows
            ),
            "decisions": dict(
                sorted(
                    {
                        d: sum(
                            (r.get("selection") or {}).get("decision") == d
                            for r in rows
                        )
                        for d in {
                            (r.get("selection") or {}).get("decision") for r in rows
                        }
                        if d
                    }.items()
                )
            ),
            "statuses": dict(
                sorted(
                    {
                        s: sum(r.get("status") == s for r in rows)
                        for s in {r.get("status") for r in rows}
                    }.items()
                )
            ),
        }
    triples: dict = {}
    for row in records:
        key = (row.get("family"), row.get("profile"), row.get("repetition"))
        triples.setdefault(key, {})[row.get("arm")] = _tokens(row)
    complete = [t for t in triples.values() if all(t.get(a) is not None for a in ARMS)]
    totals = {a: sum(t[a] for t in complete) for a in ARMS} if complete else None
    j2_needed = [
        r
        for r in records
        if r.get("arm") == "J2" and r.get("profile") in {"missing", "correction"}
    ]
    valid = len(records) == len(schedule) and all(
        r.get("measurement_complete") and not r.get("instrument_failure")
        for r in records
    )
    rule = bool(j2_needed) and all(
        (r.get("evidence_hits") or {}).get("rule") for r in j2_needed
    )
    quality = arms["J2"]["both_checks"] >= arms["FULL"]["both_checks"] - 1
    ratio = totals["J2"] / totals["FULL"] if totals and totals["FULL"] else None
    tokens_met = ratio is not None and ratio <= 0.60
    return {
        "experiment": "context-minimal-phase-2",
        "set": task_set,
        "scheduled_runs": len(schedule),
        "recorded_runs": len(records),
        "arms": arms,
        "complete_triples": len(complete),
        "triple_token_totals": totals,
        "j2_to_full_token_ratio": round(ratio, 4) if ratio is not None else None,
        "j2_to_k_token_ratio": round(totals["J2"] / totals["K"], 4)
        if totals and totals["K"]
        else None,
        "targets": TARGETS,
        "acceptance": {
            "validity": valid,
            "rule_delivery": rule,
            "quality": quality,
            "tokens": tokens_met,
            "supported": valid and rule and quality and tokens_met,
        },
        "experiment_complete": valid,
    }


def configure() -> None:
    """Point the Phase 1 runner at Phase 2 inputs; its operation is unchanged."""
    run.FIXTURES = FIXTURES
    run.EVALUATION = FIXTURES / "evaluation"
    run.ARMS = ARMS
    run.SETS = SETS
    run.run_order = run_order
    run.summarize = summarize
    run.GATE_UPSTREAM_RETRY = True
    run.CONTEXT_WINDOW = 65536
    identity = run.protocol_identity

    def protocol_identity(config: dict, task_set: str, transport_label: str) -> dict:
        value = identity(config, task_set, transport_label)
        value.update(
            experiment="context-minimal-phase-2",
            protocol_version=4,
            phase2_driver_sha256=run.legacy.digest(Path(__file__).read_bytes()),
            arms=list(ARMS),
            targets=TARGETS,
        )
        return value

    run.protocol_identity = protocol_identity


if __name__ == "__main__":
    configure()
    raise SystemExit(run.main())
