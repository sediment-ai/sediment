# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dataset-diagnostics report rendering tests."""

from __future__ import annotations

import json

from sediment_api.reports import dataset_diagnostics as report_module
from sediment_derive import Provenance
from sediment_export import (
    AbandonmentSummary,
    ConfidenceDistributionRow,
    ConfidenceFloorExclusionRow,
    CrossSplitDuplicateReport,
    DatasetDiagnostics,
    DistributionStats,
    FateDiagnostic,
    build_dataset_diagnostics,
)


def test_table_prints_every_abandonment_summary_field(capsys) -> None:
    summary = AbandonmentSummary(
        abandoned_sessions=4,
        grade_eligible_sessions=2,
        implicit_only_sessions=2,
        negative_completions=3,
        explicit_accepts_unjoined=1,
        derivation_skipped={"within_grace_horizon": 5},
        provenance=Provenance(policy_version="2", quarantine_revision=0),
    )
    report = build_dataset_diagnostics([], [], abandonment=summary)

    report_module._print_table(report)

    output = capsys.readouterr().out
    assert "abandoned sessions: 4" in output
    assert "grade-eligible sessions: 2" in output
    assert "implicit-only sessions: 2" in output
    assert "negative completions: 3" in output
    assert "explicit accepts unjoined: 1" in output
    assert "derivation skipped: within_grace_horizon=5" in output
    assert "provenance: policy_version=2 quarantine_revision=0" in output


def test_table_prints_fate_diagnostic_once(capsys) -> None:
    report = build_dataset_diagnostics([], [])
    report = DatasetDiagnostics(
        model_balance=report.model_balance,
        confidence_distributions=report.confidence_distributions,
        cross_split_duplicates=report.cross_split_duplicates,
        confidence_floor_exclusions=report.confidence_floor_exclusions,
        dpo_bucket_sparsity=report.dpo_bucket_sparsity,
        fate=FateDiagnostic(
            fates={"deleted": 2},
            explicit_accept_fates={"deleted": 1},
            fates_with_external_changes={"deleted": 1},
            skipped={"invalid_score": 3},
            provenance=Provenance(policy_version="1", quarantine_revision=4),
        ),
    )

    report_module._print_table(report)

    output = capsys.readouterr().out
    assert output.count("Fate diagnostic") == 1
    assert "fates: deleted=2" in output
    assert "human-explicit accept fates: deleted=1" in output
    assert "fates with external changes: deleted=1" in output
    assert "derivation skipped: invalid_score=3" in output
    assert "provenance: policy_version=1 quarantine_revision=4" in output


def test_json_includes_empty_abandonment_summary(
    tmp_path, postgres_database_url, capsys
) -> None:
    assert (
        report_module.main(
            [
                "--org",
                "acme-corp",
                "--database-url",
                postgres_database_url,
                "--mirror-path",
                str(tmp_path / "mirrors"),
                "--json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["abandonment"] == {
        "abandoned_sessions": 0,
        "grade_eligible_sessions": 0,
        "implicit_only_sessions": 0,
        "negative_completions": 0,
        "explicit_accepts_unjoined": 0,
        "derivation_skipped": {},
        "provenance": {
            "policy_version": "6",
            "quarantine_revision": 0,
            "policy_digest": None,
        },
    }
    assert payload["fate"] == {
        "fates": {},
        "explicit_accept_fates": {},
        "fates_with_external_changes": {},
        "skipped": {},
        "provenance": {
            "policy_version": "1",
            "quarantine_revision": 0,
            "policy_digest": None,
        },
    }


def test_recipe_defaults_are_conservative_and_outcome_recipes_are_explicit() -> None:
    parser = report_module.build_parser()

    defaults = parser.parse_args(["--org", "acme-corp"])
    assert defaults.dpo_recipe == "dpo_human"
    assert defaults.sft_recipe == "sft_curated"

    selected = parser.parse_args(
        [
            "--org",
            "acme-corp",
            "--dpo-recipe",
            "dpo_outcome",
            "--sft-recipe",
            "sft_verified",
        ]
    )
    assert selected.dpo_recipe == "dpo_outcome"
    assert selected.sft_recipe == "sft_verified"


def test_tables_identify_every_recipe_and_evidence_source(capsys) -> None:
    stats = DistributionStats(
        count=1,
        mean=0.9,
        median=0.9,
        min=0.9,
        max=0.9,
        stdev=0.0,
    )
    strata = [
        ("sft", "sft_curated", None, None, "explicit_accept"),
        ("sft", "sft_curated", None, None, "edit_retention"),
        (
            "dpo",
            "dpo_human",
            "explicit_accept",
            "explicit_reject",
            None,
        ),
        (
            "dpo",
            "dpo_outcome",
            "resolved_ci_pass",
            "resolved_ci_fail",
            None,
        ),
    ]
    distributions = [
        ConfidenceDistributionRow(
            dataset=dataset,
            recipe_id=recipe_id,
            recipe_version=1,
            chosen_label_source=chosen_source,
            rejected_label_source=rejected_source,
            eligibility_source=eligibility_source,
            metric="confidence",
            model="model-a",
            stats=stats,
        )
        for dataset, recipe_id, chosen_source, rejected_source, eligibility_source in strata
    ]
    duplicates = [
        CrossSplitDuplicateReport(
            dataset=dataset,
            recipe_id=recipe_id,
            recipe_version=1,
            chosen_label_source=chosen_source,
            rejected_label_source=rejected_source,
            eligibility_source=eligibility_source,
            duplicate_prompt_count=1,
        )
        for dataset, recipe_id, chosen_source, rejected_source, eligibility_source in strata
    ]
    floor_exclusions = [
        ConfidenceFloorExclusionRow(
            dataset="sft",
            recipe_id="sft_curated",
            recipe_version=1,
            eligibility_source=source,
            model="model-a",
            otherwise_eligible_completions=1,
            excluded_by_floor=1,
            exclusion_rate=1.0,
        )
        for source in ("explicit_accept", "edit_retention")
    ]
    report = DatasetDiagnostics(
        model_balance=[],
        confidence_distributions=distributions,
        cross_split_duplicates=duplicates,
        confidence_floor_exclusions=floor_exclusions,
        dpo_bucket_sparsity=[],
    )

    report_module._print_distributions(report)
    report_module._print_duplicates(report)
    report_module._print_floor_exclusions(report)

    output = capsys.readouterr().out
    for expected in (
        "sft_curated v1",
        "explicit_accept",
        "edit_retention",
        "dpo_human v1",
        "explicit_accept/explicit_reject",
        "dpo_outcome v1",
        "resolved_ci_pass/resolved_ci_fail",
    ):
        assert expected in output
