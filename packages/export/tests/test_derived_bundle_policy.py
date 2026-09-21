# SPDX-License-Identifier: AGPL-3.0-or-later
"""Resolved policy and scope contracts for derived bundles."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sediment_export import (
    DerivationPolicy,
    DerivationScope,
    load_derivation_policy,
)


def test_default_derivation_policy_resolves_every_supported_knob() -> None:
    policy = DerivationPolicy()

    assert policy.schema_version == 1
    assert policy.attribution.jaccard.min_similarity == 0.7
    assert policy.attribution.jaccard.lookback_window_minutes == 60
    assert policy.attribution.git_notes.min_similarity == 0.3
    assert policy.attribution.git_notes.lookback_window_minutes == 10080
    assert policy.attribution.post_push_grace_period_minutes == 10
    assert policy.attribution.max_commits_per_push == 20
    assert policy.eval_fraction == 0.1


def test_policy_loader_rejects_unknown_fields(tmp_path) -> None:
    path = tmp_path / "policy.toml"
    path.write_text(
        """\
schema_version = 1

[attribution.jaccard]
min_similarity = 0.6
surprise = true

[split]
eval_fraction = 0.2
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"attribution\.jaccard\.surprise"):
        load_derivation_policy(path)


def test_policy_loader_resolves_partial_config_and_has_stable_digest(tmp_path) -> None:
    path = tmp_path / "policy.toml"
    path.write_text(
        """\
schema_version = 1

[attribution.jaccard]
min_similarity = 0.6

[split]
eval_fraction = 0.2
""",
        encoding="utf-8",
    )

    first = load_derivation_policy(path)
    second = load_derivation_policy(path)

    assert first.attribution.jaccard.min_similarity == 0.6
    assert first.attribution.jaccard.lookback_window_minutes == 60
    assert first.eval_fraction == 0.2
    assert first.digest == second.digest
    assert len(first.digest) == 64
    assert first.digest != DerivationPolicy().digest


@pytest.mark.parametrize(
    ("section", "field", "value", "message"),
    [
        ("attribution.jaccard", "min_similarity", 1.1, "min_similarity"),
        ("attribution.git_notes", "min_similarity", -0.1, "min_similarity"),
        (
            "attribution.jaccard",
            "lookback_window_minutes",
            0,
            "lookback_window_minutes",
        ),
        (
            "attribution.git_notes",
            "lookback_window_minutes",
            0,
            "lookback_window_minutes",
        ),
        (
            "attribution",
            "post_push_grace_period_minutes",
            -1,
            "post_push_grace_period_minutes",
        ),
        ("attribution", "max_commits_per_push", 0, "max_commits_per_push"),
    ],
)
def test_policy_rejects_out_of_range_attribution_values(
    tmp_path, section: str, field: str, value: float | int, message: str
) -> None:
    path = tmp_path / "policy.toml"
    path.write_text(
        f"schema_version = 1\n[{section}]\n{field} = {value}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=message):
        load_derivation_policy(path)


def test_policy_rejects_out_of_range_eval_fraction() -> None:
    with pytest.raises(ValueError, match="eval_fraction"):
        DerivationPolicy(eval_fraction=0.6)


def test_policy_rejects_boolean_schema_version() -> None:
    with pytest.raises(ValueError, match="schema_version"):
        DerivationPolicy(schema_version=True)


@pytest.mark.parametrize("value", ["false", '"not-a-number"'])
def test_policy_loader_rejects_non_numeric_eval_fraction(tmp_path, value: str) -> None:
    path = tmp_path / "policy.toml"
    path.write_text(
        f"schema_version = 1\n[split]\neval_fraction = {value}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="eval_fraction must be a number"):
        load_derivation_policy(path)


def test_scope_is_inclusive_since_exclusive_until_and_all_users_by_default() -> None:
    since = datetime(2026, 8, 1, tzinfo=UTC)
    until = datetime(2026, 9, 1, tzinfo=UTC)
    scope = DerivationScope(since=since, until=until)

    assert scope.users is None
    assert scope.includes(user_id="alice", occurred_at=since)
    assert scope.includes(user_id="bob", occurred_at=datetime(2026, 8, 31, tzinfo=UTC))
    assert not scope.includes(user_id="alice", occurred_at=until)


def test_scope_normalizes_user_allowlist_and_rejects_naive_time() -> None:
    scope = DerivationScope(users=(" bob ", "alice", "bob"))

    assert scope.users == ("alice", "bob")
    assert scope.includes(user_id="alice", occurred_at=datetime(2026, 8, 1, tzinfo=UTC))
    assert not scope.includes(
        user_id="carol", occurred_at=datetime(2026, 8, 1, tzinfo=UTC)
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        DerivationScope(since=datetime(2026, 8, 1))
