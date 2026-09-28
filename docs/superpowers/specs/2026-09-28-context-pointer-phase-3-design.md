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
