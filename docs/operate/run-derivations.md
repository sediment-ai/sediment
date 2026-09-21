# Run derivations

Use `sediment derive` to create a reviewed, immutable bundle of Attributed
completions and Rollouts before you export training rows. The command derives
the deployment's configured organization. It doesn't accept an organization
selection flag.

## Prerequisites

Configure the same local deployment values the API uses:

```bash
export SEDIMENT_ORG_ID=acme-corp
export SEDIMENT_DATABASE_URL='postgresql+psycopg://sediment@database.example/sediment'
export SEDIMENT_MIRROR_PATH=/data/mirrors
```

Store the real database URL in a protected environment file or secret manager.
The example omits a password. The command needs read access to the PostgreSQL
Fact store and mirrors. Missing mirrors don't stop the run. Sediment records
them under `skipped` and may produce fewer Attributions.

## Create a complete organization bundle

Choose a destination that doesn't exist, then run the Derivation:

```bash
sediment derive --out /data/derived/2026-08-19
```

If you omit `--users`, the command includes every user in
`SEDIMENT_ORG_ID`. An empty organization is valid. It produces a manifest and
six zero-byte JSONL files.

The command prints artifact counts, referenced-inference-call count, `skipped`,
`fragmented`, `excluded`, the complete policy digest, and the Fact-derived `as_of` time.
It derives every artifact through one read-only `REPEATABLE READ` snapshot, so
concurrent ingest can't mix Fact states within the bundle.

## Select a cohort

Pass user IDs once, separated by spaces:

```bash
sediment derive \
  --out /data/derived/alice-bob-august \
  --users alice bob \
  --since 2026-08-01T00:00:00Z \
  --until 2026-09-01T00:00:00Z
```

`--since` is inclusive, and `--until` is exclusive. Both flags require a
timezone-aware RFC 3339 timestamp and select by inference-call observation
time.

Sediment keeps a complete Rollout when at least one of its inference calls
falls inside the time interval. If a user allowlist is present, every
inference-call user in the Rollout must be allowed. Sediment excludes and
counts a mixed-user Rollout.

## Set Derivation policy

Create a strict TOML file with only the fields that you want to override:

```toml
schema_version = 1

[attribution]
post_push_grace_period_minutes = 10
max_commits_per_push = 20

[attribution.git_notes]
min_similarity = 0.3
lookback_window_minutes = 10080

[attribution.jaccard]
min_similarity = 0.7
lookback_window_minutes = 60

[split]
eval_fraction = 0.1
```

Pass the file to `sediment derive`:

```bash
sediment derive \
  --policy ./derivation-policy.toml \
  --out /data/derived/policy-a
```

The loader rejects unknown sections, unknown fields, unsupported schema
versions, and out-of-bounds values. Omitted values use code defaults. The
manifest expands every value and records the SHA-256 digest of the resolved
policy.

`bundle_schema_version` identifies the manifest wire contract. The catalog at
`schemas/catalog.json` maps that version to its stable canonical schema ID.
The Derivation policy's `schema_version` identifies the policy-file shape;
policy Provenance identifies Derivation semantics. Neither value is an
Evidence recipe version, a downstream compatibility-profile version, or an
Alembic revision.

If you omit `--policy`, Sediment uses the complete version-1 policy shown in
this section.

## Check the holdout boundary

The `[split]` section implements one Session-grained train/eval split.
`eval_fraction = 0.1` assigns about 10% of Sessions to evaluation by hashing
their identifiers. The assignment is deterministic and independent of ingest
order.

If you intend to disable split files, set the fraction to `0.0`.

Use this split to keep the rows from one developer-agent workflow together.
The split doesn't keep a repository, exact prompt, or external task identifier
on one side when that identity appears in several Sessions.

If your evaluation claim requires an unseen repository, prompt, or task, make
that assignment in a versioned benchmark manifest before training. Audit the
export against that manifest.

