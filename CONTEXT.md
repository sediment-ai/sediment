# Sediment — Domain Context

The ubiquitous language for this project. Use these terms exactly in code,
issues, tests, and docs. Avoid the listed synonyms **as names for these
entities** so the vocabulary stays sharp — the words remain fine in their
own senses (a wire trace, a numeric reward, an `enterprise-tier` label), and
code identifiers keep their wire/source names. `_Avoid:_` governs prose.

## Glossary

### Fact
Something that happened, captured as an immutable row: an inference call, a
Developer decision, a push, a CI outcome. Facts are appended, never mutated,
and are the only persisted domain state. The [ADR 0017 transport buffer](docs/adr/0017-sender-transport-replay.md) is an opt-in delivery mechanism that Derivations never read. _Avoid:_ "event" for stored rows (events
arrive, and Facts are stored).

### Session (`sessions` table)
The aggregate root for developer-side Facts: one developer workflow Session,
owning its inference calls, decisions, Edit observations, rejected edits, and
Retry linkages via `session_id`. Upserted at
the storage seam with order-independent metadata. `first_observed_at` and
`last_observed_at` bound Fact observation time. An unknown `user_id` stays null.
A later known identity fills null. Conflicting known identities clear `user_id`
and set the sticky `user_id_conflict` marker. A Session has no scalar producer
because one Session can contain Facts from several producers.
_Avoid:_ treating `session_id` as an opaque string to thread through — it is
an entity.

### Inference call (`InferenceCall`)
One model call, captured from a gateway or agent trace. The canonical Fact stores
ordered `input_messages` and `output_messages`; each message keeps text, tool
calls, tool-call responses, and readable reasoning as distinct typed parts. It
identifies the capture gateway with `gateway_provider` and the model service with nullable
`model_provider`. Unknown model, user, usage, provider, and duration values
remain null. The provider payload stays on `raw` after Basic redaction.
_Avoid:_ "completion" for the fact, "log", "trace", "request",
"gateway event".

### Basic redaction
The fixed open-core capture guard that replaces high-confidence API keys and
bearer credentials with `[REDACTED_CREDENTIAL]` at the storage seam. It covers
every content field and `raw` payload before PostgreSQL writes the Fact, counts
replacements under the closed `RedactionReason` vocabulary, and never changes
identity or dedup fields. It has no operator configuration, per-org patterns,
or enforcement audit. Existing Facts
stay immutable; quarantine is the response to a credential captured before the
guard existed. _Avoid:_ "secret scanner" — the fixed patterns don't claim
comprehensive secret detection.

### Model call id (`model_call_id`)
The model service's per-call identifier on an `InferenceCall`. It is the
dedup key when present; keyless Facts insert freely. It is a different
namespace from every tool-call id. A decision attachment can resolve either
namespace explicitly.
_Avoid:_ `call_id` for this field, because that name also appears on developer
decisions.

### Decision call id (`DeveloperDecision.call_id`)
The identifier the coding agent records for the model call or tool call that
produced a decision. Attachment resolves it against `model_call_id` and the
ids on response-side tool-call parts using unique-or-drop semantics. The selected
Inference call and Developer decision must share organization and Session. Source
wire names include OTLP `tool_use_id`, Codex `call_id`, and Copilot
`request_id`.
_Avoid:_ assuming this field occupies only the model-call namespace.

### Tool-call part (`ToolCallPart`)
One tool call the model made in an inference call's response. The Fact uses
the OpenTelemetry-aligned `{type="tool_call", id, name, arguments}` part and
keeps it in message order. `id` is the agent-visible tool-use id (`toolu_…`)
that Developer decisions and Edit observations carry as their `call_id`.
Malformed wire arguments become `{}` while the original survives on `raw`.
_Avoid:_ "function call" (the OpenAI wire name for one source shape, not the
normalized part), `input` on the canonical contract.

### Reasoning part (`ReasoningPart`)
Readable model reasoning in an inference call's ordered message parts. The
canonical shape is OpenTelemetry-aligned `{type="reasoning", content}`.
Provider signatures, encrypted reasoning, and redacted thinking remain only in
Basic-redacted `InferenceCall.raw`. Attribution scoring excludes reasoning,
while prompt identity and Rollout prefix comparison retain input reasoning.
_Avoid:_ provider wire names such as "thinking block" for the canonical part,
and treating opaque continuation state as readable content.

