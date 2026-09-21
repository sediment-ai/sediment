# ADR 0016 — Bundles validate derived claims against declared source identities

Status: accepted

[ADR 0019](0019-repository-identity-and-renames.md) extends this contract with
bundle v4 repository identity evidence. The Inference call population remains.

[ADR 0020](0020-bounded-derivation-execution.md) defines bounded bundle access
and explicit materialization.

Amends [ADR 0015](0015-lossless-values-and-bundle-v2.md). Preserves
[ADR 0014](0014-factual-outcomes-and-training-evidence.md).

## Context

A bundle can carry a Developer decision that doesn't attach to its referenced
Inference call, or Turn content that differs from captured messages. Checking
only exported calls also hides ambiguous aliases outside the selected cohort.
Offline training needs a reference population independent of artifact selection.

## Decision

Decision: bundle v3 carries the producer's complete organization identity
population and validates derived claims against it. The producer declares that
population from one quarantine-excluding FactStore snapshot through `as_of`.
This declaration is not proof that an external producer disclosed every Fact.
A checksum establishes file integrity, not external truth or historical
quarantine authenticity. An online database prerequisite would prevent the
supported offline workflow; silently rejoining an exported subset would invent
uniqueness.

### Version 3 layout

The manifest declares `bundle_schema_version: 3`, the unchanged
`record_encoding: "sediment-record-json-v1"`, and
`identity_population: "organization-through-as-of-v1"`. The four files are:

- `attributed_completions.jsonl`
- `rollouts.jsonl`
- `inference_calls.jsonl`
- `inference_call_identities.jsonl`

Each line retains ADR 0015's strict outer `record_json` envelope and lossless
inner encoding. Manifest `files` and `counts` name all four populations with
exact row counts; each file has its exact byte count and SHA-256 digest.
Version 1 and 2 imports receive an unsupported-version error with a recomputation
instruction. Their published schemas remain unchanged. The manifest uses the
v3 schema; identity records use the v1 `inference-call-identity` schema.
Unchanged artifact shapes, implementation versions and recipe versions retain
their versions.

The identity record reuses the frozen `InferenceCallIdentity` projection from
the FactStore. Its fields are `inference_call_id`, `org_id`, `session_id`,
`observed_at`, and `call_ids`. `call_ids` is the sorted, distinct union of the
provider-call ID and typed output tool-call IDs. Empty aliases are valid.
Identity records sort by `(observed_at in UTC, inference_call_id)`.
Every identity belongs to the manifest organization and has an aware
`observed_at <= as_of`. Every full call agrees with its complete projected
identity. Duplicate identities fail validation. An explicitly empty identity
population is valid; omitted evidence is invalid. Nonempty evidence requires
an aware `as_of`.

The builder captures identities before cohort selection in the same snapshot
as artifact derivation. A cohort never narrows identity evidence. The identity
population has a 50,000-row limit; `LIMIT + 1` refuses an incomplete read through
`OperationalReportLimitExceeded`. Imports enforce the same row limit. The
population is bounded by design; operators must stop or choose an earlier
historical boundary when it exceeds this ceiling. This limit bounds retained
identity rows, not output-message bytes, decoding cost, database scan work, or
other artifact populations. The store decodes canonical output TEXT in Python;
it doesn't hydrate raw/input content or cast SQL JSON.

### Relationships

The shared unique-or-drop attachment owner verifies every carried Decision.
Attributed completions use the entire organization identity population.
Rollouts use the identity population for their Session. An attachment must
resolve to the artifact's exact Inference call. Missing, unmatched, ambiguous,
subsumed, or inconsistent attachments fail validation with the artifact and
failed relationship. Validation doesn't invent absent Decisions or establish
that all external Decisions were supplied.

A carried Rollout contains every call in its declared Session population once.
The existing pure Rollout owner reconstructs its ordered Segments and Turns
from canonical full calls. Validation compares message suffixes, completion
text, typed tool calls, call identity, and Segment boundaries without coercion.
The existing finite-number replay semantics remain unchanged.
Partial artifact bundles may omit Attributed completions or whole Rollouts;
they retain the full identity population. A cut-down Rollout is invalid.

ADR 0015's retention-score exception remains exact: within each artifact view,
Decision copies agree. Across views, only a captured Rollout null score may
become one consistent non-null Attributed completion score. Validation never
fills the Rollout score or repairs any Fact. All other captured fields agree.

### Public boundaries

`validate_derived_bundle` is the shared public validator before write, after
read, and before bundle-based training orchestration, including in-memory
bundles. Failures raise `BundleValidationError` before projection or publication.
Low-level projectors consume trusted canonical artifacts; their existing
arguments cannot prove an organization population they don't receive. Their
documented prerequisite is canonical assembly or a validated bundle. The
supported bundle-to-training path enforces the validator again.

## Consequences

Offline consumers can reject inconsistent internal claims and detect accidental
cohort narrowing of the declared population. They cannot detect an intentionally
omitted external Fact or authenticate the producer. Large organizations can
exceed the identity budget even for small cohorts. Bundles remain in-memory
artifacts, and publication retains ADR 0015's directory-rename contract.
