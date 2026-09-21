# SPDX-License-Identifier: AGPL-3.0-or-later
"""CI coverage for the illustrative ground-truth precision manifest.

The manifest is synthetic and hand-constructed to prove the reporting
mechanism. The literal counts below are hand-verified and are not a real-world
precision claim.
"""

from __future__ import annotations

from pathlib import Path
from dataclasses import asdict, replace
import json

import pytest
from sediment_derive import Provenance
from sediment_derive.attribution import AttributionSource, Attribution
from sediment_derive.precision_report import (
    DEFAULT_THRESHOLD,
    evaluate_attribution_precision,
    load_ground_truth_manifest,
    GroundTruthRow,
)
from sediment_derive import (
    IdentifiedRepositoryKey,
    LegacyRepositoryKey,
    RepositoryIdentity,
)
from sediment_core import ForgeProvider

FIXTURE = (
    Path(__file__).parent / "fixtures" / "ground_truth" / "illustrative_manifest.jsonl"
)
ORG = "acme-corp"
REPO = "acme-corp/backend-service"


def test_load_ground_truth_manifest_jsonl() -> None:
    rows = load_ground_truth_manifest(FIXTURE)

    assert len(rows) == 7
    assert rows[0].scenario == "notes_true_positive"
    assert rows[0].expected_attribution == AttributionSource.GIT_NOTES
    assert rows[3].scenario == "negative_respected"
    assert rows[3].expected_attribution is None
    assert rows[3].expected_commit is None
    assert rows[3].expected_file is None


def test_ground_truth_precision_report_uses_hand_verified_counts() -> None:
    # Hand-verified at threshold 0.7:
    # notes: TP=2 (two exact notes matches), FP=1 (wrong file), FN=1
    # (expected ledger.py missing), TN=4 (all non-notes rows have no notes
    # prediction).
    # jaccard: TP=1 (exact taxes.py), FP=1 (negative row attributed), FN=1
    # (discounts.py missing), TN=4 (all other non-jaccard rows have no jaccard
    # prediction).
    report = evaluate_attribution_precision(
        _illustrative_attributions(), load_ground_truth_manifest(FIXTURE)
    )

    notes = report.by_source[AttributionSource.GIT_NOTES].default
    assert notes.scorer_version == "attribution-git_notes"
    assert notes.threshold == DEFAULT_THRESHOLD
    assert notes.true_positives == 2
    assert notes.false_positives == 1
    assert notes.false_negatives == 1
    assert notes.true_negatives == 4
    assert notes.precision == pytest.approx(2 / 3)
    assert notes.recall == pytest.approx(2 / 3)
    assert (
        report.by_source[AttributionSource.GIT_NOTES].skipped_unlabelled_predictions
        == 0
    )

    jaccard = report.by_source[AttributionSource.JACCARD].default
    assert jaccard.scorer_version == "attribution-jaccard"
    assert jaccard.threshold == DEFAULT_THRESHOLD
    assert jaccard.true_positives == 1
    assert jaccard.false_positives == 1
    assert jaccard.false_negatives == 1
    assert jaccard.true_negatives == 4
    assert jaccard.precision == pytest.approx(1 / 2)
    assert jaccard.recall == pytest.approx(1 / 2)
    assert (
        report.by_source[AttributionSource.JACCARD].skipped_unlabelled_predictions == 1
    )


def test_negative_row_wrongly_attributed_counts_as_false_positive(tmp_path) -> None:
    manifest_path = tmp_path / "manifest.jsonl"
    manifest_path.write_text(
        '{"scenario":"negative","inference_call_id":"completion-negative",'
        '"expected_commit":null,"expected_file":null,'
        '"expected_attribution":null,"expected_reward_min":null,'
        '"notes":"must not correlate"}\n',
        encoding="utf-8",
    )
    attribution = _attribution(
        inference_call_id="completion-negative",
        commit_sha="eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        file_path="billing/bait.py",
        score=0.88,
        attribution_source=AttributionSource.JACCARD,
    )

    report = evaluate_attribution_precision(
        [attribution], load_ground_truth_manifest(manifest_path)
    )

    jaccard = report.by_source[AttributionSource.JACCARD].default
    assert jaccard.true_positives == 0
    assert jaccard.false_positives == 1
    assert jaccard.false_negatives == 0
    assert jaccard.true_negatives == 0