### Translator
The thin, per-source adapter in `packages/capture` that maps one gateway or
agent wire format (LiteLLM, OTel, a forge webhook) onto normalized Facts at
the capture boundary — never a Derivation, never a decision about
Attribution or grading.
`packages/capture/sediment_capture/otlp.py`'s module docstring is the
normative translator contract. `docs/agents/capture-translators.md` covers
per-source mechanics. _Avoid:_ "parser" (a translator normalizes onto the
Fact shape, not just parses the wire payload).

### Keyed / keyless Fact
Whether a Fact carries a dedup key its `UNIQUE` index uses. Keyless Facts
insert freely — a supported state, not an error. Decisions split three ways:
only claude-code decisions with a `call_id` use the keyed index. Every other
decision dedups under the natural key (its `call_id`, when present, folds in
via `COALESCE`). The keyed-index opt-in decision for a new agent harness lives in
`docs/agents/capture-translators.md`.

Native Cursor implicit `Write` receipts also carry a deterministic Fact id.
The PostgreSQL primary key preserves the first receipt. The natural index and
historical random-ID Facts remain unchanged; this doesn't repair prior duplicates.

### Agent harness (`AgentHarness`, `agent_harness`)
The coding agent harness that produced a Developer decision, Edit observation,
rejected edit, or Retry linkage. The closed values are `claude-code`, `copilot`,
`codex`, `cursor`, and `pi`. The harness stays on each immutable Fact because a
Session can contain Facts from several harnesses. _Avoid:_ bare `source` for
this provenance.

### Interaction mode (`InteractionMode`, `interaction_mode`)
How the developer interacted with the proposed edit. The closed values are
`agent` and `inline`. A coding-agent interface, including Claude Code, uses
`agent`; the harness identity belongs in `agent_harness`.
_Avoid:_ `surface`, harness names as interaction modes.

### Developer decision (`DeveloperDecision`)
A developer's accept/reject of an AI-generated change — **explicit** (a real
human gesture) or **implicit** (auto-applied or retention-inferred), recorded on the
`explicit` flag. _Avoid:_ "feedback", "signal" (retired
"developer signal").

### Edit observation (`EditObservation`)
The `applied_text` and `observed_file_text` pair for one applied AI edit — the
replacement that the tool applied next to the file's content at Session end — captured by the
client-side transcript extractor and joined to its Developer decision by
`call_id` (ADR 0007). The pair is the Fact, and the edit retention score is a
Derivation over it (scorer seam in `sediment_derive.survival`, metric
`four_gram_containment` — containment-shaped by contract: for Edit
calls the pair is the `new_string` snippet vs the whole Session-end file,
so symmetric distance metrics are invalid). _Avoid:_ "edit outcome", "survival
fact". The score is derived unless Copilot supplies its vendor grade on the
decision.

### External line counts (`external_lines_added` / `external_lines_removed`)
The two counts on an `EditObservation` recording lines changed by something
other than the agent's edit tools, between that edit and the next
observation of the file. They disambiguate a low edit retention score:
the agent revising itself and an outside correction produce the same
score and are opposite training labels. **External, never "human"** — a
formatter, a linter with autofix, a file watcher, a `git checkout` during a
Session, and the agent's own shell all land in the same count, so
naming them human would freeze a judgment into a Fact. Grading them is a
policy question for `sediment_derive.survival::external_lines_after`.
`None` means no window covered the call, which is a different claim from
`0`; the wire, the columns, and the Derivation all keep them apart.
_Avoid:_ "human edits" and "manual corrections". Never read absent as zero.

### Rejected edit (`RejectedEdit`)
The text a developer refused, for one rejected AI edit — the denied
`new_string`/`Write` content as the model wrote it, joined to its
rejecting `DeveloperDecision` by `call_id` (ADR 0007). It exists
because a reject's `DeveloperDecision` carries `file_path=""` and no
content, so for a non-gateway harness the refused proposal survives
nowhere else. Refusals only: a call the *tool* failed on is never
captured, because nobody judged that text. There is deliberately no
`observed_file_text` — a refused edit never reached the file. It is capture only today,
and feeds no projection (`project_dpo` buckets on (org, model, prompt), none
of which a `RejectedEdit` carries).
_Avoid:_ "failed edit", because a tool failure is not a refusal. Do not
assume it is already a DPO rejected side.

