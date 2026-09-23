# Fact-store playbook — `packages/core`

Fact shapes live in `sediment_core/models.py`; this playbook covers the remaining storage invariants.

## Module map

| Module | Purpose |
|---|---|
| `models.py` | All persisted Fact shapes + enums; `NonEmptyId`; `QuarantineRecord` |
| `redaction.py` | Fixed Basic redaction patterns + counted `RedactionReason` vocabulary |
| `store.py` | PostgreSQL FactStore, projected reads, repeatable-read snapshots; UTC instants determine insertion receipts and batch Session bounds |
| `postgres_*.py` + `alembic/` | Physical schema, pooled engine, and migrations; see [`postgresql.md`](postgresql.md) |
| `evidence.py` | Frozen evidence/source shapes, exact references, pure projections, bounded strict JSON encoding, and fixed limits; [ADR 0021](../adr/0021-bounded-evidence-access.md) |
| `org.py` | `normalize_org_id` — the only tenant-boundary normalizer |

Everything public re-exports flat from `sediment_core`. PostgreSQL is required at runtime. `SEDIMENT_DATABASE_URL` is the sole Fact-store setting, and public `sediment_core.FactStore` names its concrete implementation. Sediment has no backend interface, selector, dual-write path, or legacy importer.

## Dedup: 11 UNIQUE indexes across 10 tables

- **inference_calls** — `(org_id, gateway_provider, model_call_id)` when the id
  is present. A stamped gateway envelope also has a stable, organization-scoped
  primary key, including keyless calls (ADR 0017). Legacy keyless requests generate
  distinct Fact IDs. `store_inference_call_receipt` returns the retained Fact ID
  after database uniqueness resolves a duplicate; the boolean method delegates.
  Contradictory primary/natural identities raise `InferenceCallIdentityConflict`
  without disclosing a foreign organization's Fact ID. Replay never releases quarantine.
  New retained calls append their exact provider/output-tool aliases and physical count in the same transaction. Redelivery never indexes unretained incoming content; [ADR 0023](../adr/0023-indexed-call-identifiers.md) owns this physical representation.
- **decisions** — two mutually exclusive partial indexes.
  `uq_decisions_keyed` covers only `agent_harness='claude-code'` with a non-null
  `call_id`, and **omits `file_path`**. `uq_decisions_natural` covers
  everything else and **includes `file_path`**, with `COALESCE(call_id, '')`
  and `COALESCE(observation_delay_ms, -1)`. PostgreSQL treats nulls as distinct
  in a UNIQUE index, so `-1` stands in.
  Native Cursor implicit `Write` receipts use a deterministic `decision_id`; the primary key collapses later receipts. Historical rows stay immutable. `packages/capture/sediment_capture/otlp.py` defines the source key and upgrade boundary.
- **edit_observations** —
  `(org_id, agent_harness, session_id, call_id)`, because the
  wire's `tool_use_id` is row-unique per Session. **First write wins** (the
  `uq_edit_observations_call` schema comment): `SessionEnd` can refire, and the
  store drops the later observed file state. `external_lines_added` and
  `external_lines_removed` are nullable with no DEFAULT. NULL is the
  honest reading for every row captured before these fields existed, and `0` makes a different claim.
- **rejected_edits** — the same key, the same first-write-wins, the same
  reasoning.
- **retry_linkages** — `(org_id, agent_harness, session_id, tool_name,
  file_path, rejected_call_id, accepted_call_id)`. Redelivery collapses
  independently of the generated Fact id. Both call ids remain distinct and
  nonempty.
- **ci_outcomes** — `(org_id, provider, run_id, COALESCE(run_attempt, 0))`
  in the legacy branch; identified runs also include forge provider and host.
  Repository ID must agree on a retained run; it cannot create another copy.
  `0` exists only in the index; the Fact preserves an absent attempt as null.
  Result and run URL aren't identity. Different positive attempts remain distinct.
- **pushes** — org, repository, ref, before SHA, and after SHA.
- **pull_request_merges** — org, forge provider, repository, and PR number.
- **pull_request_revisions** — the merge key plus head and base SHAs.
- **session_commit_observations** — org, repository, commit SHA, and Session.
  The first row preserves when capture observed the Git-note relationship.
- **repository_renames** — org, forge provider/host, and source event ID when
  present. Without a source event ID, only the Fact primary key deduplicates.

