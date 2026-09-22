# SPDX-License-Identifier: AGPL-3.0-or-later
"""Exact commit reads retain all visible repository lifetimes before pagination."""

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta

import pytest

from sediment_core import (
    CIOutcome,
    CIProvider,
    CIResult,
    FactTable,
    SessionCommitObservation,
)

BOUNDARY = datetime(2026, 9, 22, tzinfo=UTC)
SHA = "a" * 40


@pytest.mark.parametrize("snapshot_read", [False, True])
@pytest.mark.parametrize("kind", ["observations", "ci"])
def test_exact_commit_filter_precedes_limits_and_preserves_visibility(
    postgres_store, snapshot_read, kind
):
    for identifier, overrides in (
        ("first", {"repo": "acme/a"}),
        ("second", {"repo": "acme/b"}),
        ("unrelated", {"commit_sha": "b" * 40}),
        ("foreign", {"org_id": "elsewhere"}),
        ("future", {"captured_at": BOUNDARY + timedelta(microseconds=1)}),
        ("quarantined", {}),
    ):
        common = dict(
            org_id="acme", repo="acme/repo", commit_sha=SHA, captured_at=BOUNDARY
        )
        common.update(overrides)
        if kind == "observations":
            fact = SessionCommitObservation(
                observation_id=identifier,
                session_id=identifier,
                source_push_id="push",
                **common,
            )
            postgres_store.store_session_commit_observation(fact)
        else:
            fact = CIOutcome(
                outcome_id=identifier,
                provider=CIProvider.GITHUB_ACTIONS,
                run_id=identifier,
                result=CIResult.PASSED,
                branch="main",
                raw={"evidence": identifier},
                **common,
            )
            postgres_store.store_ci_outcome(fact)
    postgres_store.quarantine_fact(
        "acme",
        FactTable.SESSION_COMMIT_OBSERVATIONS
        if kind == "observations"
        else FactTable.CI_OUTCOMES,
        "quarantined",
        reason="excluded",
    )
    with (
        postgres_store.read_snapshot()
        if snapshot_read
        else nullcontext(postgres_store) as reader
    ):
        method = (
            reader.read_session_commit_observations
            if kind == "observations"
            else reader.read_ci_outcomes
        )
        bounds = {"as_of" if kind == "observations" else "captured_through": BOUNDARY}
        expected = [fact for fact in method("acme", **bounds) if fact.commit_sha == SHA]
        assert method("acme", commit_sha=SHA.upper(), limit=2, **bounds) == expected
        assert method("acme", commit_sha="c" * 40, **bounds) == []
        with pytest.raises(ValueError):
            method("acme", commit_sha="invalid", **bounds)