def test_notes_and_jaccard_are_independent_not_pooled() -> None:
    report = evaluate_attribution_precision(
        _illustrative_attributions(), load_ground_truth_manifest(FIXTURE)
    )

    notes = report.by_source[AttributionSource.GIT_NOTES].default
    jaccard = report.by_source[AttributionSource.JACCARD].default

    assert notes.true_positives == 2
    assert jaccard.true_positives == 1
    assert notes.precision == pytest.approx(2 / 3)
    assert jaccard.precision == pytest.approx(1 / 2)


def test_threshold_sweep_reuses_precision_harness_shape() -> None:
    thresholds = [0.7, 0.9]
    report = evaluate_attribution_precision(
        _illustrative_attributions(),
        load_ground_truth_manifest(FIXTURE),
        sweep_threshold_values=thresholds,
    )

    notes_sweep = report.by_source[AttributionSource.GIT_NOTES].sweep
    assert [row.threshold for row in notes_sweep] == thresholds
    assert notes_sweep[0].true_positives == 2
    assert notes_sweep[1].true_positives == 1
    assert notes_sweep[1].false_negatives == 2

    jaccard_sweep = report.by_source[AttributionSource.JACCARD].sweep
    assert [row.threshold for row in jaccard_sweep] == thresholds
    assert jaccard_sweep[0].false_positives == 1
    assert jaccard_sweep[1].false_positives == 0


@pytest.mark.parametrize("other_repository", ["fork", "host", "tenant", "legacy"])
def test_precision_qualified_repository_prevents_shared_sha_false_positive(
    tmp_path, other_repository
):
    identity = RepositoryIdentity(ForgeProvider.GITHUB, "github.com", "101")
    key = IdentifiedRepositoryKey(ORG, identity)
    truth = GroundTruthRow(
        "identified",
        "call",
        "a" * 40,
        "code.py",
        AttributionSource.GIT_NOTES,
        None,
        "synthetic",
        expected_repository=key,
    )
    matching = replace(
        _attribution(
            inference_call_id="call",
            commit_sha="a" * 40,
            file_path="code.py",
            score=0.99,
            attribution_source=AttributionSource.GIT_NOTES,
        ),
        repository_identity=identity,
        repo="acme-corp/renamed",
    )
    fork = {
        "fork": replace(
            matching, repository_identity=replace(identity, repository_id="202")
        ),
        "host": replace(
            matching, repository_identity=replace(identity, host="forge.example")
        ),
        "tenant": replace(matching, org_id="another-org"),
        "legacy": replace(matching, repository_identity=None),
    }[other_repository]
    path = tmp_path / "identified.jsonl"
    path.write_text(json.dumps(asdict(truth)) + "\n")
    assert load_ground_truth_manifest(path) == [truth]
    reports = []
    for predictions in ([matching, fork], [fork, matching]):
        report = evaluate_attribution_precision(predictions, [truth])
        notes = report.by_source[AttributionSource.GIT_NOTES].default
        assert (notes.true_positives, notes.false_positives, notes.false_negatives) == (
            1,
            1,
            0,
        )
        reports.append(report)
    assert reports[0] == reports[1]
    wrong_only = (
        evaluate_attribution_precision([fork], [truth])
        .by_source[AttributionSource.GIT_NOTES]
        .default
    )
    assert (
        wrong_only.true_positives,
        wrong_only.false_positives,
        wrong_only.false_negatives,
    ) == (0, 1, 1)


@pytest.mark.parametrize("kind", ["identified", "multiple_legacy", "absent"])
def test_unqualified_precision_truth_declines_unproved_repository_scope(kind):
    truth = GroundTruthRow(
        "unqualified",
        "call",
        "a" * 40,
        "code.py",
        AttributionSource.GIT_NOTES,
        None,
        "synthetic",
    )
    prediction = _attribution(
        inference_call_id="call",
        commit_sha="a" * 40,
        file_path="code.py",
        score=0.99,
        attribution_source=AttributionSource.GIT_NOTES,
    )
    predictions = {
        "identified": [
            replace(
                prediction,
                repository_identity=RepositoryIdentity(
                    ForgeProvider.GITHUB, "github.com", "101"
                ),
            )
        ],
        "multiple_legacy": [prediction, replace(prediction, repo="acme-corp/fork")],
        "absent": [],
    }[kind]
    source = evaluate_attribution_precision(predictions, [truth]).by_source[
        AttributionSource.GIT_NOTES
    ]
    assert source.skipped_repository_ground_truth == 1
    assert source.skipped_repository_predictions == len(predictions)
    assert (
        source.default.true_positives
        == source.default.false_positives
        == source.default.false_negatives
        == 0
    )


