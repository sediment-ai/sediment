# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared Confidence consumers retain qualified CI across repository names."""

from datetime import UTC, datetime

from sediment_core import CIOutcome
from sediment_derive import AttributionSource, Provenance
from sediment_derive.repository_identity import (
    build_repository_context,
    repository_identity_evidence_of,
    repository_identity_of,
)
from sediment_export.attributed_completions import AttributedCompletion
from sediment_export.label_confidence import ci_failed, ci_passed, resolve_ci_resolution


def test_confidence_selects_repository_identity_before_shared_sha():
    at = datetime(2026, 9, 12, tzinfo=UTC)
    good = CIOutcome(
        org_id="acme",
        repo="acme/new",
        commit_sha="a" * 40,
        branch="main",
        provider="github_actions",
        run_id="good",
        result="passed",
        captured_at=at,
        repository_provider="github",
        repository_host="github.com",
        repository_id="101",
    )
    bad = good.model_copy(
        update={
            "outcome_id": "other",
            "run_id": "bad",
            "result": "failed",
            "repository_id": "202",
        }
    )
    old = good.model_copy(
        update={"outcome_id": "old-name", "run_id": "old-name", "repo": "acme/old"}
    )
    context = build_repository_context(
        [repository_identity_evidence_of(row) for row in (good, bad, old)],
        (),
        "acme",
        as_of=at,
    )
    row = AttributedCompletion(
        org_id="acme",
        session_id="session",
        inference_call_id="call",
        repo="acme/old",
        commit_sha="a" * 40,
        file_path="file.py",
        similarity_score=1.0,
        attribution_source=AttributionSource.GIT_NOTES,
        decisions=[],
        ci_outcomes=[good, bad],
        provenance=Provenance("4", 0),
        split="train",
        repository_identity=repository_identity_of(good),
    )
    resolution = resolve_ci_resolution(row, repository_context=context)
    assert resolution.source_outcome_ids == (good.outcome_id,)
    assert ci_passed(row, repository_context=context)
    assert not ci_failed(row, repository_context=context)
    assert resolve_ci_resolution(row) is None
