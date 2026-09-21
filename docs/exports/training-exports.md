# Choose a training export

For a specific downstream release, select a versioned consumer profile.
[Export for a consumer](consumer-compatibility.md) documents installation,
configuration, exact commands, and the limits of each support claim.

Use this guide to choose an export by training objective, prepare its source
evidence, and audit its output before training.

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

DPO uses a pairwise comparison. SFT imitates one eligible completion. Diff-SFT
uses the same SFT eligibility contract but trains on a unified patch. Recovery
uses CI lineage rather than a per-completion canonical artifact. RLVR projects
Rollouts and recorded Verifier evidence.

Every repository-bearing row carries its qualified repository identity. DPO
retains chosen and rejected identities independently. A valid rename keeps
the same identity; matching labels or commits cannot join different lifetimes.
Bundle-based exports verify complete declared evidence before projection.
If repository identity evidence is absent or conflicting, inspect repository skip counts.

## Check representation exclusions

Inspect `non_finite_number` and `unrepresentable_unicode` in export skip counts.
Every objective excludes a row if an emitted value contains a non-finite number
or an unpaired surrogate, including nested tool arguments, keys, and metadata.
A DPO or Recovery pair counts once when either side fails. Source content that
the Evidence recipe omits doesn't exclude the row. Facts and canonical bundles
preserve those values; exports don't replace them with null or replacement text.
See [Training representation](../adr/0015-lossless-values-and-bundle-v2.md#training-representation).

## Prepare the export

You need a deployment or local store with captured Facts and
`SEDIMENT_ORG_ID` set. Source requirements differ by export path:

- Direct exports and mirror-backed projections require
  `SEDIMENT_MIRROR_PATH`.
- DPO and SFT projections from an existing bundle don't require a mirror.
- Diff-SFT from a bundle still requires a mirror for commit diffs.
- RLVR from a bundle still requires mirrors for Reference patches.

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

The RLVR export still reads local mirrors for Reference patches.

Recovery has no `--from` form. It is the one sanctioned Fact-derived exception
rather than a projection of a canonical artifact. Its commit-pair shape can't
project from an Attributed completion. [Run
derivations](../operate/run-derivations.md) covers policy, cohort selection,
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

Each row also identifies its canonical JSON Schema Draft 2020-12 contract. DPO,
SFT, and diff-SFT store `schema_id` and `schema_version` in `metadata`.
Recovery stores them on its Sediment-native row envelope. RLVR stores them at
the top level or under Sediment-owned `metadata`, depending on the target. The
[Schema reference](../reference/schema.md) documents every field. The committed
`schemas/catalog.json` maps each Python source type to its stable schema ID,
positive integer schema version, artifact family, and file path.

Before adapting a row for a trainer, validate and record its canonical schema.
A downstream adapter can then remove Sediment metadata. The adapter's
compatibility-profile version identifies that mapping. An Alembic revision
identifies only the physical PostgreSQL schema.

Canonical schema identity and Evidence recipe identity are independent. The
categorical label, `metadata.ci_reliability`, and a trainer's downstream sample
weight are also distinct. `label_confidence` combines evidence for filtering
and diagnostics. It doesn't configure a trainer weight. Consumers must honor
recipe, source, Confidence, and CI reliability when they define and version
that mapping. Exporters never derive downstream sample weight.

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
prepare trainer inputs. Version 1 recipes permit inferred Attribution; an empty
observation list leaves observed identity unavailable. Full matching Facts live
on the canonical artifacts. [ADR 0014](../adr/0014-factual-outcomes-and-training-evidence.md#evidence-recipes-and-exact-metadata)
defines the source scope for both DPO members, SFT and diff-SFT targets, Recovery
enrichment sides, and each RLVR target.