Separate partial indexes use literal slugs for legacy rows and provider/host/ID for identified rows. [ADR 0019](../adr/0019-repository-identity-and-renames.md) defines the complete keys. Retained receipt methods return the stored Fact and validate primary/natural-key agreement, including PR head identity. Contradictions raise content-free `RepositoryIdentityConflict`; proven names never overwrite history.
An identified observation requires its exact retained source Push with equal organization and identity. Legacy rows keep absent identities; migration never reconstructs them. `read_session_commit_observations(..., as_of=...)` uses inclusive capture bounds, stable ordering, and optional exact commit, repository-commit, or Session filters. Its commit-only filter and the matching CI-outcome filter preserve separate repositories sharing a SHA. Observation writes upsert the Session without inventing user identity.

`store_decisions` bounds each INSERT at 60,000 parameters using the prepared row width. Chunks execute sequentially in one transaction with Session upserts. Returned rows determine ordered input receipts. Session time bounds and sticky user-identity conflicts use all inserted inputs; duplicates add no metadata. A failure rolls back the call. Redaction receipts follow commit.

Session dossier reads use dedicated `(org_id, session_id, event time, Fact id)` indexes for each Session-scoped Fact table. Delivery joins use `ix_pushes_repo_after` and `ix_ci_repo_commit`. Complete Attribution reads visible organization Pushes. Commit investigation streams scalar Push metadata and stops each repository at the target's earliest eligible owner before reading candidate output. The dossier selects file-path presence in SQL and never loads content-bearing columns.

