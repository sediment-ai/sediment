# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Transcript-derived edit retention — a pure derivation over EditObservation facts
(ADR 0007).

The client ships ``applied_text`` and ``observed_file_text`` as an
:class:`EditObservation` fact; the edit retention score is computed here, at
read time, by a caller-supplied
scorer — so the metric stays a re-derivable policy choice, never frozen into
captured facts (ADR 0001). The chosen scorer is ``four_gram_containment``
(``survival_scoring`` — containment-shaped, per the contract below); it plugs
in as the ``scorer`` argument.

Nothing is persisted. Copilot's vendor-supplied retention score is never
overwritten — the transcript path only fills the gap for agent harnesses that carry
no graded signal of their own.
"""

from __future__ import annotations

import logging
import math
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from sediment_core import (
    AgentHarness,
    DeveloperDecision,
    EditObservation,
    NonEmptyId,
    OrgId,
)

from .provenance import Provenance

logger = logging.getLogger("sediment.derive.survival")


class EditFate(StrEnum):
    """The categorical final Fate of one applied edit."""

    DELETED = "deleted"
    PARTIALLY_MODIFIED = "partially_modified"
    UNMODIFIED = "unmodified"


@dataclass(frozen=True)
class FatePolicy:
    """Versioned thresholds for mapping edit retention to Fate."""

    deleted_max: float = 0.1
    unmodified_min: float = 0.9
    policy_version: str = "1"

    def __post_init__(self) -> None:
        for name in ("deleted_max", "unmodified_min"):
            value = getattr(self, name)
            if not isinstance(value, int | float) or isinstance(value, bool):
                raise ValueError(f"FatePolicy.{name} must be a number")
            if not math.isfinite(value):
                raise ValueError(f"FatePolicy.{name} must be finite")
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"FatePolicy.{name} must be in [0.0, 1.0]")
        if self.deleted_max >= self.unmodified_min:
            raise ValueError("FatePolicy.deleted_max must be less than unmodified_min")
        if not isinstance(self.policy_version, str) or not self.policy_version.strip():
            raise ValueError("FatePolicy.policy_version must be non-empty")


@dataclass(frozen=True)
class Fate:
    """The derived final Fate of one Edit observation."""

    observation_id: NonEmptyId
    org_id: OrgId
    agent_harness: AgentHarness
    session_id: NonEmptyId
    call_id: NonEmptyId
    score: float
    fate: EditFate
    external_lines_added: int | None
    external_lines_removed: int | None
    provenance: Provenance


FateSkipReason = Literal["scorer_error", "invalid_score"]
FATE_SKIP_REASONS: tuple[FateSkipReason, ...] = ("scorer_error", "invalid_score")


@dataclass
class FateResult:
    """Deterministically ordered Fates and closed Derivation skips."""

    fates: list[Fate] = field(default_factory=list)
    skipped: Counter[str] = field(default_factory=Counter)
    provenance: Provenance = field(
        default_factory=lambda: Provenance(
            policy_version=FatePolicy().policy_version,
            quarantine_revision=0,
        )
    )


def derive_fates(
    observations: Iterable[EditObservation],
    scorer: Callable[[str, str], float],
    policy: FatePolicy | None = None,
    *,
    quarantine_revision: int = 0,
) -> list[Fate]:
    """Derive and return only the deterministically ordered Fate list."""

    return derive_fate_result(
        observations,
        scorer,
        policy,
        quarantine_revision=quarantine_revision,
    ).fates


def derive_fate_result(
    observations: Iterable[EditObservation],
    scorer: Callable[[str, str], float],
    policy: FatePolicy | None = None,
    *,
    quarantine_revision: int = 0,
) -> FateResult:
    """Derive final Fate from Edit observation Facts and versioned thresholds."""

    policy = policy or FatePolicy()
    provenance = Provenance(
        policy_version=policy.policy_version,
        quarantine_revision=quarantine_revision,
    )
    result = FateResult(provenance=provenance)
    observations = list(observations)
    external_totals = external_lines_after(observations)

    for observation in observations:
        try:
            raw_score = scorer(
                observation.applied_text,
                observation.observed_file_text,
            )
        except Exception:
            result.skipped["scorer_error"] += 1
            logger.warning(
                "fate_scorer_error",
                extra={
                    "org_id": observation.org_id,
                    "observation_id": observation.observation_id,
                },
                exc_info=True,
            )
            continue

        if (
            not isinstance(raw_score, int | float)
            or isinstance(raw_score, bool)
            or not math.isfinite(raw_score)
        ):
            result.skipped["invalid_score"] += 1
            logger.warning(
                "fate_scorer_invalid_score",
                extra={
                    "org_id": observation.org_id,
                    "observation_id": observation.observation_id,
                },
            )
            continue

        score = max(0.0, min(1.0, float(raw_score)))
        if score <= policy.deleted_max:
            fate = EditFate.DELETED
        elif score >= policy.unmodified_min:
            fate = EditFate.UNMODIFIED
        else:
            fate = EditFate.PARTIALLY_MODIFIED
        totals = external_totals.get(
            (
                observation.org_id,
                observation.agent_harness,
                observation.session_id,
                observation.call_id,
            )
        )
        result.fates.append(
            Fate(
                observation_id=observation.observation_id,
                org_id=observation.org_id,
                agent_harness=observation.agent_harness,
                session_id=observation.session_id,
                call_id=observation.call_id,
                score=score,
                fate=fate,
                external_lines_added=totals[0] if totals is not None else None,
                external_lines_removed=totals[1] if totals is not None else None,
                provenance=provenance,
            )
        )

    result.fates.sort(
        key=lambda item: (
            item.org_id,
            item.agent_harness,
            item.session_id,
            item.call_id,
            item.observation_id,
        )
    )
    return result


def external_lines_after(
    observations: list[EditObservation],
) -> dict[tuple[str, str, str, str], tuple[int, int]]:
    """Lines changed by something other than the agent, per edit, from that
    edit to session end.

    Keyed like :func:`attach_edit_retention` — ``(org_id, agent_harness, session_id,
    call_id)``. Each :class:`EditObservation` carries the counts for its *own*
    window, which closes at the agent's next edit of that file. A pair's
    ``edit_retention_score``, though, is measured against the file at session end,
    so what bears on it is every window from that edit onward: this sums
    them per file.

    A drop with lines here did not come from the agent revising itself —
    that is the disambiguation the edit retention score cannot make alone. It does
    *not* claim a human: a formatter, a linter, a watcher, or a rebase all
    land in the same counts, and grading that is a policy question for the
    consumer.

    Coverage is all-or-nothing per tail: an entry appears only when every
    window from the edit to session end carries counts. One unobserved
    window in the chain and the total would understate by an unknown amount,
    so it is omitted instead — absent, never a guessed sum (AGENTS.md).

    Pure and order-independent: observations are ordered by
    ``(occurred_at, call_id)``, never by ingest or read order (ADR 0001,
    non-negotiable rule 2).
    """
    by_file: dict[tuple[str, str, str, str], list[EditObservation]] = defaultdict(list)
    for o in observations:
        by_file[(o.org_id, o.agent_harness, o.session_id, o.file_path)].append(o)
    out: dict[tuple[str, str, str, str], tuple[int, int]] = {}
    for group in by_file.values():
        ordered = sorted(group, key=lambda o: (o.occurred_at, o.call_id))
        added = removed = 0
        covered = True
        # Walk the tail backwards: each edit's total is its own window plus
        # everything after it, and coverage fails forward from the first gap.
        for o in reversed(ordered):
            if o.external_lines_added is None or o.external_lines_removed is None:
                covered = False
                continue
            if not covered:
                continue
            added += o.external_lines_added
            removed += o.external_lines_removed
            out[(o.org_id, o.agent_harness, o.session_id, o.call_id)] = (added, removed)
    return out


def attach_edit_retention(
    decisions: list[DeveloperDecision],
    observations: list[EditObservation],
    scorer: Callable[[str, str], float],
) -> list[DeveloperDecision]:
    """Fill ``edit_retention_score`` from matching edit observations.

    Joins on ``(org_id, agent_harness, session_id, call_id)`` — the observation's dedup
    identity, matching the decision's ``call_id`` (Claude Code's
    ``tool_use_id``). A decision is only filled when its score is
    ``None`` (a vendor-supplied rate, i.e. Copilot's, always stands) and a
    matching pair exists. ``scorer(applied_text, observed_file_text)`` must return a float;
    the result is clamped to ``[0.0, 1.0]``, and a non-finite score skips the
    fill with a trail rather than poisoning the fact model's bounds.

    Scorer contract (normative): ``applied_text`` is the tool's written text —
    a whole file for Write, but only the ``new_string`` *snippet* for Edit —
    while ``observed_file_text`` is always the whole file at session end. The
    scorer must measure containment (how much applied text remains in the
    observed file),
    never symmetric distance: a normalized edit distance scores a perfectly
    surviving 300-byte Edit inside a 30 KB file at ~0.01. Cross-check any
    candidate metric against snippet-vs-whole-file pairs before plugging it
    in here.

    Returns a new list — input models are never mutated (facts are immutable;
    the filled copies are derived state for the caller to consume, ADR 0001).
    """
    by_key: dict[tuple[str, str, str, str], EditObservation] = {}
    for o in observations:
        # Keep-first on a duplicate key, mirroring the store's
        # first-write-wins posture — never last-wins dict insertion luck.
        by_key.setdefault((o.org_id, o.agent_harness, o.session_id, o.call_id), o)
    out: list[DeveloperDecision] = []
    for d in decisions:
        observation = (
            by_key.get((d.org_id, d.agent_harness, d.session_id, d.call_id))
            if d.call_id and d.edit_retention_score is None
            else None
        )
        if observation is None:
            out.append(d)
            continue
        score = scorer(
            observation.applied_text,
            observation.observed_file_text,
        )
        if not isinstance(score, (int, float)) or not math.isfinite(score):
            logger.warning(
                "edit_retention_scorer_non_finite",
                extra={"org_id": d.org_id, "call_id": d.call_id},
            )
            out.append(d)
            continue
        out.append(
            d.model_copy(
                update={"edit_retention_score": max(0.0, min(1.0, float(score)))}
            )
        )
    return out
