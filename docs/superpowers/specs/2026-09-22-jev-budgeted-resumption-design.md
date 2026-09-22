# Budgeted task resumption with JEV

Implementation tracker: [Issue #89](https://github.com/sediment-ai/sediment/issues/89).

## Purpose and boundary

Measure whether a fresh coding agent can preserve task correctness and a
historical constraint while receiving less history. Compare complete workflow
usage, including selection. This follows the implemented factual reads in
[ADR 0026](../../adr/0026-grant-scoped-factual-evidence.md) and the incomplete
live acceptance in [issue #69](https://github.com/sediment-ai/sediment/issues/69).
The earlier evaluation and its negative results remain unchanged.

Decision: use JEV for the initial decision-model proof. Selection runs in the
experiment consumer at task resumption, before the first coding-model request.
This tests harness-triggered selection. It doesn't establish that an agent
autonomously decides when to call a retrieval tool during an ongoing Session.

Use one isolated integration branch based on main after PR #85. Add Python
evaluation scripts, versioned synthetic fixtures, tests, and documentation.
Reuse the pi isolation, capture, and transport helpers in
`scripts/session_context_retrieval_eval.py`. Preserve that controller and its
fixtures. Add no production route, grant, database migration, persisted domain
state, model SDK, package, or training-label interpretation. Private experiment
artifacts aren't Facts or training labels. JEV receives synthetic evidence only.

## Tasks and comparisons

Freeze three shipment-CSV profiles before continuation runs:

1. **Required history:** the visible task omits a prior business constraint.
2. **Unnecessary history:** the visible task supplies the complete requirement.
3. **Distractor history:** relevant historical requirements and failed attempts
   coexist with irrelevant evidence.

Independent checks cover ordinary rows, quoted commas and quotes, embedded
newlines, blank rows, whitespace, and the case-sensitive historical constraint.
Run positive reference implementations and negative mutations against these
checks before freezing. Validator files and gold reference labels remain
outside both coding-agent and selector inputs. Visible checks demonstrate the
ordinary task without disclosing an intentionally hidden requirement.

Create real captured source Sessions with the native coding harness. Verify
that the preserved workspace doesn't change during source capture. Verify
each earlier captured call's input and output is a prefix of the final call's
input. The final call's input and output then form the complete historical
conversation for this experiment. Don't concatenate repeated call histories.
Save the final Fact ID, exact occurrence references, history hash, source
capture costs, and workspace identity. Source setup costs are reported
separately from per-resumption costs.

Each profile has four arms and three repetitions, for 36 continuations:

| Arm | History delivered to the coding agent |
| --- | --- |
| A | None |
| B | Complete verified captured conversation |
| C | Budgeted keyword selection |
| D | Budgeted JEV selection |

All arms use the same preserved workspace, visible continuation prompt,
Ministral model, pi version, coding tools, sampling settings, and limits.
Use the existing pinned local coding-model configuration. Disable compaction
and retries. Each continuation starts a fresh Session and isolated container.
No arm exposes a native retrieval tool; the consumer supplies history before
the first model call. Rotate the four-arm order over the three repetitions
and profiles; store the entire order before execution. This is a small
diagnostic, not a population-level quality estimate.

## Evidence and budget contract

Use the retrieval credential for `/v1/me`, inventory, manifest, and exact
reads under `/query/context/evidence`. The operator credential is reserved
for source/capture verification outside the selector. Each read must verify
Session and Fact identity, exact reference membership, representation, and
Quarantine revision. Reject a source changed since its frozen manifest.

C and D read the same final-call candidate corpus through the factual routes.
The keyword baseline reuses version-1 tokenization, overlap scoring, ordering,
and reasoning exclusion on that corpus. Both selectors retain distinct
occurrences without content deduplication. It doesn't claim to reproduce the entire
Session scan performed by `/query/context`. This avoids giving the selectors
different occurrence populations. Both arms count all candidate prefetches.

Limits for this diagnostic are 32 candidate occurrences, 32,768 bytes for the
serialized candidate catalog, 65,536 bytes for a JEV request, and 8 selected
occurrences. Reject an oversized catalog rather than silently omitting parts.
Sediment's existing read and transport limits also apply.

The complete historical-evidence envelope delivered by C or D is at most
8,192 UTF-8 bytes, including reference metadata, serialization, and escaping.
Select complete original parts. Don't truncate, summarize, or split them.
Packing is deterministic with stable reference tie-breaking. Count exclusions
and budget omissions. A selects nothing; B is the explicitly larger baseline.

Re-fetch selected references through the factual read route before delivery.
Validate their content against the catalog and its Quarantine revision. The
catalog fetch and final fetch are separate measured operations. Only selected
parts enter the coding-agent prompt, but all prefetch and selector work counts
toward the experiment's total. A reference never grants authority.

## JEV decision contract

The initial adapter uses the official
[TypeSafe HTTP API](https://docs.typesafe.ai/api) and pins `jev-1.13.0` from the
[model documentation](https://docs.typesafe.ai/models), checked on 2026-09-22.
Use the existing HTTP dependency. Keep the provider key in the consumer process;
never put it in prompts, agent containers, Git, or public artifacts.

Make at most one JEV request per D continuation, with no automatic retry or
fallback to another arm. State contains only the visible task and bounded
candidate evidence. Ask one Choice for `read_history`, `no_history`, or
`insufficient`, plus one Noul per candidate asking whether that occurrence
contains information needed for the visible task. Every candidate question
explicitly names its candidate in the instruction; API question keys aren't
model inputs. Historical instructions remain evidence, never authority.

Freeze the decision rule: act on the Choice only when confidence is at least
0.6; otherwise record `insufficient`. If it chooses `read_history`, rank
candidates with Noul at least 0.5 by decreasing value and stable occurrence
identity, then pack whole parts within both limits. Empty selection becomes
`insufficient`. Thresholds are experiment settings, not a calibration claim.
`no_history` and `insufficient` supply no historical content and remain distinct
recorded outcomes. The coding agent still attempts the visible task.

Require the pinned returned model, exact answer keys and types, finite bounded
probabilities, complete normalized Choice probabilities, valid returned option,
and nonnegative integer usage. Reject missing/extra decisions, malformed JSON,
non-finite values, model drift, redirects, oversized responses, and transport
errors. Retain bounded private traffic without authorization headers. Count
failed attempts and record unavailable usage explicitly. Missing access blocks
live D runs; it never becomes a mock result presented as a live proof.
The separate JEV preflight checks access and the response contract. A valid
abstention passes that check and remains visible in its metrics; preflight
doesn't require a favorable selection decision.

## Measurement and frozen execution

Freeze fixture, controller, selector, runtime image, model, source history,
workspace, query, thresholds, budgets, and run-order identities. Refuse altered
or resumed output directories. Read-first behavior is observed separately from
task correctness. Retain every scheduled arm, refusal, failure, and timeout.
Don't tune using the held-out results or rerun failed continuations in place.

Record correctness, historical-constraint recovery, selected source references,
evidence delivery verified in the captured coding prompt, source/capture
verification, coding calls and native tool calls, all evidence HTTP calls and
bytes, JEV requests and bytes, per-model input/output/cache usage, and elapsed
time. Missing usage stays absent. Total reported tokens include selector usage
and carry model-specific breakdowns because model tokenizers differ.

Report full prompt usage across all coding calls, not only the first history
injection. Cache reads and writes remain separate. Price estimates name the
provider, rate, source, and date. JEV's published input price on 2026-09-22 is
$0.042 per million tokens; output tokens have no listed charge. Local coding
inference has no measured total dollar cost without compute metering. Zero
API fees aren't a zero-compute-cost estimate.

The report separates three outcomes: implementation/measurement validity,
observed task quality, and observed usage. A savings claim requires complete
accounting and passing quality checks in the compared runs. Negative model
results are publishable outcomes; missing access or invalid measurement is an
incomplete experiment. No inference about general savings or statistical
non-inferiority follows from these 36 runs.

## Implementation tickets and acceptance

- **T1 — Fixtures and validators.** Own only the versioned fixture directory and
  its tests. Prove the reference solution passes, the initial program fails,
  and newline and historical-rule mutations fail the appropriate checks.
- **T2 — Evidence selection.** Own a script module and its focused tests.
  Implement the factual client, shared catalog, keyword/JEV selectors, strict
  response contract, deterministic packing, and complete request accounting.
  Exercise authority, stale visibility, exact integers, malformed responses,
  missing usage, and budget overflow with real schema instances.
- **T3 — Experiment driver.** Own the command, its tests, private artifacts,
  freeze checks, matrix, source capture, isolated continuations, independent
  validation, and summary. T3 integrates T1/T2 after their contracts settle.
- **T4 — Validation and closeout.** Run offline checks, real Sediment API
  acceptance, native preflight, and one live matrix when access exists. Update
  maintained documentation and the tracker. Perform independent whole-branch
  and different-vendor reviews, then required CI. Preserve raw artifacts
  privately and publish only sanitized counts and limitations.

T1 and T2 can proceed independently after this contract. Root owns T3,
integration, maintained documentation, and T4. Use isolated implementer
worktrees and serialize integration. Keep the PR draft while any required
acceptance is incomplete. An honestly recorded unfavorable JEV outcome doesn't
block completion; absent credentials or an invalid live run does. This work
doesn't automatically close issue #69 or authorize merging the PR.
