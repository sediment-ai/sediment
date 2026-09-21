# ADR 0014 — Factual outcomes and training evidence preserve their scope

Status: accepted

[ADR 0019](0019-repository-identity-and-renames.md) amends literal repository-name
matching with captured provider identity. Factual and recipe boundaries remain.

Amends [ADR 0009](0009-canonical-attribution-contract.md) and
[ADR 0011](0011-training-objectives-own-evidence-interpretation.md).
Builds on [ADR 0013](0013-git-note-observation-facts.md).

## Context

Attribution can identify a plausible relationship between an Inference call and
a file in a commit. An operational report that presents this relationship as a
factual Session outcome overstates its evidence. Similarity 1.0 and high
Confidence don't prove Session identity. A live Git note also lacks the capture
time needed to establish a historical relationship.

The existing Evidence recipes permit inferred Attribution. Requiring observed
identity for those recipes would change their eligibility rather than repair
an operational-report boundary. The two decisions need separate treatment.

## Decision

Decision: factual Session-to-commit outcomes require eligible
`SessionCommitObservation` Facts. Existing version 1 Evidence recipes retain
inferred Attribution eligibility and gain the missing source metadata. Sediment
doesn't introduce observed-only version 2 recipes in this implementation.
The alternatives are a global Jaccard removal or silently treating all canonical
artifacts as observed. Both erase a distinction that consumers need.

### Factual identity

A shared pure helper in `packages/derive/sediment_derive` binds observations by
`(org_id, repo, commit_sha, session_id)`. Its explicit aware `as_of` boundary
admits `captured_at <= as_of`. It consumes quarantine-excluding FactStore reads,
returns source observation identities, and doesn't inspect mutable Git notes.
No caller substitutes a Push timestamp, an Attribution, or a Developer decision's
commit field for an observation. Observing one commit doesn't authorize another
commit in the same Session.

An observation proves only the Session-to-commit edge recorded in the note.
It doesn't prove that an individual Inference call wrote a particular file,
caused a CI outcome, or caused a merge. Similarity-based selection within an
observed Session remains inferred. `attribution_source` retains its existing
`git_notes` or `jaccard` meaning; neither value is an observed-identity flag.

If the observation is absent, the factual Session status remains
`attribution_unavailable` (unknown). Missing evidence can't prove abandonment,
zero commits, CI failure, or failure to merge. The consumer counts
`session_commit_unobserved` separately from measured zero outcomes. Session
status consumers count each evaluated Session once. Consumers evaluating a
specific edge count each distinct `(org_id, repo, commit_sha, session_id)` once.
Aggregation preserves the declared unit instead of adding incompatible counts.
Direct Developer decision, Edit observation retention, and usage metrics remain
available without a Session-to-commit observation.

The binding helper follows the FactStore's historical snapshot and quarantine
contract. Repeated reads and shuffled eligible Facts produce identical bindings
and counts. It doesn't persist derived state or backfill historical observations.

### Canonical artifact fields

`AttributedCompletion` and `Rollout` each add this frozen-dataclass field:

```python
session_commit_observations: tuple[SessionCommitObservation, ...] = ()
```

The value contains the canonical Fact instances, not a second observation model.
An Attributed completion includes only observations matching its organization,
Session, repository, and commit. Its abandonment variant has `()`. A Rollout
includes only observations matching its organization, Session, and one of its
repository-qualified `commits`. Both use the derivation's historical boundary.
Ordering is `(repo, commit_sha, session_id, captured_at in UTC, observation_id)`.
The store owns deduplication; this ordering doesn't invent a second dedup seam.
Conflicting payloads for one observation identity fail bundle validation.

An empty tuple means that the artifact carries no observed Session-to-commit
relationship. It doesn't erase or relabel inferred Attribution. A matching
observation can coexist with `attribution_source="jaccard"`, because those fields
state different claims. Factual consumers validate the matching Fact rather
than testing the tuple's truthiness or changing the Attribution source.

[ADR 0015](0015-lossless-values-and-bundle-v2.md) embeds these Facts in bundle v2.
No fourth artifact file or duplicate persisted observation shape is introduced.

### Evidence recipes and exact metadata

All existing recipe IDs and integer recipe versions remain unchanged. Existing
label, eligibility, Confidence, CI reliability, split, and Provenance fields
remain independent. The added fields carry evidence; they don't create an
eligibility gate. A missing observation never increments
`session_commit_unobserved` in a recipe that permits inferred Attribution.

