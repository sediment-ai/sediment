# Bounded evidence access for coding-agent continuity

Status: proposed implementation specification; no runtime behavior ships with
this document. Implementation tracker:
[issue #64](https://github.com/sediment-ai/sediment/issues/64).

## Outcome and scope

Sediment exposes captured Inference call evidence through a small, self-hosted
read interface. A consumer discovers source records, selects exact message parts,
and supplies those parts to an agent in a fresh Session. The first reference
consumer is an operator-run Python command-line interface (CLI). Its example
resumes a controlled coding task with an intact working tree.

This adds another use for the same Facts that support agent-quality reports,
Attribution, Derivations, and training exports. It does not change their semantics
or introduce a third canonical training artifact. Retrieved history is evidence
of recorded activity; retrieval does not establish task success or code quality.

Decision: build evidence access first. The alternatives are a full continuity
service with checkpoint capture, or a retrieval system with indexing and ranking.
Both require additional capture or policy decisions before proving that a
consumer can use the existing evidence. This slice supplies the read contract
that either can consume later.

JEV, a local retrieval model, and a deterministic selector are potential consumers.
No JEV integration, compatible protocol claim, model, embedding index, or external
service is required. The consumer owns selection and agent behavior. Sediment owns
Fact identity, scoped reads, visibility, representation, and resource bounds.

Success means that an operator can retrieve a bounded, source-linked packet
through the public interface without changing the existing pipeline. Cheaper
context and better task continuation are hypotheses to measure, not acceptance
claims for this implementation.

## Foundation and architecture

Use the existing five packages and CLI. Add no package, dependency, Fact type,
database migration, checkpoint, persistent summary, or retrieval cache.

| Concern | Existing owner | Required addition |
| --- | --- | --- |
| Canonical content | `packages/core/sediment_core/models.py` | Reuse InferenceMessage and the four canonical part types without changing their shapes. |
| Scoped reads | `packages/core/sediment_core/store.py` | Bounded metadata and selected-content projections using the existing Session lookup index. |
| Response construction | `apps/api/sediment_api/routers/query.py` | Evidence operations and frozen response projections; reuse strict query serialization. |
| Authentication | `apps/api/sediment_api/deps.py` | Reuse operator authority and deployment-bound organization. |
| Bounded execution | `apps/api/sediment_api/workers.py`, `apps/api/sediment_api/worker.py` | Register evidence operations and a fixed evidence admission sublimit. |
| Reference consumer | `cli/sediment_cli/cli.py`, `cli/sediment_cli/client.py` | Remote evidence commands and private packet publication. |

New projection and reference shapes are frozen dataclasses. HTTP request
envelopes may use Pydantic. Canonical Fact and part shapes remain in the model
owner. IDs use the existing validated identity types; message and part indices
are strict nonnegative integers. A reference is an occurrence selector, not a
persisted entity or an inferred relationship.

Reuse `read_snapshot` for a read-only, repeatable-read transaction per request.
Reuse the content-budget and Quarantine-filtering patterns in the FactStore.
Do not expose the eager `read_inference_calls_by_ids` method: it hydrates complete
Facts, including `raw`. Do not repurpose the complete-Session Rollout read as a
selective endpoint. PostgreSQL stores message content as opaque serialized text;
decode selected columns in Python after the byte preflight.

T1 records these decisions in a proposed architecture decision record (ADR), using
the next available ADR number. It references ADRs 0001, 0002, 0004, 0006, 0007,
0008, 0012, 0015, 0018, and 0020. It must not relax those contracts. In particular,
the first consumer stays within the single-team open-source boundary.

## Read contract, version 1

All three operations require operator authority. All successful envelopes contain
`schema_version: 1`, `session_id`, and the request snapshot's
`quarantine_revision`. Unknown fields on request envelopes are rejected. The
server supplies the organization from deployment settings; requests cannot choose
it. Responses have `Cache-Control: no-store`.

The requested Session and every selected Fact must match. Carry IDs in query
parameters or the JSON body, never ordinary URL path segments. Valid IDs can
contain slashes; percent-encoding does not make such path segments portable.
The CLI uses the HTTP client's parameter encoding. Do not treat a model-call ID
or tool-call ID as an Inference call Fact ID.

### Inventory a Session

`GET /query/evidence?session_id=...`

Return the complete list of visible captured Inference calls for the Session,
within the inventory and response limits. Each entry contains
`inference_call_id`, `observed_at`, `model_provider`, and `model`. Sort entries by
`(observed_at in UTC, inference_call_id)`.

The envelope also contains:

- `found`: whether the Session exists in this deployment.
- `capture_completeness: "unknown"`: storage cannot prove complete capture.
- `visible_inference_calls`: the number of visible calls in this request.
- `quarantined_inference_calls`: the number of quarantined calls in this Session.
- `calls`: the complete visible inventory.

An unknown or foreign Session returns HTTP 200 with `found: false`, zero counts,
and an empty list. A known Session without Inference calls returns `found: true`
and an empty list. Developer decisions or transcript hooks alone do not imply
conversation capture. Inventory reads do not select or decode message content,
`raw`, or user identifiers. Quarantined entries have no individual metadata in
the response; only the scoped count is visible.

### Inspect one Inference call

`GET /query/evidence/manifest?session_id=...&inference_call_id=...`

Return `call` metadata and `messages`, a complete manifest of its input and output
messages. Visit input before output, then preserve canonical message and part order.
Each message contains `side`, `message_index`, `role`, `finish_reason`, and
`parts`. Each part entry contains its `type` and `reference`.

The structured reference has exactly four fields:

| Field | Meaning |
| --- | --- |
| `inference_call_id` | The canonical Inference call Fact ID. |
| `side` | The literal `input` or `output`. |
| `message_index` | Zero-based index in that side's message list. |
| `part_index` | Zero-based index in that message's parts. |

Call metadata uses the same fields as an inventory entry. A manifest preserves
empty messages and empty sides. It does not contain text previews, reasoning
text, tool names, tool arguments, or tool results. This avoids a second excerpt
or summarization policy. Descriptive roles and finish reasons remain historical
data and use the same serialization and response limit as all other output.

### Fetch selected parts

`POST /query/evidence/read`

This POST is a read operation. The body contains exactly `schema_version: 1`,
`session_id`, and a nonempty `references` list. Reject duplicate references and
unsupported versions with HTTP 422. A consumer can construct references without an earlier
inventory; the server applies the same scope and visibility checks.

Return `items` in request order. Each item contains its `reference`, the source
call's `observed_at`, its message's `role` and `finish_reason`, and the complete
canonical `part`. Preserve all four part types and their values. Preserve
repeated input history as distinct occurrences. Do not deduplicate text, merge
tool exchanges, summarize, or clip a part.

Resolve the entire selection before returning content. An unavailable reference,
capacity failure, or representation failure declines the complete response.
There is no successful partial packet. A selected part is historical data; its
role or tool-call shape does not authorize a consumer to execute it.

### Errors and representation

Use existing authentication responses: 401 for missing or invalid credentials;
403 for a valid ingest-only credential. Malformed authenticated inputs return
400 or 422 under the existing request-validation conventions.

Evidence-specific failures use HTTP 409 with `detail.reason`:

| Reason | Meaning and additional fields |
| --- | --- |
| `evidence_inventory_limit` | Visible inventory exceeds 1,000 calls; include `count` and `limit`. |
| `evidence_source_limit` | Selected variable-width source columns, including metadata, exceed 8 MiB; include `bytes` and `limit`. |
| `evidence_response_limit` | Encoded response exceeds 1 MiB; include `limit`. |
| `evidence_unavailable` | A Fact is absent, belongs to another Session or organization, or is quarantined. For fetch, include the first failing `reference_index`. |
| `evidence_part_absent` | A visible, scoped Fact has no selected message or part; include `reference_index`. |

Check references in request order after resolving visibility. Never disclose which
inaccessible condition caused `evidence_unavailable`. For inspect, the same
unavailability reason omits `reference_index`. Diagnostics contain IDs or counts,
never prompts, content, credentials, or exception text containing payloads.

Follow [ADR 0015](../../adr/0015-lossless-values-and-bundle-v2.md) exactly: validate
declared response shapes in Python, then encode ASCII-escaped strict JSON. NUL,
surrogates, large integers, and finite numeric values retain their values.
An emitted non-finite number declines the response with HTTP 409 and
`{"detail":{"reason":"non_finite_number"}}`. Do not use a `record_json`
bundle wrapper, convert a value to null, or silently omit it. A non-finite value
in an unselected part cannot block a finite selection or its metadata manifest.

This operational read contract cannot return every value that canonical storage
accepts. Lossless bundles retain their separate contract. Version 1 deliberately
inherits the existing query limitation rather than changing canonical storage
or bundle encoding. Storage corruption and programming errors use the existing
controlled worker failure path; they do not become representation errors.

### Capacity and execution

Decision: use fixed first-version limits without deployment knobs. These bounds
are engineering choices, not measured throughput or token-cost claims.
Kibibytes (KiB) and mebibytes (MiB) use 1,024-byte units.

| Resource | Limit |
| --- | --- |
| Visible Inference calls in an inventory | 1,000 |
| References per fetch | 32 |
| Fetch HTTP request body | 64 KiB |
| Combined stored bytes in selected variable-width columns per operation | 8 MiB |
| Encoded successful response body for each operation | 1 MiB |
| Concurrent evidence workers per API process | 1 within the existing 2 query/report slots |
| Worker deadline | Existing 30 seconds |
| Evidence CLI HTTP read timeout | 40 seconds; unrelated CLI requests retain their existing timeout |

Bound the fetch request before decoding its JSON, while retaining the deployment's
pre-auth request ceiling. Reject a body
over the operation limit with HTTP 413. Bound inventory cardinality and scalar
metadata bytes before hydrating its rows. Model and identity columns can be
unbounded text; a row-count cap alone does not bound their memory use. Refuse
overflow without a successful partial inventory.

For inspect, preflight both message columns and the selected call metadata. For
fetch, preflight the unique `(Inference call, side)` columns and source metadata
needed by the references. Include every selected variable-width column, counting
each `(row, column)` once. Check their combined stored byte size before
transferring or decoding any of those columns. This includes IDs, model fields,
and descriptive scalar content when an operation selects them. No `raw`, user
identifier, or unrelated message column may enter the content SELECT. A small
part inside an oversized source column can be refused; do not parse fragments
with SQL or change storage layout to evade the limit.

Bound output encoding before publishing bytes. Use incremental bounded encoding
if escaping or repeated selected data could exceed the output limit. Keep the
existing subprocess deadline, cancellation, diagnostic bound, and cleanup.
The evidence admission sublimit shares the existing worker supervisor; it does
not create another service or unbounded queue. Evidence traffic alone cannot
occupy both query/report slots. Busy or timed-out workers retain HTTP 503 behavior.
This reserves admission capacity, not a database or latency guarantee.

The [shared-admission amendment](../../adr/0021-bounded-evidence-access.md#capacity-and-representation)
supersedes this original reservation. See the [concurrency specification](2026-09-22-shared-evidence-admission-design.md).

### Live reads and visibility

Scope checks, Quarantine, preflight, content selection, and response construction
use one snapshot for each request. The next request takes another snapshot and
checks visibility again. A reference has stable content identity because the
Fact is immutable; it does not grant continued access.

The interface has no pagination, event-time cutoff, cross-request snapshot token,
or retained database transaction. A late-arriving Fact can appear in a later
inventory. Quarantine revision describes visibility, not delivery completeness
or an insertion watermark. A Quarantine committed after a read snapshot starts
affects later requests; it cannot recall content already read or a local packet.

## Reference consumer and controlled restart

Add these remote commands to the existing parser and dispatcher:

| Command | Result |
| --- | --- |
| `sediment evidence inventory SESSION` | Print the inventory as strict JSON to stdout. |
| `sediment evidence inspect SESSION INFERENCE_CALL` | Print the part manifest as strict JSON to stdout. |
| `sediment evidence fetch SESSION --references PATH --output PATH` | Read the versioned selection from a local file and write the validated response as a private evidence packet. |

The selection file contains exactly `schema_version: 1` and `references`; the
CLI adds `session_id` from its positional argument to construct the HTTP body.
Fetch requires an explicit output path. It has no content-to-stdout mode and no
overwrite flag in version 1. Bound the selection file before parsing, then
validate the complete encoded HTTP body and reference limits before making the
request. The selection file also has a 64 KiB limit. Bound the HTTP response while
reading it, validate the version and shape, and publish only a complete success.
Use a temporary file in the destination directory with mode 0600 and an atomic,
no-clobber publication step. Refuse existing destinations, including symlinks.
Failures remove temporary files and preserve existing destinations. Stdout reports
only the path and item count; stderr reports a content-free error and nonzero exit.

The CLI uses the established operator login and URL-validation path. It does not
open PostgreSQL, call a model, install a harness extension, or pass operator
credentials into capture configuration. An operator credential remains
deployment-wide; a Session filter does not make it Session-scoped. Scoped agent
read credentials require a separate ADR 0018 decision.

The implementation includes an operator procedure for this demonstration:

1. Run a small coding task through an existing gateway-backed capture path, with
   a known goal and constraints. Record the source's real Session identifier.
2. Pause after a verifiable edit and tool result. Verify the relevant Inference
   calls reached Sediment before discarding the Session. Preserve the working
   tree and record its revision and uncommitted changes locally.
3. Inventory and inspect the source Session. Select the goal, relevant constraints,
   and the last useful tool evidence by their exact references. Fetch the packet.
4. Start a fresh pi Session with a distinct Session identifier. Supply the packet
   and an explicit user instruction to inspect the preserved workspace, identify
   missing information, and continue the task. Keep historical messages as data;
   do not promote stored roles into system instructions or replay tool calls.
5. Verify the task's concrete result. Record packet bytes, selected part count,
   read latency, and the continuation result. When the model exposes token use,
   record it with the model and tokenizer identity.

If the source did not capture a required goal or constraint, the operator supplies
it explicitly and records the gap. The procedure does not fabricate a checkpoint
or infer that the task can resume. Existing transcript extraction remains narrow;
do not use extraction at pause time to freeze final Edit observations.

For an entirely self-hosted demonstration, run the gateway and agent inference
endpoint inside the customer perimeter. Sediment self-hosting alone does not
make an external model endpoint private. The procedure uses documented pi
commands verified against the tested version; it does not require shim changes.

Automated tests prove the packet and restart inputs. A live model run is optional
and reports its observed result separately. Do not claim reliable crash recovery
or cost savings from a synthetic fixture. A later comparison must hold the task,
workspace, model, and evaluation fixed and include retrieval and inference cost.

## Implementation tickets and frontier

The task IDs in [issue #64](https://github.com/sediment-ai/sediment/issues/64)
are the associated implementation tickets. Keep their status there. Use one
integration branch and one implementation pull request. A specification-only
commit does not close the issue.

| Ticket | Blockers | Owned work and acceptance |
| --- | --- | --- |
| T1: bounded evidence projections | None | Record the ADR; freeze the wire contract from this spec; add typed projections and bounded scoped reads in core. Prove byte preflight, immutable references, Quarantine behavior, and deterministic order. |
| T2: authenticated read API | T1 | Implement the three operations in the query router, register bounded workers, enforce auth and limits, and add HTTP contract tests. Update API reference generators and generated outputs. |
| T3: reference CLI consumer | T1 | Implement the three commands against the frozen contract, evidence-specific client bounds, and private packet publication. Test with a transport fixture; update the CLI reference generator output. |
| T4: integration and validation | T2, T3 | Exercise CLI-to-HTTP-to-PostgreSQL behavior, preserve report/export results, finish docs and restart procedure, and complete independent review and required checks. |

Initial frontier: T1. After T1 integrates, T2 and T3 may run in separate
worktrees. Their source ownership is distinct; T4 owns shared documentation and
integration adjustments. Integrate completed commits serially. Recompute the
frontier after each integration. Do not assign T4 while either dependency remains.

An implementing agent reads this specification and the entire issue, claims the
issue under repository rules, and follows the implement-spec workflow. It creates
a fresh integration branch from the target branch with the reviewed spec commit
included. Its pull request uses `Closes #64` only when it implements all four
tickets. No implementation starts as part of publishing this specification.

## Verification and closeout

Use real canonical models and PostgreSQL-backed fixtures. Extend or add uniquely
named siblings of these test owners:

- `packages/core/tests/test_bounded_inference_reads.py`
- `packages/core/tests/test_scalar_representation.py`
- `apps/api/tests/test_query.py`
- `apps/api/tests/test_worker_processes.py`
- `apps/api/tests/test_worker_routes.py`
- `cli/tests/test_cli_remote.py`

No model or external-service dependency enters the test suite. Each nontrivial
boundary has a runnable failure case.

Required acceptance cases:

- Same Facts and selection produce identical projections. Shuffled insertion
  order produces the same inventory and manifest. Fetch preserves request order.
  Pure projection tests take Quarantine revision explicitly; wall-clock metadata
  is not part of the envelope.
- Inventory distinguishes unknown, known-empty, and quarantined-only Sessions.
  Repeated tool-call aliases and repeated history still have distinct references.
- Every canonical part type roundtrips. Empty sides, empty messages, null finish
  reasons, NUL, surrogates, large integers, and finite values retain their meaning.
  Emitted non-finite values refuse the whole response; unselected values do not.
- Missing, foreign, cross-Session, and quarantined references return the same
  unavailability shape. Bad indices and duplicate selections are explicit.
  Operator, ingest-only, and invalid credentials exercise their actual boundaries.
  IDs containing slashes, percent signs, question marks, and hash signs roundtrip
  through all three public operations and CLI commands.
- Inventory, then insert a backdated Fact, then quarantine a selected Fact.
  A later inventory can change, and a later fetch cannot return quarantined
  content. Do not test an event-time bound as a frozen snapshot.
- Exact-limit and over-limit requests cover counts, scalar metadata bytes, message
  bytes, response bytes, and body size. Include oversized model and identity fields
  in metadata-only reads. Inspect actual SQL to prove preflight before transfer
  and omission of `raw`, user identifiers, and unselected source columns.
- Evidence admission cannot consume both query/report slots. Saturation, deadline,
  cancellation, representation refusal, and corrupt stored content release worker
  and database resources without exposing content in logs.
- CLI tests cover malformed selections and responses, unsupported versions,
  oversized responses, network failures, output mode 0600, symlinks, publication
  races, and cleanup. The public HTTP test uses the real API and PostgreSQL.
- Run evidence reads between two reports and canonical exports over identical
  Facts and fixed policy/boundaries. Compare domain values, artifact populations,
  and declared serialization; verify Fact counts and Session metadata stay equal.
  No retrieval result becomes a Reward or training label.

Run affected core, API, CLI, and export tests, then the repository's required
checks under Python 3.12 with `uv`. Follow [Doc sync](../../agents/doc-sync.md)
and [Review closeout](../../agents/review.md). Regenerate API and CLI references;
do not hand-edit generated pages. Preserve Fact and canonical schema versions.
Update the owning playbooks, architecture/network-boundary documentation, a
single operator how-to, the AGENTS router, the published-page manifest, and the
CHANGELOG with the implemented behavior. Planning documents do not claim that
the feature ships.

Before marking the implementation pull request ready, an independent reviewer
checks the whole branch against this specification and all four tickets. Resolve
actionable findings and run proportionate checks. A maintainer approves merging.

## Deferred work and related issues

The first slice excludes semantic search, ranking, embeddings, context packing,
checkpoint capture, automatic summaries, arbitrary enterprise-document ingestion,
workspace reconstruction, automatic tool replay, and direct agent credentials.
It does not add a Model Context Protocol server or a general query language.

[Issue #23](https://github.com/sediment-ai/sediment/issues/23) owns capture without
a gateway. [Issue #26](https://github.com/sediment-ai/sediment/issues/26) owns
repeated-history storage measurements. [Issue #38](https://github.com/sediment-ai/sediment/issues/38)
owns broader Session-capacity diagnostics. [Issue #45](https://github.com/sediment-ai/sediment/issues/45)
owns general worker retry guidance. These are adjacent work, not prerequisites.

After a consumer demonstrates value, use measured retrieval failures to choose
the next increment: scoped read authority for agent tools, local selection or
ranking, broader evidence types, or explicit handoff capture. Keep each decision
separate from changes to training-objective evidence interpretation.