def test_precision_legacy_qualified_manifest_validation(tmp_path):
    truth = GroundTruthRow(
        "legacy",
        "call",
        "a" * 40,
        "code.py",
        AttributionSource.GIT_NOTES,
        None,
        "synthetic",
        expected_repository=LegacyRepositoryKey(ORG, REPO),
    )
    path = tmp_path / "legacy.jsonl"
    path.write_text(json.dumps(asdict(truth)) + "\n")
    assert load_ground_truth_manifest(path) == [truth]
    raw = asdict(truth)
    raw["expected_repository"]["identity"] = {
        "provider": "github",
        "host": "github.com",
        "repository_id": "101",
    }
    path.write_text(json.dumps(raw) + "\n")
    with pytest.raises(ValueError, match="expected_repository"):
        load_ground_truth_manifest(path)


def test_identified_negative_control_remains_a_false_positive():
    truth = GroundTruthRow("negative", "call", None, None, None, None, "synthetic")
    prediction = replace(
        _attribution(
            inference_call_id="call",
            commit_sha="a" * 40,
            file_path="code.py",
            score=0.99,
            attribution_source=AttributionSource.GIT_NOTES,
        ),
        repository_identity=RepositoryIdentity(
            ForgeProvider.GITHUB, "github.com", "101"
        ),
    )
    source = evaluate_attribution_precision([prediction], [truth]).by_source[
        AttributionSource.GIT_NOTES
    ]
    assert source.default.false_positives == 1
    assert (
        source.skipped_repository_predictions
        == source.skipped_repository_ground_truth
        == 0
    )


def _illustrative_attributions() -> list[Attribution]:
    return [
        _attribution(
            inference_call_id="completion-notes-tp-1",
            commit_sha="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            file_path="billing/invoices.py",
            score=0.99,
            attribution_source=AttributionSource.GIT_NOTES,
        ),
        _attribution(
            inference_call_id="completion-jaccard-tp",
            commit_sha="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            file_path="billing/taxes.py",
            score=0.91,
            attribution_source=AttributionSource.JACCARD,
        ),
        _attribution(
            inference_call_id="completion-notes-wrong-file",
            commit_sha="cccccccccccccccccccccccccccccccccccccccc",
            file_path="billing/wrong.py",
            score=0.95,
            attribution_source=AttributionSource.GIT_NOTES,
        ),
        _attribution(
            inference_call_id="completion-negative-wrong",
            commit_sha="eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
            file_path="billing/bait.py",
            score=0.82,
            attribution_source=AttributionSource.JACCARD,
        ),
        _attribution(
            inference_call_id="completion-notes-tp-2",
            commit_sha="ffffffffffffffffffffffffffffffffffffffff",
            file_path="billing/payments.py",
            score=0.88,
            attribution_source=AttributionSource.GIT_NOTES,
        ),
        _attribution(
            inference_call_id="completion-unlabelled",
            commit_sha="9999999999999999999999999999999999999999",
            file_path="billing/unlabelled.py",
            score=0.99,
            attribution_source=AttributionSource.JACCARD,
        ),
    ]


def _attribution(
    *,
    inference_call_id: str,
    commit_sha: str,
    file_path: str,
    score: float,
    attribution_source: AttributionSource,
) -> Attribution:
    return Attribution(
        org_id=ORG,
        repo=REPO,
        commit_sha=commit_sha,
        file_path=file_path,
        inference_call_id=inference_call_id,
        session_id=f"session-{inference_call_id}",
        similarity_score=score,
        attribution_source=attribution_source,
        provenance=Provenance(policy_version="test", quarantine_revision=0),
    )
