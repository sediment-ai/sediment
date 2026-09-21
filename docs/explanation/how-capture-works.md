# How capture works

Sediment records events from coding agents, gateways, git, and forges as
immutable Facts. It derives Attribution, edit retention, and Rewards later, so
every result can be recomputed under a different policy.

This page explains the capture boundary. For setup procedures, use
[Configure local capture](../capture/local-capture.md) or
[Roll out managed capture](../capture/managed-capture.md).

## The capture boundary

Developer machines observe Developer decisions, Edit observations, Retry
linkages, and commit Attribution. Gateways observe inference calls.

Forges and CI systems observe Pushes, Pull request revisions and merges, and CI outcomes.
The Sediment API validates and stores those events as Facts.

No client computes an Attribution or Reward. No request handler persists a
derived result. Capture stores Facts; Derivation decides what those Facts
mean.

For CI, capture preserves provider run and attempt identity, the exact provider
result, a normalized terminal result, and supplied structured metadata.

Capture doesn't parse logs or guess whether a failure was a flake or whether
code caused it.

## Data flow

```text
coding agent ── decisions, observations, and Retry linkages ──► OTLP /v1/logs ─┐
LLM gateway ── successful inference calls ─────► /ingest/gateway│
forge ──────── Pushes, pull requests, CI outcomes ► webhooks ──┼─► immutable Facts
                                                               │
git hooks ──── Session markers and notes ─────► pushed notes ──► git mirror
                                                               │
                                  Facts + mirror + policy ─────┴─► Derivations
```

The API authenticates each ingest path with a bearer token or webhook HMAC.
Database uniqueness enforces idempotency when a client or forge redelivers an
event.

## Gateway request and capture paths

The gateway owns the model request path. It authenticates the agent, selects a
model deployment, sends the request to the model provider, and returns the
response. Sediment receives a copy of a completed call through a separate
capture path:

```text
agent ── model request ──► gateway ── provider request ──► model provider
  ▲                          │                                  │
  └──── model response ──────┴──────── provider response ◄──────┘
                             │
                             └── completed-call payload ──► Sediment API
                                                               │
                                                               └──► InferenceCall Fact
```

This separation keeps a Sediment API outage outside the model request's
availability boundary when the gateway export is fail-soft. A gateway or model
provider failure can still delay or fail the model request. Sediment's LiteLLM
callback logs and drops a delivery failure without changing a successful model
response. Another gateway integration needs the same failure boundary.

An LLM gateway isn't an unconditional passthrough. It can translate protocols,
select another model, retry against a fallback, return a cached response, apply
a guardrail, or modify a prompt. Those choices can change model behavior.
Unsupported parameters or content types can also change behavior when the
gateway translates between client and provider protocols.

A thin gateway configuration should not change model quality when it preserves
the request and parameters, sends them to the same provider and model, and
doesn't enable substitution, fallbacks, caching, guardrails, or prompt
rewriting. It still adds network and processing latency plus another
operational failure point. Because model generation is nondeterministic, a
single direct-versus-gateway response pair doesn't establish equivalence.

The ingest envelope identifies the capture gateway and carries its completed
call payload. Each payload format needs a registered Sediment adapter that
normalizes it into an `InferenceCall`. The repository registers only the
LiteLLM adapter. Other gateway names in the schema identify the namespace; they
don't claim an implemented integration.

Bundled LiteLLM is the tested on-ramp for a team that doesn't operate a
gateway. Its default configuration authenticates clients, maps requested
`claude-*` names to the matching Anthropic model, and forwards successful-call
payloads to Sediment. It doesn't configure model substitution, fallbacks,
caching, guardrails, or prompt rewriting. It is enabling infrastructure for
capture, not a general-purpose Sediment gateway product.

## Captured signals

