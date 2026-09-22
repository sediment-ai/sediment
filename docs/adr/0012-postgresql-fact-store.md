# ADR 0012 — PostgreSQL is the sole fact store

Status: accepted

Preserves [ADR 0003](0003-database-enforced-fact-identity.md)'s database-enforced identity and
transaction contract and [ADR 0006](0006-open-core-boundary.md)'s open-core boundary.

## Context

One Fact store must serve open-source and enterprise deployments. Multiple
backends would need to agree on natural-key deduplication, atomic batches,
order-independent Session upserts, quarantine revision ordering, transaction
snapshots, and every schema revision. Backend parity would create a permanent
test and maintenance obligation around Sediment's evidence boundary.

## Decision

PostgreSQL is the sole fact store for open-source and enterprise deployments.
Local development, tests, continuous integration, and deployed API replicas
all use PostgreSQL. No alternate runtime backend is supported.

The storage stack is:

- SQLAlchemy Core for physical tables, constraints, indexes, SQL expressions,
  transactions, and pooled connections;
- Alembic for explicit, forward-only physical schema revisions; and
- psycopg 3 as the PostgreSQL driver.

`FactStore` remains the domain-facing seam. Callers use fact models and store
methods; they do not receive SQLAlchemy rows, connections, or engines.
Pydantic fact models remain the canonical fact shapes. Sediment does not add
ORM entities or make SQLAlchemy models a second domain model.

The store remains synchronous. An asynchronous store would force asynchronous
behavior through batch derivations, reports, scripts, and the operator CLI
without improving their workload. API replicas run blocking store operations
in worker threads or synchronous FastAPI route boundaries. A request handler
never performs PostgreSQL network I/O on the asynchronous event-loop thread.

### Physical contract

The PostgreSQL schema preserves domain semantics:

- Serialized `TEXT` stores content-bearing structures and Basic-redacted raw
  payloads. Pydantic validates and decodes these fields at the storage seam.
  PostgreSQL does not parse or query inside them.
- `TIMESTAMPTZ` stores aware timestamps.
- Partial and expression-based `UNIQUE` indexes enforce natural fact identity.
- `INSERT ... ON CONFLICT DO NOTHING` makes a duplicate fact a successful
  idempotent write.
- Batch fact writes and quarantine operations remain atomic.
- Session upserts remain order-independent.
- A `BIGINT GENERATED ALWAYS AS IDENTITY` column supplies the monotonic
  quarantine revision.
- A read-only, repeatable-read transaction supplies one consistent fact
  snapshot to a derivation run.
- Basic redaction runs before any fact reaches a database statement.

Facts remain append-only. Only Alembic changes the physical schema. A schema
revision never changes the meaning of an existing fact contract version.
If a future query needs a value that exists only inside `raw`, a new fact
schema version promotes that value to a validated typed column. The database
does not reinterpret an opaque provider payload as a query contract.

[ADR 0023](0023-indexed-call-identifiers.md) permits the storage seam to copy
already-validated provider and typed output tool-call identifiers into a physical
lookup table. PostgreSQL indexes those scalar copies without parsing content.

### Migrations and startup

Deployments run `sediment db upgrade` before starting API replicas. The
command acquires a PostgreSQL advisory lock and runs Alembic to the supported
head revision. Repeating the command at head succeeds without changing the
database.

`sediment db upgrade` upgrades a PostgreSQL schema only. Sediment does not ship
a SQLite-to-PostgreSQL migration command, SQLite importer, or SQLite migration
guide.

The API verifies connectivity and the Alembic revision during startup. It
refuses to serve if the schema is absent, behind, or ahead. The API never
creates or upgrades tables.

The Docker Compose deployment contains PostgreSQL and a one-shot migration
service. API startup depends on successful migration completion. Git mirrors
remain on their separate filesystem volume.

`SEDIMENT_DATABASE_URL` selects the database. Direct-store commands may
accept `--database-url`; operator documentation prefers the environment
variable so credentials do not enter shell history.

### Failure behavior

The migration command exits with a concise diagnostic when connection,
permission, advisory-lock, or revision work fails. A failed migration prevents
API startup.

The store rolls back failed transactions and does not hide database failures
behind internal retries. If PostgreSQL becomes unavailable after startup,
ingest routes return 503 so senders can retry. Authentication and request
validation retain their existing public status contracts.

### Test contract

Every store test uses PostgreSQL. Continuous integration starts a PostgreSQL
service and upgrades it before pytest. Local development uses the Docker
Compose service.

Each pytest worker receives an isolated migrated database. Tests clear tables
and restart identity sequences between cases. Determinism and concurrency
tests can request additional isolated databases. No SQLite or in-memory store
substitute remains.

Tests cover migration idempotency and revision mismatch, every natural-key
conflict, atomic batch rollback, session upserts, quarantine revisions,
concurrent writes, redaction-before-write, and repeatable-read snapshots.

## Rejected alternatives

### Keep SQLite for local development and tests

Rejected. It would leave production-only SQL, constraints, transactions, and
connection behavior untested in the default workflow.

### Support SQLite and PostgreSQL behind `FactStore`

Rejected. The seam isolates domain callers; it does not justify two physical
contracts. Backend parity would make changes to fact identity and quarantine
semantics riskier.

### Use direct psycopg with a custom migration registry

Rejected. It would preserve the smallest initial code change by rebuilding
revision discovery, migration bookkeeping, and operational tooling.

### Store content-bearing fact fields as `JSONB`

Rejected. PostgreSQL's [`jsonb` input
rules](https://www.postgresql.org/docs/current/datatype-json.html) reject
values that canonical facts can preserve, including `\u0000`, invalid Unicode
surrogate pairs, non-finite numbers, and numbers outside PostgreSQL `numeric`.
Rejecting the row would lose a fact. Replacing or coercing the value would
change what happened. Sediment does not need database-side queries over these
fields, so serialized `TEXT` preserves the stronger contract.

### Introduce an ORM

Rejected. Facts already have canonical Pydantic models. ORM entities would add
a second domain representation without improving fact-store behavior.

### Rewrite the store as asynchronous

Rejected. Most consumers are synchronous batch derivations and operator
commands. The wider rewrite does not serve the storage decision.

## Consequences

- Open-source and enterprise deployments share one physical fact contract.
- PostgreSQL becomes a required development, test, and deployment dependency.
- A source checkout no longer runs the complete pipeline without a PostgreSQL
  service.
- Alembic revisions make database evolution explicit.
- SQLAlchemy Core and psycopg add dependencies but remove bespoke connection,
  DDL, and migration machinery.
- Content-bearing fact fields do not support PostgreSQL JSON operators or GIN
  indexes. Typed fact columns own every supported database query.
- PostgreSQL access belongs in the open core.
