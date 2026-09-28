# Long-session resumption with a pointer list (Phase 3)

Implementation tracker: [Issue #134](https://github.com/sediment-ai/sediment/issues/134).
Phase 2: [context-minimal resumption](2026-09-28-context-minimal-phase-2-design.md).

## Question

Phase 2's J2 cut delivered context from 15 KB to 0.9 KB but saved only 17% of
tokens against full history. With minimal context, the agent re-read files and
re-ran commands, doubling its model round trips. Does a pointer list (the paths
already read and commands already run, with no content) stop that
re-exploration enough to reach the Phase 2 token target on longer Sessions?

## Arms

| Arm | Context passed to the agent |
| --- | --- |
| FULL | Every non-reasoning part of the captured catalog. |
| J2 | Policy version 2, unchanged from Phase 2. |
| J2P | J2's parts, plus a pointer block derived deterministically from the captured tool calls: each read, edit, or write path and each command, once, in first-use order. The block says files may have changed and asks the agent to read a file again only when it needs the current content. |

## Fixtures and protocol

- One development family (`threshold-alerts`) and two held-out families
  (`invoice-subtotal`, `username-check`) in
  `scripts/tests/fixtures/context_pointer/`. Each workspace holds about 18 KB
  across eight files, most of them irrelevant to the task.
- Each source Session inspects the workspace over five short turns (about ten
  file reads, the visible check, and an unrelated archived diagnostic), then
  receives the owner's note, or an obsolete note and its correction.
- Long catalogs exceed the Phase 1 limits, so Phase 3 raises them to 96 parts and
  128 KB, allows 12 evidence calls per selection, and chunks each factual read at
  the API's limit of 32 references.
- Source Sessions may make up to 40 model calls, since five inspection turns
  exceed the 12-call continuation limit. Continuations keep 12.
- Everything else follows the Phase 2 version 5 protocol: gemma4:31b on Ollama
  cloud, a 65,536-token window, an 840-second pi idle timeout, no gate retry, and
  `upstream_unavailable` runs excluded and counted. The schedule is 2 families ×
  3 profiles × 3 arms × 2 repetitions = 36 continuations.
- `scripts/context_pointer_eval.py` configures the Phase 2 driver. Its protocol
  identity is version 6, and Phase 3 keeps its own probe ledger.

## Development findings and J2 policy version 3

The development run (`threshold-alerts`, 9 continuations) showed two things
before any held-out run:

- On long histories, both J2 arms used about 35% of FULL's tokens across the 3
  complete tasks. FULL's 36 KB context was resent on every call, which outweighed
  the two extra round trips the minimal arms needed. The pointer list changed
  little (J2P used 1.02 times J2's tokens).
- In the correction profile, JEV scored the correction note relevance 0.87 but
  new information 0.56 and conflict 0.52, while the obsolete note it replaces
  qualified. Policy version 2 therefore delivered the superseded rule without
  its correction.

Policy version 3 also qualifies a candidate whose relevance is at least 0.80.
Re-scoring the recorded development selections, version 3 delivers the rule in
all seven runs with JEV scores, against three for version 2. Its only other
effect is one small restated note in redundant runs. Phase 3 uses version 3 for
J2 and J2P (protocol version 7); Phase 2's recorded results used version 2. On
this family, every arm, FULL included, failed the constraint check in missing
and correction runs: the visible task states "every reading below or at the
limit is quiet", which the note contradicts. That is a fixture difficulty, not
an instrument failure, and the held-out families are unchanged.

## Targets (fixed before any development or held-out run)

| Target | Criterion |
| --- | --- |
| Validity | Every slot recorded; no instrument failure; at least 10 of 12 tasks with all three arms measured and complete |
| Rule delivery | J2P delivers the rule in every measured missing and correction run |
| Quality | Over complete triples, J2P passes both checks in at least as many runs as FULL, minus one |
| Tokens | Over complete triples, J2P uses at most 60% of FULL's combined coding and JEV tokens |

J2P against J2 is reported as the pointer list's effect. A correctly measured
negative result completes the experiment.

## Limits

Two synthetic families and two repetitions can't support a population estimate.
Histories of about 40 KB are longer than Phase 2's but far shorter than real
multi-hour Sessions. The agent must still read the file it edits and rerun the
check to verify it, so a pointer list can remove only optional re-reads.