| Signal | Observer | Stored Fact | What it establishes |
|---|---|---|---|
| Inference call | LLM gateway | `InferenceCall` | The model input and output as ordered typed message parts |
| Developer decision | Agent hook or shim | `DeveloperDecision` | Whether an edit-tool call was accepted |
| Edit observation | Session-end hook | `EditObservation` | The applied text and later observed file text |
| Push | Forge webhook | `Push` | Which commits reached a branch |
| Session-to-commit observation | Successful mirror refresh | `SessionCommitObservation` | When Sediment first observed a valid Git-note relationship |
| Pull request revision | Forge webhook | `PullRequestRevision` | A head and optional preceding head observed before merge |
| Pull request merge | Forge webhook | `PullRequestMerge` | The final head and merged commit boundaries |
| CI outcome | Forge webhook or CI API | `CIOutcome` | One provider run attempt's exact and normalized terminal result |

A `RejectedEdit` can preserve refused model text when a harness observes it.
It supplements the decision path; it doesn't turn a rejected proposal into an
applied Edit observation.

A `RetryLinkage` records that a developer refused one Edit or Write call,
entered correction text, and accepted a later successful call for the same
file in the same Session.

It stores the two call IDs and event metadata, not the correction or either
attempt's text. It records no training eligibility.

Each developer-side Fact belongs to a real Session. The Session is the
aggregate root that joins Facts from one agent run.

## Developer decisions by agent harness

A Developer decision is one Fact at the unit that a coding agent exposes. It
isn't a cross-agent-normalized human gesture.

One apparent developer action can produce no decision Fact, one Fact, or
several Facts depending on the agent harness.

| Agent harness | Observed event and verdict | `accepted` | `explicit` | Decision unit | Missing signals and limits |
|---|---|---|---|---|---|
| GitHub Copilot (`copilot`) | `copilot_chat.edit.feedback`: `outcome=accepted\|rejected`; `copilot_chat.inline.done`: boolean `accepted`; `copilot_chat.edit.survival` for `edit_source=apply_patch`: `survival_rate_no_revert` | The feedback outcome or inline boolean maps directly. Survival is accepted when the rate is greater than zero. | Feedback and inline decisions are explicit. Survival decisions are implicit. | Each feedback or inline event produces one Fact. Each emitted survival observation, including each time-delay bucket, produces another Fact. | A feedback outcome other than `accepted` or `rejected`, including `saved`, produces no Fact. Neither does a non-Boolean inline `accepted` value or a nonnumeric `survival_rate_no_revert`. Inline survival is skipped to avoid double-counting `inline.done`. A missing or malformed file path, Session id, or event time also produces no Fact. |
| Claude Code (`claude-code`) | `claude_code.tool_decision`: `decision=accept\|reject` | `accept` is true. `reject` is false. | `user_permanent`, `user_temporary`, `user_abort`, and `user_reject` are explicit. `config` and `hook` are implicit. An unknown source degrades to implicit and leaves a log trail. | One Fact per Edit, Write, MultiEdit, or NotebookEdit tool call. | A reject emits no tool result. A result can also cross a batch boundary, `OTEL_LOG_TOOL_DETAILS` can be disabled, or `tool_input` can be unparseable. These cases retain the decision with an empty file path. Unknown verdicts skip with a log trail. |
| Codex (`codex`) | `codex.tool_decision` for native `apply_patch` or an `exec_command` that starts with an `apply_patch` heredoc: `approved\|approved_for_session\|denied\|abort` | `approved` and `approved_for_session` are true. `denied` and `abort` are false. | Only `source=user`, compared case-insensitively, is explicit. Other sources are implicit. | One Fact per distinct file path extracted from a V4A patch. A multi-file patch therefore expands one verdict into several Facts. Repeated markers for one path collapse within the verdict. | An interactive rejection can emit no decision. A pathless native patch, reject, missing result, or cross-batch result produces one pathless Fact. A shell-tool decision without its in-batch result produces no Fact because the decision alone doesn't prove an edit. Unknown verdicts skip with a log trail. Other shell commands don't produce decision Facts. |
| Cursor (`cursor`) | A successful Agent `Write` `postToolUse` causes the adapter to emit `sediment.tool_decision` with `decision=accept`. | Always true for an emitted Cursor decision. | Always false. The event records an automatic tool result, not a human approval gesture. | One Fact per successful Agent `Write` tool call. | Failed Agent `Write` calls and Tab edits emit no decision. Tab supplies no per-call identifier. A missing `conversation_id` or `tool_use_id`, absent telemetry configuration, or delivery failure also produces no Developer decision Fact. |
| pi (`pi`) | A successful `edit` or `write` `tool_execution_end` causes the shim to emit `sediment.tool_decision` with `decision=accept`. | Always true for an emitted pi decision. | Always implicit. | One Fact per successful `edit` or `write` execution. | Errored executions and non-edit tools emit no decision. Stock pi exposes no explicit human gesture. |

