# Derivations playbook — `packages/derive`

Values drift, so the cited file wins. Citations take the form `path` or
`path::symbol`. `docs/explanation/attribution.md` holds the Attribution
semantics: Attribution sources, purity constraints, tie-breaks, and the
trigger. [Segment](../../CONTEXT.md#segment) holds the segment semantics.
This file carries only what those two do not. Runtime callers use PostgreSQL; multi-artifact Derivations reuse one read-only `REPEATABLE READ` snapshot and stamp its quarantine revision into `Provenance`.

## Module map

| Module | Purpose |
|---|---|
| `attribution.py` | `derive_attributions(store, mirrors, org_id, policy, *, pushes, scorer, candidates)` → per-(qualified repository, sha, file) `Attribution`; snapshot-scoped git-notes first, bounded jaccard fallback; stored-only `derive_commit_attributions` selects target owners before content |
| `repository_identity.py` | Pure immutable repository/commit keys, complete-evidence resolver, source-role validation, unambiguous operator selectors, and deterministic labels (ADR 0019) |
| `repository_context.py` | Bounded complete evidence reads inside the caller's snapshot; explicit boundary or latest captured repository Fact, with declared legacy-only supplements for direct APIs; `read_repository_witness_context` compacts commit-investigation metadata with exact source support |
| `provenance.py` | Structured `Provenance` shared by canonical derived artifacts |
| `context_retrieval.py` | Pure version-1 keyword selection from a bounded `EvidenceContextSource`; exact references, counted exclusions, and whole-part byte packing |
| `similarity.py` | `tokenize()` + `jaccard_tokens()` — the only similarity math |
| `inference_call.py` | Canonical inference-call projections, including the pure Attribution scoring-text renderer and native prompt values and typed, totally ordered prompt keys without scalar coercion |
| `scoring.py` | `Scorer` Protocol + `JaccardScorer` swap seam |
| `precision_harness.py` | Labelled-case precision/recall harness + threshold sweep/drift report; corpus-size planning helper |
| `precision_report.py` | JSONL ground-truth manifest loader + notes/jaccard precision reports |
| `diff.py` | `parse_diff_sections` → exact sections, decoded paths, hunk additions, and closed skips; `parse_unified_diff` is the compatibility additions view; code-file gate (`CODE_EXTENSIONS`/`SKIP_PATTERNS`) |
| `notes.py` | `refs/notes/sediment` reader; Pydantic wire contract; fail-soft → `None` |
| `mirror.py` | Bare-mirror manager; all git subprocess calls; SSRF confinement |
| `gc.py` | `MirrorGCPolicy`/`gc_mirrors`: mirror reclamation when no recent Push exists — a scheduled filesystem side effect, not an ADR-0001 Derivation |
| `attachment.py` | Shared joins: decisions-by-call-id (unique-or-drop), CI-by-(qualified repository, commit) |
| `ci_resolution.py` | `derive_ci_resolution_result` → attempt-aware commit verdict, reliability, exact evidence, and closed skips |
| `rollout.py` | `derive_rollout_result` → complete Session processing with optional finalized Rollout sink; `derive_rollouts` explicitly returns a list |
| `recovery.py` | Snapshot-scoped projected CI → red→green transitions + fixing diff (the one Fact-direct export shape) |
| `split.py` | Deterministic Session-grained train/eval holdout primitives |
| `survival.py` | `attach_edit_retention` fills decision `edit_retention_score`; `external_lines_after` accumulates all-or-nothing external-line tails; `derive_fate_result` maps the same scored Edit observations to diagnostic-only final Fate with closed skip accounting. `FatePolicy` version 1 uses inclusive deleted ≤0.1 and unmodified ≥0.9 thresholds. External changes aren't a human claim and Fate isn't training evidence. [Partial quarantine](../explanation/how-capture-works.md#external-edit-windows) can understate external totals without a coverage flag |
| `survival_scoring.py` | `four_gram_containment` (the scorer this module recommends for `EditObservation`'s snippet-vs-whole-file pairs) + `four_gram_survival` (symmetric, kept only as a Copilot-parity reference); consumed via `survival.py::attach_edit_retention`, never by Attribution |
| `merge_retention.py` | Identity-bearing pull-request membership outcomes plus final-head and merged-commit containment scores; diagnostic-only `MergeRetention` rows |
| `abandonment.py` | Accepted Session outcomes from captured observations; missing evidence stays unknown (ADR 0014) |
| `session_commit.py` | `bind_session_commits`: pure repository-qualified Session binding of captured Facts through explicit aware `as_of` |
| `attribution_share.py` | `derive_attribution_share` → per-qualified-repository notes-share `RepoAttributionShare`; `check_attribution_share_alerts` → decline/zero-notes-share verdicts |

`__init__.py` excludes the `notes.py` models on purpose; they are a wire contract.
## Repository evidence

`repository_identity.py` owns identity resolution before cohort selection. The key is organization/provider/host/ID; labels, clone URLs, Sessions, and SHAs never prove identity. Exact source Push references can qualify legacy observations. Other unresolved inputs remain absent and counted under the shared closed vocabulary. `read_repository_context` reads both complete populations in the caller's snapshot. Explicit `as_of` stays authoritative; implicit consumers include the actual timestamps of their consumed Facts.

Attribution retains `source_push_id`; every repository-bearing derived row carries `repository_identity`. Original observation IDs remain unchanged. Identified mirrors use the stable key. A distinct pull-request head requires its own proved identity and mirror; target objects cannot replace head evidence. Missing scoring evidence cannot erase independently proved CI membership.

## Session context retrieval

`context_retrieval.py::retrieve_context` reads no store and writes no Fact. `ContextRetrievalPolicy` version 1 reuses `similarity.tokenize`, removes the fixed query stopword set, and ranks distinct-token overlap by score, UTC observation time, Fact ID, side, and indices. Response-only repeated-content suppression preserves the first ranked exact occurrence. It doesn't change database deduplication or training evidence.
Closed skips, in precedence order: `reasoning_part`, `non_finite_number`, `no_match`, `repeated_content`, `item_limit`, `response_budget`. Every scanned part is selected or counted once. Packing keeps at most eight complete parts inside a requested 4–64 KiB response with a 2 KiB envelope reserve. Core's shared strict encoder validates the complete response; unsupported metadata or source capacity refuses the operation. No match doesn't prove an event absent.

## The knobs, and who else they re-tune

All on `attribution.py::AttributionPolicy` (implementation version 3) unless noted. Bump `policy_version` on any tuning.

| Knob | Default | Downstream consumers to check |
|---|---|---|
| `eval_fraction` (`derived_bundle.py::DerivationPolicy`, TOML `[split]`) | 0.1 | The canonical policy TOML is the only source; `RolloutPolicy` and `AttributedCompletionPolicy` default to 0.1 |
| `jaccard.min_similarity` | 0.7 | `sediment_export/sft.py::SFTPolicy.min_confidence` (0.6) is tuned against reward defaults, and similarity scores multiply jaccard-attributed Confidence. A change shifts SFT eligibility |
| `git_notes.min_similarity` | 0.3 | A best match below it abandons git-notes Attribution for that file and falls to jaccard. Raising it converts git-notes Attributed completions into discounted jaccard rows |
| `jaccard.lookback_window_minutes` | 60 | — |
| `git_notes.lookback_window_minutes` | 10080 (7 d) | — |
| `post_push_grace_period_minutes` | 10 | OTLP or gateway batches landing after the push webhook |
| `max_commits_per_push` | 20 | `mirror.py::list_push_commits` keeps the NEWEST when capped |

**Embedded policies do not share tuning.** `RecoveryPolicy`, `RolloutPolicy`,
`AttributionSharePolicy`, and `AttributedCompletionPolicy` each embed their own
`AttributionPolicy`. Thread the policy explicitly or run several behaviors.

## Attribution deltas (beyond `docs/explanation/attribution.md`)

- `similarity.py::tokenize` produces a lowercased **set** of identifier-ish
  tokens. It drops punctuation, keeps numbers, and applies no multiset
  weighting. `jaccard_tokens(set(), set())` returns `0.0`, and `<= 0.0` never
  matches.
- A commit reachable from several pushes attributes once, against the
  **earliest** push's window. The `seen` set in `attribution.py` enforces
  that, and pushes order by `(captured_at, push_id)`.
- `derive_commit_attributions` streams stored Push metadata, selects the target's earliest owner in each qualified repository, and scores only that commit. Its required captured-note map makes empty evidence authoritative. Candidate SQL admits the owner's Jaccard window plus the longer window for observed note Sessions, bounded by `as_of`. General and preloaded Derivations retain complete-source validation; see [ADR 0024](../adr/0024-targeted-commit-investigations.md).
- Only source-code files with non-blank added lines score.
  `diff.py::CODE_EXTENSIONS` is an allowlist. `diff.py::SKIP_PATTERNS` matches
  by **substring**.
- `diff.py::parse_diff_sections` owns Git C quoting, section boundaries, and hunk counts. File headers apply only outside hunks; `+++counter;` adds `++counter;`.
  Invalid sections contribute no partial additions. Closed skips are
  `unsupported_diff_section` (binary, combined, or unsupported dialect) and
  `malformed_diff_section`, counted once per section per consuming operation.
- Run scorer swaps and threshold retuning through `precision_harness.py`
  first. The `Scorer` Protocol requires a `version` string.
  `threshold_drift_report()` checks the historical threshold against the
  current F1-optimal plateau, over synthetic fixtures
  (`docs/explanation/attribution.md`). Survival scoring uses containment
  rather than a symmetric metric: the comparison is a snippet against a
  whole file.
- Corpus sizing runs through `scripts/corpus_sizing.py`, the `precision_harness.py` planning helper. It gives a pre-labelling estimate, not a reported interval.
- Ground-truth manifest scoring goes through `precision_report.py`. It matches
  exactly on `(inference_call_id, commit, file)`, and keeps the notes and jaccard
  confusion matrices separate. Negative manifest rows count: a
  threshold-passing Attribution against one is a false positive.
- Historical callers supply `note_sessions_by_commit` from bounded `SessionCommitObservation` Facts. An explicit empty mapping is authoritative: Attribution doesn't read mutable Git notes and may still use Jaccard.

## Notes wire contract (`notes.py`)

- `extra="forbid"` on the note models **is** the privacy gate. Any extra field
  fails validation, which drops the whole note and falls back to jaccard.
  Loosening it is a privacy decision, not a convenience.
- The schema holds the version gate (`v: Literal[1]`). The 64 KiB cap counts
  UTF-8 **bytes**, not `len()`.
- A note body may hold several concatenated JSON payloads, because
  `notes.rewriteMode=concatenate`. The reader loops `raw_decode`, unions the
  Sessions, and keeps the first `(tool, session_id)`. One bad payload drops
  the whole note.
- Fail-soft is absolute. A missing, oversized, malformed, or unknown-version
  note returns `None` and never raises. An absent note logs at *debug*.
- The ref name `refs/notes/sediment` is a hard constant on both the reader and
  the stamper (`scripts/sediment_attribution.py`). It is never a knob.

## Mirror rules (`mirror.py`)

- Derivations call `MirrorManager.open_repository(key)`, never `ensure()`.
  Reads return `None` when no mirror exists; only capture fetches. Stable paths
  and locks use organization/provider/host/ID under `repositories-v1`. Neither
  directory labels nor `remote.origin.url` supplies identity evidence.
- Capture qualifies the retained Push and target location against complete
  snapshot evidence before filesystem work. Known competing identities decline
  with `repository_mirror_identity_unresolved`. Git cannot detect unobserved
  remote name reuse; [ADR 0019](../adr/0019-repository-identity-and-renames.md) states that limit.
- `commit_sha` arrives from the webhook and stays **unvalidated** here.
  `--end-of-options` on every git call is the load-bearing defense against
  option injection. Only `notes.py` also regex-validates a SHA.
- `_git` decodes with `errors="replace"`, because a repo may legally hold a
  non-UTF-8 blob. It maps `TimeoutExpired` and `OSError` into `MirrorError`, so
  fail-soft handlers catch them. `is_ancestor` bypasses `_git`, because its
  answer is the exit code.
- The notes fetch refspec is a **glob**, `refs/notes/sediment*`. An exact refspec for a ref the remote lacks fails the entire fetch.
- `MirrorManager.refresh_snapshot()` holds the mirror lock through its caller's in-memory observation pass. `ensure()` releases the lock before it returns. `observation_capture()` uses a separate repository lock to serialize Push capture through persistence without holding the mirror lock during PostgreSQL writes.
- `MirrorPolicy` carries the SSRF confinement, and defaults to
  `enforce=False` for dev: `GIT_ALLOW_PROTOCOL`, no HTTP redirects, and a
  DNS-resolving internal-host check. The router injects it from API settings.
  This package never reads it.
- `read_repository_snapshot` locks qualified keys in deterministic order.
  Nested reads reuse a subset of the outer snapshot's locks; adding keys fails.
  Lifecycle and merge reports hold one snapshot across their Derivations.
  `list_mirrored_repositories` and `remove_repository` use that same namespace.
  Slug-based `open`, `rename`, `remove`, and enumeration are legacy-only wrappers;
  the forge rename route never calls them. Legacy directories aren't promoted.
- `gc_mirrors` in `gc.py` groups `store.iter_push_gc_rows` by qualified repository
  identity, preserving a label only when complete evidence supports it. It excludes
  quarantined Facts. Zero pushes there reads as `quarantine_ambiguous`, and the
  mirror is **kept**, because that read cannot tell a true orphan from a
  fully-quarantined repo.

## Shared joins (`attachment.py`)

- **Unique-or-drop.** Resolve `model_call_id` or response `tool_calls[].id` over the supplied population before requiring equal `org_id` and `session_id`.
- Closed skips: `missing_decision_call_id`, `unmatched_decision_call_id`,
  `ambiguous_decision_call_id`, `decision_org_mismatch`, and `decision_session_mismatch`.
  Count one reason per declined decision; missing/unmatched/ambiguous ID takes
  precedence over organization mismatch, then Session mismatch. Log IDs, never content.
- Rollout intentionally supplies one Session's calls. Assembly, lifecycle,
  merge-retention reports, and commit queries retain organization-wide ambiguity.
- Codex `file_path=""` subsumption keys on the full natural key *minus*
  `file_path`, and applies to `AgentHarness.CODEX` only. Keying on `call_id`
  alone would silently drop a genuine reject, which poisons a label by
  flipping it. The shared join compares event instants in UTC for subsumption and Decision ordering.
- CI outcomes index on `(qualified repository, commit_sha)`. The shared complete
  repository context separates hosts, lifetimes, and unresolved legacy evidence.
- `CIResolutionResult.outcomes_by_commit` retains original outcomes from complete consistent runs before commit selection; `conflicting_commit_keys` identifies existing run conflicts. Both reuse the shared run identity owner. Non-verdict and flake evidence remains eligible for attachment. `read_ci_outcome_projections` supports an inclusive capture boundary and SQL-enforced cap without hydrating raw payloads.

## Rollouts (`rollout.py`)

- `derive_rollout_result(..., rollout_sink=...)` emits finalized Rollouts in Session order and returns empty `rollouts` with complete counters. The sink owns retention; failures propagate. Without a sink, the legacy list contract remains. Orchestration reads one complete Session through `as_of`, hydrates its exact Decision/CI Facts, and releases its content before the next Session. Cohort selection cannot slice a Session.

- Version 4 requires typed input to replay prior input and nonempty output.
  `_canonical_history` compares roles and ordered parts without `finish_reason`;
  text and semantic `cache_control` stay intact. Object-key order is immaterial.
  Equal non-finite categories match; message regrouping opens a Segment.
- `RolloutResult.fragmented` counts retained boundaries under
  `prior_output_absent`, `input_history_changed`, and `prior_output_not_replayed`,
  in that precedence order. `skipped` remains for declined inputs. Continued
  `new_messages` keeps the suffix after prior input, including the response echo.
  A boundary Turn keeps its full input. Direct and bundle callers share
  `ROLLOUT_IMPLEMENTATION_VERSION`; bundle diagnostics copy the reasons.
- Bundle assembly supplies explicit observation Facts and `as_of` to both artifact owners before binding. Standalone Rollout derives its boundary from the latest visible observation instant in UTC; an explicitly supplied empty population remains authoritative.
- Pure `rollout.py::project_session_turns` orders calls by UTC instant and Fact ID and owns Segments, message suffixes, completion text, typed tool calls, and Session-scoped Decision attachment. Bundle v4 validation reuses it against the complete declared Session call population; finite numeric replay semantics remain unchanged.
- Identified repositories bind captured observation Facts through the complete
  context. Legacy-only calls may read notes from legacy mirrors. Jaccard runs
  lazily when a Session has no qualified notes binding.
- `attribution_note_unreadable` counts a listed note that can't be decoded
  before jaccard fallback; a missing notes ref doesn't count.
- Git notes bind one Session as `GIT_NOTES`; fallback binding is `JACCARD`.
  A Session with no attributable commit emits an unlabeled Rollout with empty
  `terminal_outcomes`; the selected export recipe decides eligibility.
- Ordering. Inference calls order by `(observed_at, inference_call_id)`. Commits order
  by `(committer_time, qualified repository, sha)`, with an unreadable time first. Outcome id
  supplies stable evidence bytes only; `CIResolution` supplies attempt order.
- `Turn.tool_calls` carries response-side `ToolCallPart` values in message order
  and is empty when capture saw none. Visible text stays on `Turn.completion`; readable reasoning remains only on the inference-call Fact.

## Recovery pairs (`recovery.py`)

- Version 5 groups by org, qualified repository, branch, provider, and resolved workflow
  definition. `CIWorkflowResolution.workflow_id` wins; an absent ID permits a
  nonempty path in a separate namespace. Display names never join definitions.
  `RECOVERY_IMPLEMENTATION_VERSION` owns the direct and CLI default.
- Recovery consumes `CIResolution`. Suspected-flake lineages count under
  `unreliable_ci_resolution`; aggregate ambiguity counts under
  `ambiguous_workflow_verdicts`. Neither can seed a pair. Non-verdicts never
  become a boundary. Consecutive clean failures collapse onto the last red.
- `merge-base --is-ancestor` gates every pair (force-push, reordered delivery,
  branch reuse).
- `max_recovery_diff_lines` (200) counts hunk-aware. Removed content behind
  `--` counts, and the `+++` and `---` headers do not. `RecoveryResult` splits
  the per-candidate counts into kept and dropped.
- The closed skip vocabulary adds the resolver's
  `conflicting_run_identity` and `ambiguous_workflow_verdicts` plus
  `unreliable_ci_resolution`, `workflow_identity_absent`, `mirror_absent`, `same_commit`,
  `ancestry_check_failed`, `not_ancestor`, `diff_unavailable`, and
  `diff_oversized`. Missing definition counts once per otherwise clean resolution;
  `mirror_absent` counts candidate transitions. `failed_inference_call_ids` and `fixed_inference_call_ids`
  enrichment is best-effort: an absent id leaves
  the list empty and never blocks the pair.

## Abandonment (`abandonment.py`)

- `AbandonmentPolicy.policy_version` is `"6"`. A matching captured `SessionCommitObservation` establishes `committed`; absence establishes `attribution_unavailable`, never abandonment. Live notes, perfect similarity, other Sessions, and mirror liveness cannot supply the missing Fact ([ADR 0014](../adr/0014-factual-outcomes-and-training-evidence.md)).
- Closed emitted skips are `no_accepted_decision`, `reached_a_commit`, and `session_commit_unobserved`. The observation gap counts accepted Sessions once. Historical status and policy fields remain parseable; the horizon cannot produce a negative outcome.
- `AbandonmentResult.outcomes` retains one status, decision counts, boundary, and Provenance per accepted Session. `sessions` stays empty without positive negative-outcome evidence. Rejected-only Sessions stay outside the population.
- Scoped callers preload decisions, pushes, Inference calls, observations, and `as_of`; supplied empty collections are authoritative. Legacy Attribution and commit-map arguments cannot establish factual identity. All-history boundaries and last-Decision extrema compare Fact instants in UTC, never wall time. Explicit capture cutoffs also compare UTC instants.
- `session_commit.py` admits `captured_at <= as_of`, sorts by qualified edge, UTC capture time, and observation ID, and retains source Facts. Callers supply one quarantine-excluding snapshot. A binding never proves individual-call or file authorship.

## Merge retention

`MergeRetentionPolicy.policy_version` is `"3"`. Row observation IDs identify the Session edge; call/file selection remains inferred.

- Membership requires the source commit in the final `base_sha..head_sha`, in a captured Pull request revision head while absent from the final base, or in exact `(qualified repository, commit_sha, pr_number)` CI evidence. Revision heads include each `head_sha`, each available `previous_head_sha`, and the merge Fact's final head. A revision head the mirror can't reach is logged and skipped as absent evidence; only a failed check against the merge Fact's own final head leaves membership unresolved. Several qualifying or unresolved pull requests prevent a row; the Derivation never guesses.
- Each candidate first requires a matching observation. The candidate denominator counts qualified files; observation gaps count distinct repository/commit/Session edges, so the units aren't additive. Joined pull requests count distinct `(qualified repository, pr_number)` pairs, including authoritative preloaded merge populations. Missing qualified edges count once under `session_commit_unobserved`; they emit no membership or retention row. Every eligible Attribution emits one ordered `MergeMembershipOutcome`: `joined`, `without_merge`, `ambiguous`, or `ancestry_unresolved`. A joined outcome keeps pull-request and merge identity even when boundary scoring skips. The legacy membership counters remain compatible views over the same pass.
- The exact attributed `FileDiff.added_lines` is measured with `four_gram_containment` against bounded, rename-aware file reads at both boundaries. A missing boundary skips the row instead of emitting half a row.
- `MergeRetentionPolicy` version 3 caps each file read at 1,000,000 bytes. Rename detection overrides ambient Git configuration with a fixed 1,000-path limit; larger candidate sets fail soft consistently. Rows and closed skips are diagnostics, not training evidence.
- Scoped callers can preload merge, pull-request revision, and CI outcome Facts.
  Observation maxima compare UTC instants. Supplied empty collections remain authoritative; omitted inputs retain the
  standalone organization-wide reads.

## Gotchas

- Never call `mirrors.ensure()` or pass `include_quarantined=True` from a Derivation. Call `open()` only.
- Never anchor a window on wall-clock now. `push.captured_at` is the anchor (`docs/explanation/attribution.md`).
- A scoped `pushes=[...]` run differs from the full run on purpose. The full-scope Derivation is the authority.

## Foundation conformance

The [consumer and relationship checks](../adr/0014-factual-outcomes-and-training-evidence.md#contract-checks) and [representation/version checks](../adr/0015-lossless-values-and-bundle-v2.md#contract-checks) link maintained owners and runnable tests. The integrated synthetic corpus preserves capture-to-training admission, source identities, and exact skipped/fragmented counts. Preserve positive admission and exact coverage units when extending a Derivation.
