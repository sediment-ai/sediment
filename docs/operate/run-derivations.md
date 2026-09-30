# Run derivations

Start here to turn captured Facts into a bundle that you can review and export
for training. This guide is for operators with a running deployment and captured
Sessions. `sediment derive` builds Attributed completions and Rollouts; you
review the result before exporting training rows.

1. [Prepare the operator shell](#prepare-the-operator-shell).
2. [Select a cohort](#select-a-cohort).
3. Optional: [Set Derivation policy](#set-derivation-policy).
4. [Build a bundle](#build-a-bundle).
5. [Inspect the bundle](#inspect-the-bundle).
6. [Export from the bundle](#export-from-the-bundle).

[How Derivation works](../explanation/how-derivation-works.md) explains the
pipeline. [Attribution](../explanation/attribution.md) explains how Sediment
matches Inference calls to commit files. Neither page is a prerequisite for
this procedure.

## Prepare the operator shell

If you haven't verified capture, complete
[Enroll your team](run-pilot.md) first. An empty Fact store produces a valid empty
bundle; it doesn't verify that capture works.

Use the operator shell from [Deploy Sediment on EC2](deploy-ec2.md#set-up-an-operator-shell)
or [Deploy Sediment on your own host](deploy.md#set-up-an-operator-shell).
The shell must have these settings:

| Setting | Purpose |
| --- | --- |
| `SEDIMENT_ORG_ID` | The organization to derive. There is no organization selection flag. |
| `SEDIMENT_DATABASE_URL` | The operator connection to the PostgreSQL Fact store. |
| `SEDIMENT_MIRROR_PATH` | The deployment's Git mirror root. |

Signing in with `sediment login` doesn't configure the database connection.
The command reads PostgreSQL and the mirrors directly.

Create private parent directories for the examples. The bundle writer requires
an existing parent and an unused destination:

```bash
install -d -m 700 "$HOME/sediment-derived" "$HOME/sediment-exports"
```

If your container uses memory-backed `/tmp`, set `TMPDIR` to an existing private,
disk-backed directory before building. The Compose `operator` service already
uses disk-backed staging. Provision space for both staging and the saved bundle.

## Select a cohort

For your first run, use the default scope: every user and all available history
in `SEDIMENT_ORG_ID`.

If you need a subset, add these options to the build command in
[Build a bundle](#build-a-bundle):

| Option | Selection |
| --- | --- |
| `--users alice bob` | Inference calls from the listed users. Replace the examples with captured user identifiers. |
| `--since 2026-08-01T00:00:00Z` | Inference calls observed at or after this instant. |
| `--until 2026-09-01T00:00:00Z` | Inference calls observed before this instant. |

Both time options require a timezone. Sediment selects an Attributed completion
by its Inference call's observation time. It keeps a complete Rollout when at
least one of its Inference calls falls within the interval. If you specify
`--users`, every Inference call user in that Rollout must be listed.

These filters select the output after organization-wide Derivation. They don't
limit the underlying evidence reads or bypass capacity limits.

## Set Derivation policy

Optional: If you need to change a default, save a TOML configuration file as
`derivation-policy.toml`. Include only the fields that you want to override.
This example shows the supported settings and their defaults:

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

If you saved a policy file, add `--policy ./derivation-policy.toml` to the build
command. Otherwise, Sediment uses the defaults. The loader rejects unknown
sections or fields, unsupported schema versions, and out-of-range values.

`eval_fraction = 0.1` assigns about 10% of Sessions to evaluation by hashing
their identifiers. Set it to `0.0` to disable the split. All rows from one
Session stay in the same partition across both artifacts.

If your evaluation requires unseen repositories, prompts, or tasks, define those
boundaries in a versioned benchmark manifest before training. The Session split
doesn't enforce them. [Train and evaluation isolation](../explanation/how-derivation-works.md#train-and-evaluation-isolation)
explains this limitation.

## Build a bundle

Samples can contain prompts, responses, Developer decisions, and CI outcomes.
Run the sample command only in a terminal whose output you can protect. If you
can't protect that output, omit `--sample 2` and review the saved files privately.

Run this command once, adding any cohort and policy options that you selected:

```bash
sediment derive --out "$HOME/sediment-derived/review" --sample 2
```

For later runs, choose another unused destination. Sediment never overwrites a
bundle. The command reports the saved path, artifact counts, diagnostic counts,
policy digest, `as_of`, and file hashes. With `--sample 2`, it also prints up to
two Attributed completions and two Rollouts from that bundle.

The result contains `manifest.json` and six JSON Lines (JSONL) files. The bundle
directory has mode `0700`; each file has mode `0600`. A successful write means
the bundle passed validation. It doesn't mean that the data is ready to train on.

## Inspect the bundle

Open `manifest.json` in the saved bundle. Review it with the sample rows from
the build:

1. Check `org_id`, `scope`, and `as_of` against the evidence that you intended
   to include. `as_of` is the latest visible source timestamp, not the run time
   or the requested `--until` value.
2. Check `policy`, `policy_digest`, `implementation_versions`,
   `quarantine_revision`, and `mirror_revisions`. These identify the inputs
   and behavior behind the result.
3. Review `counts` and every entry in `skipped`, `excluded`, and `fragmented`.
   Resolve unexpected gaps before exporting.
4. Inspect representative Attributed completions and Rollouts. Confirm that
   their Inference calls, Developer decisions, and commit evidence match the
   workflow that you intend to train on. Two sample rows can't establish
   dataset quality.

| Diagnostic | What to check |
| --- | --- |
| `skipped` | Inputs or artifacts that didn't qualify. Missing mirrors can reduce Attribution. `unmatched_decision_call_id` means that no captured Inference call matches the decision's identifier. |
| `excluded` | Artifacts removed by your cohort selection. Confirm that the exclusions match the user and time boundaries that you chose. |
| `fragmented` | Retained Turns whose continuity Sediment couldn't prove. Review `prior_output_absent`, `input_history_changed`, and `prior_output_not_replayed` before treating separate Segments as one trajectory. |

Keep all seven files together. Each JSONL line wraps a canonical record in a
`record_json` string. Use the bundle reader for programmatic inspection and
exports; it validates the complete bundle. Validation proves the bundle's
internal relationships. It can't prove that an external producer supplied a
complete or truthful Fact population.

## Export from the bundle

After review, choose an objective and Evidence recipe in
[Choose a training export](../exports/training-exports.md). For example, export
supervised fine-tuning (SFT) rows from the same bundle:

```bash
sediment export sft \
  --from "$HOME/sediment-derived/review" \
  --out "$HOME/sediment-exports/review-sft"
```

The default SFT Evidence recipe is `sft_curated`. Each objective has its own
eligibility rules, so a nonempty bundle can produce no training rows. Inspect
the export's counts and skip reasons before training. If CI resolution reports
`conflicting_run_identity` or `ambiguous_workflow_verdicts`, investigate the
conflicting evidence before using outcome-derived labels.

Every export validates the bundle first. It stops on invalid content and never
substitutes live Facts. Some objectives also need Git mirrors; the export guide
lists those requirements. Recovery reads Facts directly and doesn't accept a
bundle.

## Compare two policies

If you want to measure a policy change, keep the Fact and mirror inputs fixed.
Two runs on a live deployment can include different evidence even when their
cohort filters match.

Save the policies as `policy-a.toml` and `policy-b.toml`. Build each into an
unused destination:

```bash
sediment derive --policy policy-a.toml --out "$HOME/sediment-derived/policy-a"
sediment derive --policy policy-b.toml --out "$HOME/sediment-derived/policy-b"
```

If you selected a cohort, pass the same cohort options to both commands.
Compare scope, `as_of`, `quarantine_revision`, `mirror_revisions`, and
`implementation_versions` before attributing row differences to the policy.
The policy digests should differ only when the resolved configurations differ.
Compare artifact counts and all diagnostic maps before comparing individual rows.

Matching manifest metadata alone doesn't prove that two runs saw identical
Facts. For a controlled comparison, use the same restored database and mirrors
with capture and mirror updates stopped in that isolated environment.
[Profile reports and Derivations](../../CONTRIBUTING.md#profile-reports-and-derivations)
covers repeatable comparisons.

## Troubleshoot

| Error or result | Action |
| --- | --- |
| `derived bundle parent does not exist` | Create the parent directory with private permissions, then retry. |
| `destination already exists` | Choose an unused path. There is no overwrite flag. |
| `--since must be timezone-aware` | Add `Z` or a numeric timezone offset. |
| `unknown derivation policy field` | Check the field against the supported policy settings. |
| `SEDIMENT_MIRROR_PATH is not set` | Set the deployment's mirror root in the operator shell. |
| `unsupported bundle_schema_version` | Rebuild from Facts into an unused destination. Bundle versions 1–3 require recomputation. |
| `Inference call identity population exceeds 50000` | Request a supported capacity change as described in this section. |
| Empty artifact counts | Check capture, cohort selection, and `skipped`. An empty organization produces a valid empty bundle. |

The bundle carries complete organization populations for Inference call
identities, repository identities, and Repository renames. Each population has
an independent 50,000-row cap. A narrower cohort doesn't reduce those populations.
Don't trim identity files, quarantine valid Facts, or split an organization to
bypass the cap. [Open an issue](https://github.com/sediment-ai/sediment/issues)
with the sanitized failed command and error. Include `as_of` if the run reported it.

If the command stops before reporting success, inspect the destination before
retrying. If it exists, validate it with `open_derived_bundle` before use;
existence alone doesn't establish validity. If it doesn't exist, retry with an
unused destination. An interrupted writer can leave a private directory named
`.<destination>.<suffix>`. Remove only the directory owned by that writer after
confirming that its process has ended.

## Use the bundle from Python

Optional: In a Python environment with `sediment_export` installed, use
`open_derived_bundle` to validate and inspect a saved bundle without rebuilding it:

```python
from pathlib import Path

from sediment_export import open_derived_bundle

with open_derived_bundle(Path.home() / "sediment-derived" / "review") as bundle:
    print(bundle.org_id, bundle.as_of)
    print(dict(bundle.skipped))
```

Keep the context open until you finish reading or projecting its records.
`build_derived_bundle_context` provides the same context lifetime when building
from Facts. For small bundles, `build_derived_bundle` and `read_derived_bundle`
load up to 64 MiB of encoded payload into memory by default.
