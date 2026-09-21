# ADR 0005 — Measure outcomes before extending export destinations

Status: accepted

## Context

Per-model acceptance and retained-work measurements make captured evidence
useful before a team trains a model. They also help assess whether a fine-tune
improves observed outcomes. A dataset alone cannot answer that question.

## Decision

The end-to-end pipeline includes a per-model outcome report with call counts,
Attribution, CI results, and explicit accept/reject evidence. The deterministic
evaluation split accompanies canonical artifacts so exported datasets can be
assessed independently.

Complete-path acceptance requires both synthetic verification and representative
capture that produces a dataset and a report. A structural change must preserve
that path. Synthetic checks do not establish compatibility with every live
harness or deployment.

## Consequences

- Measurement remains part of the core pipeline.
- Export destinations stay minimal until a concrete consumer requires another.
- Deployment acceptance names the captured sources, evidence coverage, and
  operator result instead of treating a successful export as sufficient proof.
