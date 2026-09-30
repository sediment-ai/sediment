# Choose a training export

Start here to turn captured evidence into a dataset that you can audit before
training. This guide is for operators who have verified capture. If you haven't
captured a Session, complete [Enroll your team](../operate/run-pilot.md) first.

1. [Choose an objective](#choose-an-objective) that matches your evidence.
2. [Prepare the export](#prepare-the-export), usually from a reviewed bundle.
3. [Choose the output contract](#choose-the-output-contract).
4. Follow the objective guide, then [inspect the output](#inspect-the-output).
5. [Audit the split before training](#audit-the-split-before-training).

## Choose an objective

| You want to… | Evidence needed | Guide |
| --- | --- | --- |
| Compare distinct responses to the same prompt and model with direct preference optimization (DPO) | Attributed completions with human-explicit accept/reject decisions, or clean opposing CI verdicts | [Export DPO pairs](dpo.md) |
| Imitate a captured response with supervised fine-tuning (SFT) | An Attributed completion with a human-explicit accept, sufficient edit retention, or a clean CI pass, under the selected recipe | [Export SFT and diff-SFT rows](sft.md) |
| Imitate an exact attributed-file patch with diff-shaped SFT (diff-SFT) | SFT-eligible Attributed completions and the commit diff in a Git mirror | [Export SFT and diff-SFT rows](sft.md#export-diff-sft-rows) |
| Learn from a failed CI run followed by a passing run | A clean workflow lineage and the fixing diff | [Export Recovery rows](recovery.md) |
| Audit trajectories or prepare reinforcement learning from verifiable rewards (RLVR) data | Captured Rollouts; task and Reward eligibility depend on the target | [Export RLVR tasks and trajectories](rlvr-export.md) |

Each objective selects its own Evidence recipe. Capture doesn't guarantee
training rows: an accept-only workflow can produce SFT rows and no DPO pairs.
A Retry linkage or a corrective prompt doesn't supply a DPO rejection label.
[Training-objective evidence](../adr/0011-training-objectives-own-evidence-interpretation.md)
explains the separation.

## Prepare the export

For DPO, SFT, diff-SFT, or RLVR, follow
[Run derivations](../operate/run-derivations.md) to build and inspect one bundle.
The examples in this section use that guide's bundle at
`$HOME/sediment-derived/review`. Reuse the reviewed bundle across exports;
don't rebuild it between objectives.

### Export a reviewed bundle

Commands with `--from` read the organization, scope, and policy from the bundle.
They don't require `SEDIMENT_ORG_ID` or `SEDIMENT_DATABASE_URL`, and they don't
fall back to live Facts. Some projections still need mirrors:

| Projection from `--from` | `SEDIMENT_MIRROR_PATH` required |
| --- | --- |
| DPO or SFT, including consumer profiles | No |
| Diff-SFT | Yes, for commit diffs |
| RLVR `sediment` or `swe-bench` | Yes, for Reference patches |
| RLVR `nemo-gym` | No |

If the projection needs mirrors, use the deployment's mirror root and ensure
that it contains the referenced commits. The bundle doesn't contain Git patches.
Recovery doesn't accept `--from`; use its direct procedure.

### Export directly from Facts

If you don't need a saved review boundary, omit `--from` from an objective's
command. Use the configured
[operator shell](../operate/deploy.md#set-up-an-operator-shell) with
`SEDIMENT_ORG_ID`, `SEDIMENT_DATABASE_URL`, and `SEDIMENT_MIRROR_PATH`.
Signing in with `sediment login` doesn't configure this access.

Direct exports read the organization's available history with default policy.
They don't accept the bundle builder's cohort or policy flags. Separate direct
runs can see different Facts. Recovery always uses this path.

### Prepare private destinations

Run the installed CLI in the operator or offline export environment. Create a
private parent for the examples:

```bash
install -d -m 700 "$HOME/sediment-exports"
umask 077
```

Use a fresh child directory for each command. Don't create a consumer-profile
destination in advance: profile publication requires a nonexistent path.
If `/tmp` is memory-backed, set `TMPDIR` to an existing private, disk-backed
directory. Provision space for staging and the output.

## Choose the output contract

Without `--profile`, each command writes Sediment's canonical training rows.
Use these to inspect evidence or to implement a separately validated adapter.
An RLVR `--target` selects a row shape; it doesn't establish compatibility with
a particular downstream release.

If you need a supported Hugging Face TRL, Fireworks, SWE-bench, or NeMo Gym
format, follow [Export for a consumer](consumer-compatibility.md) after choosing
your objective and recipe. That guide covers dependencies, explicit runtime
inputs, and profile commands. Diff-SFT and Recovery have no consumer profile.

## Inspect the output

The objective guides name their files and diagnostic counts. With the default
`eval_fraction = 0.1`, populated partitions use `<name>.train.jsonl` and
`<name>.eval.jsonl`. A bundle built with `eval_fraction = 0.0` produces
`<name>.jsonl`. Empty partitions produce no file.

1. Read the command's row counts, written paths, and diagnostic maps. A zero-row
   result isn't a dataset. `nothing written` leaves earlier files untouched.
2. Check the files at the reported paths. JSON Lines (JSONL) stores one row per
   line. Inspect representative rows privately; they can contain source code,
   prompts, and responses.
3. Check the recipe, label or eligibility source, repository identity, and
   Provenance. Confirm that they match the evidence you intended to use.
4. Validate the canonical `schema_id` and `schema_version` against the
   [Schema reference](../reference/schema.md#training-rows) before adapting rows.
   For a consumer profile, inspect its evidence sidecars and compatibility
   manifest instead of treating the data file as the complete audit record.

Canonical file replacement is atomic per file, not across a train/eval pair.
If publication fails or stops, preserve the diagnostics and rerun to a fresh
directory. Reusing a directory can mix earlier files with later output.
[RLVR output safeguards](rlvr-export.md#inspect-the-output) and consumer profiles
also reject destinations that could mix generations.

### Check representation exclusions

Inspect `non_finite_number` and `unrepresentable_unicode` in export skip counts.
An emitted non-finite number or unpaired surrogate excludes the row, including
values in nested arguments, keys, and metadata. A DPO or Recovery pair counts
once when either side fails. Content that the recipe omits doesn't exclude a
row. Facts and bundles preserve those values; exports don't replace them.

Keep diagnostic populations separate. Counts can describe inputs, pairs,
buckets, or Segments; their sum isn't a count of lost training rows.

## Audit the split before training

Review the exported rows against your versioned benchmark manifest. The built-in
split hashes Session identifiers. It doesn't enforce repository, prompt, or
task isolation across Sessions. DPO and Recovery put a pair in eval when either
resolvable member belongs to an eval Session. Recovery rows with no resolvable
Session evidence go to train.

Exclude pilot or development tasks when the benchmark requires it. Check model
and recipe balance, label or Reward contrast, prompt overlap, and the size of
the labeled evaluation population. Consumer profiles reject identical full
prompts across train and eval; they don't detect every shared task.

Optional: For a deployment-wide DPO/SFT diagnostic, run the following command
in the configured operator shell. Set the recipe flags to match your export:

```bash
sediment report dataset-diagnostics \
  --org "$SEDIMENT_ORG_ID" \
  --dpo-recipe dpo_human \
  --sft-recipe sft_curated \
  --json > "$HOME/sediment-exports/dataset-diagnostics.json"
```

This report reads live Facts and mirrors. It doesn't accept `--from`, bundle
cohort filters, or a Derivation policy file. Its counts don't certify a saved
export. It reports model balance, Confidence, duplicate prompts, DPO bucket
sparsity, SFT Confidence-floor exclusions, abandonment, and diagnostic Fate.
Use [Measure agent work](../operate/measure-agent-work.md) for operational and
merge-retention reports; those reports don't determine training eligibility.

Confidence and CI reliability don't set trainer weights. If you use either for
weighting, define and version that mapping. Confidence isn't a calibrated
probability of correctness. Fate and merge retention don't enter training rows.

## Retain source metadata

Keep the bundle manifest, export command and recipe selection, diagnostic
output, benchmark manifest, and export files together. For mirror-dependent
exports, retain the repository revisions used to produce patches.

Preserve Attribution sources and Session observation IDs. Recipes permit
inferred Attribution; an empty observation list supplies no observed
Session-to-commit evidence. DPO preserves the evidence for each member separately.
Qualified repository identity distinguishes repository lifetimes across renames.
Don't join rows only by a matching repository name or commit.

Schema versions, recipe versions, policy versions, and consumer-profile versions
identify separate contracts. Full source Facts remain on canonical artifacts;
[the source metadata contract](../adr/0014-factual-outcomes-and-training-evidence.md#evidence-recipes-and-exact-metadata)
defines what each training format retains.
