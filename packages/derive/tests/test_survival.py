# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the edit-retention join and fill seam."""

from __future__ import annotations

import random
from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest

from sediment_core import (
    AgentHarness,
    DeveloperDecision,
    EditObservation,
    InteractionMode,
)
from sediment_derive import (
    EditFate,
    Fate,
    FatePolicy,
    FateResult,
    Provenance,
    attach_edit_retention,
    derive_fate_result,
    derive_fates,
    external_lines_after,
)

T0 = datetime(2026, 7, 10, 12, 0, 0, tzinfo=UTC)


def test_fate_contract_is_closed_and_frozen() -> None:
    fate = Fate(
        observation_id="observation-1",
        org_id="acme",
        agent_harness=AgentHarness.CLAUDE_CODE,
        session_id="sess-1",
        call_id="toolu-1",
        score=0.5,
        fate=EditFate.PARTIALLY_MODIFIED,
        external_lines_added=None,
        external_lines_removed=None,
        provenance=Provenance(policy_version="1", quarantine_revision=0),
    )

    assert [item.value for item in EditFate] == [
        "deleted",
        "partially_modified",
        "unmodified",
    ]
    with pytest.raises(AttributeError):
        fate.score = 0.75  # type: ignore[misc]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"deleted_max": True}, "deleted_max must be a number"),
        ({"deleted_max": "0.1"}, "deleted_max must be a number"),
        ({"deleted_max": float("nan")}, "deleted_max must be finite"),
        ({"unmodified_min": float("inf")}, "unmodified_min must be finite"),
        ({"deleted_max": -0.01}, "deleted_max must be in [0.0, 1.0]"),
        ({"unmodified_min": 1.01}, "unmodified_min must be in [0.0, 1.0]"),
        (
            {"deleted_max": 0.9, "unmodified_min": 0.9},
            "deleted_max must be less than unmodified_min",
        ),
        ({"policy_version": ""}, "policy_version must be non-empty"),
        ({"policy_version": "  "}, "policy_version must be non-empty"),
    ],
)
def test_fate_policy_rejects_invalid_values(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(
        ValueError, match=message.replace("[", r"\[").replace("]", r"\]")
    ):
        FatePolicy(**overrides)


def test_fate_policy_defaults_are_versioned() -> None:
    assert FatePolicy() == FatePolicy(
        deleted_max=0.1,
        unmodified_min=0.9,
        policy_version="1",
    )


def test_fate_thresholds_are_inclusive_and_scores_are_clamped() -> None:
    observations = [
        _outcome(observation_id="o-low", call_id="c-low", applied_text="-1"),
        _outcome(observation_id="o-deleted", call_id="c-deleted", applied_text="0.1"),
        _outcome(observation_id="o-partial", call_id="c-partial", applied_text="0.5"),
        _outcome(
            observation_id="o-unmodified", call_id="c-unmodified", applied_text="0.9"
        ),
        _outcome(observation_id="o-high", call_id="c-high", applied_text="2"),
    ]

    result = derive_fate_result(observations, lambda original, _final: float(original))

    assert [fate.call_id for fate in result.fates] == [
        "c-deleted",
        "c-high",
        "c-low",
        "c-partial",
        "c-unmodified",
    ]
    assert {fate.observation_id: (fate.score, fate.fate) for fate in result.fates} == {
        "o-low": (0.0, EditFate.DELETED),
        "o-deleted": (0.1, EditFate.DELETED),
        "o-partial": (0.5, EditFate.PARTIALLY_MODIFIED),
        "o-unmodified": (0.9, EditFate.UNMODIFIED),
        "o-high": (1.0, EditFate.UNMODIFIED),
    }
    assert result.skipped == Counter()
    assert result.provenance == Provenance(policy_version="1", quarantine_revision=0)
    assert (
        derive_fates(observations, lambda original, _final: float(original))
        == result.fates
    )


def test_fate_carries_complete_external_line_tail_totals() -> None:
    observations = [
        _window("toolu-1", 0, 1, 2, observation_id="o-1"),
        _window("toolu-2", 1, 3, 4, observation_id="o-2"),
    ]

    result = derive_fate_result(observations, lambda _original, _final: 0.5)

    assert [
        (fate.call_id, fate.external_lines_added, fate.external_lines_removed)
        for fate in result.fates
    ] == [("toolu-1", 4, 6), ("toolu-2", 3, 4)]


def test_fate_keeps_score_when_external_line_coverage_is_incomplete() -> None:
    observations = [
        _window("toolu-1", 0, 1, observation_id="o-1"),
        _window("toolu-2", 1, None, observation_id="o-2"),
    ]

    result = derive_fate_result(observations, lambda _original, _final: 0.5)

    assert len(result.fates) == 2
    assert all(fate.external_lines_added is None for fate in result.fates)
    assert all(fate.external_lines_removed is None for fate in result.fates)


@pytest.mark.parametrize("invalid", [True, "0.5", float("nan"), float("inf")])
def test_fate_invalid_scores_skip_and_log(
    invalid: object, caplog: pytest.LogCaptureFixture
) -> None:
    observation = _outcome(observation_id="observation-invalid")

    with caplog.at_level("WARNING", logger="sediment.derive.survival"):
        result = derive_fate_result(
            [observation],
            lambda _original, _final: invalid,  # type: ignore[return-value]
        )

    assert result.fates == []
    assert result.skipped == Counter({"invalid_score": 1})
    [record] = caplog.records
    assert record.org_id == "acme"  # type: ignore[attr-defined]
    assert record.observation_id == "observation-invalid"  # type: ignore[attr-defined]


def test_fate_scorer_error_skips_and_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bad = _outcome(observation_id="o-bad", call_id="c-bad", applied_text="bad")
    good = _outcome(observation_id="o-good", call_id="c-good", applied_text="good")

    def scorer(original: str, _final: str) -> float:
        if original == "bad":
            raise RuntimeError("scorer failed")
        return 1.0

    with caplog.at_level("WARNING", logger="sediment.derive.survival"):
        result = derive_fate_result([bad, good], scorer)

    assert [fate.observation_id for fate in result.fates] == ["o-good"]
    assert result.skipped == Counter({"scorer_error": 1})
    [record] = caplog.records
    assert record.org_id == "acme"  # type: ignore[attr-defined]
    assert record.observation_id == "o-bad"  # type: ignore[attr-defined]


def test_fate_is_repeatable_and_independent_of_fact_order() -> None:
    observations = [
        _window("toolu-2", 1, 2, observation_id="o-2", applied_text="0.5"),
        _window("toolu-1", 0, 1, observation_id="o-1", applied_text="0.1"),
        _window(
            "toolu-other",
            0,
            0,
            observation_id="o-other",
            session_id="sess-2",
            applied_text="0.9",
        ),
    ]

    def scorer(original: str, _final: str) -> float:
        return float(original)

    expected = derive_fate_result(observations, scorer, quarantine_revision=7)

    assert derive_fate_result(observations, scorer, quarantine_revision=7) == expected
    for seed in range(8):
        shuffled = list(observations)
        random.Random(seed).shuffle(shuffled)
        assert derive_fate_result(shuffled, scorer, quarantine_revision=7) == expected


def test_fate_result_defaults_to_an_empty_closed_tally() -> None:
    assert FateResult().skipped == Counter()


def _decision(**over) -> DeveloperDecision:
    base = dict(
        org_id="acme",
        session_id="sess-1",
        user_id="dev-1",
        agent_harness=AgentHarness.CLAUDE_CODE,
        file_path="app/math.py",
        accepted=True,
        explicit=False,
        interaction_mode=InteractionMode.AGENT,
        call_id="toolu-1",
        occurred_at=T0,
    )
    base.update(over)
    return DeveloperDecision(**base)


def _outcome(**over) -> EditObservation:
    base = dict(
        org_id="acme",
        session_id="sess-1",
        user_id="dev-1",
        agent_harness=AgentHarness.CLAUDE_CODE,
        file_path="app/math.py",
        call_id="toolu-1",
        applied_text="a",
        observed_file_text="b",
        occurred_at=T0,
    )
    base.update(over)
    return EditObservation(**base)


def test_fills_matching_decision_and_leaves_input_untouched() -> None:
    d = _decision()
    [filled] = attach_edit_retention([d], [_outcome()], lambda o, f: 0.75)
    assert filled.edit_retention_score == 0.75
    assert d.edit_retention_score is None  # input model never mutated
    assert filled.decision_id == d.decision_id  # same fact, derived copy


def test_no_match_passes_through() -> None:
    [same] = attach_edit_retention(
        [_decision(call_id="toolu-other")], [_outcome()], lambda o, f: 0.5
    )
    assert same.edit_retention_score is None
    # Path-less rejects (call_id present but no outcome — a rejected edit
    # never produces a pair) and keyless decisions both pass through.
    [keyless] = attach_edit_retention(
        [_decision(call_id=None)], [_outcome()], lambda o, f: 0.5
    )
    assert keyless.edit_retention_score is None


def test_vendor_supplied_rate_never_overwritten() -> None:
    copilot = _decision(
        agent_harness=AgentHarness.COPILOT,
        interaction_mode=InteractionMode.AGENT,
        edit_retention_score=0.4,
    )
    [same] = attach_edit_retention(
        [copilot],
        [_outcome(agent_harness=AgentHarness.COPILOT)],
        lambda o, f: 0.9,
    )
    assert same.edit_retention_score == 0.4


def test_score_clamped_and_non_finite_skipped() -> None:
    [hi] = attach_edit_retention([_decision()], [_outcome()], lambda o, f: 1.5)
    assert hi.edit_retention_score == 1.0
    [lo] = attach_edit_retention([_decision()], [_outcome()], lambda o, f: -0.1)
    assert lo.edit_retention_score == 0.0
    [nan] = attach_edit_retention(
        [_decision()], [_outcome()], lambda o, f: float("nan")
    )
    assert nan.edit_retention_score is None


def test_scorer_contract_is_snippet_vs_whole_file() -> None:
    # Normative shape test: for Edit calls, original is only the new_string
    # snippet while final is the WHOLE file at session end. A containment
    # scorer handles that; a symmetric normalized distance scores a
    # perfectly-surviving snippet near zero — pinned here so the metric is
    # validated against the shape this seam actually produces.
    snippet = "def add(a, b):\n    return int(a) + int(b)\n"
    whole_file = ("# preamble\n" * 200) + snippet + ("# postamble\n" * 200)

    def containment(original: str, final: str) -> float:
        return 1.0 if original in final else 0.0

    def symmetric_distance(original: str, final: str) -> float:
        # Length-ratio floor of any normalized edit distance.
        return min(len(original), len(final)) / max(len(original), len(final))

    pair = _outcome(applied_text=snippet, observed_file_text=whole_file)
    [good] = attach_edit_retention([_decision()], [pair], containment)
    assert good.edit_retention_score == 1.0  # the snippet survived verbatim
    [bad] = attach_edit_retention([_decision()], [pair], symmetric_distance)
    assert bad.edit_retention_score < 0.05  # symmetric metric calls survival ~zero


def test_duplicate_outcome_keys_keep_first() -> None:
    first = _outcome(observed_file_text="first")
    second = _outcome(observed_file_text="second")
    [filled] = attach_edit_retention(
        [_decision()], [first, second], lambda o, f: 1.0 if f == "first" else 0.0
    )
    assert filled.edit_retention_score == 1.0  # store's first-write-wins, mirrored


def test_scorer_receives_the_pair() -> None:
    seen: list[tuple[str, str]] = []

    def scorer(original: str, final: str) -> float:
        seen.append((original, final))
        return 1.0

    attach_edit_retention(
        [_decision()], [_outcome(applied_text="orig", observed_file_text="fin")], scorer
    )
    assert seen == [("orig", "fin")]


# ── external_lines_after ──────────────────────────────────────────────────


def _window(call_id: str, minute: int, added: int | None, removed: int = 0, **over):
    """One edit's own window: counts for the span from this edit to the next
    agent edit of the same file (or to session end for the last)."""
    return _outcome(
        call_id=call_id,
        occurred_at=T0 + timedelta(minutes=minute),
        external_lines_added=added,
        external_lines_removed=None if added is None else removed,
        **over,
    )


def _key(call_id: str) -> tuple[str, str, str, str]:
    return ("acme", AgentHarness.CLAUDE_CODE, "sess-1", call_id)


def test_each_edit_sums_the_windows_after_it() -> None:
    # A pair's survival is measured against session end, so what bears on it
    # is every window from that edit onward, not just its own.
    outcomes = [
        _window("toolu-1", 0, 1),
        _window("toolu-2", 1, 2),
        _window("toolu-3", 2, 4),
    ]
    totals = external_lines_after(outcomes)
    assert totals[_key("toolu-3")] == (4, 0)
    assert totals[_key("toolu-2")] == (6, 0)
    assert totals[_key("toolu-1")] == (7, 0)


def test_added_and_removed_accumulate_independently() -> None:
    outcomes = [_window("toolu-1", 0, 1, 5), _window("toolu-2", 1, 2, 3)]
    totals = external_lines_after(outcomes)
    assert totals[_key("toolu-2")] == (2, 3)
    assert totals[_key("toolu-1")] == (3, 8)


def test_files_do_not_bleed_into_each_other() -> None:
    outcomes = [
        _window("toolu-a", 0, 1, file_path="a.py"),
        _window("toolu-b", 1, 9, file_path="b.py"),
    ]
    totals = external_lines_after(outcomes)
    assert totals[_key("toolu-a")] == (1, 0)
    assert totals[_key("toolu-b")] == (9, 0)


def test_uncovered_window_omits_every_edit_before_it() -> None:
    # One unobserved window and the totals before it would understate by an
    # unknown amount — absent beats a wrong sum.
    outcomes = [
        _window("toolu-1", 0, 1),
        _window("toolu-2", 1, None),
        _window("toolu-3", 2, 4),
    ]
    totals = external_lines_after(outcomes)
    assert totals[_key("toolu-3")] == (4, 0)
    assert _key("toolu-2") not in totals
    assert _key("toolu-1") not in totals


def test_all_uncovered_yields_nothing() -> None:
    assert external_lines_after([_window("toolu-1", 0, None)]) == {}


def test_zero_windows_are_a_real_answer() -> None:
    # Nobody else touched the file — that is an observation, not a gap.
    totals = external_lines_after([_window("toolu-1", 0, 0)])
    assert totals[_key("toolu-1")] == (0, 0)


def test_same_facts_identical_output() -> None:
    outcomes = [_window("toolu-1", 0, 1), _window("toolu-2", 1, 2)]
    assert external_lines_after(outcomes) == external_lines_after(outcomes)


def test_shuffled_ingest_order_identical_output() -> None:
    # Non-negotiable rule 2: no derivation may depend on ingest order.
    outcomes = [
        _window("toolu-1", 0, 1),
        _window("toolu-2", 1, 2),
        _window("toolu-3", 2, 4),
        _window("toolu-4", 3, 8, file_path="other.py"),
    ]
    expected = external_lines_after(outcomes)
    for seed in range(8):
        shuffled = list(outcomes)
        random.Random(seed).shuffle(shuffled)
        assert external_lines_after(shuffled) == expected


def test_simultaneous_edits_break_ties_on_call_id_not_input_order() -> None:
    # Equal occurred_at is real (a batched refire); the tiebreak must be a
    # fact field, so the two orderings cannot disagree.
    a = _window("toolu-a", 0, 1)
    b = _window("toolu-b", 0, 2)
    assert external_lines_after([a, b]) == external_lines_after([b, a])