`accepted_decisions` counts visible decision Facts where `accepted=True`. It
doesn't normalize their units across agents.

Native Cursor successful `Write` receipts have a deterministic Fact identity.
PostgreSQL preserves the first receipt across redeliveries with later receipt
times. Historical random-ID Facts remain unchanged; the first post-upgrade
receipt can coexist with one. Cursor's recorded time is hook receipt time,
not a supplied execution timestamp.

A check for explicit-accept eligibility uses
`any(d.accepted and d.explicit)` rather than comparing count magnitude across
agents.

The Attributed completion is the canonical cross-agent comparison surface. It
keeps each Developer decision's agent harness, acceptance, and explicitness
beside CI and Attribution evidence.

[Label-confidence inspection](../reference/cli.md#sediment-report-label-confidence-inspection)
shows those fields with the selected Confidence branch and factor breakdown.

Its sensitivity mode sweeps `LabelConfidencePolicy` over the same artifacts.

Sediment doesn't infer a normalized human-gesture count from harness-specific
Facts or expose per-harness Reward weights.

A library caller can pass a custom `LabelConfidencePolicy`. The standard DPO,
SFT, and diff-SFT export commands use the default policy.

Quarantined Facts are absent from Derivation reads. Database uniqueness
indexes handle Fact deduplication before Derivation.

## Capture receipts

After an authenticated `POST /v1/logs` completes every storage call, the receiver
logs an `otlp_logs_received` receipt with its deployment `org_id`. The HTTP
success body remains `{}`. The receipt separates three units:

- `record_counts` partitions every supplied `logRecords` list entry into
  `translated` (emits at least one Fact), `untranslated` (an object emits no
  Fact), or `malformed` (the entry isn't an object). Their sum is `received`.
  Unsupported and declined records are untranslated. A tool-result record
  that supplies context but emits no Fact itself is also untranslated.
- `malformed_containers` counts non-list repeated fields and non-object
  resource or scope entries that prevent record enumeration. Missing repeated
  fields mean empty lists. Invalid containers don't imply a record count.
- `fact_counts` reports `candidates`, `stored`, and `duplicates` for
  `developer_decisions`, `edit_observations`, `rejected_edits`, and
  `retry_linkages`, including zeros. For each type, candidates equal stored plus
  duplicates. A Codex decision can emit several Facts while counting as one
  translated record. `retry_linkage_skips` retains the translator's skip reasons.

A storage failure emits no completed receipt. Earlier storage calls can have
committed Facts before the failure. On replay, PostgreSQL uniqueness indexes
count those Facts as duplicates while the remaining Facts store. These receipts
describe ingest operations; they don't count later quarantine or export exclusions.

## Session identity

Harness hooks know the Session identifier directly. Shims carry it in OTLP
attributes. Gateway clients use request metadata or a protocol-specific
field that the server resolves.

The server skips an inference call whose `session_id` can't be resolved. It
never stores the call under a placeholder Session. Structured logs name the
skip so an operator can distinguish missing capture from an empty Session.

Identity belongs at the edge that observes it. A downstream matcher can't
reconstruct a Session identifier from timestamps or file paths without
guessing.

## Commit Attribution

An agent hook appends a marker after each edit. The marker contains the agent
name, Session identifier, and timestamp. It contains no prompt, output, diff,
or file content.

At commit time, a git hook unions the markers into a JSON note under
`refs/notes/sediment`. Git carries the note through amend and rebase. A
pre-push hook reconciles and pushes the notes ref with the branch.

When the forge reports a Push, the mirror reads the note and joins the commit
to the named Sessions. Notes Attribution is deterministic because the client
recorded the relationship when it happened.

After a successful mirror refresh, Sediment stores the first
`SessionCommitObservation` for each Git-note Session-to-commit relationship in
the Push's bounded commit range. The Fact preserves when Sediment learned the
relationship. A historical Derivation can exclude observations captured after
its `as_of` boundary without reading a later mutable note. Sediment doesn't
fabricate observation times for notes that predate this capture path.

If a note is missing or invalid, the Attribution deriver can fall back to
jaccard similarity between completion paths and commit paths. The fallback is
visible as a different Attribution source.

[Attribution](attribution.md) defines the scoring and fallback semantics.

## Edit retention and external deltas

An applied edit can change again before the Session ends. Transcript capture
sends the model-written text and the file's Session-end state as an
`EditObservation` Fact.

The edit retention deriver uses four-gram containment. The comparison asks what
fraction of the model's snippet remains in the larger file, so a symmetric
similarity metric would answer the wrong question.

The final Fate Derivation maps that score into `deleted`,
`partially_modified`, or `unmodified`. The default policy uses inclusive outer
thresholds: scores through 0.1 are deleted, and scores from 0.9 are unmodified.
The outcome report and dataset diagnostics expose Fate as a diagnostic. Fate
doesn't change Reward, Confidence, Evidence recipes, or training rows.

Merge retention extends this observation to review and integration. It derives
two scores from the original attributed commit-file addition: one at the final
pull request head and one at the provider-reported merged commit. A low score
means the original text didn't remain at that boundary. It doesn't establish
why the text changed or whether the original response was wrong.

### Transcript extraction

At Session end, the extractor reads applied Edit and Write calls. It excludes
failed and rejected calls from `EditObservation` pairs. It also ignores history
stamped with another Session identifier.

For a rejected Claude Code edit, the extractor emits the model's proposed text
as a separate `RejectedEdit`. It doesn't attach observed file state because
the tool never applied the proposal. Failed calls emit neither Fact.

When a developer text entry follows that refusal and precedes a later accepted
Edit or Write for the same file and Session, the extractor emits a
`RetryLinkage`.

It excludes tool failures, implicit refusals, agent-only rewrites, pure
regenerate sequences, and cross-file or cross-Session attempts.

The extractor reads the Session-end content of each edited file and sends one
OTLP batch. It drops an `EditObservation` when either text side exceeds 256 KiB
and a `RejectedEdit` when the proposed text exceeds 256 KiB.

The client is best-effort and exits successfully after logging a dropped pair.
A Session that crashes or never fires its end hook can miss this signal without
losing decisions or Attribution.

Observations deduplicate first-write-wins on organization, agent harness,
Session, and call ID. A repeated SessionEnd event therefore collapses in the
database.

### External edit windows

A low edit retention score can't distinguish an agent revision from a correction by
something else. External line counts measure what changed while the agent's
edit tools weren't editing the file.

External doesn't mean human. A formatter, watcher, generator, git checkout, or
the agent's own shell command can change the count. The Fact records the lines;
policy decides how to interpret them.

The measurement needs the file state between two agent edits. Claude Code's
transcript can omit `originalFile`, so transcript fields alone cannot provide
complete pre-edit file state.

A PreToolUse hook therefore records per-line hashes before an Edit or Write. It
also computes the hashes that the applied edit is expected to produce. Raw
snapshot lines never enter the cache or payload.

An edit window closes when the next applied edit of that file begins. The last
window closes against Session-end content. Rejected and failed calls don't
close a window because they didn't change the file.

Counts ship only when every applied edit on a file has a complete window and a
pair. Other files in the Session remain eligible. This all-or-nothing rule
prevents a partial chain from reporting the agent's own later edit as external.

That capture guarantee doesn't detect later partial quarantine. If one Edit
observation is hidden, `external_lines_after` sums the surviving windows for
that file, Session, agent harness, and organization. The remaining Facts don't
identify the missing window. Their external-line totals can therefore
understate the change without a partial-coverage flag, including reporting zero
when a hidden window contained changes.

This limitation affects diagnostic Fate `external_lines_added` and
`external_lines_removed`, plus external-change counts and rates in outcome,
lifecycle, and dataset diagnostic reports. It doesn't change the surviving
observation's text-based retention score or Fate category. These external counts
don't supply training labels or Rewards. Missing windows aren't reconstructed
from quarantined content. The
[quarantine procedure](../operate/deploy.md#83-quarantine-and-wholesale-deletion)
explains how to exclude affected diagnostics from interpretation.

## Delivery guarantees

Client hooks favor developer workflow availability over guaranteed delivery.
An Attribution hook can't fail a commit or push.

A transcript hook logs and drops an invalid or oversized pair. A gateway
callback logs and swallows an API failure.

Each path leaves evidence for diagnosis: hook health, local structured logs,
API skip logs, Fact counts, and forge delivery history.

Redelivery is safe because the database owns deduplication. Clients don't scan
for prior events or mutate stored Facts.

## Facts and Derivations

Facts record events: a response arrived, an edit was accepted, text survived to
Session end, a commit was pushed, or CI finished. Facts are appended and never
updated.

Attributions, edit retention scores, final Fates, and Rewards are pure functions
of Facts and policy. A policy change can recompute all history without rewriting
capture.

The boundary also keeps uncertain evidence honest. A missing outcome remains
absent. A failed mirror refresh preserves the Push Fact. A matcher records its
Attribution source instead of presenting a fallback as deterministic evidence.

## Privacy boundaries and ceilings

Inference-call capture contains full prompt and response content. A repository
mirror contains pushed code. Decision Facts contain metadata such as file path,
tool name, Session id, and user id.

Codex native Developer decisions can also retain patch code and other tool
arguments in `raw`. This path is independent of the transcript hook and
`--transcripts`. `log_user_prompt=false` disables native user-prompt logging;
it doesn't strip tool-argument content. pi's decision-only path carries no
applied or observed file text.

Before PostgreSQL writes a content-bearing Fact, Basic redaction replaces
high-confidence API keys and bearer credentials with
`[REDACTED_CREDENTIAL]`. The fixed pattern set covers normalized inference-call
content, tool inputs, applied and rejected edit text, and every `raw` payload.

Basic redaction logs counts without logging source text. Identity and dedup
fields never change, so redaction can't alter Attribution or idempotency.

Basic redaction has no operator configuration and isn't a comprehensive secret
scanner. It doesn't support per-org redaction policies.

Existing Facts remain immutable. Quarantine excludes a Fact captured before
redaction without rewriting it.

Attribution notes contain only Session ids, agent names, and timestamps.
Transcript capture contains model-written edit text, Session-end content for
files that the agent edited, and external line counts.
pi requires the separate `SEDIMENT_PI_TRANSCRIPTS=1` opt-in for content at
shutdown or optional settle. Its endpoint and token authorize decision delivery
only. Codex and Claude Code use their opt-in transcript hooks.

A `RejectedEdit` contains identifying metadata and the model proposal that the
developer refused. Its content payload contains no observed file state or
developer-authored replacement text.

A `RetryLinkage` contains identifiers and event metadata only. It contains no
raw transcript, correction text, or duplicate attempt text.

The transcript payload contains no raw transcript, prompts, Read results, tool
results, or environment.

Operator evidence reads expose selected canonical Inference-call parts and
message metadata, excluding `raw` and user identity. A tool result needs a
captured model request that contains it. Reads cannot establish complete capture
or reconstruct missing workspace state. See
[Continue a task with captured evidence](../operate/resume-with-evidence.md).

The external-delta cache stores per-line hashes under
`<base>/sediment/deltas`. The base is `SEDIMENT_DELTA_CACHE`, then
`XDG_CACHE_HOME`, then `~/.cache`.

If none resolves because no home directory exists, the cache no-ops and the
emit proceeds without deltas.

The extractor removes its Session snapshots after SessionEnd. A crash can
leave snapshots behind; the next cleanup reaps sibling Session directories
older than seven days.

Claude Code transcript survival covers Edit and Write. NotebookEdit and legacy
MultiEdit fall back to commit Attribution. Codex covers successful single-file
patches; pi covers successful `edit` and `write` calls. External line counts
cover Claude Code only; pi and Codex have no snapshot equivalent.

An agent's later revision can lower its earlier edit's survival. External line
counts reduce that ambiguity but don't identify the actor that changed a line.
The Derivation must keep that limit visible.
