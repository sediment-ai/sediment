# SPDX-License-Identifier: AGPL-3.0-or-later
"""
SFT projection tests — real ``AttributedCompletion``/``InferenceCall``/
``DeveloperDecision``/``CIOutcome`` instances (per AGENTS.md: never mocked).
Same isolation level as ``test_dpo.py``: ``project_sft`` is a pure function
of attributed completions + a completion lookup, no git needed.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

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
    ReasoningPart,
    TextPart,
    ToolCallPart,
    ToolCallResponsePart,
)
from sediment_derive import AttributionSource, Provenance, SessionAbandonment

from sediment_export import (
    ExportRow,
    LabelConfidencePolicy,
    SFTPolicy,
    project_sft as _project_sft,
    sft_to_export_rows,
)
from sediment_export.attributed_completions import AttributedCompletion
from export_factories import inference_call, message

ORG = "acme-corp"
REPO = "acme-corp/backend-service"
SHA = "a" * 40
MODEL = "claude-sonnet-5"
PROMPT = [message("user", "write fib")]


def project_sft(attributed_completions, inference_calls, policy=None):
    """Exercise the verified recipe in legacy CI-mechanics tests."""

    return _project_sft(
        attributed_completions,
        inference_calls,
        policy or SFTPolicy(recipe_id="sft_verified"),
    )


def _completion(inference_call_id: str) -> InferenceCall:
    return inference_call(
        inference_call_id=inference_call_id,
        org_id=ORG,
        session_id="sess-1",
        model=MODEL,
        input_messages=list(PROMPT),
        output="def fib(): ...",
    )


def _decision(*, accepted: bool, explicit: bool = True) -> DeveloperDecision:
    return DeveloperDecision(
        org_id=ORG,
        session_id="sess-1",
        user_id="dev",
        agent_harness=AgentHarness.CLAUDE_CODE,
        file_path="a.py",
        accepted=accepted,
        explicit=explicit,
        interaction_mode=InteractionMode.AGENT,
        occurred_at=datetime.now(UTC),
    )


def _ci(
    result: CIResult,
    *,
    run: str = "run/1",
    run_attempt: int | None = None,
    workflow_id: str | None = "workflow-1",
    workflow_name: str = "CI",
    workflow_path: str | None = ".github/workflows/ci.yml",
) -> CIOutcome:
    return CIOutcome(
        org_id=ORG,
        provider=CIProvider.GITHUB_ACTIONS,
        run_id=run,
        run_attempt=run_attempt,
        repo=REPO,
        commit_sha=SHA,
        branch="main",
        result=result,
        workflow_id=workflow_id,
        workflow_name=workflow_name,
        workflow_path=workflow_path,
        run_url=run,
    )


def _attributed_completion(
    inference_call_id: str,
    *,
    decisions: list[DeveloperDecision] = (),
    ci_outcomes: list[CIOutcome] = (),
    split: str = "train",
    file_path: str = "a.py",
) -> AttributedCompletion:
    return AttributedCompletion(
        org_id=ORG,
        session_id="sess-1",
        inference_call_id=inference_call_id,
        repo=REPO,
        commit_sha=SHA,
        file_path=file_path,
        similarity_score=1.0,
        attribution_source=AttributionSource.GIT_NOTES,
        decisions=list(decisions),
        ci_outcomes=list(ci_outcomes),
        provenance=Provenance(policy_version="1", quarantine_revision=0),
        split=split,
    )


def _abandoned_attributed_completion(inference_call_id: str) -> AttributedCompletion:
    now = datetime.now(UTC)
    return AttributedCompletion(
        org_id=ORG,
        session_id="sess-1",
        inference_call_id=inference_call_id,
        repo=None,
        commit_sha=None,
        file_path=None,
        similarity_score=None,
        attribution_source=None,
        decisions=[_decision(accepted=True)],
        ci_outcomes=[],
        provenance=Provenance(policy_version="3", quarantine_revision=0),
        split="train",
        abandonment=SessionAbandonment(
            org_id=ORG,
            session_id="sess-1",
            accepted_decisions=1,
            explicit_accepted_decisions=1,
            last_decision_at=now,
            as_of=now,
            provenance=Provenance(policy_version="2", quarantine_revision=0),
        ),
    )


def test_abandonment_is_never_an_sft_positive_even_with_a_zero_floor() -> None:
    abandoned = _abandoned_attributed_completion("c-abandoned")

    out = project_sft(
        [abandoned],
        {"c-abandoned": _completion("c-abandoned")},
        SFTPolicy(min_confidence=0.0),
    )

    assert out.rows == []
    assert out.skipped == {"abandoned": 1}


def test_ci_passed_with_no_decision_is_included() -> None:
    t = _attributed_completion("c-1", ci_outcomes=[_ci(CIResult.PASSED)])
    out = project_sft([t], {"c-1": _completion("c-1")})
    assert len(out.rows) == 1
    row = out.rows[0]
    assert row.metadata.completion_id == "c-1"
    assert row.metadata.org_id == ORG
    assert row.metadata.source_model == MODEL
    assert row.prompt == [{"role": "user", "content": "write fib"}]
    assert row.completion == [{"role": "assistant", "content": "def fib(): ..."}]
    assert row.metadata.split == "train"
    assert row.metadata.ci_reliability == pytest.approx(1.0)


def test_flake_suspected_pass_is_not_verified_eligibility() -> None:
    t = _attributed_completion(
        "c-retry",
        ci_outcomes=[
            _ci(CIResult.FAILED, run_attempt=1),
            _ci(CIResult.PASSED, run_attempt=2),
        ],
    )

    out = project_sft(
        [t],
        {"c-retry": _completion("c-retry")},
        SFTPolicy(recipe_id="sft_verified", min_confidence=0.0),
    )

    assert out.rows == []
    assert out.skipped["unreliable_ci_resolution"] == 1


def test_conflicting_workflow_verdicts_skip_and_count_as_ci_ineligible() -> None:
    t = _attributed_completion(
        "c-ambiguous",
        ci_outcomes=[
            _ci(CIResult.PASSED, run="run-lint"),
            _ci(
                CIResult.FAILED,
                run="run-tests",
                workflow_id="workflow-2",
                workflow_name="Tests",
                workflow_path=".github/workflows/tests.yml",
            ),
        ],
    )

    out = project_sft([t], {"c-ambiguous": _completion("c-ambiguous")})

    assert out.rows == []
    assert out.skipped["ambiguous_workflow_verdicts"] == 1
    assert out.skipped["resolved_ci_failure"] == 1


def test_curated_explicit_accept_is_vetoed_by_an_ambiguous_ci_failure() -> None:
    attributed_completion = _attributed_completion(
        "c-ambiguous-accept",
        decisions=[_decision(accepted=True)],
        ci_outcomes=[
            _ci(CIResult.PASSED, run="run-lint"),
            _ci(
                CIResult.FAILED,
                run="run-tests",
                workflow_id="workflow-2",
                workflow_name="Tests",
                workflow_path=".github/workflows/tests.yml",
            ),
        ],
    )

    out = _project_sft(
        [attributed_completion],
        {"c-ambiguous-accept": _completion("c-ambiguous-accept")},
    )

    assert out.rows == []
    assert out.skipped["ambiguous_workflow_verdicts"] == 1
    assert out.skipped["resolved_ci_failure"] == 1


def test_structured_inference_call_projects_native_prompt_and_rendered_output() -> None:
    call = InferenceCall(
        inference_call_id="inference-1",
        org_id=ORG,
        session_id="sess-1",
        user_id="dev",
        gateway_provider=GatewayProvider.LITELLM,
        model=MODEL,
        input_messages=[
            InferenceMessage(role="user", parts=[TextPart(content="write fib")])
        ],
        output_messages=[
            InferenceMessage(
                role="assistant",
                parts=[
                    ToolCallPart(
                        id="tool-1",
                        name="Write",
                        arguments={"content": "def fib(): ..."},
                    )
                ],
            )
        ],
    )
    labeled = _attributed_completion(
        call.inference_call_id, ci_outcomes=[_ci(CIResult.PASSED)]
    )

    [row] = project_sft([labeled], {call.inference_call_id: call}).rows

    assert row.prompt == [{"role": "user", "content": "write fib"}]
    assert row.completion == [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "tool-1",
                    "type": "function",
                    "function": {
                        "name": "Write",
                        "arguments": {"content": "def fib(): ..."},
                    },
                }
            ],
        }
    ]


def test_projects_canonical_parts_to_trl_messages_without_exposing_reasoning() -> None:
    call = InferenceCall(
        inference_call_id="inference-trl",
        org_id=ORG,
        session_id="sess-1",
        user_id="dev",
        gateway_provider=GatewayProvider.LITELLM,
        model=MODEL,
        input_messages=[
            InferenceMessage(
                role="system", parts=[TextPart(content="Use the repository tools.")]
            ),
            InferenceMessage(
                role="assistant",
                parts=[
                    ReasoningPart(content="Inspect the existing file."),
                    ToolCallPart(
                        id="tool-1",
                        name="Read",
                        arguments={"file_path": "math_utils.py"},
                    ),
                ],
            ),
            InferenceMessage(
                role="tool",
                parts=[
                    ToolCallResponsePart(id="tool-1", result="def fibonacci(): ...")
                ],
            ),
            InferenceMessage(role="user", parts=[TextPart(content="Fix it.")]),
        ],
        output_messages=[
            InferenceMessage(
                role="assistant",
                parts=[
                    ReasoningPart(content="Keep the public signature."),
                    TextPart(content="Implemented the fix."),
                ],
            )
        ],
    )
    attributed_completion = _attributed_completion(
        call.inference_call_id, ci_outcomes=[_ci(CIResult.PASSED)]
    )

    [row] = project_sft([attributed_completion], {call.inference_call_id: call}).rows

    assert row.prompt == [
        {"role": "system", "content": "Use the repository tools."},
        {
            "role": "assistant",
            "thinking": "Inspect the existing file.",
            "tool_calls": [
                {
                    "id": "tool-1",
                    "type": "function",
                    "function": {
                        "name": "Read",
                        "arguments": {"file_path": "math_utils.py"},
                    },
                }
            ],
        },
        {
            "role": "tool",
            "name": "Read",
            "tool_call_id": "tool-1",
            "content": "def fibonacci(): ...",
        },
        {"role": "user", "content": "Fix it."},
    ]
    assert row.completion == [
        {
            "role": "assistant",
            "thinking": "Keep the public signature.",
            "content": "Implemented the fix.",
        }
    ]
    assert row.tools == []
    assert "Keep the public signature." not in row.completion[0]["content"]
    assert row.metadata.org_id == ORG
    assert row.metadata.source_model == MODEL
    assert row.metadata.completion_id == call.inference_call_id
    assert row.metadata.label_confidence == pytest.approx(0.66)
    assert row.metadata.provenance == attributed_completion.provenance
    assert row.metadata.split == "train"


def test_structured_tool_result_preserves_json_in_the_sft_row() -> None:
    call = InferenceCall(
        inference_call_id="inference-structured-result",
        org_id=ORG,
        session_id="sess-1",
        user_id="dev",
        gateway_provider=GatewayProvider.LITELLM,
        model=MODEL,
        input_messages=[
            InferenceMessage(
                role="assistant",
                parts=[ToolCallPart(id="tool-1", name="Read", arguments={})],
            ),
            InferenceMessage(
                role="tool",
                parts=[ToolCallResponsePart(id="tool-1", result={"ok": True})],
            ),
        ],
        output_messages=[
            InferenceMessage(role="assistant", parts=[TextPart(content="Done")])
        ],
    )
    attributed_completion = _attributed_completion(
        call.inference_call_id, ci_outcomes=[_ci(CIResult.PASSED)]
    )

    out = project_sft([attributed_completion], {call.inference_call_id: call})

    assert len(out.rows) == 1
    assert out.rows[0].prompt[-1]["content"] == '{"ok":true}'
    assert out.skipped == {}


def test_tool_response_with_non_tool_role_skips_and_counts_the_sft_row() -> None:
    call = InferenceCall(
        inference_call_id="inference-tool-role-mismatch",
        org_id=ORG,
        session_id="sess-1",
        user_id="dev",
        gateway_provider=GatewayProvider.LITELLM,
        model=MODEL,
        input_messages=[
            InferenceMessage(
                role="assistant",
                parts=[ToolCallPart(id="tool-1", name="Read", arguments={})],
            ),
            InferenceMessage(
                role="assistant",
                parts=[ToolCallResponsePart(id="tool-1", result="file text")],
            ),
        ],
        output_messages=[
            InferenceMessage(role="assistant", parts=[TextPart(content="Done")])
        ],
    )
    attributed_completion = _attributed_completion(
        call.inference_call_id, ci_outcomes=[_ci(CIResult.PASSED)]
    )

    out = project_sft([attributed_completion], {call.inference_call_id: call})

    assert out.rows == []
    assert out.skipped == {"unsupported_role_part": 1}


def test_non_assistant_completion_role_skips_and_counts_the_sft_row() -> None:
    call = InferenceCall(
        inference_call_id="inference-completion-role-mismatch",
        org_id=ORG,
        session_id="sess-1",
        user_id="dev",
        gateway_provider=GatewayProvider.LITELLM,
        model=MODEL,
        input_messages=[
            InferenceMessage(role="user", parts=[TextPart(content="Fix it.")])
        ],
        output_messages=[
            InferenceMessage(role="user", parts=[TextPart(content="Not a response")])
        ],
    )
    attributed_completion = _attributed_completion(
        call.inference_call_id, ci_outcomes=[_ci(CIResult.PASSED)]
    )

    out = project_sft([attributed_completion], {call.inference_call_id: call})

    assert out.rows == []
    assert out.skipped == {"unsupported_completion_role": 1}


def test_missing_output_messages_skips_and_counts_the_sft_row() -> None:
    call = InferenceCall(
        inference_call_id="inference-no-output",
        org_id=ORG,
        session_id="sess-1",
        user_id="dev",
        gateway_provider=GatewayProvider.LITELLM,
        model=MODEL,
        input_messages=[
            InferenceMessage(role="user", parts=[TextPart(content="Fix it.")])
        ],
        output_messages=[],
    )
    attributed_completion = _attributed_completion(
        call.inference_call_id, ci_outcomes=[_ci(CIResult.PASSED)]
    )

    out = project_sft([attributed_completion], {call.inference_call_id: call})

    assert out.rows == []
    assert out.skipped == {"completionless": 1}


def test_explicit_accept_with_no_ci_is_included() -> None:
    t = _attributed_completion("c-1", decisions=[_decision(accepted=True)])
    out = _project_sft([t], {"c-1": _completion("c-1")})
    assert len(out.rows) == 1
    assert out.rows[0].metadata.label_confidence == pytest.approx(
        LabelConfidencePolicy().explicit_accept_confidence
    )


def test_ci_failed_with_no_decision_is_excluded() -> None:
    t = _attributed_completion("c-1", ci_outcomes=[_ci(CIResult.FAILED)])
    out = _project_sft([t], {"c-1": _completion("c-1")})
    assert out.rows == []
    assert out.skipped["resolved_ci_failure"] == 1


@pytest.mark.parametrize(
    "non_verdict",
    [
        CIResult.ERROR,
        CIResult.TIMED_OUT,
        CIResult.CANCELLED,
        CIResult.SKIPPED,
        CIResult.NEUTRAL,
        CIResult.UNKNOWN,
    ],
)
def test_non_verdict_ci_without_explicit_accept_is_excluded(
    non_verdict: CIResult,
) -> None:
    attributed_completion = _attributed_completion(
        "c-1", ci_outcomes=[_ci(non_verdict)]
    )

    out = project_sft([attributed_completion], {"c-1": _completion("c-1")})

    assert out.rows == []
    assert out.skipped["no_eligibility_source"] == 1


def test_explicit_accept_does_not_override_a_ci_failure_for_sft() -> None:
    # SFT requires passed CI, or an explicit accept when no CI exists.
    # An accept cannot override a CI failure; see sft.py's module docstring.
    t = _attributed_completion(
        "c-1", decisions=[_decision(accepted=True)], ci_outcomes=[_ci(CIResult.FAILED)]
    )
    out = _project_sft([t], {"c-1": _completion("c-1")})
    assert out.rows == []
    assert out.skipped["resolved_ci_failure"] == 1


def test_implicit_accept_with_no_ci_is_excluded() -> None:
    # No explicit accept and no CI: the "no CI but an explicit accept"
    # escape hatch requires a *real* human gesture, not an inferred one.
    t = _attributed_completion(
        "c-1", decisions=[_decision(accepted=True, explicit=False)]
    )
    out = _project_sft([t], {"c-1": _completion("c-1")})
    assert out.rows == []
    assert out.skipped["no_eligibility_source"] == 1


def test_explicit_reject_excludes_even_with_a_ci_pass() -> None:
    t = _attributed_completion(
        "c-1", decisions=[_decision(accepted=False)], ci_outcomes=[_ci(CIResult.PASSED)]
    )
    out = project_sft([t], {"c-1": _completion("c-1")})
    assert out.rows == []
    assert out.skipped["explicit_reject"] == 1


def test_below_confidence_floor_is_excluded() -> None:
    # CI-passed alone (no decision) resolves to baseline*ci_pass_multiplier
    # under the default policy; a floor above that value excludes it even
    # though it's otherwise CI-eligible.
    t = _attributed_completion("c-1", ci_outcomes=[_ci(CIResult.PASSED)])
    policy = SFTPolicy(recipe_id="sft_verified", min_confidence=0.99)
    out = project_sft([t], {"c-1": _completion("c-1")}, policy)
    assert out.rows == []
    assert out.skipped["below_confidence_floor"] == 1


def test_confidence_floor_is_configurable_and_inclusive_at_the_boundary() -> None:
    t = _attributed_completion("c-1", decisions=[_decision(accepted=True)])
    policy = SFTPolicy(min_confidence=1.0)  # explicit accept resolves to exactly 1.0
    out = project_sft([t], {"c-1": _completion("c-1")}, policy)
    assert len(out.rows) == 1


def test_inference_call_not_found_is_skipped() -> None:
    t = _attributed_completion("ghost", ci_outcomes=[_ci(CIResult.PASSED)])
    out = project_sft([t], {})
    assert out.rows == []
    assert out.skipped["inference_call_not_found"] == 1


def test_split_propagates_from_the_attributed_completion() -> None:
    t = _attributed_completion("c-1", ci_outcomes=[_ci(CIResult.PASSED)], split="eval")
    out = project_sft([t], {"c-1": _completion("c-1")})
    assert out.rows[0].metadata.split == "eval"


def test_sft_rows_round_trip_through_jsonl(tmp_path) -> None:
    from sediment_export import write_jsonl

    t = _attributed_completion("c-1", ci_outcomes=[_ci(CIResult.PASSED)])
    out = project_sft([t], {"c-1": _completion("c-1")})
    export_rows = sft_to_export_rows(out.rows)
    assert all(isinstance(r, ExportRow) for r in export_rows)

    result = write_jsonl(export_rows, tmp_path / "sft.jsonl", split_enabled=False)
    written_path = tmp_path / "sft.jsonl"
    assert str(written_path) in result.written

    import json

    lines = written_path.read_text().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["prompt"] == [{"role": "user", "content": "write fib"}]
    assert row["completion"] == [{"role": "assistant", "content": "def fib(): ..."}]
    assert row["tools"] == []
    assert row["metadata"]["completion_id"] == "c-1"
    assert "confidence" not in row


def test_sft_policy_rejects_an_out_of_range_floor() -> None:
    with pytest.raises(ValueError):
        SFTPolicy(min_confidence=1.5)
    with pytest.raises(ValueError):
        SFTPolicy(min_confidence=-0.1)


def test_sft_projection_and_jsonl_are_identical_when_inputs_are_shuffled(
    tmp_path,
) -> None:
    from sediment_export import write_jsonl

    first = _attributed_completion("c-a", ci_outcomes=[_ci(CIResult.PASSED)])
    second = _attributed_completion("c-b", ci_outcomes=[_ci(CIResult.PASSED)])
    inference_calls = {"c-a": _completion("c-a"), "c-b": _completion("c-b")}

    forward = project_sft([first, second], inference_calls)
    reversed_input = project_sft([second, first], inference_calls)

    assert forward.rows == reversed_input.rows
    assert forward.skipped == reversed_input.skipped

    forward_path = tmp_path / "forward.jsonl"
    shuffled_path = tmp_path / "shuffled.jsonl"
    write_jsonl(sft_to_export_rows(forward.rows), forward_path, split_enabled=False)
    write_jsonl(
        sft_to_export_rows(reversed_input.rows),
        shuffled_path,
        split_enabled=False,
    )
    assert forward_path.read_bytes() == shuffled_path.read_bytes()


def test_one_sample_per_completion_even_across_multiple_attributed_completions() -> (
    None
):
    # A notes-attributed session touching several files yields several
    # attributed_completions for one completion (one attribution per changed file);
    # SFTSample carries no per-file field, so one row per attributed completion would emit
    # N near-identical
    # copies of the same prompt/completion text, over-weighting multi-file
    # completions. The highest-confidence eligible attributed completion wins.
    attributed_completions = [
        _attributed_completion(
            "c-1", ci_outcomes=[_ci(CIResult.PASSED)], file_path="a.py"
        ),
        _attributed_completion(
            "c-1", ci_outcomes=[_ci(CIResult.PASSED)], file_path="b.py"
        ),
        _attributed_completion(
            "c-1",
            decisions=[_decision(accepted=True)],
            ci_outcomes=[_ci(CIResult.PASSED)],
            file_path="c.py",
        ),
    ]
    out = project_sft(attributed_completions, {"c-1": _completion("c-1")})
    assert len(out.rows) == 1
    # explicit accept (1.0) * ci pass (1.1), capped to 1.0 — the strongest
    # of the three, not whichever file happened to iterate first.
    assert out.rows[0].metadata.label_confidence == pytest.approx(1.0)
    assert out.skipped["duplicate_completion"] == 2


def test_cancelled_only_ci_with_explicit_accept_is_included() -> None:
    # A cancelled run is one non-verdict state: non-verdict-only
    # CI must not block the explicit-accept escape hatch the way a real
    # failure does — semantically this attributed completion has no pass/fail verdict, and
    # a real human accepted it.
    t = _attributed_completion(
        "c-1",
        decisions=[_decision(accepted=True)],
        ci_outcomes=[_ci(CIResult.CANCELLED)],
    )
    out = _project_sft([t], {"c-1": _completion("c-1")})
    assert len(out.rows) == 1
    assert out.rows[0].metadata.label_confidence == pytest.approx(
        LabelConfidencePolicy().explicit_accept_confidence
    )


def test_cancelled_only_ci_with_implicit_accept_is_excluded() -> None:
    # Without the explicit accept, non-verdict-only CI is still no signal at
    # all — not a training row (CONTEXT.md's reward-signal rule).
    t = _attributed_completion(
        "c-1",
        decisions=[_decision(accepted=True, explicit=False)],
        ci_outcomes=[_ci(CIResult.CANCELLED)],
    )
    out = project_sft([t], {"c-1": _completion("c-1")})
    assert out.rows == []
    assert out.skipped["no_eligibility_source"] == 1


def test_inferred_recipe_keeps_its_attribution_source_without_observation():
    from dataclasses import replace

    call = _completion("source-audit")
    row = replace(
        _attributed_completion(
            call.inference_call_id, ci_outcomes=[_ci(CIResult.PASSED)]
        ),
        attribution_source=AttributionSource.JACCARD,
    )
    result = project_sft([row], {call.inference_call_id: call})
    assert len(result.rows) == 1
    assert result.rows[0].metadata.recipe_version == 1
    assert result.rows[0].metadata.attribution_source == AttributionSource.JACCARD
    assert result.rows[0].metadata.session_commit_observation_ids == ()


@pytest.mark.parametrize("recipe", ["sft_curated", "sft_verified"])
def test_sft_v1_selected_target_preserves_only_matching_observation_ids(recipe):
    from dataclasses import replace
    from sediment_core import SessionCommitObservation

    fact = SessionCommitObservation(
        observation_id="observed",
        org_id=ORG,
        session_id="sess-1",
        repo=REPO,
        commit_sha=SHA,
        source_push_id="push",
    )
    unrelated = fact.model_copy(
        update={"observation_id": "unrelated", "commit_sha": "b" * 40}
    )
    row = replace(
        _attributed_completion(
            "call",
            decisions=[_decision(accepted=True)],
            ci_outcomes=[_ci(CIResult.PASSED)],
        ),
        attribution_source=AttributionSource.JACCARD,
        session_commit_observations=(unrelated, fact),
    )
    result = _project_sft(
        [row],
        {"call": _completion("call")},
        SFTPolicy(recipe_id=recipe, min_confidence=0.0),
    )
    [sample] = result.rows
    assert sample.metadata.recipe_version == 1
    assert sample.metadata.attribution_source == AttributionSource.JACCARD
    assert sample.metadata.session_commit_observation_ids == ("observed",)
    assert result.skipped == {}


@pytest.mark.parametrize("location", ["completion", "metadata"])
def test_sft_representation_checks_emitted_row_once(location):
    call = _completion("c")
    if location == "completion":
        call = call.model_copy(
            update={"output_messages": [message("assistant", "\ud800")]}
        )
    attributed = _attributed_completion("c", decisions=[_decision(accepted=True)])
    if location == "metadata":
        attributed = replace(
            attributed,
            provenance=Provenance(policy_version="v\ud800", quarantine_revision=0),
        )
    out = project_sft([attributed], {"c": call}, SFTPolicy())
    assert out.rows == []
    assert dict(out.skipped) == {"unrepresentable_unicode": 1}


def test_sft_omitted_raw_and_ci_descriptions_do_not_decline_row():
    call = _completion("c").model_copy(update={"raw": {"bad": float("nan")}})
    ci = _ci(CIResult.PASSED).model_copy(
        update={"reason": "\ud800", "raw": {"n": float("nan")}}
    )
    out = project_sft([_attributed_completion("c", ci_outcomes=[ci])], {"c": call})
    assert len(out.rows) == 1
    assert dict(out.skipped) == {}


@pytest.mark.parametrize("recipe", ["sft_curated", "sft_verified"])
def test_sft_nested_non_finite_completion_declines_once(recipe):
    from export_factories import tool_call

    call = _completion("c").model_copy(
        update={
            "output_messages": [
                InferenceMessage(
                    role="assistant",
                    parts=[
                        tool_call("t", "run", {"nested": [float("nan"), float("inf")]})
                    ],
                )
            ]
        }
    )
    attributed = _attributed_completion(
        "c", decisions=[_decision(accepted=True)], ci_outcomes=[_ci(CIResult.PASSED)]
    )
    out = project_sft([attributed], {"c": call}, SFTPolicy(recipe_id=recipe))
    assert out.rows == []
    assert dict(out.skipped) == {"non_finite_number": 1}


def test_sft_repository_qualified_tie_is_byte_deterministic(tmp_path):
    from dataclasses import replace
    from itertools import permutations
    from sediment_derive import CIResolutionPolicy
    from sediment_export import write_jsonl

    decision = _decision(accepted=True)
    left = _attributed_completion(
        "c", decisions=[decision], ci_outcomes=[_ci(CIResult.PASSED)]
    )
    repo = "z-fork/backend-service"
    right = replace(
        left,
        repo=repo,
        ci_outcomes=[
            _ci(CIResult.FAILED, run_attempt=1).model_copy(update={"repo": repo}),
            _ci(CIResult.PASSED, run_attempt=2).model_copy(update={"repo": repo}),
        ],
    )
    # Both cap at Confidence 1.0 while their CI reliability differs.
    policy = SFTPolicy(
        label_confidence=LabelConfidencePolicy(
            ci_pass_multiplier=2.0,
            ci_resolution=CIResolutionPolicy(suspected_flake_reliability=0.75),
        ),
        recipe_id="sft_curated",
    )
    expected = None
    for index, ordered in enumerate(permutations([left, right])):
        result = _project_sft(ordered, {"c": _completion("c")}, policy)
        assert result.rows[0].metadata.label_confidence == 1.0
        assert result.rows[0].metadata.ci_reliability == 0.75
        assert dict(result.skipped) == {"duplicate_completion": 1}
        path = tmp_path / f"rows-{index}.jsonl"
        write_jsonl(sft_to_export_rows(result.rows), path, split_enabled=False)
        if expected is None:
            expected = path.read_bytes()
        assert path.read_bytes() == expected


@pytest.mark.parametrize(
    "conflict", ["split", "provenance", "confidence", "ineligible", "omitted_raw"]
)
def test_sft_conflicting_identity_declines_entire_call_once(conflict):
    from dataclasses import replace
    from itertools import permutations

    source = _attributed_completion("c", decisions=[_decision(accepted=True)])
    if conflict == "split":
        changed = replace(source, split="eval")
    elif conflict == "provenance":
        changed = replace(
            source,
            provenance=Provenance(policy_version="different", quarantine_revision=0),
        )
    elif conflict == "confidence":
        changed = replace(
            source, similarity_score=0.6, attribution_source=AttributionSource.JACCARD
        )
    elif conflict == "ineligible":
        changed = replace(source, decisions=[_decision(accepted=False)])
    else:
        changed = replace(
            source,
            decisions=[
                source.decisions[0].model_copy(update={"raw": {"source": "different"}})
            ],
        )
    higher_sibling = replace(source, file_path="z.py")
    valid = _attributed_completion("valid", decisions=[_decision(accepted=True)])
    calls = {key: _completion(key) for key in ("c", "valid")}
    for ordered in permutations([source, changed, higher_sibling, valid]):
        result = _project_sft(ordered, calls, SFTPolicy())
        assert [row.metadata.completion_id for row in result.rows] == ["valid"]
        assert dict(result.skipped) == {"conflicting_evidence": 1}


def test_sft_identical_duplicates_preserve_duplicate_completion_count():
    source = _attributed_completion("c", decisions=[_decision(accepted=True)])
    result = _project_sft(
        [source, source, source], {"c": _completion("c")}, SFTPolicy()
    )
    assert len(result.rows) == 1
    assert dict(result.skipped) == {"duplicate_completion": 2}


def test_sft_duplicate_evidence_treats_distinct_nan_categories_as_equal():
    from dataclasses import replace

    decision = _decision(accepted=True)
    source = _attributed_completion(
        "c", decisions=[decision.model_copy(update={"raw": {"n": float("nan")}})]
    )
    duplicate = replace(
        source, decisions=[decision.model_copy(update={"raw": {"n": float("nan")}})]
    )
    result = _project_sft([source, duplicate], {"c": _completion("c")}, SFTPolicy())
    assert len(result.rows) == 1
    assert dict(result.skipped) == {"duplicate_completion": 1}


@pytest.mark.parametrize("ineligible", [False, True])
def test_sft_eligibility_and_confidence_precede_repository_order(ineligible):
    from dataclasses import replace

    high = _attributed_completion("c", decisions=[_decision(accepted=True)])
    low = replace(
        high,
        repo="z-fork/service",
        attribution_source=AttributionSource.JACCARD,
        similarity_score=0.8,
        provenance=Provenance(policy_version="low", quarantine_revision=0),
    )
    if ineligible:
        low = replace(low, decisions=[_decision(accepted=False)])
    for rows in ([low, high], [high, low]):
        result = _project_sft(rows, {"c": _completion("c")}, SFTPolicy())
        assert len(result.rows) == 1
        assert result.rows[0].metadata.provenance == high.provenance
        assert result.rows[0].metadata.label_confidence == 1.0
        assert dict(result.skipped) == {
            "explicit_reject" if ineligible else "duplicate_completion": 1
        }


def test_sft_conflicting_evidence_reason_is_closed():
    from sediment_export.sft import SFT_SKIP_REASONS

    assert SFT_SKIP_REASONS.count("conflicting_evidence") == 1
