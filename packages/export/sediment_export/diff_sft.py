# SPDX-License-Identifier: AGPL-3.0-or-later
"""Project attributed completions into conversational diff-SFT rows.

Rows group by ``(inference_call_id, commit_sha)`` and reuse ``SFTPolicy`` for
eligibility and label confidence. The prompt uses the shared canonical-to-
trainer mapper. The completion is one assistant message containing the exact
unified-diff sections for every attributed file, selected from mirror bytes and
kept in commit-diff order.

The projection never synthesizes patch headers or flattens added lines. A
missing mirror, commit diff, attributed-file section, or empty selected patch skips
and counts the whole sample. Sediment evidence stays under ``metadata``.
"""

from __future__ import annotations

import logging
from sediment_derive.repository_identity import (
    RepositoryContext,
    RepositoryIdentity,
    LegacyRepositoryKey,
    IdentifiedRepositoryKey,
    CommitKey,
    commit_sort_key,
)

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Literal, Never

from sediment_core import NonEmptyId

from sediment_derive import (
    AttributionSource,
    DIFF_SKIP_REASONS,
    DiffSkipReason,
    DiffParseResult,
    parse_diff_sections,
    MirrorError,
    MirrorManager,
    InferenceCall,
    Provenance,
    Split,
    inference_model,
)

from .jsonl import ExportRow
from .schema_identity import DIFF_SFT_SAMPLE_SCHEMA_ID, DIFF_SFT_SAMPLE_SCHEMA_VERSION
from .label_confidence import resolve_ci_resolution_result, resolve_confidence
from .sft import (
    SFT_RECIPE_VERSION,
    SFT_SKIP_REASONS,
    SFTEligibilitySource,
    SFTPolicy,
    SFTRecipeId,
    SFTRecipeVersion,
    SFTSkipReason,
    _eligibility_source,
    _explicit_reject,
)
from .attributed_completions import (
    AttributedCompletion,
    session_commit_observation_ids,
)
from .trainer import (
    TrainerAssistantMessage,
    TrainerMappingError,
    TrainerMessage,
    map_inference_prompt,
    validate_training_representation,
)

logger = logging.getLogger("sediment.export.diff_sft")

type DiffSFTSkipReason = (
    SFTSkipReason
    | DiffSkipReason
    | Literal[
        "repo_mismatch",
        "split_mismatch",
        "eligibility_source_mismatch",
        "mirror_absent",
        "commit_diff_unavailable",
        "empty_patch",
        "file_diff_unavailable",
    ]
)
DIFF_SFT_SKIP_REASONS: tuple[DiffSFTSkipReason, ...] = (
    *SFT_SKIP_REASONS,
    *DIFF_SKIP_REASONS,
    "repo_mismatch",
    "split_mismatch",
    "eligibility_source_mismatch",
    "mirror_absent",
    "commit_diff_unavailable",
    "empty_patch",
    "file_diff_unavailable",
)


@dataclass(frozen=True)
class SourceIds:
    """The identifiers contributed by a diff-SFT group's facts."""

    inference_call_id: str
    decision_ids: list[str]
    ci_outcome_ids: list[str]
    session_commit_observation_ids: tuple[NonEmptyId, ...] = ()


@dataclass(frozen=True)
class DiffSFTMetadata:
    """Evidence kept outside the trainer's prompt and patch completion."""

    org_id: str
    session_id: str
    repo: str
    commit_sha: str
    source_model: str
    completion_id: str
    recipe_id: SFTRecipeId
    recipe_version: SFTRecipeVersion
    eligibility_source: SFTEligibilitySource
    label_confidence: float
    ci_reliability: float | None
    provenance: Provenance
    split: Split
    source_ids: SourceIds
    repository_identity: RepositoryIdentity | None = None
    attribution_sources: tuple[AttributionSource, ...] = ()
    schema_id: Literal[DIFF_SFT_SAMPLE_SCHEMA_ID] = DIFF_SFT_SAMPLE_SCHEMA_ID
    schema_version: Literal[3] = DIFF_SFT_SAMPLE_SCHEMA_VERSION


@dataclass(frozen=True)
class DiffSFTSample:
    """One diff-shaped SFT training row: a completion's message history plus
    the concrete per-file edits its commit made, honest text only (module
    docstring)."""

    prompt: list[TrainerMessage]
    completion: list[TrainerAssistantMessage]
    tools: list[Never]
    metadata: DiffSFTMetadata


@dataclass
class Projection:
    """Mirrors ``sft.Projection``/``dpo.Projection``: the rows to write plus
    the skip tally (reason -> count) for the structured summary log line."""

    rows: list[DiffSFTSample] = field(default_factory=list)
    skipped: Counter[str] = field(default_factory=Counter)


