# SPDX-License-Identifier: AGPL-3.0-or-later
"""Canonical Attribution public-contract tests."""

from __future__ import annotations

import importlib.util

import pytest
import sediment_derive
import sediment_export

from sediment_export import DerivationPolicy, load_derivation_policy


def test_public_python_contract_exposes_only_attribution_vocabulary() -> None:
    for name in (
        "Attribution",
        "AttributionPolicy",
        "AttributionResult",
        "AttributedCompletion",
        "CommitRef",
        "Provenance",
        "derive_attribution_result",
        "derive_attributions",
    ):
        package = sediment_export if name == "AttributedCompletion" else sediment_derive
        assert hasattr(package, name), name

    for name in (
        "Correlation",
        "CorrelationPolicy",
        "CorrelationResult",
        "derive_correlation_result",
        "derive_correlations",
        "SplitSettings",
    ):
        assert not hasattr(sediment_derive, name), name
    for name in (
        "LabeledCompletion",
        "LabeledCompletionPolicy",
        "assemble_labeled_completions",
    ):
        assert not hasattr(sediment_export, name), name

    assert importlib.util.find_spec("sediment_derive.correlation") is None
    assert importlib.util.find_spec("sediment_export.labeled_completions") is None


def test_default_policy_serializes_as_canonical_schema_version_one() -> None:
    policy = DerivationPolicy()

    assert policy.to_dict() == {
        "schema_version": 1,
        "attribution": {
            "post_push_grace_period_minutes": 10,
            "max_commits_per_push": 20,
            "git_notes": {
                "min_similarity": 0.3,
                "lookback_window_minutes": 10080,
            },
            "jaccard": {
                "min_similarity": 0.7,
                "lookback_window_minutes": 60,
            },
        },
        "split": {"eval_fraction": 0.1},
    }
    assert policy.schema_version == 1
    assert policy.eval_fraction == 0.1


def test_policy_loader_accepts_only_the_canonical_vocabulary(tmp_path) -> None:
    canonical = tmp_path / "canonical.toml"
    canonical.write_text(
        """\
schema_version = 1

[attribution]
post_push_grace_period_minutes = 10
max_commits_per_push = 20

[attribution.git_notes]
min_similarity = 0.3
lookback_window_minutes = 10080

[attribution.jaccard]
min_similarity = 0.7
lookback_window_minutes = 60

[split]
eval_fraction = 0.1
""",
        encoding="utf-8",
    )

    policy = load_derivation_policy(canonical)

    assert policy.to_dict() == DerivationPolicy().to_dict()


@pytest.mark.parametrize(
    "text",
    [
        "[attribution]\nmax_commits_per_push = 20\n",
        "schema_version = 2\n[attribution]\nmax_commits_per_push = 20\n",
        "schema_version = 1\n[attribution]\nsimilarity_threshold = 0.7\n",
        (
            "schema_version = 1\n"
            "[attribution]\nmax_commits_per_push = 20\n"
            "[attribution]\nsimilarity_threshold = 0.7\n"
        ),
        "schema_version = 1\n[attribution]\nsurprise = true\n",
    ],
)
def test_policy_loader_rejects_noncanonical_contracts(tmp_path, text: str) -> None:
    policy_path = tmp_path / "policy.toml"
    policy_path.write_text(text, encoding="utf-8")

    with pytest.raises(ValueError):
        load_derivation_policy(policy_path)
