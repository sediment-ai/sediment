# SPDX-License-Identifier: AGPL-3.0-or-later
"""
DPO projection tests — real ``AttributedCompletion``/``InferenceCall``/
``DeveloperDecision``/``CIOutcome`` instances (per AGENTS.md: never mocked).
No real git needed here (unlike ``test_attributed_completions.py``): ``project_dpo`` is a
pure function of attributed completions + a completion lookup, so hand-built fixtures are
the right level of isolation for the pairing semantics themselves.
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
)
from sediment_derive import AttributionSource, Provenance, SessionAbandonment

from sediment_export import (
    DPOPolicy,
    ExportRow,
    LabelConfidencePolicy,
    dpo_to_export_rows,
    project_dpo as _project_dpo,
)
from sediment_export.attributed_completions import AttributedCompletion
from export_factories import inference_call, message

ORG = "acme-corp"
REPO = "acme-corp/backend-service"
SHA = "a" * 40
MODEL = "claude-sonnet-5"

PROMPT = [message("user", "write fib")]
OTHER_PROMPT = [message("user", "write a shopping cart")]


def project_dpo(attributed_completions, inference_calls, policy=None):
    """Exercise the outcome recipe in legacy CI-mechanics tests."""

    return _project_dpo(
        attributed_completions,
        inference_calls,
        policy or DPOPolicy(recipe_id="dpo_outcome"),
    )


def _completion(
    inference_call_id: str,
    messages: list[InferenceMessage] = PROMPT,
    *,
    model: str = MODEL,
    session_id: str = "sess-1",
    output: str | None = None,
) -> InferenceCall:
    return inference_call(
        inference_call_id=inference_call_id,
        org_id=ORG,
        session_id=session_id,
        model=model,
        input_messages=list(messages),
        output=output
        if output is not None
        else f"def fib(): return {inference_call_id!r}",
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
    commit_sha: str = SHA,
    file_path: str = "a.py",
    split: str = "train",
    session_id: str = "sess-1",
) -> AttributedCompletion:
    return AttributedCompletion(
        org_id=ORG,
        session_id=session_id,
        inference_call_id=inference_call_id,
        repo=REPO,
        commit_sha=commit_sha,
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


def test_abandonment_is_not_a_dpo_human_label_source() -> None:
    chosen = _attributed_completion("chosen", decisions=[_decision(accepted=True)])
    abandoned = _abandoned_attributed_completion("abandoned")
    completions = {
        "chosen": _completion("chosen"),
        "abandoned": _completion("abandoned"),
    }

    out = _project_dpo([chosen, abandoned], completions)

    assert out.rows == []
    assert out.skipped["no_label_source"] == 1


def test_ci_pass_pairs_against_ci_fail_for_the_same_prompt_and_model() -> None:
    passed = _completion("c-pass")
    failed = _completion("c-fail")
    attributed_completions = [
        _attributed_completion("c-pass", ci_outcomes=[_ci(CIResult.PASSED)]),
        _attributed_completion("c-fail", ci_outcomes=[_ci(CIResult.FAILED)]),
    ]
    completions = {"c-pass": passed, "c-fail": failed}

    out = project_dpo(attributed_completions, completions)
    assert len(out.rows) == 1
    pair = out.rows[0]
    assert pair.metadata.chosen_completion_id == "c-pass"
    assert pair.metadata.rejected_completion_id == "c-fail"
    assert pair.metadata.source_model == MODEL
    assert pair.prompt == [{"role": "user", "content": "write fib"}]
    assert pair.metadata.split == "train"
    assert pair.metadata.org_id == ORG
    assert pair.metadata.ci_reliability == pytest.approx(1.0)

    assert out.rows[0].metadata.chosen_attribution_source == AttributionSource.GIT_NOTES
    assert (
        out.rows[0].metadata.rejected_attribution_source == AttributionSource.GIT_NOTES
    )
    assert out.rows[0].metadata.chosen_session_commit_observation_ids == ()
    assert out.rows[0].metadata.rejected_session_commit_observation_ids == ()


def test_retry_is_not_a_directional_dpo_outcome_label() -> None:
    retried = _attributed_completion(
        "c-retried",
        ci_outcomes=[
            _ci(CIResult.FAILED, run_attempt=1),
            _ci(CIResult.PASSED, run_attempt=2),
        ],
    )
    failed = _attributed_completion(
        "c-fail", ci_outcomes=[_ci(CIResult.FAILED, run="run-fail")]
    )
    completions = {
        "c-retried": _completion("c-retried"),
        "c-fail": _completion("c-fail"),
    }

    out = project_dpo([retried, failed], completions)

    assert out.rows == []
    assert out.skipped["unreliable_ci_resolution"] == 1


def test_conflicting_workflow_verdicts_skip_and_count_without_classification() -> None:
    ambiguous = _attributed_completion(
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

    out = project_dpo([ambiguous], {"c-ambiguous": _completion("c-ambiguous")})

    assert out.rows == []
    assert out.skipped["ambiguous_workflow_verdicts"] == 1
    assert out.skipped["no_label_source"] == 1


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
def test_non_verdict_ci_does_not_classify_a_dpo_member(
    non_verdict: CIResult,
) -> None:
    completion = _completion("c-non-verdict")
    attributed_completion = _attributed_completion(
        "c-non-verdict", ci_outcomes=[_ci(non_verdict)]
    )

    out = project_dpo(
        [attributed_completion], {completion.inference_call_id: completion}
    )

    assert out.rows == []
    assert out.skipped["no_label_source"] == 1


def test_structured_inference_calls_bucket_on_native_message_parts() -> None:
    def call(call_id: str) -> InferenceCall:
        return InferenceCall(
            inference_call_id=call_id,
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
                    parts=[TextPart(content=f"implementation {call_id}")],
                )
            ],
        )

    passed = call("v2-pass")
    failed = call("v2-fail")
    attributed_completions = [
        _attributed_completion("v2-pass", ci_outcomes=[_ci(CIResult.PASSED)]),
        _attributed_completion("v2-fail", ci_outcomes=[_ci(CIResult.FAILED)]),
    ]

    [pair] = project_dpo(
        attributed_completions, {"v2-pass": passed, "v2-fail": failed}
    ).rows

    assert pair.prompt == [{"role": "user", "content": "write fib"}]


def test_cross_prompt_completions_never_pair() -> None:
    passed = _completion("c-pass", PROMPT)
    other = _completion("c-other", OTHER_PROMPT)
    attributed_completions = [
        _attributed_completion("c-pass", ci_outcomes=[_ci(CIResult.PASSED)]),
        _attributed_completion("c-other", ci_outcomes=[_ci(CIResult.FAILED)]),
    ]
    out = project_dpo(attributed_completions, {"c-pass": passed, "c-other": other})
    assert out.rows == []


def test_different_models_on_the_same_prompt_never_pair() -> None:
    passed = _completion("c-pass", model="model-a")
    failed = _completion("c-fail", model="model-b")
    attributed_completions = [
        _attributed_completion("c-pass", ci_outcomes=[_ci(CIResult.PASSED)]),
        _attributed_completion("c-fail", ci_outcomes=[_ci(CIResult.FAILED)]),
    ]
    out = project_dpo(attributed_completions, {"c-pass": passed, "c-fail": failed})
    assert out.rows == []


def test_a_completion_never_pairs_against_itself() -> None:
    # A completion touching two files in one commit produces two attributed completions
    # for the same inference_call_id; without dedup this would let it pair
    # against itself.
    solo = _completion("c-1")
    attributed_completions = [
        _attributed_completion(
            "c-1", decisions=[_decision(accepted=True)], file_path="a.py"
        ),
        _attributed_completion(
            "c-1", decisions=[_decision(accepted=False)], file_path="b.py"
        ),
    ]
    out = project_dpo(attributed_completions, {"c-1": solo})
    assert out.rows == []
    assert out.rows == [] or all(
        r.metadata.chosen_completion_id != r.metadata.rejected_completion_id
        for r in out.rows
    )


def test_promptless_completion_is_skipped_and_never_pairs() -> None:
    promptless = _completion("c-empty", messages=[])
    normal = _completion("c-normal", messages=[])
    attributed_completions = [
        _attributed_completion("c-empty", ci_outcomes=[_ci(CIResult.PASSED)]),
        _attributed_completion("c-normal", ci_outcomes=[_ci(CIResult.FAILED)]),
    ]
    out = project_dpo(
        attributed_completions, {"c-empty": promptless, "c-normal": normal}
    )
    assert out.rows == []
    assert out.skipped["promptless"] == 2


def test_inference_call_not_found_is_skipped() -> None:
    attributed_completions = [
        _attributed_completion("ghost", ci_outcomes=[_ci(CIResult.PASSED)])
    ]
    out = project_dpo(attributed_completions, {})
    assert out.rows == []
    assert out.skipped["inference_call_not_found"] == 1


def test_dpo_human_uses_explicit_accept_even_when_ci_failed() -> None:
    accepted_but_failed = _completion("c-accept-fail")
    plain_fail = _completion("c-plain-fail")
    attributed_completions = [
        _attributed_completion(
            "c-accept-fail",
            decisions=[_decision(accepted=True)],
            ci_outcomes=[_ci(CIResult.FAILED)],
        ),
        _attributed_completion(
            "c-plain-fail",
            decisions=[_decision(accepted=False)],
            ci_outcomes=[_ci(CIResult.FAILED, run="run/2")],
        ),
    ]
    out = _project_dpo(
        attributed_completions,
        {"c-accept-fail": accepted_but_failed, "c-plain-fail": plain_fail},
    )
    chosen_ids = {r.metadata.chosen_completion_id for r in out.rows}
    rejected_ids = {r.metadata.rejected_completion_id for r in out.rows}
    assert "c-accept-fail" in chosen_ids
    assert "c-accept-fail" not in rejected_ids
    assert "c-plain-fail" in rejected_ids


def test_dpo_human_uses_explicit_reject_even_when_ci_passed() -> None:
    rejected_but_passed = _completion("c-reject-pass")
    plain_pass = _completion("c-plain-pass")
    attributed_completions = [
        _attributed_completion(
            "c-reject-pass",
            decisions=[_decision(accepted=False)],
            ci_outcomes=[_ci(CIResult.PASSED)],
        ),
        _attributed_completion(
            "c-plain-pass",
            decisions=[_decision(accepted=True)],
            ci_outcomes=[_ci(CIResult.PASSED, run="run/2")],
        ),
    ]
    out = _project_dpo(
        attributed_completions,
        {"c-reject-pass": rejected_but_passed, "c-plain-pass": plain_pass},
    )
    chosen_ids = {r.metadata.chosen_completion_id for r in out.rows}
    rejected_ids = {r.metadata.rejected_completion_id for r in out.rows}
    assert "c-reject-pass" in rejected_ids
    assert "c-reject-pass" not in chosen_ids
    assert "c-plain-pass" in chosen_ids


def test_implicit_decisions_do_not_drive_classification_only_ci_and_explicit_do() -> (
    None
):
    # An implicit accept is not a real human gesture (CONTEXT.md) -- CI still
    # decides this completion's chosen/rejected side.
    implicit_accept_but_ci_failed = _completion("c-implicit")
    plain_pass = _completion("c-pass")
    attributed_completions = [
        _attributed_completion(
            "c-implicit",
            decisions=[_decision(accepted=True, explicit=False)],
            ci_outcomes=[_ci(CIResult.FAILED)],
        ),
        _attributed_completion("c-pass", ci_outcomes=[_ci(CIResult.PASSED)]),
    ]
    out = project_dpo(
        attributed_completions,
        {"c-implicit": implicit_accept_but_ci_failed, "c-pass": plain_pass},
    )
    rejected_ids = {r.metadata.rejected_completion_id for r in out.rows}
    assert "c-implicit" in rejected_ids


def test_per_bucket_cap_limits_emitted_pairs_and_logs_a_capped_bucket(caplog) -> None:
    completions = {}
    attributed_completions = []
    for i in range(3):
        cid = f"c-pass-{i}"
        completions[cid] = _completion(cid)
        attributed_completions.append(
            _attributed_completion(cid, ci_outcomes=[_ci(CIResult.PASSED, run=f"p{i}")])
        )
    for i in range(3):
        cid = f"c-fail-{i}"
        completions[cid] = _completion(cid)
        attributed_completions.append(
            _attributed_completion(cid, ci_outcomes=[_ci(CIResult.FAILED, run=f"f{i}")])
        )

    policy = DPOPolicy(recipe_id="dpo_outcome", max_pairs_per_bucket=2)
    with caplog.at_level("WARNING", logger="sediment.export.dpo"):
        out = project_dpo(attributed_completions, completions, policy)

    assert len(out.rows) == 2
    assert out.skipped["bucket_capped"] == 1
    assert any("dpo_bucket_capped" in r.message for r in caplog.records)


def test_bucket_at_exact_cap_is_not_reported_as_capped(caplog) -> None:
    passed = _completion("c-pass")
    failed = _completion("c-fail")
    attributed_completions = [
        _attributed_completion("c-pass", ci_outcomes=[_ci(CIResult.PASSED)]),
        _attributed_completion("c-fail", ci_outcomes=[_ci(CIResult.FAILED)]),
    ]

    with caplog.at_level("WARNING", logger="sediment.export.dpo"):
        out = project_dpo(
            attributed_completions,
            {"c-pass": passed, "c-fail": failed},
            DPOPolicy(recipe_id="dpo_outcome", max_pairs_per_bucket=1),
        )

    assert len(out.rows) == 1
    assert out.skipped["bucket_capped"] == 0
    assert not any("dpo_bucket_capped" in r.message for r in caplog.records)


def test_a_completion_appearing_in_two_attributed_completions_is_deduped_to_one_pool_slot() -> (
    None
):
    multi = _completion("c-multi")
    other = _completion("c-other-fail")
    attributed_completions = [
        # Same completion, two attributed files -- one with a higher
        # resolved confidence (explicit accept) than the other (CI pass
        # alone). Dedup should keep exactly the higher-confidence one.
        _attributed_completion(
            "c-multi", decisions=[_decision(accepted=True)], file_path="a.py"
        ),
        _attributed_completion(
            "c-multi", ci_outcomes=[_ci(CIResult.PASSED)], file_path="b.py"
        ),
        _attributed_completion("c-other-fail", ci_outcomes=[_ci(CIResult.FAILED)]),
    ]
    out = project_dpo(attributed_completions, {"c-multi": multi, "c-other-fail": other})
    # Exactly one pair involving c-multi as chosen, not two (one per attributed completion).
    matches = [r for r in out.rows if r.metadata.chosen_completion_id == "c-multi"]
    assert len(matches) == 1


def test_pair_split_matches_the_shared_member_split() -> None:
    passed = _completion("c-pass")
    failed = _completion("c-fail")
    attributed_completions = [
        _attributed_completion(
            "c-pass", ci_outcomes=[_ci(CIResult.PASSED)], split="eval"
        ),
        _attributed_completion(
            "c-fail", ci_outcomes=[_ci(CIResult.FAILED)], split="eval"
        ),
    ]
    out = project_dpo(attributed_completions, {"c-pass": passed, "c-fail": failed})
    assert len(out.rows) == 1
    assert out.rows[0].metadata.split == "eval"


def test_cross_session_pair_straddling_the_holdout_lands_in_eval() -> None:
    # Two sessions, same prompt/model, one train-side and one eval-side:
    # split.py's decided multi-session rule is eval-wins — the pair is
    # emitted into eval (never skipped, never resolved train-ward), so a
    # train-side completion cannot leak into the training set through a
    # cross-session pairing while the pair's real signal is kept.
    passed = _completion("c-pass", session_id="sess-train")
    failed = _completion("c-fail", session_id="sess-eval")
    attributed_completions = [
        _attributed_completion(
            "c-pass",
            ci_outcomes=[_ci(CIResult.PASSED)],
            split="train",
            session_id="sess-train",
        ),
        _attributed_completion(
            "c-fail",
            ci_outcomes=[_ci(CIResult.FAILED)],
            split="eval",
            session_id="sess-eval",
        ),
    ]
    out = project_dpo(attributed_completions, {"c-pass": passed, "c-fail": failed})
    assert len(out.rows) == 1
    assert out.rows[0].metadata.split == "eval"
    assert out.skipped == {}


def test_dedup_never_lets_a_signal_less_attributed_completion_shadow_a_real_reject() -> (
    None
):
    # The same completion carries an explicit-reject attributed completion (confidence 0.0)
    # and a signal-less attributed completion (resolve_confidence None) whose (sha, path)
    # sorts higher. None must rank below every real confidence: keeping the
    # signal-less attributed completion would drop the completion from classification
    # entirely, silently discarding a genuine human reject.
    rejected = _completion("c-rej")
    passed = _completion("c-pass")
    attributed_completions = [
        _attributed_completion(
            "c-rej",
            decisions=[_decision(accepted=False)],
            commit_sha="a" * 40,
            file_path="a.py",
        ),
        _attributed_completion(
            "c-rej", commit_sha="f" * 40, file_path="z.py"
        ),  # no signal
        _attributed_completion("c-pass", decisions=[_decision(accepted=True)]),
    ]
    out = _project_dpo(attributed_completions, {"c-rej": rejected, "c-pass": passed})
    assert len(out.rows) == 1
    assert out.rows[0].metadata.chosen_completion_id == "c-pass"
    assert out.rows[0].metadata.rejected_completion_id == "c-rej"


def test_pair_confidence_is_the_minimum_of_the_two_members() -> None:
    accepted = _completion("c-accept")
    rejected = _completion("c-reject")
    attributed_completions = [
        _attributed_completion("c-accept", decisions=[_decision(accepted=True)]),
        _attributed_completion("c-reject", decisions=[_decision(accepted=False)]),
    ]
    policy = DPOPolicy(label_confidence=LabelConfidencePolicy())
    out = project_dpo(
        attributed_completions,
        {"c-accept": accepted, "c-reject": rejected},
        policy,
    )
    assert len(out.rows) == 1
    expected = min(
        policy.label_confidence.explicit_accept_confidence,
        policy.label_confidence.explicit_reject_confidence,
    )
    assert out.rows[0].metadata.label_confidence == pytest.approx(expected)
    assert out.rows[0].metadata.label_confidence == pytest.approx(expected)
    assert (
        out.rows[0].metadata.label_confidence == out.rows[0].metadata.label_confidence
    )


def test_near_tie_pair_has_high_label_confidence_but_low_margin() -> None:
    accepted = _completion("c-accept-near")
    rejected = _completion("c-reject-near")
    attributed_completions = [
        _attributed_completion("c-accept-near", decisions=[_decision(accepted=True)]),
        _attributed_completion("c-reject-near", decisions=[_decision(accepted=False)]),
    ]
    policy = DPOPolicy(
        label_confidence=LabelConfidencePolicy(
            explicit_accept_confidence=0.95,
            explicit_reject_confidence=0.90,
        )
    )

    out = project_dpo(
        attributed_completions,
        {"c-accept-near": accepted, "c-reject-near": rejected},
        policy,
    )

    assert len(out.rows) == 1
    pair = out.rows[0]
    assert pair.metadata.label_confidence == pytest.approx(0.90)
    assert pair.metadata.confidence_margin == pytest.approx(0.05)


def test_clear_preference_pair_has_low_label_confidence_but_high_margin() -> None:
    accepted = _completion("c-accept-clear")
    rejected = _completion("c-reject-clear")
    attributed_completions = [
        _attributed_completion("c-accept-clear", decisions=[_decision(accepted=True)]),
        _attributed_completion("c-reject-clear", decisions=[_decision(accepted=False)]),
    ]
    policy = DPOPolicy(
        label_confidence=LabelConfidencePolicy(
            explicit_accept_confidence=0.95,
            explicit_reject_confidence=0.10,
        )
    )

    out = project_dpo(
        attributed_completions,
        {"c-accept-clear": accepted, "c-reject-clear": rejected},
        policy,
    )

    assert len(out.rows) == 1
    pair = out.rows[0]
    assert pair.metadata.label_confidence == pytest.approx(0.10)
    assert pair.metadata.confidence_margin == pytest.approx(0.85)


def test_dpo_rows_round_trip_through_jsonl(tmp_path) -> None:
    from sediment_export import write_jsonl

    passed = _completion(
        "c-pass", output="def fib(n): return n if n < 2 else fib(n-1) + fib(n-2)"
    )
    failed = _completion("c-fail", output="def fib(n): return 0")
    attributed_completions = [
        _attributed_completion("c-pass", ci_outcomes=[_ci(CIResult.PASSED)]),
        _attributed_completion("c-fail", ci_outcomes=[_ci(CIResult.FAILED)]),
    ]
    out = project_dpo(attributed_completions, {"c-pass": passed, "c-fail": failed})
    export_rows = dpo_to_export_rows(out.rows)
    assert all(isinstance(r, ExportRow) for r in export_rows)

    result = write_jsonl(export_rows, tmp_path / "dpo.jsonl", split_enabled=False)
    written_path = tmp_path / "dpo.jsonl"
    assert str(written_path) in result.written

    import json

    lines = written_path.read_text().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["prompt"] == [{"role": "user", "content": "write fib"}]
    assert row["chosen"] == [
        {
            "role": "assistant",
            "content": "def fib(n): return n if n < 2 else fib(n-1) + fib(n-2)",
        }
    ]
    assert row["rejected"] == [{"role": "assistant", "content": "def fib(n): return 0"}]
    assert row["tools"] == []
    assert row["metadata"]["chosen_completion_id"] == "c-pass"
    assert row["metadata"]["rejected_completion_id"] == "c-fail"
    assert row["metadata"]["label_confidence"] == pytest.approx(0.42)
    assert row["metadata"]["confidence_margin"] == pytest.approx(0.24)
    assert "confidence" not in row
    assert "reliability" not in row


def test_dpo_row_contains_trl_completions_and_metadata_only() -> None:
    chosen_call = _completion("c-pass")
    chosen_call.output_messages = [
        InferenceMessage(
            role="assistant",
            parts=[
                ReasoningPart(content="Use the existing helper."),
                TextPart(content="Implemented the fix."),
            ],
        )
    ]
    rejected_call = _completion("c-fail")
    rejected_call.output_messages = [
        InferenceMessage(
            role="assistant",
            parts=[
                ReasoningPart(content="Replace the whole module."),
                TextPart(content="Changed unrelated code."),
            ],
        )
    ]
    chosen = _attributed_completion("c-pass", ci_outcomes=[_ci(CIResult.PASSED)])
    rejected = _attributed_completion("c-fail", ci_outcomes=[_ci(CIResult.FAILED)])

    [pair] = project_dpo(
        [chosen, rejected], {"c-pass": chosen_call, "c-fail": rejected_call}
    ).rows
    [export_row] = dpo_to_export_rows([pair])

    assert export_row.body == {
        "prompt": [{"role": "user", "content": "write fib"}],
        "chosen": [
            {
                "role": "assistant",
                "thinking": "Use the existing helper.",
                "content": "Implemented the fix.",
            }
        ],
        "rejected": [
            {
                "role": "assistant",
                "thinking": "Replace the whole module.",
                "content": "Changed unrelated code.",
            }
        ],
        "tools": [],
        "metadata": {
            "org_id": ORG,
            "source_model": MODEL,
            "chosen_completion_id": "c-pass",
            "rejected_completion_id": "c-fail",
            "recipe_id": "dpo_outcome",
            "recipe_version": 2,
            "chosen_label_source": "resolved_ci_pass",
            "rejected_label_source": "resolved_ci_fail",
            "label_confidence": pytest.approx(0.42),
            "ci_reliability": pytest.approx(1.0),
            "confidence_margin": pytest.approx(0.24),
            "provenance": {
                "chosen": {
                    "policy_version": "1",
                    "quarantine_revision": 0,
                    "policy_digest": None,
                },
                "rejected": {
                    "policy_version": "1",
                    "quarantine_revision": 0,
                    "policy_digest": None,
                },
            },
            "split": "train",
            "schema_id": ("https://sediment.so/schemas/training-rows/dpo-pair/v4.json"),
            "schema_version": 4,
            "chosen_repository_identity": None,
            "rejected_repository_identity": None,
            "chosen_attribution_source": "git_notes",
            "rejected_attribution_source": "git_notes",
            "chosen_session_commit_observation_ids": (),
            "rejected_session_commit_observation_ids": (),
        },
    }
    assert "Use the existing helper." not in export_row.body["chosen"][0]["content"]


def test_dpo_shuffled_inputs_write_identical_jsonl(tmp_path) -> None:
    from sediment_export import write_jsonl

    passed = _attributed_completion("c-pass", ci_outcomes=[_ci(CIResult.PASSED)])
    failed = _attributed_completion("c-fail", ci_outcomes=[_ci(CIResult.FAILED)])
    inference_calls = {
        "c-pass": _completion("c-pass"),
        "c-fail": _completion("c-fail"),
    }

    forward = project_dpo([passed, failed], inference_calls)
    shuffled = project_dpo([failed, passed], inference_calls)
    assert forward == shuffled

    forward_path = tmp_path / "forward.jsonl"
    shuffled_path = tmp_path / "shuffled.jsonl"
    write_jsonl(dpo_to_export_rows(forward.rows), forward_path, split_enabled=False)
    write_jsonl(dpo_to_export_rows(shuffled.rows), shuffled_path, split_enabled=False)
    assert forward_path.read_bytes() == shuffled_path.read_bytes()


def test_dpo_policy_rejects_a_non_positive_cap() -> None:
    with pytest.raises(ValueError):
        DPOPolicy(max_pairs_per_bucket=0)


@pytest.mark.parametrize("recipe", ["dpo_human", "dpo_outcome"])
def test_dpo_v2_keeps_both_selected_members_source_metadata(recipe):
    from dataclasses import replace
    from sediment_core import SessionCommitObservation

    fact = SessionCommitObservation(
        observation_id="chosen-observation",
        org_id=ORG,
        session_id="sess-1",
        repo=REPO,
        commit_sha=SHA,
        source_push_id="push",
    )
    chosen = replace(
        _attributed_completion(
            "chosen",
            decisions=[_decision(accepted=True)],
            ci_outcomes=[_ci(CIResult.PASSED)],
        ),
        attribution_source=AttributionSource.JACCARD,
        session_commit_observations=(fact,),
    )
    rejected = _attributed_completion(
        "rejected",
        decisions=[_decision(accepted=False)],
        ci_outcomes=[_ci(CIResult.FAILED)],
    )
    result = _project_dpo(
        [rejected, chosen],
        {key: _completion(key) for key in ("chosen", "rejected")},
        DPOPolicy(recipe_id=recipe),
    )
    [row] = result.rows
    assert row.metadata.recipe_version == 2
    assert row.metadata.chosen_attribution_source == AttributionSource.JACCARD
    assert row.metadata.rejected_attribution_source == AttributionSource.GIT_NOTES
    assert row.metadata.chosen_session_commit_observation_ids == ("chosen-observation",)
    assert row.metadata.rejected_session_commit_observation_ids == ()
    assert "session_commit_unobserved" not in result.skipped


@pytest.mark.parametrize(
    "bad_sides", [("chosen",), ("rejected",), ("chosen", "rejected")]
)
def test_dpo_unrepresentable_members_decline_one_pair(bad_sides):
    calls = {side: _completion(side) for side in ("chosen", "rejected")}
    for side in bad_sides:
        calls[side] = calls[side].model_copy(
            update={"output_messages": [message("assistant", "\ud800")]}
        )
    members = [
        _attributed_completion(side, decisions=[_decision(accepted=side == "chosen")])
        for side in calls
    ]
    for ordered in (members, list(reversed(members))):
        out = project_dpo(ordered, calls, DPOPolicy())
        assert out.rows == []
        assert dict(out.skipped) == {"unrepresentable_unicode": 1}


def test_dpo_metadata_representation_declines_pair():
    calls = {side: _completion(side) for side in ("chosen", "rejected")}
    members = [
        _attributed_completion(side, decisions=[_decision(accepted=side == "chosen")])
        for side in calls
    ]
    members[0] = replace(
        members[0],
        provenance=Provenance(policy_version="v\ud800", quarantine_revision=0),
    )
    out = project_dpo(members, calls, DPOPolicy())
    assert out.rows == []
    assert dict(out.skipped) == {"unrepresentable_unicode": 1}


@pytest.mark.parametrize(
    "reason,bad",
    [("non_finite_number", float("nan")), ("unrepresentable_unicode", "\ud800")],
)
@pytest.mark.parametrize("recipe", ["dpo_human", "dpo_outcome"])
def test_dpo_nested_completion_decline_retains_valid_sibling_pair(reason, bad, recipe):
    from export_factories import tool_call

    calls = {side: _completion(side) for side in ("chosen", "bad", "valid")}
    calls["bad"] = calls["bad"].model_copy(
        update={
            "output_messages": [
                InferenceMessage(
                    role="assistant",
                    parts=[tool_call("t", "run", {"nested": [bad, bad]})],
                )
            ]
        }
    )
    members = [
        _attributed_completion(
            side,
            decisions=[_decision(accepted=side == "chosen")],
            ci_outcomes=[
                _ci(CIResult.PASSED if side == "chosen" else CIResult.FAILED, run=side)
            ],
        )
        for side in calls
    ]
    results = [
        project_dpo(ordered, calls, DPOPolicy(recipe_id=recipe))
        for ordered in (members, members[::-1], members)
    ]
    assert results[0] == results[1] == results[2]
    assert len(results[0].rows) == 1
    assert results[0].rows[0].metadata.rejected_completion_id == "valid"
    assert dict(results[0].skipped) == {reason: 1}


def test_dpo_prompt_surrogate_is_declined_without_serialization_error():
    calls = {
        side: _completion(side, messages=[message("user", "\ud800")])
        for side in ("chosen", "rejected")
    }
    members = [
        _attributed_completion(side, decisions=[_decision(accepted=side == "chosen")])
        for side in calls
    ]
    out = project_dpo(members, calls, DPOPolicy())
    assert out.rows == []
    assert dict(out.skipped) == {"unrepresentable_unicode": 1}


@pytest.mark.parametrize("exceptional", [False, True])
def test_dpo_heterogeneous_prompts_and_distinct_nans_are_counted(exceptional):
    from export_factories import tool_call

    calls = {}
    members = []
    for index, value in enumerate(["text", float("nan") if exceptional else 42]):
        for side in ("chosen", "rejected"):
            call_id = f"{index}-{side}"
            # Distinct NaN instances must identify the same excluded pair.
            argument = float("nan") if exceptional and index else value
            calls[call_id] = _completion(
                call_id,
                messages=[
                    InferenceMessage(
                        role="assistant",
                        parts=[tool_call("t", "run", {"nested": argument})],
                    )
                ],
            )
            members.append(
                _attributed_completion(
                    call_id, decisions=[_decision(accepted=side == "chosen")]
                )
            )
    first = project_dpo(members, calls, DPOPolicy())
    assert first == project_dpo(members[::-1], calls, DPOPolicy())
    assert len(first.rows) == (1 if exceptional else 2)
    assert dict(first.skipped) == ({"non_finite_number": 1} if exceptional else {})


def test_dpo_cap_counts_candidate_refusal_and_logs_actual_emission(caplog):
    calls = {side: _completion(side) for side in ("chosen", "bad", "valid")}
    calls["bad"] = calls["bad"].model_copy(
        update={"output_messages": [message("assistant", "\ud800")]}
    )
    members = [
        _attributed_completion(side, decisions=[_decision(accepted=side == "chosen")])
        for side in calls
    ]
    out = project_dpo(members, calls, DPOPolicy(max_pairs_per_bucket=1))
    assert out.rows == []
    assert dict(out.skipped) == {"unrepresentable_unicode": 1, "bucket_capped": 1}
    record = next(
        record for record in caplog.records if record.message == "dpo_bucket_capped"
    )
    assert record.emitted == 0


@pytest.mark.parametrize("recipe", ["dpo_human", "dpo_outcome"])
def test_identical_responses_decline_one_pair_without_changing_evidence(recipe):
    from copy import deepcopy

    calls = {
        side: _completion(side).model_copy(
            update={"output_messages": [message("assistant", "def fib(): return 1")]}
        )
        for side in ("chosen", "rejected")
    }
    members = [
        _attributed_completion(
            side,
            decisions=[_decision(accepted=side == "chosen")],
            ci_outcomes=[_ci(CIResult.PASSED if side == "chosen" else CIResult.FAILED)],
        )
        for side in calls
    ]
    original = deepcopy((members, calls))
    result = _project_dpo(members, calls, DPOPolicy(recipe_id=recipe))
    assert result.rows == []
    assert dict(result.skipped) == {"identical_responses": 1}
    assert (members, calls) == original


def _contrast_inputs(chosen_messages, rejected_messages):
    calls = {
        side: _completion(side).model_copy(update={"output_messages": messages})
        for side, messages in (
            ("chosen", chosen_messages),
            ("rejected", rejected_messages),
        )
    }
    members = [
        _attributed_completion(
            side,
            decisions=[_decision(accepted=side == "chosen")],
            ci_outcomes=[_ci(CIResult.PASSED if side == "chosen" else CIResult.FAILED)],
        )
        for side in calls
    ]
    return members, calls


@pytest.mark.parametrize("recipe", ["dpo_human", "dpo_outcome"])
def test_distinct_responses_preserve_exact_payload_and_versioned_evidence(recipe):
    members, calls = _contrast_inputs(
        [
            message(
                "assistant", "def fib(n): return n if n < 2 else fib(n-1) + fib(n-2)"
            )
        ],
        [message("assistant", "def fib(n): return 0")],
    )
    result = _project_dpo(members, calls, DPOPolicy(recipe_id=recipe))
    [row] = result.rows
    assert row.chosen == [
        {
            "role": "assistant",
            "content": "def fib(n): return n if n < 2 else fib(n-1) + fib(n-2)",
        }
    ]
    assert row.rejected == [{"role": "assistant", "content": "def fib(n): return 0"}]
    assert row.metadata.recipe_id == recipe
    assert row.metadata.recipe_version == 2
    assert row.metadata.schema_version == 4
    assert (
        row.metadata.schema_id
        == "https://sediment.so/schemas/training-rows/dpo-pair/v4.json"
    )
    assert row.metadata.chosen_completion_id == "chosen"
    assert row.metadata.rejected_completion_id == "rejected"
    assert row.metadata.chosen_label_source == (
        "explicit_accept" if recipe == "dpo_human" else "resolved_ci_pass"
    )
    assert row.metadata.rejected_label_source == (
        "explicit_reject" if recipe == "dpo_human" else "resolved_ci_fail"
    )
    assert row.metadata.provenance.chosen == members[0].provenance
    assert row.metadata.provenance.rejected == members[1].provenance
    assert dict(result.skipped) == {}


@pytest.mark.parametrize("kind", ["text_parts", "dictionary_order"])
def test_equal_mapped_responses_ignore_native_segmentation_and_key_order(kind):
    from export_factories import tool_call

    if kind == "text_parts":
        chosen = [message("assistant", "ab")]
        rejected = [
            InferenceMessage(
                role="assistant", parts=[TextPart(content="a"), TextPart(content="b")]
            )
        ]
    else:
        chosen = [
            InferenceMessage(
                role="assistant",
                parts=[tool_call("t", "run", {"a": 1, "b": {"x": 2, "y": 3}})],
            )
        ]
        rejected = [
            InferenceMessage(
                role="assistant",
                parts=[tool_call("t", "run", {"b": {"y": 3, "x": 2}, "a": 1})],
            )
        ]
    members, calls = _contrast_inputs(chosen, rejected)
    result = _project_dpo(members, calls)
    assert result.rows == []
    assert dict(result.skipped) == {"identical_responses": 1}


@pytest.mark.parametrize(
    "difference",
    [
        "whitespace",
        "unicode",
        "reasoning",
        "later_message",
        "message_order",
        "tool_id",
        "tool_name",
        "nested_argument",
        "list_order",
        "bool_int",
        "int_float",
    ],
)
def test_complete_response_differences_remain_eligible(difference):
    from export_factories import tool_call

    chosen = [message("assistant", "answer")]
    rejected = [message("assistant", "answer")]
    if difference == "whitespace":
        rejected = [message("assistant", "answer ")]
    elif difference == "unicode":
        chosen = [message("assistant", "é")]
        rejected = [message("assistant", "e\u0301")]
    elif difference == "reasoning":
        chosen = [
            InferenceMessage(
                role="assistant",
                parts=[ReasoningPart(content="proof"), TextPart(content="answer")],
            )
        ]
    elif difference == "later_message":
        rejected.append(message("assistant", "correction"))
    elif difference == "message_order":
        chosen.append(message("assistant", "correction"))
        rejected = list(reversed(chosen))
    else:
        left, right = {"nested": [1, 2]}, {"nested": [1, 2]}
        if difference == "nested_argument":
            right = {"nested": [1, 3]}
        elif difference == "list_order":
            right = {"nested": [2, 1]}
        elif difference == "bool_int":
            left, right = {"nested": [True]}, {"nested": [1]}
        elif difference == "int_float":
            left, right = {"nested": [1]}, {"nested": [1.0]}
        chosen = [
            InferenceMessage(role="assistant", parts=[tool_call("t", "run", left)])
        ]
        rejected = [
            InferenceMessage(
                role="assistant",
                parts=[
                    tool_call(
                        "other" if difference == "tool_id" else "t",
                        "inspect" if difference == "tool_name" else "run",
                        right,
                    )
                ],
            )
        ]
    members, calls = _contrast_inputs(chosen, rejected)
    result = _project_dpo(members, calls)
    assert len(result.rows) == 1
    assert dict(result.skipped) == {}


@pytest.mark.parametrize(
    "location,bad,reason",
    [
        ("content", float("nan"), "non_finite_number"),
        ("content", float("inf"), "non_finite_number"),
        ("content", "\ud800", "unrepresentable_unicode"),
        ("metadata", "\ud800", "unrepresentable_unicode"),
    ],
)
def test_invalid_representation_precedes_apparent_response_equality(
    bad, reason, location
):
    from export_factories import tool_call

    output = [message("assistant", "same")]
    if location == "content":
        output = [
            InferenceMessage(
                role="assistant", parts=[tool_call("t", "run", {"nested": [bad]})]
            )
        ]
    members, calls = _contrast_inputs(output, output)
    if location == "metadata":
        members[0] = replace(
            members[0], provenance=Provenance(policy_version=bad, quarantine_revision=0)
        )
    result = _project_dpo(members, calls)
    assert result.rows == []
    assert dict(result.skipped) == {reason: 1}


def test_equal_candidate_consumes_cap_without_comparing_later_pair(monkeypatch, caplog):
    import sediment_export.dpo as dpo

    members, calls = _contrast_inputs(
        [message("assistant", "same")], [message("assistant", "same")]
    )
    calls["z-rejected"] = _completion("z-rejected").model_copy(
        update={"output_messages": [message("assistant", "different")]}
    )
    members.append(
        _attributed_completion("z-rejected", decisions=[_decision(accepted=False)])
    )
    compared = []
    original = dpo._response_key

    def recording_key(response):
        compared.append(response)
        return original(response)

    monkeypatch.setattr(dpo, "_response_key", recording_key)
    result = _project_dpo(members, calls, DPOPolicy(max_pairs_per_bucket=1))
    assert result.rows == []
    assert dict(result.skipped) == {"identical_responses": 1, "bucket_capped": 1}
    assert compared == [[{"role": "assistant", "content": "same"}]] * 2
    record = next(
        record for record in caplog.records if record.message == "dpo_bucket_capped"
    )
    assert (record.possible_pairs, record.cap, record.emitted) == (2, 1, 0)


def test_mixed_response_projection_preserves_order_split_and_jsonl_bytes(tmp_path):
    from sediment_export import write_jsonl

    members, calls = _contrast_inputs(
        [message("assistant", "same")], [message("assistant", "same")]
    )
    calls["z-rejected"] = _completion("z-rejected").model_copy(
        update={"output_messages": [message("assistant", "different")]}
    )
    members.append(
        _attributed_completion(
            "z-rejected",
            decisions=[_decision(accepted=False)],
            split="eval",
            session_id="evaluation",
        )
    )
    results = []
    for index, (inputs, lookup) in enumerate(
        (
            (members, calls),
            (members[::-1], dict(reversed(list(calls.items())))),
            (members, calls),
        )
    ):
        result = _project_dpo(inputs, lookup)
        assert dict(result.skipped) == {"identical_responses": 1}
        [row] = result.rows
        assert row.metadata.split == "eval"
        assert row.rejected == [{"role": "assistant", "content": "different"}]
        path = tmp_path / f"{index}.jsonl"
        write_jsonl(dpo_to_export_rows(result.rows), path, split_enabled=False)
        results.append((result, path.read_bytes()))
    assert results[0] == results[1] == results[2]
