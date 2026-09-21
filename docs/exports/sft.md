# Export SFT and diff-SFT rows

For a specific downstream release, select a versioned consumer profile.
[Export for a consumer](consumer-compatibility.md) documents installation,
configuration, exact commands, and the limits of each support claim.

Run the default supervised fine-tuning (SFT) and diff-shaped SFT (diff-SFT)
exports:

```bash
sediment export sft --out /data/export
sediment export diff-sft --out /data/export
```

The commands write `sft.jsonl` and `diff_sft.jsonl`, or split train and eval
files. Both select `sft_curated` version 1 by default. For shared prerequisites,
reviewed-bundle steps, output inspection, and holdout guidance, see [Choose a
training export](training-exports.md).

## Choose an Evidence recipe

SFT and diff-SFT use the same selected Evidence recipe and `SFTPolicy`.
Diff-SFT doesn't redefine eligibility.

`sft_curated` version 1 is the default. It admits a human-explicit accept or an
accepted edit with `edit_retention_score >= 0.8`. A human-explicit reject,
abandonment, or resolved workflow failure vetoes eligibility. The veto includes
an ambiguous aggregate with one failing workflow. A resolved CI pass can affect
Confidence and CI reliability, but it can't create curated eligibility.

To admit only completions with a clean resolved CI pass, select the verified
recipe:

```bash
sediment export sft --recipe sft_verified --out /data/export
sediment export diff-sft --recipe sft_verified --out /data/export
```

`sft_verified` version 1 requires explicit selection. It admits only a clean
resolved CI pass. Ambiguous verdicts, non-verdict-only evidence, and suspected
flakes don't create eligibility.

Both recipes require a resolved Confidence of at least
`SFTPolicy.min_confidence`, which defaults to 0.6.

## Export SFT rows

The SFT export writes at most one row per inference call that the selected
recipe says is safe to imitate.

An inference call can have one eligible Attributed completion per attributed
file. The projection keeps the one with the highest Confidence, then uses the
repository-qualified evidence identity to break ties. It counts identical extra
copies as `duplicate_completion`. If copies of one complete evidence identity
carry conflicting payloads, the projection declines the entire Inference call
once under `conflicting_evidence`. A higher-ranked sibling cannot hide the
conflict. This includes conflicts in split, Provenance, and source Fact content.

### Read an SFT row

| Field | Meaning |
|---|---|
| `prompt` | trainer-facing request messages; earlier assistant responses remain here, outside the loss boundary |
| `completion` | the latest response-message list and the only SFT target |
| `tools` | tool definitions; empty because inference-call version 1 doesn't carry definitions |
| `metadata` | organization, source model, completion ID, recipe ID and version, eligibility source, label Confidence, CI reliability, Provenance, and split |

Example SFT row:

```json
{
  "prompt": [
    {"role": "user", "content": "Inspect the file."},
    {"role": "assistant", "tool_calls": [{"id": "tool-1", "type": "function", "function": {"name": "Read", "arguments": {"path": "app.py"}}}]},
    {"role": "tool", "name": "Read", "tool_call_id": "tool-1", "content": "def run(): ..."},
    {"role": "user", "content": "Fix it."}
  ],
  "completion": [{"role": "assistant", "thinking": "Keep the signature.", "content": "Implemented."}],
  "tools": [],
  "metadata": {
    "org_id": "acme", "source_model": "model-name", "completion_id": "call-1",
    "recipe_id": "sft_curated", "recipe_version": 1,
    "eligibility_source": "explicit_accept",
    "label_confidence": 0.9,
    "ci_reliability": 1.0,
    "provenance": {"policy_version": "5", "quarantine_revision": 0, "policy_digest": null},
    "attribution_source": "jaccard",
    "session_commit_observation_ids": [],
    "split": "train",
    "repository_identity": null,
    "schema_id": "https://sediment.so/schemas/training-rows/sft-sample/v3.json",
    "schema_version": 3
  }
}
```

