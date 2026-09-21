# SPDX-License-Identifier: AGPL-3.0-or-later
"""Public contracts for ADR 0011 objective-specific evidence recipes."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime

from sediment_core import (
    AgentHarness,
    CIOutcome,
    CIProvider,
    CIResult,
    DeveloperDecision,
    InteractionMode,
)
from sediment_derive import (
    RECOVERY_SKIP_REASONS,
    AttributionSource,
    Provenance,
    RecoverySample,
)
from sediment_export import (
    DIFF_SFT_SKIP_REASONS,
    DPO_SKIP_REASONS,
    DPOPolicy,
    RECOVERY_PROJECTION_SKIP_REASONS,
    SFT_SKIP_REASONS,
    SFTPolicy,
    build_dataset_diagnostics,
    build_label_confidence_inspection,
    build_label_confidence_sensitivity,
    dpo_to_export_rows,
    project_dpo,
    project_recovery,
    project_sft,
    recovery_to_export_rows,
    sft_to_export_rows,
    write_jsonl,
)
from sediment_export.attributed_completions import AttributedCompletion

from export_factories import inference_call, message

ORG = "acme-corp"
REPO = "acme-corp/service"
SHA = "a" * 40
PROMPT = [message("user", "repair the parser")]


def _call(call_id: str):
    return inference_call(
        call_id,
        org_id=ORG,
        session_id=f"session-{call_id}",
        input_messages=PROMPT,
        output=f"response {call_id}",
    )


def _decision(
    accepted: bool,
    *,
    explicit: bool = True,
    edit_retention_score: float | None = None,
) -> DeveloperDecision:
    return DeveloperDecision(
        decision_id=f"decision-{accepted}-{explicit}-{edit_retention_score}",
        org_id=ORG,
        session_id="session",
        user_id="developer",
        agent_harness=AgentHarness.CLAUDE_CODE,
        file_path="parser.py",
        accepted=accepted,
        explicit=explicit,
        interaction_mode=InteractionMode.AGENT,
        edit_retention_score=edit_retention_score,
        occurred_at=datetime(2026, 8, 21, tzinfo=UTC),
    )


def _ci(
    result: CIResult,
    *,
    run_id: str,
    run_attempt: int | None = None,
    workflow: str = "ci",
) -> CIOutcome:
    return CIOutcome(
        outcome_id=f"outcome-{run_id}-{run_attempt}-{result.value}",
        org_id=ORG,
        provider=CIProvider.GITHUB_ACTIONS,
        run_id=run_id,
        run_attempt=run_attempt,
        repo=REPO,
        commit_sha=SHA,
        branch="main",
        result=result,
        workflow_id=workflow,
        workflow_name=workflow.upper(),
        workflow_path=f".github/workflows/{workflow}.yml",
    )


def _attributed(
    call_id: str,
    *,
    decisions: list[DeveloperDecision] | None = None,
    outcomes: list[CIOutcome] | None = None,
) -> AttributedCompletion:
    return AttributedCompletion(
        org_id=ORG,
        session_id=f"session-{call_id}",
        inference_call_id=call_id,
        repo=REPO,
        commit_sha=SHA,
        file_path="parser.py",
        similarity_score=1.0,
        attribution_source=AttributionSource.GIT_NOTES,
        decisions=decisions or [],
        ci_outcomes=outcomes or [],
        provenance=Provenance(policy_version="3", quarantine_revision=0),
        split="train",
    )


def test_dpo_human_is_default_and_records_both_human_label_sources() -> None:
    chosen = _attributed("chosen", decisions=[_decision(True)])
    rejected = _attributed("rejected", decisions=[_decision(False)])

    projection = project_dpo(
        [rejected, chosen],
        {"chosen": _call("chosen"), "rejected": _call("rejected")},
    )

    [row] = projection.rows
    assert row.metadata.recipe_id == "dpo_human"
    assert row.metadata.recipe_version == 2
    assert row.metadata.chosen_label_source == "explicit_accept"
    assert row.metadata.rejected_label_source == "explicit_reject"
    body = dpo_to_export_rows([row])[0].body
    assert body["metadata"]["recipe_version"] == 2
    assert "classification" not in body["metadata"]


def test_dpo_human_never_mixes_a_human_label_with_a_ci_label() -> None:
    human_chosen = _attributed("human", decisions=[_decision(True)])
    ci_rejected = _attributed("ci", outcomes=[_ci(CIResult.FAILED, run_id="failed")])

    projection = project_dpo(
        [human_chosen, ci_rejected],
        {"human": _call("human"), "ci": _call("ci")},
    )

    assert projection.rows == []
    assert projection.skipped["no_label_source"] == 1
    assert set(projection.skipped) <= set(DPO_SKIP_REASONS)


def test_dpo_outcome_requires_clean_pass_and_clean_failure() -> None:
    chosen = _attributed("chosen", outcomes=[_ci(CIResult.PASSED, run_id="passed")])
    rejected = _attributed("rejected", outcomes=[_ci(CIResult.FAILED, run_id="failed")])

    projection = project_dpo(
        [chosen, rejected],
        {"chosen": _call("chosen"), "rejected": _call("rejected")},
        DPOPolicy(recipe_id="dpo_outcome"),
    )

    [row] = projection.rows
    assert row.metadata.recipe_id == "dpo_outcome"
    assert row.metadata.recipe_version == 2
    assert row.metadata.chosen_label_source == "resolved_ci_pass"
    assert row.metadata.rejected_label_source == "resolved_ci_fail"


def test_dpo_outcome_excludes_a_suspected_flake_and_counts_it() -> None:
    flaky = _attributed(
        "flaky",
        outcomes=[
            _ci(CIResult.FAILED, run_id="flaky", run_attempt=1),
            _ci(CIResult.PASSED, run_id="flaky", run_attempt=2),
        ],
    )
    failed = _attributed("failed", outcomes=[_ci(CIResult.FAILED, run_id="failed")])

    projection = project_dpo(
        [flaky, failed],
        {"flaky": _call("flaky"), "failed": _call("failed")},
        DPOPolicy(recipe_id="dpo_outcome"),
    )

    assert projection.rows == []
    assert projection.skipped["unreliable_ci_resolution"] == 1


def test_sft_curated_is_default_and_ci_pass_alone_cannot_create_eligibility() -> None:
    passed = _attributed("passed", outcomes=[_ci(CIResult.PASSED, run_id="passed")])

    projection = project_sft([passed], {"passed": _call("passed")})

    assert projection.rows == []
    assert projection.skipped["no_eligibility_source"] == 1


def test_sft_curated_records_explicit_accept_and_strong_retention_sources() -> None:
    accepted = _attributed("accepted", decisions=[_decision(True)])
    retained = _attributed(
        "retained",
        decisions=[
            _decision(True, explicit=False, edit_retention_score=0.9),
        ],
    )
    calls = {"accepted": _call("accepted"), "retained": _call("retained")}

    projection = project_sft([retained, accepted], calls)

    assert [row.metadata.eligibility_source for row in projection.rows] == [
        "explicit_accept",
        "edit_retention",
    ]
    assert all(row.metadata.recipe_id == "sft_curated" for row in projection.rows)
    assert all(row.metadata.recipe_version == 1 for row in projection.rows)


def test_sft_curated_vetoes_explicit_reject_and_resolved_ci_failure() -> None:
    rejected = _attributed(
        "rejected",
        decisions=[_decision(True), _decision(False)],
    )
    failed = _attributed(
        "failed",
        decisions=[_decision(True)],
        outcomes=[_ci(CIResult.FAILED, run_id="failed")],
    )
    calls = {"rejected": _call("rejected"), "failed": _call("failed")}

    projection = project_sft([rejected, failed], calls)

    assert projection.rows == []
    assert projection.skipped["explicit_reject"] == 1
    assert projection.skipped["resolved_ci_failure"] == 1


def test_sft_curated_vetoes_a_failed_workflow_when_ci_is_ambiguous() -> None:
    ambiguous = _attributed(
        "ambiguous",
        decisions=[_decision(True)],
        outcomes=[
            _ci(CIResult.PASSED, run_id="tests-pass", workflow="tests"),
            _ci(CIResult.FAILED, run_id="lint-fail", workflow="lint"),
        ],
    )

    projection = project_sft([ambiguous], {"ambiguous": _call("ambiguous")})

    assert projection.rows == []
    assert projection.skipped["ambiguous_workflow_verdicts"] == 1
    assert projection.skipped["resolved_ci_failure"] == 1


def test_sft_verified_is_opt_in_and_requires_a_clean_resolved_pass() -> None:
    passed = _attributed("passed", outcomes=[_ci(CIResult.PASSED, run_id="passed")])
    non_verdict = _attributed(
        "cancelled", outcomes=[_ci(CIResult.CANCELLED, run_id="cancelled")]
    )

    projection = project_sft(
        [non_verdict, passed],
        {"passed": _call("passed"), "cancelled": _call("cancelled")},
        SFTPolicy(recipe_id="sft_verified"),
    )

    [row] = projection.rows
    assert row.metadata.recipe_id == "sft_verified"
    assert row.metadata.recipe_version == 1
    assert row.metadata.eligibility_source == "resolved_ci_pass"
    assert projection.skipped["no_eligibility_source"] == 1
    assert sft_to_export_rows([row])[0].body["metadata"]["eligibility_source"] == (
        "resolved_ci_pass"
    )


def test_recovery_rows_name_the_versioned_ci_recipe() -> None:
    sample = RecoverySample(
        org_id=ORG,
        repo=REPO,
        branch="main",
        workflow_name="CI",
        workflow_path=".github/workflows/ci.yml",
        failed_commit_sha="a" * 40,
        fixed_commit_sha="b" * 40,
        failed_outcome_id="failed",
        fixed_outcome_id="fixed",
        recovery_diff="diff --git a/a.py b/a.py\n",
        failed_inference_call_ids=[],
        fixed_inference_call_ids=[],
        provenance=Provenance(policy_version="3", quarantine_revision=0),
    )

    [row] = project_recovery([sample], {}).rows

    assert row.recipe_id == "recovery_ci"
    assert row.recipe_version == 1
    body = recovery_to_export_rows([row])[0].body
    assert body["recipe_id"] == "recovery_ci"
    assert body["recipe_version"] == 1
    assert "classification" not in asdict(row)


def test_inspection_and_sensitivity_name_the_selected_recipe_and_source() -> None:
    accepted = _attributed("accepted", decisions=[_decision(True)])
    call = _call("accepted")

    [inspection] = build_label_confidence_inspection(
        [call], [accepted], sft_policy=SFTPolicy()
    )
    sensitivity = build_label_confidence_sensitivity(
        [accepted], grids={"explicit_accept_confidence": (1.0,)}
    )

    assert inspection.recipe_id == "sft_curated"
    assert inspection.recipe_version == 1
    assert inspection.eligibility_source == "explicit_accept"
    assert {
        (row.recipe_id, row.recipe_version, row.eligibility_source)
        for row in sensitivity
    } == {("sft_curated", 1, "explicit_accept")}


def test_dataset_diagnostics_stratify_recipes_and_sources_instead_of_pooling() -> None:
    calls = {
        "human-chosen": _call("human-chosen"),
        "human-rejected": _call("human-rejected"),
        "ci-chosen": _call("ci-chosen"),
        "ci-rejected": _call("ci-rejected"),
        "accepted": _call("accepted"),
        "verified": _call("verified"),
    }
    human_pair = project_dpo(
        [
            _attributed("human-chosen", decisions=[_decision(True)]),
            _attributed("human-rejected", decisions=[_decision(False)]),
        ],
        calls,
    ).rows[0]
    outcome_pair = project_dpo(
        [
            _attributed("ci-chosen", outcomes=[_ci(CIResult.PASSED, run_id="pass")]),
            _attributed("ci-rejected", outcomes=[_ci(CIResult.FAILED, run_id="fail")]),
        ],
        calls,
        DPOPolicy(recipe_id="dpo_outcome"),
    ).rows[0]
    curated = project_sft(
        [_attributed("accepted", decisions=[_decision(True)])], calls
    ).rows[0]
    verified = project_sft(
        [_attributed("verified", outcomes=[_ci(CIResult.PASSED, run_id="verified")])],
        calls,
        SFTPolicy(recipe_id="sft_verified"),
    ).rows[0]

    report = build_dataset_diagnostics([human_pair, outcome_pair], [curated, verified])

    assert {
        (
            row.dataset,
            row.recipe_id,
            row.recipe_version,
            row.chosen_label_source,
            row.rejected_label_source,
            row.eligibility_source,
        )
        for row in report.model_balance
    } == {
        ("dpo", "dpo_human", 2, "explicit_accept", "explicit_reject", None),
        (
            "dpo",
            "dpo_outcome",
            2,
            "resolved_ci_pass",
            "resolved_ci_fail",
            None,
        ),
        ("sft", "sft_curated", 1, None, None, "explicit_accept"),
        ("sft", "sft_verified", 1, None, None, "resolved_ci_pass"),
    }


def test_recipe_skip_vocabularies_are_closed_and_unique() -> None:
    for vocabulary in (
        DPO_SKIP_REASONS,
        SFT_SKIP_REASONS,
        DIFF_SFT_SKIP_REASONS,
        RECOVERY_SKIP_REASONS,
        RECOVERY_PROJECTION_SKIP_REASONS,
    ):
        assert len(vocabulary) == len(set(vocabulary))

    assert "no_label_source" in DPO_SKIP_REASONS
    assert "no_eligibility_source" in SFT_SKIP_REASONS
    assert "file_diff_unavailable" in DIFF_SFT_SKIP_REASONS
    assert "ambiguous_workflow_verdicts" in RECOVERY_SKIP_REASONS
    assert RECOVERY_PROJECTION_SKIP_REASONS == (
        "non_finite_number",
        "unrepresentable_unicode",
        "inference_call_not_found",
        "attribution_evidence_absent",
    )


def test_dpo_recipe_jsonl_is_byte_deterministic_under_shuffled_input(tmp_path) -> None:
    cases = {
        "human": (
            [
                _attributed("chosen", decisions=[_decision(True)]),
                _attributed("rejected", decisions=[_decision(False)]),
            ],
            DPOPolicy(),
        ),
        "outcome": (
            [
                _attributed("chosen", outcomes=[_ci(CIResult.PASSED, run_id="pass")]),
                _attributed("rejected", outcomes=[_ci(CIResult.FAILED, run_id="fail")]),
            ],
            DPOPolicy(recipe_id="dpo_outcome"),
        ),
    }
    calls = {"chosen": _call("chosen"), "rejected": _call("rejected")}

    for name, (inputs, policy) in cases.items():
        first = project_dpo(inputs, calls, policy)
        repeated = project_dpo(inputs, calls, policy)
        shuffled = project_dpo(list(reversed(inputs)), calls, policy)
        assert first == repeated == shuffled
        paths = [tmp_path / f"{name}-{suffix}.jsonl" for suffix in range(3)]
        for projection, path in zip((first, repeated, shuffled), paths, strict=True):
            write_jsonl(dpo_to_export_rows(projection.rows), path, split_enabled=False)
        assert len({path.read_bytes() for path in paths}) == 1


def test_sft_recipe_jsonl_is_byte_deterministic_under_shuffled_input(tmp_path) -> None:
    cases = {
        "curated": (
            [
                _attributed("a", decisions=[_decision(True)]),
                _attributed("b", decisions=[_decision(True)]),
            ],
            SFTPolicy(),
        ),
        "verified": (
            [
                _attributed("a", outcomes=[_ci(CIResult.PASSED, run_id="a")]),
                _attributed("b", outcomes=[_ci(CIResult.PASSED, run_id="b")]),
            ],
            SFTPolicy(recipe_id="sft_verified"),
        ),
    }
    calls = {"a": _call("a"), "b": _call("b")}

    for name, (inputs, policy) in cases.items():
        first = project_sft(inputs, calls, policy)
        repeated = project_sft(inputs, calls, policy)
        shuffled = project_sft(list(reversed(inputs)), calls, policy)
        assert first == repeated == shuffled
        paths = [tmp_path / f"{name}-{suffix}.jsonl" for suffix in range(3)]
        for projection, path in zip((first, repeated, shuffled), paths, strict=True):
            write_jsonl(sft_to_export_rows(projection.rows), path, split_enabled=False)
        assert len({path.read_bytes() for path in paths}) == 1
