# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for DPO/SFT dataset diagnostics."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

import pytest

from sediment_core import (
    CIOutcome,
    CIProvider,
    CIResult,
    AgentHarness,
    DeveloperDecision,
    InteractionMode,
    InferenceCall,
    InferenceMessage,
)
from sediment_derive import (
    AttributionSource,
    EditFate,
    Fate,
    FateResult,
    Provenance,
    SessionAbandonment,
)

from sediment_export import (
    AbandonmentSummary,
    DPOMetadata,
    DPOPair,
    DPOPolicy,
    DPOProvenance,
    SFTMetadata,
    SFTPolicy,
    SFTSample,
    FateDiagnostic,
    build_dataset_diagnostics,
    build_dpo_bucket_sparsity,
    project_dpo,
)
from sediment_export.attributed_completions import AttributedCompletion
from sediment_export.trainer import (
    TRAINING_REPRESENTATION_SKIP_REASONS,
    TrainerMappingError,
    map_inference_call,
)
from export_factories import inference_call, message, tool_call

ORG = "acme-corp"
PROMPT = [{"role": "user", "content": "write fib"}]
OTHER_PROMPT = [{"role": "user", "content": "write sort"}]
REPO = "acme-corp/backend-service"
SHA = "a" * 40


def _dpo_pair(
    row_id: str,
    *,
    model: str,
    label_confidence: float,
    confidence_margin: float,
    prompt: list[dict] = PROMPT,
    split: str = "train",
) -> DPOPair:
    return DPOPair(
        prompt=prompt,
        chosen=[{"role": "assistant", "content": "chosen"}],
        rejected=[{"role": "assistant", "content": "rejected"}],
        tools=[],
        metadata=DPOMetadata(
            org_id=ORG,
            source_model=model,
            chosen_completion_id=f"{row_id}-chosen",
            rejected_completion_id=f"{row_id}-rejected",
            recipe_id="dpo_human",
            recipe_version=2,
            chosen_label_source="explicit_accept",
            rejected_label_source="explicit_reject",
            label_confidence=label_confidence,
            ci_reliability=None,
            confidence_margin=confidence_margin,
            provenance=DPOProvenance(
                chosen=Provenance(policy_version="1", quarantine_revision=0),
                rejected=Provenance(policy_version="1", quarantine_revision=0),
            ),
            split=split,
        ),
    )


def _sft_sample(
    inference_call_id: str,
    *,
    model: str,
    confidence: float,
    prompt: list[dict] = PROMPT,
    split: str = "train",
) -> SFTSample:
    return SFTSample(
        prompt=prompt,
        completion=[{"role": "assistant", "content": "def fib(): ..."}],
        tools=[],
        metadata=SFTMetadata(
            org_id=ORG,
            source_model=model,
            completion_id=inference_call_id,
            recipe_id="sft_curated",
            recipe_version=1,
            eligibility_source="explicit_accept",
            label_confidence=confidence,
            ci_reliability=None,
            provenance=Provenance(policy_version="1", quarantine_revision=0),
            split=split,
        ),
    )


def _completion(
    inference_call_id: str, *, model: str, prompt: str = "write fib"
) -> InferenceCall:
    return inference_call(
        inference_call_id=inference_call_id,
        org_id=ORG,
        session_id=f"session-{inference_call_id}",
        model=model,
        input_messages=[message("user", prompt)],
        output=f"def fib(): return {inference_call_id!r}",
    )


def _unrepresentable_completion(
    inference_call_id: str,
    *,
    model: str = "model-a",
    prompt: str = "write fib",
    output_messages: list[InferenceMessage] | None = None,
) -> InferenceCall:
    """An inference call that passes the prompt/model pre-filters but raises
    ``TrainerMappingError`` in ``map_inference_call`` (empty ``output_messages``
    -> ``completionless`` by default), mirroring the inputs ``project_dpo``
    skips before bucketing but the buggy sparsity loop admitted."""
    call = _completion(inference_call_id, model=model, prompt=prompt)
    return call.model_copy(
        update={"output_messages": output_messages or [], "output_tokens": 0}
    )


def _representation_skip_completion(
    inference_call_id: str,
    *,
    value: object,
    model: str = "model-a",
    prompt: str = "write fib",
) -> InferenceCall:
    """An inference call whose completion maps structurally but fails
    ``validate_training_representation``: an exceptional scalar (NaN or lone
    surrogate) nested in a tool-call argument. ``map_inference_call`` raises a
    ``TrainerMappingError`` whose reason is in
    ``TRAINING_REPRESENTATION_SKIP_REASONS`` (``non_finite_number`` or
    ``unrepresentable_unicode``). Unlike ``_unrepresentable_completion`` (a
    structural ``completionless``/``empty_message`` reason the projector drops
    at the gate), ``project_dpo`` retains this member in the chosen/rejected
    pool until a pair is selected, so the sparsity report must retain it too."""
    return inference_call(
        inference_call_id=inference_call_id,
        org_id=ORG,
        session_id=f"session-{inference_call_id}",
        model=model,
        input_messages=[message("user", prompt)],
        output="ok",
        tool_calls=[tool_call("tc1", "run", {"nested": [value]})],
    )


