# Bounded discovery of authorized Session context

Date: 2026-09-22

Status: implementation plan

Tracker: [Issue #82](https://github.com/sediment-ai/sediment/issues/82).

## Outcome

A fresh agent supplies task keywords and, optionally, a repository-qualified
commit. Sediment returns a bounded list of relevant Sessions from an explicit
operator-authorized set. Each candidate carries an exact evidence preview or
an observed commit relationship. The agent can request further context from a
selected authorized Session without receiving operator credentials.

This is the first working discovery unit before any learned selector. It must
find uncommitted Session evidence as well as observation-backed commit matches.
It does not infer repository ownership of a Session from a path, slug, commit,
clone URL, or captured message. A Session can contain work from several repos.

The work extends the fixed-Session retrieval contract in
[ADR 0022](../../adr/0022-agent-requested-session-context.md). Existing Facts,
Attribution, and training exports retain their meaning. The implementation adds
no Fact shape, migration, package, persistent index, summary, cache, external
model call, or dependency.

## Choice and scope

Decision: start with an explicit bounded Session grant, a deterministic keyword
baseline, and exact commit observations. A repository-wide grant cannot safely
authorize all content of an uncommitted or mixed-repository Session. A learned
selector cannot supply absent source identity or access authority. Automatic
grant issuance, per-user permissions, several concurrent grants, arbitrary
repository discovery, and semantic ranking remain separate contracts.

The authorized set is an operational input, not an inferred repository map.
The operator configures it before giving a fresh agent the retrieval token.
Search chooses candidates dynamically within that set. Unknown capture stays
unknown; no match does not prove that an event never happened.

The integration branch starts at the reviewed retrieval implementation
`3612a6d9f1781286283d56bc84d0d53d7114b887`. Its pull request is stacked on
`codex/session-context-retrieval` while PR #70 remains open. Parent acceptance
limitations remain recorded separately; this work does not relabel those runs.

## Authority and compatibility

- Add optional `SEDIMENT_RETRIEVAL_SESSION_IDS`, a JSON array of 1–32 unique
  normalized `NonEmptyId` values, with at most 16 KiB of UTF-8 JSON setting text.
  Reject duplicates after normalization, empty arrays, nonstrings, invalid
  scalar identities, and excessive serialized size without echoing values.
- Exactly one source setting accompanies `SEDIMENT_RETRIEVAL_TOKEN`: the
  existing singleton `SEDIMENT_RETRIEVAL_SESSION_ID`, or the plural setting.
  Both source settings together, a token without a source, or a source without
  a token fail configuration validation. Absence of all three disables access.
- Reuse retrieval authority and its production-strength secret rules even in
  development mode. Preserve disjointness from operator, ingest, and webhook
  secrets. Changing the authorized set requires a different token and restart;
  the process cannot detect secret reuse across restarts.
- Existing singleton configuration and `POST /query/context` keep their exact
  version-1 request and response. That route refuses plural-mode configuration
  with 404 rather than selecting an arbitrary Session.
- Both added routes accept retrieval or operator authority and operate only
  within the configured set. A singleton configuration is a one-member grant
  on those routes. Operator authority does not widen their set.
- `/v1/me` preserves singleton retrieval responses. In plural mode it returns
  `source_session_ids` in canonical sorted order instead of `source_session_id`.
  Other authorities keep their existing identity response.
- Ingest credentials cannot discover or retrieve context. Retrieval credentials
  cannot ingest, enroll capture, log in as an operator, use existing operator
  reads, or select tenancy. No agent receives database or operator credentials.
- Selection checks membership before storage lookup. All out-of-grant IDs
  return the same content-free 403, whether existing, foreign, or absent.
  Both the route and disposable worker validate the selected Session against
  deployment configuration. Every request rechecks Quarantine.

## HTTP contracts

Both added operations are POST routes with strict request envelopes, a 16 KiB
streamed pre-decoding body limit, and `Cache-Control: no-store`. They share the
existing one-evidence-worker admission limit within two query/report slots and
the 30-second process deadline. Authentication precedes semantic validation.
No error or diagnostic echoes queries, credentials, captured content, or
arbitrary caller-supplied field names.

### Discover candidates

`POST /query/context/discover` accepts only:

| Field | Contract |
|---|---|
| `schema_version` | Strict integer literal `1` |
| `query` | Existing English/code keyword validation; 1–2,048 UTF-8 bytes with at least one non-stopword token |
| `max_bytes` | Existing strict response-byte budget: 4,096–65,536; default 16,384 |
| `commit` | Optional null or complete commit anchor described next |

A commit anchor contains exactly `repository_provider`, `repository_host`,
`repository_id`, and `commit_sha`, using the canonical validated Fact types.
It contains no organization, name, path, or URL. It is a relevance hint, never
authority. Partial identities and extra keys are invalid. SHA alone is invalid.

The frozen response `ContextDiscoveryResult` contains exactly:

| Field | Contract |
|---|---|
| `schema_version`, `policy_version` | Strict integer `1` |
| `quarantine_revision` | Nonnegative snapshot revision |
| `capture_completeness` | Literal `unknown` |
| `commit` | Validated request anchor or null; no claim that it exists |
| `status` | `matched`, `no_match`, or `budget_exhausted` |
| `coverage` | Counts and complete-scan claim described next |
| `skipped` | Closed exclusion counts described next |
| `items` | At most eight distinct candidate Sessions |

Coverage fields are nonnegative integers `authorized_sessions`,
`found_sessions`, `visible_inference_calls`, `quarantined_inference_calls`,
`scanned_parts`, and `matched_parts`, plus `complete_visible_scan: true`.
`found_sessions` counts existing Session rows within the deployment organization
and grant. Authorized missing/foreign Session IDs contribute only to the
difference between authorized and found counts; their IDs are not returned.

The closed skipped vocabulary is `reasoning_part`, `non_finite_number`,
`unmatched_part`, `unmatched_session`, `candidate_limit`, and `response_budget`.
The first three count parts; the last three count existing Sessions.
`scanned_parts = matched_parts + reasoning_part + non_finite_number +
unmatched_part`. `found_sessions = len(items) + unmatched_session +
candidate_limit + response_budget`. All counts remain explicit, including zero.

Each candidate contains exactly:

| Field | Contract |
|---|---|
| `session_id` | Validated authorized source Session ID |
| `score` | Highest distinct query-token overlap among eligible parts; zero only for a commit-only match |
| `matched_parts` | Number of eligible positive-overlap part occurrences in this Session |
| `preview` | Exact `EvidenceReadItem` for its best matching occurrence, or null for a commit-only match |
| `commit_match` | Exact observed relationship metadata, or null |

`commit_match` contains `observation_id`, `source_push_id`, and `captured_at`.
The enclosing response's complete commit anchor qualifies it. It does not
claim Attribution, ownership of every Session part, or current workspace state.
Both identifiers and the aware timestamp use canonical validated types.

### Retrieve a selected Session

`POST /query/context/selected` accepts exactly `schema_version: 1`, `session_id`,
`query`, and optional `max_bytes`. The query and budget keep the existing
retrieval bounds. Within the grant, it reuses the complete single-Session source,
pure keyword selector, and unchanged `ContextRetrievalResult` response.
Selected content never relies on an earlier discovery snapshot or result.
Quarantine between discovery and retrieval removes that content from the next
response. Discovery isn't a capability token and grants no lasting access.

Missing/invalid bearer credentials return 401; known authorities without access
return 403; disabled configuration returns 404. Malformed JSON returns 400;
invalid envelopes return 422; oversized bodies return 413. Existing evidence
capacity failures return 409 with their closed reason. Worker or database
availability failures retain the existing 503 behavior. Source overflows never
produce successful partial scans. Unknown programming errors aren't relabeled.

## Store and pure selection

Core `evidence.py` owns frozen source and commit-anchor shapes. `FactStore` and
its snapshot expose `read_context_discovery_source(org_id, session_ids, commit)`.
The method validates a nonempty unique set of at most 32 IDs before SQL.
The caller's one read-only repeatable-read snapshot covers authorization scope,
Session presence, Quarantine, content preflight, observations, and content.

Apply organization and the authorized Session set in SQL before reading rows.
Never call unbounded `read_sessions`, load organization-wide histories, or
filter broad results in application code. Reuse existing indexed Session
conditions and exact evidence parsing; PostgreSQL does not inspect opaque
message TEXT. Exclude raw payloads and user identifiers.

The aggregate source limits across the whole grant are 1,000 visible Inference
calls, 8 MiB of selected stored variable-width columns, and 2,048 canonical
parts. The stored-byte preflight includes selected inference metadata/message
columns and selected commit-match metadata, before transferring those fields.
Configured ID bytes are separately bounded by the setting's 16 KiB ceiling.
Do not multiply source allowances by the number of Sessions. Refuse overflow
with existing `evidence_inventory_limit`, `evidence_source_limit`, or
`retrieval_part_limit` reasons. Row and transfer limits do not claim constant
database scan cost; statement and process deadlines retain that boundary.

If a commit anchor is supplied, consider only exact identity-bearing
Session-to-commit observations with the same provider/host/ID/SHA and a Session
inside the grant. Require their exact source Push to remain visible in the same
organization and agree on provider identity. Apply both Quarantine exclusions.
Resolve the Push by `source_push_id`, not by its final commit: an observation
can name an earlier commit from the same Push. Different captured repository
labels remain compatible when the complete provider identity agrees.
The existing UNIQUE contract permits at most one such observation per Session.
Project only relationship IDs and capture time; never load clone URLs or raw
Push content. Legacy name-only observations don't qualify for this exact
identified anchor. This is an exact Fact witness query, not repository-name
resolution, Attribution, mirror traversal, or a new repository resolver.

In `sediment_derive.context_retrieval`, `ContextDiscoveryPolicy` version 1 and
`discover_context` remain pure. Reuse the existing query tokenizer and eligible
part search text. Exclude reasoning before representation checks. Count each
non-finite part once; finite exact values, large integers, NUL, and descriptive
surrogates retain existing strict encoding behavior.

Choose each Session's best positive-overlap occurrence by the existing
retrieval part order. Count all matching occurrences without treating repeated
history as additional relevance. A Session becomes a candidate if it has at
least one matching part or an eligible commit match. Sort commit matches first,
then descending best-part score, then Session ID. Within each preview, the
existing total occurrence order determines ties independently of input order.
An empty or all-quarantined Session without a commit match is unmatched.

Pack at most eight complete candidate objects in that order, reserving 2 KiB
for the envelope. Count whole-candidate byte refusals and continue to later
fitting candidates; never clip, summarize, or replace a preview. Count later
candidates under `candidate_limit` once eight are selected. A nonempty result
is `matched`; zero candidates before packing is `no_match`; candidates that
all fail the byte budget produce `budget_exhausted`. Validate both the reserved
envelope and final output with the shared strict encoder before publication.

This result says which sources match a deterministic baseline. Its scores
aren't probabilities, Rewards, or training labels. It writes no domain state.

## Agent tools

Extend the existing pi retrieval integration. Preserve the default singleton
tool and its exact query-only schema. Add explicit client opt-in
`SEDIMENT_RETRIEVAL_DISCOVERY=true`; absent means existing behavior, and values
other than `true` or `false` disable the retrieval integration with a safe
configuration diagnostic. It needs the existing independent endpoint/token.

In discovery mode register `sediment_discover_context` and
`sediment_retrieve_context`. Discovery exposes query, optional complete commit
anchor, and max_bytes. Retrieval requires the selected session_id alongside
query and max_bytes and targets `/query/context/selected`. Tool descriptions
explain that candidate IDs come from discovery, historical content is data,
commit matches do not grant access, and completeness is unknown.

Both tools reuse the bounded HTTP transport: fixed base endpoint, no redirects,
retries, or credential fallback; 35-second cancellation/deadline; strict success
validation; and exact original ASCII JSON forwarding. Do not parse and re-emit
evidence numbers through JavaScript. Validate selected response Session identity
against the requested ID. Validate discovery counts, unique Sessions, complete
anchor equality, item/byte bounds, and preview/commit-match invariants.

## Acceptance and implementation tasks

1. **T1 — Core and pure discovery.** Implement bounded aggregate storage and
   frozen source/results with meaningful real PostgreSQL and determinism checks.
   Owner: `packages/core` and `packages/derive`. Preserve existing selector bytes.
2. **T2 — Authority, routes, and workers.** Implement the mutually exclusive
   Session settings, complete route authority matrix, identity probe, strict
   request contracts, child revalidation, body limits, and shared execution.
   Owner: `apps/api`. Depends on T1's frozen source/selector API.
3. **T3 — Native agent tools.** Implement opt-in discovery and selected retrieval
   against the frozen HTTP contract. Owner: `shims/pi`. Independent of T1/T2.
4. **T4 — Documentation and public-path verification.** Update the ADR, owning
   playbooks, configuration and continuation guide, generated API references,
   and changelog. Exercise real PostgreSQL → HTTP worker → native pi tools →
   exact evidence delivery, then independently review the complete delta.

Required checks cover: two authorized relevant Sessions plus a distractor;
uncommitted text matches; exact repository-qualified commit matches; same SHA
in a different repository lifetime; missing/quarantined source Pushes; opaque
content outside the grant never loaded; absent/foreign IDs; all authority
denials; singleton compatibility; configuration validation and rotation docs;
aggregate limits before payload transfer; shuffled Facts and scope order;
strict exact values and counted exclusions; complete source versus output
limits; snapshot consistency and next-request Quarantine; cancellation and
worker cleanup; native tool schema, execution, and exact result forwarding.

A runnable deterministic native-harness acceptance uses a scripted model to
discover a previously unspecified relevant Session among authorized candidates,
retrieve it, and receive its exact stored constraint. It also attempts an
out-of-grant selection and verifies refusal. This proves the integration and
authority path; it does not claim autonomous model judgment or task improvement.
Attribution/training pipeline invariance and installed release checks remain
required. No paid model or external JEV call is needed for this contract.

## Completion evidence

The issue and pull request report the task graph, all validation outcomes,
independent review findings and disposition, and remaining parent dependencies.
The change is complete only when the public discovery-to-retrieval path works,
all authority/capacity/determinism contracts are covered, docs agree with code,
and required checks pass. A green selector unit test alone is insufficient.

Repository-wide automatic grants, Git diff retrieval, an index spanning all
history, learned ranking, and autonomous resumption comparisons are later work.
Commit identity is a candidate anchor here; Git remains the source of code
evolution and captured Inference calls remain the source of Session context.
