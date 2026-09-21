# Export RLVR tasks and trajectories

For a specific downstream release, select a versioned consumer profile.
[Export for a consumer](consumer-compatibility.md) documents installation,
configuration, exact commands, and the limits of each support claim.

Run `sediment export rlvr --target <target> --out <directory>` to project
Sediment Rollouts into reinforcement learning from verifiable rewards (RLVR)
rows. The command writes tasks, Rollouts, or both according to the selected
target. Sediment supports its audit format, SWE-bench tasks, and NeMo Gym
rollouts. The command requires an explicit target because RLVR has no universal
interchange schema.

For shared preparation, schema inspection, and holdout guidance, see [Choose a
training export](training-exports.md).

## Choose a target

| Target | Files | Eligibility | Consumer boundary |
|---|---|---|---|
| `sediment` | `tasks.jsonl`, `rollouts.jsonl`, optional `environment.yaml` | tasks require a recorded pass or failure; Rollout segments require captured turns | Sediment training projection |
| `swe-bench` | `tasks.jsonl` | recorded terminal pass and resolvable Reference patch | SWE-bench task shape |
| `nemo-gym` | `rollouts.jsonl` | captured Rollout segment; Reward remains absent without pass/fail evidence | NeMo Gym rollout boundary mapping |

Each target is a stateless projection over canonical Rollouts, as [ADR
0004](../adr/0004-canonical-training-artifacts.md) requires. The projection
reads Rollouts and local mirrors. It doesn't persist derived state, infer
missing evidence, or run a Verifier.

Every emitted row names version 1 of the `rlvr_ci` Evidence recipe. A resolved
CI pass records `reward_source: resolved_ci_pass`. A resolved CI failure
records `reward_source: resolved_ci_fail`. A non-verdict omits `reward_source`
and doesn't create a directional label or fractional Reward.

## Export one target

Configure `SEDIMENT_MIRROR_PATH`, then mirror each repository that can
contribute a Reference patch.

Run one of these commands:

```bash
sediment export rlvr --target sediment --out /data/export/sediment
sediment export rlvr --target swe-bench --out /data/export/swe-bench
sediment export rlvr --target nemo-gym --out /data/export/nemo-gym
```

If you reviewed a derived bundle, pass it with `--from`:

```bash
sediment derive --out /data/derived/review
sediment export rlvr \
  --target sediment \
  --from /data/derived/review \
  --out /data/export/sediment-reviewed
```

The bundle freezes canonical Rollouts. The `sediment` and `swe-bench` targets
still read local mirrors for Reference patches, so they require
`SEDIMENT_MIRROR_PATH` even from a bundle. The `nemo-gym` target projects
purely from captured segments, so `sediment export rlvr --target nemo-gym
--from <bundle>` doesn't read a mirror and runs with `SEDIMENT_MIRROR_PATH`
unset. [Run derivations](../operate/run-derivations.md) covers the review
workflow.

A Reference patch ends at the attributed commit named by its exact Verifier
result. Later attributed commits without Verifier evidence never extend that
patch.

### Export from Python

As a library caller, pass the target explicitly:

```python
from sediment_export import export_rlvr_from_rollouts

summary = export_rlvr_from_rollouts(
    bundle.rollouts,
    mirrors,
    "out",
    target="sediment",
    split_enabled=bundle.policy.eval_fraction > 0,
)
```

## Inspect the output

If evaluation splitting is enabled, the writer produces
`<name>.train.jsonl` and `<name>.eval.jsonl`. Otherwise, it produces one
`<name>.jsonl`. An empty row list doesn't create or truncate a file.

The built-in split is Session-grained. It keeps every segment and task derived
from one Session on one side. It doesn't isolate repositories, exact prompts,
or external task identifiers across Sessions.

If a benchmark uses task-keyed isolation, audit both files against the
versioned task manifest before training or evaluation.

**Warning:** Use a fresh output directory for each export. Stale split, task,
Rollout, or manifest files could otherwise create a mixed-generation consumer
contract.

The first export writes a hidden `.sediment-rlvr-target` claim in its output
directory. If the directory contains an RLVR artifact, another export fails
before writing, including an export for the same target. A directory with
unclaimed RLVR artifacts also fails closed.

An empty export can reuse its claimed directory because it wrote no artifact.
A hidden generation lock rejects concurrent exports for the same directory.
If a stopped process leaves the lock behind, the directory remains
fail-closed. Use a fresh directory for the next export.

## Understand target contracts

All targets preserve deterministic row order by organization and Session.
Repeated exports of the same Rollouts and mirror state produce identical JSONL
bytes. Shuffling input Rollouts doesn't change the output.

