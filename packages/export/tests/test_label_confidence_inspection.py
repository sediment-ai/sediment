# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Label-confidence inspection tests — stratified sampling and the per-row
factor breakdown. Real ``AttributedCompletion``/``InferenceCall``/``DeveloperDecision``/
``CIOutcome`` instances (per AGENTS.md: never mocked), same isolation level
as ``test_label_confidence.py``/``test_outcome_report.py``: ``build_label_confidence_inspection``
is a pure aggregation over already-assembled facts, no store or real git
needed.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from sediment_core import (
    CIOutcome,
    CIProvider,
    CIResult,
    AgentHarness,
    DeveloperDecision,
    InteractionMode,
    GatewayProvider,
    InferenceCall,
    InferenceMessage,
    TextPart,
    ToolCallPart,
)
from sediment_derive import AttributionSource, Provenance, SessionAbandonment

from sediment_export import (
    LabelConfidencePolicy,
    AttributedCompletion,
    build_label_confidence_inspection,
    resolve_confidence_breakdown,
    stratified_sample,
)
from sediment_export.label_confidence_inspection import _cell_key
from export_factories import inference_call, message

ORG = "acme-corp"
REPO = "acme-corp/backend-service"
NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)


def _sha(seed: str) -> str:
    """A valid full-length commit sha, deterministic per seed name."""
    return hashlib.sha1(seed.encode()).hexdigest()


def _completion(inference_call_id: str, text: str = "done") -> InferenceCall:
    return inference_call(
        inference_call_id=inference_call_id,
        org_id=ORG,
        session_id="sess-1",
        model="claude-sonnet-5",
        input_messages=[message("user", "do it")],
        output=text,
        observed_at=NOW,
    )


def _decision(
    *,
    accepted: bool,
    explicit: bool,
    agent_harness: AgentHarness = AgentHarness.CLAUDE_CODE,
) -> DeveloperDecision:
    return DeveloperDecision(
        org_id=ORG,
        session_id="sess-1",
        user_id="dev",
        agent_harness=agent_harness,
        file_path="a.py",
        accepted=accepted,
        explicit=explicit,
        interaction_mode=InteractionMode.AGENT,
        occurred_at=NOW,
    )


def _ci(
    result: CIResult, *, commit_sha: str = _sha("sha1"), **over: object
) -> CIOutcome:
    base = dict(
        org_id=ORG,
        provider=CIProvider.GITHUB_ACTIONS,
        run_id=f"run/{commit_sha}/{result}",
        repo=REPO,
        commit_sha=commit_sha,
        branch="main",
        result=result,
        run_url=f"run/{commit_sha}",
    )
    base.update(over)
    return CIOutcome(**base)


def _attributed_completion(
    inference_call_id: str,
    *,
    decisions: list[DeveloperDecision] | None = None,
    ci_outcomes: list[CIOutcome] | None = None,
    attribution_source: AttributionSource = AttributionSource.GIT_NOTES,
    similarity_score: float = 0.9,
    commit_sha: str = _sha("sha1"),
) -> AttributedCompletion:
    return AttributedCompletion(
        org_id=ORG,
        session_id="sess-1",
        inference_call_id=inference_call_id,
        repo=REPO,
        commit_sha=commit_sha,
        file_path="a.py",
        similarity_score=similarity_score,
        attribution_source=attribution_source,
        decisions=decisions or [],
        ci_outcomes=ci_outcomes or [],
        provenance=Provenance(policy_version="1", quarantine_revision=0),
        split="train",
    )


def _abandoned_attributed_completion(inference_call_id: str) -> AttributedCompletion:
    return AttributedCompletion(
        org_id=ORG,
        session_id="sess-1",
        inference_call_id=inference_call_id,
        repo=None,
        commit_sha=None,
        file_path=None,
        similarity_score=None,
        attribution_source=None,
        decisions=[_decision(accepted=True, explicit=True)],
        ci_outcomes=[],
        provenance=Provenance(policy_version="3", quarantine_revision=0),
        split="train",
        abandonment=SessionAbandonment(
            org_id=ORG,
            session_id="sess-1",
            accepted_decisions=1,
            explicit_accepted_decisions=1,
            last_decision_at=NOW,
            as_of=NOW,
            provenance=Provenance(policy_version="2", quarantine_revision=0),
        ),
    )