def _decision(
    *,
    accepted: bool,
    explicit: bool = True,
    session_id: str = "sess-1",
    call_id: str | None = None,
) -> DeveloperDecision:
    return DeveloperDecision(
        org_id=ORG,
        session_id=session_id,
        user_id="dev",
        agent_harness=AgentHarness.CLAUDE_CODE,
        file_path="a.py",
        accepted=accepted,
        explicit=explicit,
        interaction_mode=InteractionMode.AGENT,
        call_id=call_id,
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _ci(result: CIResult, *, commit_sha: str = SHA) -> CIOutcome:
    return CIOutcome(
        org_id=ORG,
        provider=CIProvider.GITHUB_ACTIONS,
        run_id=f"run/{commit_sha}/{result}",
        repo=REPO,
        commit_sha=commit_sha,
        branch="main",
        result=result,
        run_url=f"https://ci.example/{commit_sha}",
    )


def _attributed_completion(
    inference_call_id: str,
    *,
    decisions: list[DeveloperDecision] = (),
    ci_outcomes: list[CIOutcome] = (),
    similarity_score: float = 1.0,
    attribution_source: AttributionSource = AttributionSource.GIT_NOTES,
    commit_sha: str | None = None,
) -> AttributedCompletion:
    if commit_sha is None:
        commit_sha = ci_outcomes[0].commit_sha if ci_outcomes else SHA
    return AttributedCompletion(
        org_id=ORG,
        session_id=f"session-{inference_call_id}",
        inference_call_id=inference_call_id,
        repo=REPO,
        commit_sha=commit_sha,
        file_path="a.py",
        similarity_score=similarity_score,
        attribution_source=attribution_source,
        decisions=list(decisions),
        ci_outcomes=list(ci_outcomes),
        provenance=Provenance(policy_version="1", quarantine_revision=0),
        split="train",
    )


def _abandoned_attributed_completion(inference_call_id: str) -> AttributedCompletion:
    session_id = f"session-{inference_call_id}"
    now = datetime(2026, 2, 1, tzinfo=UTC)
    return AttributedCompletion(
        org_id=ORG,
        session_id=session_id,
        inference_call_id=inference_call_id,
        repo=None,
        commit_sha=None,
        file_path=None,
        similarity_score=None,
        attribution_source=None,
        decisions=[_decision(accepted=True, session_id=session_id)],
        ci_outcomes=[],
        provenance=Provenance(policy_version="3", quarantine_revision=0),
        split="train",
        abandonment=SessionAbandonment(
            org_id=ORG,
            session_id=session_id,
            accepted_decisions=1,
            explicit_accepted_decisions=1,
            last_decision_at=now,
            as_of=now,
            provenance=Provenance(policy_version="2", quarantine_revision=0),
        ),
    )


def _bucket_fixtures(
    bucket_outcomes: list[tuple[str, list[bool]]],
) -> tuple[dict[str, InferenceCall], list[AttributedCompletion]]:
    """One DPO prompt bucket per scenario name, one accept/reject per label."""
    completions: dict[str, InferenceCall] = {}
    attributed_completions: list[AttributedCompletion] = []
    for bucket_name, labels in bucket_outcomes:
        for index, accepted in enumerate(labels):
            inference_call_id = f"{bucket_name}-{index}"
            completions[inference_call_id] = _completion(
                inference_call_id, model="model-a", prompt=f"prompt:{bucket_name}"
            )
            attributed_completions.append(
                _attributed_completion(
                    inference_call_id, decisions=[_decision(accepted=accepted)]
                )
            )
    return completions, attributed_completions


def test_model_balance_counts_dpo_pairs_and_sft_samples_by_model() -> None:
    report = build_dataset_diagnostics(
        [
            _dpo_pair(
                "dpo-1", model="model-a", label_confidence=0.8, confidence_margin=0.3
            ),
            _dpo_pair(
                "dpo-2", model="model-a", label_confidence=0.7, confidence_margin=0.2
            ),
            _dpo_pair(
                "dpo-3", model="model-b", label_confidence=0.6, confidence_margin=0.1
            ),
        ],
        [
            _sft_sample("sft-1", model="model-a", confidence=0.9),
            _sft_sample("sft-2", model="model-b", confidence=0.8),
            _sft_sample("sft-3", model="model-b", confidence=0.7),
        ],
    )

    assert [(row.dataset, row.model, row.rows) for row in report.model_balance] == [
        ("dpo", "model-a", 2),
        ("dpo", "model-b", 1),
        ("sft", "model-a", 1),
        ("sft", "model-b", 2),
    ]


def test_confidence_distribution_reports_hand_computed_values() -> None:
    report = build_dataset_diagnostics(
        [
            _dpo_pair(
                "dpo-1", model="model-a", label_confidence=0.25, confidence_margin=0.125
            ),
            _dpo_pair(
                "dpo-2", model="model-a", label_confidence=0.5, confidence_margin=0.25
            ),
            _dpo_pair(
                "dpo-3", model="model-b", label_confidence=0.75, confidence_margin=0.375
            ),
        ],
        [
            _sft_sample("sft-1", model="model-a", confidence=0.25),
            _sft_sample("sft-2", model="model-a", confidence=0.75),
        ],
    )

    by_key = {
        (row.dataset, row.metric, row.model): row.stats
        for row in report.confidence_distributions
    }

    dpo_label_confidence = by_key[("dpo", "label_confidence", "overall")]
    assert dpo_label_confidence.count == 3
    assert dpo_label_confidence.mean == 0.5
    assert dpo_label_confidence.median == 0.5
    assert dpo_label_confidence.min == 0.25
    assert dpo_label_confidence.max == 0.75

    sft_label_confidence = by_key[("sft", "label_confidence", "model-a")]
    assert sft_label_confidence.count == 2
    assert sft_label_confidence.mean == 0.5
    assert sft_label_confidence.median == 0.5


def test_cross_split_duplicate_prompt_is_reported() -> None:
    report = build_dataset_diagnostics(
        [
            _dpo_pair(
                "train",
                model="model-a",
                label_confidence=0.8,
                confidence_margin=0.2,
                prompt=[{"role": "user", "content": "Write Fib"}],
                split="train",
            ),
            _dpo_pair(
                "eval",
                model="model-a",
                label_confidence=0.7,
                confidence_margin=0.1,
                prompt=[{"role": "user", "content": "write fib"}],
                split="eval",
            ),
        ],
        [
            _sft_sample(
                "sft-train",
                model="model-a",
                confidence=0.8,
                prompt=OTHER_PROMPT,
                split="train",
            )
        ],
    )

    duplicates = {row.dataset: row for row in report.cross_split_duplicates}
    assert duplicates["dpo"].duplicate_prompt_count == 1
    assert duplicates["dpo"].examples[0].train_count == 1
    assert duplicates["dpo"].examples[0].eval_count == 1
    assert duplicates["dpo"].examples[0].train_row_ids == [
        "train-chosen>train-rejected"
    ]
    assert duplicates["dpo"].examples[0].eval_row_ids == ["eval-chosen>eval-rejected"]
    assert duplicates["sft"].duplicate_prompt_count == 0
    assert duplicates["sft"].examples == []


def test_clean_cross_split_prompts_report_zero_duplicates() -> None:
    report = build_dataset_diagnostics(
        [
            _dpo_pair(
                "train",
                model="model-a",
                label_confidence=0.8,
                confidence_margin=0.2,
                prompt=PROMPT,
                split="train",
            ),
            _dpo_pair(
                "eval",
                model="model-a",
                label_confidence=0.7,
                confidence_margin=0.1,
                prompt=OTHER_PROMPT,
                split="eval",
            ),
        ],
        [
            _sft_sample(
                "sft-train",
                model="model-a",
                confidence=0.8,
                prompt=PROMPT,
                split="train",
            ),
            _sft_sample(
                "sft-eval",
                model="model-a",
                confidence=0.7,
                prompt=OTHER_PROMPT,
                split="eval",
            ),
        ],
    )

    for duplicates in report.cross_split_duplicates:
        assert duplicates.duplicate_prompt_count == 0
        assert duplicates.examples == []


def test_near_duplicate_dpo_prompt_buckets_are_reported() -> None:
    near_a = [
        {
            "role": "user",
            "content": (
                "Write a Python function that sorts a list of integers in "
                "ascending order."
            ),
        }
    ]
    near_b = [
        {
            "role": "user",
            "content": (
                "Write a Python function that orders a list of integers in "
                "ascending order."
            ),
        }
    ]

    report = build_dataset_diagnostics(
        [
            _dpo_pair(
                "near-a",
                model="model-a",
                label_confidence=0.8,
                confidence_margin=0.2,
                prompt=near_a,
            ),
            _dpo_pair(
                "near-b",
                model="model-a",
                label_confidence=0.7,
                confidence_margin=0.1,
                prompt=near_b,
            ),
        ],
        [],
    )

    [near_misses] = report.dpo_near_duplicate_buckets
    assert near_misses.bucket_count == 2
    assert near_misses.comparable_pair_count == 1
    assert near_misses.near_miss_pair_count == 1
    assert near_misses.examples[0].similarity >= near_misses.threshold
    assert {
        near_misses.examples[0].bucket_a_row_ids[0],
        near_misses.examples[0].bucket_b_row_ids[0],
    } == {
        "near-a-chosen>near-a-rejected",
        "near-b-chosen>near-b-rejected",
    }


def test_different_task_dpo_prompt_buckets_are_not_near_duplicates() -> None:
    report = build_dataset_diagnostics(
        [
            _dpo_pair(
                "sort",
                model="model-a",
                label_confidence=0.8,
                confidence_margin=0.2,
                prompt=[
                    {
                        "role": "user",
                        "content": (
                            "Write a Python function that sorts a list of "
                            "integers in ascending order."
                        ),
                    }
                ],
            ),
            _dpo_pair(
                "oauth",
                model="model-a",
                label_confidence=0.7,
                confidence_margin=0.1,
                prompt=[
                    {
                        "role": "user",
                        "content": (
                            "Explain how OAuth refresh tokens work for a mobile client."
                        ),
                    }
                ],
            ),
        ],
        [],
    )

    [near_misses] = report.dpo_near_duplicate_buckets
    assert near_misses.bucket_count == 2
    assert near_misses.comparable_pair_count == 1
    assert near_misses.near_miss_pair_count == 0
    assert near_misses.examples == []


def test_single_dpo_prompt_bucket_has_nothing_to_compare() -> None:
    report = build_dataset_diagnostics(
        [
            _dpo_pair(
                "single",
                model="model-a",
                label_confidence=0.8,
                confidence_margin=0.2,
                prompt=PROMPT,
            )
        ],
        [],
    )

    [near_misses] = report.dpo_near_duplicate_buckets
    assert near_misses.bucket_count == 1
    assert near_misses.comparable_pair_count == 0
    assert near_misses.near_miss_pair_count == 0
    assert near_misses.examples == []


def test_confidence_floor_exclusions_are_attributed_per_model() -> None:
    attributed_completions = [
        _attributed_completion(
            "a-good", ci_outcomes=[_ci(CIResult.PASSED, commit_sha="a" * 40)]
        ),
        _attributed_completion(
            "a-failed", ci_outcomes=[_ci(CIResult.FAILED, commit_sha="b" * 40)]
        ),
        _attributed_completion(
            "a-rejected",
            decisions=[_decision(accepted=False)],
            ci_outcomes=[_ci(CIResult.PASSED, commit_sha="c" * 40)],
        ),
        _attributed_completion(
            "b-low",
            ci_outcomes=[_ci(CIResult.PASSED, commit_sha="d" * 40)],
            similarity_score=0.8,
            attribution_source=AttributionSource.JACCARD,
        ),
        _attributed_completion(
            "b-failed", ci_outcomes=[_ci(CIResult.FAILED, commit_sha="e" * 40)]
        ),
        _attributed_completion(
            "b-rejected",
            decisions=[_decision(accepted=False)],
            ci_outcomes=[_ci(CIResult.PASSED, commit_sha="f" * 40)],
        ),
    ]
    completions = {
        "a-good": _completion("a-good", model="model-a"),
        "a-failed": _completion("a-failed", model="model-a"),
        "a-rejected": _completion("a-rejected", model="model-a"),
        "b-low": _completion("b-low", model="model-b"),
        "b-failed": _completion("b-failed", model="model-b"),
        "b-rejected": _completion("b-rejected", model="model-b"),
    }

    report = build_dataset_diagnostics(
        [],
        [],
        attributed_completions=attributed_completions,
        inference_calls=completions,
        sft_policy=SFTPolicy(recipe_id="sft_verified", min_confidence=0.6),
    )

    by_model = {row.model: row for row in report.confidence_floor_exclusions}
    assert by_model["model-a"].otherwise_eligible_completions == 1
    assert by_model["model-a"].excluded_by_floor == 0
    assert by_model["model-a"].exclusion_rate == 0.0
    assert by_model["model-b"].otherwise_eligible_completions == 1
    assert by_model["model-b"].excluded_by_floor == 1
    assert by_model["model-b"].exclusion_rate == 1.0


def test_confidence_floor_exclusions_do_not_create_false_disparity() -> None:
    attributed_completions = [
        _attributed_completion(
            "a-good", ci_outcomes=[_ci(CIResult.PASSED, commit_sha="a" * 40)]
        ),
        _attributed_completion(
            "a-low",
            ci_outcomes=[_ci(CIResult.PASSED, commit_sha="b" * 40)],
            similarity_score=0.8,
            attribution_source=AttributionSource.JACCARD,
        ),
        _attributed_completion(
            "b-good", ci_outcomes=[_ci(CIResult.PASSED, commit_sha="c" * 40)]
        ),
        _attributed_completion(
            "b-low",
            ci_outcomes=[_ci(CIResult.PASSED, commit_sha="d" * 40)],
            similarity_score=0.8,
            attribution_source=AttributionSource.JACCARD,
        ),
    ]
    completions = {
        "a-good": _completion("a-good", model="model-a"),
        "a-low": _completion("a-low", model="model-a"),
        "b-good": _completion("b-good", model="model-b"),
        "b-low": _completion("b-low", model="model-b"),
    }

    report = build_dataset_diagnostics(
        [],
        [],
        attributed_completions=attributed_completions,
        inference_calls=completions,
        sft_policy=SFTPolicy(recipe_id="sft_verified", min_confidence=0.6),
    )

    rates = {
        row.model: row.exclusion_rate for row in report.confidence_floor_exclusions
    }
    assert rates == {"model-a": 0.5, "model-b": 0.5}


def test_confidence_floor_exclusions_are_deterministic_under_shuffled_attributed_completions() -> (
    None
):
    attributed_completions = [
        _attributed_completion(
            "a-good", ci_outcomes=[_ci(CIResult.PASSED, commit_sha="a" * 40)]
        ),
        _attributed_completion(
            "a-low",
            ci_outcomes=[_ci(CIResult.PASSED, commit_sha="b" * 40)],
            similarity_score=0.8,
            attribution_source=AttributionSource.JACCARD,
        ),
        _attributed_completion(
            "b-good", ci_outcomes=[_ci(CIResult.PASSED, commit_sha="c" * 40)]
        ),
        _attributed_completion(
            "b-low",
            ci_outcomes=[_ci(CIResult.PASSED, commit_sha="d" * 40)],
            similarity_score=0.8,
            attribution_source=AttributionSource.JACCARD,
        ),
    ]
    completions = {
        "b-low": _completion("b-low", model="model-b"),
        "a-good": _completion("a-good", model="model-a"),
        "b-good": _completion("b-good", model="model-b"),
        "a-low": _completion("a-low", model="model-a"),
    }

    forward = build_dataset_diagnostics(
        [],
        [],
        attributed_completions=attributed_completions,
        inference_calls=completions,
    )
    shuffled = build_dataset_diagnostics(
        [],
        [],
        attributed_completions=list(reversed(attributed_completions)),
        inference_calls=completions,
    )

    assert forward.confidence_floor_exclusions == shuffled.confidence_floor_exclusions


def test_dpo_bucket_sparsity_reports_histogram_singletons_and_cap_hits() -> None:
    completions, attributed_completions = _bucket_fixtures(
        [
            ("singleton", [True]),
            ("small-pairable", [True, False]),
            ("large-capped", [True, True, True, False, False, False]),
        ]
    )

    report = build_dpo_bucket_sparsity(
        attributed_completions,
        completions,
        DPOPolicy(max_pairs_per_bucket=3),
    )

    assert report.total_buckets == 3
    assert report.total_candidates == 9
    assert report.bucket_size_histogram == {
        "1": 1,
        "2": 1,
        "3-5": 0,
        "6-10": 1,
        "10+": 0,
    }
    assert report.singleton_candidates == 1
    assert report.singleton_candidate_fraction == pytest.approx(1 / 9)
    assert report.cap_hit_buckets == 1


def test_dpo_bucket_sparsity_reports_all_singletons_clearly() -> None:
    completions, attributed_completions = _bucket_fixtures(
        [(f"singleton-{index}", [index % 2 == 0]) for index in range(3)]
    )

    report = build_dpo_bucket_sparsity(attributed_completions, completions)

    assert report.total_buckets == 3
    assert report.total_candidates == 3
    assert report.bucket_size_histogram == {
        "1": 3,
        "2": 0,
        "3-5": 0,
        "6-10": 0,
        "10+": 0,
    }
    assert report.singleton_candidates == 3
    assert report.singleton_candidate_fraction == pytest.approx(1.0)
    assert report.cap_hit_buckets == 0


def test_dpo_bucket_sparsity_is_deterministic_over_input_order() -> None:
    completions, attributed_completions = _bucket_fixtures(
        [
            ("one", [True]),
            ("two", [True, False]),
            ("large", [True, True, False, False]),
        ]
    )

    policy = DPOPolicy(max_pairs_per_bucket=2)

    assert build_dpo_bucket_sparsity(
        attributed_completions, completions, policy
    ) == build_dpo_bucket_sparsity(
        list(reversed(attributed_completions)), completions, policy
    )


@pytest.mark.parametrize(
    "output_messages, reason",
    [
        ([], "completionless"),
        ([InferenceMessage(role="assistant", parts=[])], "empty_message"),
    ],
)
def test_dpo_bucket_sparsity_excludes_trainer_unrepresentable_completions(
    output_messages: list[InferenceMessage], reason: str
) -> None:
    """Completions ``project_dpo`` drops at the trainer-mapping gate must not
    enter sparsity buckets. One bucket holds a well-formed accepted completion
    plus a trainer-unrepresentable rejected completion; both share an identical
    prompt/model key and pass the prompt/model pre-filters, so only the
    ``map_inference_call`` gate can exclude the offending candidate."""
    completions = {
        "well": _completion("well", model="model-a", prompt="prompt:bucket"),
        "bad": _unrepresentable_completion(
            "bad",
            model="model-a",
            prompt="prompt:bucket",
            output_messages=output_messages,
        ),
    }
    attributed = [
        _attributed_completion("well", decisions=[_decision(accepted=True)]),
        _attributed_completion("bad", decisions=[_decision(accepted=False)]),
    ]

    projection = project_dpo(attributed, completions, DPOPolicy())
    assert projection.skipped[reason] == 1
    assert projection.rows == []

    report = build_dpo_bucket_sparsity(attributed, completions, DPOPolicy())
    assert report.total_buckets == 1
    assert report.total_candidates == 1
    assert report.bucket_size_histogram == {
        "1": 1,
        "2": 0,
        "3-5": 0,
        "6-10": 0,
        "10+": 0,
    }
    assert report.singleton_candidates == 1
    assert report.singleton_candidate_fraction == 1.0
    assert report.cap_hit_buckets == 0


def test_dpo_bucket_sparsity_no_false_cap_hit_from_trainer_skipped_completion() -> None:
    """A trainer-skipped completion must not flip a bucket across the cap
    threshold. Two well-formed accepted + one well-formed rejected + one
    completionless rejected share a bucket. ``project_dpo`` skips the
    completionless completion, leaving 2 chosen x 1 rejected = 2 pairs (under
    the default cap of 3); the sparsity report must therefore report zero
    cap-hit buckets. The bug reported a cap-hit because it counted the skipped
    completion, inflating the rejected pool to 2 (2x2 = 4 > 3)."""
    completions = {
        "acc1": _completion("acc1", model="model-a", prompt="prompt:bucket"),
        "acc2": _completion("acc2", model="model-a", prompt="prompt:bucket"),
        "rej1": _completion("rej1", model="model-a", prompt="prompt:bucket"),
        "rej-bad": _unrepresentable_completion(
            "rej-bad", model="model-a", prompt="prompt:bucket"
        ),
    }
    attributed = [
        _attributed_completion("acc1", decisions=[_decision(accepted=True)]),
        _attributed_completion("acc2", decisions=[_decision(accepted=True)]),
        _attributed_completion("rej1", decisions=[_decision(accepted=False)]),
        _attributed_completion("rej-bad", decisions=[_decision(accepted=False)]),
    ]
    policy = DPOPolicy(max_pairs_per_bucket=3)

    projection = project_dpo(attributed, completions, policy)
    assert projection.skipped["completionless"] == 1
    assert len(projection.rows) == 2
    assert "bucket_capped" not in projection.skipped

    report = build_dpo_bucket_sparsity(attributed, completions, policy)
    assert report.total_buckets == 1
    assert report.total_candidates == 3
    assert report.bucket_size_histogram == {
        "1": 0,
        "2": 0,
        "3-5": 1,
        "6-10": 0,
        "10+": 0,
    }
    assert report.singleton_candidates == 0
    assert report.cap_hit_buckets == 0


@pytest.mark.parametrize(
    "value, reason",
    [
        (float("nan"), "non_finite_number"),
        ("bad\ud800", "unrepresentable_unicode"),
    ],
)
def test_dpo_bucket_sparsity_retains_representation_skip_completions(
    value: object, reason: str
) -> None:
    """A completion whose ``map_inference_call`` failure is a
    ``TRAINING_REPRESENTATION_SKIP_REASONS`` reason is retained by
    ``project_dpo``'s gate for pair-level representation validation, so the
    sparsity report must retain it too. One bucket holds a well-formed accepted
    completion plus a representation-skip rejected completion sharing an
    identical prompt/model key. The buggy catch-all gate dropped the rep-skip
    member, undercounting ``total_candidates`` (2 -> 1); the fixed two-branch
    gate keeps it."""
    completions = {
        "well": _completion("well", model="model-a", prompt="prompt:bucket"),
        "bad": _representation_skip_completion(
            "bad", value=value, model="model-a", prompt="prompt:bucket"
        ),
    }
    attributed = [
        _attributed_completion("well", decisions=[_decision(accepted=True)]),
        _attributed_completion("bad", decisions=[_decision(accepted=False)]),
    ]

    # Negative control: the gate's failure is exactly a representation-skip
    # reason, which is why project_dpo (and now the sparsity report) retains it.
    with pytest.raises(TrainerMappingError) as error:
        map_inference_call(completions["bad"])
    assert error.value.reason == reason
    assert reason in TRAINING_REPRESENTATION_SKIP_REASONS

    # project_dpo retains the member, then skips the one pair containing it at
    # emission time. 1 chosen x 1 rejected = 1 pair, under the default cap.
    projection = project_dpo(attributed, completions, DPOPolicy())
    assert projection.skipped[reason] == 1
    assert projection.rows == []
    assert "bucket_capped" not in projection.skipped

    report = build_dpo_bucket_sparsity(attributed, completions, DPOPolicy())
    assert report.total_buckets == 1
    assert report.total_candidates == 2
    assert report.bucket_size_histogram == {
        "1": 0,
        "2": 1,
        "3-5": 0,
        "6-10": 0,
        "10+": 0,
    }
    assert report.singleton_candidates == 0
    assert report.singleton_candidate_fraction == 0.0
    assert report.cap_hit_buckets == 0


@pytest.mark.parametrize(
    "value, reason",
    [
        (float("nan"), "non_finite_number"),
        ("bad\ud800", "unrepresentable_unicode"),
    ],
)
def test_dpo_bucket_sparsity_counts_representation_skip_members_for_cap(
    value: object, reason: str
) -> None:
    """The cap decision in ``build_dpo_bucket_sparsity`` must mirror
    ``project_dpo``'s. Two accepted + two rejected completions share a bucket;
    one rejected completion is a representation-skip member that both
    implementations retain. ``project_dpo`` computes ``possible_pairs = 2x2 = 4
    > cap 3``, logs ``dpo_bucket_capped``, and counts ``bucket_capped``; the
    sparsity report must therefore report ``cap_hit_buckets == 1`` and
    ``total_candidates == 4``. The buggy catch-all gate dropped the rep-skip
    member, yielding ``2x1 = 2 <= 3`` and ``cap_hit_buckets == 0`` for the very
    bucket ``project_dpo`` capped."""
    completions = {
        "acc1": _completion("acc1", model="model-a", prompt="prompt:bucket"),
        "acc2": _completion("acc2", model="model-a", prompt="prompt:bucket"),
        "rej1": _completion("rej1", model="model-a", prompt="prompt:bucket"),
        "rej_bad": _representation_skip_completion(
            "rej_bad", value=value, model="model-a", prompt="prompt:bucket"
        ),
    }
    attributed = [
        _attributed_completion("acc1", decisions=[_decision(accepted=True)]),
        _attributed_completion("acc2", decisions=[_decision(accepted=True)]),
        _attributed_completion("rej1", decisions=[_decision(accepted=False)]),
        _attributed_completion("rej_bad", decisions=[_decision(accepted=False)]),
    ]
    policy = DPOPolicy(max_pairs_per_bucket=3)

    # project_dpo retains rej_bad, caps the bucket, emits the two well-formed
    # pairs, and skips the pair containing the rep-skip member.
    projection = project_dpo(attributed, completions, policy)
    assert len(projection.rows) == 2
    assert projection.skipped["bucket_capped"] == 1
    assert projection.skipped[reason] == 1

    report = build_dpo_bucket_sparsity(attributed, completions, policy)
    assert report.total_buckets == 1
    assert report.total_candidates == 4
    assert report.bucket_size_histogram == {
        "1": 0,
        "2": 0,
        "3-5": 1,
        "6-10": 0,
        "10+": 0,
    }
    assert report.singleton_candidates == 0
    assert report.cap_hit_buckets == 1


def test_abandonment_is_not_a_recipe_candidate_or_sft_floor_eligible() -> None:
    chosen = _attributed_completion("chosen", decisions=[_decision(accepted=True)])
    abandoned = _abandoned_attributed_completion("abandoned")
    completions = {
        "chosen": _completion("chosen", model="model-a"),
        "abandoned": _completion("abandoned", model="model-a"),
    }

    sparsity = build_dpo_bucket_sparsity([chosen, abandoned], completions)
    diagnostics = build_dataset_diagnostics(
        [],
        [],
        attributed_completions=[chosen, abandoned],
        inference_calls=completions,
        sft_policy=SFTPolicy(min_confidence=0.0),
        dpo_bucket_sparsity=sparsity,
    )

    [bucket_sparsity] = diagnostics.dpo_bucket_sparsity
    assert bucket_sparsity.total_candidates == 1
    assert bucket_sparsity.bucket_size_histogram["1"] == 1
    [floor] = diagnostics.confidence_floor_exclusions
    assert floor.otherwise_eligible_completions == 1
    assert floor.excluded_by_floor == 0


def test_dataset_diagnostics_carries_the_shared_abandonment_summary() -> None:
    summary = AbandonmentSummary(
        abandoned_sessions=2,
        grade_eligible_sessions=1,
        implicit_only_sessions=1,
        negative_completions=1,
        explicit_accepts_unjoined=2,
        derivation_skipped={"within_grace_horizon": 3},
        provenance=Provenance(policy_version="2", quarantine_revision=0),
    )

    report = build_dataset_diagnostics([], [], abandonment=summary)

    assert report.abandonment == summary


def test_dataset_diagnostics_summarizes_joined_fates_once() -> None:
    accepted = _decision(accepted=True, explicit=True, call_id="call-accepted")
    implicit = _decision(accepted=True, explicit=False, call_id="call-implicit")
    attributed_completions = [
        _attributed_completion("a", decisions=[accepted]),
        _attributed_completion("a", decisions=[accepted], commit_sha="b" * 40),
        _attributed_completion("b", decisions=[implicit]),
    ]
    provenance = Provenance(policy_version="1", quarantine_revision=5)
    fate_result = FateResult(
        fates=[
            Fate(
                observation_id="o-accepted",
                org_id=ORG,
                agent_harness=AgentHarness.CLAUDE_CODE,
                session_id="sess-1",
                call_id="call-accepted",
                score=0.0,
                fate=EditFate.DELETED,
                external_lines_added=0,
                external_lines_removed=0,
                provenance=provenance,
            ),
            Fate(
                observation_id="o-implicit",
                org_id=ORG,
                agent_harness=AgentHarness.CLAUDE_CODE,
                session_id="sess-1",
                call_id="call-implicit",
                score=0.5,
                fate=EditFate.PARTIALLY_MODIFIED,
                external_lines_added=1,
                external_lines_removed=0,
                provenance=provenance,
            ),
            Fate(
                observation_id="o-unmatched",
                org_id=ORG,
                agent_harness=AgentHarness.CLAUDE_CODE,
                session_id="sess-1",
                call_id="call-unmatched",
                score=1.0,
                fate=EditFate.UNMODIFIED,
                external_lines_added=None,
                external_lines_removed=None,
                provenance=provenance,
            ),
        ],
        skipped=Counter({"invalid_score": 2}),
        provenance=provenance,
    )

    report = build_dataset_diagnostics(
        [],
        [],
        attributed_completions=attributed_completions,
        fate_result=fate_result,
    )

    assert report.fate == FateDiagnostic(
        fates={"deleted": 1, "partially_modified": 1},
        explicit_accept_fates={"deleted": 1},
        fates_with_external_changes={"partially_modified": 1},
        skipped={"invalid_score": 2},
        provenance=provenance,
    )


def test_dataset_fate_diagnostic_is_independent_of_input_order() -> None:
    decision = _decision(accepted=True, call_id="call-1")
    attributed_completions = [
        _attributed_completion("a", decisions=[decision]),
        _attributed_completion("b", decisions=[decision]),
    ]
    provenance = Provenance(policy_version="1", quarantine_revision=0)
    fate_result = FateResult(
        fates=[
            Fate(
                observation_id="o-1",
                org_id=ORG,
                agent_harness=AgentHarness.CLAUDE_CODE,
                session_id="sess-1",
                call_id="call-1",
                score=1.0,
                fate=EditFate.UNMODIFIED,
                external_lines_added=None,
                external_lines_removed=None,
                provenance=provenance,
            )
        ],
        provenance=provenance,
    )

    assert build_dataset_diagnostics(
        [], [], attributed_completions=attributed_completions, fate_result=fate_result
    ) == build_dataset_diagnostics(
        [],
        [],
        attributed_completions=list(reversed(attributed_completions)),
        fate_result=fate_result,
    )


def test_dpo_diagnostics_use_recipe_owner_and_preserve_historical_strata():
    from dataclasses import replace
    from sediment_export.dpo import DPO_RECIPE_VERSION
    from sediment_export.dataset_diagnostics import DPONearDuplicateBucketReport

    assert DPO_RECIPE_VERSION == 2
    assert DPONearDuplicateBucketReport().recipe_version == DPO_RECIPE_VERSION
    for recipe in ("dpo_human", "dpo_outcome"):
        assert (
            build_dpo_bucket_sparsity(
                [], {}, DPOPolicy(recipe_id=recipe)
            ).recipe_version
            == DPO_RECIPE_VERSION
        )
    historical = _dpo_pair(
        "old", model="m", label_confidence=0.5, confidence_margin=0.5
    )
    historical = replace(
        historical,
        metadata=replace(
            historical.metadata,
            recipe_version=1,
            schema_id="https://sediment.so/schemas/training-rows/dpo-pair/v3.json",
            schema_version=3,
        ),
    )
    running = replace(
        historical,
        metadata=replace(
            historical.metadata,
            recipe_version=DPO_RECIPE_VERSION,
            schema_id="https://sediment.so/schemas/training-rows/dpo-pair/v4.json",
            schema_version=4,
        ),
    )
    report = build_dataset_diagnostics([historical, running], [])
    assert {row.recipe_version for row in report.model_balance} == {1, 2}
    assert {row.recipe_version for row in report.dpo_near_duplicate_buckets} == {1, 2}
