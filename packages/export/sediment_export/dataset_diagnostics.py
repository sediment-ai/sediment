# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dataset diagnostics for DPO/SFT exports.

These rows are a derived report over already-projected ``DPOPair`` and
``SFTSample`` rows. They do not persist anything and deliberately reuse each
row's existing ``prompt`` field for the train/eval duplicate check rather
than reconstructing message history from facts again.

Every result stays stratified by evidence recipe, recipe version, and label
or eligibility source. The report never pools heterogeneous recipe rows.
"""

from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from sediment_core import FactStore
from sediment_derive import (
    EVAL,
    TRAIN,
    FatePolicy,
    FateResult,
    MirrorManager,
    InferenceCall,
    Provenance,
    Split,
    derive_fate_result,
    four_gram_containment,
    inference_fact_id,
    inference_model,
    jaccard_tokens,
    tokenize,
)

from .dpo import (
    DPOPair,
    DPO_RECIPE_VERSION,
    DPOPolicy,
    _bucket_candidates,
    _classify,
    _dedup_by_completion,
    project_dpo,
)
from sediment_derive.repository_identity import RepositoryContext
from .label_confidence import resolve_confidence
from .sft import (
    SFT_RECIPE_VERSION,
    SFTPolicy,
    SFTSample,
    _eligibility_source,
    _explicit_reject,
    project_sft,
)
from .attributed_completions import (
    AbandonmentSummary,
    AttributedCompletion,
    AttributedCompletionPolicy,
    assemble_attributed_completions_result,
    build_abandonment_summary,
)

DatasetKind = Literal["dpo", "sft"]
DEFAULT_DPO_NEAR_DUPLICATE_THRESHOLD = 0.8
BucketSizeBin = Literal["1", "2", "3-5", "6-10", "10+"]

DPO_BUCKET_SIZE_BINS: tuple[BucketSizeBin, ...] = ("1", "2", "3-5", "6-10", "10+")


@dataclass(frozen=True)
class ModelBalanceRow:
    """Row count for one export kind and model."""

    dataset: DatasetKind
    recipe_id: str
    recipe_version: int
    chosen_label_source: str | None
    rejected_label_source: str | None
    eligibility_source: str | None
    model: str
    rows: int


@dataclass(frozen=True)
class DistributionStats:
    """Basic distribution summary for one numeric signal."""

    count: int
    mean: float
    median: float
    min: float
    max: float
    stdev: float


@dataclass(frozen=True)
class ConfidenceDistributionRow:
    """Distribution for one confidence-like metric, per model or overall."""

    dataset: DatasetKind
    recipe_id: str
    recipe_version: int
    chosen_label_source: str | None
    rejected_label_source: str | None
    eligibility_source: str | None
    metric: str
    model: str
    stats: DistributionStats


@dataclass(frozen=True)
class CrossSplitDuplicateExample:
    """One normalized prompt that appears in both train and eval."""

    prompt: str
    train_count: int
    eval_count: int
    train_row_ids: list[str]
    eval_row_ids: list[str]


@dataclass(frozen=True)
class CrossSplitDuplicateReport:
    """Duplicate prompt summary for one export kind."""

    dataset: DatasetKind
    recipe_id: str
    recipe_version: int
    chosen_label_source: str | None
    rejected_label_source: str | None
    eligibility_source: str | None
    duplicate_prompt_count: int
    examples: list[CrossSplitDuplicateExample] = field(default_factory=list)


@dataclass(frozen=True)
class ConfidenceFloorExclusionRow:
    """Per-model SFT confidence-floor exclusion rate."""

    dataset: DatasetKind
    recipe_id: str
    recipe_version: int
    eligibility_source: str
    model: str
    otherwise_eligible_completions: int
    excluded_by_floor: int
    exclusion_rate: float


@dataclass(frozen=True)
class DPONearDuplicateBucketExample:
    """One pair of distinct DPO prompt buckets with near-duplicate text."""

    org_id: str
    model: str
    similarity: float
    bucket_a_prompt: str
    bucket_b_prompt: str
    bucket_a_rows: int
    bucket_b_rows: int
    bucket_a_row_ids: list[str]
    bucket_b_row_ids: list[str]


@dataclass(frozen=True)
class DPONearDuplicateBucketReport:
    """Near-duplicate prompt buckets that exact DPO bucketing split.

    The comparison is exhaustive over distinct DPO prompt buckets within the
    same ``(org_id, model)`` group: every bucket is compared against every other
    bucket in that group. That is O(bucket_count^2) in the number of distinct
    buckets, not total prompt rows, which is acceptable at the expected scale of
    tens to low hundreds of buckets. This report does not sample or truncate
    comparisons; only the stored examples list is capped by the caller.

    Similarity uses symmetric token-set Jaccard over representative prompt
    text. ``four_gram_containment`` is directional and tuned for snippet-vs-file
    edit retention scoring, while DPO prompt buckets are roughly comparable prompt
    texts. The default 0.8 threshold flags one- or two-token rephrasings in
    otherwise identical prompts while avoiding broad same-domain boilerplate
    matches.
    """

    recipe_id: str = "dpo_human"
    recipe_version: int = DPO_RECIPE_VERSION
    chosen_label_source: str = "explicit_accept"
    rejected_label_source: str = "explicit_reject"
    threshold: float = DEFAULT_DPO_NEAR_DUPLICATE_THRESHOLD
    bucket_count: int = 0
    comparable_pair_count: int = 0
    near_miss_pair_count: int = 0
    examples: list[DPONearDuplicateBucketExample] = field(default_factory=list)


@dataclass(frozen=True)
class DPOBucketSparsityReport:
    """Pre-pairing DPO prompt-bucket sparsity summary."""

    recipe_id: str
    recipe_version: int
    chosen_label_source: str
    rejected_label_source: str
    total_buckets: int
    total_candidates: int
    bucket_size_histogram: dict[BucketSizeBin, int]
    singleton_candidates: int
    singleton_candidate_fraction: float
    cap_hit_buckets: int


@dataclass(frozen=True)
class DatasetDiagnostics:
    """All diagnostics for one DPO/SFT export run."""

    model_balance: list[ModelBalanceRow]
    confidence_distributions: list[ConfidenceDistributionRow]
    cross_split_duplicates: list[CrossSplitDuplicateReport]
    confidence_floor_exclusions: list[ConfidenceFloorExclusionRow]
    dpo_bucket_sparsity: list[DPOBucketSparsityReport]
    dpo_near_duplicate_buckets: list[DPONearDuplicateBucketReport] = field(
        default_factory=list
    )
    abandonment: AbandonmentSummary = field(default_factory=AbandonmentSummary)
    fate: FateDiagnostic = field(default_factory=lambda: FateDiagnostic())


@dataclass(frozen=True)
class FateDiagnostic:
    """Final-Fate counts over Edit observations joined to report evidence."""

    fates: dict[str, int] = field(default_factory=dict)
    explicit_accept_fates: dict[str, int] = field(default_factory=dict)
    fates_with_external_changes: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    provenance: Provenance | None = None


@dataclass(frozen=True)
class _PromptRecord:
    split: Split
    row_id: str
    prompt: str


@dataclass(frozen=True)
class _DPOBucket:
    org_id: str
    model: str
    prompt_key: tuple[tuple[str, str], ...]
    representative_prompt: str
    row_ids: list[str]


type _RecipeStratum = tuple[str, int, str | None, str | None, str | None]


def _row_stratum(row: DPOPair | SFTSample) -> _RecipeStratum:
    metadata = row.metadata
    return (
        metadata.recipe_id,
        metadata.recipe_version,
        getattr(metadata, "chosen_label_source", None),
        getattr(metadata, "rejected_label_source", None),
        getattr(metadata, "eligibility_source", None),
    )


def _stratum_values(stratum: _RecipeStratum) -> dict[str, object]:
    return dict(
        recipe_id=stratum[0],
        recipe_version=stratum[1],
        chosen_label_source=stratum[2],
        rejected_label_source=stratum[3],
        eligibility_source=stratum[4],
    )


def _distribution(values: list[float]) -> DistributionStats:
    return DistributionStats(
        count=len(values),
        mean=statistics.mean(values),
        median=statistics.median(values),
        min=min(values),
        max=max(values),
        stdev=statistics.stdev(values) if len(values) > 1 else 0.0,
    )


def _balance_rows(
    dataset: DatasetKind, rows: Iterable[DPOPair] | Iterable[SFTSample]
) -> list[ModelBalanceRow]:
    counts = Counter((_row_stratum(row), row.metadata.source_model) for row in rows)
    return [
        ModelBalanceRow(
            dataset=dataset,
            **_stratum_values(stratum),
            model=model,
            rows=count,
        )
        for (stratum, model), count in sorted(counts.items())
    ]


def _distribution_rows(
    dataset: DatasetKind,
    rows: Iterable[DPOPair] | Iterable[SFTSample],
    metrics: dict[str, str],
) -> list[ConfidenceDistributionRow]:
    rows = list(rows)
    out: list[ConfidenceDistributionRow] = []

    for metric, attr in metrics.items():
        values_by_stratum_model: dict[tuple[_RecipeStratum, str], list[float]] = (
            defaultdict(list)
        )
        overall_by_stratum: dict[_RecipeStratum, list[float]] = defaultdict(list)
        for row in rows:
            value = float(getattr(row.metadata, attr))
            stratum = _row_stratum(row)
            values_by_stratum_model[(stratum, row.metadata.source_model)].append(value)
            overall_by_stratum[stratum].append(value)

        for stratum, overall in sorted(overall_by_stratum.items()):
            out.append(
                ConfidenceDistributionRow(
                    dataset=dataset,
                    **_stratum_values(stratum),
                    metric=metric,
                    model="overall",
                    stats=_distribution(overall),
                )
            )
        for (stratum, model), values in sorted(values_by_stratum_model.items()):
            out.append(
                ConfidenceDistributionRow(
                    dataset=dataset,
                    **_stratum_values(stratum),
                    metric=metric,
                    model=model,
                    stats=_distribution(values),
                )
            )
    return out


def _prompt_text(prompt: object) -> str:
    if isinstance(prompt, str):
        return prompt
    return json.dumps(prompt, ensure_ascii=False, sort_keys=True)


def _dpo_row_id(row: DPOPair) -> str:
    return f"{row.metadata.chosen_completion_id}>{row.metadata.rejected_completion_id}"


def _prompt_records(
    rows: Iterable[DPOPair] | Iterable[SFTSample],
    row_id: Callable[[DPOPair], str] | Callable[[SFTSample], str],
) -> list[_PromptRecord]:
    return [
        _PromptRecord(
            split=row.metadata.split,
            row_id=row_id(row),
            prompt=_prompt_text(row.prompt),
        )
        for row in rows
    ]


def _duplicate_report(
    dataset: DatasetKind,
    records: Iterable[_PromptRecord],
    *,
    stratum: _RecipeStratum,
    max_examples: int,
) -> CrossSplitDuplicateReport:
    by_prompt: dict[str, dict[Split, list[_PromptRecord]]] = defaultdict(
        lambda: {TRAIN: [], EVAL: []}
    )
    for record in records:
        if record.split in (TRAIN, EVAL):
            by_prompt[record.prompt.strip().lower()][record.split].append(record)

    duplicate_keys = sorted(
        prompt
        for prompt, split_records in by_prompt.items()
        if split_records[TRAIN] and split_records[EVAL]
    )
    examples = [
        CrossSplitDuplicateExample(
            prompt=by_prompt[prompt][TRAIN][0].prompt,
            train_count=len(by_prompt[prompt][TRAIN]),
            eval_count=len(by_prompt[prompt][EVAL]),
            train_row_ids=[r.row_id for r in by_prompt[prompt][TRAIN]],
            eval_row_ids=[r.row_id for r in by_prompt[prompt][EVAL]],
        )
        for prompt in duplicate_keys[:max_examples]
    ]
    return CrossSplitDuplicateReport(
        dataset=dataset,
        **_stratum_values(stratum),
        duplicate_prompt_count=len(duplicate_keys),
        examples=examples,
    )


def _duplicate_reports(
    dataset: DatasetKind,
    rows: Iterable[DPOPair] | Iterable[SFTSample],
    row_id: Callable[[DPOPair], str] | Callable[[SFTSample], str],
    *,
    max_examples: int,
) -> list[CrossSplitDuplicateReport]:
    by_stratum: dict[_RecipeStratum, list[DPOPair | SFTSample]] = defaultdict(list)
    for row in rows:
        by_stratum[_row_stratum(row)].append(row)
    return [
        _duplicate_report(
            dataset,
            _prompt_records(stratum_rows, row_id),
            stratum=stratum,
            max_examples=max_examples,
        )
        for stratum, stratum_rows in sorted(by_stratum.items())
    ]


def _sft_confidence_floor_exclusion_rows(
    attributed_completions: Iterable[AttributedCompletion],
    inference_calls: Mapping[str, InferenceCall],
    policy: SFTPolicy,
    *,
    repository_context: RepositoryContext | None = None,
) -> list[ConfidenceFloorExclusionRow]:
    best_confidence_by_completion: dict[tuple[str, str], tuple[float, str]] = {}

    for attributed_completion in attributed_completions:
        if attributed_completion.abandonment is not None:
            continue
        inference_call = inference_calls.get(attributed_completion.inference_call_id)
        if inference_call is None:
            continue
        if _explicit_reject(attributed_completion):
            continue
        eligibility_source, _ = _eligibility_source(
            attributed_completion, policy, repository_context=repository_context
        )
        if eligibility_source is None:
            continue

        confidence = resolve_confidence(
            attributed_completion,
            policy.label_confidence,
            repository_context=repository_context,
        )
        if confidence is None:
            continue

        model = inference_model(inference_call)
        if model is None:
            continue
        key = (attributed_completion.inference_call_id, eligibility_source)
        current = best_confidence_by_completion.get(key)
        if current is None or confidence > current[0]:
            best_confidence_by_completion[key] = (confidence, model)

    totals: Counter[str] = Counter()
    excluded: Counter[str] = Counter()
    for (_, source), (confidence, model) in best_confidence_by_completion.items():
        totals[(source, model)] += 1
        if confidence < policy.min_confidence:
            excluded[(source, model)] += 1

    return [
        ConfidenceFloorExclusionRow(
            dataset="sft",
            recipe_id=policy.recipe_id,
            recipe_version=SFT_RECIPE_VERSION,
            eligibility_source=source,
            model=model,
            otherwise_eligible_completions=totals[(source, model)],
            excluded_by_floor=excluded[(source, model)],
            exclusion_rate=excluded[(source, model)] / totals[(source, model)],
        )
        for source, model in sorted(totals)
    ]


def _freeze(value: object):
    if isinstance(value, dict):
        return tuple((key, _freeze(value[key])) for key in sorted(value))
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _dpo_prompt_key(prompt: list[dict]) -> tuple:
    return tuple(_freeze(message) for message in prompt)


def _dpo_representative_prompt(prompt: list[dict]) -> str:
    def strings(value: object) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            return [text for key in sorted(value) for text in strings(value[key])]
        if isinstance(value, list):
            return [text for item in value for text in strings(item)]
        return []

    return "\n".join(strings(prompt))


def _dpo_buckets(rows: Iterable[DPOPair]) -> list[_DPOBucket]:
    by_key: dict[tuple[str, str, tuple[tuple[str, str], ...]], list[DPOPair]] = {}
    for row in rows:
        key = (
            row.metadata.org_id,
            row.metadata.source_model,
            _dpo_prompt_key(row.prompt),
        )
        by_key.setdefault(key, []).append(row)

    buckets: list[_DPOBucket] = []
    for (org_id, model, prompt_key), bucket_rows in sorted(by_key.items()):
        ordered_rows = sorted(
            bucket_rows,
            key=lambda row: (
                row.metadata.chosen_completion_id,
                row.metadata.rejected_completion_id,
            ),
        )
        buckets.append(
            _DPOBucket(
                org_id=org_id,
                model=model,
                prompt_key=prompt_key,
                representative_prompt=_dpo_representative_prompt(
                    ordered_rows[0].prompt
                ),
                row_ids=[_dpo_row_id(row) for row in ordered_rows],
            )
        )
    return buckets


def _near_duplicate_bucket_report(
    rows: Iterable[DPOPair],
    *,
    threshold: float,
    max_examples: int,
) -> DPONearDuplicateBucketReport:
    """Find near-duplicate DPO prompt buckets split by exact prompt equality."""
    if not 0.0 <= threshold <= 1.0:
        raise ValueError(
            "near-duplicate DPO bucket threshold must be between 0.0 and 1.0 "
            f"(got {threshold})"
        )

    rows = list(rows)
    if not rows:
        raise ValueError("DPO near-duplicate report requires one recipe stratum")
    stratum = _row_stratum(rows[0])
    if any(_row_stratum(row) != stratum for row in rows):
        raise ValueError("DPO near-duplicate rows must share one recipe stratum")
    buckets = _dpo_buckets(rows)
    tokens_by_key = {
        bucket.prompt_key: tokenize(bucket.representative_prompt) for bucket in buckets
    }
    comparable_pair_count = 0
    near_miss_pair_count = 0
    examples: list[DPONearDuplicateBucketExample] = []

    for index, bucket_a in enumerate(buckets):
        for bucket_b in buckets[index + 1 :]:
            if (bucket_a.org_id, bucket_a.model) != (bucket_b.org_id, bucket_b.model):
                continue
            comparable_pair_count += 1
            similarity = jaccard_tokens(
                tokens_by_key[bucket_a.prompt_key],
                tokens_by_key[bucket_b.prompt_key],
            )
            if similarity < threshold:
                continue
            near_miss_pair_count += 1
            if len(examples) < max_examples:
                examples.append(
                    DPONearDuplicateBucketExample(
                        org_id=bucket_a.org_id,
                        model=bucket_a.model,
                        similarity=similarity,
                        bucket_a_prompt=bucket_a.representative_prompt,
                        bucket_b_prompt=bucket_b.representative_prompt,
                        bucket_a_rows=len(bucket_a.row_ids),
                        bucket_b_rows=len(bucket_b.row_ids),
                        bucket_a_row_ids=bucket_a.row_ids,
                        bucket_b_row_ids=bucket_b.row_ids,
                    )
                )

    return DPONearDuplicateBucketReport(
        recipe_id=stratum[0],
        recipe_version=stratum[1],
        chosen_label_source=stratum[2] or "",
        rejected_label_source=stratum[3] or "",
        threshold=threshold,
        bucket_count=len(buckets),
        comparable_pair_count=comparable_pair_count,
        near_miss_pair_count=near_miss_pair_count,
        examples=examples,
    )


def _bucket_size_bin(size: int) -> BucketSizeBin:
    if size == 1:
        return "1"
    if size == 2:
        return "2"
    if 3 <= size <= 5:
        return "3-5"
    if 6 <= size <= 10:
        return "6-10"
    return "10+"


def build_dpo_bucket_sparsity(
    attributed_completions: Iterable[AttributedCompletion],
    inference_calls: Mapping[str, InferenceCall],
    policy: DPOPolicy | None = None,
    *,
    repository_context: RepositoryContext | None = None,
) -> DPOBucketSparsityReport:
    """Report DPO bucket sizes before pairing/capping.

    Candidates are the deduped, classified completions that enter DPO's
    chosen/rejected pools. That keeps this report aligned with DPO's self-pair
    guard and signal requirement while measuring the buckets before the
    cross-product pairing loop applies the per-bucket cap.
    """
    policy = policy or DPOPolicy()
    chosen_label_source = (
        "explicit_accept" if policy.recipe_id == "dpo_human" else "resolved_ci_pass"
    )
    rejected_label_source = (
        "explicit_reject" if policy.recipe_id == "dpo_human" else "resolved_ci_fail"
    )
    buckets, _, _ = _bucket_candidates(
        attributed_completions, inference_calls, Counter()
    )

    histogram: Counter[BucketSizeBin] = Counter()
    singleton_candidates = 0
    total_candidates = 0
    cap_hit_buckets = 0

    for _, bucket_attributed_completions in sorted(
        buckets.items(), key=lambda kv: kv[0]
    ):
        deduped = _dedup_by_completion(
            bucket_attributed_completions,
            policy.label_confidence,
            policy.recipe_id,
            repository_context=repository_context,
        )
        chosen_ids: list[str] = []
        rejected_ids: list[str] = []

        for inference_call_id, attributed_completion in deduped.items():
            label = _classify(
                attributed_completion,
                policy.label_confidence.ci_resolution,
                policy.recipe_id,
                repository_context=repository_context,
            )
            if label == "chosen":
                chosen_ids.append(inference_call_id)
            elif label == "rejected":
                rejected_ids.append(inference_call_id)

        bucket_size = len(chosen_ids) + len(rejected_ids)
        if bucket_size == 0:
            continue

        histogram[_bucket_size_bin(bucket_size)] += 1
        total_candidates += bucket_size
        if bucket_size == 1:
            singleton_candidates += bucket_size

        if len(chosen_ids) * len(rejected_ids) > policy.max_pairs_per_bucket:
            cap_hit_buckets += 1

    return DPOBucketSparsityReport(
        recipe_id=policy.recipe_id,
        recipe_version=DPO_RECIPE_VERSION,
        chosen_label_source=chosen_label_source,
        rejected_label_source=rejected_label_source,
        total_buckets=sum(histogram.values()),
        total_candidates=total_candidates,
        bucket_size_histogram={
            label: histogram[label] for label in DPO_BUCKET_SIZE_BINS
        },
        singleton_candidates=singleton_candidates,
        singleton_candidate_fraction=(
            singleton_candidates / total_candidates if total_candidates else 0.0
        ),
        cap_hit_buckets=cap_hit_buckets,
    )


def build_dataset_diagnostics(
    dpo_pairs: Iterable[DPOPair],
    sft_samples: Iterable[SFTSample],
    *,
    attributed_completions: Iterable[AttributedCompletion] = (),
    inference_calls: Mapping[str, InferenceCall] | None = None,
    sft_policy: SFTPolicy | None = None,
    max_duplicate_examples: int = 5,
    dpo_near_duplicate_threshold: float = DEFAULT_DPO_NEAR_DUPLICATE_THRESHOLD,
    max_near_duplicate_examples: int = 5,
    dpo_bucket_sparsity: DPOBucketSparsityReport
    | Iterable[DPOBucketSparsityReport]
    | None = None,
    abandonment: AbandonmentSummary | None = None,
    fate_result: FateResult | None = None,
    repository_context: RepositoryContext | None = None,
) -> DatasetDiagnostics:
    """Build diagnostics over already-projected DPO/SFT rows."""
    dpo_pairs = list(dpo_pairs)
    sft_samples = list(sft_samples)
    attributed_completions = list(attributed_completions)
    inference_calls = inference_calls or {}
    sft_policy = sft_policy or SFTPolicy()
    if dpo_bucket_sparsity is None:
        dpo_bucket_sparsity_rows: list[DPOBucketSparsityReport] = []
    elif isinstance(dpo_bucket_sparsity, DPOBucketSparsityReport):
        dpo_bucket_sparsity_rows = [dpo_bucket_sparsity]
    else:
        dpo_bucket_sparsity_rows = list(dpo_bucket_sparsity)

    dpo_by_stratum: dict[_RecipeStratum, list[DPOPair]] = defaultdict(list)
    for row in dpo_pairs:
        dpo_by_stratum[_row_stratum(row)].append(row)

    return DatasetDiagnostics(
        model_balance=[
            *_balance_rows("dpo", dpo_pairs),
            *_balance_rows("sft", sft_samples),
        ],
        confidence_distributions=[
            *_distribution_rows(
                "dpo",
                dpo_pairs,
                {
                    "label_confidence": "label_confidence",
                    "confidence_margin": "confidence_margin",
                },
            ),
            *_distribution_rows(
                "sft", sft_samples, {"label_confidence": "label_confidence"}
            ),
        ],
        cross_split_duplicates=[
            *_duplicate_reports(
                "dpo",
                dpo_pairs,
                _dpo_row_id,
                max_examples=max_duplicate_examples,
            ),
            *_duplicate_reports(
                "sft",
                sft_samples,
                lambda row: row.metadata.completion_id,
                max_examples=max_duplicate_examples,
            ),
        ],
        confidence_floor_exclusions=_sft_confidence_floor_exclusion_rows(
            attributed_completions,
            inference_calls,
            sft_policy,
            repository_context=repository_context,
        ),
        dpo_bucket_sparsity=dpo_bucket_sparsity_rows,
        dpo_near_duplicate_buckets=[
            _near_duplicate_bucket_report(
                stratum_rows,
                threshold=dpo_near_duplicate_threshold,
                max_examples=max_near_duplicate_examples,
            )
            for _, stratum_rows in sorted(dpo_by_stratum.items())
        ],
        abandonment=abandonment or AbandonmentSummary(),
        fate=_build_fate_diagnostic(attributed_completions, fate_result),
    )


def _build_fate_diagnostic(
    attributed_completions: Iterable[AttributedCompletion],
    fate_result: FateResult | None,
) -> FateDiagnostic:
    """Summarize only Fates joined to attached Developer decisions."""

    if fate_result is None:
        return FateDiagnostic()

    decision_keys = {
        (decision.org_id, decision.agent_harness, decision.session_id, decision.call_id)
        for item in attributed_completions
        for decision in item.decisions
        if decision.call_id is not None
    }
    explicit_accept_keys = {
        (decision.org_id, decision.agent_harness, decision.session_id, decision.call_id)
        for item in attributed_completions
        for decision in item.decisions
        if decision.call_id is not None and decision.explicit and decision.accepted
    }
    fates: Counter[str] = Counter()
    explicit_accept_fates: Counter[str] = Counter()
    fates_with_external_changes: Counter[str] = Counter()
    seen_observations: set[str] = set()
    for fate in fate_result.fates:
        key = (fate.org_id, fate.agent_harness, fate.session_id, fate.call_id)
        if key not in decision_keys or fate.observation_id in seen_observations:
            continue
        seen_observations.add(fate.observation_id)
        label = fate.fate.value
        fates[label] += 1
        if key in explicit_accept_keys:
            explicit_accept_fates[label] += 1
        if (fate.external_lines_added or 0) > 0 or (
            fate.external_lines_removed or 0
        ) > 0:
            fates_with_external_changes[label] += 1

    return FateDiagnostic(
        fates=dict(sorted(fates.items())),
        explicit_accept_fates=dict(sorted(explicit_accept_fates.items())),
        fates_with_external_changes=dict(sorted(fates_with_external_changes.items())),
        skipped=dict(sorted(fate_result.skipped.items())),
        provenance=fate_result.provenance,
    )


def generate_dataset_diagnostics(
    store: FactStore,
    mirrors: MirrorManager,
    org_id: str,
    *,
    attributed_completion_policy: AttributedCompletionPolicy | None = None,
    dpo_policy: DPOPolicy | None = None,
    sft_policy: SFTPolicy | None = None,
    max_duplicate_examples: int = 5,
    dpo_near_duplicate_threshold: float = DEFAULT_DPO_NEAR_DUPLICATE_THRESHOLD,
    max_near_duplicate_examples: int = 5,
    fate_policy: FatePolicy | None = None,
) -> DatasetDiagnostics:
    """Read facts, project DPO/SFT rows, and report dataset diagnostics."""
    with store.read_snapshot() as snapshot:
        inference_calls = snapshot.read_rollout_inference_calls(org_id)
        inference_calls_by_id = {
            inference_fact_id(call): call for call in inference_calls
        }
        assembly = assemble_attributed_completions_result(
            snapshot, mirrors, org_id, attributed_completion_policy
        )
        attributed_completions = assembly.rows
        repository_context = assembly.repository_context
        dpo_policy = dpo_policy or DPOPolicy()
        dpo_rows = project_dpo(
            attributed_completions,
            inference_calls_by_id,
            dpo_policy,
            repository_context=repository_context,
        ).rows
        sft_rows = project_sft(
            attributed_completions,
            inference_calls_by_id,
            sft_policy,
            repository_context=repository_context,
        ).rows
        quarantine_revision = snapshot.quarantine_revision(org_id)
        fate_result = derive_fate_result(
            snapshot.read_edit_observation_projections(org_id),
            four_gram_containment,
            fate_policy,
            quarantine_revision=quarantine_revision,
        )
        return build_dataset_diagnostics(
            dpo_rows,
            sft_rows,
            attributed_completions=attributed_completions,
            inference_calls=inference_calls_by_id,
            sft_policy=sft_policy,
            max_duplicate_examples=max_duplicate_examples,
            dpo_near_duplicate_threshold=dpo_near_duplicate_threshold,
            max_near_duplicate_examples=max_near_duplicate_examples,
            dpo_bucket_sparsity=build_dpo_bucket_sparsity(
                attributed_completions,
                inference_calls_by_id,
                dpo_policy,
                repository_context=repository_context,
            ),
            abandonment=build_abandonment_summary(assembly),
            fate_result=fate_result,
            repository_context=repository_context,
        )
