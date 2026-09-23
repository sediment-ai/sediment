# Choose a training export

Choose an objective, prepare its evidence, and audit the output before training.
For a supported trainer release, use a versioned
[consumer profile](consumer-compatibility.md).

Sediment exports direct preference optimization (DPO), supervised fine-tuning
(SFT), diff-shaped SFT (diff-SFT), Recovery, and reinforcement learning from
verifiable rewards (RLVR) rows.

## Choose an objective

| Objective | Command | Output | Default Evidence recipe | Guide |
|---|---|---|---|---|
| Compare a chosen response with a rejected response for the same prompt and model | `sediment export dpo` | `dpo.jsonl` | `dpo_human` version 2 | [Export DPO pairs](dpo.md) |
| Imitate an eligible response | `sediment export sft` | `sft.jsonl` | `sft_curated` version 1 | [Export SFT and diff-SFT rows](sft.md) |
| Imitate the exact attributed-file patch | `sediment export diff-sft` | `diff_sft.jsonl` | `sft_curated` version 1 | [Export SFT and diff-SFT rows](sft.md) |
| Learn from a red-to-green CI transition | `sediment export recovery` | `recovery.jsonl` | `recovery_ci` version 1 | [Export Recovery rows](recovery.md) |
| Train or audit a trajectory against a Verifier | `sediment export rlvr` | `tasks.jsonl`, `rollouts.jsonl`, optional `environment.yaml` | `rlvr_ci` version 1 | [Export RLVR tasks and trajectories](rlvr-export.md) |

Rows retain qualified repository identity; DPO keeps each member's identity
separately. Renames preserve identity. Matching names or commits don't join
different repository lifetimes. Inspect repository skip counts for absent or
conflicting evidence.

## Check representation exclusions

Inspect `non_finite_number` and `unrepresentable_unicode` in export skip counts.
Every objective excludes a row if an emitted value contains a non-finite number
or an unpaired surrogate, including nested tool arguments, keys, and metadata.
A DPO or Recovery pair counts once when either side fails. Source content that
the Evidence recipe omits doesn't exclude the row. Facts and canonical bundles
preserve those values; exports don't replace them with null or replacement text.
See [Training representation](../adr/0015-lossless-values-and-bundle-v2.md#training-representation).

## Prepare the export

Set `SEDIMENT_ORG_ID`. Direct exports need database and mirror access. Bundle
projections have these requirements:

| Projection from `--from` | Mirror required |
| --- | --- |
| DPO and SFT | No |
| Diff-SFT | Yes, for commit diffs |
| RLVR `sediment` and `swe-bench` | Yes, for Reference patches |
| RLVR `nemo-gym` | No |

The canonical Derivation policy defaults `eval_fraction` to `0.1`.

On a deployment, run the command in the operator container, which mounts the
export and private staging volumes. For example:

```bash
docker compose --profile operator run --rm operator sediment export dpo --out /data/export
```

## Export a reviewed bundle

For a reviewed, reproducible workflow, derive once. Then project the same
bundle into each per-completion format:

```bash
sediment derive --out /data/derived/review
sediment export dpo --from /data/derived/review --out /data/export
sediment export sft --from /data/derived/review --out /data/export
sediment export diff-sft --from /data/derived/review --out /data/export
```

For RLVR, pass the same bundle and an explicit target:

```bash
sediment export rlvr \
  --target sediment \
  --from /data/derived/review \
  --out /data/export/sediment-reviewed
```

The `sediment` target still reads mirrors for Reference patches.

Recovery reads CI Facts and mirrors directly and doesn't accept `--from`.
See [Run derivations](../operate/run-derivations.md) for policy, cohort selection,
inspection, and recomputation.

## Inspect the output

The DPO, SFT, diff-SFT, and Recovery exports write `dpo.jsonl`, `sft.jsonl`,
`diff_sft.jsonl`, or `recovery.jsonl`. If `eval_fraction` is greater than zero,
the exporter writes `<name>.train.jsonl` and `<name>.eval.jsonl` instead.

Writes are atomic. If a projection is empty, the exporter leaves existing
files untouched and prints `nothing written`. It never truncates an earlier
export. Every run prints row counts and a `skipped` tally. Each format uses a
closed skip-reason vocabulary, so no ineligible input disappears silently.

The RLVR writer has additional output-directory safeguards. [Inspect the RLVR
output](rlvr-export.md#inspect-the-output) before you reuse an RLVR export
directory.

DPO, SFT, and diff-SFT rows keep Sediment evidence under `metadata`, outside
the trainer inputs. For those rows, `metadata.split` contains `train` or
`eval`. Recovery uses its Sediment-native row envelope. RLVR uses the location
that its target contract defines. Structured `provenance` contains
`policy_version`, integer `quarantine_revision`, and nullable full
`policy_digest`. Confidence values come from the
[Confidence ladder](../agents/exports-and-stats.md#the-confidence-ladder-label_confidencepy).
The eval split is deterministic per Session
([Eval split](../../CONTEXT.md#eval-split-split)).

Validate each row's canonical `schema_id` and `schema_version` before adapting
it. The [Schema reference](../reference/schema.md) defines fields; the
[consumer profile](consumer-compatibility.md) versions the downstream mapping.
Schema, recipe, policy, and database versions identify separate contracts.

Confidence and CI reliability don't set trainer weights. If you use them for
weighting, define and version that mapping with the Evidence recipe and source.

## Audit the split before training

If you plan to train on an export, run dataset diagnostics with the same
Evidence recipes first:

```bash
sediment report dataset-diagnostics \
  --org acme-corp \
  --dpo-recipe dpo_human \
  --sft-recipe sft_curated \
  --json > dataset-diagnostics.json
```

The report counts model and recipe balance, Confidence distributions,
cross-split exact prompt duplicates, near-duplicate DPO prompt buckets, SFT
Confidence-floor exclusions, sparse DPO buckets, abandonment coverage, and
diagnostic final Fate counts. Fate doesn't enter an Evidence recipe or any
training row.

If you capture pull request merge Facts, run `sediment report merge-retention`
to inspect whether attributed changes survived review and integration. Its
thresholds are sensitivity diagnostics. They don't enter an Evidence recipe,
Confidence, Reward, or any training row.

If you need to audit individual scored changes, write the canonical rows while
you generate the aggregate report:

```bash
sediment report merge-retention \
  --org acme-corp \
  --rows-out merge-retention.jsonl
```

The JSONL artifact contains source identities, boundary scores, Attribution
metadata, and Provenance. It contains no source text, prompt text, completion
text, Developer decision label, training eligibility, Reward, Confidence, or
Evidence recipe. An empty or failed export leaves an earlier destination file
untouched.

The built-in split keeps one Session in one partition. It doesn't enforce a
repository, prompt, or task-keyed holdout across Sessions.

If a benchmark uses one of those boundaries, compare every exported row with
the benchmark manifest through its source identifiers. Exclude pilot or
development tasks before training.

Preserve the diagnostics, benchmark manifest, Derivation manifest, and export
files together. The row-level `split` value alone doesn't prove that the
evaluation set matches a stronger holdout claim.

## Retain source metadata

Keep each row's Attribution source and Session observation ID fields when you
prepare trainer inputs. Recipes permit inferred Attribution; an empty
observation list leaves observed identity unavailable. Full matching Facts live
on the canonical artifacts. [ADR 0014](../adr/0014-factual-outcomes-and-training-evidence.md#evidence-recipes-and-exact-metadata)
defines the source scope for both DPO members, SFT and diff-SFT targets, Recovery
enrichment sides, and each RLVR target.
