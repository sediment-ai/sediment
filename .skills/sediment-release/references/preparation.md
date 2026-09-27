# Preparation and verification

Run from the isolated release worktree with the repository's Python 3.12 and uv
versions. Put logs and artifacts outside the checkout.

## Version surfaces

| Surface | Action |
| --- | --- |
| Root, CLI, API, and four package `pyproject.toml` files | Update seven project versions and all exact `sediment-*` pins. The root project isn't published. |
| `apps/api/sediment_api/__init__.py` | Update `__version__`. |
| `uv.lock` | Run `uv lock`, then `uv sync --locked`. Inspect for unintended dependency changes. |
| `apps/api/tests/test_v1.py` | Update the release-specific response assertion if it remains literal. |
| `openapi.yaml` and API reference | Run `dump_openapi.py` and `gen_api_docs.py`; don't hand-edit generated files. |
| CLI help golden | Run `uv run pytest -q cli/tests/test_cli_help.py --update-goldens`; inspect the diff. |
| `CHANGELOG.md` | Insert `## VERSION — YYYY-MM-DD` after an empty `## Unreleased`. Preserve release entries. |
| Operator docs | Update installation pins and health examples. Remove release caveats only when the source satisfies them. |
| `README.md` PyPI badge | Keep the dynamic `https://img.shields.io/pypi/v/sediment-cli` image linked to `https://pypi.org/project/sediment-cli/`. Correct stale static badges or incorrect package links. Verify the displayed version after publication as described in [Publication and recovery](publication.md#verify-the-readme-badge). |
| `security/maintenance.json` | Append the version to six `pypi/sediment-*` entries. Preserve historical versions and expiry. |

Search tracked files for the old version. CDEvents protocol values, historical
changelog entries, and arbitrary installer-test versions aren't release values.

## Security review bindings

`scripts/ci_preflight.py reviews` checks dispositions against
`security_image_assurance.review_digest`. Read its live input set.
`source_digest` is a separate identity for image provenance.

If a version-only lockfile edit stales the review digest:

1. Parse base and candidate lockfiles. Normalize only the seven first-party
   version values in a copy and assert complete equality with the base.
   Inspect every other changed input included in `review_digest`.
2. Reconstruct the base digest from the same input set. Verify every retained
   record matches it; inspect its predicates and expiry. Mismatched records,
   expired reviews, changed dependencies, or changed image inputs require
   substantive review and fresh evidence.
3. If only verified release labels changed, bind existing decisions to the
   candidate digest. Preserve targets, reasons, predicates, dates, and expiry.
   Explain that comparison in the pull request.
4. Run `ci_preflight.py reviews` again and require fresh hosted artifact scans.
   A valid binding alone doesn't establish that an image passes security policy.

## Checks

Use disposable PostgreSQL 17 with an administrative connection that can create
and drop scratch databases. Set `SEDIMENT_TEST_DATABASE_URL` to that connection.
Don't read deployment credentials. On macOS the managed PostgreSQL binaries
can run a separate `initdb` cluster on an unused loopback port. Stop only that
owned cluster after validation.

```sh
uv run ruff check .
uv run ruff format --check .
uv run python scripts/add_spdx.py --check
uv run python scripts/check_docs.py --base origin/main
uv run python scripts/dump_openapi.py --check
uv run python scripts/gen_cli_docs.py --check
uv run python scripts/gen_api_docs.py --check
uv run python scripts/gen_schema_docs.py --check --compatibility-base origin/main
uv run python scripts/gen_compatibility_docs.py --check
uv run python scripts/ci_preflight.py reviews
uv run python scripts/check_commit_msg.py --title "chore(deps): release version $RELEASE_VERSION"
git diff --check
```

Focused coverage includes `apps/api/tests/test_v1.py`,
`cli/tests/test_cli_help.py`, and the release-rehearsal, security-policy, and
retained-release-security suites under `scripts/tests/`. Include documentation
contract tests when changing guides. Don't add tests that only mirror version edits.

Run the complete rehearsal with an absent or empty output directory:

```sh
uv run python scripts/release_rehearsal.py \
  --tag "v$RELEASE_VERSION" --out-dir "$RELEASE_ARTIFACTS"
```

It validates six wheels and six source distributions, rebuilds wheels from
source archives, installs outside the checkout, and exercises the synthetic
pipeline in a scratch database. Retain the `pipeline acceptance:` JSON and
final pass message. `--tag` validates version equality; it doesn't require
creating the Git tag before merge.

Full repository tests, installed pi-helper coverage, native platform checks,
and artifact security use the maintained workflows. Record local and hosted
results separately. Synthetic acceptance doesn't verify live agents, model
quality, public HTTPS, or a production deployment.
