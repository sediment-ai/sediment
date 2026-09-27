# Exports & statistics playbook — `packages/export`

Values drift; see `docs/agents/statistics.md` and `docs/exports/rlvr-export.md`. Operator exports borrow the process-owned PostgreSQL engine. Assembly uses one read-only `REPEATABLE READ` Fact snapshot and stamps its quarantine revision into `Provenance`.

`OperationalReportScope` defines reproducible Inference-call cohort and `as_of` evidence bounds with a default 50,000-call cap. Model and lifecycle report assembly apply the cohort before aggregation, retain related evidence only through `as_of`, reject supporting Fact populations over the fixed 50,000-row cap, look up at most 30,000 distinct Decision identifiers across visible organization history through `as_of` before attachment, and use immutable `SessionCommitObservation` bindings instead of mutable Git notes. Supplied identity evidence, including an empty collection, is authoritative; omitted evidence retains the public builder's supplied call population. Two owners prove ambiguity without reading message content. Exceeding the requested-key budget refuses the complete report; database scan work remains subject to the worker deadline. Before either scoped report reads CI or pull-request evidence, it rejects derived repository-commit and repository-PR filters over the store's 30,000-key composite-filter cap.

## Module map

| Module | Purpose |
|---|---|
| `derived_bundle.py`, `_record_storage.py`, `staged_rows.py`, `bounded_training.py` | Bundle v4, private bounded record storage, complete-group training orchestration, shared validation, deterministic publication |
| `attributed_completions.py` | Attribution and explicit-abandonment evidence → `AttributedCompletion`; Provenance + split stamped once; shared abandonment audit summary |
| `label_confidence.py` | Confidence ladder plus shared `CIResolution` helpers and exact policy-v3 comparison |
| `config.py` | `LabelConfidenceSettings` — `SEDIMENT_LABEL_CONFIDENCE_*` env → `LabelConfidencePolicy` |
| `compatibility.py`, `consumer_rlvr.py` | Versioned downstream profiles, private publication, native Rollout hydration, and explicit `ConsumerSettings` task/runtime binding |
| `trainer.py`, `schema_contracts.py`, `schema_identity.py` | Canonical inference-call parts → closed trainer messages; one canonical schema registry and stable row-schema identities |
| `dpo.py` | `project_dpo` → `DPOPair`; identical-prompt+model bucketing, versioned human/outcome recipes, per-bucket cap |
| `sft.py` | `project_sft` → `SFTSample`; owns the versioned curated/verified eligibility recipes |
| `diff_sft.py` | `project_diff_sft` → per-(inference call, commit) row with the selected SFT recipe and exact attributed-file patch sections off the mirror |
| `recovery.py` | `project_recovery` → `RecoveryRow` over `RecoverySample`; `recovery_ci` metadata and split stamped at projection time |
| `rlvr.py` | Explicit Sediment, SWE-bench, and NeMo Gym projectors plus `export_rlvr` orchestration |
| `verifier_commands.py` | Per-repo `verification_command` TOML; operator config only, never inferred |
| `environment_manifest.py`, `price_manifest.py` | Operator-supplied runtime and versioned exact model-price inputs; never inferred or stored as Facts |
| `jsonl.py` | `ExportRow`/`write_jsonl` — atomic write, no-truncate guard, split-aware naming |
| `outcome_report.py`, `operational_scope.py` | Per-model rows, explicit operational cohort bounds, Fate diagnostics, Wilson intervals, CI grain, failure buckets, Attribution share + alerts, stratification, temporal trends, Recovery yield + diff-size distribution |
| `significance.py` | Stdlib two-proportion z-test, non-inferiority, Cohen's h, MDE, Bonferroni, BH FDR, Beta-Binomial posterior, O'Brien-Fleming peeking boundary, effect-decay diagnostic, expected regret |
| `calibration.py` | Brier, ECE, AUROC, reliability buckets, and inversions stratified by recipe and evidence source |
| `interrater.py` | Stdlib Cohen's kappa + raw percent agreement over two human label columns |
| `label_confidence_inspection.py` | Stratified attributed-completion sample, consumed by `sediment report label-confidence-inspection` |
| `label_confidence_sensitivity.py` | One-`LabelConfidencePolicy`-knob-at-a-time sensitivity sweep, consumed by `sediment report label-confidence-inspection --sensitivity` |
| `dataset_diagnostics.py` | Recipe/source-stratified model balance, Confidence distributions, prompt and bucket checks, SFT Confidence floor exclusions, abandonment coverage, and diagnostic-only Fate metadata |
| `decision_latency.py` | Quantile-buckets unique decisions by `decision.captured_at - inference_call.observed_at`; accept-rate and Confidence deltas per bucket. Retention observations (`observation_delay_ms` set) are excluded — counted under `observation_delay` in the closed six-reason skip vocabulary |
| `merge_retention_report.py` | Merge-retention coverage, score distributions, fixed sensitivity thresholds, explicit-accept slices, Provenance, and canonical row adaptation for JSONL output |