### Retry linkage (`RetryLinkage`)
The immutable relationship between one human-explicit rejected Edit or Write
tool call and a later accepted retry in the same agent harness, Session, and
file. A developer text entry must occur between the refusal and the accepted
retry. Tool failures, implicit refusals, agent-only rewrites, pure regenerate
sequences, and cross-file or cross-Session attempts produce no Fact. The Fact
stores only identifiers and event metadata. It stores no raw transcript,
developer correction text, or duplicate attempt text. A Retry linkage records
what happened; it doesn't make either attempt eligible for a training
objective. _Avoid:_ "retry pair" when referring to the fact, and any claim
that the relationship is a training label.

### Edit retention score (`edit_retention_score`)
The per-decision `[0,1]` score of how much of an applied AI edit's text
remains in a later file observation, carried on
`DeveloperDecision.edit_retention_score` with its `observation_delay_ms`.
Populated by Copilot from its vendor `survival_rate_four_gram` field and filled
for Claude Code at derive time from edit-observation pairs via
`four_gram_containment` — containment rather than a
symmetric metric, because the comparison is a snippet against a whole
file.
_Avoid:_ `survival_rate` or `survival_window_ms` as normalized fact fields;
those names describe vendor wire data, not the canonical value.

### Fate (`Fate`, `EditFate`)
The diagnostic-only categorical Derivation over one Edit observation's final
retention score: `deleted`, `partially_modified`, or `unmodified`. `FatePolicy`
version 1 maps scores at or below 0.1 to `deleted`, scores at or above 0.9 to
`unmodified`, and scores between them to `partially_modified`. A Fate carries
the accumulated external line counts when every tail window has coverage.
Outcome reports and dataset diagnostics summarize Fate, but no Evidence recipe,
Reward, Confidence, Attributed completion, or training row consumes it.
_Avoid:_ treating Fate as a training label or interpreting external line changes
as human changes.

### CI outcome (`CIOutcome`)
The terminal result for one provider pipeline-run attempt on a commit — the
recorded verifier result and numeric Reward's raw material. The canonical Fact
uses `schema_version = 1`.
`provider` is the normalized CI system selected by the capture adapter or
declared by the vendor-neutral sender. `run_id` is the provider-issued pipeline
run identity, unique within the deployment organization and provider namespace;
`run_attempt` distinguishes retries when the provider supplies it. `run_url` is
an optional location, never identity. `workflow_id` is provider definition
identity; `workflow_name` and `workflow_path` identify the readable check and
the workflow's YAML inside the repo.

The normalized result vocabulary is `passed`, `failed`, `error`, `timed_out`,
`cancelled`, `skipped`, `neutral`, and `unknown`. Only `passed` and `failed`
carry a binary CI verdict; neither proves code quality. `provider_result`
preserves the provider's exact terminal value. `error_type`, `reason`, and
source-event fields preserve only
structured evidence that the sender supplied. A later pass after a failure is
evidence for a pure suspected-flake Derivation; it is not an immutable flake or
code-causality Fact (ADR 0010).
The Fact does not claim that the verifier can run again; Verification
configuration supplies that separate claim.
_Avoid:_ "build result", "status".

### CI resolution (`CIResolution`)
The pure, recomputable commit verdict derived from `CIOutcome` Facts and
`CIResolutionPolicy`. Attempts group by `(org_id, provider, run_id)` and order
only by `run_attempt`; null sorts as attempt 0 when mixed with numbered
attempts. Capture time, ingest order, URL, and generated ids never order
attempts. The last numbered `passed` or `failed` attempt supplies a workflow
verdict. Non-verdict results remain evidence and never supply direction.

A workflow lineage containing both verdicts is `suspected_flake` and keeps its
categorical verdict with default CI reliability 0.0. Clean lineages default to
1.0. Agreeing workflows take the minimum reliability. Conflicting workflow
verdicts produce no aggregate verdict and count
`ambiguous_workflow_verdicts`. Every resolution preserves source outcome ids
and Provenance. _Avoid:_ "latest CI outcome"; treating fact ordering as attempt
ordering; persisting a resolution.

### CI reliability
The `[0, 1]` trust value beside a CI resolution's categorical verdict. It can
discount Confidence without changing a pass into failure, a failure into pass,
or an RLVR Reward away from 1.0 or 0.0. A trainer that consumes a row must honor
this metadata, the Evidence recipe, its source, and Confidence when it chooses a
downstream sample weight. _Avoid:_ "reward"; using reliability as a verdict.

