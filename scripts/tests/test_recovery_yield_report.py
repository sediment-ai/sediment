# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for sediment_api/reports/recovery_yield_report.py: the CLI wraps
``sediment_export.generate_recovery_yield_report`` and prints the recovery
pair yield plus skip-reason breakdown.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sediment_api.reports import recovery_yield_report
from sediment_core import CIOutcome, CIProvider, CIResult

ORG = "acme-corp"
REPO = "acme-corp/backend-service"
BASE = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


main = recovery_yield_report.main


def _report(database_url: str, mirror_path: str, *extra: str) -> int:
    return main(
        [
            "--org",
            ORG,
            "--database-url",
            database_url,
            "--mirror-path",
            mirror_path,
            *extra,
        ]
    )


def _outcome(commit_sha: str, result: CIResult, *, minutes: int) -> CIOutcome:
    return CIOutcome(
        org_id=ORG,
        provider=CIProvider.GITHUB_ACTIONS,
        run_id=f"run-{minutes}-{result}-{commit_sha[:8]}",
        repo=REPO,
        commit_sha=commit_sha,
        branch="main",
        result=result,
        workflow_name="tests",
        workflow_id="workflow-tests",
        captured_at=BASE + timedelta(minutes=minutes),
    )


def _seed_no_mirror_candidate(postgres_store_factory) -> str:
    database_url, store = postgres_store_factory()
    store.store_ci_outcome(_outcome("a" * 40, CIResult.FAILED, minutes=0))
    store.store_ci_outcome(_outcome("b" * 40, CIResult.PASSED, minutes=1))
    return database_url


def test_json_output_reports_recovery_yield_and_skips(
    tmp_path, postgres_store_factory, capsys
) -> None:
    database_url = _seed_no_mirror_candidate(postgres_store_factory)

    assert _report(database_url, str(tmp_path / "mirrors"), "--json") == 0

    row = json.loads(capsys.readouterr().out)
    assert row == {
        "total_failed_ci_runs": 1,
        "recovery_pairs": 0,
        "recovery_yield_rate": 0.0,
        "skipped": {"mirror_absent": 1},
        "diff_size_distribution": {
            "max_recovery_diff_lines": 200,
            "kept": {
                "count": 0,
                "min": None,
                "median": None,
                "p90": None,
                "max": None,
                "histogram": {},
            },
            "dropped": {
                "count": 0,
                "min": None,
                "median": None,
                "p90": None,
                "max": None,
                "histogram": {},
            },
        },
    }


def test_table_output_prints_counts_and_skip_breakdown(
    postgres_store_factory, capsys
) -> None:
    database_url = _seed_no_mirror_candidate(postgres_store_factory)

    assert _report(database_url, "missing") == 0

    out = capsys.readouterr().out
    assert "failed_ci_runs: 1" in out
    assert "recovery_pairs: 0" in out
    assert "mirror_absent: 1" in out
    assert "diff_size_distribution:" in out


def test_invalid_org_id_is_a_clean_error_not_a_traceback(
    tmp_path, postgres_store_factory, capsys
) -> None:
    database_url, _ = postgres_store_factory()

    code = main(
        [
            "--org",
            "not an org id!",
            "--database-url",
            database_url,
            "--mirror-path",
            str(tmp_path / "mirrors"),
        ]
    )
    assert code == 2
    assert "error:" in capsys.readouterr().err
