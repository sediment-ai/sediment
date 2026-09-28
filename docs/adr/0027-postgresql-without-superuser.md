# ADR 0027 — Run on PostgreSQL without a superuser

Status: proposed ([#185](https://github.com/sediment-ai/sediment/issues/185)).
The maintainer resolved decisions 1–4 on 2026-09-28; decisions 5–7 carry
recommendations pending confirmation. When accepted, it amends
[ADR 0012](0012-postgresql-fact-store.md)'s migration and startup contract.
Nothing described here is implemented.

## Context

Sediment provisions one dedicated database through
`postgres_roles.py::provision_database`. That function refuses any connection
whose role lacks `rolsuper`, then, as that superuser:

1. Creates or reconciles the fixed, cluster-wide roles `sediment_migrator`,
   `sediment_runtime`, and `sediment_operator` with
   `ALTER ROLE … LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT
   NOREPLICATION NOBYPASSRLS PASSWORD …`, and revokes every membership each
   role holds.
2. Revokes database and `public` schema access from `PUBLIC`, and grants
   `CONNECT`, `USAGE`, and the migrator's `CREATE`.
3. Transfers every known table to `sediment_migrator` with
   `ALTER TABLE … OWNER TO`, including tables the migrator already owns.
4. Runs Alembic as the migrator.
5. Revokes and grants table, column, and sequence privileges on
   migrator-owned objects.
6. Validates the runtime and operator roles with
   `_validate_privileges`.

`sediment server` with `SEDIMENT_BOOTSTRAP_DATABASE_URL` repeats this on every
start, so an external deployment keeps a superuser credential in the server's
environment. The API validates the revision and `sediment_runtime`'s effective
privileges at startup in production mode. Facts stay append-only through grants
alone: the runtime role holds `SELECT` and `INSERT`, plus `UPDATE` on four
`sessions` columns, and no role other than the owner can change a Fact.

Managed PostgreSQL services such as Amazon RDS, Cloud SQL, and Azure Database
for PostgreSQL don't grant `SUPERUSER`. Their documentation describes an
administrator role with `CREATEROLE` and `CREATEDB` that owns the databases it
creates; no Sediment test has verified a provider yet. Sediment requires
PostgreSQL 16 or later, where these rules apply:

- Only a superuser can name the `SUPERUSER` attribute in `ALTER ROLE`. Only a
  role that holds `REPLICATION` or `BYPASSRLS` can name those attributes.
- A `CREATEROLE` role can alter a role, including its password, only with the
  `ADMIN` option on it. Creating a role grants the creator `ADMIN`, but not
  `INHERIT` or `SET`, while `createrole_self_grant` is empty (the default).
- Revoking a membership requires the `ADMIN` option on the parent role.
- Changing a table's owner requires the owner's privileges; granting on a table
  requires ownership or a grant option.
- Any role can change its own password.

Issue #185 records probes on a disposable PostgreSQL 17.11 cluster with a
`LOGIN CREATEROLE CREATEDB` administrator that owns the database. That
administrator fails at the `rolsuper` gate, at the attribute clauses in step 1,
at the ownership transfer in step 3 once tables exist, and at the grants in
step 5. It also can't alter a role that another administrator created or revoke
a membership that it didn't grant. It can create the roles without the three
superuser-only clauses and complete step 2. The migrator can then migrate and
apply every grant from step 5 as the owner, and the runtime and operator
validators pass. A `pg_restore --no-owner --no-privileges` run as the migrator
leaves the migrator owning every restored object. `sediment_operator` can't run
`pg_dump` because it lacks `SELECT` on the quarantine identity sequence.

Removing the `rolsuper` check alone therefore fails at the next statement. The
design problem is ownership: an administrator that doesn't own Sediment's tables
can't manage their privileges, and Sediment's own migrator already can.

## Decision (proposed)

### Separate provisioning, migration, and startup

One privileged operation becomes three steps with three credentials:

| Step | Credential | Does | Runs |
|---|---|---|---|
| Provision | An administrator: superuser, or `CREATEROLE` plus ownership of the dedicated database | Creates or reconciles the three roles, database `CONNECT`, and `public` schema `USAGE` and migrator `CREATE`. Never touches tables. | Once per installation, and to rotate passwords in the Sediment-managed mode |
| Migrate | The migrator role | Takes the advisory lock, runs Alembic, verifies columns, applies every table, column, and sequence grant as the owner, then validates all three roles | Every `sediment server` start, and `sediment db upgrade` where a deployment runs migration as a separate job |
| Serve | The runtime role | Unchanged: revision and effective-privilege validation, then serve | Every start, after the migrate step |

The migrator owns every Sediment object, and privileges on those objects come
only from the owner. The administrator never owns a Sediment table and never
issues a table grant. An existing superuser deployment keeps working: a
superuser is a valid administrator, and the migrator applies the same grants
that the bootstrap role applies today.

Against an external database, `sediment server` stops provisioning at start
and migrates instead, as `coder server` does. It runs the migrate step with the
migrator credential, removes that credential from its environment, and serves
with the runtime credential, the way `_run_server` removes the bootstrap
credential today. A replica that finds the migration lock held waits a bounded
time and then continues when the schema is at head; today's
`pg_try_advisory_lock` fails immediately instead. The administrator credential
never reaches the server. The local managed cluster, where Sediment owns a
private PostgreSQL instance as its superuser, keeps its behavior.

Like Coder, Sediment doesn't fix role names. The operator names the three roles
through configuration, and the defaults `sediment_migrator`,
`sediment_runtime`, and `sediment_operator` apply only when Sediment provisions
them. Each validator checks the configured names. Two deployments with their
own databases and roles can therefore share one PostgreSQL instance without
sharing credentials; today's fixed names make the second provisioning rotate the
first deployment's passwords.

### Two supported setup modes

**Sediment-managed provisioning.** The operator gives the provision step a
non-superuser administrator that owns a dedicated, empty database.
Provisioning replaces the `rolsuper` gate with capability checks and fails
before its first change when any check fails:

- the administrator is a superuser, or has `CREATEROLE` and the privileges of
  the database owner;
- each Sediment role either doesn't exist or grants the administrator `ADMIN`;
- each Sediment role holds no membership that the administrator can't revoke.

It sets only the attributes that the administrator is allowed to name. It reads
`rolsuper`, `rolreplication`, and `rolbypassrls` and requires each to be false
instead of setting them.

**Administrator-provisioned roles.** A database administrator creates the three
roles and the dedicated database, and Sediment never receives an administrator
credential. Sediment publishes the required state as SQL, and the migrate step
verifies it. This mode also covers organizations whose policy forbids
applications from creating roles.

### Required state and effective-permission checks

| Subject | Required state |
|---|---|
| Dedicated database | Owned by the administrator. `PUBLIC` holds no `CONNECT`, `CREATE`, or `TEMP`. The three roles hold `CONNECT`. Contains no objects outside Sediment's schema that the runtime or operator role can reach. |
| `public` schema | `PUBLIC` holds no privileges. The three roles hold `USAGE`; only the migrator role holds `CREATE`. |
| Migrator role | `LOGIN`; no `SUPERUSER`, `CREATEDB`, `CREATEROLE`, `REPLICATION`, or `BYPASSRLS`; no memberships; owns every Sediment object, or no Sediment object exists yet. |
| Runtime role | The existing `_validate_privileges` policy, unchanged: `SELECT` and `INSERT` on Fact tables, `sessions`, and `inference_call_aliases`; `UPDATE` on the four `sessions` columns; no memberships, ownership, elevated attributes, grant options, or reachable functions. |
| Operator role | The existing policy, unchanged: `SELECT` on every table, `INSERT` on `fact_quarantine`, and `USAGE` on its identity sequence. |

The migrator check is new; today no step validates the migrator. The runtime and
operator checks don't change, so managed support can't weaken the
append-only boundary. The effective-permission queries use `pg_roles`,
`pg_auth_members`, `pg_has_role`, and the `has_*_privilege` functions, which
need no special privilege.

Each failed check raises `DatabasePrivilegeError` with a credential-free
diagnostic that names the subject, the missing or excess privilege, and the
statement that an administrator runs to correct it. A read-only check command
reports every failed check at once instead of stopping at the first.

### Configuration and credentials

| Setting | Holder | Lifetime |
|---|---|---|
| `SEDIMENT_BOOTSTRAP_DATABASE_URL` | Provision step, Sediment-managed mode only | One-shot; never stored in the server environment for an external database |
| `SEDIMENT_MIGRATOR_DATABASE_URL` | `sediment server` at start, and `sediment db upgrade` | Read at start and removed from the environment before the API serves |
| `SEDIMENT_DATABASE_URL` | The API | Every start |
| `SEDIMENT_MIGRATOR_ROLE`, `SEDIMENT_RUNTIME_ROLE`, `SEDIMENT_OPERATOR_ROLE` | Provisioning, migration, and validation | Role names only, defaulting to `sediment_*`; each URL's user must match its role |
| `SEDIMENT_MIGRATOR_PASSWORD`, `SEDIMENT_RUNTIME_PASSWORD`, `SEDIMENT_OPERATOR_PASSWORD` | Provision step, Sediment-managed mode only | Unchanged, including client-side SCRAM verifiers |

`_run_server` already strips `SEDIMENT_MIGRATOR_DATABASE_URL` and
`SEDIMENT_OPERATOR_DATABASE_URL`, but nothing reads them; this design gives the
migrator URL its meaning. Password authentication is the only supported method;
IAM database authentication needs a provider membership, such as `rds_iam`,
that the validators reject. Provisioning keeps its existing query allowlist, which
permits the TLS options that managed services require. Unix-socket URLs, such
as Cloud SQL's `/cloudsql/` host, stay unsupported because provisioning requires
an explicit host.

### Lifecycle

- **Fresh installation.** Provision (or apply the administrator's SQL), then
  start; the start migrates.
- **Upgrade.** Stop Sediment, install the new version, then start; the start
  migrates and grants any new table. A deployment that runs migration as a
  separate job runs `sediment db upgrade` first. An API started against an
  unmigrated schema still refuses to serve, because the existing runtime
  validator rejects missing grant coverage.
- **Rotation.** In the Sediment-managed mode, rerun provisioning with new
  passwords; the administrator holds `ADMIN` on roles that it created. Each
  role can also rotate its own password, which covers the
  administrator-provisioned mode without an administrator credential. The
  administrator's own credential belongs to the managed service.
- **Backup.** Provider snapshots and point-in-time recovery are the primary
  backup for a managed service. A logical backup runs `pg_dump` as the
  migrator, which can read every object, including the identity sequence.
- **Restore.** Prepare an empty database with the provision step or the
  administrator's SQL, run `pg_restore --no-owner --no-privileges` as the
  migrator, then start or run `sediment db upgrade`. The restored objects belong to the migrator, so
  restoration doesn't need `_transfer_known_tables`. That adoption path stays
  for superuser provisioning of databases that predate the role model.

### Qualification before compatibility claims

The first qualification targets Amazon RDS for PostgreSQL 17.
Documentation claims support for one managed service only after the acceptance
tests in this ADR pass against a real instance of it, with its default
parameter group. The qualification record names the provider, the PostgreSQL
version, the administrator's memberships and attributes, the TLS settings, and
the backup and restore path exercised. Other services remain unqualified until
they have their own record. The disposable-cluster tests prove PostgreSQL's
rules; they don't prove a provider's patches or defaults.

## Alternatives considered

### Remove the `rolsuper` check

Rejected. The probes in #185 show that the next statement fails, and that each
later step needs ownership that the administrator doesn't have.

### Let the administrator inherit the migrator

`GRANT sediment_migrator TO <administrator> WITH INHERIT TRUE, SET TRUE` makes
the existing code work unchanged on PostgreSQL 17; #185 records that probe.
Rejected as the default: every provisioning run would hold the migrator's
object authority through a second credential, and table grants would stay on
the administrator's path. It remains a candidate for adopting a legacy,
administrator-owned database.

### Default privileges for future tables

Rejected. `ALTER DEFAULT PRIVILEGES FOR ROLE sediment_migrator` would grant any
future table automatically, including one created by mistake. The validator's
exact table coverage depends on explicit per-table grants.

### Run with one owner credential, as Coder does

`coder server` connects with one role that owns its schema. Rejected for
Sediment: append-only Facts rely on a runtime role that doesn't own the tables.
Sediment adopts Coder's migration at start and operator-named roles, not its
single credential.

## Consequences

- ADR 0012's migration and startup section would name the migrate step, which
  applies grants and validates roles, instead of `sediment db upgrade` alone.
  Its test contract gains a non-superuser administrator fixture.
- `docs/operate/deploy.md` replaces the superuser requirement with the two
  modes. `docs/operate/maintain.md` updates rotation, backup, and restore.
- Configured role names let several deployments share a PostgreSQL instance.
  A restore test that reuses the live role names on the same instance still
  grants those roles `CONNECT` on the copy; use distinct names for a copy.
- An external deployment no longer keeps an administrator credential. It keeps
  the migrator credential in its private configuration, and the running API
  never receives it.

## Decisions

Resolved by the maintainer on 2026-09-28:

1. Qualify Amazon RDS for PostgreSQL 17 first.
2. `sediment server` migrates at start, following coder/coder: `coder server`
   runs its migrations on every start under a PostgreSQL advisory lock, and
   concurrent replicas wait up to two minutes. Sediment keeps its separate
   credentials; only the startup migration uses the migrator credential.
3. Role names follow coder/coder too: the operator names the roles, and Sediment
   applies its defaults only when it provisions them.
4. No provider memberships in the first release, so no `rds_iam`: password
   authentication only.

Recommended, pending maintainer confirmation:

5. Decision: extend `sediment db upgrade`, which ADR 0012 already names. After
   Alembic it applies the owner's grants, verifies columns, and validates all
   three roles; `sediment server` calls the same function at start, so both
   paths share one implementation. Add one read-only `sediment db check` that
   reports every failed check with its fixing statement. Alternative: a new
   migrate command, which would leave `db upgrade` as a second, weaker path.
6. Decision: generate the administrator's SQL. `sediment db provision
   --print-sql` prints it without a connection, for the configured role names
   and database, from the same policy the validators enforce. A test applies
   that output as a non-superuser administrator and requires `db check` to pass,
   so the SQL can't drift. Alternative: static SQL in `docs/operate/deploy.md`,
   which no check keeps current.
7. Decision: fail before any change when an existing Sediment role grants the
   administrator no `ADMIN` option. The diagnostic names the role and both
   fixes: grant `ADMIN` on it to this administrator, or switch to the
   administrator-provisioned mode with that role's credentials. Alternative:
   skip its password reconciliation, which would store a password that the role
   doesn't have and fail later at login with a less specific error.

## Implementation sequence

1. Add the read-only check: migrator validation, provisioning preconditions,
   and the existing runtime and operator validators, reporting every failure.
   Nothing else changes.
2. Extend `sediment db upgrade` to run grants, column verification, and
   validation as the migrator after each migration; provisioning calls the
   same function. Existing `cluster_roles` tests keep passing unchanged.
3. Replace the `rolsuper` gate with the capability checks, and name only the
   attributes that the administrator may set.
4. Migrate at `sediment server` start with a bounded lock wait, then serve
   with the runtime credential. Accept configured role names, and add
   `sediment db provision --print-sql`.
5. Update deployment, rotation, backup, and restore procedures and the
   changelog.
6. Qualify the chosen service, then state the compatibility claim.

Each step lands as its own pull request with its tests.

## Acceptance tests

Both modes run in the `cluster_roles` pass against disposable clusters, with an
administrator fixture that has `LOGIN CREATEROLE CREATEDB` and owns the
database but isn't a superuser.

Sediment-managed provisioning:

- Fresh provision, migrate, and production startup succeed. The runtime role
  stores every Fact type. `UPDATE`, `DELETE`, `TRUNCATE`, object creation, and
  `SET ROLE` fail for runtime and operator, as in
  `test_fact_mutation_and_administration_fail`.
- Repeating provisioning preserves Facts and rotates passwords.
- A synthetic next migration that adds a table leaves the API refusing to start
  until the migrate step grants it.
- A Sediment role created by another administrator, a membership that the
  administrator can't revoke, and a database that the administrator doesn't own
  each fail before any change, with a diagnostic that names the fix.
- A superuser administrator passes every existing `cluster_roles` test
  unchanged.

Administrator-provisioned roles:

- The published SQL, applied by the administrator fixture, passes the check;
  migrate and production startup succeed with the same append-only assertions.
- Removing each required state one at a time makes the check and the migrate
  step fail, naming it: missing `CONNECT`, missing `CREATE` on `public`,
  `PUBLIC` `TEMP`, a runtime membership, a migrator with `CREATEDB`, and a
  Sediment table owned by the administrator.
- Each role rotates its own password, and startup succeeds with the new
  runtime password.

Both modes:

- Two `sediment server` replicas start together against an unmigrated
  database: one migrates, the other waits on the lock, and both serve with
  the runtime credential only. A lock held past the bound fails startup with a
  diagnostic.
- Two deployments with distinct role names and databases share one instance:
  provisioning the second leaves the first deployment's passwords unchanged,
  and neither runtime role can read the other's tables.
- A runtime role holding a provider membership, such as a stand-in for
  `rds_iam`, fails the check with a diagnostic that names the membership.
- `pg_dump` as the migrator, then `pg_restore --no-owner --no-privileges` as
  the migrator into a newly provisioned database, then migrate, reproduces the
  same `sediment facts` counts, quarantine log, and bundle hash.
- A qualification run on the chosen managed service repeats the provisioning,
  restore, and startup tests and records the provider details listed in
  [Qualification before compatibility claims](#qualification-before-compatibility-claims).
