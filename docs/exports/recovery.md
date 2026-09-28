# Export Recovery rows

Export fixing diffs from clean failed-to-passed CI transitions. This procedure
is for operators with captured CI outcomes and local Git mirrors. Inference
calls enrich the pairs when available; they aren't required to produce a row.

## Prepare the evidence

Use the [configured operator shell and private output parent](training-exports.md#prepare-the-export).
Set `SEDIMENT_ORG_ID`, `SEDIMENT_DATABASE_URL`, and `SEDIMENT_MIRROR_PATH`.
The mirrors must contain the failed and fixed commits and their ancestry.

Recovery reads Facts and mirrors directly. It doesn't accept `--from`, cohort
filters, a Derivation policy file, or a consumer profile. A reviewed bundle
doesn't freeze the evidence for this command.

## Understand Recovery lineage

The Recovery export writes one row per red-to-green CI transition in a clean,
resolved workflow lineage ([Recovery
pair](../../CONTEXT.md#recovery-pair-recoverysample)). It pairs the last
`failed` run with the next `passed` run on a descendant commit. The row carries
the fixing diff and identifiers for Inference calls attributed to each side.
Match workflow IDs within one provider, or paths when both IDs are absent. Names alone don't
establish a definition; `workflow_identity_absent` counts each excluded clean
resolution. If an earlier attempt carries the identity, the resolved run retains it.

[Recovery pairs](../agents/derivations.md#recovery-pairs-recoverypy) documents
the Derivation gates: ancestry, suspected-flake exclusion, ambiguous-verdict
exclusion, non-verdict exclusion, same-commit rerun decline, and the diff-size
cap.

## Export the pairs

Run the command with an unused destination:

```bash
sediment export recovery --out "$HOME/sediment-exports/review-recovery"
```

The command selects `recovery_ci` version 1. It prints `pairs derived` and
`recovery rows`, each with a separate `skipped` map, followed by written paths.
The CLI uses the default evaluation fraction of 0.1. Populated partitions
produce `recovery.train.jsonl` and `recovery.eval.jsonl`; an empty partition
produces no file. The CLI has no split override.

## Inspect the output

1. Check that the failed and fixed outcomes describe the same workflow lineage
   and that the fixed commit descends from the failed commit.
2. Inspect `recovery_diff` against the mirror. The default limit is 200 changed
   lines, counting additions and removals inside hunks.
3. Review both diagnostic maps. `workflow_identity_absent`, `same_commit`,
   `not_ancestor`, and `diff_oversized` explain common Derivation exclusions.
4. [Audit the split](training-exports.md#audit-the-split-before-training).
   A pair goes to eval if any resolvable Session on either side belongs to eval.
   With no resolvable Session evidence, it goes to train. That fallback doesn't
   establish a task-level holdout.

If no rows qualify, the command prints `nothing written` and leaves earlier
files untouched. Use a fresh destination to keep that result unambiguous.

## Read a Recovery row

The [RecoveryRow schema](../reference/schema.md#recoveryrow) defines the complete
wire contract. Inspect these fields when auditing a pair:

| Field | Meaning |
|---|---|
| `recipe_id`, `recipe_version` | `recovery_ci`, integer version 1 |
| `org_id`, `repo`, `branch` | Lineage identity |
| `workflow_name`, `workflow_path` | The check that failed, then passed |
| `failed_commit_sha`, `fixed_commit_sha` | The commit pair |
| `failed_outcome_id`, `fixed_outcome_id` | the CI Facts behind it |
| `recovery_diff` | the fixing diff (capped at derive time) |
| `failed_inference_call_ids`, `fixed_inference_call_ids` | Inference calls attributed to each side when available; absence is expected and never fabricated |
| `failed_attribution_evidence`, `fixed_attribution_evidence` | Optional per-call Session, Attribution sources, and matching observation IDs for each side; missing records count as `attribution_evidence_absent` without excluding the CI pair |
| `provenance`, `split` | Derivation Provenance and the partition assigned at export time |
| `repository_identity` | provider, host, and immutable repository ID shared by failed and fixed commits; null for an unambiguous legacy repository |
| `schema_id`, `schema_version` | canonical Recovery-row wire contract; independent of `recovery_ci` recipe version |

## Interpret the degradation tally

The projection declines each unrepresentable pair once under `non_finite_number`
or `unrepresentable_unicode`. It checks emitted content and metadata, excluding
omitted Inference-call messages. The separate `inference_call_not_found` tally counts
attributed inference-call IDs that are missing from the Inference call lookup.
Missing source records count under `attribution_evidence_absent`, per side and
Inference call. These two enrichment counts don't drop the CI pair. Don't add
them to pair-level exclusions to estimate lost rows.
