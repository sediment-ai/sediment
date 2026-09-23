# Export DPO pairs

For a supported downstream release, select a versioned
[consumer profile](consumer-compatibility.md).

Run the default direct preference optimization (DPO) export:

```bash
sediment export dpo --out /data/export
```

The command writes `dpo.jsonl`, or split train and eval files, with distinct
chosen and rejected responses for matching prompt buckets. It selects
`dpo_human` version 2 by default. For shared prerequisites, reviewed-bundle steps, output
inspection, and holdout guidance, see [Choose a training
export](training-exports.md).

## Choose an Evidence recipe

`dpo_human` version 2 is the default. The chosen member requires a
human-explicit accept. The rejected member requires a human-explicit reject.
These are independent gestures. The row doesn't claim that the developer
compared the two members directly.

To use CI outcomes, select the outcome recipe:

```bash
sediment export dpo --recipe dpo_outcome --out /data/export
```

`dpo_outcome` version 2 requires explicit selection. A clean resolved CI pass
labels the chosen member. A clean resolved CI failure labels the rejected
member. Ambiguous verdicts, non-verdict-only evidence, and suspected flakes
don't label a member. A pair never mixes human and CI sources.

## Understand the pairing contract

The DPO export pairs a `chosen` completion with a `rejected` completion. Both
members must share the same model and an identical prompt, defined as a
structurally equal native message history. Those shared values form the prompt
bucket.

Within a bucket, the projection first keeps one Attributed completion that the
selected recipe can label for each inference call. Confidence and a
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

A pair that straddles the holdout lands in eval.

## Read a DPO row

| Field | Meaning |
|---|---|
| `prompt` | shared trainer-facing request messages |
| `chosen`, `rejected` | preferred and dispreferred response-message lists |
| `tools` | tool definitions; empty because inference-call version 1 doesn't carry definitions |
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
    "label_confidence": 0.7,
    "ci_reliability": 1.0,
    "confidence_margin": 0.3,
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

`metadata.label_confidence` is the weaker member's Confidence.
`metadata.confidence_margin` is chosen Confidence minus rejected Confidence,
bounded to [−1, 1].

## Interpret skipped inputs

Review `skipped` before training. The DPO export reports every skipped input
under this closed vocabulary: `conflicting_run_identity`,
`ambiguous_workflow_verdicts`, `repository_identity_absent`,
`repository_identity_conflict`, `repository_identity_unresolved`,
`repository_mirror_identity_unresolved`, `repository_source_absent`,
`non_finite_number`, `unrepresentable_unicode`, `completionless`,
`duplicate_tool_call_id`, `empty_message`, `non_string_tool_result`,
`unrepresentable_part_order`, `unresolved_tool_call`,
`unsupported_completion_role`, `unsupported_message_role`,
`unsupported_role_part`, `inference_call_not_found`, `promptless`,
`model_absent`, `no_label_source`, `unreliable_ci_resolution`, `bucket_capped`,
and `identical_responses`.

`identical_responses` counts evaluated pairs; `bucket_capped` counts buckets.
Don't add these different units to estimate lost pairs. Existing representation
failures take precedence over equality and count once. If every pair declines,
the command reports zero rows and leaves earlier files untouched. That result
doesn't describe a fresh dataset.

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
