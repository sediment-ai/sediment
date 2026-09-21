# Export Recovery rows

Run the Recovery export:

```bash
sediment export recovery --out /data/export
```

The command writes `recovery.jsonl`, or split train and eval files, with one
row per eligible red-to-green CI transition. It selects `recovery_ci` version
1. For shared prerequisites, output inspection, schema guidance, and holdout
checks, see [Choose a training export](training-exports.md).

## Understand Recovery lineage

The Recovery export writes one row per red-to-green CI transition in a clean,
resolved workflow lineage ([Recovery
pair](../../CONTEXT.md#recovery-pair-recoverysample)). It pairs the last
`failed` run with the next `passed` run on a descendant commit. The row carries
the fixing diff and inference calls attributed to each side. Match workflow
IDs within one provider, or paths when both IDs are absent. Names alone don't
establish a definition; `workflow_identity_absent` counts each excluded clean
resolution. If an earlier attempt carries the identity, the resolved run retains it.

[Recovery pairs](../agents/derivations.md#recovery-pairs-recoverypy) documents
the Derivation gates: ancestry, suspected-flake exclusion, ambiguous-verdict
exclusion, non-verdict exclusion, same-commit rerun decline, and the diff-size
cap.

Recovery is the one training-row format that a Derivation produces directly
from Facts and the mirror. It doesn't project from Attributed completions or
Rollouts because its commit-pair shape can't project from either canonical
artifact. ADR 0004 sanctions this exception.

The Recovery command has no `--from` form.

## Read a Recovery row

| Field | Meaning |
|---|---|
| `recipe_id`, `recipe_version` | `recovery_ci`, integer version 1 |
| `org_id`, `repo`, `branch` | lineage identity |
| `workflow_name`, `workflow_path` | the check that went red → green |
| `failed_commit_sha`, `fixed_commit_sha` | the pair |
| `failed_outcome_id`, `fixed_outcome_id` | the CI Facts behind it |
| `recovery_diff` | the fixing diff (capped at derive time) |
| `failed_inference_call_ids`, `fixed_inference_call_ids` | inference calls attributed to each side when available; absence is expected and never fabricated |
| `failed_attribution_evidence`, `fixed_attribution_evidence` | Optional per-call Session, Attribution sources, and matching observation IDs for each side; missing records count as `attribution_evidence_absent` without excluding the CI pair |
| `provenance`, `split` | split stamps at export time: eval if **any** resolvable Session on either side is eval; no Session evidence at all → train (the holdout covers captured Sessions only) |
| `repository_identity` | provider, host, and immutable repository ID shared by failed and fixed commits; null for an unambiguous legacy repository |
| `schema_id`, `schema_version` | canonical Recovery-row wire contract; independent of `recovery_ci` recipe version |

## Interpret the degradation tally

The projection declines each unrepresentable pair once under `non_finite_number`
or `unrepresentable_unicode`. It checks emitted content and metadata, excluding
omitted Inference-call messages. The separate `inference_call_not_found` tally counts
attributed inference-call IDs that are missing from the inference-call lookup.
The count makes degradation visible without dropping rows.
