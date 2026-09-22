# ADR 0015 — Canonical values survive storage and bundle roundtrip

Status: accepted

Amends [ADR 0009](0009-canonical-attribution-contract.md),
[ADR 0010](0010-canonical-ci-outcome-facts.md), and
[ADR 0012](0012-postgresql-fact-store.md).

## Context

Canonical content can preserve NUL, unpaired surrogates, and non-finite numeric
values. PostgreSQL scalar columns and strict training JSON impose different
limits. Serializing every destination identically either loses accepted Facts
or emits a training row that the consumer can't represent.

Decision: preserve canonical content through lossless serialized storage and a
versioned bundle envelope. Validate real scalar identities before SQL. Apply
trainer representation exclusions only to values that the selected recipe
emits. The alternatives are coercion, silent omission, or one restrictive
validation rule for every destination; none preserves these contracts.

## Decision

### Scalar storage

Domain meaning determines validation, not the name of an existing Python alias.
Organization, Fact, Session, user, call, run, workflow, and source-event IDs;
repository, branch/ref, commit, and file-path keys; and model grouping keys
reject NUL and unpaired surrogates before a database statement. Existing
normalization, optional values, and documented empty sentinels remain.

Descriptive scalar content uses ASCII-escaped serialized `TEXT` at the FactStore
seam. This includes CI `reason`, `workflow_name`, `run_url`, `provider_result`,
`error_type`, `source_event_type`, `source_spec_version`, Push `clone_url`, and
quarantine `reason`. Existing trimming, nonempty rules, and logical length bounds
remain. Descriptive uses of `NonEmptyId` move to content validation before that
identity validator tightens. `workflow_path` is a definition key;
`model_provider` is a grouping key. Neither inherits descriptive semantics.

Messages, raw trees, authored text, observed text, and proposals retain their
lossless serialized `TEXT` contract after Basic redaction. PostgreSQL doesn't
parse this content. Projected reads decode selected content in Python without
loading unrelated `raw`. The compatibility inference-evidence read extracts
tool-call IDs from canonical typed parts and removes its JSONB cast.

[ADR 0023](0023-indexed-call-identifiers.md) adds a physical index over exact
copies of typed call identifiers for interactive attachment. Capture and a
forward migration extract those copies in Python. The canonical messages and
their exceptional values retain this lossless contract.

A forward-only Alembic revision converts affected descriptive columns without
changing logical Facts. SQL NULL stays NULL; decoded empty string stays distinct.
Constraints measure logical values at canonical validation, not encoded text
length. Facts remain immutable; this encoding migration doesn't repair history.
The [executable scalar inventory](../../packages/core/tests/test_scalar_representation.py)
classifies every physical column and its nullability. Its
`test_representation_inventory_covers_every_column_and_fact_field` also checks
complete Fact and inference-message part field coverage, including tool-call IDs
inside opaque content. Added fields extend that inventory and their contract in
the same change. [Canonical models](../../packages/core/sediment_core/models.py)
and [physical metadata](../../packages/core/sediment_core/postgres_schema.py)
remain the schema owners; the test inventory is not a second runtime registry.

Aware timestamps preserve their instant. Insertion receipts normalize timestamp
components to UTC before comparing returned rows with input candidates. The
receipt still matches the complete returned identity rather than Fact ID alone;
PostgreSQL remains the deduplication authority. Every accepted Developer decision
upserts its Session in the same transaction. Every `BIGINT` field retains its
lower bound and has a signed 64-bit upper bound; the store doesn't clamp values.

### Canonical query responses

Query responses validate their declared response models in Python, then use
ASCII-escaped strict JSON. Descriptive NUL and surrogate strings remain exact.
If an emitted value contains a non-finite number, the query declines the complete
response with HTTP 409 and `{"detail":{"reason":"non_finite_number"}}`.
This reason has a closed vocabulary of one value. The stored Fact stays intact;
metadata projections that omit the affected content remain available. Query
responses don't substitute null, omit content silently, or use the bundle
encoding. Unrelated validation, programming, and storage errors don't acquire
this representation reason.

### Bundle version 2

