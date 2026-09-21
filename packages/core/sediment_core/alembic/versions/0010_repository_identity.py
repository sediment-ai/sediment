# SPDX-License-Identifier: AGPL-3.0-or-later
"""Preserve provider repository identity and immutable rename receipts.

Revision ID: 0010_repository_identity
Revises: 0009_descriptive_text_encoding

Every `ADD CONSTRAINT ... CHECK` here validates existing rows under an
`ACCESS EXCLUSIVE` lock (no `NOT VALID` split, matching every prior revision
in this directory). Every `CREATE UNIQUE INDEX` blocks concurrent writes for
the build (no `CONCURRENTLY`, which Alembic's single-transaction migration
run cannot use). On a deployment with substantial `ci_outcomes`,
`pushes`, `session_commit_observations`, `pull_request_merges`, or
`pull_request_revisions` history, this revision's roughly 90 statements
across those five tables can hold each table's write path for longer than a
schema-only change would. Run it in a maintenance window on a history-heavy
deployment; see "Upgrade the deployment" in `docs/operate/deploy.md`.
"""

from alembic import op

revision = "0010_repository_identity"
down_revision = "0009_descriptive_text_encoding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE ci_outcomes ADD COLUMN repository_provider TEXT")
    op.execute("ALTER TABLE ci_outcomes ADD COLUMN repository_host TEXT")
    op.execute("ALTER TABLE ci_outcomes ADD COLUMN repository_id TEXT")
    op.execute("ALTER TABLE ci_outcomes DROP CONSTRAINT ck_ci_outcomes_schema_version")
    op.execute(
        "ALTER TABLE ci_outcomes ADD CONSTRAINT ck_ci_outcomes_repository_complete CHECK ((repository_provider IS NULL AND repository_host IS NULL AND repository_id IS NULL) OR (repository_provider IS NOT NULL AND repository_host IS NOT NULL AND repository_id IS NOT NULL))"
    )
    op.execute(
        "ALTER TABLE ci_outcomes ADD CONSTRAINT ck_ci_outcomes_repository_host CHECK (length(repository_host) <= 253 AND repository_host ~ '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$')"
    )
    op.execute(
        "ALTER TABLE ci_outcomes ADD CONSTRAINT ck_ci_outcomes_repository_id CHECK (repository_id ~ '^[1-9][0-9]{0,19}$')"
    )
    op.execute(
        "ALTER TABLE ci_outcomes ADD CONSTRAINT ck_ci_outcomes_repository_legacy CHECK (schema_version = 2 OR repository_id IS NULL)"
    )
    op.execute(
        "ALTER TABLE ci_outcomes ADD CONSTRAINT ck_ci_outcomes_repository_provider CHECK (repository_provider IN ('github'))"
    )
    op.execute(
        "ALTER TABLE ci_outcomes ADD CONSTRAINT ck_ci_outcomes_schema_version CHECK (schema_version IN (1, 2))"
    )
    op.execute("DROP INDEX uq_ci_run")
    op.execute(
        "CREATE UNIQUE INDEX uq_ci_run ON ci_outcomes (org_id, provider, run_id, coalesce(run_attempt, 0)) WHERE repository_id IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_ci_run_identified ON ci_outcomes (org_id, repository_provider, repository_host, provider, run_id, coalesce(run_attempt, 0)) WHERE repository_id IS NOT NULL"
    )
    op.execute("ALTER TABLE pushes ADD COLUMN repository_provider TEXT")
    op.execute("ALTER TABLE pushes ADD COLUMN repository_host TEXT")
    op.execute("ALTER TABLE pushes ADD COLUMN repository_id TEXT")
    op.execute("ALTER TABLE pushes ADD COLUMN schema_version BIGINT NOT NULL DEFAULT 1")
    op.execute("ALTER TABLE pushes ALTER COLUMN schema_version DROP DEFAULT")
    op.execute(
        "ALTER TABLE pushes ADD CONSTRAINT ck_pushes_forge_agreement CHECK (repository_provider = provider)"
    )
    op.execute(
        "ALTER TABLE pushes ADD CONSTRAINT ck_pushes_repository_complete CHECK ((repository_provider IS NULL AND repository_host IS NULL AND repository_id IS NULL) OR (repository_provider IS NOT NULL AND repository_host IS NOT NULL AND repository_id IS NOT NULL))"
    )
    op.execute(
        "ALTER TABLE pushes ADD CONSTRAINT ck_pushes_repository_host CHECK (length(repository_host) <= 253 AND repository_host ~ '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$')"
    )
    op.execute(
        "ALTER TABLE pushes ADD CONSTRAINT ck_pushes_repository_id CHECK (repository_id ~ '^[1-9][0-9]{0,19}$')"
    )
    op.execute(
        "ALTER TABLE pushes ADD CONSTRAINT ck_pushes_repository_legacy CHECK (schema_version = 2 OR repository_id IS NULL)"
    )
    op.execute(
        "ALTER TABLE pushes ADD CONSTRAINT ck_pushes_repository_provider CHECK (repository_provider IN ('github'))"
    )
    op.execute(
        "ALTER TABLE pushes ADD CONSTRAINT ck_pushes_schema_version CHECK (schema_version IN (1, 2))"
    )
    op.execute("DROP INDEX uq_pushes_natural")
    op.execute(
        "CREATE UNIQUE INDEX uq_pushes_natural ON pushes (org_id, repo, ref, before_sha, after_sha) WHERE repository_id IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_pushes_natural_identified ON pushes (org_id, repository_provider, repository_host, repository_id, ref, before_sha, after_sha) WHERE repository_id IS NOT NULL"
    )
    op.execute(
        "ALTER TABLE session_commit_observations ADD COLUMN repository_provider TEXT"
    )
    op.execute(
        "ALTER TABLE session_commit_observations ADD COLUMN repository_host TEXT"
    )
    op.execute("ALTER TABLE session_commit_observations ADD COLUMN repository_id TEXT")
    op.execute(
        "ALTER TABLE session_commit_observations DROP CONSTRAINT ck_session_commit_observations_schema_version"
    )
    op.execute(
        "ALTER TABLE session_commit_observations ADD CONSTRAINT ck_session_commit_observations_repository_complete CHECK ((repository_provider IS NULL AND repository_host IS NULL AND repository_id IS NULL) OR (repository_provider IS NOT NULL AND repository_host IS NOT NULL AND repository_id IS NOT NULL))"
    )
    op.execute(
        "ALTER TABLE session_commit_observations ADD CONSTRAINT ck_session_commit_observations_repository_host CHECK (length(repository_host) <= 253 AND repository_host ~ '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$')"
    )
    op.execute(
        "ALTER TABLE session_commit_observations ADD CONSTRAINT ck_session_commit_observations_repository_id CHECK (repository_id ~ '^[1-9][0-9]{0,19}$')"
    )
    op.execute(
        "ALTER TABLE session_commit_observations ADD CONSTRAINT ck_session_commit_observations_repository_legacy CHECK (schema_version = 2 OR repository_id IS NULL)"
    )
    op.execute(
        "ALTER TABLE session_commit_observations ADD CONSTRAINT ck_session_commit_observations_repository_provider CHECK (repository_provider IN ('github'))"
    )
    op.execute(
        "ALTER TABLE session_commit_observations ADD CONSTRAINT ck_session_commit_observations_schema_version CHECK (schema_version IN (1, 2))"
    )
    op.execute("DROP INDEX uq_session_commit_observations_edge")
    op.execute(
        "CREATE UNIQUE INDEX uq_session_commit_observations_edge ON session_commit_observations (org_id, repo, commit_sha, session_id) WHERE repository_id IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_session_commit_observations_edge_identified ON session_commit_observations (org_id, repository_provider, repository_host, repository_id, commit_sha, session_id) WHERE repository_id IS NOT NULL"
    )
    op.execute("ALTER TABLE pull_request_merges ADD COLUMN repository_provider TEXT")
    op.execute("ALTER TABLE pull_request_merges ADD COLUMN repository_host TEXT")
    op.execute("ALTER TABLE pull_request_merges ADD COLUMN repository_id TEXT")
    op.execute(
        "ALTER TABLE pull_request_merges ADD COLUMN head_repository_provider TEXT"
    )
    op.execute("ALTER TABLE pull_request_merges ADD COLUMN head_repository_host TEXT")
    op.execute("ALTER TABLE pull_request_merges ADD COLUMN head_repository_id TEXT")
    op.execute(
        "ALTER TABLE pull_request_merges ADD COLUMN schema_version BIGINT NOT NULL DEFAULT 1"
    )
    op.execute(
        "ALTER TABLE pull_request_merges ALTER COLUMN schema_version DROP DEFAULT"
    )
    op.execute(
        "ALTER TABLE pull_request_merges ADD CONSTRAINT ck_pull_request_merges_forge_agreement CHECK (repository_provider = provider)"
    )
    op.execute(
        "ALTER TABLE pull_request_merges ADD CONSTRAINT ck_pull_request_merges_head_legacy CHECK (schema_version = 2 OR head_repository_id IS NULL)"
    )
    op.execute(
        "ALTER TABLE pull_request_merges ADD CONSTRAINT ck_pull_request_merges_head_provider CHECK (head_repository_provider = provider)"
    )
    op.execute(
        "ALTER TABLE pull_request_merges ADD CONSTRAINT ck_pull_request_merges_head_repository_complete CHECK ((head_repository_provider IS NULL AND head_repository_host IS NULL AND head_repository_id IS NULL) OR (head_repository_provider IS NOT NULL AND head_repository_host IS NOT NULL AND head_repository_id IS NOT NULL))"
    )
    op.execute(
        "ALTER TABLE pull_request_merges ADD CONSTRAINT ck_pull_request_merges_head_repository_host CHECK (length(head_repository_host) <= 253 AND head_repository_host ~ '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$')"
    )
    op.execute(
        "ALTER TABLE pull_request_merges ADD CONSTRAINT ck_pull_request_merges_head_repository_id CHECK (head_repository_id ~ '^[1-9][0-9]{0,19}$')"
    )
    op.execute(
        "ALTER TABLE pull_request_merges ADD CONSTRAINT ck_pull_request_merges_head_repository_provider CHECK (head_repository_provider IN ('github'))"
    )
    op.execute(
        "ALTER TABLE pull_request_merges ADD CONSTRAINT ck_pull_request_merges_repository_complete CHECK ((repository_provider IS NULL AND repository_host IS NULL AND repository_id IS NULL) OR (repository_provider IS NOT NULL AND repository_host IS NOT NULL AND repository_id IS NOT NULL))"
    )
    op.execute(
        "ALTER TABLE pull_request_merges ADD CONSTRAINT ck_pull_request_merges_repository_host CHECK (length(repository_host) <= 253 AND repository_host ~ '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$')"
    )
    op.execute(
        "ALTER TABLE pull_request_merges ADD CONSTRAINT ck_pull_request_merges_repository_id CHECK (repository_id ~ '^[1-9][0-9]{0,19}$')"
    )
    op.execute(
        "ALTER TABLE pull_request_merges ADD CONSTRAINT ck_pull_request_merges_repository_legacy CHECK (schema_version = 2 OR repository_id IS NULL)"
    )
    op.execute(
        "ALTER TABLE pull_request_merges ADD CONSTRAINT ck_pull_request_merges_repository_provider CHECK (repository_provider IN ('github'))"
    )
    op.execute(
        "ALTER TABLE pull_request_merges ADD CONSTRAINT ck_pull_request_merges_schema_version CHECK (schema_version IN (1, 2))"
    )
    op.execute("DROP INDEX uq_pull_request_merges_natural")
    op.execute(
        "CREATE UNIQUE INDEX uq_pull_request_merges_natural ON pull_request_merges (org_id, provider, repo, pr_number) WHERE repository_id IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_pull_request_merges_natural_identified ON pull_request_merges (org_id, repository_provider, repository_host, repository_id, pr_number) WHERE repository_id IS NOT NULL"
    )
    op.execute("ALTER TABLE pull_request_revisions ADD COLUMN repository_provider TEXT")
    op.execute("ALTER TABLE pull_request_revisions ADD COLUMN repository_host TEXT")
    op.execute("ALTER TABLE pull_request_revisions ADD COLUMN repository_id TEXT")
    op.execute(
        "ALTER TABLE pull_request_revisions ADD COLUMN head_repository_provider TEXT"
    )
    op.execute(
        "ALTER TABLE pull_request_revisions ADD COLUMN head_repository_host TEXT"
    )
    op.execute("ALTER TABLE pull_request_revisions ADD COLUMN head_repository_id TEXT")
    op.execute(
        "ALTER TABLE pull_request_revisions ADD COLUMN schema_version BIGINT NOT NULL DEFAULT 1"
    )
    op.execute(
        "ALTER TABLE pull_request_revisions ALTER COLUMN schema_version DROP DEFAULT"
    )
    op.execute(
        "ALTER TABLE pull_request_revisions ADD CONSTRAINT ck_pull_request_revisions_forge_agreement CHECK (repository_provider = provider)"
    )
    op.execute(
        "ALTER TABLE pull_request_revisions ADD CONSTRAINT ck_pull_request_revisions_head_legacy CHECK (schema_version = 2 OR head_repository_id IS NULL)"
    )
    op.execute(
        "ALTER TABLE pull_request_revisions ADD CONSTRAINT ck_pull_request_revisions_head_provider CHECK (head_repository_provider = provider)"
    )
    op.execute(
        "ALTER TABLE pull_request_revisions ADD CONSTRAINT ck_pull_request_revisions_head_repository_complete CHECK ((head_repository_provider IS NULL AND head_repository_host IS NULL AND head_repository_id IS NULL) OR (head_repository_provider IS NOT NULL AND head_repository_host IS NOT NULL AND head_repository_id IS NOT NULL))"
    )
    op.execute(
        "ALTER TABLE pull_request_revisions ADD CONSTRAINT ck_pull_request_revisions_head_repository_host CHECK (length(head_repository_host) <= 253 AND head_repository_host ~ '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$')"
    )
    op.execute(
        "ALTER TABLE pull_request_revisions ADD CONSTRAINT ck_pull_request_revisions_head_repository_id CHECK (head_repository_id ~ '^[1-9][0-9]{0,19}$')"
    )
    op.execute(
        "ALTER TABLE pull_request_revisions ADD CONSTRAINT ck_pull_request_revisions_head_repository_provider CHECK (head_repository_provider IN ('github'))"
    )
    op.execute(
        "ALTER TABLE pull_request_revisions ADD CONSTRAINT ck_pull_request_revisions_repository_complete CHECK ((repository_provider IS NULL AND repository_host IS NULL AND repository_id IS NULL) OR (repository_provider IS NOT NULL AND repository_host IS NOT NULL AND repository_id IS NOT NULL))"
    )
    op.execute(
        "ALTER TABLE pull_request_revisions ADD CONSTRAINT ck_pull_request_revisions_repository_host CHECK (length(repository_host) <= 253 AND repository_host ~ '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$')"
    )
    op.execute(
        "ALTER TABLE pull_request_revisions ADD CONSTRAINT ck_pull_request_revisions_repository_id CHECK (repository_id ~ '^[1-9][0-9]{0,19}$')"
    )
    op.execute(
        "ALTER TABLE pull_request_revisions ADD CONSTRAINT ck_pull_request_revisions_repository_legacy CHECK (schema_version = 2 OR repository_id IS NULL)"
    )
    op.execute(
        "ALTER TABLE pull_request_revisions ADD CONSTRAINT ck_pull_request_revisions_repository_provider CHECK (repository_provider IN ('github'))"
    )
    op.execute(
        "ALTER TABLE pull_request_revisions ADD CONSTRAINT ck_pull_request_revisions_schema_version CHECK (schema_version IN (1, 2))"
    )
    op.execute("DROP INDEX uq_pull_request_revisions_natural")
    op.execute(
        "CREATE UNIQUE INDEX uq_pull_request_revisions_natural ON pull_request_revisions (org_id, provider, repo, pr_number, head_sha, base_sha) WHERE repository_id IS NULL"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_pull_request_revisions_natural_identified ON pull_request_revisions (org_id, repository_provider, repository_host, repository_id, pr_number, head_sha, base_sha) WHERE repository_id IS NOT NULL"
    )
    op.execute(
        "CREATE TABLE repository_renames ( schema_version BIGINT NOT NULL, rename_id TEXT NOT NULL, org_id TEXT NOT NULL, repository_provider TEXT NOT NULL, repository_host TEXT NOT NULL, repository_id TEXT NOT NULL, old_repo TEXT NOT NULL, new_repo TEXT NOT NULL, source_event_id TEXT, occurred_at TIMESTAMP WITH TIME ZONE, captured_at TIMESTAMP WITH TIME ZONE NOT NULL, PRIMARY KEY (rename_id), CONSTRAINT ck_repository_renames_repository_complete CHECK ((repository_provider IS NULL AND repository_host IS NULL AND repository_id IS NULL) OR (repository_provider IS NOT NULL AND repository_host IS NOT NULL AND repository_id IS NOT NULL)), CONSTRAINT ck_repository_renames_repository_provider CHECK (repository_provider IN ('github')), CONSTRAINT ck_repository_renames_repository_host CHECK (length(repository_host) <= 253 AND repository_host ~ '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$'), CONSTRAINT ck_repository_renames_repository_id CHECK (repository_id ~ '^[1-9][0-9]{0,19}$'), CONSTRAINT ck_repository_renames_schema_version CHECK (schema_version = 1), CONSTRAINT ck_repository_renames_rename_id CHECK (rename_id <> '' AND rename_id = btrim(rename_id, E' \\t\\n\\r')), CONSTRAINT ck_repository_renames_org_id CHECK (org_id ~ '^[a-z0-9][a-z0-9._-]{0,63}$'), CONSTRAINT ck_repository_renames_old_repo CHECK (old_repo <> '' AND old_repo = '' OR (old_repo = lower(old_repo) AND old_repo ~ '^[^/]+/[^/]+$')), CONSTRAINT ck_repository_renames_new_repo CHECK (new_repo <> '' AND new_repo = '' OR (new_repo = lower(new_repo) AND new_repo ~ '^[^/]+/[^/]+$')), CONSTRAINT ck_repository_renames_distinct_names CHECK (old_repo <> new_repo), CONSTRAINT ck_repository_renames_source_event_id CHECK (source_event_id IS NULL OR (source_event_id <> '' AND source_event_id = btrim(source_event_id, E' \\t\\n\\r'))) )"
    )
    op.execute(
        "CREATE INDEX ix_repository_renames_as_of ON repository_renames (org_id, captured_at, rename_id)"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_repository_renames_delivery ON repository_renames (org_id, repository_provider, repository_host, source_event_id) WHERE source_event_id IS NOT NULL"
    )
    op.execute("ALTER TABLE fact_quarantine DROP CONSTRAINT ck_quarantine_table")
    op.execute(
        "ALTER TABLE fact_quarantine ADD CONSTRAINT ck_quarantine_table CHECK (fact_table IN ('inference_calls', 'developer_decisions', 'ci_outcomes', 'pushes', 'pull_request_merges', 'pull_request_revisions', 'edit_observations', 'rejected_edits', 'retry_linkages', 'session_commit_observations', 'repository_renames'))"
    )


def downgrade() -> None:
    raise NotImplementedError("repository identity migration is forward-only")