# One attributed completion per real decision_branch, each with a distinct commit_sha so CI
# outcomes don't cross-contaminate (CI is keyed by (repo, commit_sha)).
_BRANCH_ATTRIBUTED_COMPLETIONS = {
    "no_decision": _attributed_completion("c-none", commit_sha=_sha("sha-none")),
    "explicit_reject": _attributed_completion(
        "c-exp-rej",
        decisions=[_decision(accepted=False, explicit=True)],
        commit_sha=_sha("sha-exp-rej"),
    ),
    "explicit_accept": _attributed_completion(
        "c-exp-acc",
        decisions=[_decision(accepted=True, explicit=True)],
        commit_sha=_sha("sha-exp-acc"),
    ),
    "implicit_reject": _attributed_completion(
        "c-imp-rej",
        decisions=[_decision(accepted=False, explicit=False)],
        commit_sha=_sha("sha-imp-rej"),
    ),
    "implicit_accept_codex_neutral": _attributed_completion(
        "c-codex",
        decisions=[
            _decision(accepted=True, explicit=False, agent_harness=AgentHarness.CODEX)
        ],
        commit_sha=_sha("sha-codex"),
    ),
    "implicit_accept": _attributed_completion(
        "c-imp-acc",
        decisions=[_decision(accepted=True, explicit=False)],
        commit_sha=_sha("sha-imp-acc"),
    ),
}


def test_stratified_sample_covers_every_branch_when_n_is_large_enough() -> None:
    attributed_completions = list(_BRANCH_ATTRIBUTED_COMPLETIONS.values())
    sample = stratified_sample(attributed_completions, n=len(attributed_completions))
    assert len(sample) == len(attributed_completions)
    assert {t.inference_call_id for t in sample} == {
        t.inference_call_id for t in attributed_completions
    }


def test_stratified_sample_respects_n_and_still_spreads_across_cells() -> None:
    attributed_completions = list(_BRANCH_ATTRIBUTED_COMPLETIONS.values())
    sample = stratified_sample(attributed_completions, n=3)
    assert len(sample) == 3
    # Each cell here holds exactly one attributed completion, so 3 distinct cells (and
    # hence 3 distinct branches) must be represented in a 3-sample draw.
    branches = {_cell_key(t)[0] for t in sample}
    assert len(branches) == 3


def test_stratified_sample_is_deterministic() -> None:
    attributed_completions = list(_BRANCH_ATTRIBUTED_COMPLETIONS.values())
    first = stratified_sample(attributed_completions, n=4)
    second = stratified_sample(attributed_completions, n=4)
    assert [t.inference_call_id for t in first] == [t.inference_call_id for t in second]


def test_stratified_sample_handles_n_larger_than_population() -> None:
    attributed_completions = list(_BRANCH_ATTRIBUTED_COMPLETIONS.values())
    sample = stratified_sample(attributed_completions, n=1000)
    assert len(sample) == len(attributed_completions)


def test_stratified_sample_empty_input_or_nonpositive_n() -> None:
    assert stratified_sample([], n=50) == []
    assert stratified_sample(list(_BRANCH_ATTRIBUTED_COMPLETIONS.values()), n=0) == []
    assert stratified_sample(list(_BRANCH_ATTRIBUTED_COMPLETIONS.values()), n=-1) == []


def test_stratified_sample_spreads_across_ci_and_attribution_cells_too() -> None:
    # Same decision branch, but different CI/attribution cells — a good n
    # should still spread across them rather than piling into one cell.
    attributed_completions = [
        _attributed_completion(
            "c1",
            decisions=[_decision(accepted=True, explicit=True)],
            ci_outcomes=[_ci(CIResult.PASSED, commit_sha=_sha("sha-a"))],
            commit_sha=_sha("sha-a"),
            attribution_source=AttributionSource.GIT_NOTES,
        ),
        _attributed_completion(
            "c2",
            decisions=[_decision(accepted=True, explicit=True)],
            ci_outcomes=[_ci(CIResult.FAILED, commit_sha=_sha("sha-b"))],
            commit_sha=_sha("sha-b"),
            attribution_source=AttributionSource.GIT_NOTES,
        ),
        _attributed_completion(
            "c3",
            decisions=[_decision(accepted=True, explicit=True)],
            attribution_source=AttributionSource.JACCARD,
            commit_sha=_sha("sha-c"),
        ),
    ]
    sample = stratified_sample(attributed_completions, n=3)
    assert len(sample) == 3
    cells = {_cell_key(t) for t in sample}
    assert len(cells) == 3