### Repository identity
The deployment organization, forge provider, forge host, and provider-issued
repository ID that identify one repository lifetime. Repository-bearing Facts
capture the flat `repository_provider`, `repository_host`, and `repository_id`
fields together or leave them absent. PR target and head identities remain
independent. A rename changes a repository's name, not its provider identity.
The pure resolver in [ADR 0019](docs/adr/0019-repository-identity-and-renames.md)
owns qualification across Derivations, reports, and exports. A repository name,
clone URL, commit SHA, or Session ID cannot supply missing identity.
_Avoid:_ treating a name or Git history as a repository lifetime.

### Repository rename (`RepositoryRename`)
An immutable forge receipt that records one repository identity's old and new
names, source delivery identity when supplied, capture time, and occurrence
time only when the source proves it. A rename receipt doesn't mutate prior
Facts or assign an ID to a historical Fact that lacks one. GitHub rename
occurrence time remains absent under ADR 0019.
_Avoid:_ a persisted alias map or an inferred current-name record.

### Representative repository label
The smallest normalized repository name in the eligible captured identity
evidence at a Derivation boundary. It labels a stable repository identity
without claiming the provider's latest name. Captured Facts keep their own
source names. Observed names can accompany the representative label in
repository inspection.
_Avoid:_ "current repository name" for this deterministic label.

### Push (`Push`)
A forge push receipt: repo, ref, before and after SHAs. It is a trigger and an
audit Fact. Git truth itself lives in the mirror. _Avoid:_ storing diffs on the push.

### Session-to-commit observation (`SessionCommitObservation`)
The first time a successful mirror refresh exposes a valid Git-note
relationship between one Session and one repository-qualified commit. The Fact
records the triggering Push and capture time without copying the note body.
It establishes when Sediment could use the relationship in a historical
Derivation; it isn't an Attribution. _Avoid:_ "attribution observation";
backfilling capture time from a Push timestamp.

### Pull request merge (`PullRequestMerge`)
A forge-neutral Fact that records one pull request's final head, target branch,
and provider-reported merged commit. It establishes merge boundaries, not code
quality or the cause of a later change. _Avoid:_ "accepted commit"; storing the
pull request diff or review text on the Fact.

### Pull request revision (`PullRequestRevision`)
A forge-neutral Fact that records one observed pull request head and target
boundary. A synchronize event can also record the preceding head. The
Derivation uses these boundaries as pull-request membership evidence after a
rebase changes commit identity. _Avoid:_ "commit mapping"; inferring membership
from timestamps; persisting a derived mapping.

### Quarantine (`QuarantineRecord`, `fact_quarantine`)
The exclusion of a bad Fact from every Derivation and export **without
deleting it**: an append-only log of quarantine/release actions, latest-wins
per Fact, each row carrying a required reason. The Fact's row — and its
dedup key — stay put, so a replayed poisoned payload still collapses.
Releasing restores the Fact to reads. A quarantined Fact is not **visible**
to Derivations — "visible" always means this, nothing else. The organization's
`quarantine_revision` is the integer quarantine-log high-water mark. Every
derived artifact carries the revision in Provenance, so pre- and post-incident
datasets stay distinguishable.
_Avoid:_ "delete" and "purge" for fact removal, because facts are never
deleted; `quarantine_state`; treating quarantine as a mutation of the fact.

### Mirror
The local bare Git repository carrying branches, `refs/notes/sediment`
(Git-notes Attribution), and PR head refs. It is part of the raw substrate
alongside PostgreSQL. ADR 0019 keys identified mirrors by Repository identity
in a separate namespace from legacy (org, repo) mirrors. The path is ownership
metadata, not proof of the remote repository's identity. Known unsafe refreshes
decline visibly; unobserved remote name reuse remains a substrate limit.

### Derivation
A pure, recomputable function of (Facts, policy): Attribution, verifier-result
linkage, attributed-completion assembly. Policy versions it. It is never a source of truth.
A Derivation may be cached, but the cache can always be rebuilt from Facts.
_Avoid:_ "backfill", "healing" (ordering-dependent repair concepts —
Derivations make them unnecessary).

