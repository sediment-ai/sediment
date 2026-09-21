# SPDX-License-Identifier: AGPL-3.0-or-later
"""The public label-confidence vocabulary has no reward-era aliases."""

from __future__ import annotations

import importlib.util
from dataclasses import fields

import pytest

import sediment_export


def test_public_label_confidence_vocabulary_has_no_reward_policy_aliases() -> None:
    assert hasattr(sediment_export, "LabelConfidencePolicy")
    assert hasattr(sediment_export, "LabelConfidenceSettings")
    assert not hasattr(sediment_export, "RewardPolicy")
    assert not hasattr(sediment_export, "RewardSettings")
    assert importlib.util.find_spec("sediment_export.label_confidence") is not None
    assert importlib.util.find_spec("sediment_export.reward") is None


def test_projection_policies_use_label_confidence_nesting() -> None:
    assert [field.name for field in fields(sediment_export.DPOPolicy)] == [
        "label_confidence",
        "max_pairs_per_bucket",
        "recipe_id",
    ]
    assert [field.name for field in fields(sediment_export.SFTPolicy)] == [
        "label_confidence",
        "min_confidence",
        "recipe_id",
    ]


def test_sft_curated_v1_retention_threshold_is_part_of_the_recipe_contract() -> None:
    assert sediment_export.SFT_CURATED_V1_STRONG_RETENTION_THRESHOLD == 0.8
    with pytest.raises(TypeError):
        sediment_export.SFTPolicy(strong_retention_threshold=0.7)
