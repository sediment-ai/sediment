# Export RLVR tasks and trajectories

Export captured Rollouts as task rows, trajectory rows, or both for
reinforcement learning from verifiable rewards (RLVR). This procedure is for
operators who need to audit trajectories or prepare a downstream dataset.
The exporter doesn't run a Verifier or fill missing evidence.

1. [Choose a target](#choose-a-target).
2. [Prepare the inputs](#prepare-the-inputs).
3. Optional: [Configure verification](#configure-verification).
4. [Export one target](#export-one-target).
5. [Inspect the output](#inspect-the-output).

## Choose a target

| Target | Files | Eligibility | Consumer boundary |
|---|---|---|---|
| `sediment` | `tasks.jsonl`, `rollouts.jsonl`, optional `environment.yaml` | tasks require a recorded pass or failure; Rollout segments require captured turns | Sediment training projection |
| `swe-bench` | `tasks.jsonl` | recorded terminal pass and resolvable Reference patch | SWE-bench task shape |
| `nemo-gym` | `rollouts.jsonl` | captured Rollout segment; Reward remains absent without pass/fail evidence | NeMo Gym rollout boundary mapping |

`--target` is required. These are Sediment's canonical projections; a target
name alone doesn't establish downstream compatibility. If you need a qualified
SWE-bench or NeMo Gym release, use
[Export for a consumer](consumer-compatibility.md) after choosing the target.

Every emitted row names version 1 of the `rlvr_ci` Evidence recipe. A resolved
CI pass records `reward_source: resolved_ci_pass`. A resolved CI failure
records `reward_source: resolved_ci_fail`. A non-verdict omits `reward_source`
and doesn't create a directional label or fractional Reward.

## Prepare the inputs

[Build and review a bundle](../operate/run-derivations.md), then
[prepare a private output parent](training-exports.md#prepare-private-destinations).
The examples use `$HOME/sediment-derived/review`.

For `sediment` or `swe-bench`, set `SEDIMENT_MIRROR_PATH` to the deployment's
mirror root. The mirrors must contain the attributed commits, their parents,
and the exact verified commit. The bundle freezes Rollouts, not Git patches.
For `nemo-gym` with `--from`, no mirror or database connection is required.

If you omit `--from`, [configure direct Fact access](training-exports.md#export-directly-from-facts).
Every direct RLVR target needs database and mirror access.

## Configure verification

Verification configuration tells a consumer how a Verifier can run again. A
Verifier result is the exact recorded `CIOutcome` Fact. Every target keeps
these values separate.

If a repository has a known verification command, save it in
`$HOME/sediment-exports/verifier-commands.toml`. Replace `owner/repo` and the
example command with that repository's verified values:

```toml
[repos."owner/repo"]
verification_command = "pytest -q"
```

Set the configuration path:

```bash
export SEDIMENT_VERIFIER_COMMANDS_FILE="$HOME/sediment-exports/verifier-commands.toml"
```

If the setting or repository entry is absent, the projection omits
`verification`. The projection retains recorded Verifier results. Sediment
never derives a command from workflow YAML, and it never runs the configured
command.

If you need the Sediment target's experimental `environment.yaml`, set its
[operator-supplied runtime inputs](#interpret-the-experimental-environment-manifest)
before exporting. That manifest doesn't establish an executable environment.

## Export one target

Run the command for the target you selected. Each example uses a different,
unused destination:

```bash
sediment export rlvr --target sediment \
  --from "$HOME/sediment-derived/review" \
  --out "$HOME/sediment-exports/review-sediment"
```

```bash
sediment export rlvr --target swe-bench \
  --from "$HOME/sediment-derived/review" \
  --out "$HOME/sediment-exports/review-swe-bench"
```

```bash
sediment export rlvr --target nemo-gym \
  --from "$HOME/sediment-derived/review" \
  --out "$HOME/sediment-exports/review-nemo-gym"
```

For later runs, choose another unused directory. Don't rebuild the reviewed
bundle as part of the export step.

## Inspect the output

The command prints `rollouts derived`, `task rows`, `rollout rows`, separate
skip maps, `fragmented`, and the written paths.

1. Check the selected target's row counts. A nonempty Rollout population can
   produce no tasks when Verifier or patch evidence is missing.
2. Inspect recorded Verifier results separately from their resolved verdict,
   Reward, and reliability. A numeric Reward alone doesn't establish reliable
   evidence.
3. Review `fragmented` before treating separate Segments as one trajectory.
   The counts describe retained continuity boundaries, not excluded rows.
4. Review target-specific skips and
   [audit the split](training-exports.md#audit-the-split-before-training).

With the default evaluation split, the writer produces
`<name>.train.jsonl` and `<name>.eval.jsonl`. Otherwise, it produces one
`<name>.jsonl`. An empty row list doesn't create or truncate a file.

The built-in split is Session-grained. It keeps every segment and task derived
from one Session on one side. It doesn't isolate repositories, exact prompts,
or external task identifiers across Sessions.

If a benchmark uses task-keyed isolation, audit both files against the
versioned task manifest before training or evaluation.

Use a fresh output directory for each export to keep task, Rollout, split,
and manifest files from different runs separate.

The first export writes a hidden `.sediment-rlvr-target` claim in its output
directory. If the directory contains an RLVR artifact, another export fails
before writing, including an export for the same target. A directory with
unclaimed RLVR artifacts also fails closed.

An empty export can reuse its claimed directory because it wrote no artifact.
A hidden generation lock rejects concurrent exports for the same directory.
If a stopped process leaves the lock behind, the directory remains
fail-closed. Use a fresh directory for the next export.

## Understand target contracts

Compare repeated exports within one mode by JSONL bytes and row order. Across
direct and bundle exports, compare parsed values and order: bundles sort nested
keys while direct exports retain source order.

Inspect representation skips such as `non_finite_number` and
`unrepresentable_unicode`. Each declined task or Segment counts once.
File replacement is atomic per file, not across all partitions. If publication
fails or stops, retain its diagnostics and export to a fresh directory. Empty
partitions don't clear existing files.

Validate rows against the [Schema reference](../reference/schema.md) before
consumer adaptation. A target contract doesn't establish compatibility with
an external trainer release; use a qualified
[consumer profile](consumer-compatibility.md). Canonical schema, Evidence recipe,
and consumer-profile versions identify separate contracts.

## Read Sediment target rows

The target writes [task rows](../reference/schema.md#sedimenttaskrow) and
[Rollout rows](../reference/schema.md#sedimentrolloutrow). Task rows preserve a
passing or failing historical patch, recorded Verifier results, CI resolution,
Attribution source, observation IDs, split, and Provenance.

The Reference patch starts at the parent of the first attributed commit in the
selected CI resolution's repository and ends at its exact verified commit.
Later unverified commits and commits in other repositories don't extend it.

### Resolve attempts

Inspect `ci_resolution` alongside the exact `verifier_results`.
[CI evidence and verdicts](../explanation/how-derivation-works.md#ci-evidence-and-verdicts)
explains attempt ordering, workflow conflicts, and suspected flakes.

A pass after a failure, or a failure after a pass, can carry a categorical
verdict with default reliability 0.0. NeMo Gym Reward remains 1.0, 0.0, or absent;
reliability doesn't change that numeric value. If you weight or filter training
rows by reliability, define that downstream rule explicitly.

### Read task prompts

Task exporters retain each canonical `TextPart.content` verbatim, including
JSON-looking examples, whitespace, and Unicode. They join explicit parts with
newlines and mark each non-text part as `[non-text content omitted]`.
They don't decode text as legacy provider content.

In canonical `rollouts.jsonl`, each Turn preserves typed input messages in
`new_messages`. Its `completion` contains scoring text: joined output text and
tool-argument string values. That field omits readable reasoning and output
part boundaries. Full captured responses remain in the bundle's Inference calls.
If you need those responses in NeMo's native format, use the
[NeMo consumer profile](consumer-compatibility.md#export-nemo-gym-rollouts).

### Read Rollout rows

Rollout implementation version 4 continues a Segment only when typed input
replays the prior input and captured output. Missing or contradictory echoes,
rewritten input, missing output, and unsupported message regrouping retain the
Turn in a separate Segment. The Derivation counts these boundaries in
`fragmented`, separately from projection skips.

Each retained Rollout Segment becomes one row. The row preserves Session identifiers,
segment index, structured messages, completion text, tool calls, decisions,
split, Provenance, and exact `verifier_results`.

Session-terminal Verifier results repeat on each segment because Sediment has
no Fact that assigns a result to one segment. `ci_resolutions` carries the
recomputed commit-level interpretations. `ci_resolution` identifies the
resolution that supplied `reward_source`, including its reliability.

Check these task exclusions when diagnosing missing output:

| Reason | Meaning |
|---|---|
| `conflicting_run_identity` | One provider run ID carries conflicting repository, commit, branch, or workflow identity. |
| `ambiguous_workflow_verdicts` | Workflow lineages on one commit disagree on pass versus failure. |
| `no_attributed_commit` | The Rollout has no attributed commit. |
| `missing_verifier_evidence` | The Rollout has no recorded terminal pass or failure. |
| `mirror_absent` | The Verifier-result repository has no local mirror. |
| `no_commit_in_verifier_repo` | No attributed commit resolves in that repository mirror. |
| `degenerate_commit_range` | The first commit isn't an ancestor of the last commit. |
| `root_commit_no_base` | The first attributed commit has no parent. |
| `reference_patch_unavailable` | A mirror ancestry, parent, or diff read failed. |

Sediment Rollout rows add `no_segments` to the shared CI-resolution,
repository-identity, and representation exclusions.

## Read SWE-bench target rows

See [SWEBenchTaskRow](../reference/schema.md#swebenchtaskrow) for the complete
shape. The row carries repository, base commit, problem statement, patch, and
Sediment evidence in `metadata`.

The target populates `patch` only from a recorded terminal pass. It omits issue
identifiers, test patches, versions, `FAIL_TO_PASS`, and `PASS_TO_PASS` because
Sediment Facts don't contain them.

SWE-bench adds `failed_reference_patch` to the Sediment task skip vocabulary.
It counts that reason when the recorded terminal Verifier result is a failure.
This count remains distinct from `missing_verifier_evidence`.

## Read NeMo Gym target rows

Each captured Segment maps to a [NemoGymRolloutRow](../reference/schema.md#nemogymrolloutrow)
with `responses_create_params`, `response`, optional `reward`, and evidence in
`metadata`.

A resolved pass maps to numeric Reward `1.0`. A resolved failure maps to
`0.0`. Without a resolved pass or failure, the row omits both `reward` and
`reward_source`.

Metadata retains every exact terminal Verifier result, including non-label
outcomes. It keeps the selected `ci_resolution` separate from optional
Verification configuration. The target doesn't infer a NeMo Gym resource
server or executable environment. Its skips include `no_segments` and the
shared CI-resolution, repository-identity, and representation exclusions.

This mapping names [NeMo Gym's rollout
boundary](https://github.com/NVIDIA-NeMo/Gym), but it doesn't claim direct
compatibility. The canonical target retains Turn input messages, scoring text,
and tool calls. The [NeMo consumer profile](consumer-compatibility.md#export-nemo-gym-rollouts)
loads full output messages from the referenced Inference calls and validates
the upstream response format.

## Interpret the experimental environment manifest

Only the Sediment target can write `environment.yaml`. The document contains
`experimental: true` because its OpenEnv shape follows a proposal rather than
a compatibility-tested contract. SWE-bench and NeMo Gym exports never write
this file.

The manifest can include an informational CI workflow reference when every
task agrees on one workflow. It includes `frameworks.openenv` only from
`SEDIMENT_OPENENV_IMAGE` or `SEDIMENT_OPENENV_PACKAGE`. It includes
`frameworks.nemo_gym` only from an operator-supplied
`SEDIMENT_NEMO_GYM_RESOURCES_SERVER`, with optional
`SEDIMENT_NEMO_GYM_CONFIG`. It never derives either runtime from workflow YAML.

The OpenEnv field mapping documents intent, not compatibility. An OpenEnv
dataset needs a runtime that implements the expected protocol. The
Verifier-result-to-Reward mapping doesn't provide a
[Verifiers](https://github.com/PrimeIntellect-ai/verifiers) taskset or Verifier
implementation.

## Understand capture limits

Rollout rows require captured Inference calls. The supported capture path for
those Facts is the gateway. OpenTelemetry-only capture
can produce decisions and CI Facts without a trajectory. Copilot model calls
can't route through the Sediment gateway, so Copilot contributes decisions but
not Rollouts.

For field-level types, see the generated [Schema reference](../reference/schema.md).
For Python use, start with the [validated bundle reader](../operate/run-derivations.md#use-the-bundle-from-python)
and `sediment_export.export_rlvr_from_bundle`. Keep the reader context open
through projection.
