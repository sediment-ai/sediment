# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Label-confidence policy (ADR 0004) — the precedence ladder that
resolves one ``confidence: float`` per
:class:`~sediment_export.attributed_completions.AttributedCompletion`, consumed by
the DPO (``dpo.py``), SFT (``sft.py``), and diff-SFT (``diff_sft.py``)
projections. :func:`resolve_confidence_breakdown` exposes the same
computation's three factors — decision, CI, similarity — for tooling
(``sediment report label-confidence-inspection``) that needs to show *why* a confidence
resolved the way it did, not just the final float; :func:`resolve_confidence`
is the float-only entry point every projection uses.

**The ladder** (highest precedence first):

1. An **abandoned explicit accept** resolves to
   ``explicit_reject_confidence``. The developer initially accepted the work,
   but no commit retained it after the abandonment horizon. This branch must
   precede the attached accept that makes the row grade-eligible.
2. An **explicit** developer decision (``DeveloperDecision.explicit is True``)
   is the strongest signal. ``accepted=True`` resolves near/at
   ``explicit_accept_confidence`` (default ``1.0``); ``accepted=False``
   resolves to ``explicit_reject_confidence`` (default ``0.0``) — a rejection
   is still a real attributed completion carrying real data, it just never becomes
   a ``chosen``/SFT-positive row (that gate lives in ``dpo.py``/``sft.py``, not
   here — this module only resolves a number).
3. An **implicit** decision (``explicit is False`` — auto-applied or
   survival-inferred) gets a smaller adjustment off ``baseline_confidence``:
   ``implicit_accept_multiplier`` (default ``1.1x``) for an
   implicit accept, ``implicit_reject_multiplier`` (default ``0.9x``, this
   module's own choice — a smaller *penalty*, not a veto, mirrors the accept
   bonus without making an implicit reject as final as an explicit one) for an
   implicit reject. **Two per-harness/per-signal nuances exist on top of this
   flat multiplier:**

   - Codex emits ``tool_decision`` only for actions it runs — an interactive
     reject produces no telemetry and there is no revert/undo channel, so
     Codex is an accept-only harness. An implicit accept whose decisions are
     *all* ``AgentHarness.CODEX`` therefore carries none of the "survived
     without revert" evidence an implicit accept normally carries, and
     resolves to a **neutral 1.0 multiplier** (``baseline_confidence``
     unadjusted) instead of ``implicit_accept_multiplier``. A Codex decision
     with ``explicit is True`` (a reviewed diff approval) is unaffected — it
     still gets the normal explicit-accept treatment in step 1, same as any
     other harness. This guard is checked **before** the
     ``edit_retention_score``
     interpolation below, so a (hypothetical — nothing populates it on Codex
     decisions) Codex ``edit_retention_score`` can never override the neutral-1.0
     outcome.
   - Implicit-accept decisions can carry a graded
     ``DeveloperDecision.edit_retention_score`` (``[0.0, 1.0]``: Copilot's vendor
     four-gram grade, or the assembly-time transcript fill) instead of only
     the flat accept/reject boolean. When present, it replaces the flat
     ``implicit_accept_multiplier`` with a linear interpolation between
     ``implicit_reject_multiplier`` (at ``edit_retention_score == 0.0``) and
     ``implicit_accept_multiplier`` (at ``edit_retention_score == 1.0``) — see
     :func:`_lerp` and :func:`_decision_factor`. The interpolation applies
     only in the implicit-**accept** branch; ``edit_retention_score`` is a
     *strength*
     signal, not an independent accept/reject signal, so it never re-routes a
     decision into the reject branch or interacts with the reject-wins
     tie-break above — it only replaces the accept-side multiplier. A decision
     set with no ``edit_retention_score`` (no vendor grade, no transcript pair) falls
     back to the flat ``implicit_accept_multiplier``.

   A broader per-harness policy matrix is deliberately not built (keep the
   mechanism minimal) — see :func:`_decision_factor`.
4. No decision at all (survival evidence only) starts from the same
   ``baseline_confidence``, unadjusted by step 2.
