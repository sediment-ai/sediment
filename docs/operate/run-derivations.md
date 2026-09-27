# Run derivations

Use `sediment derive` to build a reviewed, immutable bundle of Attributed
completions and Rollouts, and then export training rows from that bundle.
[How Derivation works](../explanation/how-derivation-works.md) explains the
snapshot, policy, and bundle contracts.

Run the commands from the operator shell.
[Set up an operator shell](deploy-ec2.md#set-up-an-operator-shell) shows it
for EC2, and [the same section](deploy.md#set-up-an-operator-shell) for your
own host. `sediment derive` reads PostgreSQL and the Git mirrors, and derives
the organization in `SEDIMENT_ORG_ID`.

## Build a bundle

Choose a destination that doesn't exist, and run:

```bash
sediment derive --out /data/derived/2026-08-19
```

Sediment derives every artifact from one read-only database snapshot, so
concurrent capture can't mix states within the bundle. The command prints
artifact counts, the `skipped`, `fragmented`, and `excluded` counts, the policy
digest, and the bundle's `as_of` time.

The bundle holds `manifest.json` and six JSONL files. The directory has mode
`0700`, and each file has mode `0600`. Sediment never overwrites a bundle, so
each run needs an unused destination.

Missing mirrors don't stop the run. Sediment counts them under `skipped`, and
the bundle can hold fewer Attributions.

## Select a cohort

To limit the bundle to some users and a time window, run:

```bash
sediment derive \
  --out /data/derived/alice-bob-august \
  --users alice bob \
  --since 2026-08-01T00:00:00Z \
  --until 2026-09-01T00:00:00Z
```

`--since` is inclusive and `--until` is exclusive. Both need a timezone, and
both select by Inference call observation time. Sediment keeps a whole Rollout
when any of its Inference calls falls in the window. It excludes and counts a
Rollout that mixes allowed and other users.

## Set Derivation policy

To change a default, write a TOML file with only the fields that you want to
override. These values are the defaults:

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

The loader rejects unknown fields, unsupported versions, and out-of-range
values. The manifest records every resolved value and the policy's SHA-256
digest.

`eval_fraction` assigns about that share of Sessions to evaluation, by a hash
of the Session identifier. Set it to `0.0` to write no split. The split keeps
each Session on one side, but the same repository, prompt, or task can appear
on both sides. If your evaluation needs unseen repositories or tasks, assign
them in your own versioned benchmark manifest before training.

## Inspect the bundle

To print the first `N` Attributed completions and Rollouts after the write,
add `--sample N`:

```bash
sediment derive --out /data/derived/review --sample 2
```

Samples can contain prompts, responses, and decisions, so treat the terminal
output as sensitive.

Before you train, check these counts:

- `skipped`: each closed reason says why an input didn't qualify. For example,
  `unmatched_decision_call_id` means that a Developer decision names no
  captured Inference call. A narrow cohort appears under `excluded`, not
  `skipped`.
- `fragmented`: each Turn stays in a Segment. Read `prior_output_absent`,
  `input_history_changed`, and `prior_output_not_replayed` before you treat
  separate Segments as one trajectory.
- `conflicting_run_identity` and `ambiguous_workflow_verdicts`: CI outcomes that
  exports can't resolve to one verdict.

Each JSONL line wraps a canonical record in a `record_json` string. Use the
bundle reader, which validates the whole bundle, rather than parsing lines
yourself. Validation proves the bundle's internal relationships. It can't prove
that an external producer supplied a complete or truthful Fact population.

## Export from the bundle

Project the same bundle into each objective that you need:

```bash
sediment export dpo --from /data/derived/review --out /data/export/dpo
sediment export sft --from /data/derived/review --out /data/export/sft
```

Every export validates the bundle first and stops on invalid content. It never
falls back to live Facts. [Choose a training export](../exports/training-exports.md)
lists each objective, its Evidence recipes, and which ones also need mirrors.

## Compare two policies

Build each policy into its own destination:

```bash
sediment derive --policy policy-a.toml --out /data/derived/policy-a
sediment derive --policy policy-b.toml --out /data/derived/policy-b
```

Compare `policy_digest`, scope, `as_of`, and the `skipped`, `fragmented`, and
`excluded` counts before you compare rows.

## Troubleshoot

| Error | Action |
| --- | --- |
| `destination already exists` | Choose an unused path. There is no overwrite flag. |
| `--since must be timezone-aware` | Add `Z` or a UTC offset. |
| `unknown derivation policy field` | Remove the field. |
| `SEDIMENT_MIRROR_PATH is not set` | Set it in the operator shell. |
| `unsupported bundle_schema_version 1` | Rebuild the bundle into an unused destination. |
| `Inference call identity population exceeds 50000` | See the following paragraph. |

A bundle carries the organization's complete call and repository identity
populations, each capped at 50,000 rows, even for a small cohort. A narrower
cohort doesn't help. Don't trim identity files, quarantine valid Facts, or
split the organization to get under the cap.
[Open an issue](https://github.com/sediment-ai/sediment/issues) with the
failed command and its `as_of`.

If `sediment derive` stops before it reports success, check the destination.
If the destination exists, it holds a complete bundle. If it doesn't, rerun
into an unused path. A process that ends abruptly can leave a private staging
directory named `.<destination>.<suffix>` beside the destination. Remove it
after the process has ended.

In a container where `/tmp` is memory-backed, set `TMPDIR` to a private
disk-backed directory before a large run. The Compose `operator` service
already does.

## Use the bundle from Python

`build_derived_bundle_context` derives a file-backed bundle, and
`open_derived_bundle` validates and opens one from disk. Use both as context
managers, and keep the context open until you finish reading. For a small
bundle, `build_derived_bundle` and `read_derived_bundle` load it into memory,
up to 64 MiB of encoded payload.