def test_build_label_confidence_inspection_row_breakdown_matches_resolve_confidence() -> (
    None
):
    completions = [_completion("c-exp-acc", text="x" * 50)]
    attributed_completions = [_BRANCH_ATTRIBUTED_COMPLETIONS["explicit_accept"]]
    policy = LabelConfidencePolicy()
    rows = build_label_confidence_inspection(
        completions, attributed_completions, n=10, policy=policy
    )
    assert len(rows) == 1
    row = rows[0]
    expected = resolve_confidence_breakdown(attributed_completions[0], policy)
    assert row.confidence == expected
    assert row.decision_branch == "explicit_accept"
    assert row.completion_snippet == "x" * 50


def test_inspection_preserves_ci_provenance_fields() -> None:
    outcome = _ci(
        CIResult.ERROR,
        run_attempt=2,
        workflow_id="workflow-9",
        provider_result="startup_failure",
        error_type="runner_lost",
        reason="runner stopped responding",
        source_event_type="dev.cdevents.pipelinerun.finished.0.2.0",
        source_spec_version="0.5.0",
        source_event_id="event-9",
    )
    attributed_completion = _attributed_completion("c-rich-ci", ci_outcomes=[outcome])

    [row] = build_label_confidence_inspection(
        [_completion("c-rich-ci")], [attributed_completion], n=1
    )

    [summary] = row.ci_outcomes
    assert summary.outcome_id == outcome.outcome_id
    assert summary.provider == "github_actions"
    assert summary.repo == REPO
    assert summary.commit_sha == outcome.commit_sha
    assert summary.run_id == outcome.run_id
    assert summary.run_attempt == 2
    assert summary.workflow_id == "workflow-9"
    assert summary.provider_result == "startup_failure"
    assert summary.error_type == "runner_lost"
    assert summary.reason == "runner stopped responding"
    assert summary.source_event_type == "dev.cdevents.pipelinerun.finished.0.2.0"
    assert summary.source_spec_version == "0.5.0"
    assert summary.source_event_id == "event-9"


def test_inspection_exposes_resolution_reliability_and_policy_v3_comparison() -> None:
    commit_sha = _sha("retry")
    failed = _ci(
        CIResult.FAILED,
        commit_sha=commit_sha,
        run_id="run/retry",
        run_attempt=1,
    )
    passed = _ci(
        CIResult.PASSED,
        commit_sha=commit_sha,
        run_id="run/retry",
        run_attempt=2,
    )
    attributed_completion = _attributed_completion(
        "c-retry",
        ci_outcomes=[passed, failed],
        commit_sha=commit_sha,
    )

    [row] = build_label_confidence_inspection(
        [_completion("c-retry")], [attributed_completion], n=1
    )

    assert row.ci_resolution is not None
    assert row.ci_resolution.verdict == CIResult.PASSED
    assert row.ci_resolution.reliability == 0.0
    assert row.ci_resolution.suspected_flake is True
    assert row.ci_resolution_skips == {}
    assert row.confidence is not None
    assert row.confidence.final == 0.0
    assert row.policy_v3_confidence == 0.66
    assert row.confidence_delta_from_policy_v3 == -0.66


def test_build_label_confidence_inspection_renders_version_2_scoring_text() -> None:
    call = InferenceCall(
        inference_call_id="c-exp-acc",
        org_id=ORG,
        session_id="sess-1",
        gateway_provider=GatewayProvider.LITELLM,
        model="claude-sonnet-5",
        input_messages=[],
        output_messages=[
            InferenceMessage(
                role="assistant",
                parts=[
                    TextPart(content="done"),
                    ToolCallPart(
                        id="tool-1",
                        name="write",
                        arguments={"path": "app.py"},
                    ),
                ],
            )
        ],
        observed_at=NOW,
    )

    [row] = build_label_confidence_inspection(
        [call], [_BRANCH_ATTRIBUTED_COMPLETIONS["explicit_accept"]]
    )

    assert row.completion_snippet == "done\napp.py"


