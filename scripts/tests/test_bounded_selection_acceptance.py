# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bounded K/J1 selection against the real API and PostgreSQL."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bounded_selection_acceptance as acceptance  # noqa: E402


def test_bounded_selection_uses_real_grants_quarantine_and_reads(postgres_admin_url):
    result = acceptance.run(postgres_admin_url)
    assert result["passed"], result["checks"]
    for task in result["results"].values():
        assert task["j1_decision"] == "jev"
        assert task["rule_delivered_by_j1"] and task["rule_delivered_by_k"]
        assert task["j1_bytes"] <= 8192 and task["k_bytes"] <= 8192