def _group_key(attributed_completion: AttributedCompletion) -> tuple:
    row = attributed_completion
    identity = row.repository_identity
    repository = (
        LegacyRepositoryKey(row.org_id, row.repo)
        if identity is None
        else IdentifiedRepositoryKey(row.org_id, identity)
    )
    return (row.inference_call_id, CommitKey(repository, row.commit_sha))


def project_diff_sft(
    attributed_completions: Iterable[AttributedCompletion],
    inference_calls: Mapping[str, InferenceCall],
    mirrors: MirrorManager,
    policy: SFTPolicy | None = None,
    *,
    repository_context: RepositoryContext | None = None,
) -> Projection:
    """Project ``attributed_completions`` into diff-shaped SFT samples, per the module
    docstring's grouping/disqualification/confidence rules. Inputs require
    canonical assembly or a validated bundle; this low-level API cannot verify
    an omitted identity population.

    ``inference_calls`` maps a attributed-completion id to its inference-call
    fact, matching ``sft.project_sft`` and ``dpo.project_dpo``.
    ``mirrors`` opens the same read-only bare
    mirrors ``attribution.py`` reads from; a group whose repo was never
    mirrored, or whose commit the mirror can no longer diff, is skipped —
    never fetched (derivations must not touch the network, ``mirror.py``).
    """
    policy = policy or SFTPolicy()
    out = Projection()
    parsed_commits: dict[CommitKey, DiffParseResult] = {}

    groups: dict[tuple[str, CommitKey], list[AttributedCompletion]] = {}
    for attributed_completion in attributed_completions:
        if attributed_completion.abandonment is not None:
            out.skipped["abandoned"] += 1
            continue
        groups.setdefault(_group_key(attributed_completion), []).append(
            attributed_completion
        )

    # Visit each commit once without retaining every commit's full patch text.
    ordered_groups = sorted(
        groups.items(),
        key=lambda item: (commit_sort_key(item[0][1]), item[0][0]),
    )
    for (inference_call_id, qualified_commit), members in ordered_groups:
        commit_sha = qualified_commit.commit_sha
        members = sorted(members, key=lambda t: t.file_path)

        # Identity groups valid aliases, but each supplied name must still be
        # supported by the declared population before any member contributes.
        if repository_context is not None and any(
            repository_context.commit_key(
                member.org_id,
                member.repo,
                member.commit_sha,
                repository_identity=member.repository_identity,
            )
            != qualified_commit
            for member in members
        ):
            out.skipped["repository_identity_unresolved"] += 1
            continue
        repo = (
            repository_context.repo_for(qualified_commit.repository)
            if repository_context is not None
            else min(member.repo for member in members)
        )

        splits = {t.split for t in members}
        if len(splits) > 1:
            logger.error(
                "diff_sft_split_mismatch",
                extra={
                    "inference_call_id": inference_call_id,
                    "commit_sha": commit_sha,
                    "splits": sorted(splits),
                },
            )
            out.skipped["split_mismatch"] += 1
            continue

        if any(_explicit_reject(t) for t in members):
            out.skipped["explicit_reject"] += 1
            continue
        ci_result = resolve_ci_resolution_result(
            members[0],
            policy.label_confidence.ci_resolution,
            outcomes={
                outcome.outcome_id: outcome
                for member in members
                for outcome in member.ci_outcomes
            }.values(),
            repository_context=repository_context,
        )
        out.skipped.update(ci_result.skipped)
        member_sources: list[SFTEligibilitySource] = []
        member_skip_reason: str | None = None
        for member in members:
            source, skip_reason = _eligibility_source(
                member, policy, repository_context=repository_context
            )
            if source is None:
                member_skip_reason = skip_reason
                break
            member_sources.append(source)
        if member_skip_reason is not None:
            out.skipped[member_skip_reason] += 1
            continue
        eligibility_sources = set(member_sources)
        if len(eligibility_sources) != 1:
            out.skipped["eligibility_source_mismatch"] += 1
            continue
        eligibility_source = member_sources[0]

        member_confidences = [
            resolve_confidence(
                t, policy.label_confidence, repository_context=repository_context
            )
            for t in members
        ]
        if any(c is None for c in member_confidences):
            # Unreachable in practice: _eligibility_source already required
            # the selected recipe's positive evidence on every member. Kept as a
            # fail-soft guard (skip + count, never crash an export), the
            # same posture sft.py takes on its own None branch.
            out.skipped["no_reward_signal"] += 1
            continue
        confidence = min(member_confidences)
        if confidence < policy.min_confidence:
            out.skipped["below_confidence_floor"] += 1
            continue

        inference_call = inference_calls.get(inference_call_id)
        if inference_call is None:
            out.skipped["inference_call_not_found"] += 1
            continue
        model = inference_model(inference_call)
        if model is None:
            out.skipped["model_absent"] += 1
            continue
        try:
            prompt = map_inference_prompt(inference_call)
        except TrainerMappingError as exc:
            out.skipped[exc.reason] += 1
            continue

        mirror = mirrors.open_repository(qualified_commit.repository)
        if mirror is None:
            logger.warning(
                "diff_sft_mirror_absent", extra={"repo": repo, "commit_sha": commit_sha}
            )
            out.skipped["mirror_absent"] += 1
            continue

        commit_key = qualified_commit
        if commit_key not in parsed_commits:
            try:
                raw_diff = mirror.fetch_commit_diff(repo, commit_sha)
            except MirrorError as exc:
                logger.warning(
                    "diff_sft_commit_diff_unavailable",
                    extra={"repo": repo, "commit_sha": commit_sha, "error": str(exc)},
                )
                out.skipped["commit_diff_unavailable"] += 1
                continue
            if not raw_diff.strip():
                out.skipped["empty_patch"] += 1
                continue
            parsed = parse_diff_sections(raw_diff)
            parsed_commits.clear()
            parsed_commits[commit_key] = parsed
            out.skipped.update(parsed.skipped)
        required_paths = {member.file_path for member in members}
        sections = parsed_commits[commit_key].sections
        available_paths = {section.file_path for section in sections}
        missing_paths = sorted(required_paths - available_paths)
        if missing_paths:
            logger.warning(
                "diff_sft_file_diff_unavailable",
                extra={
                    "repo": repo,
                    "commit_sha": commit_sha,
                    "file_paths": missing_paths,
                },
            )
            out.skipped["file_diff_unavailable"] += 1
            continue
        patch = "".join(
            section.patch for section in sections if section.file_path in required_paths
        )
        decision_ids = sorted({d.decision_id for t in members for d in t.decisions})
        ci_outcome_ids = sorted({o.outcome_id for t in members for o in t.ci_outcomes})
        ci_reliability = min(
            (
                resolution.reliability
                for resolution in ci_result.resolutions
                if resolution.reliability is not None
            ),
            default=None,
        )

        sample = DiffSFTSample(
            prompt=prompt,
            completion=[{"role": "assistant", "content": patch}],
            tools=[],
            metadata=DiffSFTMetadata(
                org_id=members[0].org_id,
                session_id=members[0].session_id,
                repo=repo,
                repository_identity=members[0].repository_identity,
                commit_sha=commit_sha,
                source_model=model,
                completion_id=inference_call_id,
                recipe_id=policy.recipe_id,
                recipe_version=SFT_RECIPE_VERSION,
                eligibility_source=eligibility_source,
                label_confidence=confidence,
                ci_reliability=ci_reliability,
                provenance=members[0].provenance,
                split=members[0].split,
                attribution_sources=tuple(
                    sorted(
                        {
                            member.attribution_source
                            for member in members
                            if member.attribution_source is not None
                        },
                        key=lambda value: value.value,
                    )
                ),
                source_ids=SourceIds(
                    inference_call_id=inference_call_id,
                    decision_ids=decision_ids,
                    ci_outcome_ids=ci_outcome_ids,
                    session_commit_observation_ids=tuple(
                        sorted(
                            {
                                identifier
                                for member in members
                                for identifier in session_commit_observation_ids(
                                    member, repository_context=repository_context
                                )
                            }
                        )
                    ),
                ),
            ),
        )
        try:
            validate_training_representation(sample)
        except TrainerMappingError as exc:
            out.skipped[exc.reason] += 1
            continue
        out.rows.append(sample)

    out.rows.sort(key=lambda row: (row.metadata.completion_id, row.metadata.commit_sha))
    logger.info(
        "diff_sft_projected",
        extra={"rows": len(out.rows), "skipped": dict(out.skipped)},
    )
    return out


def to_export_rows(rows: Iterable[DiffSFTSample]) -> list[ExportRow]:
    """Adapt ``DiffSFTSample`` rows to ``jsonl.py``'s ``ExportRow``
    destination shape — reused, not reforked (ADR 0004): the write path lives
    exactly once in ``jsonl.py``, for every projection."""
    return [ExportRow(split=row.metadata.split, body=asdict(row)) for row in rows]
