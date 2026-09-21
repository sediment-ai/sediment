# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Label-confidence inspection — a stratified sample of attributed completions with
``resolve_confidence``'s factor breakdown, for an operator to eyeball what
the label-confidence policy actually produced without writing Python. NVIDIA's
agentic-RL guidance treats "run the reward function against 50-100 outputs
and manually inspect the scores" as mandatory pre-training hygiene; this
module is that hygiene, over ``label_confidence.py``'s precedence ladder.

``build_label_confidence_inspection`` is a **thin, stateless aggregation over
attributed_completions** (ADR 0004, the same "never re-join" discipline
``outcome_report.py`` and ``rlvr.py`` follow): it reads a list of
``AttributedCompletion`` plus the org's inference calls (for the snippet text — a
field the attributed-completion layer does not carry) and never re-derives a
attribution, a decision join, or a CI join. ``generate_label_confidence_inspection``
is the one-call orchestration (``store`` + ``mirrors`` in, rows out) that
``sediment report label-confidence-inspection`` wraps, mirroring ``generate_model_report``'s
shape.

**Stratified sampling.** The precedence ladder in ``label_confidence.py`` has three
independent evidence axes an attributed completion can vary along:

* the decision branch (:func:`~sediment_export.label_confidence.decision_branch`) —
  ``abandoned``, ``no_decision``, ``explicit_reject``, ``explicit_accept``,
  ``implicit_reject``, ``implicit_accept_codex_neutral``,
  ``implicit_accept``;
* the CI bucket — ``ci_pass``, ``ci_fail``, ``ci_absent`` (no outcomes, or
  non-verdict-only — see :func:`~sediment_export.label_confidence.ci_passed`/
  :func:`~sediment_export.label_confidence.ci_failed`);
* the evidence source — ``git_notes``, ``jaccard``, ``abandonment``.
* the selected SFT recipe's eligibility source — ``explicit_accept``,
  ``edit_retention``, ``resolved_ci_pass``, or ineligible.

:func:`stratified_sample` groups attributed completions into one *cell* per
``(decision_branch, ci_bucket, attribution_source, eligibility_source)``
combination actually
present in the data, then round-robins across cells (in a fixed,
deterministic order — no randomness, so a re-run over the same attributed completions
reproduces the same sample) taking one attributed completion per cell per pass. When
``n`` is at least the number of non-empty cells, every real branch the
org's data actually exercises is represented in the sample; when ``n`` is
smaller, coverage is still spread as evenly as possible across cells
rather than skewed toward whichever branch happens to be most populous.

**No fabricated human judgment.** Every :class:`InspectionRow` carries a
``human_judgment`` field that is always ``None`` — a column for a human to
fill in later against real data, never a value this tool invents. Nothing
in this module computes or guesses at one.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from sediment_core import FactStore
from sediment_derive import (
    CIResolution,
    CIResolutionPolicy,
    MirrorManager,
    InferenceCall,
    RepositoryContext,
    inference_fact_id,
    render_scoring_text,
)

from .label_confidence import (
    ConfidenceBreakdown,
    DecisionBranch,
    LabelConfidencePolicy,
    ci_failed,
    ci_passed,
    decision_branch,
    resolve_ci_resolution,
    resolve_ci_resolution_result,
    resolve_confidence_breakdown,
    resolve_policy_v3_confidence,
)
from .attributed_completions import (
    AttributedCompletion,
    AttributedCompletionPolicy,
    _evidence_sort_key,
    assemble_attributed_completions_result,
)
from .sft import (
    SFT_RECIPE_VERSION,
    SFTEligibilitySource,
    SFTPolicy,
    _eligibility_source,
)

# Model output text is truncated to this many characters for the snippet
# field — long enough to be readable context, short enough that a table
# row (or a JSON row skimmed in a terminal) stays scannable.
SNIPPET_LENGTH = 200

CIBucket = str  # "ci_pass" | "ci_fail" | "ci_absent"


@dataclass(frozen=True)
class DecisionSummary:
    """One ``DeveloperDecision``, flattened to the fields the ladder
    actually reads: which agent harness emitted it, whether it was a real human
    gesture (``explicit``) or auto-applied/inferred, and whether it was an
    accept or a reject."""

    agent_harness: str
    explicit: bool
    accepted: bool


@dataclass(frozen=True)
class CIOutcomeSummary:
    """One ``CIOutcome`` with the evidence needed to audit its CI factor."""

    outcome_id: str
    provider: str
    repo: str
    commit_sha: str
    branch: str
    result: str
    workflow_name: str
    workflow_path: str | None
    run_id: str
    run_attempt: int | None
    workflow_id: str | None
    provider_result: str | None
    error_type: str | None
    reason: str | None
    source_event_type: str | None
    source_spec_version: str | None
    source_event_id: str | None