5. **Resolved CI pass/fail** multiplies onto the decision-based factor from steps 1-4
   (``ci_pass_multiplier`` / ``ci_fail_multiplier``) — it never overrides it
   (ADR 0004: CI doesn't replace explicit Developer-decision labels).
   A cancelled-only or absent CI outcome leaves the factor unchanged (``1.0``).
6. **CI reliability** discounts suspected-flake evidence independently of
   the categorical verdict. Non-verdict evidence remains neutral by default.
7. **Attribution similarity** discounts the result for JACCARD attributions
   only — multiplied by ``AttributedCompletion.similarity_score`` — since a
   weaker structural match is weaker evidence that the completion and the
   commit are really the same event. NOTES attribution is deterministic (a
   real session stamp, not a guess) and is never discounted.

The product is capped to ``[0.0, 1.0]`` at the end.

A attributed completion with **neither** a decision nor a CI outcome carries no
reward signal at all (``CONTEXT.md`` — "Reward signal": "A attributed completion
only becomes a training row once it carries a reward or an explicit
decision"): :func:`resolve_confidence` returns ``None`` for that case,
distinguishing it from a real (if low) resolved number so a projection can
filter on identity (``is None``) rather than guessing at a magic sentinel
float.

**Multiple decisions/CI outcomes on one attributed completion.**
``AttributedCompletion.decisions``/``ci_outcomes`` are lists (several decisions
can share a completion, e.g. an accept and a per-file reject; several CI runs
can share a commit, e.g. retries). Both ladders resolve conservatively:
*reject wins* — if any explicit decision on the attributed completion is a
reject, the attributed completion is treated as an explicit reject even if
another explicit decision on it is an accept (same tie-break at the implicit
tier). CI uses ``CIResolution``: attempts order by provider ``run_attempt``;
conflicting workflow verdicts produce no aggregate verdict; agreeing workflow
reliability takes ``min()``. Generated ids, URLs, capture time, and ingest
order never select an attempt.

``LabelConfidencePolicy.policy_version`` is this ladder's provenance stamp (ADR 0001),
same convention as ``AttributedCompletionPolicy``/``AttributionPolicy``/
``RolloutPolicy``/``RecoveryPolicy``: ``"1"`` was the flat-multiplier ladder,
``"2"`` adds the ``edit_retention_score`` interpolation above, ``"3"`` adds
abandonment precedence, and ``"4"`` adds attempt-aware CI resolution and its
independent reliability factor.
"""

from __future__ import annotations

from sediment_derive.repository_identity import RepositoryContext

from dataclasses import dataclass, field
from collections.abc import Iterable
from typing import Literal

from sediment_core import CIOutcome, CIResult, AgentHarness
from sediment_derive import (
    AttributionSource,
    CIResolution,
    CIResolutionPolicy,
    CIResolutionResult,
    derive_ci_resolution_result,
)

from .attributed_completions import AttributedCompletion


@dataclass(frozen=True)
class LabelConfidencePolicy:
    """The tunable knobs of the confidence ladder. See the module docstring
    for what each multiplies onto. Every confidence float is meant to land in
    ``[0.0, 1.0]`` before capping; multipliers may exceed ``1.0`` (a bonus) or
    sit below it (a penalty) — validated to stay non-negative and sane."""

    explicit_accept_confidence: float = 1.0
    explicit_reject_confidence: float = 0.0
    # The neutral starting point for a attributed completion with no explicit
    # decision: an implicit decision's baseline before its multiplier, and the
    # sole decision-based factor for a no-decision (survival-only) one.
    baseline_confidence: float = 0.6
    implicit_accept_multiplier: float = 1.1
    implicit_reject_multiplier: float = 0.9
    ci_pass_multiplier: float = 1.1
    ci_fail_multiplier: float = 0.7
    ci_resolution: CIResolutionPolicy = field(default_factory=CIResolutionPolicy)
    # Stamps which confidence-ladder shape produced a resolved number (ADR 0001
    # provenance discipline). Not yet threaded into any provenance string,
    # unlike the sibling policies, since LabelConfidencePolicy itself is not stored on a
    # AttributedCompletion.
    policy_version: str = "4"

    def __post_init__(self) -> None:
        for name in (
            "explicit_accept_confidence",
            "explicit_reject_confidence",
            "baseline_confidence",
        ):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(
                    f"LabelConfidencePolicy.{name} must be in [0.0, 1.0] (got {value})"
                )
        for name in (
            "implicit_accept_multiplier",
            "implicit_reject_multiplier",
            "ci_pass_multiplier",
            "ci_fail_multiplier",
        ):
            value = getattr(self, name)
            if value < 0.0:
                raise ValueError(
                    f"LabelConfidencePolicy.{name} must be >= 0.0 (got {value})"
                )


def _lerp(low: float, high: float, t: float) -> float:
    """Linear interpolation from ``low`` (at ``t=0.0``) to ``high`` (at
    ``t=1.0``). ``t`` is expected in ``[0.0, 1.0]`` (``DeveloperDecision.
    edit_retention_score``'s own schema-enforced bounds), so no extra clamping is
    done here — the ``resolve_confidence`` caller caps the final product
    anyway."""
    return low + (high - low) * t


DecisionBranch = Literal[
    "abandoned",
    "no_decision",
    "explicit_reject",
    "explicit_accept",
    "implicit_reject",
    "implicit_accept_codex_neutral",
    "implicit_accept",
]


def decision_branch(attributed_completion: AttributedCompletion) -> DecisionBranch:
    """Which decision branch (steps 1-4) ``attributed_completion`` falls into — the
    categorical label under :func:`_decision_factor`'s numeric resolution,
    exposed for tooling that needs to know *which* branch a attributed completion
    took (stratified sampling, a breakdown display) rather than just the
    resulting factor. ``_decision_factor`` is built on top of this so the
    two can never drift apart: change the branching here and both follow.
    ``implicit_accept`` covers both the flat-multiplier case and the graded
    ``edit_retention_score``-interpolated case — the branch label doesn't
    distinguish them, only ``_decision_factor``'s numeric result does, since
    ``edit_retention_score`` is a strength signal within the accept branch, not a
    separate branch of its own.
    """
    if attributed_completion.abandonment is not None:
        return "abandoned"
    if not attributed_completion.decisions:
        return "no_decision"
    explicit = [d for d in attributed_completion.decisions if d.explicit]
    if explicit:
        # Reject wins under multiple explicit decisions on one attributed completion (a
        # per-file reject alongside an accept, say) — the conservative
        # reading: never call a attributed completion with any explicit reject on it a
        # positive signal.
        if any(not d.accepted for d in explicit):
            return "explicit_reject"
        return "explicit_accept"
    # No explicit decision; fall back to the implicit ones, same reject-wins
    # tie-break.
    if any(not d.accepted for d in attributed_completion.decisions):
        return "implicit_reject"
    # Codex is accept-only (no interactive-reject or revert telemetry), so an
    # implicit accept sourced entirely from Codex carries no "survived without
    # revert" evidence — neutralize the bonus rather than reward a signal that
    # can never be counterbalanced. Checked BEFORE _decision_factor's
    # edit-retention interpolation: Codex never populates edit_retention_score,
    # but the precedence must hold even if it someday did.
    if all(
        d.agent_harness == AgentHarness.CODEX for d in attributed_completion.decisions
    ):
        return "implicit_accept_codex_neutral"
    return "implicit_accept"


def _decision_factor(
    attributed_completion: AttributedCompletion, policy: LabelConfidencePolicy
) -> float | None:
    """The decision-based factor (ladder steps 1-4), or ``None`` when the
    attributed_completion carries no decision at all (the caller falls back to
    ``baseline_confidence`` only when there's a CI outcome to combine it
    with; a attributed completion with neither is the ``resolve_confidence`` ``None`` case,
    decided one level up). A thin numeric lookup over :func:`decision_branch`
    — see that function for the actual branching logic/rationale."""
    if not attributed_completion.decisions:
        return None
    branch = decision_branch(attributed_completion)
    if branch == "abandoned":
        return policy.explicit_reject_confidence
    if branch == "explicit_reject":
        return policy.explicit_reject_confidence
    if branch == "explicit_accept":
        return policy.explicit_accept_confidence
    if branch == "implicit_reject":
        return policy.baseline_confidence * policy.implicit_reject_multiplier
    if branch == "implicit_accept_codex_neutral":
        return policy.baseline_confidence
    # A graded edit_retention_score replaces the flat implicit_accept_multiplier with
    # an interpolation between implicit_reject_multiplier (rate 0.0) and
    # implicit_accept_multiplier (rate 1.0): a stronger-surviving edit is
    # stronger evidence of a good completion, so the bonus scales with it
    # rather than being all-or-nothing. Several decisions on one labeled
    # completion take the minimum rate — the weakest-surviving edit is the
    # weakest evidence, the same conservative spirit as the reject-wins and
    # pass-anywhere-wins tie-breaks. A decision set where nobody carries a
    # rate falls back to the flat multiplier.
    edit_retention_scores = [
        d.edit_retention_score
        for d in attributed_completion.decisions
        if d.edit_retention_score is not None
    ]
    if edit_retention_scores:
        edit_retention_score = min(edit_retention_scores)
        factor = _lerp(
            policy.implicit_reject_multiplier,
            policy.implicit_accept_multiplier,
            edit_retention_score,
        )
        return policy.baseline_confidence * factor
    return policy.baseline_confidence * policy.implicit_accept_multiplier


def resolve_ci_resolution(
    attributed_completion: AttributedCompletion,
    policy: CIResolutionPolicy | None = None,
    *,
    repository_context: RepositoryContext | None = None,
) -> CIResolution | None:
    """Resolve one attributed completion's commit-level CI evidence."""

    result = resolve_ci_resolution_result(
        attributed_completion, policy, repository_context=repository_context
    )
    if attributed_completion.repo is None or attributed_completion.commit_sha is None:
        return None
    return next(
        (
            resolution
            for resolution in result.resolutions
            if resolution.repository_identity
            == attributed_completion.repository_identity
            and (
                resolution.repository_identity is not None
                or resolution.repo == attributed_completion.repo
            )
            and resolution.commit_sha == attributed_completion.commit_sha
        ),
        None,
    )


def resolve_ci_resolution_result(
    attributed_completion: AttributedCompletion,
    policy: CIResolutionPolicy | None = None,
    *,
    outcomes: Iterable[CIOutcome] | None = None,
    repository_context: RepositoryContext | None = None,
) -> CIResolutionResult:
    """Resolve CI evidence and retain its closed skip tally for projections."""

    return derive_ci_resolution_result(
        attributed_completion.ci_outcomes if outcomes is None else outcomes,
        policy,
        quarantine_revision=attributed_completion.provenance.quarantine_revision,
        repository_context=repository_context,
    )


def ci_passed(
    attributed_completion: AttributedCompletion,
    policy: CIResolutionPolicy | None = None,
    *,
    repository_context: RepositoryContext | None = None,
) -> bool:
    """Whether attempt-aware CI resolution produces a passing verdict."""

    resolution = resolve_ci_resolution(
        attributed_completion, policy, repository_context=repository_context
    )
    return resolution is not None and resolution.verdict == CIResult.PASSED


def ci_failed(
    attributed_completion: AttributedCompletion,
    policy: CIResolutionPolicy | None = None,
    *,
    repository_context: RepositoryContext | None = None,
) -> bool:
    """Whether attempt-aware CI resolution produces a failing verdict."""

    resolution = resolve_ci_resolution(
        attributed_completion, policy, repository_context=repository_context
    )
    return resolution is not None and resolution.verdict == CIResult.FAILED


@dataclass(frozen=True)
class ConfidenceBreakdown:
    """The four factors :func:`resolve_confidence_breakdown` multiplies
    together — exposed so an operator (or ``sediment report
    label-confidence-inspection``) can see *why* a attributed completion resolved to its
    confidence, not just the final number.

    ``decision_factor`` is ladder steps 1-4 (abandonment, explicit accept/reject,
    implicit accept/reject incl. the Codex neutral case, or
    ``baseline_confidence`` for survival-only); ``ci_factor`` is step 5
    (``ci_pass_multiplier``/``ci_fail_multiplier``/neutral ``1.0``);
    ``ci_reliability`` is step 6; ``similarity_discount`` is step 7
    (``AttributedCompletion.similarity_score`` for a
    JACCARD attribution, neutral ``1.0`` for NOTES). ``final`` is the
    product of all four, capped to ``[0.0, 1.0]`` — the same value
    :func:`resolve_confidence` returns. The three factors are recorded
    *uncapped*: multiplying them back together and capping reproduces
    ``final`` exactly, which is what makes this a faithful breakdown rather
    than a lossy summary."""

    decision_factor: float
    ci_factor: float
    ci_reliability: float
    similarity_discount: float
    final: float


def resolve_confidence_breakdown(
    attributed_completion: AttributedCompletion,
    policy: LabelConfidencePolicy | None = None,
    *,
    repository_context: RepositoryContext | None = None,
) -> ConfidenceBreakdown | None:
    """Resolve ``attributed_completion``'s confidence with its factor breakdown, per the
    module's precedence ladder — or ``None`` when the attributed completion carries neither
    a decision nor a CI outcome (no reward signal at all, ``CONTEXT.md``'s
    "Reward signal" entry). :func:`resolve_confidence` is a thin wrapper
    over this that returns just ``.final``; both share this one
    implementation so the two can never drift apart.
    """
    policy = policy or LabelConfidencePolicy()

    if (
        attributed_completion.abandonment is None
        and not attributed_completion.decisions
        and not attributed_completion.ci_outcomes
    ):
        return None

    decision_factor = _decision_factor(attributed_completion, policy)
    if decision_factor is None:
        # Survival evidence only: no decision, but a CI outcome exists (the
        # `not ... and not ...` guard above already ruled out "neither").
        decision_factor = policy.baseline_confidence

    resolution = resolve_ci_resolution(
        attributed_completion,
        policy.ci_resolution,
        repository_context=repository_context,
    )
    ci_factor = 1.0
    ci_reliability = 1.0
    if resolution is not None and resolution.verdict == CIResult.PASSED:
        ci_factor = policy.ci_pass_multiplier
        assert resolution.reliability is not None
        ci_reliability = resolution.reliability
    elif resolution is not None and resolution.verdict == CIResult.FAILED:
        ci_factor = policy.ci_fail_multiplier
        assert resolution.reliability is not None
        ci_reliability = resolution.reliability

    # JACCARD is a similarity guess; NOTES is a deterministic session
    # stamp and needs no discount.
    similarity_discount = 1.0
    if attributed_completion.attribution_source == AttributionSource.JACCARD:
        similarity_discount = attributed_completion.similarity_score

    confidence = decision_factor * ci_factor * ci_reliability * similarity_discount
    final = max(0.0, min(1.0, confidence))

    return ConfidenceBreakdown(
        decision_factor=decision_factor,
        ci_factor=ci_factor,
        ci_reliability=ci_reliability,
        similarity_discount=similarity_discount,
        final=final,
    )


def resolve_confidence(
    attributed_completion: AttributedCompletion,
    policy: LabelConfidencePolicy | None = None,
    *,
    repository_context: RepositoryContext | None = None,
) -> float | None:
    """Resolve one confidence float for ``attributed_completion``, per the module's
    precedence ladder — or ``None`` when the attributed completion carries neither a
    decision nor a CI outcome (no reward signal at all, ``CONTEXT.md``'s
    "Reward signal" entry). Callers (``dpo.py``, ``sft.py``, ``diff_sft.py``)
    filter on identity (``is None``), never on a magic sentinel float.

    A thin wrapper over :func:`resolve_confidence_breakdown` — same
    computation, ``.final`` only — so the two can never drift apart.
    """
    breakdown = resolve_confidence_breakdown(
        attributed_completion, policy, repository_context=repository_context
    )
    return breakdown.final if breakdown is not None else None


def resolve_policy_v3_confidence(
    attributed_completion: AttributedCompletion,
    policy: LabelConfidencePolicy | None = None,
) -> float | None:
    """Recompute the prior version-3 confidence for diagnostic comparison.

    Version 3 treated any pass as the CI verdict and applied no independent
    CI reliability factor. Export projections must use :func:`resolve_confidence`;
    this function exists only for inspection, calibration, and sensitivity
    comparisons across the policy-version boundary.
    """

    policy = policy or LabelConfidencePolicy()
    if (
        attributed_completion.abandonment is None
        and not attributed_completion.decisions
        and not attributed_completion.ci_outcomes
    ):
        return None
    decision_factor = _decision_factor(attributed_completion, policy)
    if decision_factor is None:
        decision_factor = policy.baseline_confidence
    results = {outcome.result for outcome in attributed_completion.ci_outcomes}
    ci_factor = 1.0
    if CIResult.PASSED in results:
        ci_factor = policy.ci_pass_multiplier
    elif CIResult.FAILED in results:
        ci_factor = policy.ci_fail_multiplier
    similarity_discount = 1.0
    if attributed_completion.attribution_source == AttributionSource.JACCARD:
        similarity_discount = attributed_completion.similarity_score
    confidence = decision_factor * ci_factor * similarity_discount
    return max(0.0, min(1.0, confidence))
