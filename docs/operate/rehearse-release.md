# Rehearse a release

Use the release rehearsal to build and exercise all six distributions without
publishing them. Each distribution produces one wheel and one source
distribution. Maintainers run it before creating a version tag.

Installing an approved release doesn't require a local rehearsal. Review its
release evidence, then [verify the deployment](deploy.md#5-verify-the-deployment)
and [test live capture](run-pilot.md#verify-an-enrolled-harness).

The opt-in [container tests](../../scripts/tests/test_container_images.py) check
built Docker images. The [security workflow](../../.github/workflows/security.yml)
runs the gateway cases against its built images. These checks validate builds;
customer installation uses the deployment checks.

## Prerequisites

Install Python 3.12 and uv. Start PostgreSQL 17 with an administrative role that
can create and drop databases. Set its administrative URL:

```bash
export SEDIMENT_TEST_DATABASE_URL='postgresql+psycopg://postgres:postgres@localhost:5432/postgres'
```

The rehearsal creates one randomly named `sediment_rehearsal_*` database and
runs migrations and synthetic capture there. It drops that exact database when
the run ends, including failed checks. It refuses to proceed if creation fails.
It doesn't migrate or insert the corpus into the database named in your URL.
The scratch URL removes an administrative `dbname` query override so the
driver selects the owned database.
Use a disposable PostgreSQL instance so rehearsal load doesn't affect a deployment.

## Run the no-publish rehearsal

From the repository root, run:

```bash
uv run python scripts/release_rehearsal.py
```

The rehearsal verifies:

| Area | Checks |
| --- | --- |
| Source and packages | Synchronized versions, exact first-party dependencies, pinned Actions, six wheels, six source distributions, metadata, licenses, and required content |
| Installation | Wheels rebuilt from source archives; original wheels installed outside the checkout; CLI, migration, Fact reads, and transcript hooks |
| Capture and replay | Synthetic gateway, transcript, and OTLP inputs; receiver outages; lost acknowledgments; retained bytes and Fact IDs after restart |
| Repository evidence | Real Git patches and notes, Push capture, Session-to-commit observations, Repository rename, and CI identity |
| Reports and exports | Authenticated reads, bundle v4 roundtrip, invalid-bundle refusal, quarantine/release, SFT and RLVR rows, and counted exclusions |
| Transcript batching | 32 observations of a 256 KiB file in two bounded requests; original bytes survive an outage, source changes, and transcript removal |

The [rehearsal implementation](../../scripts/release_rehearsal.py) defines the
complete acceptance checks.

The command prints a pass message only after every check succeeds. It has no
publication step and doesn't use PyPI credentials.

The installed pipeline has a 180-second deadline. On timeout, Ctrl+C, or worker
exit, the parent stops that worker's process group before cleaning its database
and workspace. Cleanup sends `SIGTERM`, then `SIGKILL` to remaining descendants,
with bounded waits. A timeout or interruption reports a failed rehearsal.

Retain the `pipeline acceptance:` JSON line. It records versions, hashes,
delivery results, policy and mirror revisions, Fact and row identities, and
stage counts. Records, Facts, Segments, and training rows use different units.
The later `transcript_batch` population has separate counts.

Repeated runs with unchanged Facts, policy, mirror refs, and quarantine revision
must produce identical bundle and training bytes. Quarantine and release change
Provenance, so bytes across those revisions differ.

The rehearsal uses synthetic inputs. Live harness, gateway, forge, and model
behavior require separate verification. pi's installed-helper check runs in the
`shims` workflow. A passing rehearsal doesn't establish training quality.

## Record a revision-bound rehearsal

If you need an acceptance record for a source revision, use the disposable
PostgreSQL instance from [Prerequisites](#prerequisites). From a clean checkout
of the approved revision, run:

```bash
SEDIMENT_REVISION='<approved full commit hash>'
export SEDIMENT_TEST_DATABASE_URL='postgresql+psycopg://postgres:postgres@localhost:5432/postgres'
ACCEPTANCE_DIR="$HOME/sediment-acceptance"
ACCEPTANCE_LOG="$ACCEPTANCE_DIR/pipeline-acceptance-$SEDIMENT_REVISION.log"
ACCEPTANCE_TMP="$ACCEPTANCE_LOG.tmp"

mkdir -p "$ACCEPTANCE_DIR"
rm -f "$ACCEPTANCE_TMP"
if {
  test "${#SEDIMENT_REVISION}" -eq 40 &&
  test "$(git rev-parse HEAD)" = "$SEDIMENT_REVISION" &&
  test -z "$(git status --porcelain=v1 --untracked-files=all)" &&
  printf 'sediment_revision=%s\nworktree=clean\n' "$SEDIMENT_REVISION" &&
  uv run python scripts/release_rehearsal.py
} >"$ACCEPTANCE_TMP" 2>&1
then
  mv "$ACCEPTANCE_TMP" "$ACCEPTANCE_LOG"
else
  cat "$ACCEPTANCE_TMP"
  rm -f "$ACCEPTANCE_TMP"
  exit 1
fi
cat "$ACCEPTANCE_LOG"
```

Retain the pass message, `pipeline acceptance:` record, revision, and
`worktree=clean` line. This synthetic rehearsal doesn't replace live harness,
gateway, or forge verification.

## Rehearse a tag build

To verify a proposed tag and preserve the validated artifacts, pass the tag and
an empty output directory:

```bash
uv run python scripts/release_rehearsal.py \
  --tag v0.1.0 \
  --out-dir dist
```

The tag must equal `v` followed by the synchronized source version. Releases
accept a final `X.Y.Z` version or an `X.Y.ZrcN` release candidate. The output
directory must be absent or empty so an old or extra artifact can't enter the
release set.

The pull-request and tag workflows call the same rehearsal. The tag workflow
passes `--out-dir dist`, then preserves the twelve files as one GitHub Actions
artifact. Running this procedure by itself never publishes a distribution.

## Publish a version tag

Before the first release, configure these external controls:

- Make the repository public. The workflow rejects a release while the
  repository is private.
- Create a tag ruleset for `v*` that prevents tag updates and deletions. Limit
  tag creation to maintainers who can release.
- Create a protected GitHub environment named `pypi` with a maintainer as a
  required reviewer.
- Enable immutable releases in the GitHub repository settings.

For each existing PyPI project, configure the same trusted publisher:
repository `sediment-ai/sediment`, workflow `release.yaml`, and environment
`pypi`.

If the no-publish rehearsal passes and the release version is synchronized,
create and push an annotated tag:

```bash
git tag -a v0.1.0 -m "v0.1.0"
git push origin v0.1.0
```

The release workflow verifies that the checkout matches that annotated tag and
that the tagged commit belongs to `origin/main`. After the environment
approval, it verifies the tag, commit, and `main` ancestry again before PyPI
publication. It repeats the verification before GitHub publication. The
workflow runs these jobs in order:

1. `build` runs the complete rehearsal and preserves the twelve validated
   Python artifacts. Compatibility and security jobs check the release inputs.
2. `prepare-release` collects those artifacts, the tagged installer, and the
   security inventories. It generates support metadata and `SHA256SUMS`.
3. `publish-pypi` waits for approval from the protected `pypi` environment,
   then publishes the Python artifacts through PyPI trusted publishing.
4. `publish-github` publishes the prepared asset set as a GitHub Release.

The GitHub Release remains absent if PyPI rejects the version. If GitHub
publication stops after creating its draft, rerun that job. The retry replaces
the complete draft asset set, verifies every prepared asset filename, and refuses to
change a published release.

If PyPI accepts only part of the artifact set before a network failure, rerun
the PyPI job. The publisher checks PyPI and skips filenames that the index has
already accepted before uploading the remaining files.

A release-candidate tag such as `v0.1.0rc1` produces a GitHub prerelease. A
final version becomes the latest GitHub Release. GitHub supplies the tagged
source archives in addition to Sediment's attached artifacts.

## Bootstrap the six PyPI projects

If the six PyPI projects don't exist, PyPI permits only one pending publisher
for the same repository, workflow, and environment at a time. Bootstrap the
projects with a release candidate and the retry-safe publisher:

1. Create the pending publisher for `sediment-api` with the `pypi` environment.
2. Push an annotated release-candidate tag and approve `publish-pypi`. uv
   publishes the `sediment-api` wheel, then stops at the unregistered
   `sediment-capture` wheel because uv uploads all wheels before source
   distributions.
3. Create the pending publisher for `sediment-capture`, then rerun the failed
   job. The retry skips the accepted `sediment-api` wheel and publishes the
   `sediment-capture` wheel.
4. Repeat the preceding step for `sediment-cli`, `sediment-core`,
   `sediment-derive`, and `sediment-export`, in that order. Each retry publishes
   the next wheel. After you register `sediment-export`, the final retry
   publishes its wheel and then all six source distributions.
5. After the final PyPI upload succeeds, let `publish-github` create the GitHub
   prerelease. Verify every project page and install `sediment-cli` from a clean
   environment.

Each pending publisher becomes that project's trusted publisher when its first
file creates the project. Later releases publish all twelve files in one
approved job. See [PyPI trusted publisher internals](https://docs.pypi.org/trusted-publishers/internals/)
for the publisher-to-project relationship.