@dataclass(frozen=True)
class InspectionRow:
    """One sampled attributed completion's label-confidence-inspection row: enough context to judge
    whether ``confidence`` looks right, plus an always-empty
    ``human_judgment`` slot for a human to fill in later (never populated
    by this tool — see the module docstring's "No fabricated human
    judgment" section)."""

    org_id: str
    inference_call_id: str
    recipe_id: str
    recipe_version: int
    eligibility_source: SFTEligibilitySource | None
    repo: str | None
    commit_sha: str | None
    file_path: str | None
    completion_snippet: str | None
    decisions: list[DecisionSummary]
    ci_outcomes: list[CIOutcomeSummary]
    similarity_score: float | None
    attribution_source: str
    decision_branch: DecisionBranch
    ci_bucket: CIBucket
    confidence: ConfidenceBreakdown | None
    ci_resolution: CIResolution | None
    ci_resolution_skips: dict[str, int]
    policy_v3_confidence: float | None
    confidence_delta_from_policy_v3: float | None
    human_judgment: str | None = field(default=None)


def _ci_bucket(
    attributed_completion: AttributedCompletion,
    policy: CIResolutionPolicy | None = None,
    *,
    repository_context: RepositoryContext | None = None,
) -> CIBucket:
    """Which of the three CI states (pass/fail/absent) ``attributed_completion`` falls
    into — ``ci_absent`` covers both "no CI outcomes at all" and
    "non-verdict-only" (:func:`~sediment_export.label_confidence.ci_passed`/
    :func:`~sediment_export.label_confidence.ci_failed` agree neither is a signal)."""
    if ci_passed(attributed_completion, policy, repository_context=repository_context):
        return "ci_pass"
    if ci_failed(attributed_completion, policy, repository_context=repository_context):
        return "ci_fail"
    return "ci_absent"


def _cell_key(
    attributed_completion: AttributedCompletion,
    policy: CIResolutionPolicy | None = None,
    sft_policy: SFTPolicy | None = None,
    *,
    repository_context: RepositoryContext | None = None,
) -> tuple[DecisionBranch, CIBucket, str, str]:
    selected_sft_policy = sft_policy or SFTPolicy()
    eligibility_source, _ = _eligibility_source(
        attributed_completion,
        selected_sft_policy,
        repository_context=repository_context,
    )
    return (
        decision_branch(attributed_completion),
        _ci_bucket(
            attributed_completion, policy, repository_context=repository_context
        ),
        (
            "abandonment"
            if attributed_completion.abandonment is not None
            else str(attributed_completion.attribution_source)
        ),
        eligibility_source or "ineligible",
    )


def stratified_sample(
    attributed_completions: Sequence[AttributedCompletion],
    n: int,
    ci_resolution_policy: CIResolutionPolicy | None = None,
    sft_policy: SFTPolicy | None = None,
    *,
    repository_context: RepositoryContext | None = None,
) -> list[AttributedCompletion]:
    """Sample up to ``n`` attributed completions, spread as evenly as possible across every
    ``(decision_branch, ci_bucket, attribution_source, eligibility_source)``
    cell present in
    ``attributed_completions`` — see the module docstring's "Stratified sampling" section.

    Deterministic (no randomness): cells are visited in a fixed sorted
    order and, within a cell, attributed completions are taken in the order given, so
    re-running this over the same ``attributed_completions`` list reproduces an identical
    sample. Returns ``[]`` for ``n <= 0`` or an empty ``attributed_completions``; returns
    every attributed_completion (unsampled) when ``n >= len(attributed_completions)``.
    """
    if n <= 0 or not attributed_completions:
        return []

    cells: dict[
        tuple[DecisionBranch, CIBucket, str, str], deque[AttributedCompletion]
    ] = defaultdict(deque)
    for t in attributed_completions:
        cells[
            _cell_key(
                t,
                ci_resolution_policy,
                sft_policy,
                repository_context=repository_context,
            )
        ].append(t)

    queues = [cells[key] for key in sorted(cells)]
    sample: list[AttributedCompletion] = []
    while len(sample) < n and any(queues):
        for q in queues:
            if not q:
                continue
            sample.append(q.popleft())
            if len(sample) == n:
                break
    return sample


def _snippet(text: str) -> str:
    if len(text) <= SNIPPET_LENGTH:
        return text
    return text[:SNIPPET_LENGTH] + "…"