A direct export and a bundle-backed export of the same Derivation produce the
same parsed rows in the same order. Their bytes can differ: the canonical
bundle stores nested objects, such as a Verifier result's `raw` payload, with
sorted keys, while a direct export keeps the key order of the source Fact.
Each mode keeps its own historical bytes. Compare exports across modes by
parsed JSON value and row order; compare bytes only within one mode.

Inspect `non_finite_number` and `unrepresentable_unicode` skip counts for values
that a target cannot represent, including nested Verifier results and metadata.
Each declined task or Segment row counts once. A task's omitted completion
content doesn't affect eligibility. JSONL uses ASCII escapes and strict numbers.
If serialization fails, every existing split partition remains intact because
the writer prepares all nonempty partitions before replacing any destination.
Each replacement is atomic; the sequence of replacements isn't a transaction.
A failure between replacements can leave complete files from different exports.
After correcting the failure, replay the same ordered rows to restore exact
output bytes and row counts. Empty partitions preserve their prior files.
Process interruption tests verify this recovery, not power-loss durability.

Each row identifies its canonical JSON Schema Draft 2020-12 contract. Sediment
task and Rollout rows carry `schema_id` and `schema_version` at the top level.
SWE-bench and NeMo Gym rows keep them in Sediment-owned `metadata`, outside
trainer inputs. The generated [Schema reference](../reference/schema.md) comes
from the same dataclasses.

Sediment defines a closed, recursive row contract for each target. The test
suite decodes every emitted row and checks every required key, nested object,
value type, enum value, and optional-key omission against that contract.

This validation guarantees Sediment's named target contract. It doesn't claim
that every release of an external trainer accepts the row unchanged. Before a
downstream adapter removes metadata, validate and record the input contract.
Consumer compatibility requires a versioned adapter and a test against the
consumer's loader. A target name alone can't provide that guarantee.

The compatibility-profile version, Evidence recipe version, and canonical
schema version identify separate contracts. Recipe, Reward source, numeric
Reward, CI reliability, exact Verifier results, Verification configuration,
and environment claims also remain separate.

## Configure verification

Verification configuration tells a consumer how a Verifier can run again. A
Verifier result is the exact recorded `CIOutcome` Fact. Every target keeps
these values separate.

If a repository has a known verification command, create a TOML file:

```toml
[repos."owner/repo"]
verification_command = "pytest -q"
```

Set the configuration path:

```bash
export SEDIMENT_VERIFIER_COMMANDS_FILE=/etc/sediment/verifier-commands.toml
```

If the setting or repository entry is absent, the projection omits
`verification`. The projection retains recorded Verifier results. Sediment
never derives a command from workflow YAML, and it never runs the configured
command.

Writers and readers use `VerifierCommands`, `verification_command`, and
`SEDIMENT_VERIFIER_COMMANDS_FILE`.

## Read Sediment target rows

The Sediment target writes task and Rollout rows. A task row retains a passing
or failing historical patch as `reference_patch`. It also keeps exact Verifier
evidence:

```json
{
  "instance_id": "acme-session-abc1234",
  "recipe_id": "rlvr_ci",
  "recipe_version": 1,
  "reward_source": "resolved_ci_pass",
  "repo": "owner/repo",
  "base_commit": "0123456789abcdef",
  "problem_statement": "Fix cash rounding",
  "reference_patch": "diff --git a/app.py b/app.py\n...",
  "verification": {"verification_command": "pytest -q"},
  "verifier_results": [
    {"outcome_id": "<uuid>", "result": "passed", "run_attempt": 2}
  ],
  "ci_resolution": {
    "repo": "owner/repo",
    "commit_sha": "abcdef0123456789",
    "verdict": "passed",
    "reliability": 1.0,
    "suspected_flake": false,
    "source_outcome_ids": ["<uuid>"]
  },
  "attribution_source": "git_notes",
  "session_commit_observation_ids": [],
  "split": "train",
  "provenance": {
    "policy_version": "4",
    "quarantine_revision": 0,
    "policy_digest": null
  },
  "repository_identity": null,
  "schema_id": "https://sediment.so/schemas/training-rows/sediment-rlvr-task/v3.json",
  "schema_version": 3
}
```

