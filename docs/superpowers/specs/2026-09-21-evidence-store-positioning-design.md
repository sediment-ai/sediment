# Evidence store positioning

Decision: Position Sediment as the open-source, self-hosted evidence store for
coding agents. [Issue 67](https://github.com/sediment-ai/sediment/issues/67)
records the maintainer's approval and tracks the core work. The
[implementation claim on 2026-09-21](https://github.com/sediment-ai/sediment/issues/67#issuecomment-5762081465)
records the accepted scope. [Site issue 109](https://github.com/sediment-ai/sediment-site/issues/109)
tracks the website.

## Message and audience

Engineering teams capture what coding agents did and what happened to their
work. They keep that evidence on their infrastructure and use it to evaluate
agent work, provide selected evidence as agent context, and build training
datasets. These are three uses of one evidence store.

The README and homepage lead with this definition. Training remains a complete
use case with its own methodology and export guides. The primary example shows
the shared evidence and its three uses; the training-row example moves into the
training section.

## Claim boundaries

[PR 65](https://github.com/sediment-ai/sediment/pull/65) implements bounded evidence
access. The positioning depends on that implementation. Operators inventory a
Session, inspect captured Inference-call structure, and fetch selected parts for
a consuming agent. The guide demonstrates controlled continuation with an intact
workspace. The copy makes no claim about automatic context selection, autonomous
memory, workspace recovery, continuation quality, or measured token savings.

Sediment's capture, storage, evidence reads, Derivations, and exports run on the
customer's infrastructure. Installation can download software; optional mirrors
connect to configured repositories. The consuming agent's model endpoint controls
inference traffic. Prepared internal dependencies and endpoints support an
internal deployment.

Facts remain the only persisted domain state. This work changes no schema,
retrieval behavior, Derivation, Evidence recipe, or training contract.

## Implementation and verification

1. Align the README, conceptual introductions, and architecture diagrams.
2. Align homepage copy, metadata, structured data, and machine-readable guidance.
3. Update Sediment descriptions on comparison pages. Preserve competitor claims
   and their review dates; distinguish the positioning edit date.
4. Synchronize generated site documentation and expose the evidence guide in
   navigation.
5. Run documentation checks, render both diagram themes, run the website checks
   and tests, and inspect desktop and mobile layouts.
6. Open coordinated draft pull requests. Merge and publication follow maintainer
   approval and availability of the evidence-access implementation.
