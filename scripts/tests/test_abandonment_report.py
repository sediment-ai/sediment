# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for sediment_api/reports/abandonment_report.py: the CLI wraps
``sediment_derive.derive_abandonment`` and prints the abandoned sessions plus
the skip-reason breakdown.

The skip tally is the point of the report, so these assert on it: an operator
reading ``attribution_unavailable`` is reading a stamper-health signal, not a
developer-behaviour one.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sediment_api.reports import abandonment_report
from sediment_core import (
    AgentHarness,
    DeveloperDecision,
    InteractionMode,
)

ORG = "acme-corp"
BASE = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)

main = abandonment_report.main


def _decision(
    session_id: str, *, accepted: bool = True, days: int = 0
) -> DeveloperDecision:
    when = BASE + timedelta(days=days)
    return DeveloperDecision(
        org_id=ORG,
        session_id=session_id,
        user_id="dev",
        agent_harness=AgentHarness.CLAUDE_CODE,
        file_path="a.py",
        accepted=accepted,
        explicit=True,
        interaction_mode=InteractionMode.AGENT,
        occurred_at=when,
        captured_at=when,
    )


def _database(postgres_store_factory, decisions: list[DeveloperDecision]) -> str:
    database_url, store = postgres_store_factory()
    store.store_decisions(decisions)
    return database_url


def _argv(tmp_path: Path, database_url: str, *extra: str) -> list[str]:
    return [
        "--org",
        ORG,
        "--database-url",
        database_url,
        "--mirror-path",
        str(tmp_path / "mirrors"),
        *extra,
    ]


def test_table_reports_skips_when_nothing_could_be_attributed(
    tmp_path: Path, postgres_store_factory, capsys
) -> None:
    # No completions and no notes: the sessions are unjudgeable, and the
    # report must say so rather than reporting zero abandonment as if it
    # were a clean bill of health.
    database_url = _database(
        postgres_store_factory,
        [_decision("sess-a"), _decision("sess-b"), _decision("sess-clock", days=40)],
    )
    assert main(_argv(tmp_path, database_url)) == 0
    out = capsys.readouterr().out
    assert "abandoned_sessions: 0" in out
    assert "session_commit_unobserved: 3" in out
    # A reason that did not fire must not be listed at all.
    assert "no_accepted_decision" not in out


def test_reject_only_sessions_are_counted_separately(
    tmp_path: Path, postgres_store_factory, capsys
) -> None:
    database_url = _database(
        postgres_store_factory,
        [_decision("sess-rejected", accepted=False), _decision("sess-a", days=40)],
    )
    assert main(_argv(tmp_path, database_url)) == 0
    assert "no_accepted_decision: 1" in capsys.readouterr().out


def test_json_mode_is_machine_readable(
    tmp_path: Path, postgres_store_factory, capsys
) -> None:
    database_url = _database(
        postgres_store_factory,
        [_decision("sess-a"), _decision("sess-clock", days=40)],
    )
    assert main(_argv(tmp_path, database_url, "--json")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["org_id"] == ORG
    assert payload["grace_horizon_days"] == 14
    assert payload["policy_version"] == "6"
    assert payload["abandoned_sessions"] == []
    assert {item["status"] for item in payload["accepted_session_outcomes"]} == {
        "attribution_unavailable"
    }
    assert payload["skipped"]["session_commit_unobserved"] == 2
    # as_of is the fact set's newest stamp, not the wall clock.
    assert payload["as_of"].startswith("2026-08-24")


def test_grace_horizon_is_settable(
    tmp_path: Path, postgres_store_factory, capsys
) -> None:
    database_url = _database(
        postgres_store_factory,
        [_decision("sess-a"), _decision("sess-clock", days=40)],
    )
    assert (
        main(
            _argv(
                tmp_path,
                database_url,
                "--grace-horizon-days",
                "60",
                "--json",
            )
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["grace_horizon_days"] == 60


def test_degenerate_horizon_is_refused(
    tmp_path: Path, postgres_store_factory, capsys
) -> None:
    # A zero horizon would call every in-flight session abandoned; the policy
    # refuses it and the CLI exits non-zero rather than reporting nonsense.
    database_url = _database(postgres_store_factory, [_decision("sess-a")])
    assert main(_argv(tmp_path, database_url, "--grace-horizon-days", "0")) == 2
    assert "grace_horizon_days" in capsys.readouterr().err


def test_empty_store_reports_nothing(
    tmp_path: Path, postgres_store_factory, capsys
) -> None:
    database_url = _database(postgres_store_factory, [])
    assert main(_argv(tmp_path, database_url)) == 0
    out = capsys.readouterr().out
    assert "abandoned_sessions: 0" in out
    assert "none" in out
