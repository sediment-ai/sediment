# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for sediment_api/reports/precision_report.py."""

from __future__ import annotations

import json

import pytest

from sediment_api.reports import precision_report

ORG = "acme-corp"


main = precision_report.main


def _report(manifest, database_url: str, mirror_path: str, *extra: str) -> int:
    return main(
        [
            "--org",
            ORG,
            "--manifest",
            str(manifest),
            "--database-url",
            database_url,
            "--mirror-path",
            mirror_path,
            *extra,
        ]
    )


def test_json_output_reports_separate_empty_source_counts(
    tmp_path, postgres_store_factory, capsys
) -> None:
    database_url, _ = postgres_store_factory()
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        '{"scenario":"negative","inference_call_id":"completion-negative",'
        '"expected_commit":null,"expected_file":null,'
        '"expected_attribution":null,"expected_reward_min":null,'
        '"notes":"must not correlate"}\n',
        encoding="utf-8",
    )

    assert _report(manifest, database_url, str(tmp_path / "mirrors"), "--json") == 0
    row = json.loads(capsys.readouterr().out)
    assert row["by_source"]["git_notes"]["default"]["true_negatives"] == 1
    assert row["by_source"]["jaccard"]["default"]["true_negatives"] == 1


def test_invalid_manifest_is_a_clean_error(
    tmp_path, postgres_store_factory, capsys
) -> None:
    database_url, _ = postgres_store_factory()
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text('{"scenario":"missing fields"}\n', encoding="utf-8")

    assert _report(manifest, database_url, str(tmp_path / "mirrors")) == 2
    assert "error:" in capsys.readouterr().err


@pytest.mark.parametrize("threshold", ["-0.01", "1.01"])
def test_default_threshold_outside_unit_interval_is_a_clean_error(
    tmp_path, postgres_store_factory, capsys, threshold
) -> None:
    database_url, _ = postgres_store_factory()
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text("", encoding="utf-8")

    assert (
        _report(
            manifest,
            database_url,
            str(tmp_path / "mirrors"),
            "--threshold",
            threshold,
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--threshold must be between 0.0 and 1.0" in captured.err
