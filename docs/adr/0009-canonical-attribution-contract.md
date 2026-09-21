# ADR 0009 — Attribution is the canonical derived contract

Status: accepted

## Context

A canonical artifact can contain Attribution without a Developer decision or
CI label. Its name must distinguish observed content from derived evidence.
Policies also need explicit owners and stable structure so a dataset can name
which resolved settings produced it.

## Decision

Attribution is the canonical term for the derived inference-call-to-change
join. `Attribution`, `AttributionPolicy`, `AttributionResult`, and
`derive_attributions` own the Python API. Each Attribution
carries `similarity_score` and an `attribution_source` of `git_notes` or
`jaccard`.

The derivation policy uses one closed TOML schema with `schema_version = 1`.
The `attribution.git_notes` and `attribution.jaccard` tables each contain
`min_similarity` and `lookback_window_minutes`. The top-level attribution table
contains `post_push_grace_period_minutes` and `max_commits_per_push`. The split
table contains `eval_fraction`. Sediment rejects missing or unsupported schema
versions, unknown fields, and removed vocabulary. The resolved policy produces
one deterministic SHA-256 digest.

An `AttributedCompletion` can exist without a Developer decision or CI label. Canonical derived artifacts and
training rows carry a structured `Provenance` object with `policy_version`,
integer `quarantine_revision`, and nullable full `policy_digest`.

Rollouts carry repository-qualified `commits` entries. Recovery samples name
both sides symmetrically with `failed_inference_call_ids` and
`fixed_inference_call_ids`. [ADR 0015](0015-lossless-values-and-bundle-v2.md)
defines lossless container encoding. [ADR 0016](0016-bundle-derivation-consistency.md)
and [ADR 0019](0019-repository-identity-and-renames.md) define validation and
repository identity for the supported version-4 bundle.

## Consequences

- DPO, SFT, diff-SFT, recovery, and RLVR consume one set of canonical artifact
  names and provenance fields.
- Identical commit SHAs in different repositories remain distinct through
  rollout derivation and bundle round-trip.
- Unsupported policy and bundle versions require regeneration from Facts.
- Sediment doesn't expose compatibility aliases, legacy policy loaders, or
  older bundle readers for the removed contract.
- A future schema change requires an explicit version decision. Compatibility
  code exists only when deployed consumers or retained data require it.