| Field | Source |
|---|---|
| `instance_id` | `<org_id>-<session_id>-<first attributed commit SHA, 7 hex>` |
| `recipe_id` | the closed `rlvr_ci` Evidence recipe |
| `recipe_version` | `1`; incremented when the recipe semantics change |
| `reward_source` | `resolved_ci_pass` or `resolved_ci_fail` from the selected CI resolution |
| `repo` | the repository of the selected CI resolution |
| `base_commit` | `mirror.parent_commit(first attributed commit)` — the checkout point that the patch applies to |
| `problem_statement` | the first user message, with canonical text retained verbatim and explicit non-text parts marked |
| `reference_patch` | `mirror.diff_range(base_commit, verifier commit)` |
| `verification` | optional operator-supplied Verification configuration |
| `verifier_results` | exact recorded `CIOutcome` Facts |
| `ci_resolution` | the recomputed verdict, reliability, source identifiers, and Provenance |
| `attribution_source` | Inferred Rollout binding method: `git_notes` or `jaccard`; neither proves individual-call authorship |
| `session_commit_observation_ids` | Sorted source observation IDs matching emitted CI resolution commits and the Rollout Session; empty when absent |
| `split` | the Rollout's `train`/`eval` assignment |
| `provenance` | structured `policy_version`, integer `quarantine_revision`, and nullable full `policy_digest` |

The task uses the selected CI resolution's repository and commit. Its reference
patch starts at the parent of the first attributed commit in that repository
and ends at the exact verified commit. Commits from another repository don't
produce a cross-history diff.

### Resolve attempts

Outcomes group by `(org_id, provider, run_id)`. The resolver orders them only
by provider `run_attempt`. Null sorts as attempt 0
when mixed with numbered attempts. Capture time, ingest order, URL, and
generated IDs never select a verdict.

The last ordered `passed` or `failed` result supplies the workflow verdict.
Non-verdicts remain in `verifier_results` without creating Reward. Conflicting
workflow verdicts produce no aggregate Reward and count
`ambiguous_workflow_verdicts`. Agreeing workflows use minimum reliability.

A fail-to-pass retry therefore resolves to `passed` with
`suspected_flake: true` and default `reliability: 0.0`. A pass-to-fail retry
resolves to `failed` with the same reliability treatment. NeMo Gym Reward
remains exactly 1.0, 0.0, or absent. When consumers choose a downstream sample
weight, they must honor reliability metadata. A categorical verdict beside
zero reliability is intentional. `verifier_results` preserves every recorded
`CIOutcome` exactly.

### Read task prompts

Task exporters retain each canonical `TextPart.content` verbatim, including
JSON-looking examples, whitespace, and Unicode. They join explicit parts with
newlines and mark each non-text part as `[non-text content omitted]`.
They don't decode text as legacy provider content. Turns in `rollouts.jsonl`
keep the full typed message structure.

### Read Rollout rows

Rollout implementation version 4 continues a Segment only when typed input
replays the prior input and captured output. Missing or contradictory echoes,
rewritten input, missing output, and unsupported message regrouping retain the
Turn in a separate Segment. The Derivation counts these boundaries in
`fragmented`, separately from projection skips.

Each Rollout segment becomes one row. The row preserves Session identifiers,
segment index, structured messages, completion text, tool calls, decisions,
split, Provenance, and exact `verifier_results`.

Session-terminal Verifier results repeat on each segment because Sediment has
no Fact that assigns a result to one segment. `ci_resolutions` carries the
recomputed commit-level interpretations. `ci_resolution` identifies the
resolution that supplied `reward_source`, including its reliability.

```json
{
  "instance_id": "acme-session-seg0",
  "recipe_id": "rlvr_ci",
  "recipe_version": 1,
  "reward_source": "resolved_ci_pass",
  "org_id": "acme",
  "session_id": "session",
  "segment_index": 0,
  "turns": [
    {
      "inference_call_id": "call-1",
      "new_messages": [
        {"role": "user", "parts": [{"type": "text", "content": "Fix it"}]}
      ],
      "completion": "I will update the function.",
      "tool_calls": [],
      "decisions": []
    }
  ],
  "verifier_results": [
    {"outcome_id": "<uuid>", "org_id": "acme-corp", "provider": "github_actions",
     "run_id": "12345", "run_attempt": 2, "workflow_id": "987",
     "provider_result": "success", "result": "passed", "repo": "owner/repo",
     "commit_sha": "...", "branch": "main", "workflow_name": "CI",
     "workflow_path": ".github/workflows/ci.yml", "run_url": "https://...",
     "error_type": null, "reason": null,
     "source_event_type": "github.workflow_run.completed",
     "source_spec_version": null, "source_event_id": null,
     "pr_number": null, "captured_at": "...", "raw": {}}
  ],
  "ci_resolutions": [
    {"repo": "owner/repo", "commit_sha": "...", "verdict": "passed",
     "reliability": 1.0, "suspected_flake": false,
     "source_outcome_ids": ["<uuid>"], "workflow_resolutions": ["..."]}
  ],
  "ci_resolution": {
    "repo": "owner/repo", "commit_sha": "...", "verdict": "passed",
    "reliability": 1.0, "source_outcome_ids": ["<uuid>"]
  },
  "attribution_source": "git_notes",
  "session_commit_observation_ids": [],
  "split": "train",
  "provenance": {
    "policy_version": "4",
    "quarantine_revision": 0,
    "policy_digest": null
  },
  "repository_identity": null,
  "schema_id": "https://sediment.so/schemas/training-rows/sediment-rlvr-rollout/v4.json",
  "schema_version": 4
}
```