The shared canonical-to-trainer mapper keeps text in `content` and readable
reasoning in assistant `thinking`. It keeps tool calls in structured
`tool_calls` and tool responses in `tool` messages. Reasoning never enters
visible `content`. Tool arguments remain objects. OpenAI Responses `developer`
prompt messages retain their role and ordered text. Tool results in native user
messages retain their position; strings remain verbatim and structured JSON
values become deterministic JSON strings. The mapper doesn't call
`render_scoring_text`.

### Interpret skipped SFT inputs

The SFT export reports every skipped input under this closed vocabulary:

- `abandoned`
- `explicit_reject`
- `resolved_ci_failure`
- `no_eligibility_source`
- `unreliable_ci_resolution`
- `conflicting_run_identity`
- `ambiguous_workflow_verdicts`
- `no_reward_signal`
- `below_confidence_floor`
- `inference_call_not_found`
- `model_absent`
- `duplicate_completion`
- `conflicting_evidence`
- `completionless`
- `duplicate_tool_call_id`
- `empty_message`
- `non_string_tool_result`
- `unrepresentable_part_order`
- `unresolved_tool_call`
- `unsupported_completion_role`
- `unsupported_message_role`
- `unsupported_role_part`

## Export diff-SFT rows

The diff-SFT projection writes one row per `(inference call, commit)` group and
reads the concrete per-file edits from the mirror. It never synthesizes a diff.

The projection skips abandonment rows before grouping or mirror access. If any
remaining member is ineligible or explicitly rejected, the projection skips
the whole group. Group Confidence is the `min()` across members.

### Read a diff-SFT row

| Field | Meaning |
|---|---|
| `prompt` | the trainer-facing request messages |
| `completion` | one assistant message whose `content` is the exact selected unified patch |
| `tools` | tool definitions; empty because inference-call version 1 doesn't carry definitions |
| `metadata` | organization, Session, repo, commit, source model, completion ID, recipe ID and version, eligibility source, label Confidence, CI reliability, Provenance, split, and source Fact IDs |

The `completion` value uses this shape, with the mirror text preserved exactly:

```json
[{"role": "assistant", "content": "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n"}]
```

The projection selects each attributed file's complete patch section from the
mirror bytes and concatenates sections in the commit diff's order. If any
attributed file has no patch section, the projection skips the whole sample.
It doesn't synthesize patch headers or emit `files[].added_lines`. The shared
Git parser decodes quoted filenames while preserving the original patch text,
including rename, deletion, empty-file, and no-newline-marker sections. Invalid
sections never contribute partial patches. A declined section counts once per
commit even when several groups use that commit; valid selected sections can
still produce rows.

### Interpret skipped diff-SFT inputs

The diff-SFT export reports every skipped input under this closed vocabulary:

- `abandoned`
- `repo_mismatch`
- `split_mismatch`
- `explicit_reject`
- `conflicting_run_identity`
- `ambiguous_workflow_verdicts`
- `resolved_ci_failure`
- `no_eligibility_source`
- `unreliable_ci_resolution`
- `eligibility_source_mismatch`
- `no_reward_signal`
- `below_confidence_floor`
- `inference_call_not_found`
- `model_absent`
- `duplicate_completion`
- `conflicting_evidence`
- `mirror_absent`
- `commit_diff_unavailable`
- `unsupported_diff_section`
- `malformed_diff_section`
- `file_diff_unavailable`
- `empty_patch`
- `completionless`
- `duplicate_tool_call_id`
- `empty_message`
- `non_string_tool_result`
- `unrepresentable_part_order`
- `unresolved_tool_call`
- `unsupported_completion_role`
- `unsupported_message_role`
- `unsupported_role_part`

Keep Attribution source metadata and observation IDs with each row. Version 1
recipe eligibility permits inferred Attribution; an empty observation list means
that the row carries no observed Session-to-commit evidence. See
[the source contract](../adr/0014-factual-outcomes-and-training-evidence.md#evidence-recipes-and-exact-metadata).