def build_label_confidence_inspection(
    inference_calls: Iterable[InferenceCall],
    attributed_completions: Iterable[AttributedCompletion],
    n: int = 50,
    policy: LabelConfidencePolicy | None = None,
    sft_policy: SFTPolicy | None = None,
    *,
    repository_context: RepositoryContext | None = None,
) -> list[InspectionRow]:
    """Stratify-sample up to ``n`` of ``attributed_completions`` and build one
    :class:`InspectionRow` per sampled attributed_completion, resolving each one's
    confidence breakdown fresh (never re-joining facts — the breakdown is
    computed straight from the attributed completion's own already-assembled fields, same
    as ``resolve_confidence`` itself).

    Rows are returned sorted by the attributed completion's own identity tuple
    ``(repo, commit_sha, file_path, inference_call_id)`` — the same ordering
    ``assemble_attributed_completions`` produces — so output is stable regardless of which
    stratification cell a attributed completion was drawn from.

    A attributed completion whose ``inference_call_id`` cannot be found among
    ``inference_calls``
    (a caller passing a mismatched pair) still gets a row — every other
    field is available from the attributed completion itself — but ``completion_snippet``
    is ``None`` rather than guessed at.
    Identified CI evidence requires the caller's complete repository context.
    """
    policy = policy or (sft_policy.label_confidence if sft_policy else None)
    policy = policy or LabelConfidencePolicy()
    sft_policy = sft_policy or SFTPolicy(label_confidence=policy)
    inference_calls_by_id = {inference_fact_id(call): call for call in inference_calls}
    attributed_completions_list = list(attributed_completions)
    sample = stratified_sample(
        attributed_completions_list,
        n,
        policy.ci_resolution,
        sft_policy,
        repository_context=repository_context,
    )
    sample_sorted = sorted(sample, key=_evidence_sort_key)

    rows: list[InspectionRow] = []
    for t in sample_sorted:
        inference_call = inference_calls_by_id.get(t.inference_call_id)
        ci_result = resolve_ci_resolution_result(
            t, policy.ci_resolution, repository_context=repository_context
        )
        ci_resolution = resolve_ci_resolution(
            t, policy.ci_resolution, repository_context=repository_context
        )
        confidence = resolve_confidence_breakdown(
            t, policy, repository_context=repository_context
        )
        policy_v3_confidence = resolve_policy_v3_confidence(t, policy)
        eligibility_source, _ = _eligibility_source(
            t, sft_policy, repository_context=repository_context
        )
        rows.append(
            InspectionRow(
                org_id=t.org_id,
                inference_call_id=t.inference_call_id,
                recipe_id=sft_policy.recipe_id,
                recipe_version=SFT_RECIPE_VERSION,
                eligibility_source=eligibility_source,
                repo=t.repo,
                commit_sha=t.commit_sha,
                file_path=t.file_path,
                completion_snippet=(
                    _snippet(render_scoring_text(inference_call))
                    if inference_call
                    else None
                ),
                decisions=[
                    DecisionSummary(
                        agent_harness=str(d.agent_harness),
                        explicit=d.explicit,
                        accepted=d.accepted,
                    )
                    for d in t.decisions
                ],
                ci_outcomes=[
                    CIOutcomeSummary(
                        outcome_id=o.outcome_id,
                        provider=str(o.provider),
                        repo=o.repo,
                        commit_sha=o.commit_sha,
                        branch=o.branch,
                        result=str(o.result),
                        workflow_name=o.workflow_name,
                        workflow_path=o.workflow_path,
                        run_id=o.run_id,
                        run_attempt=o.run_attempt,
                        workflow_id=o.workflow_id,
                        provider_result=o.provider_result,
                        error_type=o.error_type,
                        reason=o.reason,
                        source_event_type=o.source_event_type,
                        source_spec_version=o.source_spec_version,
                        source_event_id=o.source_event_id,
                    )
                    for o in t.ci_outcomes
                ],
                similarity_score=t.similarity_score,
                attribution_source=(
                    "abandonment"
                    if t.abandonment is not None
                    else str(t.attribution_source)
                ),
                decision_branch=decision_branch(t),
                ci_bucket=_ci_bucket(
                    t, policy.ci_resolution, repository_context=repository_context
                ),
                confidence=confidence,
                ci_resolution=ci_resolution,
                ci_resolution_skips=dict(ci_result.skipped),
                policy_v3_confidence=policy_v3_confidence,
                confidence_delta_from_policy_v3=(
                    confidence.final - policy_v3_confidence
                    if confidence is not None and policy_v3_confidence is not None
                    else None
                ),
                human_judgment=None,
            )
        )
    return rows


def generate_label_confidence_inspection(
    store: FactStore,
    mirrors: MirrorManager,
    org_id: str,
    n: int = 50,
    attributed_completion_policy: AttributedCompletionPolicy | None = None,
    label_confidence_policy: LabelConfidencePolicy | None = None,
    sft_policy: SFTPolicy | None = None,
) -> list[InspectionRow]:
    """The whole label-confidence-inspection sample as one call, mirroring
    ``generate_model_report``'s orchestration shape: read the org's
    completions, assemble its attributed completions (``assemble_attributed_completions`` — the clean
    attributed_completion, never re-joined), stratify-sample and build rows.
    ``sediment report label-confidence-inspection`` wraps exactly this.
    """
    with store.read_snapshot() as snapshot:
        completions = snapshot.read_rollout_inference_calls(org_id)
        assembled = assemble_attributed_completions_result(
            snapshot, mirrors, org_id, attributed_completion_policy
        )
        return build_label_confidence_inspection(
            completions,
            assembled.rows,
            n,
            label_confidence_policy,
            sft_policy,
            repository_context=assembled.repository_context,
        )