Sediment task skip reasons form a closed vocabulary:

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

Sediment Rollout rows use `conflicting_run_identity`,
`ambiguous_workflow_verdicts`, and `no_segments`.

## Read SWE-bench target rows

The SWE-bench target writes the observed task fields that Sediment can support:

```json
{
  "instance_id": "acme-session-abc1234",
  "repo": "owner/repo",
  "base_commit": "0123456789abcdef",
  "problem_statement": "Fix cash rounding",
  "patch": "diff --git a/app.py b/app.py\n...",
  "metadata": {
    "recipe_id": "rlvr_ci",
    "recipe_version": 1,
    "reward_source": "resolved_ci_pass",
    "verification": {"verification_command": "pytest -q"},
    "verifier_results": [
      {"outcome_id": "<uuid>", "result": "passed", "run_attempt": 2}
    ],
    "ci_resolution": {
      "repo": "owner/repo",
      "commit_sha": "abcdef0123456789",
      "verdict": "passed",
      "reliability": 1.0
    },
    "attribution_source": "git_notes",
  "session_commit_observation_ids": [],
    "split": "train",
    "provenance": {
      "policy_version": "4",
      "quarantine_revision": 0,
      "policy_digest": null
    },
    "repository_identity": null,
    "schema_id": "https://sediment.so/schemas/training-rows/swe-bench-task/v3.json",
    "schema_version": 3
  }
}
```

The target populates `patch` only from a recorded terminal pass. It omits issue
identifiers, test patches, versions, `FAIL_TO_PASS`, and `PASS_TO_PASS` because
Sediment Facts don't contain them.

SWE-bench adds `failed_reference_patch` to the Sediment task skip vocabulary.
It counts that reason when the recorded terminal Verifier result is a failure.
This count remains distinct from `missing_verifier_evidence`.

## Read NeMo Gym target rows

The NeMo Gym target maps each captured segment to the required boundary names:

```json
{
  "responses_create_params": {
    "input": [
      {"role": "user", "parts": [{"type": "text", "content": "Fix it"}]}
    ]
  },
  "response": {"turns": []},
  "reward": 1.0,
  "metadata": {
    "instance_id": "acme-session-seg0",
    "recipe_id": "rlvr_ci",
    "recipe_version": 1,
    "reward_source": "resolved_ci_pass",
    "org_id": "acme",
    "session_id": "session",
    "segment_index": 0,
    "verification": {"verification_command": "pytest -q"},
    "verifier_results": [{"outcome_id": "<uuid>", "result": "passed"}],
    "ci_resolution": {
      "verdict": "passed",
      "reliability": 1.0,
      "source_outcome_ids": ["<uuid>"]
    },
    "attribution_source": "git_notes",
  "session_commit_observation_ids": [],
    "split": "train",
    "provenance": {
      "policy_version": "4",
      "quarantine_revision": 0,
      "policy_digest": null
    },
    "repository_identity": null,
    "schema_id": "https://sediment.so/schemas/training-rows/nemo-gym-rollout/v4.json",
    "schema_version": 4
  }
}
```

A resolved pass maps to numeric Reward `1.0`. A resolved failure maps to
`0.0`. Without a resolved pass or failure, the row omits both `reward` and
`reward_source`.

Metadata retains every exact terminal Verifier result, including non-label
outcomes. It keeps the selected `ci_resolution` separate from optional
Verification configuration. The target doesn't infer a NeMo Gym resource
server or executable environment. Its closed skip vocabulary contains
`conflicting_run_identity`, `ambiguous_workflow_verdicts`, and `no_segments`.

This mapping names [NeMo Gym's rollout
boundary](https://github.com/NVIDIA-NeMo/Gym), but it doesn't claim direct
compatibility. Sediment messages and captured responses remain canonical
evidence, not fabricated OpenAI response objects.

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

Rollout rows require gateway-routed inference calls. OpenTelemetry-only capture
can produce decisions and CI Facts without a trajectory. Copilot model calls
can't route through the Sediment gateway, so Copilot contributes decisions but
not Rollouts.

For field-level types, see the generated [Schema
reference](../reference/schema.md).
