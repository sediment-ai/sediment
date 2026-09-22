# PostgreSQL playbook — `packages/core`

This playbook covers the PostgreSQL substrate, complete FactStore, and runtime boundary. Read
[`fact-store.md`](fact-store.md) for Fact-store invariants and
[`../adr/0012-postgresql-fact-store.md`](../adr/0012-postgresql-fact-store.md)
for the binding storage decision.

## Module map

| Module | Purpose |
|---|---|
| `postgres_schema.py` | Complete SQLAlchemy Core metadata for Facts and Sessions |
| `postgres_engine.py` | Homebrew libpq discovery and bounded synchronous psycopg 3 pool; API-only statement, lock, and idle-transaction limits |
| `postgres_migrations.py` + `alembic/` | Advisory-locked upgrades and read-only revision inspection |
| `postgres_roles.py` | Fixed deployment roles, legacy ownership adoption, and runtime privilege validation |
| `store.py` | FactStore, projected reads, and repeatable-read snapshot |

## Physical schema

`postgres_schema.py` defines every Fact table, the Sessions aggregate root, and the physical call-identifier lookup representation from [ADR 0023](../adr/0023-indexed-call-identifiers.md).
The baseline uses PostgreSQL timestamps with time zones, booleans, integer
identity for quarantine ordering, named checks, and database-owned natural-key
deduplication. Content-bearing structured values remain serialized `TEXT`. Descriptive scalar columns use the same lossless ASCII-escaped representation through the column codec; full reads, projections, and equality filters share it.
Do not convert them to `JSONB`: provider payloads can contain lone surrogates, null characters, nonfinite numbers, and integers outside PostgreSQL's numeric range.

Alembic revisions: `0001_postgresql_baseline` freezes the schema;
`0002_retry_linkages` adds RetryLinkage Facts; `0003_cursor_agent_harness` admits
`cursor`; `0004_pull_request_merges` and `0005_pull_request_revisions` add pull-request Facts; `0006_ci_failure_lookup` and `0007_session_dossier_lookup` add CI-investigation and Session-dossier indexes; `0008_session_commit_observations` adds immutable Git-note observation Facts and their historical-read index. `0009_descriptive_text_encoding` converts CI descriptions, Push clone URLs, and quarantine reasons without changing logical values or SQL NULL. Canonical validators retain logical limits; encoded-length checks are removed. `0010_repository_identity` adds nullable identity triples, separate legacy/identified indexes, and Repository rename Facts. Existing rows retain their logical values and absent identity under payload version 1; version 2 permits captured identity. `0011_inference_call_aliases` copies canonical provider/output-tool identifiers into a native hash-indexed table and adds a non-null physical parent count without an INSERT default. Its frozen Python backfill reads one output at a time, includes quarantined parents, and blocks parent writes through commit. Stale INSERTs fail; canonical payloads stay intact. Keep revisions frozen; later changes require a successor. Tests compare checks with live metadata.

## Migration operations

`sediment db upgrade` runs migrations under a PostgreSQL advisory lock. `sediment db status` performs read-only inspection and reports `absent`, `behind`, `at_head`, or `ahead`. Both commands read `SEDIMENT_DATABASE_URL` or `--database-url`. Expected connection, authentication, permission, lock, and migration failures print a credential-free error and exit 1.

`sediment db provision` reads `SEDIMENT_BOOTSTRAP_DATABASE_URL`, `SEDIMENT_MIGRATOR_PASSWORD`, `SEDIMENT_RUNTIME_PASSWORD`, and `SEDIMENT_OPERATOR_PASSWORD`. The URL requires an explicit host and database. Passwords must differ, including from bootstrap. Stop services before provisioning or rotating credentials. Client-side SCRAM verifiers keep plaintext passwords out of SQL.

`postgres_roles.py` reconciles fixed `sediment_migrator`, `sediment_runtime`, and `sediment_operator` identities for one dedicated database. It adopts recognized bootstrap-owned tables and identity sequences, migrates through the migrator credential, then applies explicit grants. Repeat provisioning preserves Facts. Unknown objects remain untouched; unexpected shapes, ownership, or permissions fail.

Runtime reads and appends regular Facts and physical call aliases; operator can read the aliases. The source foreign key cascades sanctioned wholesale deletion, and its primary key indexes source lookup. Neither service role can mutate physical aliases. Session UPDATE covers only `first_observed_at`, `last_observed_at`, `user_id`, and `user_id_conflict`. Operator reads and appends quarantine records with sequence usage. Migrator owns application objects. Runtime and operator cannot change Facts, create objects, write Alembic state, or assume other roles.

