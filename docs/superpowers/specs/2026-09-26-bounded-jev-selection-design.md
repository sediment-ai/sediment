# Bounded JEV evidence selection

Implementation tracker: [Issue #134](https://github.com/sediment-ai/sediment/issues/134).
Earlier experiment: [issue #89](https://github.com/sediment-ai/sediment/issues/89)
and its [specification](2026-09-22-jev-budgeted-resumption-design.md).

## Purpose and boundary

The earlier JEV arm asked for one global history choice plus separate passage
scores. A `no_history` choice, or a low-confidence choice converted to
`insufficient`, suppressed every passage. In all nine JEV runs, the coding agent
received no history. This experiment tests a different policy. It keeps the
first keyword evidence and asks JEV only whether a few later candidates add
missing or conflicting information. The question is whether that policy lowers
complete resumption token usage while preserving task correctness and
historical requirements, relative to Sediment's keyword selection.

Decision: run this one-pass comparison (Phase 1) as a separate, versioned
experiment. The earlier scripts, fixtures, outputs, and verdicts stay unchanged,
and issue #89 keeps its acceptance record. Adaptive expansion (Phase 2) needs
separate fixtures and a separate schedule; it never runs to rescue a Phase 1
result.

Selection runs in the experiment consumer at resumption, before the first
coding-model request. It reuses the grant-scoped factual reads from
[ADR 0026](../../adr/0026-grant-scoped-factual-evidence.md). A reference never
authorizes a read. The experiment adds no Fact, schema, API route, grant,
Derivation, training-export change, dependency, or package. JEV receives
synthetic evidence only.

## Arms

| Arm | Policy |
| --- | --- |
| K | Version-1 keyword tokenization, ranking, reasoning exclusion, stable tie-breaking, and whole-part packing, unchanged from `budgeted_context_selection.py`. At most eight parts and 8,192 serialized UTF-8 bytes. No selector call. |
| J1 | The same keyword order. Keep the first two complete parts that fit the final envelope as initial evidence. Ask JEV about up to four later candidates in one request. Add qualifying candidates under the same envelope. No global history choice. |

Both arms read the same authorized catalog of one final captured call: at most
32 occurrences and 32,768 serialized bytes. An oversized source is refused, not
truncated.

## J1 policy, version 1

These values define an experiment policy, not calibrated probabilities.

1. Rank the catalog with the version-1 keyword policy. Keep the first two parts
   that fit the final envelope as initial evidence. Count each whole-part
   omission. Never slice a part.
2. Take up to four later ranked occurrences as candidates. Pack whole
   candidates, in rank order, into one request of at most 16,384 bytes. The
   request budget includes the task, the initial evidence, every question, and
   JSON escaping. Count each omitted candidate.
3. The request state contains the visible task, a short historical-record note,
   and the initial evidence and candidates. Each item carries a short
   identifier (`e1`, `c1`), its conversation order, its role, and its exact
   part content. Long reference identifiers and tool-call identifiers stay in a
   local map; a tool result carries its tool name instead. The consumer
   generates no summaries.
4. Ask three independent yes/no propositions (Noul questions) per candidate:
   relevance to the task, information absent from both the task and the initial
   evidence, and unresolved conflict with them. Each instruction names its
   candidate (`candidates[i]`, id `c1`) and the state fields it compares. Each
   question has explicit `true` and `false` criteria. No question depends on
   another question's answer.
5. A candidate qualifies when relevance is at least 0.60 and either new
   information or conflict is at least 0.60. Qualifying candidates sort by
   decreasing relevance, then new information, then occurrence identity. The
   consumer adds whole parts after the initial evidence within the eight-part,
   8,192-byte envelope.
6. Initial evidence stays regardless of the answers.
7. The consumer re-fetches the final references through the factual read route.
   It verifies exact content, identity, and Quarantine revision before delivery.

Novelty is relative to the task and initial evidence in the request. The
policy doesn't remove duplication between two candidates. If a qualifying
conflicting candidate doesn't fit the envelope, the record reports an
unresolved conflict; the consumer doesn't invent a resolution.

### Fallback and refusal

| Condition | Outcome |
| --- | --- |
| No candidates after the initial evidence | Skip JEV (`no_additional_candidates`); deliver the initial evidence; zero selector usage. |
| No candidate fits the request budget | K's output with reason `controller_request_budget`; zero selector usage. |
| JEV HTTP error, transport error, oversized response, or invalid response | One attempt, no retry. K's output with the provider reason. Attempted usage stays unknown unless a valid usage object arrived. |
| Missing or invalid credential, deadline, grant, Quarantine, source identity, or changed content | Refusal. No fallback and no cached content. The coding agent doesn't start. |
| Empty authorized corpus | Refusal `empty_corpus`, distinct from a size refusal (`catalog_limit`). |
| No keyword match, or no matching part fits | Empty context with gap `no_keyword_match` or `initial_budget`; the coding agent continues from the visible task. |

The summary reports fallback runs separately from JEV-controlled runs.

## Fixtures and validators

Two held-out families use different programs and semantics:

- **Event aggregation** (`event_rollup.py`). The hidden rule counts each event
  ID once, and the last line with that ID is authoritative.
- **Configuration resolution** (`layered_config.py`). The hidden rule makes an
  assignment with an empty value delete its key.

A third family, duration parsing, is the development set. Its hidden rule
treats a bare integer as minutes. Each family has three history profiles:

| Profile | Source Session | Visible continuation |
| --- | --- | --- |
| Missing | Task, inspection, then a separate note that states the rule | Omits the rule |
| Redundant | The same, plus an unrelated failed diagnostic | States the rule |
| Correction | Task, inspection, an archived diagnostic distractor, an obsolete note, then an explicit correction | Omits the rule |

Source Sessions send each prompt in sequence through the native harness. The
rule arrives in a separate, low-overlap note so that the keyword ranking
doesn't make it initial evidence. A simulated capture places the necessary note
among J1's candidates and inside K's envelope for every missing and correction
profile. Real captured ranks can differ; the runner records them and never
enforces them.

`evaluation/verify.py` holds the independent checks, and `evaluation/labels.json`
holds the evidence labels. Neither enters a model input or a workspace. Tests
prove that each reference passes and each initial program fails. They also
prove these mutations fail the intended check: removing the rule, applying the
obsolete rule, breaking ordinary behavior, and following the distractor.
Visible checks never reveal the rule.

## Protocol

The held-out schedule is 6 tasks × 2 arms × 2 repetitions = 24 continuations.
The order is repetition-major. K goes first for six task pairs and J1 for the
other six, and each task's two repetitions use opposite orders. Each
continuation uses a distinct Session, container, and workspace copy.

### Generation contract

The earlier gate required `max_tokens` to equal 2048. Pinned pi 0.84.1 lowers
it to `min(2048, max(1, 16384 − estimated context − 4096))` under context
pressure, so long-context runs stopped at that guard. Both arms use one
contract: `temperature` 0, streaming, the pinned model, and an integer
`max_tokens` from 1 to 2048. The gate records every request's value.
Compaction, automatic retries, and native retrieval tools stay disabled. The
limits are 12 coding calls, a 16,384-token context window, and 900 seconds for
selection plus coding.

### Freeze and execution

`protocol_identity` binds the runner, selector, earlier keyword selector,
legacy controller, fixture and validator hashes, the policy, the generation
contract, runtime pins, the JEV route type (`direct` or `loopback_proxy`), and
the run order. Preflight, source capture, and the matrix must share that
identity. The runner verifies that the retrieval credential names exactly the
captured source Sessions.

A resumed matrix records a started slot without a result as `interrupted`. It
never reruns or overwrites a slot. A status outside `settled`,
`budget_exhausted`, and `run_deadline` is an instrument failure. So is an
unverified capture, delivery, or final-workspace check. An instrument failure
stops the experiment and blocks resumption. The runner validates the final
workspace even after a resource stop or a capture failure.

A call-limit stop counts as a measured unsuccessful continuation only when all
12 dispatches completed, usage is known, and capture is verified. Missing
coding or selector usage leaves the measurement incomplete.

### Measurements

Each run record keeps separate axes: task outcome, selection detail, per-model
usage, request counts, traffic bytes (headers excluded), latency components,
and integrity checks. The summary reports quality by arm, family, and profile;
coding and selector input, output, and cache tokens; the combined token
reduction; per-task paired differences; JEV decisions and fallback reasons; the
parts JEV added; and whether the delivered rule came from initial evidence or a
JEV addition.

## Acceptance and interpretation

- **Validity:** offline checks, real API checks, and preflight pass; every slot
  has a record; required axes are complete.
- **Quality:** J1 passes both checks in all 12 of its continuations. A savings
  claim also requires K to pass all 12.
- **Tokens:** J1's combined coding and JEV input plus output is at least 10%
  lower than K's. Tokenizers differ, so this sum is an accounting proxy.
- **Latency:** secondary; report medians and ranges without excluding runs.
- **Attribution:** if initial evidence carried the rule, JEV's contribution is
  unproven.

A correctly measured negative result completes the experiment. Don't tune and
rerun held-out tasks. A repaired protocol needs another version and fresh
held-out tasks.

## Runtime requirements

Live execution needs a scoped `JEV_API_KEY`, egress to `api.typesafe.ai`, a
reachable coding backend for the pinned model, Docker, PostgreSQL 17, the
pinned pi harness image, and a LiteLLM gateway route with the Sediment capture
callback. The runner accepts an explicit loopback HTTP proxy for the JEV
connection; it ignores ambient proxy variables. A different coding model requires the user's
choice and becomes a separate experiment; don't compare its absolute token
counts with the earlier Ministral results.

## Limits

Six synthetic tasks and two repetitions per arm can't support a population
estimate. The comparison tests a combined policy change; it doesn't attribute
a gain to compaction, initial evidence, or question wording separately. Hosted
JEV doesn't show that the decision model can run inside a customer perimeter.
Local coding compute cost is unmeasured.
