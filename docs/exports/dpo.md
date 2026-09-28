# Export DPO pairs

Export distinct chosen and rejected responses for direct preference optimization
(DPO), using the same prompt and model.
This procedure is for operators with captured preference or CI evidence.
Before starting, [prepare a reviewed bundle and private output parent](training-exports.md#prepare-the-export).
The examples use `$HOME/sediment-derived/review`.

## Choose an Evidence recipe

Both members need Attributed completions for the same model and structurally
identical input messages. Their mapped responses must differ. A rejected edit
or Retry linkage alone doesn't supply those canonical inputs.

`dpo_human` version 2 is the default. The chosen member requires a
human-explicit accept. The rejected member requires a human-explicit reject.
These are independent gestures. The row doesn't claim that the developer
compared the two members directly.

`dpo_outcome` version 2 requires explicit selection. A clean resolved CI pass
labels the chosen member. A clean resolved CI failure labels the rejected
member. Ambiguous verdicts, non-verdict-only evidence, and suspected flakes
don't label a member. A pair never mixes human and CI sources.

## Export the selected recipe

For human preferences, run:

```bash
sediment export dpo \
  --from "$HOME/sediment-derived/review" \
  --recipe dpo_human \
  --out "$HOME/sediment-exports/review-dpo"
```

If you selected CI outcomes, use `--recipe dpo_outcome` and another unused
output directory. If you need a supported trainer format, use the matching
[consumer-profile procedure](consumer-compatibility.md#export-sft-or-dpo).

## Inspect the output

The command prints `pairs projected`, `skipped`, and each written path. With
the default split, expect `dpo.train.jsonl` and `dpo.eval.jsonl` only for
populated partitions. A bundle with splitting disabled produces `dpo.jsonl`.

1. Check that each pair has the intended recipe and both label sources in
   `metadata`. A pair never mixes human and CI labels.
2. Inspect `prompt`, `chosen`, and `rejected`. Verify that the responses differ
   and that the prompt and source model match the task you want to train.
3. Review exclusions and [audit the split](training-exports.md#audit-the-split-before-training).
   Zero rows leave any earlier files untouched.

## Read a DPO row

| Field | Meaning |
|---|---|
| `prompt` | shared trainer-facing request messages |
| `chosen`, `rejected` | preferred and dispreferred response-message lists |
| `tools` | tool definitions; empty because Inference call schema version 1 doesn't carry definitions |
| `metadata` | organization, source model, both completion IDs, recipe ID and version, both label sources, label Confidence, CI reliability, Confidence margin, per-member Provenance, and split |

Example DPO row:

```json
{
  "prompt": [{"role": "user", "content": "Fix the test."}],
  "chosen": [{"role": "assistant", "thinking": "Keep the API.", "content": "Fixed it."}],
  "rejected": [{"role": "assistant", "content": "Replaced the module."}],
  "tools": [],
  "metadata": {
    "org_id": "acme",
    "source_model": "model-name",
    "chosen_completion_id": "call-good",
    "rejected_completion_id": "call-bad",
    "recipe_id": "dpo_human",
    "recipe_version": 2,
    "chosen_label_source": "explicit_accept",
    "rejected_label_source": "explicit_reject",
    "label_confidence": 0.0,
    "ci_reliability": 1.0,
    "confidence_margin": 1.0,
    "provenance": {
      "chosen": {"policy_version": "5", "quarantine_revision": 0, "policy_digest": null},
      "rejected": {"policy_version": "5", "quarantine_revision": 0, "policy_digest": null}
    },
    "chosen_attribution_source": "git_notes",
    "rejected_attribution_source": "jaccard",
    "chosen_session_commit_observation_ids": [],
    "rejected_session_commit_observation_ids": [],
    "split": "train",
    "chosen_repository_identity": null,
    "rejected_repository_identity": null,
    "schema_id": "https://sediment.so/schemas/training-rows/dpo-pair/v4.json",
    "schema_version": 4
  }
}
```

`metadata.label_confidence` is the weaker member's Confidence. Under the
default policy, an explicit reject has Confidence 0.0, so a human-preference
pair can have label Confidence 0.0 and still qualify. Confidence isn't a
probability that the pair's preference label is correct.
`metadata.confidence_margin` is chosen Confidence minus rejected Confidence,
bounded to [−1, 1].

## Interpret skipped inputs

| Result | What to check |
| --- | --- |
| `no_label_source` | The selected recipe lacks an explicit human decision or a resolved CI verdict. Implicit accepts and corrective prompts don't supply human-explicit labels. |
| `unreliable_ci_resolution` | Suspected flakes can't label an outcome pair. |
| `identical_responses` | The mapped response lists are equal. Metadata differences don't provide response contrast. |
| `bucket_capped` | The bucket reached the default limit of three evaluated candidate pairs. Declined pairs consume slots without replacement. |
| Zero pairs with eligible members | Both labels must occur in one structurally identical prompt/model bucket. Different prompts and different models don't pair. |

`identical_responses` counts evaluated pairs; `bucket_capped` counts buckets.
Don't add these units to estimate lost pairs. Representation failures take
precedence over equality and count once. For the complete vocabulary, see
[DPO projection contracts](../agents/exports-and-stats.md#dpo-dpopy) and
[representation exclusions](training-exports.md#check-representation-exclusions).

## Understand the pairing contract

The DPO export pairs a `chosen` completion with a `rejected` completion. Both
members must share the same model and an identical prompt, defined as a
structurally equal native message history. Those shared values form the prompt
bucket.

Within a bucket, the projection first keeps one Attributed completion that the
selected recipe can label for each Inference call. Confidence and a
deterministic evidence key break ties. Each bucket evaluates the first
`DPOPolicy.max_pairs_per_bucket` candidate pairs in sorted call-ID order, with a
default of 3. A declined pair consumes a slot. The projection doesn't examine
later pairs to replace it.

After complete-row representation validation, the projection compares the full
mapped response lists through strict JSON. It ignores dictionary insertion
order. Whitespace, Unicode code points, readable reasoning, message/list order,
tool identities, nested arguments, and scalar types remain significant.
Different native text-part segmentation can map to identical responses.
Metadata differences don't provide response contrast.

If either member belongs to an eval Session, the pair lands in eval.
This rule alone doesn't enforce prompt or task isolation across the dataset.

## Migrate retained DPO exports

Use `hf-trl-dpo-v2` or `fireworks-dpo-v2` for recipe-v2 rows with canonical DPO
schema v4. Their mapping and dependency pins match the prior profiles. Runtime
requests for `hf-trl-dpo-v1` or `fireworks-dpo-v1` fail with migration guidance.

If you retained recipe-v1/schema-v3 rows, re-export their canonical evidence
under the running recipe. Successor profiles refuse those historical rows;
they don't relabel or adapt them. Published schema v1–v3 files and retained
artifacts remain unchanged. There is no historical algorithm selector.

The recipe stamp identifies response-contrast eligibility. `DPOPolicy` still
lacks an independent policy-version field, so the recipe stamp doesn't version
every policy setting.

Keep Attribution source metadata and observation IDs with each row. Version 2
recipe eligibility permits inferred Attribution; an empty observation list means
that the row carries no observed Session-to-commit evidence. See
[the source contract](../adr/0014-factual-outcomes-and-training-evidence.md#evidence-recipes-and-exact-metadata).