### Policy (`*Policy`, `policy_version`)
The frozen dataclass of knobs a Derivation is versioned by.
`DerivationPolicy` is the canonical version-1 TOML contract shared by attributed
completions and Rollouts. It contains `attribution.git_notes`,
`attribution.jaccard`, and `split`. Component policies include
`AttributionPolicy`, `CIResolutionPolicy`, `RolloutPolicy`, `RecoveryPolicy`, `AttributedCompletionPolicy`,
`ContextRetrievalPolicy` (read-only evidence selection), `OutcomeReportPolicy`, `LabelConfidencePolicy`, `FatePolicy`, and `AttributionSharePolicy`. Tuning any knob bumps
`policy_version`. A policy embedding another (`default_factory`) does not
share its tuning — each embed is tuned separately. _Avoid:_ "config",
"settings" (env-loaded `BaseSettings` are a different layer).

`ConsumerSettings` loads versioned JSON task and response configuration for a
consumer export. It isn't a Policy: the adapter records the operator's source
and digest without changing Fact-derived Evidence or Reward.

### Evidence recipe
A named, versioned interpretation that maps Fact-derived evidence to one
training objective's eligibility, label, or Reward. Each training row identifies
one Evidence recipe and preserves the source of every target it creates; the
same Facts may support separate recipes without becoming one universal quality
label (ADR 0011). _Avoid:_ an unlabeled mixture of human judgment, edit
retention, and CI outcomes; "all signals" as a recipe.

### Canonical schema
A versioned JSON wire-shape contract published with a stable JSON Schema Draft
2020-12 `$id`. Its positive integer version changes when field shape or field
semantics change. It does not identify an Evidence recipe, downstream adapter,
Derivation policy, or physical database revision. _Avoid:_ using
`recipe_version`, `policy_version`, or an Alembic revision as a row schema
version.

### Compatibility profile
A named, versioned downstream adapter contract. It records which canonical
schema the adapter validated and how it maps that row into one consumer's
accepted shape. A profile may remove Sediment metadata only after validation
and recording the input schema. _Avoid:_ treating a target name such as
`swe-bench` as a claim of compatibility with every consumer release.

### Label source
The closed evidence name for one categorical target. DPO records a separate
`chosen_label_source` and `rejected_label_source`; `dpo_human` version 2 uses
`explicit_accept` and `explicit_reject`, while `dpo_outcome` version 2 uses
`resolved_ci_pass` and `resolved_ci_fail`. The human recipe records two
independent gestures and does not claim that the developer compared the members
directly. _Avoid:_ mixing human and CI sources in one pair; reading a source as
a downstream sample weight.

### Eligibility source
The closed evidence name that admitted an SFT or diff-SFT target.
`sft_curated` version 1 uses `explicit_accept` or `edit_retention`; the strong
retention threshold is 0.8. `sft_verified` version 1 uses
`resolved_ci_pass`. A resolved CI pass never creates `sft_curated`
eligibility. _Avoid:_ a broad positive classification; treating a veto or
Confidence as an eligibility source.

### Provenance
The structured audit object on derived artifacts:
`{policy_version, quarantine_revision, policy_digest}`. `quarantine_revision`
is a non-negative integer. `policy_digest` is the full SHA-256 digest of the
resolved canonical policy when that Derivation has one; otherwise it is null.
Two artifacts with different Provenance aren't comparable.
_Avoid:_ compact provenance strings; `quarantine_state`; digest prefixes.

### Grain
The unit one row of an aggregation counts as one "trial" of —
`CIGrain.COMMIT` (default: distinct CI-linked commits) vs
`CIGrain.ATTRIBUTED_COMPLETION` (one trial per Attributed completion;
comparison-only, never inference).
The outcome report carries `grain` beside its structured Provenance, so a grain
change is visible in any exported snapshot. _Avoid:_ conflating grain (the policy choice of what
counts as a trial) with CI trial (the specific dedup unit that choice
currently resolves to).

### CI trial
The de-duplicated unit CI-outcome aggregation counts under the default
commit grain: one trial per `(repo, commit_sha)`, regardless of how many
inference calls or Attributed completions that commit touches — see Grain. CI
trials use CI resolution before pass/fail aggregation. Accepts and rejects
dedup by `decision_id`.

### Fail-soft
The capture/Derivation posture: malformed input degrades — skip, log, fall
back — never raises, because a raise loses sibling Facts (a 500 drops the
batch) or crashes a recomputable Derivation. Distinct from the
skip-and-count house rule (AGENTS.md), though a fail-soft path may also
count a skip. _Avoid:_ "best-effort" without naming what degrades.