## Attributed completions (`attributed_completions.py`)

- Every Attributed completion carries one complete evidence variant: Attribution,
  or supplied legacy abandonment with a uniquely joined explicit accept and no repository, commit, file, score, or Attribution source.
- An `AttributedCompletion` carries no model, prompt, completion text, or diff text. Every projection takes an `inference_call_id` to `InferenceCall` mapping alongside it.
- The shared evidence ordering key keeps Attribution rows on `(qualified repository, commit_sha, file_path, inference_call_id)`; abandonment uses `(session_id, inference_call_id)`. Assembly preserves the shared join’s Decision order by `(occurred_at in UTC, decision_id)`; outcomes sort by `outcome_id`.
- Assembly reuses one Fact snapshot, joins projections without `raw`, and hydrates selected Facts by ID. Scoped application services can supply authoritative Attribution, completion, decision, Edit observation, CI, and abandonment populations without fallback reads. It fills missing `edit_retention_score` via
  `attach_edit_retention` and `four_gram_containment`. Captured rates stay unchanged. `AttributedCompletionPolicy.policy_version` is `"5"`; bundle defaults consume the same owner.
- `assemble_attributed_completions_result` returns rows, `AbandonmentResult`, and skips. Provenance includes the full resolved `policy_digest` when available.
- Assembly uses complete CI run qualification before selected commit attachment. Scoped preloads pass `ci_population` from the same snapshot; an explicit empty population is authoritative. Selected CI copies must agree with their declared sources. Qualified commit selection retains every consistent attempt, including non-verdicts and flakes.
- Assembly selects its default observation boundary by UTC instant. Structured assembly counts each decision-call attachment loss separately under the [shared join contract](derivations.md#shared-joins-attachmentpy). A mismatched accept grants no Confidence or recipe eligibility.

## Canonical bundle (`derived_bundle.py`)

- [ADR 0019](../adr/0019-repository-identity-and-renames.md) extends the bundle to six JSONL files plus its manifest. Complete call identity, repository source-role, and rename populations remain independent of cohort selection through `as_of`. Each has a 50,000-row cap. [Derivation operations](../operate/run-derivations.md#diagnose-failures) states the failure and operator response; narrowing the cohort can't bypass it. ADR 0015's lossless `record_json` encoding remains unchanged. The shared validator returns the verified repository context for all projectors; source Push, CI, and observation proofs survive serialization. Versions 1–3 require recomputation.
- [The existing assembly retention projection](../adr/0015-lossless-values-and-bundle-v2.md#bundle-version-2) permits only captured Rollout score null → one consistent Attributed completion score; every other captured field agrees.
- Public `validate_derived_bundle` runs before write, after read and before bundle-based training, including in-memory exports. It checks exact identity projections, batched Decision attachment at organization/Session scope, complete Session Turn reconstruction, existing Fact relationships and Provenance. Before Session hydration, it enforces the shared 256 MiB input/output content limit with `BundleCapacityError`; raw audit bytes do not count. Contradictions raise `BundleValidationError`; no Facts are repaired. External completeness remains a producer claim.
- Low-level projectors require trusted canonical artifacts from assembly or a validated bundle. `export_rlvr_from_bundle` validates in-memory bundles before its trusted Rollout projector. CLI DPO/SFT/diff-SFT use `project_training_bundle` after validation. It retains one complete call, cross-Session DPO prompt/model group, or qualified diff-SFT commit at a time. Private staged rows preserve projector order and diagnostic multiplicity; group capacity failure aborts publication.
- The builder determines `as_of` before selecting artifacts and copies `fragmented` from Rollout. Manifest versions consume owner defaults. [ADR 0020](../adr/0020-bounded-derivation-execution.md) permits private file-backed payload sequences: `build_derived_bundle_context` stages selected artifacts and incrementally reads referenced Facts within one snapshot, validates, then releases database/mirror locks before yielding. `open_derived_bundle` validates its owned snapshot before yielding. Their contexts own payload lifetimes; `build_derived_bundle` and `read_derived_bundle` explicitly materialize at most 64 MiB of encoded payload by default. `BundleLimits` defaults to 512 MiB per encoded record and 8 GiB per staging store; overlapping stores have separate budgets. Staged publication never replaces a destination.

## The Confidence ladder (`label_confidence.py`)

Apply precedence first, then multiplication. Never average.

1. An abandonment row takes the existing explicit-reject floor (default 0.0)
   before its attached explicit accept can win.
2. An explicit accept gives 1.0, an explicit reject gives 0.0. **Reject wins**
   when both exist.
3. An implicit accept gives baseline (0.6) × 1.1. An implicit reject gives
   0.6 × 0.9.
4. No decision gives the bare baseline, 0.6.
5. Resolved CI multiplies onto the decision factor: pass ×1.1, fail ×0.7. Non-verdicts never classify; only `run_attempt` orders attempts.
6. CI reliability multiplies independently: suspected flakes default to 0.0,
   clean verdicts to 1.0, and agreeing workflows take `min()`.
7. A jaccard Attribution multiplies by `similarity_score`. **Git-notes Attributions are never
   discounted.** The product caps to [0, 1].

- `resolve_confidence` returns `None` when neither decision nor CI exists; never filter on a sentinel float.
- **Codex is accept-only.** An all-Codex implicit accept gets a neutral ×1.0,
  which leaves the bare baseline. The check runs *before* survival interpolation.
- **Edit survival** interpolates between the reject and accept multipliers.
  The rate comes from Copilot's vendor grade or from the assembly fill.
  Several rates take `min()`. It never re-routes into the reject branch.
- `ConfidenceBreakdown` records four uncapped factors so multiply-then-cap reproduces `final`.
- `LabelConfidencePolicy.policy_version` (`"4"`) is **not yet threaded into
  Provenance**. See [Provenance](../../CONTEXT.md#provenance); don't fix the
  known gap silently.
- Env prefix hazard. `LabelConfidenceSettings` owns
  `SEDIMENT_LABEL_CONFIDENCE_*`; `VerifierCommandsSettings` owns
  `SEDIMENT_VERIFIER_COMMANDS_FILE`. Both set `extra="ignore"`.

`trainer.py::validate_training_representation` checks every emitted row, including nested tool arguments, keys, and metadata, before JSON serialization. All projector vocabularies compose `non_finite_number` and `unrepresentable_unicode`; each declined pair or Segment row counts once. Omitted values don't affect eligibility; diff-SFT maps only the source prompt. Facts and bundles retain ADR 0015's wider domain.

## DPO (`dpo.py`)

- `dpo_human` version 2 is the default. It requires a human-explicit accept and
  reject without claiming that the developer directly compared the members.
- Explicit `dpo_outcome` version 2 pairs only clean resolved CI passes and failures. It
  excludes ambiguity, non-verdicts, and flakes. A pair never mixes sources.
- Bucketing uses raw structural equality of the inference call's native input
  messages, including readable reasoning. It compares ordered roles and typed
  parts. `inference_prompt_key` preserves typed scalars and equal non-finite
  categories with total ordering across heterogeneous arguments. It doesn't use
  Rollout's `_canonical_history` or strip `cache_control`. ADR 0004 prefers fewer, cleaner pairs.
- Per-bucket dedup ranks one row per `inference_call_id` by selected-recipe eligibility, Confidence, then `evidence_sort_key`.
- `max_pairs_per_bucket` is 3 evaluated pairs in sorted call-ID order. Declines consume slots without backfill. `dpo_bucket_capped` retains possible pairs, cap, and actual emission; `bucket_capped` counts buckets.
- After complete-row representation validation, strict JSON equality of full mapped responses declines once under `identical_responses`. Object key order is irrelevant; values and scalar types remain significant. Representation reasons win. Pair and bucket counts have different units.
- `metadata.label_confidence` is `min(chosen, rejected)`.
  `metadata.confidence_margin` is `chosen − rejected` and can be negative.
  Trainer messages stay outside `metadata`; metadata records recipe, integer
  version, both label sources, Confidence, and CI reliability separately.
- Closed DPO skips add shared resolver/mapping reasons plus
  `inference_call_not_found`, `promptless`, `model_absent`, `no_label_source`,
  `unreliable_ci_resolution`, `bucket_capped`, and `identical_responses`.

## SFT and diff-SFT (`sft.py`, `diff_sft.py`)

- Default `sft_curated` version 1 requires a human-explicit accept or retention
  at least 0.8; CI pass changes confidence/reliability, not eligibility.
- Explicit `sft_verified` requires a clean resolved CI pass. Reject,
  abandonment, or any workflow failure vetoes both recipes; ambiguity and flakes cannot
  admit a row. Skips use `SFT_SKIP_REASONS`.
- SFT and diff-SFT count and skip abandonment under `abandoned` before every
  positive gate. A zero Confidence floor cannot admit a negative.
- `min_confidence` (0.6) remains tuned against the Confidence ladder defaults.
  It gates recipe-eligible members but never creates eligibility. Check both
  policies together when defaults change.
- SFT keeps one sample per Inference call: recipe eligibility, then Confidence, then the repository-qualified `_evidence_sort_key`. Identical extras count under `duplicate_completion`. Conflicting payloads at one evidence identity decline the entire Inference call once under `conflicting_evidence`, before eligibility or selection.
- `trainer.py` maps text to `content`, readable reasoning to assistant
  `thinking`, tool calls to structured `tool_calls`, and user/tool-role results
  to `tool` messages. Strings remain verbatim; JSON values use strict JSON text. It preserves OpenAI Responses `developer` prompt roles. It never calls `render_scoring_text` or places reasoning in visible `content`.
- Diff-SFT requires every `(inference_call_id, commit_sha)` member to satisfy
  the selected recipe with one source; Confidence is `min()`. It concatenates
  exact patch sections from `sediment_derive.diff` in diff order. Missing sections, empty patches, and split mismatches skip.
  Shared parser losses count once per commit section under `unsupported_diff_section` or `malformed_diff_section`, separately from skipped groups. ADR 0004 allows reading mirror diffs.

## Recovery projection (`recovery.py`)

- Every row names `recovery_ci` version 1. Shared resolution excludes ambiguous,
  non-verdict, and flaky lineages before projection.
- Projection emits each representable derived pair; `inference_call_not_found` counts unresolved IDs and `attribution_evidence_absent` counts missing source records per side and call. Eval wins across resolvable Sessions on both sides; no
  Sessions means train. `RecoverySample` never persists the split.

## RLVR targets (shapes and caveats: `docs/exports/rlvr-export.md`)

- `export_rlvr` requires one of `sediment`, `swe-bench`, or `nemo-gym`. The CLI
  wraps the same target-explicit orchestration and has no default.
- `project_sediment_tasks` accepts recorded passes and failures.
  `project_swe_bench_tasks` accepts passes only and counts a recorded failure
  as `failed_reference_patch`, distinct from `missing_verifier_evidence`.
- Task prompts retain canonical `TextPart.content` verbatim, including JSON-looking prose. Explicit non-text parts retain the visible omission marker.
- Both task projectors share mirror resolution. The verifier-result repository
  selects the mirror. Session commits filter to that mirror. The patch ends at
  the exact verified commit. An ancestry check protects `reference_patch`
  because trajectory order follows committer time.
- Every target selects the final semantic attempt of the last attributed commit
  with a resolved CI verdict. A later non-verdict commit cannot hide an earlier Reward. Resolver skips, reliability, suspected-flake state, exact verifier
  results, source outcome ids, and resolution provenance remain separate.
- `project_sediment_rollouts` retains turns, exact results, every resolution,
  and the selected resolution with reliability. `project_nemo_gym_rollouts` maps a resolved pass to `1.0`, a resolved failure to `0.0`, and omits unknown Reward. Reliability never changes that numeric value.
- Every target row names `rlvr_ci`; directional rows name a closed resolved-CI reward source, and non-verdict rows omit `reward_source`.
- `VerifierCommandsSettings` loads operator configuration separately from
  recorded verifier results. The clean-slate contract has no legacy setting,
  TOML spelling, Python name, or module alias.
- RLVR orchestration stages one complete Rollout at a time through unchanged projectors. It prepares every selected target file and manifest before publication under one target claim. Staged rows and prepared output share the stage byte quota; replacements remain atomic per file. Only the Sediment target writes the experimental manifest.
  `runtime_reference` requires unanimity across every task. `frameworks.*` entries come only from operator settings and remain absent when unknown.
- DPO recipe v2 uses pair/metadata schemas v4. Profiles `hf-trl-dpo-v2` and `fireworks-dpo-v2` retain mapping and dependency pins; retired v1 names and historical recipe-v1/schema-v3 rows require re-export. See [DPO migration](../exports/dpo.md#migrate-retained-dpo-exports).
- `compatibility.py::PROFILES` owns exact consumer identities, versions, and support claims; `scripts/gen_compatibility_docs.py` generates the reference. Optional parser dependencies stay outside the base runtime.
- `consumer_rlvr.py::ConsumerSettings` is a closed version-1 JSON settings loader, not a policy or Fact. NeMo response parameters and SWE task/runtime fields require an operator source; the evidence sidecar records its digest. No environment variables configure this contract.
- `NEMO_PROFILE_SKIP_REASONS` composes `TRAINER_SKIP_REASONS` with `missing_reward`, `inference_call_absent`, `inference_call_identity_mismatch`, `model_absent`, and `model_conflict`. NeMo skips and logs ineligible rows; malformed settings, unsupported contracts, or broken continuity fail before writes.
- Consumer profiles use `BundleLimits.max_materialized_bytes` (64 MiB) for source preflight, projected rows, adapted data/evidence, loader inputs, settings, and prepared output. NeMo and SWE-bench projectors support a staging row sink; errors propagate before publication. File-backed sources refuse excess before hydration. See [Check consumer capacity](../exports/consumer-compatibility.md#check-consumer-capacity).
- Profiles preserve labels and full native outputs. Exact prompt overlap across split partitions fails; semantic task overlap remains unassessed. Private directory publication keeps data, source evidence, and diagnostics together. See [Export for a consumer](../exports/consumer-compatibility.md).

## JSONL destination (`jsonl.py`)

`docs/exports/rlvr-export.md` documents splitting and the no-truncate guard. The writer uses `mkstemp`, `fsync`, and `os.replace`; projectors own ordering. It escapes strings, rejects non-finite numbers with `allow_nan=False`, and prepares all nonempty split partitions before replacement. Optional `max_bytes` bounds encoded output across all partitions. Serialization failure preserves prior files; replacements remain atomic per file. Failure between replacements can expose complete train/eval files from different generations. Replaying the same ordered rows restores exact output bytes; an empty partition still preserves its prior file. Process interruption checks make no power-loss durability claim. Reports can retain escaped surrogates without claiming trainer eligibility.

## Statistics deltas (semantics: `docs/agents/statistics.md`)

- **Dedup by real event.** CI trials dedup by `(qualified repository, commit_sha)` after CI
  resolution. Resolved failing workflows supply failure buckets. Accepts and
  rejects dedup by `decision_id`. Counting per Attributed completion is the
  pseudo-replication bug, so `CIGrain.ATTRIBUTED_COMPLETION` exists for
  comparison only, never for inference.
- Zero-denominator convention. Every rate reads 0.0, never NaN. The Wilson
  zero-trial sentinel is `(0.0, 0.0)`
  (`outcome_report.py::wilson_score_interval`).
- `significance.py`. `insufficient_data=True`, which fires when either n is 0,
  makes every numeric field a 0.0 placeholder. A degenerate pooled SE of 0
  yields `z=0.0, p=1.0` explicitly. The small-n flag never switches the test.
  `bootstrap_two_proportion_interval_diagnostic` is a fixed-seed diagnostic, never
  a persisted Derivation. The peeking correction is diagnostic only:
  `information_fraction` populates `sequential_boundary` beside the naive
  p-value, and never administers a trial.
- `compare_all_models` runs `compare_models` per unordered pair, then
  Benjamini-Hochberg over the computable p-values. Insufficient-data metrics
  stay visible but never enter BH as fake `p=0.0`.
- `effect_decay_check` displays nested, overlapping prefixes, for display
  only. `decay_detected` instead runs `mann_kendall_trend_test` over Cohen's h
  on disjoint blocks. A threshold or trend test over the nested series
  false-positives on flat real effects.
- `expected_regret` uses the closed-form normal positive-part expectation over
  `p_alt - p_best`; its SE uses the plus-four smoothed rate so a boundary p in
  {0,1} doesn't force SE (and regret) to 0.0.
- Outcome-report policy version `"4"` gates CI populations in direct builders, strata, funnels, trends, and comparisons on matching captured observations. Missing edges count once under `session_commit_unobserved`; direct decisions remain available. The funnel describes this observed CI population, not total version-1 training yield. Precomputed aggregate rows cannot bypass this boundary. Outcome-report rows carry structured Provenance and a separate `grain` field.
  `since_days` windows inference calls and Attributed completions
  **together** on the inference call's `observed_at` using UTC instant comparisons — deliberately not the store's
  `since=` filter (pre-filtering desyncs the model lookup). Effect-decay diagnostics reuse this population, including the inclusive `as_of` upper bound.
- The top-level `attribution_share` and `attribution_alerts` use the model rows' `since_days` boundary.
  Attribution rate, signal-funnel Attribution, and mean similarity use Attribution-evidence rows only.
  The top-level `AbandonmentSummary` reports abandoned, grade-eligible, and implicit-only Sessions; emitted negative
  completions; unjoined explicit accepts; Derivation skips; and abandonment
  Provenance without guessing a model for Session-only evidence.
- `ci_pass_rate` stratification includes raw and empirical-Bayes shrunk per-repo
  rates (`outcome_report.py::shrink_stratified_rates`); reversal detection uses
  the raw rates. `attribution_rate` stratification is hard-coded `NOT_CHECKABLE`.
- Explicit scopes keep the same cohort in model rows, funnels, strata, and trends; `as_of` only qualifies evidence. Model and funnel `since_days` retain the rounded cohort duration. Temporal trends (`outcome_report.py::build_temporal_trend_report`): seven-day
  captured-at buckets anchored to explicit `cohort_start` when supplied, empty-denominator buckets omitted, Mann-Kendall
  (`outcome_report.py::mann_kendall_trend_test`) over ordered rates. Fewer than
  three non-empty windows is `INSUFFICIENT_DATA`, not an exception.

## Diagnostics (`label_confidence_inspection.py`, `label_confidence_sensitivity.py`, `calibration.py`, `interrater.py`, `dataset_diagnostics.py`)

- Dataset, Confidence, sensitivity, and decision-latency generators reuse assembly's returned `repository_context` inside one snapshot. Preloaded builders accept that same context. Its boundary includes later Developer decisions and Edit observations; recipe rules, historical policy-v3 comparisons, and skip units remain unchanged.
- `InspectionRow.human_judgment` is **always `None`** — the tool never invents a
  judgment. Sampling is deterministic round-robin over `(decision_branch, ci_bucket, attribution_source)` cells — no
  RNG.
- Label-confidence inspection records the selected SFT recipe and eligibility
  source. It leaves ineligible rows visible under an `ineligible` stratum and
  leaves unavailable Attribution fields absent. `--n` applies to sample mode;
  knob, values, and confidence flags apply to sensitivity; `--latency-buckets`
  selects decision latency. The CLI rejects cross-mode flags.
- `label_confidence_sensitivity.py` reports per-attributed-completion SFT eligibility under
  the selected `sft.py` recipe plus `min_confidence`; it intentionally skips
  completion lookup and one-sample-per-completion dedup. Each sweep row changes
  one `LabelConfidencePolicy` knob, including nested CI reliability factors,
  and compares against the unswept base and exact policy version 3.
  Abandonment responds to the explicit-reject Confidence knob but never enters
  the SFT-eligible population, including at a zero floor. Every statistic stays
  inside one recipe and eligibility-source stratum.
- Calibration compares versions 4 and 3 against the same judgments within one
  recipe/source stratum. It refuses records without recipe and closed source
  metadata and never pools heterogeneous evidence.
  **Calibration and inter-rater metrics have no in-repo label producer**;
  `actual_outcome` ships null and `interrater.py` consumes two filled label lists.
  Don't fabricate either input.
- `ci_bucket` ∈ {`ci_pass`, `ci_fail`, `ci_absent`} — absent covers no outcomes
  and non-verdict-only outcomes.
- Cross-split duplicate detection normalizes prompts as `strip().lower()` over a
  sort-keyed JSON rendering; DPO distributions report `label_confidence` + `confidence_margin`,
  SFT reports `label_confidence`; every row is grouped by `recipe_id`,
  `recipe_version`, and its label or eligibility source before the `overall`
  pseudo-model and per-model rows.
- DPO near-duplicate detection compares distinct prompt buckets within `(org_id,
  model)` by symmetric token-set Jaccard over representative prompt text;
  threshold 0.8; exhaustive over distinct buckets (O(n²)), only examples capped.
- SFT Confidence floor exclusion rates count inference calls: denominator = calls
  with a pre-floor-eligible SFT Attributed completion; numerator = those below
  `SFTPolicy.min_confidence`.
- Dataset diagnostics carries `AbandonmentSummary`. DPO sparsity and projection share `_bucket_candidates`: structural failures exclude members; representation failures remain until pair selection. Each Evidence recipe classifies its own candidate labels; abandonment alone supplies no DPO label.
- `build_model_report` and `build_model_report_result` accept an authoritative `decisions` population for direct counts and Fate keys, independent of Attribution artifacts. The shared attachment checks uniqueness over all supplied calls before report cohort filtering, then organization and Session agreement; its closed diagnostics remain logged. Calls retain tool-call IDs; all-history generators reuse output/identity projections without input/raw content. Omitted `decisions` retains the supplied artifacts' assembled decision population. Store callers bound decisions and Edit observations by `as_of` and quarantine; callers supplying a `FateResult` must bound its source observations themselves.
- Reports deduplicate decisions by `decision_id` and Fates by `observation_id`, split human-explicit accepts and external changes, and expose global Fate skips and Provenance. Fate isn't training evidence. The signal funnel remains an Attribution-conditioned training diagnostic.
- Repository strata use organization and qualified identity. Model `shrunk_rates` is an ordered list of `RepositoryShrunkRates`, not a name-keyed map. `repository_skipped` separates evaluated source populations; `ci_skipped` separates source outcomes, runs, and commits. Never add those units. Model panels and effect-decay reuse the same complete CI qualification; lifecycle and merge reports preserve original observation IDs and independent denominators.
- The merge-retention report keeps final-head and merged-commit distributions separate. Thresholds 0.8, 0.9, and 1.0 are sensitivity points, not filters. Decision attachment ambiguity is counted separately from Attribution and scoring skips. `generate_merge_retention_report_result` returns the aggregate and canonical rows from one snapshot; `--rows-out` uses the no-truncate `write_jsonl` path with splitting disabled. `LifecycleReportPolicy` version 3 preserves decision scope-mismatch counts in progression skips and missing coverage. It applies the observation boundary to scoped and all-history reads. `accepted_work_lifecycle.py` composes Attribution, abandonment, final Fate, continuous integration resolution, and merge retention once over one Fact and mirror snapshot. Its four panels retain separate grains and denominators. Never sum rework components into a score. Merge durability remains `partial_pull_request_history` because captured pull-request revision history can be incomplete. The artifact serializes aggregate measurements and Session identifiers, never captured content.
- [Consumer and relationship checks](../adr/0014-factual-outcomes-and-training-evidence.md#contract-checks) cover positive capture-to-training output and factual populations. [Representation/version checks](../adr/0015-lossless-values-and-bundle-v2.md#contract-checks) cover owner propagation, counted exclusions, and the narrow cross-artifact retention view.
