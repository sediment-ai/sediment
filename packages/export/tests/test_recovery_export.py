# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Recovery row projection tests — real ``RecoverySample``/``InferenceCall``
instances (per AGENTS.md: never mocked). ``project_recovery`` is a pure
function of pairs + a completion lookup, no git or ``FactStore`` needed:
``RecoverySample`` already carries its diff text as a plain string.
"""

from __future__ import annotations

import pytest

from sediment_core import InferenceCall
from sediment_derive import Provenance, RecoverySample, split_of

from sediment_export import (
    ExportRow,
    project_recovery,
    recovery_to_export_rows,
)
from export_factories import inference_call, message

ORG = "acme-corp"
REPO = "acme-corp/backend-service"
BRANCH = "main"
MODEL = "claude-sonnet-5"


def _completion(inference_call_id: str, *, session_id: str) -> InferenceCall:
    return inference_call(
        inference_call_id=inference_call_id,
        org_id=ORG,
        session_id=session_id,
        model=MODEL,
        input_messages=[message("user", "fix the build")],
        output="applied the fix",
    )


def _sample(
    *,
    failed_inference_call_ids: list[str] = (),
    fixed_inference_call_ids: list[str] = (),
    workflow_name: str = "tests",
    workflow_path: str | None = ".github/workflows/tests.yml",
) -> RecoverySample:
    return RecoverySample(
        org_id=ORG,
        repo=REPO,
        branch=BRANCH,
        workflow_name=workflow_name,
        workflow_path=workflow_path,
        failed_commit_sha="a" * 40,
        fixed_commit_sha="b" * 40,
        failed_outcome_id="outcome-failed-1",
        fixed_outcome_id="outcome-fixed-1",
        recovery_diff="diff --git a/f.py b/f.py\n@@ -1 +1 @@\n-bad\n+good\n",
        failed_inference_call_ids=list(failed_inference_call_ids),
        fixed_inference_call_ids=list(fixed_inference_call_ids),
        provenance=Provenance(policy_version="1", quarantine_revision=0),
    )


def test_row_carries_every_recovery_sample_field() -> None:
    sample = _sample(
        failed_inference_call_ids=["c-1"], fixed_inference_call_ids=["c-2"]
    )
    out = project_recovery(
        [sample],
        {
            "c-1": _completion("c-1", session_id="sess-train"),
            "c-2": _completion("c-2", session_id="sess-train-2"),
        },
    )
    assert len(out.rows) == 1
    row = out.rows[0]
    assert row.org_id == ORG
    assert row.repo == REPO
    assert row.branch == BRANCH
    assert row.workflow_name == "tests"
    assert row.workflow_path == ".github/workflows/tests.yml"
    assert row.failed_commit_sha == "a" * 40
    assert row.fixed_commit_sha == "b" * 40
    assert row.failed_outcome_id == "outcome-failed-1"
    assert row.fixed_outcome_id == "outcome-fixed-1"
    assert row.recovery_diff == sample.recovery_diff
    assert row.failed_inference_call_ids == ["c-1"]
    assert row.fixed_inference_call_ids == ["c-2"]
    assert row.provenance == Provenance(policy_version="1", quarantine_revision=0)

    assert out.rows[0].failed_attribution_evidence == ()
    assert out.rows[0].fixed_attribution_evidence == ()


def test_every_pair_becomes_exactly_one_row_no_skips() -> None:
    samples = [_sample(failed_inference_call_ids=[]) for _ in range(3)]
    out = project_recovery(samples, {})
    assert len(out.rows) == 3
    assert dict(out.skipped) == {}


def test_no_completions_defaults_to_train() -> None:
    out = project_recovery([_sample(failed_inference_call_ids=[])], {})
    assert out.rows[0].split == "train"


def test_unresolvable_inference_call_ids_default_to_train_and_are_counted() -> None:
    # inference_call_id present on the sample but absent from the lookup: the
    # enrichment is best-effort (module docstring), never an error — but the
    # degradation is counted, one tally entry covering both sides.
    out = project_recovery(
        [
            _sample(
                failed_inference_call_ids=["ghost"],
                fixed_inference_call_ids=["ghost-2"],
            )
        ],
        {},
    )
    assert out.rows[0].split == "train"
    assert dict(out.skipped) == {
        "inference_call_not_found": 2,
        "attribution_evidence_absent": 2,
    }


def test_eval_wins_across_resolvable_completions() -> None:
    # What this asserts is the *any*-wins combination rule, not a specific
    # session's score, so pick real sessions that straddle the split at a
    # fixed fraction rather than hand-computing a sha256 boundary.
    fraction = 0.5
    splits = {sid: split_of(sid, fraction) for sid in ("sess-a", "sess-b")}
    assert set(splits.values()) == {"train", "eval"}, (
        "fixture sessions must straddle the split at fraction=0.5"
    )
    eval_session = next(sid for sid, s in splits.items() if s == "eval")
    train_session = next(sid for sid, s in splits.items() if s == "train")

    completions = {
        "c-train": _completion("c-train", session_id=train_session),
        "c-eval": _completion("c-eval", session_id=eval_session),
    }
    out = project_recovery(
        [_sample(failed_inference_call_ids=["c-train", "c-eval"])],
        completions,
        eval_fraction=fraction,
    )
    assert out.rows[0].split == "eval"


def test_eval_wins_from_the_fixed_side_alone() -> None:
    # The fix's attributed sessions are holdout evidence too: a resolvable
    # eval session on the fixed side makes the row eval even with an empty
    # mistake side.
    fraction = 0.5
    splits = {sid: split_of(sid, fraction) for sid in ("sess-a", "sess-b")}
    assert set(splits.values()) == {"train", "eval"}, (
        "fixture sessions must straddle the split at fraction=0.5"
    )
    eval_session = next(sid for sid, s in splits.items() if s == "eval")

    out = project_recovery(
        [_sample(fixed_inference_call_ids=["c-fix"])],
        {"c-fix": _completion("c-fix", session_id=eval_session)},
        eval_fraction=fraction,
    )
    assert out.rows[0].split == "eval"


def test_eval_wins_across_sides() -> None:
    # train-side mistake evidence + eval-side fix evidence -> eval.
    fraction = 0.5
    splits = {sid: split_of(sid, fraction) for sid in ("sess-a", "sess-b")}
    assert set(splits.values()) == {"train", "eval"}, (
        "fixture sessions must straddle the split at fraction=0.5"
    )
    eval_session = next(sid for sid, s in splits.items() if s == "eval")
    train_session = next(sid for sid, s in splits.items() if s == "train")

    completions = {
        "c-train": _completion("c-train", session_id=train_session),
        "c-eval": _completion("c-eval", session_id=eval_session),
    }
    out = project_recovery(
        [
            _sample(
                failed_inference_call_ids=["c-train"],
                fixed_inference_call_ids=["c-eval"],
            )
        ],
        completions,
        eval_fraction=fraction,
    )
    assert out.rows[0].split == "eval"


def test_all_train_sessions_stay_train() -> None:
    completions = {
        "c-1": _completion("c-1", session_id="sess-1"),
        "c-2": _completion("c-2", session_id="sess-2"),
    }
    out = project_recovery(
        [_sample(failed_inference_call_ids=["c-1"], fixed_inference_call_ids=["c-2"])],
        completions,
        eval_fraction=0.0,
    )
    assert out.rows[0].split == "train"
    assert dict(out.skipped) == {"attribution_evidence_absent": 2}


def test_eval_fraction_out_of_range_raises() -> None:
    with pytest.raises(ValueError):
        project_recovery([], {}, eval_fraction=0.6)


def test_to_export_rows_carries_split_and_body() -> None:
    out = project_recovery(
        [_sample(failed_inference_call_ids=[])], {}, eval_fraction=0.0
    )
    export_rows = recovery_to_export_rows(out.rows)
    assert len(export_rows) == 1
    row = export_rows[0]
    assert isinstance(row, ExportRow)
    assert row.split == "train"
    assert row.body["repo"] == REPO
    assert row.body["failed_inference_call_ids"] == []
    assert row.body["fixed_inference_call_ids"] == []


def test_recovery_preserves_symmetric_inferred_enrichment_and_counts_gaps():
    from dataclasses import replace
    from sediment_derive import AttributionSource
    from sediment_derive.recovery import RecoveryAttributionEvidence

    failed = RecoveryAttributionEvidence(
        "failed",
        "failed-session",
        (AttributionSource.JACCARD,),
        ("failed-observation",),
    )
    fixed = RecoveryAttributionEvidence(
        "fixed", "fixed-session", (AttributionSource.GIT_NOTES,), ()
    )
    sample = replace(
        _sample(
            failed_inference_call_ids=["failed", "gap"],
            fixed_inference_call_ids=["fixed"],
        ),
        failed_attribution_evidence=(failed,),
        fixed_attribution_evidence=(fixed,),
    )
    calls = {
        cid: _completion(cid, session_id=sid)
        for cid, sid in (
            ("failed", "failed-session"),
            ("fixed", "fixed-session"),
            ("gap", "gap-session"),
        )
    }
    out = project_recovery([sample], calls)
    [row] = out.rows
    assert (row.recipe_id, row.recipe_version) == ("recovery_ci", 1)
    assert row.failed_attribution_evidence == (failed,)
    assert row.fixed_attribution_evidence == (fixed,)
    assert out.skipped == {"attribution_evidence_absent": 1}


@pytest.mark.parametrize("field", ["recovery_diff", "workflow_name"])
def test_recovery_representation_declines_whole_pair_once(field):
    from dataclasses import replace

    sample = replace(_sample(), **{field: "\ud800"})
    out = project_recovery([sample], {})
    assert out.rows == []
    assert dict(out.skipped) == {"unrepresentable_unicode": 1}


def test_recovery_omitted_call_content_does_not_decline_pair():
    call = _completion("c", session_id="s").model_copy(
        update={"output_messages": [message("assistant", "\ud800")]}
    )
    out = project_recovery([_sample(failed_inference_call_ids=["c"])], {"c": call})
    assert len(out.rows) == 1
    assert dict(out.skipped) == {"attribution_evidence_absent": 1}


def test_recovery_row_keeps_common_repository_identity():
    from dataclasses import replace
    from sediment_derive.repository_identity import RepositoryIdentity

    identity = RepositoryIdentity("github", "github.com", "101")
    sample = replace(_sample(), repository_identity=identity)
    [row] = project_recovery([sample], {}).rows
    assert row.repository_identity == identity
    assert row.failed_outcome_id == sample.failed_outcome_id
    assert row.fixed_outcome_id == sample.fixed_outcome_id