The version-1 Derivation policy rejects a `split.mode` field; no mode value
exists to set. It doesn't provide repository, prompt, or strict boundaries as
policy options. Task-keyed isolation remains benchmark-specific.

## Inspect sample rows

Standard output contains only the summary. To print the first `N` Attributed
completions and Rollouts after a successful write, add `--sample N`:

```bash
sediment derive --out /data/derived/review --sample 2
```

Samples can contain prompts, responses, decisions, and CI outcomes. Treat the
terminal output as sensitive.

CI outcome order in a bundle is stable evidence order, not semantic attempt
order. Exports recompute `CIResolution` from provider `run_id` and
`run_attempt`. Before training, inspect the `conflicting_run_identity` and
`ambiguous_workflow_verdicts` counts. A suspected-flake verdict can remain
`passed` or `failed` while carrying reliability 0.0.

For a complete review, inspect `manifest.json` and the six JSONL files. The
directory mode is `0700`; every file mode is `0600`. Bundle version 4 declares
`record_encoding: "sediment-record-json-v1"`. Decode each line's `record_json`
string to inspect the canonical record. Use the bundle reader when exporting;
it validates the envelope, metadata, and canonical relationships.

Keep `inference_call_identities.jsonl`, `repository_identities.jsonl`, and
`repository_renames.jsonl` intact when selecting artifacts. Each file declares
a complete organization population through `as_of`, including evidence outside
the cohort. Each has an independent 50,000-row limit, which can refuse a small
cohort in a large organization. Validate with `validate_derived_bundle` before projecting an
in-memory bundle. Versions 1–3 require recomputation. Validation proves the
bundle's internal relationships. It can't prove that an external producer
supplied a complete or truthful Fact population.

If the writer stops before reporting success, check the destination. If it
exists, use `open_derived_bundle` to validate the complete artifact; another
write refuses to overwrite it. If it is absent, retry with an unused
destination. `SIGKILL` before the final directory rename can leave a private
staging directory named `.<destination>.<suffix>`. Remove only the staging
directory owned by that writer after confirming its process has ended.
Process-interruption checks cover the rename boundary, not power-loss recovery.

## Export a reviewed bundle

Use the same bundle for each supported projection:

```bash
sediment export dpo --from /data/derived/review --out /data/export/dpo
sediment export sft --from /data/derived/review --out /data/export/sft
sediment export diff-sft --from /data/derived/review --out /data/export/diff-sft
sediment export rlvr \
  --target sediment \
  --from /data/derived/review \
  --out /data/export/rlvr
```

Direct preference optimization (DPO) defaults to `dpo_human`. Supervised
fine-tuning (SFT) and diff-shaped SFT (diff-SFT) default to `sft_curated`.
Select outcome-derived evidence explicitly:

```bash
sediment export dpo --recipe dpo_outcome \
  --from /data/derived/review --out /data/export/dpo-outcome
sediment export sft --recipe sft_verified \
  --from /data/derived/review --out /data/export/sft-verified
sediment export diff-sft --recipe sft_verified \
  --from /data/derived/review --out /data/export/diff-sft-verified
```

DPO and SFT read only the bundle. Diff-SFT and reinforcement learning from
verifiable rewards (RLVR) also read the local git mirrors for commit diffs.
Before projection, each command validates the manifest, file paths, counts,
hashes, row shapes, organization, Session, split, inference-call references, and
embedded observation identity and capture time. A validation
failure stops the export. Sediment doesn't substitute live Facts.

Recovery doesn't accept `--from` because a red-to-green commit pair doesn't
project from a canonical artifact. The Recovery export derives each pair
directly from CI Facts and mirrors. It resolves attempts first, then excludes
suspected-flake lineages, ambiguous verdicts, and non-verdicts. Every Recovery
row names `recovery_ci` version 1.

## Recompute a bundle

Sediment never overwrites a bundle directory. Choose a distinct destination
for each run:

```bash
sediment derive --policy policy-a.toml --out /data/derived/policy-a
sediment derive --policy policy-b.toml --out /data/derived/policy-b
```