The manifest declares `bundle_schema_version: 2` and
`record_encoding: "sediment-record-json-v1"`. The existing three artifact files
remain `attributed_completions.jsonl`, `rollouts.jsonl`, and
`inference_calls.jsonl`. Every line has exactly one outer field:

```json
{"record_json":"<serialized canonical record>"}
```

The manifest and outer envelope use strict JSON: ASCII escapes, no non-finite
numeric tokens, sorted keys, and compact separators. The inner record uses
ASCII-escaped JSON with the explicitly declared Python numeric extensions
`NaN`, `Infinity`, and `-Infinity`. Only the declared inner decoder accepts those
extensions. The encoder operates on canonical Python values without a model
serialization step that converts exceptional numbers to null. Protocol-shaped
trees are supported; arbitrary Python objects in `Any` aren't.

Both layers reject duplicate object keys and trailing payload data. Unknown
outer fields and unknown encodings fail validation. A user dictionary lives
inside `record_json` and can't collide with the outer envelope. Checksums, byte
sizes, and row counts describe the exact outer file bytes. Equivalent canonical
inputs produce deterministic bytes. NUL, surrogates, finite numbers, non-finite
numeric categories, empty values, and null survive read/write unchanged.

The inner Attributed completion and Rollout records contain the full matching
`session_commit_observations` Facts from [ADR 0014](0014-factual-outcomes-and-training-evidence.md).
The reader reconstructs those canonical Facts. It needs no live store or mirror
to validate their identity and `captured_at <= manifest.as_of` boundary. If any
artifact carries observations, the manifest requires an aware non-null `as_of`.
The builder determines that boundary before binding observations. An empty
collection remains a valid inferred artifact; the reader doesn't demand observed
identity from a recipe that permits inference.

One semantic validator runs before write and after read. It verifies exact
Inference call references, organization and Session agreement for every artifact
and Turn, the Session's split under resolved policy, and matching observation
organization, Session, repository, commit, capture boundary, and consistent
observation IDs. Existing manifest, Provenance, hash, and quarantine-revision
checks remain. This validates internal claims; a self-contained bundle can't
prove whether an external source was truthful or later quarantined.

The pre-existing Attributed completion assembly projection can fill a captured
Developer decision's absent `edit_retention_score` from Edit observations.
Rollout retains the captured Decision. Within each artifact view, every copy of
a decision ID must have an identical payload. Across those two views, all other
captured fields must agree. The only permitted score difference is a captured
Rollout score of null and one consistent non-null Attributed completion score.
A captured non-null score must match exactly; two derived scores cannot disagree.
This exception preserves the existing assembly projection and version 1 recipe
eligibility. It doesn't permit other conflicting Facts, change stored Decisions,
or fill Rollout scores during validation.

The manifest requires `fragmented`, a mapping of nonnegative integers with the
closed reasons `prior_output_absent`, `input_history_changed`, and
`prior_output_not_replayed`. The builder copies Rollout derivation counts;
Segment count can't reconstruct the cause of each boundary. `skipped` continues
to describe declined inputs or artifacts.

Validation failures raise `BundleValidationError` with the artifact identity and
failed relationship. Readers don't repair IDs, choose a different split, or drop
an offending artifact. Writers prepare the destination before replacement;
validation or serialization failure leaves existing files intact.

Version 1 bundles receive an unsupported-version error with a recomputation
instruction. They aren't rewritten. Their published schemas remain immutable.
Bundle v2 is an explicit replacement for development bundles, not a compatibility
reader or a historical algorithm executor.

### Training representation

The shared trainer mapper rejects non-finite numbers and unpaired surrogates in
emitted values, including nested tool arguments and dictionary keys. Its closed
reasons are `non_finite_number` and `unrepresentable_unicode`. Each projector
also validates its emitted metadata and target-specific values. Content omitted
by a recipe doesn't make the emitted row ineligible. A paired target declines
as one candidate when either side is unrepresentable. Each projector composes
these reasons into its existing vocabulary and counts one reason per declined
candidate.

