# SPDX-License-Identifier: AGPL-3.0-or-later
from collections import Counter
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bounded_selection_eval as run  # noqa: E402
import context_minimal_eval as phase2  # noqa: E402


def test_schedule_rotates_three_arms_over_36_continuations(monkeypatch):
    monkeypatch.setattr(run, "SETS", phase2.SETS)
    order = phase2.run_order()
    assert len(order) == 36 and Counter(s["arm"] for s in order) == dict.fromkeys(
        phase2.ARMS, 12
    )
    firsts = Counter(order[i]["arm"] for i in range(0, 36, 3))
    assert firsts == dict.fromkeys(phase2.ARMS, 4)
    for i in range(0, 36, 3):
        task = {(s["family"], s["profile"], s["repetition"]) for s in order[i : i + 3]}
        assert len(task) == 1 and {s["arm"] for s in order[i : i + 3]} == set(
            phase2.ARMS
        )


def _row(slot, tokens, both=True, rule=True):
    return {
        **slot,
        "status": "settled",
        "measured": True,
        "measurement_complete": True,
        "instrument_failure": False,
        "behavior_pass": True,
        "constraint_pass": both,
        "coding_usage": {"input": tokens, "output": 0},
        "selector_usage": {"input_tokens": 0, "output_tokens": 0},
        "selection": {"decision": "jev", "context_bytes": 1},
        "evidence_hits": {"rule": rule},
    }


def test_summary_applies_the_declared_targets(monkeypatch):
    monkeypatch.setattr(run, "SETS", phase2.SETS)
    tokens = {"FULL": 100, "K": 80, "J2": 55}
    rows = [_row(s, tokens[s["arm"]]) for s in phase2.run_order()]
    result = phase2.summarize(rows)
    assert result["acceptance"]["supported"] is True
    assert result["j2_to_full_token_ratio"] == 0.55
    tokens["J2"] = 61
    rows = [_row(s, tokens[s["arm"]]) for s in phase2.run_order()]
    assert phase2.summarize(rows)["acceptance"]["tokens"] is False
    # A redundant run doesn't need the rule; a missing or correction run does.
    rows = [
        _row(s, 50, rule=s["arm"] != "J2" or s["profile"] != "redundant")
        for s in phase2.run_order()
    ]
    assert phase2.summarize(rows)["acceptance"]["rule_delivery"] is True
    missing_j2 = next(r for r in rows if r["arm"] == "J2" and r["profile"] == "missing")
    missing_j2["evidence_hits"]["rule"] = False
    assert phase2.summarize(rows)["acceptance"]["rule_delivery"] is False
    assert phase2.summarize(rows[:-1])["acceptance"]["validity"] is False


def test_configure_points_the_runner_at_phase_2(monkeypatch, tmp_path):
    for name in (
        "FIXTURES",
        "EVALUATION",
        "ARMS",
        "SETS",
        "run_order",
        "summarize",
        "GATE_UPSTREAM_RETRY",
        "PI_IDLE_TIMEOUT_MS",
        "EXCLUDE_UPSTREAM_UNAVAILABLE",
        "CONTEXT_WINDOW",
        "protocol_identity",
    ):
        monkeypatch.setattr(run, name, getattr(run, name))
    phase2.configure()
    runtime = tmp_path / "runtime.json"
    runtime.write_text("{}")
    config = {
        "model": "m",
        "agent_image": "a",
        "gate_image": "g",
        "gateway_url": "u",
        "api_url": "u",
        "operator_api_url": "u",
        "runtime_identity_path": runtime,
    }
    identity = run.protocol_identity(config, "heldout", "direct")
    assert identity["experiment"] == "context-minimal-phase-2"
    assert identity["protocol_version"] == 5
    assert identity["generation"]["context_window"] == 65536
    assert identity["generation"]["gate_upstream_retry"] is False
    assert identity["generation"]["exclude_upstream_unavailable"] is True
    assert identity["generation"]["pi_idle_timeout_ms"] == 840_000
    assert len(identity["run_order"]) == 36
    assert set(identity["fixture_hashes"]) >= {"evaluation/verify.py"}


def test_upstream_unavailable_runs_are_tolerated_up_to_two_tasks(monkeypatch):
    monkeypatch.setattr(run, "SETS", phase2.SETS)
    rows = [
        _row(s, {"FULL": 100, "K": 80, "J2": 50}[s["arm"]]) for s in phase2.run_order()
    ]
    for row in rows[:2]:  # the first task loses two arms; row 3 fails the second
        row.update(
            status="upstream_unavailable",
            measured=False,
            measurement_complete=False,
            upstream_unavailable=True,
        )
    rows[3].update(
        rows[0]
        | {k: rows[3][k] for k in ("slot", "arm", "family", "profile", "repetition")}
    )
    result = phase2.summarize(rows)
    assert result["complete_triples"] == 10 and result["acceptance"]["validity"]
    rows[6].update(
        rows[0]
        | {k: rows[6][k] for k in ("slot", "arm", "family", "profile", "repetition")}
    )
    assert phase2.summarize(rows)["acceptance"]["validity"] is False
