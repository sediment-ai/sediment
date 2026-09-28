# Context-minimal resumption with a decision model (Phase 2)

Implementation tracker: [Issue #134](https://github.com/sediment-ai/sediment/issues/134).
Phase 1: [bounded JEV selection](2026-09-26-bounded-jev-selection-design.md).

## Question

Can a decision model sit between a resuming coding agent and Sediment's
captured history, pass the agent only what it can't recover from the
workspace, and cut token usage substantially without lowering task quality?

## What Phase 1 and the diagnostics showed

- J1 required a relevance score of at least 0.60. On the event-rollup family,
  JEV scored the rule-carrying note 0.33 to 0.51 and dropped it, although it
  scored new information and conflict above 0.80.
- A one-factor replay of those requests showed that relevance rises by 0.31 to
  0.36 when the detailed task is shortened, and by 0.35 to 0.44 when the note is
  the only candidate. New information and conflict stayed high in every variant.
- On 22 clean offline cases, JEV kept every missing requirement and added no
  restated one (`scripts/jev_judgment_eval.py`).
- Phase 1 histories were about 8 KB, so keyword selection already delivered
  nearly everything and J1 could save little.

## Arms

| Arm | Context passed to the agent |
| --- | --- |
| FULL | Every non-reasoning part of the captured catalog, in conversation order. No selector call. |
| K | Phase 1 keyword selection, unchanged: at most eight parts and 8,192 bytes. |
| J2 | JEV policy version 2 (below). |

### J2 policy, version 2

1. Candidates are the recorded user and assistant text parts, in conversation
   order, at most 12. Tool calls and tool results are excluded, since the agent
   can recover them by reading files and rerunning commands. When the request
   exceeds 16,384 bytes, the oldest candidates are dropped first.
2. One JEV request asks the unchanged Phase 1 questions (relevance, new
   information, conflict) about each candidate. There is no initial evidence.
3. A candidate qualifies when new information or conflict is at least 0.60.
   Relevance is recorded but doesn't gate.
4. Qualifying parts are delivered in conversation order within the eight-part,
   8,192-byte envelope, then re-read and verified through the factual read
   route. An empty result is a judgment, not a gap.
5. If no valid answer arrives, J2 sends the same request again, up to three
   attempts of 20 seconds each, waiting 10 and then 20 seconds between them. If
   every attempt fails, it falls back to K and the run is reported as a fallback.
   The development run showed why: JEV failed about one call in ten (gateway
   errors and stalls), and one development run fell back after two 35-second
   timeouts.

## Fixtures and protocol

- One development family (`sensor-window`) and two held-out families
  (version 5: `shipping-weight`, `score-average`) in
  `scripts/tests/fixtures/context_minimal/`.
  They use the Phase 1 profiles: missing, redundant, and correction.
- Each source Session reads a long README, the module, and a sample file, runs the
  visible check, and runs an unrelated archived diagnostic. The visible task
  states a detailed contract that the hidden rule amends.
- The schedule is 2 families × 3 profiles × 3 arms × 2 repetitions = 36
  continuations. The three arms rotate their order across tasks.
- The coding model is gemma4:31b on Ollama cloud, with the protocol version 3
  generation contract. Every arm has a 65,536-token context window, so full
  history isn't truncated by a window sized for Phase 1.
- The gate retries an upstream request once, only when it failed before the
  agent received any byte. Each retry is recorded. An instrument failure still
  stops the matrix, and no slot is rerun.
- `scripts/context_minimal_eval.py` points the Phase 1 runner at these inputs.
  Its protocol identity is version 4.

## Version 4 stop and version 5 repair

The version 4 held-out matrix (families `ledger-balance` and `tag-normalize`)
stopped at slot 5 of 36 with `capture_incomplete`. Ollama cloud held one coding
request in its queue ("timed out waiting for a concurrent request slot") until
pi's 300-second idle timeout abandoned it. The provider then returned HTTP 429,
and Sediment captured the failed request as a fourth call, so the captured
count no longer matched the three completed calls. Those two families are spent;
their five recorded slots are reported, never rerun, and not pooled with
version 5.

Version 5 changes only the instrument and uses fresh held-out families
(`shipping-weight` and `score-average`):

- pi's HTTP idle timeout is 840 seconds, so the agent doesn't abandon a request
  before the gate's 300-second read limit decides it.
- The gate doesn't retry. A failed attempt still becomes a captured call, so a
  retry can't keep capture counts exact.
- A run whose model request failed upstream (a non-200 response, or a gate
  forward failure) is recorded as `upstream_unavailable`. It isn't an
  instrument failure: the matrix continues, the workspace is still validated,
  and the run is excluded from measurement and counted per arm.
- Quality and tokens are compared over complete triples: tasks where all three
  arms were measured with complete usage.



| Target | Criterion |
| --- | --- |
| Validity | Every slot recorded; no instrument failure; at least 10 of 12 tasks with all three arms measured and complete |
| Rule delivery | J2 delivers the rule in every measured missing and correction run |
| Quality | Over complete triples, J2 passes both checks in at least as many runs as FULL, minus one |
| Tokens | Over complete triples, J2 uses at most 60% of FULL's combined coding and JEV tokens |

The thresholds are unchanged from version 4; version 5 adds only the
complete-triple basis. The thesis is supported only when all four hold. A correctly measured negative
result completes the experiment.

## Limits

Two synthetic families and two repetitions can't support a population estimate.
Catalogs are capped at 32 parts and 32 KB, far below real long Sessions, so the
measured savings understate what longer histories would show, and also leave
their risks untested. Excluding tool output assumes the workspace still holds
the files and commands that produced it.
