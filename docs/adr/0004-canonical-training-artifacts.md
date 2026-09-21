# ADR 0004 — Canonical artifacts own the training-data join

Status: accepted

## Context

If every training formatter joins capture, decisions, retained work, and CI
independently, each format can assign different meaning to the same evidence.
Sediment needs shared canonical artifacts with explicit source metadata before
it projects evidence into training rows.

## Decision

Sediment derives two canonical artifacts:

- **Attributed completions** carry an inference call's file-level Attribution
  or supported outcome variant, Developer-decision evidence, retained-work
  evidence, CI resolution, Provenance, and deterministic train/eval split.
- **Rollouts** carry whole Sessions, their captured turns, and verifier evidence.

Training formats are stateless projections over these artifacts. The
[canonical Attribution contract](0009-canonical-attribution-contract.md)
defines their names and Provenance. [Training-objective evidence](0011-training-objectives-own-evidence-interpretation.md)
defines which evidence can supply labels, eligibility, and Reward for each
objective. Artifacts preserve evidence without collapsing it into one quality
judgment.

For human-decision labels, explicit Developer decisions take precedence over
CI evidence in both directions. CI can change Confidence and reliability
without replacing the decision label. Each Evidence recipe still controls
eligibility and Reward under ADR 0011; this precedence does not waive a
recipe's CI requirements.

DPO pairs compare structurally identical prompts for the same model. A call
cannot pair with itself. Promptless calls cannot pair. Candidate bounds and
all eligibility declines remain counted. Each Evidence recipe owns its label
sources; exporters cannot mix incompatible label sources to increase yield.

**Recovery** is the one sanctioned Fact-derived exception. A Recovery sample
represents a red-to-green CI transition within one workflow lineage and carries
the fixing diff. That commit-pair shape does not project from one Attributed
completion or Rollout. Its Derivation remains pure, deterministic,
quarantine-excluding, and mirror-read-only, and reuses Attribution for optional
enrichment. Any other training format must consume a canonical artifact unless
an architectural decision establishes another exception.

The [factual-outcome contract](0014-factual-outcomes-and-training-evidence.md)
governs observed Session-to-commit relationships and absence. Missing evidence
cannot fabricate an abandonment label. A retained outcome variant does not
itself make an input eligible for any training objective.

## Consequences

- Shared assembly owns evidence attachment, Provenance, and split assignment.
- Formatters cannot fork the join or reinterpret an unsupported observation.
- Unique-or-drop attachment favors a counted missing signal over a label
  attached to the wrong call.
- Each training row names its Evidence recipe and retains its label sources.
