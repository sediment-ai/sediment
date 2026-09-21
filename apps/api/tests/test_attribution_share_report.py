# SPDX-License-Identifier: AGPL-3.0-or-later
"""Operator rendering tests for the canonical attribution-share contract."""

from datetime import UTC, datetime

from sediment_api.reports.attribution_share_report import _print_row
from sediment_derive import Provenance, RepoAttributionShare


def test_text_report_renders_canonical_git_notes_fields(capsys) -> None:
    row = RepoAttributionShare(
        org_id="acme",
        repo="acme/service",
        window_start=datetime(2026, 8, 1, tzinfo=UTC),
        window_end=datetime(2026, 8, 8, tzinfo=UTC),
        agent_plausible_commits=4,
        git_notes_attributed=3,
        jaccard_attributed=1,
        unattributed=0,
        git_notes_share=0.75,
        git_notes_share_ci=(0.3, 0.95),
        provenance=Provenance(policy_version="1", quarantine_revision=0),
    )

    _print_row(row)

    assert capsys.readouterr().out == (
        "  acme/service: git_notes_share=0.7500 ci=(0.3000, 0.9500) "
        "agent_plausible_commits=4 git_notes=3 jaccard=1 unattributed=0\n"
    )


def test_both_text_report_paths_distinguish_same_label_lifetimes(capsys):
    from sediment_api.reports.model_report import _print_attribution_share
    from sediment_derive import RepositoryIdentity

    rows = [
        RepoAttributionShare(
            org_id="acme",
            repo="acme/service",
            repository_identity=RepositoryIdentity("github", "github.com", identifier),
            window_start=datetime(2026, 8, 1, tzinfo=UTC),
            window_end=datetime(2026, 8, 8, tzinfo=UTC),
            agent_plausible_commits=1,
            git_notes_attributed=1,
            jaccard_attributed=0,
            unattributed=0,
            git_notes_share=1.0,
            git_notes_share_ci=(0.2, 1.0),
            provenance=Provenance("test", 0),
        )
        for identifier in ("101", "202")
    ]
    _print_attribution_share(rows, [])
    rendered = capsys.readouterr().out
    assert all(
        f"acme/service [github/github.com/{identifier}]" in rendered
        for identifier in ("101", "202")
    )
    for row in rows:
        _print_row(row)
    rendered = capsys.readouterr().out
    assert all(
        f"acme/service [github/github.com/{identifier}]" in rendered
        for identifier in ("101", "202")
    )
