# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for scripts/calibration_check.py.

``scripts/`` is not a package, so the module is loaded by path.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "calibration_check.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("calibration_check", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


main = _load_module().main


def test_csv_input_prints_metrics_and_bucket_table(tmp_path, capsys) -> None:
    path = tmp_path / "labels.csv"
    path.write_text(
        "predicted_confidence,actual_outcome\n".replace(
            "predicted_confidence,actual_outcome",
            "predicted_confidence,actual_outcome,recipe_id,recipe_version,eligibility_source",
        )
        + "0.9,true,sft_curated,1,explicit_accept\n"
        "0.9,false,sft_curated,1,explicit_accept\n"
        "0.9,true,sft_curated,1,explicit_accept\n"
        "0.9,false,sft_curated,1,explicit_accept\n"
    )

    code = main([str(path), "--bins", "10"])

    assert code == 0
    out = capsys.readouterr().out
    assert "rows: 4" in out
    assert "brier_score: 0.410000" in out
    assert "ece: 0.400000" in out
    assert "auroc: 0.500000" in out
    assert "bucket_inversions: 0" in out
    assert "mean_predicted" in out
    assert "0.900000" in out
    assert "0.500000" in out


def test_jsonl_input_accepts_recipe_stamped_objects(tmp_path, capsys) -> None:
    path = tmp_path / "labels.jsonl"
    rows = [
        {
            "predicted_confidence": 0.0,
            "actual_outcome": False,
            "recipe_id": "sft_curated",
            "recipe_version": 1,
            "eligibility_source": "explicit_accept",
        },
        {
            "predicted_confidence": 1.0,
            "actual_outcome": True,
            "recipe_id": "sft_curated",
            "recipe_version": 1,
            "eligibility_source": "explicit_accept",
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")

    code = main([str(path)])

    assert code == 0
    out = capsys.readouterr().out
    assert "rows: 2" in out
    assert "brier_score: 0.000000" in out
    assert "ece: 0.000000" in out
    assert "auroc: 1.000000" in out


def test_inverted_buckets_print_inversion_count_and_table(tmp_path, capsys) -> None:
    path = tmp_path / "labels.jsonl"
    rows = [
        {
            "predicted_confidence": 0.1,
            "actual_outcome": True,
            "recipe_id": "sft_curated",
            "recipe_version": 1,
            "eligibility_source": "explicit_accept",
        },
        {
            "predicted_confidence": 0.9,
            "actual_outcome": False,
            "recipe_id": "sft_curated",
            "recipe_version": 1,
            "eligibility_source": "explicit_accept",
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")

    code = main([str(path), "--bins", "10"])

    assert code == 0
    out = capsys.readouterr().out
    assert "bucket_inversions: 1" in out
    assert "lower_bucket" in out
    assert "higher_bucket" in out
    assert "           1             9        1.000000         0.000000" in out


def test_bad_input_is_a_clean_error_not_a_traceback(tmp_path, capsys) -> None:
    path = tmp_path / "labels.csv"
    path.write_text("confidence,outcome\n0.5,true\n")

    code = main([str(path)])

    assert code == 2
    err = capsys.readouterr().err
    assert "error:" in err
    assert "missing required column" in err


def test_policy_v3_column_prints_version_comparison(tmp_path, capsys) -> None:
    path = tmp_path / "labels.csv"
    path.write_text(
        "predicted_confidence,policy_v3_confidence,actual_outcome,"
        "recipe_id,recipe_version,eligibility_source\n"
        "0.0,0.8,false,sft_curated,1,explicit_accept\n"
        "1.0,1.0,true,sft_curated,1,explicit_accept\n"
    )

    code = main([str(path), "--bins", "2"])

    assert code == 0
    out = capsys.readouterr().out
    assert "policy comparison: v4 - v3" in out
    assert "brier_score_delta: -0.320000" in out


def test_partial_policy_v3_jsonl_column_is_a_clean_error(tmp_path, capsys) -> None:
    path = tmp_path / "labels.jsonl"
    rows = [
        {
            "predicted_confidence": 0.2,
            "actual_outcome": False,
            "recipe_id": "sft_curated",
            "recipe_version": 1,
            "eligibility_source": "explicit_accept",
        },
        {
            "predicted_confidence": 0.8,
            "policy_v3_confidence": 0.9,
            "actual_outcome": True,
            "recipe_id": "sft_curated",
            "recipe_version": 1,
            "eligibility_source": "explicit_accept",
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")

    code = main([str(path)])

    assert code == 2
    assert "policy_v3_confidence must be present on every JSONL row" in (
        capsys.readouterr().err
    )


def test_recipe_metadata_produces_separate_calibration_strata(tmp_path, capsys) -> None:
    path = tmp_path / "labels.csv"
    path.write_text(
        "predicted_confidence,actual_outcome,recipe_id,recipe_version,"
        "eligibility_source\n"
        "0.9,true,sft_curated,1,explicit_accept\n"
        "0.8,false,sft_verified,1,resolved_ci_pass\n"
    )

    code = main([str(path), "--bins", "2"])

    assert code == 0
    out = capsys.readouterr().out
    assert "recipe: sft_curated v1 eligibility_source=explicit_accept" in out
    assert "recipe: sft_verified v1 eligibility_source=resolved_ci_pass" in out
    assert out.count("rows: 1") == 2


def test_dpo_label_source_pair_is_one_calibration_stratum(tmp_path, capsys) -> None:
    path = tmp_path / "labels.csv"
    path.write_text(
        "predicted_confidence,actual_outcome,recipe_id,recipe_version,"
        "chosen_label_source,rejected_label_source\n"
        "0.9,true,dpo_outcome,1,resolved_ci_pass,resolved_ci_fail\n"
    )

    assert main([str(path)]) == 0
    out = capsys.readouterr().out
    assert (
        "recipe: dpo_outcome v1 chosen_label_source=resolved_ci_pass "
        "rejected_label_source=resolved_ci_fail"
    ) in out