| Recipe and projector | Required identity and permitted evidence |
|---|---|
| `dpo_human` v1; `dpo.py` | Each direction requires its own uniquely attached human-explicit Developer decision; matching prompt and model rules remain. Both members may carry inferred Attribution. The pair doesn't claim a direct comparative human judgment. |
| `dpo_outcome` v1; `dpo.py` | Each direction requires the applicable resolved CI verdict on its repository-qualified attributed commit. Inferred Attribution remains permitted. CI reliability and pairing gates remain unchanged. |
| `sft_curated` v1; `sft.py`, `diff_sft.py` | Human-explicit acceptance or qualifying Edit observation retention admits the target. Existing vetoes and Confidence floor remain. Inferred Attribution may select the commit/file; it doesn't become the eligibility source. |
| `sft_verified` v1; `sft.py`, `diff_sft.py` | A clean resolved CI pass admits the target under existing gates. Inferred Attribution remains permitted. Diff-SFT additionally requires exact attributed-file patch sections. |
| `recovery_ci` v1; derive/export `recovery.py` | Clean failed and passing CI resolutions require the same provider and workflow definition under the [resolved ID/path rule](../agents/derivations.md#recovery-pairs-recoverypy) and Git ancestry checks. Session-to-commit identity isn't required for the commit-pair target. Optional completion enrichment permits inferred Attribution on both sides. |
| `rlvr_ci` v1; Sediment task and rollout targets | The existing resolved CI, reference-patch, Segment, and optional verification rules remain. Inferred Rollout commit bindings remain permitted. The resolved verdict alone supplies Reward; an audit Segment may lack Reward. |
| `rlvr_ci` v1; SWE-bench target | The existing passing resolution and patch requirements remain. Inferred Session-to-commit Attribution remains permitted; the row doesn't claim individual-call causality. |
| `rlvr_ci` v1; NeMo Gym target | The existing Segment, repository, and verifier rules remain. Inferred Attribution remains permitted. Pass is 1.0, failure is 0.0, and absent verdict omits Reward. |

The following metadata fields are the minimum schema additions. JSON arrays
represent the listed Python collections. Observation ID arrays contain sorted,
unique `observation_id` strings from matching eligible Facts; absence is `[]`,
never null or a fabricated ID. Attribution source arrays contain sorted, unique
`AttributionSource` values ordered by their string value. Existing single source
fields retain their spelling.

| Owner | Fields and source scope |
|---|---|
| `DPOMetadata` | `chosen_attribution_source: AttributionSource \| None = None`, `rejected_attribution_source: AttributionSource \| None = None`; `chosen_session_commit_observation_ids: tuple[NonEmptyId, ...]`, `rejected_session_commit_observation_ids: tuple[NonEmptyId, ...]`, each defaulting to `()`. Each side uses only the selected member's canonical evidence. No Attribution means null source. |
| `SFTMetadata` | `attribution_source: AttributionSource \| None = None`; `session_commit_observation_ids: tuple[NonEmptyId, ...]` defaults to `()`. Both describe the selected Attributed completion, not every candidate considered during ranking. |
| `DiffSFTMetadata` and `SourceIds` | `DiffSFTMetadata.attribution_sources: tuple[AttributionSource, ...]` defaults to `()`. `SourceIds.session_commit_observation_ids: tuple[NonEmptyId, ...]` defaults to `()`. Both collect only members that supply the emitted grouped patch. |
| `RecoverySample` and `RecoveryRow` | Symmetric `failed_attribution_evidence` and `fixed_attribution_evidence` collections, default empty. Both use `tuple[RecoveryAttributionEvidence, ...]`. Each frozen record contains `inference_call_id: NonEmptyId`, `session_id: NonEmptyId`, `attribution_sources: tuple[AttributionSource, ...]`, and `session_commit_observation_ids: tuple[NonEmptyId, ...] = ()`. Records sort by `(inference_call_id, session_id)`. IDs and sources describe that side's repository-qualified commit only. Existing failed/fixed Inference call ID fields and best-effort split behavior remain. |
| `SedimentTaskRow` and `SWEBenchMetadata` | `session_commit_observation_ids: tuple[NonEmptyId, ...]` defaults to `()` and names only observations for the selected `ci_resolution` repository/commit and source Rollout Session. |
| `SedimentRolloutRow` | `session_commit_observation_ids: tuple[NonEmptyId, ...]` defaults to `()` and names observations for the repository/commit pairs represented by the row's emitted `ci_resolutions` and source Rollout Session. An observation for an unrelated Rollout commit isn't copied. |
| `NemoGymMetadata` | `session_commit_observation_ids: tuple[NonEmptyId, ...]` defaults to `()` and names only observations matching the emitted `ci_resolution` and source Rollout Session. Without that resolution the list is empty. |

`RecoveryAttributionEvidence` lives beside `RecoverySample` in derive and stays
a frozen dataclass. It records the optional enrichment result, not a Fact or a
universal evidence interface. An unresolved enrichment ID remains counted under
`inference_call_not_found`; it doesn't create a placeholder Session or discard
an otherwise eligible Recovery pair. A record that has no Attribution source
isn't invented to fill an empty enrichment collection. If a public caller supplies
an enrichment ID without source evidence, the pair remains eligible and counts
`attribution_evidence_absent` once per `(side, inference_call_id)`. This is a
provenance gap, not a fabricated source or an observed-identity requirement.

Training metadata IDs reference the canonical artifacts. They don't supply
standalone proof of a qualified relationship without those source Facts. Bundle
validation verifies the embedded Facts under ADR 0015.

### Consumer boundary

The [consumer inventory](#consumer-inventory) classifies direct callers and
downstream consumers. Factual consumers include
abandonment, accepted-work lifecycle, CI investigations, Session dossier commit
fields, merge retention, repository outcome fields, and outcome-based model or
agent comparisons. A diagnostic intermediary can't feed an inferred edge into
those factual totals.

Attribution coverage, install health, precision evaluation, Confidence
inspection, and explicitly probabilistic analyses retain labeled inferred
Attribution. Canonical Attributed completions, Rollouts, bundle transport, and
training recipes also retain it under their stated purpose. The scorer,
historical policy parser, and capture contract remain available.

## Consequences

Observed coverage can be lower than inferred coverage. Historical data that lacks
observations remains useful for direct metrics, labeled diagnostics, and the
existing training recipes. It can't establish a factual negative outcome.

Published canonical schemas change independently of recipe versions.
[Version ownership](0015-lossless-values-and-bundle-v2.md#version-ownership)
identifies the maintained owners and compatibility checks. Changes update their
affected generated contracts and documentation together.

## Consumer inventory

Each factual consumer validates the qualified observation before aggregating its
outcome population. A diagnostic intermediary can't supply an inferred edge to
a factual total. An observation establishes that a Session appears on a commit;
it doesn't establish that a particular call or model causes its CI or merge
result. Each row identifies the maintained owner and its observable boundary.

| Consumer and owners | Permitted evidence and coverage unit | Checks |
|---|---|---|
| Accepted Session status: [abandonment](../../packages/derive/sediment_derive/abandonment.py), its [report](../../apps/api/sediment_api/reports/abandonment_report.py), and canonical assembly | Observations establish committed status. Missing observations leave `attribution_unavailable`; count each accepted Session once. Legacy maps and mirror or stamper liveness establish no negative outcome. | [Session status tests](../../packages/derive/tests/test_abandonment.py) |
| Merge membership and retention: [Derivation](../../packages/derive/sediment_derive/merge_retention.py), [public builders](../../packages/export/sediment_export/merge_retention_report.py), and [report](../../apps/api/sediment_api/reports/merge_retention_report.py) | Check each qualified candidate Session-to-commit edge once. File-retention denominators remain separate; similarity-selected call/file retention stays inferred within an observed Session. | [Membership tests](../../packages/derive/tests/test_merge_retention.py), [direct report tests](../../packages/export/tests/test_merge_retention_report.py) |
| Accepted-work lifecycle: [assembly](../../packages/export/sediment_export/accepted_work_lifecycle.py) and [report](../../apps/api/sediment_api/reports/lifecycle_report.py) | Scoped and all-history paths gate progression, CI, merge, and model/harness comparisons. Edge coverage counts distinct qualified edges; Session status keeps its Session unit. Direct accepted-call counts remain independent. | [Lifecycle tests](../../packages/export/tests/test_accepted_work_lifecycle.py) |
| Model outcomes: [builders](../../packages/export/sediment_export/outcome_report.py), [report](../../apps/api/sediment_api/reports/model_report.py), and [significance](../../packages/export/sediment_export/significance.py) | Gate before CI grains, workflow counts, strata, funnels, trends, comparisons, bootstrap, effect-decay, and regret. Count each declined qualified edge once, not per file or statistic. Direct Developer decisions and usage retain their own populations. Statistical arithmetic consumes qualified counts without inferring Fact relationships. | [Public builders and intermediate populations](../../packages/export/tests/test_outcome_report.py), [effect-decay inputs](../../scripts/tests/test_model_report.py) |
| Bounded reports: [service](../../apps/api/sediment_api/services/operational_reports.py) and [routes](../../apps/api/sediment_api/routers/reports.py) | Preserve snapshot, scope, `as_of`, identity evidence, supporting caps, and upstream coverage units. Observation-derived Attribution candidates don't disable fallback or waive factual validation. | [Service tests](../../apps/api/tests/test_operational_reports_service.py), [route tests](../../apps/api/tests/test_operational_reports.py) |
| Session dossier: [query routes](../../apps/api/sediment_api/routers/query.py) | Stored observations support exact commit, Push, and CI associations. A Session with no observation has one Session coverage gap. Inferred diagnostics can't populate the exact commit list. Metadata omissions and row caps remain. | [Investigation and mirror-free dossier tests](../../apps/api/tests/test_query.py) |
| Commit and CI investigations: [query routes](../../apps/api/sediment_api/routers/query.py) | Repository-qualified CI Facts remain factual without a Session observation. Gate only Session associations and count distinct candidate edges. Similarity-selected calls stay inferred; absent mirrors don't erase stored observation or CI identity. | [Investigation tests](../../apps/api/tests/test_query.py) |
| Recovery yield: [builders](../../packages/export/sediment_export/outcome_report.py) and [report](../../apps/api/sediment_api/reports/recovery_yield_report.py) | CI failure denominators and same-workflow Recovery pairs need no Session edge. Optional call/Session enrichment remains separate and source-labeled; missing enrichment doesn't remove a valid CI pair. | [Yield tests](../../packages/export/tests/test_outcome_report.py), [Recovery projection tests](../../packages/export/tests/test_recovery_export.py) |
| Canonical evidence and transport: [Attribution](../../packages/derive/sediment_derive/attribution.py), [Rollouts](../../packages/derive/sediment_derive/rollout.py), [Recovery](../../packages/derive/sediment_derive/recovery.py), [assembly](../../packages/export/sediment_export/attributed_completions.py), and [bundles](../../packages/export/sediment_export/derived_bundle.py) | Preserve inferred Attribution for declared uses. Carry full matching observation Facts on both canonical artifacts and source evidence on both Recovery enrichment sides. Canonical presence alone never authorizes a factual total. [ADR 0016](0016-bundle-derivation-consistency.md) governs bundle identity consistency. | [Bundle relationship tests](../../packages/export/tests/test_derived_bundle_io.py), [derived-claim tests](../../packages/export/tests/test_bundle_consistency.py) |
| Attribution coverage and install health: [Attribution share](../../packages/derive/sediment_derive/attribution_share.py) and its [report](../../apps/api/sediment_api/reports/attribution_share_report.py) | Retain labeled note/Jaccard coverage and alerts, including model-report coverage fields. Historical reads respect their boundary; diagnostic coverage can't feed factual commit totals. | [Attribution-share tests](../../packages/derive/tests/test_attribution_share.py) |
| Matcher precision and Push diagnostics: [precision harness](../../packages/derive/sediment_derive/precision_harness.py), [precision report](../../packages/derive/sediment_derive/precision_report.py), and [forge routes](../../apps/api/sediment_api/routers/forge.py) | Retain scorer evaluation and structured Attribution logs, including report and simulation consumers. Capturing a Session-to-commit observation remains a distinct Fact-producing path. | [Harness tests](../../packages/derive/tests/test_precision_harness.py), [precision-report tests](../../packages/derive/tests/test_precision_report.py), [Push tests](../../apps/api/tests/test_push_mirror.py) |
| Confidence and dataset diagnostics: [Confidence](../../packages/export/sediment_export/label_confidence.py), [inspection](../../packages/export/sediment_export/label_confidence_inspection.py), [sensitivity](../../packages/export/sediment_export/label_confidence_sensitivity.py), [dataset diagnostics](../../packages/export/sediment_export/dataset_diagnostics.py), and [calibration](../../packages/export/sediment_export/calibration.py) | Retain recipe/source strata and the Jaccard Confidence discount. Unknown relationships remain explicit or counted exclusions, never factual zero outcomes or delivery claims. | [Owning export tests](../../packages/export/tests/) |
| Direct Developer decisions, usage, Edit observations, and final Fate: [decision latency](../../packages/export/sediment_export/decision_latency.py), [survival](../../packages/derive/sediment_derive/survival.py), [scoring](../../packages/derive/sediment_derive/survival_scoring.py), and [CI resolution](../../packages/derive/sediment_derive/ci_resolution.py) | Their exact attachment, timestamps, retention, and provider-run CI requirements need no Session-to-commit observation. Attribution-selected populations stay labeled. Fate means file retention, not delivery or merge. | [Attachment tests](../../packages/derive/tests/test_attachment.py), [Derivation tests](../../packages/derive/tests/), [export tests](../../packages/export/tests/) |
| Training projections: [DPO](../../packages/export/sediment_export/dpo.py), [SFT](../../packages/export/sediment_export/sft.py), [diff-SFT](../../packages/export/sediment_export/diff_sft.py), [Recovery](../../packages/export/sediment_export/recovery.py), [RLVR](../../packages/export/sediment_export/rlvr.py), and [trainer mapping](../../packages/export/sediment_export/trainer.py) | Every shipped version 1 recipe retains its inferred eligibility. Source metadata describes selected evidence, including both DPO sides and both Recovery enrichment sides. [ADR 0015](0015-lossless-values-and-bundle-v2.md#training-representation) governs representation exclusions. | [Recipe tests](../../packages/export/tests/test_objective_evidence_recipes.py) and each projector's tests |

## Contract checks

The [captured pipeline conformance test](../../apps/api/tests/test_push_mirror.py)
`test_captured_pipeline_conformance_preserves_sources_and_training` crosses
authenticated capture, real Git notes, PostgreSQL, canonical assembly, bundle
roundtrip, and training projection. It checks positive admission, selected source
Fact IDs, split, Provenance, exact skipped and fragmented counts, and identical
bytes after reversed insertion. Its observation cases include absent, late,
quarantined, and wrong organization, repository, commit, and Session evidence.
An observation at `as_of` qualifies. Evidence for another commit can establish
that a Session reaches some commit without authorizing the candidate outcome.
The corpus is synthetic and doesn't establish live harness compatibility.

The consumer tests retain positive, absent-evidence, and near-match controls at
public builders and intermediate populations. Same-Facts and shuffled-order
checks compare identities and diagnostics, not only row counts. The following
checks cover the relationships that supply those populations.

| Relationship | Maintained contract and checks |
|---|---|
| Decision attachment | [Shared joins](../agents/derivations.md#shared-joins-attachmentpy); [attachment tests](../../packages/derive/tests/test_attachment.py) check uniqueness before scope and exact decline counts. [Rollout tests](../../packages/derive/tests/test_rollout.py) preserve its deliberate Session-scoped ambiguity population. |
| Recovery definition identity | [Recovery pairs](../agents/derivations.md#recovery-pairs-recoverypy); [Recovery tests](../../packages/derive/tests/test_recovery.py) cover ID/path/name collisions, retained resolved identity, ancestry, and determinism. |
| Typed Rollout continuity | [Rollouts](../agents/derivations.md#rollouts-rolloutpy); [continuity tests](../../packages/derive/tests/test_rollout.py) retain every Turn, exact typed content, and one reason per fragmented boundary. |
| Shared Git patch grammar | [Attribution deltas](../agents/derivations.md#attribution-deltas-beyond-docsexplanationattributionmd); [real-Git parser tests](../../packages/derive/tests/test_diff.py) preserve exact paths/additions and prevent malformed sections from borrowing adjacent evidence. [Diff-SFT tests](../../packages/export/tests/test_diff_sft.py) check the public consumer. |
| Capture grammar and isolation | [Capture clients](../agents/capture-clients.md) and [Translators](../agents/capture-translators.md); [transcript tests](../../scripts/tests/test_transcript.py) cover execution directory, native content, authored increments, retention, and anchored status using the [documented fixture grammar](../../scripts/tests/fixtures/transcripts/README.md). [Authenticated isolation tests](../../apps/api/tests/test_capture_record_isolation.py) retain valid siblings while storage failures roll back the batch. |
| Representation, selection, and publication | [Representation and version checks](0015-lossless-values-and-bundle-v2.md#contract-checks) cover lossless storage, exceptional trainer values, deterministic evidence selection, literal text, and source/schema consistency. |