`postgres_roles.py::validate_runtime_privileges` checks an engine without writes. Production startup calls it after revision validation; only explicit development configuration bypasses it. The validator rejects privileged attributes, memberships, ownership, excess table/column grants, grant options, unexpected accessible objects/functions, and missing schema/grant coverage. `DatabasePrivilegeError` carries a credential-free diagnostic.

`sediment server` owns a native PostgreSQL 17 process for local use; its private root retains the cluster across restarts. It provisions through the same role boundary before starting the API. An explicit `SEDIMENT_BOOTSTRAP_DATABASE_URL` keeps database lifecycle under the caller's control. See [Operator CLI](api-and-operations.md#operator-cli-clisediment_cliclipy).
Compose pins PostgreSQL 17 by digest. The one-shot `migrate` service waits for
database health and must finish before the API starts. CI applies the same
baseline before running tests. The API never creates or upgrades tables. Its
lifespan performs read-only inspection and accepts only the exact revision
supported by the build. Absent, behind, ahead, unreachable, and unauthorized
databases block startup.

## FactStore and runtime boundary

`FactStore` implements all Fact operations, atomic batches,
order-independent Sessions, quarantine, counts, health, and keyset Push reads.
Tests terminate owned workers before and after the real transaction commit. Before commit, Facts and Session upserts roll back together; after commit, replay retains stored identities and canonical payloads. These checks cover process interruption, not host power loss. Retry requires the sender to retain the original event; database deduplication cannot guarantee delivery of events never sent.
Writes redact first and use database-owned `ON CONFLICT` deduplication;
quarantine visibility follows the greatest `BIGINT IDENTITY` revision.
`read_snapshot()` owns one read-only `REPEATABLE READ` transaction and always
returns its pooled connection. Nested Derivations reuse it. The compatibility projection decodes only output messages in Python to extract typed tool-call IDs; PostgreSQL never parses opaque Fact content. Projections omit
unused serialized `TEXT`. Reports use `read_report_inference_calls` without input/raw; `iter_inference_calls_by_ids` buffers one full Fact row without caching; `read_session_inference_calls` loads complete Session input/output through its inclusive boundary.
SQL byte preflight refuses over 64 MiB per transferred content row, 256 MiB per Session input/output, or 256 MiB per materialized report/Attribution output population. Report projections default to 50,000 rows; explicit materializers retain their own memory cost. CI investigation reads use the exact run-identity index or a bounded `(org_id, repo, result, captured_at, outcome_id)` keyset. Push iteration keys on `(captured_at, push_id)`, never `OFFSET`; cursor order is not semantic.
Session dossiers read visible Fact metadata under one read-only, repeatable-read transaction and reject Sessions over the fixed cap. Evidence operations each own a snapshot and refuse inventories over 1,000 visible calls. SQL preflights an 8 MiB sum of selected variable-width columns, counting each row/column once, including metadata. Part reads select only requested message sides and omit raw payloads and user identity; [ADR 0021](../adr/0021-bounded-evidence-access.md) defines the separate wire limits.
Scoped reports and bundle v4 read complete organization-wide `InferenceCallIdentity` projections from that snapshot through `as_of`. Projected `observed_at` retains its instant. Output messages stream for Python alias extraction; input messages and raw payloads aren't selected. The 50,000-row cap and separate 64 MiB output-row ceiling don't bound total decoding cost or database pages. Commit investigation instead uses the native hash index over `ARRAY[org_id, call_id]` with exact equality rechecks. Its attachment-only reader retains at most two visible Fact witnesses per requested identifier and scopes metadata to selected Fact IDs; the 30,000-key budget bounds each filter. It reads no output content for attachment. Attribution retains separate content limits and broader scans. Complete bundle/report identity readers retain their existing population contract.

The bounded-read memory test uses a 64 × 1 MiB low-compressibility corpus.
Continuous integration caps projection memory growth at 16 MiB and requires
the full-row control to exceed 32 MiB. Run the test on the supported runtime
to verify those boundaries; a past measurement does not establish capacity.

PostgreSQL is required. `SEDIMENT_DATABASE_URL` is its sole active setting. Each process owns and disposes its engine. API worker engines use one connection and no overflow; API statement/lock/idle-transaction limits are 30/5/30 seconds. Migrations and local exports keep independent budgets. Multi-artifact Derivations reuse one read-only `REPEATABLE READ` snapshot.
Diagnostics sanitize secrets and driver details. Sediment exposes no backend selector, dual writes, or legacy import tooling.