### Seam
An internal extension point where an alternate implementation can be
swapped without touching callers — the `FactStore` seam (a domain boundary,
not a multi-backend plug-in; PostgreSQL is the sole implementation under ADR
0012), the scorer seam
(`packages/derive/sediment_derive/scoring.py`'s `Scorer` protocol,
exercised by the precision harness), the storage seam (where a Session is
upserted from whichever Fact arrives first). _Avoid:_ "hook", "plugin" — an
installed client-side mechanism (see Attribution stamper) is a different
kind of extension point.

### Harness
The word names four distinct runners here, so never write "the harness"
bare — qualify every use. **Sim harness:** the Tier A scenario runner
(`sim/scenarios.py`) that replays scripted traffic through the real
pipeline against a ground-truth manifest; `sim/README.md` explains the
scenarios. **Precision
harness:** the scorer precision/recall instrument
(`packages/derive/sediment_derive/precision_harness.py`, a source
identifier, keeps its name). **Agent harness:** the runtime a coding agent
runs under — Claude Code, Cursor, Codex, Gemini CLI; "harness-neutral" means
independent of it, and `docs/exports/rlvr-export.md` imports NeMo Gym's "agent
harness" in this same runtime sense. The agent-experience layer (AGENTS.md, the playbooks, `.claude/skills`, the
client hooks) is the **agent docs layer**, not "the harness".
_Avoid:_ bare "harness", and "the sediment harness" for the agent docs layer —
say "agent docs layer".

### Attribution
The derived join between an inference call and a commit: `git_notes`
(deterministic, from the git-notes Session stamp) with `jaccard`
(token-overlap similarity) as
the universal fallback. Carries `attribution_source` provenance. The word is
the record-linking sense (as in Attribution IDs), not the statistical one —
an Attribution's `similarity_score` is a Jaccard similarity, not an attribution
coefficient.
_Avoid:_ "correlation", "match record", "link", `score`, "Tier 0"/"Tier 1".

### Attribution source (`attribution_source`)
The provenance value on every Attribution-derived artifact: `git_notes`
(deterministic git-notes stamp) or `jaccard` (token-overlap guess). Gates
Confidence. Sediment never discounts a git-notes Attribution. A jaccard Attribution
multiplies Confidence by the similarity score. _Avoid:_ `notes`, "tier", "match type".

### Merge retention (`MergeRetention`)
The diagnostic Derivation that measures how much of one attributed file
addition remains at the pull request's final head and merged commit. It keeps
the two four-gram containment scores separate. A low score doesn't identify a
defect, an editor, or a reason for the change. Merge retention is not an
Evidence recipe. _Avoid:_ "correctness score"; using it as training eligibility.

### Attribution stamper
The client-side writer of the `refs/notes/sediment` stamp that git-notes
Attribution consumes: `scripts/sediment_attribution.py`. Install
mechanics are `sediment install` (`docs/quickstart.md`). _Avoid:_ "hook"
alone.

### Attributed completion (`AttributedCompletion`)
One of the two canonical derived artifacts (ADR 0004, with the Rollout): a
resolved *(inference call, decision, survival, verifier results)* record with Provenance and
the deterministic eval split. It carries exactly one evidence variant: a
complete Attribution `(repo, commit_sha, file_path, similarity_score, attribution_source)`,
or a `SessionAbandonment` with every Attribution field absent. An abandonment
variant exists only when a human explicitly accepted a decision that joins
uniquely to the inference call. Per-inference-call training-row formats (DPO, SFT) are
projections over Attributed completions. Matching captured Session-to-commit Facts
live in `session_commit_observations`; an empty tuple leaves observed identity
unavailable without erasing inferred Attribution. The historical abandonment
variant remains representable, but missing observations cannot derive a negative
Session outcome (ADR 0014).
_Avoid:_ "labeled completion", "triple", "clean triple"; building any export
directly from Facts (sole sanctioned
exception: the Recovery pair, whose commit-pair shape cannot project out of
a canonical artifact — ADR 0004).

### Unique-or-drop
The zero-poisoning signal-attachment rule applied at attributed-completion
assembly and
anywhere else a Fact must join to exactly one other Fact by an id (e.g.
`call_id`): an ambiguous or non-unique id drops the signal rather than
guessing an attachment. Lost signal is the accepted cost of avoiding a
mis-attributed one. _Avoid:_ "best guess", "first match" for this behavior —
it is a deliberate drop, not a heuristic pick.

