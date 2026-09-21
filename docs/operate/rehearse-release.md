# Rehearse a release

Use the release rehearsal to build and exercise all six distributions without
publishing them. Each distribution produces one wheel and one source
distribution. Run the rehearsal before you create a version tag.

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

The command performs these checks:

1. All seven source version declarations agree. Every first-party dependency
   uses the same exact version. The version is `X.Y.Z` or `X.Y.ZrcN`.
2. Every third-party GitHub Action uses a full commit SHA and a readable
   version comment.
3. The build produces exactly six wheels and six source distributions. Their
   filenames, metadata, first-party requirements, and required package content
   agree. Each archive includes the repository's AGPL license text. None
   contains an `enterprise/` directory.
4. Each source distribution builds a wheel from an isolated temporary
   directory. The rebuilt wheels pass the same metadata and content checks.
5. A temporary environment installs only the original wheels. From a directory
   outside the checkout, the installed `sediment` command runs help, version,
   migration, database status, the read-only `facts` command, and transcript-hook
   installation.
6. The installed transcript hooks point to the installed command and retain
   the `Edit|Write` snapshot matcher.
7. An isolated Python process verifies that all six Sediment packages come from
   the installed environment. It runs `sediment server` on loopback and
   `sediment transcript --agent claude-code` against the declared
   `sediment-release-synthetic-v1` corpus in
   `scripts/fixtures/release_synthetic_v1.json`. The fixture supplies wire
   inputs; the runner fills the scratch file path and relative event times.
   The LiteLLM callback prepares gateway requests while that server is stopped.
   Separate installed helper processes replay them after restart. An intermediary
   drops the first acknowledgment after database commit; replay must return the
   retained Fact ID. OTLP and transcript payloads cross another outage. The runner
   changes the source file before replay and verifies the original observation.
   Requests include matching Decisions, a malformed sibling, and Codex file fanout.
8. A scratch Git repository supplies a real patch and Session notes. Push
   capture populates its mirror and Session-to-commit observations. A signed
   Repository rename and CI under the renamed label retain the same provider ID. The
   installed `derive`, `quarantine`, `release`, `export sft`, and `export rlvr`
   commands validate bundle v4, source identities, Evidence recipes, exclusions,
   retained Segments, strict Unicode declines, and emitted rows. A rehashed
   contradictory bundle must fail both import and training export. Changing
   source repository identity and rehashing the bundle must also refuse training.
9. Authenticated model and lifecycle HTTP reports reconcile the same source
   evidence. Authenticated commit inspection returns the same repository identity
   and original CI outcome after the rename. With a fixed cohort and time boundary, replay preserves Fact IDs,
   report bytes, canonical bundles, and training rows. Missing pull-request
   membership remains counted absence.
10. A separate generated Claude Code JSONL population carries 32 Edit observations
    of a 256 KiB file. The installed producer must enqueue two requests within
    the 8 MiB transport limit. After an API outage, a changed file, and a removed
    transcript, restarted sender processes deliver the original bytes. PostgreSQL
    must retain all 32 source identities, texts, and times. Duplicate replay
    must preserve their Fact IDs and the earlier corpus.

The command prints a pass message only after every check succeeds. It has no
publication step and doesn't use PyPI credentials.

The installed pipeline has a 180-second deadline. On timeout, Ctrl+C, or worker
exit, the parent stops that worker's process group before cleaning its database
and workspace. Cleanup sends `SIGTERM`, then `SIGKILL` to remaining descendants,
with bounded waits. A timeout or interruption reports a failed rehearsal.

Inspect the `pipeline acceptance:` JSON line for exact package and runtime
versions, wheel, callback, helper, and source payload hashes, delivery limits and
dispositions, policy and mirror revisions, Fact and row identities, and stage
counts. OTLP records, Facts, artifacts, and training rows
have separate units. One Codex record emits two Decisions; one selected Rollout
retains three Segments and emits two representable RLVR rows. The third Segment
is counted under `unrepresentable_unicode`.
The `transcript_batch` stage reports its later 32-observation population
separately from the fixed corpus's capture, delivery, report, and training counts.

Repeated builds from unchanged Facts, policy, mirror refs, and quarantine revision
must produce identical bundle and training bytes. Quarantine removes a call from
visible source evidence; release restores its content and training eligibility.
Their Provenance revisions change from 0 to 1 to 2, so bytes across those revisions
aren't interchangeable.

This run exercises Sediment's installed local producer and receiver with synthetic
LiteLLM callback, Claude Code JSONL, and OTLP inputs. It doesn't call a vendor-hosted model or certify
a vendor release. Collect separate source fixtures when a vendor changes its wire.
The run doesn't establish training quality or prove events that a sender omitted.
Pi's process-to-installed-helper contract also runs in the `shims` workflow.
Live pi, Cursor, Codex, gateway, and forge capture remain a separate release gate.

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
   Python artifacts.
2. `publish-pypi` waits for approval from the protected `pypi` environment,
   then publishes those files through PyPI trusted publishing.
3. `publish-github` adds the tagged `install.sh`, generates `SHA256SUMS`, and
   publishes a GitHub Release from the same Python artifacts.

The GitHub Release remains absent if PyPI rejects the version. If GitHub
publication stops after creating its draft, rerun that job. The retry replaces
the complete draft asset set, verifies all fourteen filenames, and refuses to
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