Compare `policy_digest`, scope, `as_of`, mirror revisions, counts, `skipped`,
`fragmented`, and `excluded` before you compare rows. If you need the same destination name,
move or delete the old build artifact explicitly, then rerun the command.

## Diagnose failures

If visible organization-wide Inference call identities through `as_of` exceed
50,000, `sediment derive` exits 1 with
`error: Inference call identity population exceeds 50000`. Direct training
exports that build a bundle use the same limit. Importing an oversized bundle
for `export --from` fails with `error: identity population exceeds row limit`.
A narrower cohort doesn't reduce this required identity population.

Record the failed command, its scope and `as_of`, and the error. Pause affected
Derivations and exports, and request a supported capacity change from the
maintainer. If a historical result is sufficient, select an earlier `as_of`
whose complete populations fit the limits and label the result with that
boundary. That result excludes later evidence. Don't trim identity files,
quarantine valid Facts, or divide one organization into artificial tenants to
bypass the completeness check.

- `unsupported bundle_schema_version 1`: recompute from Facts with `sediment
  derive` into a distinct directory. Sediment does not rewrite the old bundle.
- `destination already exists`: choose a different path. There is no overwrite
  flag.
- `--since must be timezone-aware`: include `Z` or a numeric UTC offset.
- `unknown derivation policy field`: remove the field or use a supported
  policy schema.
- `SEDIMENT_MIRROR_PATH is not set`: configure the deployment's mirror root.
- A non-empty `skipped` map: before exporting, inspect its closed reasons. A
  narrow cohort should appear under `excluded`, not `skipped`.
  Decision-attachment reasons under `attributed_completion` and `rollout`
  distinguish a missing call identifier (`missing_decision_call_id`), an
  identifier with no matching Inference call (`unmatched_decision_call_id`),
  and an identifier that resolves to several calls
  (`ambiguous_decision_call_id`). Scope mismatches use
  `decision_org_mismatch` or `decision_session_mismatch`.
  `rollout.attribution_note_unreadable` identifies a listed Attribution note
  that couldn't be read or decoded.
- A non-empty `fragmented` map: every Turn remains in a Segment. Inspect
  `prior_output_absent`, `input_history_changed`, and `prior_output_not_replayed`
  before treating separate Segments as one continuous trajectory.

[How Derivation works](../explanation/how-derivation-works.md) explains the
snapshot, full-organization Derivation, policy, and bundle contracts.

## Use bounded bundle readers

Use `build_derived_bundle_context` to derive a private file-backed bundle, or
`open_derived_bundle` to validate and consume one from disk. Use either API
within a context manager. Keep that context open through projection and
publication. Access after context exit fails. `build_derived_bundle` and
`read_derived_bundle` explicitly materialize a bundle with a default 64 MiB
encoded payload limit. The CLI uses the bounded contexts.

`BundleLimits` defaults to 512 MiB per encoded record and 8 GiB per private
staging store. Overlapping stores consume separate budgets. These byte limits
do not guarantee a fixed Python memory peak. An oversized record or stage raises
`BundleCapacityError`; it does not truncate the artifact.

The Compose `operator` service stages in the disk-backed `sediment-staging`
volume through `TMPDIR`. In any other deployment that mounts `/tmp` as
memory-backed storage, choose a private disk-backed `temporary_parent` or set
`TMPDIR` before opening a large bundle. Memory-backed staging also counts
against the container memory limit. Provision filesystem capacity independently
of container memory. The context removes its private snapshot on normal exit
and handled failure. A terminated or killed process leaves its stage behind;
the next staging command in the same directory removes every stage whose owner
no longer holds its lock and logs `abandoned_private_stage_removed`. An
interrupted bundle publication directory beside `--out` is a complete private
bundle, not a payload stage. Sediment leaves it for you to inspect or remove.

For repeatable memory and storage measurements, follow
[Profile reports and Derivations](profile-derivations.md).
