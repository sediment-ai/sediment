# Agent-requested context from one previous Session

Date: 2026-09-21

Status: implementation in progress under [issue #69](https://github.com/sediment-ai/sediment/issues/69)
and [PR #70](https://github.com/sediment-ai/sediment/pull/70). The implementation
must record validation before it claims continuation benefit.

## Outcome

A coding agent in a fresh Session asks a question about one previous Session.
Sediment returns a bounded selection of exact captured evidence. The agent uses
that evidence to continue a task in a preserved workspace. A person chooses the
source Session but doesn't choose the evidence parts or write a handoff summary.

The first tool is `sediment_retrieve_context` in the existing pi extension.
It returns evidence that helps the agent answer; it doesn't generate an answer.
A deterministic keyword selector supplies a baseline. One controlled coding
task, repeated across three comparison arms, tests whether retrieval helps.

Decision: use pi's native tool interface for this slice. An MCP (Model Context
Protocol) server would support more clients but adds another integration to
validate. A CUA-S1 or Jev selector would also require model selection, evaluation,
and potentially training. Both remain follow-up work.

## Foundation and scope

Reuse the evidence identity and read boundaries delivered by
[PR #65](https://github.com/sediment-ai/sediment/pull/65), described in
[ADR 0021](../../adr/0021-bounded-evidence-access.md). Reuse pi capture and the
[controlled continuation procedure](../../operate/resume-with-evidence.md).

| Existing owner | Reuse or addition |
| --- | --- |
| `packages/core/sediment_core/models.py` | Reuse canonical Inference messages and parts. No Fact shape change. |
| `packages/core/sediment_core/evidence.py` | Reuse occurrence references and exact read items; add a frozen source projection and share bounded strict encoding between packing and API publication. |
| `packages/core/sediment_core/store.py` | Add one bounded, Quarantine-excluding content projection within a caller-owned snapshot. Reuse evidence scope, inventory, and byte preflight. |
| `packages/derive/sediment_derive/similarity.py` | Reuse `tokenize` without changing its behavior for existing callers. |
| Proposed module: packages/derive/sediment_derive/context_retrieval.py | Own pure keyword selection, policy version, response projections, and counted exclusions. |
| `apps/api/sediment_api/config.py`, `deps.py` | Add an optional fixed Session read credential and explicit route authority checks. |
| `apps/api/sediment_api/routers/query.py`, `workers.py`, `worker.py` | Add one read route using the existing disposable evidence worker and serializer. |
| `shims/pi/` | Register one opt-in native tool and perform one bounded HTTP request per invocation. |

Keep the five pipeline packages, the existing CLI member, and the scoped
TypeScript shim. Add no package, database migration, model dependency, embedding
index, checkpoint, persistent summary, or retrieval cache. The operator evidence
commands remain available without changes to their read semantics.

This slice supports one configured source Session per enabled deployment.
Concurrent grants for several source Sessions, Session discovery, repository
search, workspace restoration, automatic compaction, and automatic task restart
are outside its scope. A question can retrieve only captured Inference-call
content. Transcript capture alone doesn't supply that content.

## Architecture and read authority

The request path is pi tool → authenticated query route → disposable worker →
bounded FactStore projection → pure selector → exact evidence response.
The API executes selection without giving the agent deployment-wide read access.
The storage layer owns visibility and source identity; the selector owns
relevance and output packing. Selection isn't persisted.

Decision: add a fixed retrieval authority to the existing API. The alternatives
are a separate credential-holding broker or an agent with the operator token.
The broker duplicates service lifecycle and request controls. The operator token
grants broader authority than this task requires.

This decision requires an ADR during implementation that explicitly amends
[ADR 0018](../../adr/0018-static-credential-authorities.md) and extends
[ADR 0021](../../adr/0021-bounded-evidence-access.md). Don't relabel the existing
operator credential or broaden the three evidence routes.

### Deployment configuration

Add optional `SEDIMENT_RETRIEVAL_TOKEN` and
`SEDIMENT_RETRIEVAL_SESSION_ID` settings. Both absent disables retrieval. Setting
only one fails configuration validation, including in development mode.
The Session identifier uses `NonEmptyId`; the token uses `SecretStr`.
When enabled, the token must meet the existing production secret rules and must
differ from every operator, ingest, legacy, and webhook secret. Enforce these
retrieval-specific requirements in development mode too.

The deployment supplies the organization and the source Session. Requests can't
choose either. There is no token issuance endpoint, grant database, account
model, or dynamic authorization policy. Configuration takes effect on restart.
Removing both settings and restarting revokes access. Changing the Session
requires rotating the token so a previous consumer doesn't acquire access to a
different Session. The server doesn't claim to detect secret reuse across
restarts.

| Surface | Operator | Ingest, including legacy | Retrieval |
| --- | --- | --- | --- |
| Existing bearer-authenticated ingest routes | Allow | Allow | Deny |
| Existing query, report, and Fact reads | Allow | Deny | Deny |
| `POST /query/context`, when configured | Allow to the fixed Session | Deny | Allow to the fixed Session |
| `/v1/me` | Allow | Allow | Allow; report `authority: "retrieval"` |

The retrieval identity uses reserved `client_id: "retrieval"` and reports the
configured `source_session_id` in `/v1/me`. Ingest enrollment rejects this
authority. Operator login continues to require operator authority. Preserve all
signature-authenticated webhook and public health behavior.
If a deployment already uses `retrieval` as an ingest client identifier, its
operator must rename that configured client before upgrading. Fail with a
content-free configuration error rather than reclassifying its credential.

Audit every caller of `verify_token` and `verify_ingest_token`. The existing
ingest dependency accepts every authenticated identity; it must explicitly allow
only ingest and operator identities once retrieval exists. Unknown credentials
return 401; a known credential without the route's authority returns 403.

The agent receives only the retrieval token and its API endpoint. Its execution
environment must not contain an operator login, database credentials, deployment
configuration, or a host mount that exposes those credentials. A separate
process under the same unrestricted account doesn't establish that boundary.
For the demonstration, use a separate container or operating-system account.
Keep the gateway and model endpoint inside the customer perimeter as well.

## Tool and HTTP contract

The tool accepts `query` and optional `max_bytes`. It supplies `schema_version`
itself. Its description states that selection uses English/code keywords, reads
one authorized previous Session, and returns historical evidence with unknown
capture completeness. It asks the agent to include relevant symbols, file
names, commands, or error terms when available.

The extension makes one `POST /query/context` request:

```json
{
  "schema_version": 1,
  "query": "pytest parser failure and earlier constraints",
  "max_bytes": 16384
}
```

| Request field | Contract |
| --- | --- |
| `schema_version` | Strict integer literal `1`. |
| `query` | Nonblank string, at most 2,048 UTF-8 bytes; reject unpaired surrogates. Tokenization must leave at least one non-stopword token. |
| `max_bytes` | Strict integer, 4,096–65,536 inclusive; default 16,384. Limits the entire encoded success body, not model tokens. |

Reject unknown fields, including Session, organization, reference, endpoint,
model, and credential selectors. Bound the raw request body at 16 KiB before
parsing; don't trust `Content-Length`. Authentication precedes body validation
errors wherever existing route behavior requires it. Errors never echo input.

### Successful response

Use frozen dataclasses for the response and policy. Pydantic remains limited to
the HTTP request envelope, validated canonical Facts, and settings. Version 1
contains exactly these top-level fields:

| Field | Meaning |
| --- | --- |
| `schema_version` | Integer `1`. |
| `policy_version` | Integer `1`; identifies the keyword, exclusion, ordering, and packing rules. |
| `source_session_id` | The configured Session identifier. |
| `quarantine_revision` | The read snapshot's revision. |
| `status` | `matched`, `no_match`, or `budget_exhausted`. |
| `capture_completeness` | Literal `unknown`. |
| `coverage` | The counts defined next. |
| `skipped` | Every closed exclusion reason with a nonnegative count, including zeros. |
| `items` | At most eight selected items, in relevance order. |

`coverage` contains `visible_inference_calls`, `quarantined_inference_calls`,
`scanned_parts`, and `complete_visible_scan: true`. Success examines every
visible call and every canonical part within the fixed capacity envelope.
This reports a complete scan of visible storage, not complete capture or
complete evidence in the response. A known Session without calls can succeed
with an empty result. An unknown Session returns `evidence_unavailable`.

Each item contains an integer `score` and `evidence`, an existing exact
`EvidenceReadItem`: occurrence reference, observation time, historical role,
finish reason, and complete canonical part. The score counts matched query
tokens. It isn't a probability, Confidence, Reward, or training label.

`matched` means at least one item fits. `no_match` means no eligible part has
positive overlap. `budget_exhausted` means positive eligible matches exist but
none fits the response budget. The latter two return empty `items` and retain
coverage and exclusion counts. They don't authorize claims that an event never
happened. The response doesn't echo the question or generate a prose summary.

### Errors

Reuse 401/403 authority handling, 400/422 malformed-request handling, the
pre-parse 413 body limit, and 503 worker/database failure handling. An operator
request to the disabled route returns 404; no retrieval identity authenticates
when the feature is disabled.

HTTP 409 carries a content-free `detail.reason`:

| Reason | Meaning |
| --- | --- |
| `evidence_unavailable` | The configured Session isn't available in the deployment. |
| `evidence_inventory_limit` | More than 1,000 visible Inference calls. |
| `evidence_source_limit` | The metadata or selected stored-column byte preflight exceeds its limit. |
| `retrieval_part_limit` | More than 2,048 parts across the complete selected content. |
| `evidence_response_limit` | Response metadata alone exceeds its reserved capacity, or serialization violates the final response bound. |
| `non_finite_number` | A non-finite number remains in emitted metadata; selected parts receive the exclusion treatment described next. |

Keep existing evidence error details for reused failures. The part-limit error
adds only `count` and `limit`. Never return a successful truncated scan after a
capacity refusal. Logs contain operation names, IDs, closed reasons, counts,
and timings; they exclude questions, evidence text, credentials, and raw errors.

## Bounded source read

One request owns one read-only repeatable-read snapshot. Within it:

1. Resolve the configured organization and Session, visible inventory, and
   Quarantine revision using existing evidence read semantics.
2. Preflight the byte size of the complete visible population's selected
   metadata, `input_messages`, and `output_messages` before transferring those
   columns from PostgreSQL. Count each selected row and column once. Apply an
   aggregate 8 MiB limit, independent of the existing inventory preflight.
3. Decode complete canonical messages into a source projection. Exclude `raw`,
   the Fact's `user_id` column, and unrelated Fact fields. Refuse the complete operation
   if its part count exceeds 2,048. Empty messages remain valid storage but
   contribute no selectable part.
4. Pass the complete bounded projection to the pure selector and serialize its
   result before releasing the snapshot.

Don't hydrate complete Inference call Facts or repeatedly call HTTP manifests
and fetches to scan a Session. Reuse the store's scope and preflight helpers;
add the missing bounded projection once at the storage seam.

Retain the existing 1,000-call inventory cap and its metadata byte bound. The
content read has its own aggregate 8 MiB cap. Count decoded content, candidate
keys, serialization buffers, and inventory metadata when measuring worker
memory. The limits don't promise a particular resident-memory size.

The `context-retrieve` operation shares the one-evidence-worker admission limit
inside the existing two query/report slots and the 30-second deadline. It
inherits cancellation, process-group cleanup, bounded diagnostics, and capacity
refusal. It creates no additional worker pool or unbounded queue.

The [shared-admission amendment](../../adr/0021-bounded-evidence-access.md#capacity-and-representation)
supersedes this original worker reservation. The [concurrency specification](2026-09-22-shared-evidence-admission-design.md)
defines the replacement acceptance criteria.

Every request rechecks Quarantine. A concurrent change after snapshot creation
applies to the next request, as with existing evidence reads. Returned content
cannot be recalled. Send `Cache-Control: no-store`; retain no cross-request
candidate cache. Repeated input histories count toward storage-read capacity.

## Deterministic selection, version 1

Place a frozen `ContextRetrievalPolicy` beside the selector. Its version is `1`.
The request's query and `max_bytes` are explicit inputs. No model, clock,
network call, ingest order, or mutable global state influences selection.
Don't add a selector interface or plugin registry for this one implementation.

### Searchable content

Use the existing `similarity.tokenize` function for both query and evidence.
It lowercases ASCII identifier-like tokens and numbers. This baseline doesn't
claim multilingual or semantic retrieval. Don't modify the shared tokenizer.

Remove this fixed query stopword set after tokenization:
`a`, `an`, `and`, `are`, `as`, `at`, `be`, `by`, `did`, `do`, `does`, `for`,
`from`, `how`, `i`, `in`, `is`, `it`, `of`, `on`, `or`, `that`, `the`, `this`,
`to`, `was`, `were`, `what`, `when`, `where`, `which`, `who`, `why`, `with`.

For text parts, search `content`. For tool-call parts, search the tool name and
the strict JSON representation of `arguments`. For tool-response parts, search
the strict JSON representation of `result`. Sort object keys for this internal
representation, preserve string values, and don't add occurrence IDs or generic
part-type field names as searchable text. Reasoning parts are excluded.

If arguments or results contain a non-finite number, count the entire part as
`non_finite_number` and exclude it. Preserve exceptional but representable
strings and finite numeric values in returned evidence under
[ADR 0015](../../adr/0015-lossless-values-and-bundle-v2.md).
An excluded part never changes the stored Fact.

### Ranking and packing

1. Assign each eligible part the number of distinct non-stopword query tokens
   also present in its searchable text. Count zero-overlap parts as `no_match`.
2. Sort positive matches by descending score, descending observation time in
   UTC, ascending Fact ID, input before output, then ascending message and part
   indices. Use explicit ordering independent of database collation.
3. Suppress repeated content only within this response. The key is the strict,
   sorted-key JSON encoding of historical role, finish reason, and canonical
   part. Keep the first ranked occurrence and its exact reference. Count later
   copies as `repeated_content`. Don't merge parts or infer tool-call links.
4. Reserve 2 KiB of `max_bytes` for envelope metadata and array punctuation.
   Refuse metadata that exceeds this reserve. Visit unique positive candidates
   in rank order. Add a complete encoded item if it fits the remaining item
   budget; otherwise count it as `response_budget` and consider the next item.
   After eight items, count remaining unique matches as `item_limit`.
5. Validate and serialize the entire success response with strict,
   ASCII-escaped JSON and the requested byte ceiling before publishing it.

The envelope reserve is a deliberate simplification. It can leave unused
capacity but avoids a variable-sized metadata packing algorithm. Count commas
between items against the item budget. Item serialization must stop at the
remaining bound; don't allocate an unrestricted serialized copy of a large part.
Extract the transport-independent bounded encoder from the existing query
serializer into the core evidence owner so packing and publication use the same
timestamp and scalar representation. Keep HTTP exception translation in the
API. The derive package must not import API or CLI modules. Preserve existing
query-response bytes and error behavior with regression checks.

Closed exclusion reasons, in classification precedence, are `reasoning_part`,
`non_finite_number`, `no_match`, `repeated_content`, `item_limit`, and
`response_budget`. Each scanned part belongs to exactly one exclusion reason or
one selected item. The sum of those counts equals `coverage.scanned_parts`.
Duplicate suppression precedes packing even if the retained occurrence later
fails the budget. Return every exclusion key, including zeros.

This is consumer selection, not database deduplication. Existing exact-part reads
continue to preserve every occurrence. Bump `policy_version` if token handling,
ranking, exclusions, duplicate suppression, or packing changes.

## Pi integration

Extend the existing shim without changing capture opt-ins or ingest credential
resolution. Add `SEDIMENT_RETRIEVAL_ENDPOINT` and `SEDIMENT_RETRIEVAL_TOKEN` as
an independent opt-in pair. The endpoint is the API base URL, validated with the
existing HTTPS-or-literal-loopback policy. Reject redirects. Neither setting
falls back to capture credentials, operator login, or workspace configuration.

When both settings are absent, register no retrieval tool. Incomplete or invalid
configuration logs a closed, content-free reason and leaves capture active.
When enabled, register exactly `sediment_retrieve_context`. It sends only the
fixed route and declared request fields; model arguments cannot select a URL,
Session, local file, or credential.

The client combines the pi cancellation signal with a 35-second total deadline.
It caps response bytes at the requested `max_bytes`, rejects unexpected content
encoding and malformed/version-incompatible responses, and publishes no partial
tool result. It doesn't retry automatically. A declined request becomes a
content-free tool error that preserves the safe server reason where available.

Return the exact ASCII JSON response as one text tool-result block. Parsing for
validation must not cause JavaScript to round large canonical integers in the
published evidence: forward the original validated text, not a reserialized
object. Exclude evidence and secrets from `details`, progress events, and logs.
Test the model-visible result through pi's native tool path, not only a fake
registration callback.

Historical roles and tool invocations remain data. The tool doesn't replay
commands, add system instructions, import source Session history, or promise
protection from instructions embedded in retrieved text. Normal harness tool
controls govern subsequent actions.

Pi's documented native interface supports `registerTool` and cancellation.
The implementation must exercise the repository's supported pi version against
the [version-pinned extension contract](https://github.com/badlogic/pi-mono/blob/53fa77ccd8a279eb87e92294ef3687b03ff80112/packages/coding-agent/docs/extensions.md).
Keep runtime imports within the host-provided extension API. Add no second
Node toolchain or unpinned package download to the test path.

## Controlled continuation evaluation

The unit is complete only when both the deterministic contract checks and a
recorded live continuation demonstration pass. Automated fixtures alone don't
establish that an agent uses retrieved context. A missing local model or test
environment leaves the live result unverified; report that explicitly.

### Task and isolation

Use one disposable Python coding task with a runnable final verification command.
The source Session receives the goal, one material constraint not recoverable
from the repository alone, and a failing tool result relevant to the task.
Include an unrelated failure as a retrieval distractor. Pause the source Session
after the decisive evidence reaches Inference-call capture, then end it normally.
Don't extract a paused transcript to invent a checkpoint.

Before continuation, the operator verifies capture using the existing evidence
API and records the source references for the material constraint and failure.
These references are evaluation answers, never tool inputs or agent hints.
The task manifest and evaluation answers remain outside every agent environment.

Record the source Session ID, code revision, index, working-tree files, and
untracked-file hashes. Prepare isolated copies with identical file bytes for
each continuation. Give each copy a separate agent home and a distinct fresh
Session. Exclude old transcripts, source prompts, generated handoff files,
operator credentials, and evaluation answers from those environments.

Capture all three arms with the same existing settings. If the deployment must
stay inside the perimeter, model inference, gateway capture, and retrieval all
use internal endpoints. Don't count installation downloads as inference traffic.

### Comparison arms

Run three repetitions of each arm, for nine continuations total. Pin the same
model, harness, task goal, initial workspace, generation settings, and stopping
rules. Record run order; rotate arm order across repetitions. Record provider
seeds if supported and otherwise mark them unavailable.

| Arm | Initial context | Retrieval |
| --- | --- | --- |
| A: no historical evidence | Goal and preserved workspace | Disabled |
| B: full captured history | Same goal plus the final source call's complete canonical input and output messages, supplied as historical data | Disabled |
| C: requested evidence | Same goal and the retrieval tool description; no preselected evidence | Agent chooses questions and receives selected parts |

The source task must use an uncompacted, unforked conversation. Before running
the comparison, verify that the final captured call contains the complete
conversation prefix, the material constraint, and the decisive failure. Its
output completes B's history. Don't concatenate repeated copies of input history
from every call: that would inflate the baseline. Refuse an evaluation fixture
whose capture cannot support this baseline.

The small evaluation fixture must fit the existing read limits so the operator
can assemble B without changing the evidence API. It is a full-history injection
baseline in a fresh Session, not a claim about native harness resume or a warm
provider cache. The prompt tells every arm to inspect the workspace and preserve
earlier constraints, and to report missing information honestly. Only B receives
the earlier constraint up front; C must retrieve it.

The separate `shipment-totals` task uses an instructed-lookup protocol. Every
arm receives the same visible goal and conditional instruction: inspect the
workspace with `read`; if a prior-Session retrieval tool is available, invoke
it for earlier constraints and relevant failure evidence before any `edit`,
`write`, or `bash` call. The agent chooses the question. The prompt provides no
query text, source references, material constraint, or selected evidence. Arm B
uses supplied full history. If neither history nor a retrieval tool is available,
the agent reports missing context honestly and continues the visible goal.

This protocol measures instructed retrieval and application. It doesn't establish
that an agent decides to retrieve without an instruction. The invoice and
environment-profile tasks retain their earlier prompts and acceptance rules.

Limit each continuation to 12 model calls and four retrieval calls. Enforce the
limits through the evaluation controller and record a budget stop as an outcome.
Don't impose an untracked retry that changes only one arm. Final checks run
outside the agent and verify both the requested code behavior and the material
constraint. A claimed success in the agent's prose doesn't satisfy the check.

### Evidence and acceptance

For each run, retain a private record of the model/harness versions, fresh Session
ID, workspace identity, actual retrieval questions and returned references,
verification result, constraint result, stops/errors, and elapsed time. Record
input, output, and cache usage when the provider supplies it. Record complete
tool-response bytes and tool-schema overhead separately; bytes aren't tokens.
Count all retries and failed model requests in usage where observable.

The live demonstration passes when:

- All three C continuations call the tool, retrieve evidence containing the
  relevant constraint and failure, and pass the independent final checks.
- At least one paired A continuation fails the constraint or explicitly cannot
  continue without the missing history, while C passes. If A performs equally,
  report retrieval feasibility but leave the claimed benefit unproven.
- The evaluation reports every B result and compares C with the full-history
  baseline for task quality, token usage, and elapsed time. Lower cost or fewer
  tokens isn't a pass requirement for this slice.
- The captured C trajectory connects a question to returned source references
  and subsequent task work; no person injects a selected packet after startup.
- For `shipment-totals`, every C run also passes a post-run ordering check: a
  successful, nonempty native retrieval result for the configured source Session
  precedes the first `edit`, `write`, or `bash` start. `read` may precede retrieval.
  Even a failed early work invocation violates the order. This check observes
  recorded native events; it doesn't block tools or supply evidence at runtime.

This is a controlled demonstration on one task, not statistical evidence of
general improvement or crash recovery. Don't tune the selector against these
continuation outcomes and call the same task a holdout. If the result motivates
a policy change, version it and evaluate another untouched task.

Keep detailed records private and publish only sanitized counts and outcome
summaries in the implementation issue. Use existing capture for ordinary model
and tool traffic where supported. Add no Fact type for queries or selections.
Retrieval scores and continuation checks don't become canonical training labels,
an Evidence recipe, or a third training artifact.

## Required automated checks

| Boundary | Runnable evidence |
| --- | --- |
| Pure selector | Identical bytes for the same source and request; shuffled call input and Fact insertion order produce identical selection; exact tie breaks, counted duplicate suppression, unrelated distractors, empty inputs, reasoning exclusion, and no-match behavior. |
| Capacity and representation | Request and response boundaries; whole-part packing; metadata reserve; eight-item cap; all exclusions sum to scanned parts; no partial success on source or part overflow; exact NUL, surrogate, finite-number, and large-integer preservation; counted non-finite exclusions. |
| Real PostgreSQL read | Tenant/Session scope, missing Session, zero calls, Quarantine exclusion, one snapshot across count/preflight/content, backdated Facts between requests, and source-byte preflight before transferring content. No mocks of Fact shapes. |
| Authority | Complete credential/route matrix, disabled mode, invalid/overlapping settings, source-Session injection refusal, ingest enrollment refusal, and inability to use the retrieval token on any existing evidence, report, or ingest route. |
| Worker | Shared evidence admission limit, deadline, disconnect/cancellation, process cleanup, and content-free failure diagnostics. |
| Pi tool | Independent opt-in, fixed endpoint, no credential fallback, redirect refusal, bounded reads, cancellation, safe errors, original JSON forwarding, and a real supported-harness tool invocation without paid inference. |
| Pipeline invariance | Existing evidence API tests pass; frozen fixture Attributions and canonical/training outputs agree before and after retrieval with capture held constant. Retrieval itself writes no Fact. |

Use focused selector and shim tests first. Run PostgreSQL-backed API, authority,
worker, and pipeline checks before review. Run the existing full required gates
for the behavior-changing implementation; this design-only change runs the
documentation checker and whitespace check.

## Implementation sequence and documentation

Implement the unit in three dependent steps, with one issue and reviewable pull
request unless the implementing agent discovers a concrete reason to split it:

1. Add the bounded source projection and pure selector, with determinism,
   representation, and capacity checks.
2. Add the fixed retrieval authority, route, worker dispatch, and full authority
   regression matrix. Document the ADR amendment before enabling agent access.
3. Add the opt-in pi tool, native-harness check, evaluation controller and task,
   and the recorded continuation comparison.

Keep the selector out of the training/export path. Reuse existing source
projections and test fixtures where they fit; don't change frozen capture
fixtures. Put the evaluation controller under `scripts/` and its owned task
fixtures/tests beside the existing script tests. No package is required.

In the implementation PR, update the API/operations, FactStore, PostgreSQL,
Derivations, and capture-client playbooks; pi README and capture guide; deployment
and security configuration; and controlled-continuation guide. Register proposed
modules and the ADR in `AGENTS.md`. Regenerate OpenAPI, API reference, and affected
schema references. Update generated CLI reference only if the implementation
adds or changes a CLI command. Add a `CHANGELOG.md` entry. Follow
[Doc sync](../../agents/doc-sync.md) and the
[review contract](../../agents/review.md).

## CUA-S1 and Jev follow-up

Decision: this slice ships no CUA-S1 or Jev integration. The baseline and
continuation task provide something to compare a learned selector against.
Replacing keyword overlap later must preserve the source identity, authority,
Quarantine, representation, and byte-budget contracts.

[CUA-S1](https://github.com/trycua/cua/tree/main/libs/cua-s1) is a candidate for a
separate local experiment. Its published
[forms checkpoint](https://huggingface.co/cua-ai/cua-s1-forms) targets form-field
decisions; its documented short byte windows and training domain don't establish
coding-context relevance. Adaptation needs explicit candidate representation,
training data, and Session-separated evaluation. No compatibility, accuracy,
calibration, or cost claim is made by this spec.

Source code and checkpoint terms must be checked for the exact artifacts used
by that experiment. The deterministic tool remains usable without a model
download, a model service, or training work.
