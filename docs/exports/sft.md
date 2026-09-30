# Export SFT and diff-SFT rows

Export a captured response for supervised fine-tuning (SFT), or an exact
attributed-file patch for diff-shaped SFT (diff-SFT). This procedure is for
operators who want to imitate eligible work.

Before starting, [prepare a reviewed bundle and private output parent](training-exports.md#prepare-the-export).
The examples use `$HOME/sediment-derived/review`. SFT reads the bundle alone;
diff-SFT also requires `SEDIMENT_MIRROR_PATH` and the referenced commit diffs.

## Choose an Evidence recipe

SFT and diff-SFT use the same selected Evidence recipe and `SFTPolicy`.
Diff-SFT doesn't redefine eligibility.

`sft_curated` version 1 is the default. It admits a human-explicit accept or an
accepted edit with `edit_retention_score >= 0.8`. A human-explicit reject,
abandonment, or resolved workflow failure vetoes eligibility. The veto includes
an ambiguous aggregate with one failing workflow. A resolved CI pass can affect
Confidence and CI reliability, but it can't create curated eligibility.

`sft_verified` version 1 requires explicit selection. It admits only a clean
resolved CI pass. Ambiguous verdicts, non-verdict-only evidence, and suspected
flakes don't create eligibility.

Human-explicit rejection, abandonment, and any resolved workflow failure veto
both recipes. Both recipes require a resolved Confidence of at least
`SFTPolicy.min_confidence`, which defaults to 0.6.

## Export SFT rows

For the default curated recipe, run:

```bash
sediment export sft \
  --from "$HOME/sediment-derived/review" \
  --recipe sft_curated \
  --out "$HOME/sediment-exports/review-sft"
```

If you selected verified imitation, use `--recipe sft_verified` and another
unused destination. If you need a supported trainer format, follow
[Export for a consumer](consumer-compatibility.md#export-sft-or-dpo).

The command prints `samples projected`, `skipped`, and each written path.
With the default split, populated partitions produce `sft.train.jsonl` and
`sft.eval.jsonl`. With splitting disabled, the file is `sft.jsonl`.

The SFT export writes at most one row per Inference call that the selected
recipe admits. Eligibility doesn't establish code quality.

An Inference call can have one eligible Attributed completion per attributed
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
| `tools` | tool definitions; empty because Inference call schema version 1 doesn't carry definitions |
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

The mapper keeps text in `content`, readable reasoning in `thinking`, and tool
calls in `tool_calls`. Arguments remain objects. Tool results retain their order;
structured results become deterministic JSON strings. Earlier assistant messages
stay in `prompt`, outside the completion loss boundary.

### Interpret skipped SFT inputs

| Reason | What to check |
| --- | --- |
| `no_eligibility_source` | The selected recipe needs an explicit accept, qualifying retention, or a clean CI pass. An implicit accept alone doesn't qualify. |
| `abandoned`, `explicit_reject`, or `resolved_ci_failure` | Negative evidence vetoes imitation, including when other positive evidence exists. |
| `unreliable_ci_resolution` | Suspected-flake evidence can't establish verified eligibility. |
| `below_confidence_floor` | A recipe-eligible candidate falls below the default Confidence floor of 0.6. |
| `duplicate_completion` | Another copy represents the same Inference call; one row remains. |
| `conflicting_evidence` | Copies of one evidence identity disagree; the entire Inference call is excluded. |

See [SFT projection contracts](../agents/exports-and-stats.md#sft-and-diff-sft-sftpy-diff_sftpy)
for the complete contract and
[representation exclusions](training-exports.md#check-representation-exclusions)
for invalid emitted values.

## Export diff-SFT rows

If you need patches instead of captured responses, run:

```bash
sediment export diff-sft \
  --from "$HOME/sediment-derived/review" \
  --recipe sft_curated \
  --out "$HOME/sediment-exports/review-diff-sft"
```

If you selected verified imitation, use `--recipe sft_verified` and another
unused destination. Diff-SFT has no consumer profile.

The command prints `samples projected`, `skipped`, and each written path.
With the default split, populated partitions produce `diff_sft.train.jsonl`
and `diff_sft.eval.jsonl`. With splitting disabled, the file is `diff_sft.jsonl`.

The diff-SFT projection writes one row per `(Inference call, commit)` group and
reads the concrete per-file edits from the mirror. It never synthesizes a diff.

The projection skips abandonment rows before grouping or mirror access. If any
remaining member is ineligible or explicitly rejected, the projection skips
the whole group. Group Confidence is the `min()` across members.

### Read a diff-SFT row

| Field | Meaning |
|---|---|
| `prompt` | the trainer-facing request messages |
| `completion` | one assistant message whose `content` is the exact selected unified patch |
| `tools` | tool definitions; empty because Inference call schema version 1 doesn't carry definitions |
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

Diff-SFT applies the SFT exclusions, then checks the commit group and patch:

| Reason | What to check |
| --- | --- |
| `mirror_absent` or `commit_diff_unavailable` | The configured mirror must contain the referenced commit. |
| `empty_patch` or `file_diff_unavailable` | Every attributed file needs a patch section. The projection doesn't invent missing text. |
| `unsupported_diff_section` or `malformed_diff_section` | Git evidence contains an unusable section. Counts describe sections per commit, not lost samples. |
| `repo_mismatch`, `split_mismatch`, or `eligibility_source_mismatch` | Group members disagree on the repository, partition, or eligibility source. |

## Audit rows before training

1. Check `metadata.recipe_id`, `eligibility_source`, and `label_confidence`
   against the recipe you selected. A nonempty bundle can produce zero rows.
2. For SFT, keep earlier assistant messages in `prompt` and train on
   `completion`. For diff-SFT, compare the target patch with the attributed
   file sections in the mirror.
3. Review diagnostic counts and [audit the split](training-exports.md#audit-the-split-before-training).
   Empty projections and empty partitions leave earlier files untouched.

Keep Attribution source metadata and observation IDs with each row. Version 1
recipe eligibility permits inferred Attribution; an empty observation list means
that the row carries no observed Session-to-commit evidence. See
[the source contract](../adr/0014-factual-outcomes-and-training-evidence.md#evidence-recipes-and-exact-metadata).