### Outcome report
The per-model descriptive-plus-inferential artifact
`sediment_export.outcome_report`/`sediment report model` builds over
Attributed completions: rates (Attribution, CI pass, acceptance), confidence intervals,
per-repo stratification, temporal trends, and the `--compare`/
`--compare-all` significance machinery. A projection
like a training row, but reporting-shaped rather than training-shaped.
_Avoid:_ "dashboard" — there is no persisted or served dashboard, only a
report over a fresh read.

### Reversal detection / stratification
The outcome report's per-repo breakdown of a metric (e.g. `ci_pass_rate`),
checked for a Simpson's-paradox-style reversal: an aggregate "model A wins"
that hides model A losing inside every individual repo. See
`outcome_report.py::build_stratification_checks`.

### Attribution rate
The outcome report's ratio of attributed inference calls to captured inference
calls for one model. It is a documented lower bound because Attribution can miss
work that shipped after substantial edits. Abandonment evidence never counts as
attributed. _Avoid:_ `survival_rate`; confusing this report-level attribution
metric with the per-decision edit retention score.

### Rollout
The second canonical derived artifact (ADR 0004): one Session's full
trajectory — its turns in order, grouped into segments — bound to the
Session's repository-qualified `commits` (`git_notes` first, jaccard fallback,
`attribution_source`
carried) and stamped with the terminal CI outcomes, Provenance, and the eval
split. Derived, never persisted. Sequence-shaped training formats (RLVR
`tasks.jsonl` / `rollouts.jsonl`) are projections over Rollouts.
_Avoid:_ "trace", "transcript" (a rollout is derived from inference-call facts,
not captured).

### Turn
One request/response exchange within a Session: the conversation-so-far in,
one model response out, carrying its tool activity and any Developer decision
joined by call id. The atomic unit inside a Rollout.
_Avoid:_ "step" and "message" (a turn *contains* messages).

### Segment
An unbroken chain of Turns within a Rollout. Each continued Turn's typed input
replays the prior input followed by its captured output, preserving roles,
message order, part order, and semantic values. Response `finish_reason` is
excluded from the comparison. Literal text and semantic `cache_control` keys
remain unchanged; capture handles provider-envelope cache metadata. Missing
output, changed input, missing output echo, and message regrouping open another
Segment. Every Turn remains. `RolloutResult.fragmented` counts these boundaries
separately from declined inputs. When continuity is unproven, split: a later
Derivation can recover fragmentation, while a wrong stitch poisons the Rollout.
_Avoid:_ treating rows of the same Rollout as one continuous trajectory.

### Eval split (`split`)
The deterministic train/eval holdout: sha256 of the Session id maps to
[0, 1); eval iff below `eval_fraction` (default 0.1). Session-grained because a
Session's rows are near-duplicates. A DPO pair straddling the holdout lands
in eval (eval wins). Diff-SFT instead skips a mismatched group defensively.
The Recovery pair has no Session of its own, so it stamps split at export
time (`sediment_export.recovery`) instead of attributed-completion/rollout
assembly time:
eval membership is checked over BOTH the failed commit's and the fixed
commit's attributed inference-call Sessions — eval wins over either side;
rows with no resolvable Sessions on either side default to train, and the
unresolvable ids are counted (`inference_call_not_found`). Fixes authored
outside any captured Session carry no Session evidence and fall to the
train default — the holdout guarantee covers captured Sessions only.
_Avoid:_ treating the split as stored — it is recomputed, never
persisted.

### Training row (`DPOPair`, `SFTSample`, `DiffSFTSample`)
A projection of Attributed completions under one Evidence recipe. A **DPO
pair** sets one labeled inference call (`chosen`) against another (`rejected`)
for the **same prompt** — identical message history — and the same model;
pairs never cross prompts or Label sources. DPO recipe v2 requires distinct
complete mapped responses under strict JSON equality. An **SFT sample** is one target
admitted by the selected Eligibility source and Confidence floor. A
**diff-shaped SFT sample** applies the same selected recipe to the exact
attributed-file unified patch read from the mirror. Every row records the
recipe id, integer recipe version, source, CI reliability, Confidence, and
Provenance separately. Trainer inputs use conversational `prompt` and
response-message fields. Sediment evidence stays under `metadata`.
_Avoid:_ "example"; putting evidence fields into the trainer input; deriving a
downstream sample weight in an exporter.