The common JSONL writer uses ASCII escaping and `allow_nan=False`. It enforces
serialization rather than duplicating recipe eligibility. Empty inputs don't
truncate existing destinations. It serializes all nonempty split partitions to
temporary files before replacing any destination, so a later serialization
failure can't partially publish earlier partitions. The filesystem doesn't
promise an atomic transaction across multiple successful replacements.

## Consequences

Bundle consumers perform an explicit inner decode. Training rows retain strict
JSON and narrower representation guarantees. Reports may preserve exceptional
strings through escaping without claiming trainer compatibility.

Physical encoding changes retain Fact payload `schema_version`. Published schema
IDs advance whenever shape, defaults, nested definitions, or constraints change.
Each published ID/version pair stays immutable. The [version owners](#version-ownership)
keep these schema versions separate from recipes, policy labels, implementation
defaults, and Alembic revisions.

## Version ownership

Each algorithm owner supplies its implementation default to direct calls,
embedded policies, default factories, bundle construction, and validation.
Caller-supplied policy labels stamp the running implementation; they don't
select historical code. The policy digest identifies resolved knobs, while
implementation versions identify algorithms. Neither replaces the other.
Values live with these owners rather than in a second documentation registry.

| Owner | Propagation and independent meaning |
|---|---|
| [Attribution policy](../../packages/derive/sediment_derive/attribution.py) | Direct Attribution, Recovery enrichment, canonical assembly, Rollouts, diagnostics, report Provenance, Derivation policy, and bundle implementation map |
| [Attributed completion policy](../../packages/export/sediment_export/attributed_completions.py) | Public assembly, DPO/SFT/diff-SFT evidence, reports, bundle builder, and validator |
| [Rollout policy](../../packages/derive/sediment_derive/rollout.py) | Direct calls, default factories, RLVR, bundle construction, and validation consume the owner's implementation version |
| [Recovery policy](../../packages/derive/sediment_derive/recovery.py) | Direct Recovery, CLI export/yield, and exported row Provenance |
| [Abandonment policy](../../packages/derive/sediment_derive/abandonment.py) | Session status, assembly, lifecycle, bounded service, and bundle implementation map |
| [Merge-retention policy](../../packages/derive/sediment_derive/merge_retention.py) | Direct membership and retention, reports, and lifecycle retention Provenance |
| [Lifecycle policy](../../packages/export/sediment_export/accepted_work_lifecycle.py) and [outcome-report policy](../../packages/export/sediment_export/outcome_report.py) | Direct builders, generators, CLI, and HTTP paths retain their own version plus distinct upstream Provenance fields |
| [CI resolution](../../packages/derive/sediment_derive/ci_resolution.py), [label Confidence](../../packages/export/sediment_export/label_confidence.py), [Fate](../../packages/derive/sediment_derive/survival.py), and [Attribution share](../../packages/derive/sediment_derive/attribution_share.py) | Each algorithm owns its default. An upstream change doesn't imply unrelated tuning. Label Confidence's [Provenance gap](../agents/exports-and-stats.md#the-confidence-ladder-label_confidencepy) remains explicit. |
| [Evidence recipe owners](0014-factual-outcomes-and-training-evidence.md#evidence-recipes-and-exact-metadata) | Recipe versions describe evidence eligibility independently of schema shape. Adding source metadata doesn't introduce an observed-only requirement. |
| [DPO policy](../../packages/export/sediment_export/dpo.py), [SFT policy](../../packages/export/sediment_export/sft.py), and [mirror policy](../../packages/derive/sediment_derive/mirror.py) | These policies lack an independent implementation-version field. Changes describe this stamp gap in release notes; they don't invent an unsupported field. |
| [Derivation policy and bundle implementation map](../../packages/export/sediment_export/derived_bundle.py) | Policy schema versions describe keys and knob meaning; the digest covers resolved knobs. Bundle implementation constraints consume algorithm owners independently of caller labels. |
| [Fact models](../../packages/core/sediment_core/models.py) and [Alembic revisions](../../packages/core/sediment_core/alembic/versions/) | Fact payload versions and physical migration revisions are separate. Encoding-only migrations preserve logical Facts. |
| [Schema registry](../../packages/export/sediment_export/schema_contracts.py), [row identities](../../packages/export/sediment_export/schema_identity.py), owning classes, and [generator](../../scripts/gen_schema_docs.py) | Changed shape, defaults, constraints, or nested definitions require the affected schema successors. Published ID/version pairs remain immutable, including inactive schemas absent from the active catalog. |
| [Bundle owner](../../packages/export/sediment_export/derived_bundle.py) and [ADR 0016](0016-bundle-derivation-consistency.md) | Container and record-encoding versions remain separate from algorithms, recipes, and Fact payloads. ADR 0016 amends the layout and derived-claim validation while preserving this ADR's lossless encoding. |

The [schema closure test](../../scripts/tests/test_canonical_json_schemas.py)
`test_foundation_policy_owners_match_the_published_schema_closure` checks owner
defaults, caller labels, bundle implementation constraints, and published nested
policy defaults. The [schema generator](../../scripts/gen_schema_docs.py) checks
freshness and compatibility against every versioned schema at the selected base,
including inactive published files. A generated-document check alone doesn't
establish compatibility. Algorithm versions can't decrease or identify different
behavior under the same version.

## Contract checks

These checks complement the [factual consumer checks](0014-factual-outcomes-and-training-evidence.md#contract-checks).
Each boundary retains positive admission and exact exclusions. Representation
checks use real canonical values and the owning store, reader, or writer.

| Boundary | Runnable checks |
|---|---|
| Scalar classification and lossless storage | [Scalar representation tests](../../packages/core/tests/test_scalar_representation.py) enumerate physical columns, nullability, Facts, and typed parts; check invalid identities before SQL; and compare full/projected descriptive reads. [Compatibility response tests](../../apps/api/tests/test_v1.py) and [CI ingest tests](../../apps/api/tests/test_ci_vendor.py) cross authenticated routes. |
| Timestamp receipts and Session atomicity | [FactStore tests](../../packages/core/tests/test_store.py) distinguish both repeated-hour instants, preserve duplicate-ID receipts, and commit stored Developer decisions with their Sessions. [Migration tests](../../packages/core/tests/test_postgres_migrations.py) compare logical values and schema revisions. |
| Canonical bundle values and relationships | [Bundle read/write tests](../../packages/export/tests/test_derived_bundle_io.py) retain exceptional values and deterministic bytes, reject conflicting identities and train/eval reference mismatches, and check observation identity/time. [Bundle consistency tests](../../packages/export/tests/test_bundle_consistency.py) enforce ADR 0016's complete declared identity population, attachment, and full Session Turn reconstruction. |
| Narrow retention view | [Bundle read/write tests](../../packages/export/tests/test_derived_bundle_io.py), `test_bundle_preserves_documented_retention_views` and `test_retention_projection_exception_is_narrow`, preserve the [sole score exception](#bundle-version-2) without changing captured Facts. |
| Training representation | [Trainer tests](../../packages/export/tests/test_trainer.py) and [DPO](../../packages/export/tests/test_dpo.py), [SFT](../../packages/export/tests/test_sft.py), [diff-SFT](../../packages/export/tests/test_diff_sft.py), [Recovery](../../packages/export/tests/test_recovery_export.py), and [RLVR](../../packages/export/tests/test_rlvr.py) suites check nested values, emitted metadata, complete row/pair exclusions, and exact reasons. |
| Deterministic evidence and literal text | [SFT tests](../../packages/export/tests/test_sft.py) check repository-qualified ties, conflicting evidence, eligibility precedence, and selected metadata under permutations. [RLVR tests](../../packages/export/tests/test_rlvr.py) preserve canonical text verbatim for each task target. |
| JSONL publication | [Writer tests](../../packages/export/tests/test_jsonl.py), including `test_all_split_partitions_are_prepared_before_replacement`, preserve existing destinations when later serialization fails. These checks don't claim a transaction across multiple replacements. |
| Version and schema closure | [Owner/default tests](../../scripts/tests/test_canonical_json_schemas.py) and [generator tests](../../scripts/tests/test_gen_schema_docs.py) check the maintained registry and immutable published history. |
