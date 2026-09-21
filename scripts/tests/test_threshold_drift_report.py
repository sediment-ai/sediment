# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for scripts/threshold_drift_report.py.

``scripts/`` is not a package, so the module is loaded by path.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "threshold_drift_report.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("threshold_drift_report", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


threshold_drift_report = _load_module()
main = threshold_drift_report.main


def _case(prefix: str, shared: int, completion_unique: int, should_match: bool) -> dict:
    shared_tokens = [f"{prefix}_shared_{i}" for i in range(shared)]
    completion_tokens = shared_tokens + [
        f"{prefix}_completion_{i}" for i in range(completion_unique)
    ]
    return {
        "completion_text": " ".join(completion_tokens),
        "diff_added_lines": " ".join(shared_tokens),
        "should_match": should_match,
    }


def _manifest(tmp_path: Path) -> Path:
    """Two true positives and two true negatives — the tiny illustrative
    bundle both drift paths read."""
    path = tmp_path / "cases.json"
    path.write_text(
        json.dumps(
            {
                "cases": [
                    _case("tp_one", 11, 9, True),
                    _case("tp_two", 11, 9, True),
                    _case("fp_one", 1, 1, False),
                    _case("fp_two", 1, 1, False),
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def test_check_drift_json_reports_material_drift(tmp_path, capsys) -> None:
    manifest = _manifest(tmp_path)

    code = main(
        [
            str(manifest),
            "--check-drift",
            "--historical-threshold",
            "0.7",
            # This fixture is a tiny 4-case illustrative example, far below
            # the real MIN_DRIFT_CASES floor -- override it here to exercise
            # the drift logic itself, not the floor gate.
            "--min-cases",
            "2",
            "--json",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["material"] is True
    assert payload["historical_threshold"] == 0.7
    assert payload["optimal_threshold"] == 0.55
    assert payload["historical_result"]["f1"] == 0.0
    assert payload["optimal_result"]["f1"] == 1.0
    assert payload["precision_outside_ci"] is True
    assert payload["recall_outside_ci"] is True


def test_check_drift_default_min_cases_reports_insufficient_data(
    tmp_path, capsys
) -> None:
    # The default --min-cases derives from the corpus-sizing planner
    # (currently 97), so this repo's own tiny bundled fixture correctly
    # reports insufficient_data rather than a trusted verdict, unless the
    # operator explicitly overrides the floor.
    manifest = _manifest(tmp_path)

    code = main(
        [
            str(manifest),
            "--check-drift",
            "--historical-threshold",
            "0.7",
            "--json",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "insufficient_data"
    assert payload["reason"] == "too_few_cases"
