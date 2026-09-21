# ADR 0010 — CI outcomes preserve provider evidence

Status: accepted

[ADR 0019](0019-repository-identity-and-renames.md) amends repository qualification
and the forge-instance namespace for run identity. Attempt semantics remain.

## Context

Sediment needs one vendor-neutral CI fact that preserves what a provider
reported without turning that observation into a quality judgment. A result
vocabulary must preserve timeouts, infrastructure errors, skips, and unknown
provider results. Run identity must distinguish retries that share a URL.

[CDEvents 0.5](https://github.com/cdevents/spec/tree/v0.5.0) defines
vendor-neutral event and subject identity, source event type and version, and
finished build and test semantics. The
[OpenTelemetry CI/CD semantic conventions](https://opentelemetry.io/docs/specs/semconv/cicd/cicd-spans/)
distinguish `success`, workload `failure`, CI-system `error`, `timeout`,
`cancellation`, and `skip`. OpenTelemetry also treats `error.type` as structured,
low-cardinality evidence. Neither standard defines Sediment's repository and
commit attribution keys, tenancy, quarantine behavior, or immutable fact-store
contract.

## Decision

`CIOutcome` remains a Sediment-owned immutable fact aligned with CDEvents and
OpenTelemetry semantics. It is not a native CDEvents or OpenTelemetry wire
shape. [ADR 0019](0019-repository-identity-and-renames.md) defines the Fact
payload version and repository identity additions.

Every CI outcome requires the provider-issued pipeline `run_id`, unique within
the deployment organization and normalized provider namespace. A positive
nullable `run_attempt` distinguishes retries; absence remains null. PostgreSQL
deduplicates on `(org_id, provider, run_id, coalesce(run_attempt, 0))`. `run_url`
remains an optional location. `workflow_id` identifies the provider's workflow
definition; `workflow_name` and `workflow_path` remain descriptive and
repository definition fields.

`CIResult` contains `passed`, `failed`, `error`, `timed_out`, `cancelled`,
`skipped`, `neutral`, and `unknown`. Only `passed` and `failed` are binary CI
verdicts; neither proves code quality. Translators preserve the provider's exact
terminal value in `provider_result`. They populate `error_type` and `reason`
only from structured provider evidence. They never parse logs to invent either
field.

`source_event_type`, `source_spec_version`, and `source_event_id` preserve the
source contract when the sender supplies it. GitHub workflow-run capture uses
`github.workflow_run.completed` as the event type and keeps the complete
workflow-run object in Basic-redacted `raw`.

`POST /ingest/ci` requires the sender to declare the normalized `provider` and
provider-issued `run_id`. Sediment does not infer either value from `run_url`.
The route accepts the optional structured evidence and does not require
`run_url`. GitHub webhooks and the vendor-neutral route produce the same fact
model. The GitHub adapter selects `github_actions` and maps `workflow_run.id` to
`run_id` from the signed webhook payload. On the vendor-neutral route,
`provider`, `run_id`, and source-event fields are assertions by the
bearer-authenticated integration; Sediment does not verify them against the CI
provider.

Derivations may use attempts and structured error evidence to assess reward
eligibility or suspected flakes. They must remain pure functions over facts and
policy. A failure followed by a pass is evidence for a flake assessment; it is
not an immutable `is_flake` fact. CI-system errors, timeouts, cancellations,
skips, neutral results, and unknown results remain visible but neutral unless a
versioned policy explicitly interprets them.

`CIResolution` is that pure interpretation. It groups attempts by
`(org_id, provider, run_id)`, requires one repository, commit, and workflow
lineage per group, and orders attempts only by `run_attempt`. A lone null
attempt remains observable. When null and numbered attempts coexist, null sorts
first as attempt 0, matching the fact-store dedup index. Capture time, ingest
order, URL, and generated ids never order attempts.

The last ordered `passed` or `failed` attempt supplies the lineage verdict.
Every source outcome id and intervening non-verdict remains evidence. A lineage
containing both verdicts keeps its categorical result and defaults to
reliability 0.0. A clean lineage defaults to 1.0. Agreeing workflow lineages use
their minimum reliability. Conflicting workflow verdicts supply no aggregate
verdict and count `ambiguous_workflow_verdicts`.

RLVR maps a resolved pass to 1.0 and failure to 0.0. It never blends
reliability or a non-verdict into reward. DPO, SFT, diff-SFT, recovery, and
outcome reports consume the same resolver. Training exports keep the
categorical label, CI reliability, and downstream sample weight as distinct
concepts. Consumers must honor reliability metadata; a suspected-flake verdict
can therefore remain categorical and carry a full RLVR reward beside zero
reliability by design.

Derived bundles preserve the canonical Fact under their declared schema
contract. Unsupported bundle versions require recomputation from retained Facts.

Fact and bundle schema versions describe serialized contracts. The bundle's
`implementation_versions` and each derived artifact's `policy_version` describe
recomputable derivation semantics. Those provenance values do not select a
legacy fact or bundle reader.

## Consequences

- Provider-issued run identity and retry identity replace URLs as database
  identity.
- Exact provider results and normalized results remain independently auditable.
- Reports and training projections cannot silently count non-verdict outcomes
  as failures.
- RLVR rollouts preserve the exact CI fact. Verifier projections use only
  `passed` and `failed`; non-verdict outcomes never supply a verifier result.
- Flake and code-causality classifications require separate, recomputable
  policies rather than persisted guesses.
- Recovery cannot treat a suspected-flake failure as a red state that a later
  commit repaired.
- Native CDEvents ingest, task-level facts, test-case facts, and CI log parsing
  remain outside this decision.