### Prompt bucket
The identical-(prompt, model) grouping unit `project_dpo` (`dpo.py`) forms
DPO pairs within, capped per bucket. Near-duplicate prompts that should
have formed one bucket but split on exact structural equality are a
diagnosed dataset-quality issue (`dataset_diagnostics.py`'s DPO
near-duplicate and bucket-sparsity checks), not corrected automatically.

### Recovery pair (`RecoverySample`)
A red-to-green CI transition of one workflow lineage — the last clean resolved FAILED run
paired with the next PASSED run of the same check on a descendant commit —
carrying the fixing diff and any inference calls attributed to the failed
commit. The one training-row format derived directly from Facts and the
mirror rather than projected over a canonical artifact: its commit-pair
shape does not project out of Attributed completions or Rollouts (ADR 0004). An ambiguous aggregate commit verdict, suspected flake, or
non-verdict never supplies either boundary. _Avoid:_ treating a same-commit
rerun (flaky test) as a recovery; pairing across different workflows.

### Retry/regenerate pair
A not-yet-implemented training-row source: pairing a developer's
retried or regenerated completion against the one it replaced within the
same turn, mined from the client-side transcript extractor's richer
structure (ADR 0007's "second payload"). Distinct from Recovery pair (a CI
red-to-green transition across commits, already shipped).

### Verifier
The evaluation mechanism that checks a candidate result. A CI workflow can act
as a verifier. Sediment records what the verifier reported; it does not infer
an executable verifier from workflow YAML. _Avoid:_ using "reward" for the
mechanism.

### Verifier result
The recorded outcome from a Verifier. `CIOutcome` is Sediment's verifier-result
Fact. A verifier result remains exact evidence and is not interchangeable with
a numeric Reward. _Avoid:_ "reward result".

### Verification configuration
Operator configuration that explains how a Verifier can run again. RLVR target
rows use `verification` with `verification_command` when configured. It remains
separate from Verifier result and is absent when unknown. _Avoid:_ "rerun
command" and inferring it from workflow YAML.

### Reference patch
An observed historical patch resolved from the mirror between `base_commit`
and the attributed commit named by the Verifier result. It may have passed or
failed. Target eligibility decides whether to emit it. _Avoid:_
"gold patch", which falsely implies correctness.

### Reward
A numeric reinforcement-learning value. The NeMo Gym target maps a recorded
resolved CI pass to `1.0` and failure to `0.0`; without a resolved pass or
failure it omits Reward. _Avoid:_ using Reward for a CI outcome, Verifier,
Verification configuration, CI reliability, or confidence.

### Confidence
The Confidence ladder's `[0, 1]` output on an Attributed completion or training row
(`packages/export/sediment_export/label_confidence.py`): decision branch × CI
multiplier × CI reliability × Attribution factor, capped. `None` when the Attributed completion carries
neither a decision nor a CI outcome — never a sentinel float. _Avoid:_
"reward ladder" (legacy phrasing; the code says confidence ladder);
conflating with Reward (a numeric reinforcement-learning value).

### Downstream sample weight
The trainer-selected coefficient applied to a training row. Sediment exports
the categorical label, CI reliability, and Confidence as separate evidence; it
does not silently turn any one of them into a trainer weight. Consumers define
and version that mapping, and must honor zero reliability. _Avoid:_ using
"confidence", "reliability", and "weight" interchangeably.

### Confidence floor
`SFTPolicy.min_confidence` (default `0.6`): the minimum resolved Confidence
an Attributed completion must clear to be SFT-eligible. An Attributed completion
below it is skipped and
counted under `below_confidence_floor` in the closed skip vocabulary, not
silently dropped. _Avoid:_ confusing with Quarantine — a Confidence floor
skip is a per-export eligibility rule over a visible Fact, not Fact
exclusion.

### Org / tenant (`org_id`)
The tenant boundary. Every Fact, Derivation, and export is keyed by `org_id`;
orgs never mix. Canonical form: lowercase ASCII matching
`^[a-z0-9][a-z0-9._-]{0,63}$`, enforced by `normalize_org_id` at every
boundary — case variants are the same tenant, never distinct ones.

## Decisions

Architectural decisions live in `docs/adr/`. Domain-shaping choices
made while building are recorded here as glossary terms.