Bounded reads, including Attribution candidate projections, use `LIMIT + 1`; overflow raises instead of truncating. `read_inference_call_identities` reads organization-wide provider and typed output tool aliases through inclusive `observed_through`, without a cohort lower bound. It excludes quarantine and retains only Fact ID, organization, Session, and distinct aliases. It decodes output messages in Python without loading input messages or raw payloads. Its positive row limit rejects incomplete populations with `OperationalReportLimitExceeded`; a separate 64 MiB per-row encoded-output check precedes decoding. Neither bounds database scan work.
Commit attachment uses `read_inference_call_identity_witnesses`: an indexed lookup over up to 30,000 requested identifiers retains one owner or two ambiguity witnesses per key, with only requested aliases. Parent organization, inclusive time, and Quarantine filters share its snapshot; no Session or cohort lower bound hides collisions. It selects no message/raw content. Summary reads can select exact Inference call IDs under the same key budget. These witnesses cannot replace complete bundle identity evidence.
Identity projections retain canonical `observed_at` so offline bundle validation can check the declared bound. Time filters use half-open cohort windows or inclusive capture/merge bounds. `ReportInferenceCall` adds output to summary metadata without input/raw; [bounded content reads](postgresql.md#factstore-and-runtime-boundary) retain one snapshot and explicit byte limits.
Exact filters accept Session/call, repository-commit, or repository-PR keys. `read_evidence_inventory`, `read_evidence_manifest`, and `read_evidence_parts` scope organization and Session before selection, recheck Quarantine, and return complete bounded projections without persisting state. Materialized `read_context_source` and `read_context_discovery_source` retain 8 MiB and 2,048-part limits as small-source readers. Production keyword reads use snapshot-only `stream_context_source` and `stream_context_discovery_source`; callers must consume their complete iterator before leaving the context. See [PostgreSQL read boundaries](postgresql.md#factstore-and-runtime-boundary).
Keyword streams apply at most 32 configured Session IDs before SQL reads. One snapshot shares aggregate 1,000-call, 64 MiB selected-column, 8 MiB row/metadata, and 16,384-part limits, omitting raw payloads/user identity. An exact commit anchor reads matching observations and their visible identity-consistent source Pushes; it never joins by Push.after_sha or infers a grant. [ADR 0025](../adr/0025-authorized-session-candidate-discovery.md) defines the source and absence contract. Grant-scoped factual routes keep independent projections and limits; usefulness judgments never enter storage ([ADR 0026](../adr/0026-grant-scoped-factual-evidence.md)).
Qualified repository-commit and repository-PR filters accept 15,000 keys. A `RepositoryReadKey` is a validated provider/host/ID tuple or an explicit legacy slug;
the legacy branch selects only NULL identity rows. Literal compatibility filters
accept 30,000 pairs. Filters use a SQL `VALUES` relation, with a combined 60,000-bind
budget including extra filters. Overflow refuses before SQL; empty means no rows.
`read_ci_outcome_by_run` refuses multiple visible matches with
`RepositoryReadAmbiguous` instead of choosing a namespace. CI summaries combine half-open capture windows with an optional inclusive `captured_through` before pagination.
`read_repository_identities` and `read_repository_renames` retain complete visible
organization evidence through an inclusive boundary. Each has a separate 50,000-row
cap. Read both from the same snapshot before cohort or repository selection. Commit investigation uses `read_repository_context_witnesses`: original scalar representatives preserve complete names and claims, plus exact queried sources and observation Push anchors. The separate cap counts witnesses, not repeated historical Facts. PostgreSQL grouping still examines history; complete bundle/report populations remain unchanged ([ADR 0024](../adr/0024-targeted-commit-investigations.md)).
## Quarantine

- Latest-wins follows `quarantine_revision`, the database-owned identity.
  `recorded_at` remains audit metadata and never decides visibility.
- `quarantine_revision()` returns the greatest identity for the organization,
  or `0` when clean. Derivations fold that monotone revision into Provenance.
- Reads and `count_facts` exclude quarantined Facts **by default**. Pass
  `include_quarantined=True` from audit tooling only, never from a
  Derivation.
- `quarantine_inference_calls_where` filters inference-call Facts. It rejects
  naive datetimes and validates `reason`
  before dry-run, and rolls back failed batches. Quarantining a nonexistent
  Fact id is legal. Sessions are metadata and cannot be quarantined. Quarantine, release, and bulk apply require the schema at head to prevent pre-migration descriptive-text double encoding; bulk dry-run only counts matches and doesn't certify legacy-schema read compatibility.

## Model-layer rules (`models.py`)

- `tests/test_models.py` enforces validated types or an explicit `WAIVED` entry
  for every Fact and inference-message part field (rule 5).
- `ScalarIdentity` rejects NUL and surrogate code points before SQL. `NonEmptyId` also raises on a whitespace-only id at **construction**, and
  strips surrounding whitespace. `'sess-1\n'` and `'sess-1'` join one
  Session; `raw` preserves the original value.
- `CommitSha` accepts full-length 40 or 64 hex only, stripped and
  lowercased. It guards `CIOutcome.commit_sha`,
  `DeveloperDecision.commit_sha` when present, and `Push.before_sha` and
  `Push.after_sha`. `uq_ci_run` and `uq_pushes_natural` compare byte-exact,
  so case must not fragment one commit into two entries. Capture parsers
  call `normalize_commit_sha` and then skip-and-warn. `notes.py` keeps its
  wider `_HEX_OBJECT` read guard, because that guard validates a git
  argument rather than a Fact.
- Identity carries validated types:
  - `OrgId`, and `NonEmptyId` for `ref`, `call_id`, the primary keys, and
    the quarantine `fact_id`.
  - `ModelName` is the A/B arm key and strips only. The provider-prefix
    waiver is deliberate: `anthropic/claude-x` and `claude-x` stay
    distinct arms.
  - Nullable inference-call and developer-side user identity never become
    `unknown`.
  - `RepoSlug` lowercases, and `""` stays the absent sentinel.
    `BranchName` maps `refs/heads/x` to `x`.
  - `CIOutcome.run_id` is required and non-empty. `run_attempt` is either null
    or a positive int64. `run_url` is an optional non-empty location.
    `pr_number` runs 1 to int64.
  - `AwareDatetime` everywhere; token and latency counts run from 0 to int64.
  - `NonEmptyContent`, `WorkflowName`, and `CIReason` preserve descriptive strings; CI reason keeps its 4,096-character logical limit. `WorkflowPath` validates definition identity. Descriptive storage follows [ADR 0015](../adr/0015-lossless-values-and-bundle-v2.md).

  Parity tests pin nullability. The DDL pins `NOT NULL` on TEXT primary
  keys so the physical contract matches model identity. The vendor
  envelope requires `run_id`, not `run_url`. Webhook parsers degrade rather
  than raise.
- Enum members with no adapter (`PORTKEY`, `HELICONE`, `UNKNOWN`) stay
  envelope-valid **on purpose**, so the route returns a clean 400 rather
  than a 422. Removing them changes the ingest contract.
- `file_path=""` on a decision is a load-bearing sentinel — the Claude Code
  reject path, and the Codex path-less patch — never missing data. The
  read-time collapse lives in `sediment_derive/attachment.py`.
- `occurred_at` is required on decisions and isn't backfilled from server ingest
  time. Native Cursor records hook receipt time; other decision keys include event time. The schema enforces
  `edit_retention_score` bounds, so NaN and inf fail. Pairing the score with
  its observation delay is the translator's job.