def test_build_label_confidence_inspection_renders_and_sorts_abandonment_evidence() -> (
    None
):
    attributed = _attributed_completion("c-attributed")
    abandoned = _abandoned_attributed_completion("c-abandoned")

    rows = build_label_confidence_inspection(
        [_completion("c-attributed"), _completion("c-abandoned")],
        [abandoned, attributed],
        n=10,
    )

    assert [row.inference_call_id for row in rows] == [
        "c-attributed",
        "c-abandoned",
    ]
    abandonment_row = rows[1]
    assert abandonment_row.repo is None
    assert abandonment_row.commit_sha is None
    assert abandonment_row.file_path is None
    assert abandonment_row.similarity_score is None
    assert abandonment_row.attribution_source == "abandonment"
    assert abandonment_row.decision_branch == "abandoned"


def test_build_label_confidence_inspection_truncates_long_completions() -> None:
    long_text = "y" * 500
    completions = [_completion("c-exp-acc", text=long_text)]
    attributed_completions = [_BRANCH_ATTRIBUTED_COMPLETIONS["explicit_accept"]]
    rows = build_label_confidence_inspection(completions, attributed_completions, n=10)
    assert len(rows) == 1
    assert len(rows[0].completion_snippet) < len(long_text)
    assert rows[0].completion_snippet.startswith("y" * 200)


def test_build_label_confidence_inspection_missing_inference_call_gets_none_snippet() -> (
    None
):
    attributed_completions = [_BRANCH_ATTRIBUTED_COMPLETIONS["explicit_accept"]]
    rows = build_label_confidence_inspection([], attributed_completions, n=10)
    assert len(rows) == 1
    assert rows[0].completion_snippet is None
    # Everything else is still populated from the attributed completion itself.
    assert rows[0].confidence is not None


def test_build_label_confidence_inspection_human_judgment_always_none() -> None:
    completions = [_completion(cid) for cid in _BRANCH_ATTRIBUTED_COMPLETIONS]
    attributed_completions = list(_BRANCH_ATTRIBUTED_COMPLETIONS.values())
    rows = build_label_confidence_inspection(
        completions, attributed_completions, n=len(attributed_completions)
    )
    assert len(rows) == len(attributed_completions)
    assert all(row.human_judgment is None for row in rows)


def test_build_label_confidence_inspection_no_signal_attributed_completion_has_none_confidence() -> (
    None
):
    attributed_completions = [_BRANCH_ATTRIBUTED_COMPLETIONS["no_decision"]]
    rows = build_label_confidence_inspection([], attributed_completions, n=10)
    assert len(rows) == 1
    assert rows[0].confidence is None
    assert rows[0].decision_branch == "no_decision"


def test_build_label_confidence_inspection_empty_attributed_completions_is_empty_result() -> (
    None
):
    assert build_label_confidence_inspection([], [], n=50) == []


def test_build_label_confidence_inspection_respects_n() -> None:
    completions = [_completion(cid) for cid in _BRANCH_ATTRIBUTED_COMPLETIONS]
    attributed_completions = list(_BRANCH_ATTRIBUTED_COMPLETIONS.values())
    rows = build_label_confidence_inspection(completions, attributed_completions, n=2)
    assert len(rows) == 2


def test_build_label_confidence_inspection_rows_are_json_serializable_with_breakdown() -> (
    None
):
    from dataclasses import asdict

    completions = [_completion(cid) for cid in _BRANCH_ATTRIBUTED_COMPLETIONS]
    attributed_completions = list(_BRANCH_ATTRIBUTED_COMPLETIONS.values())
    rows = build_label_confidence_inspection(
        completions, attributed_completions, n=len(attributed_completions)
    )
    payload = json.dumps([asdict(r) for r in rows])
    parsed = json.loads(payload)
    assert len(parsed) == len(rows)
    for row in parsed:
        assert row["human_judgment"] is None
        assert "decision_branch" in row
        assert "ci_bucket" in row
        # confidence is either null (no-signal attributed completion) or a breakdown dict.
        if row["confidence"] is not None:
            assert set(row["confidence"]) == {
                "decision_factor",
                "ci_factor",
                "ci_reliability",
                "similarity_discount",
                "final",
            }
