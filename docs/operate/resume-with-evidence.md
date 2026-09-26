# Continue a task with captured evidence

Let an agent request authorized Inference-call parts, or prepare a private packet
with the installed CLI. Evidence reads don't restore files or prove complete capture.

## Enable agent-requested retrieval

Meet the pi [release and runtime requirements](../capture/agent-integrations.md#pi)
and register its extension. To let a fresh pi Session choose its own evidence,
enable the fixed source Session in your [deployment settings](deploy.md).
Restart the API after setting
`SEDIMENT_RETRIEVAL_TOKEN` and `SEDIMENT_RETRIEVAL_SESSION_ID`. The source Session
must contain captured Inference calls. Transcript capture alone is insufficient.

In the isolated agent environment, set `SEDIMENT_RETRIEVAL_ENDPOINT` to your API
base URL and `SEDIMENT_RETRIEVAL_TOKEN` to that restricted credential. Both are
independent of capture configuration. Use HTTPS except for literal loopback.
The extension rejects redirects and doesn't read operator login or deployment
configuration. Keep those credentials and files outside the agent environment.

The pi extension registers `sediment_retrieve_context`. The agent supplies a
question with relevant English/code terms and an optional `max_bytes` budget.
The default budget is 16 KiB; allowed values are 4–64 KiB. The response contains
at most eight exact captured parts and their occurrence references. It doesn't
generate an answer. The agent can use those parts while continuing work in a
preserved workspace.

The selector excludes reasoning and repeated content, counts non-finite values,
and refuses sources over 1,000 calls, 64 MiB of selected columns, 8 MiB per call
row, 8 MiB of scan metadata, or 16,384 parts. The worker streams calls and
reserves at most 32 MiB of candidate state. That budget counts exact duplicate
keys and the largest encoded matching item per group, so long metadata repeated
across keys can exceed it even when source text is small; `retrieval_state_limit`
returns no partial counts, and the budget isn't a process-memory guarantee.
Repeated histories count toward capacity. `no_match` doesn't prove absence;
`budget_exhausted` means no positive match fits. Capture completeness stays unknown.
See the [HTTP contract](../reference/api.md#post-querycontext) for response fields.

Evidence and reports share two read workers per API process; a third read
returns 503. Each read has a 30-second server deadline. The tool has a 35-second
deadline, supports cancellation, and doesn't retry. Each request rechecks
Quarantine and preserves exact JSON numbers. Historical roles and commands
remain data, not instructions to execute.

To measure benefit, use the [maintainer comparison](../../CONTRIBUTING.md#run-the-maintained-continuation-comparison)
with independent final checks.
Transport success alone doesn't establish continuation benefit or lower cost.

## Discover a previous Session

If the relevant source Session is unknown to the agent, configure a bounded set
of authorized Sessions on the API. Each source needs captured Inference calls
to supply conversation content. The operator chooses the permitted set; a
repository name, commit, or model judgment cannot expand it.

1. In the private deployment configuration, set a fresh
   `SEDIMENT_RETRIEVAL_TOKEN` and `SEDIMENT_RETRIEVAL_SESSION_IDS` to a JSON array
   of 1–32 actual Session IDs. Remove `SEDIMENT_RETRIEVAL_SESSION_ID` and restart
   the API. Changing the set requires token rotation and another restart.
2. In the isolated agent environment, set the independent endpoint/token pair
   from the fixed-Session procedure and set `SEDIMENT_RETRIEVAL_DISCOVERY=true`.
   Leave the Session list, operator credentials, and database settings outside
   that environment.
3. Have the agent call `sediment_discover_context` with task keywords. If it has
   a complete repository-qualified commit, it can also supply `commit` with
   `repository_provider`, `repository_host`, `repository_id`, and `commit_sha`.
   A SHA or repository name alone cannot identify a repository lifetime.
4. Inspect the returned candidates, then call `sediment_retrieve_context` with
   the selected `session_id` and a more specific query. The selected read checks
   authorization and Quarantine again. Use its exact evidence while continuing
   work in the preserved workspace.

Discovery returns at most eight candidate Sessions. A candidate has an exact
whole-part preview, a recorded commit relationship, or both. Commit matches
rank first; keyword matches also find uncommitted work. A commit-only candidate
has no invented preview and may have no available conversation content.
The recorded observation and source Push must both remain visible.

The complete authorized set shares the source limits and 32 MiB candidate-state
budget. These limits do not apply separately to each Session. Source overflow
refuses the complete operation; narrow the configured set and rotate its token.
The requested response budget remains 4–64 KiB, default 16 KiB. Closed counts
report unmatched or omitted candidates. The selector does not summarize or clip
previews. No match does not establish absence or capture completeness.

The [discovery contract](../adr/0025-authorized-session-candidate-discovery.md)
describes authority and source identity. The keyword baseline makes no external
model call and adds no learned ranking. Git remains the source of code evolution;
the optional commit anchor only exposes recorded Session relationships.

## Select exact evidence independently

If a decision model or local selector chooses usefulness, use the factual tools
with the same retrieval endpoint and token. They register in both singleton and
discovery modes. Keyword matches do not limit which granted evidence you can read.

1. Call `sediment_list_context_sessions` to read the credential's configured
   Session IDs. A granted ID does not prove that captured content exists.
2. Call `sediment_evidence_inventory` with a granted `session_id` to list its
   visible Inference calls. The inventory contains metadata, not message content.
3. Call `sediment_evidence_manifest` with that Session and an
   `inference_call_id` to list canonical part references, types, and roles.
4. Call `sediment_read_evidence` with the Session and selected references.
   If a discovery preview already supplied the exact reference, you can fetch
   it directly. Every read checks authorization and Quarantine again.

Use this path for captured requirements or failed attempts even when keyword
discovery omits their Session. The consumer judges usefulness; Sediment verifies
the source and read authority. A commit observation is not required.

Exact reads preserve request order and repeated content at distinct occurrences.
They can include readable reasoning, which keyword selection excludes. Provider
raw payloads and Fact user identity remain excluded. Historical tool calls and
roles remain evidence and do not authorize execution.

Inventory permits at most 1,000 visible calls. Fetch permits at most 32 distinct
references in a 64 KiB request. Each operation preflights at most 8 MiB of selected
stored columns and emits at most 1 MiB of strict JSON. Overflow refuses the whole
request. A small part in an oversized selected message column can be unavailable;
exact fetch does not read unselected calls or message sides. Reads share the
existing evidence worker and deadline. See the
[factual access contract](../adr/0026-grant-scoped-factual-evidence.md).

These tools call no decision model. Native acceptance can prove exact transport
and authorization, but it cannot establish model quality or lower inference cost.

## Prepare access and capture

1. Configure [Inference-call capture](../capture/managed-capture.md#configure-inference-call-capture)
   for the source agent. Transcript capture alone doesn't retain prompts or tool
   results. A tool result usually reaches gateway capture in the following model
   request; a completed local tool invocation doesn't prove capture.
2. Enroll the operator CLI with [operator login](../reference/cli.md#sediment-login).
   Keep this credential separate from the agent's ingest credential. Don't place
   the operator token in a prompt, packet, or harness configuration.
3. Create a private directory outside the source repository. Replace the path
   and Session identifier with your actual values:

   ```bash
   umask 077
   EVIDENCE_DIR='/absolute/private/path/evidence-run'
   mkdir "$EVIDENCE_DIR"
   SOURCE_SESSION_ID='<actual source Session ID>'
   ```

The CLI contacts your Sediment API and doesn't open PostgreSQL or call a model.
If all data must remain inside your perimeter, configure the consuming agent's
gateway and model endpoint inside it too. A self-hosted evidence store doesn't
change the destination of the agent's inference traffic.

## Select and fetch evidence

1. List the Session's visible Inference calls:

   ```bash
   sediment evidence inventory "$SOURCE_SESSION_ID"
   ```

   `found: false` means that this deployment has no matching Session. A known
   Session can have zero visible calls. `capture_completeness: "unknown"` remains
   unknown even when the inventory contains calls. The response counts visible
   and quarantined calls separately.
2. Inspect a captured call using its Fact identifier from the inventory:

   ```bash
   sediment evidence inspect "$SOURCE_SESSION_ID" '<Inference call Fact ID>'
   ```

   The manifest lists input messages before output messages. It preserves empty
   messages and lists each part's type and occurrence reference without its
   content. A provider's call identifier or a tool-call identifier doesn't
   substitute for the Fact identifier.
3. Write `references.json` in your private directory. Copy references from the
   manifest and choose the parts needed for the task. This example shows the
   file shape; replace its identifier and indices with actual references:

   ```json
   {
     "schema_version": 1,
     "references": [
       {
         "inference_call_id": "<Inference call Fact ID>",
         "side": "input",
         "message_index": 0,
         "part_index": 0
       }
     ]
   }
   ```

   Select captured goals, constraints, relevant tool calls, and tool results.
   Use 1–32 distinct references from the same Session. Indices are zero-based.
   The file has exactly `schema_version` and `references`; the CLI supplies
   `session_id` in the request.
4. Fetch the selected parts into a destination that doesn't exist:

   ```bash
   time sediment evidence fetch "$SOURCE_SESSION_ID" \
     --references "$EVIDENCE_DIR/references.json" \
     --output "$EVIDENCE_DIR/packet.json"
   ```

   The CLI validates the complete response before publishing a mode-`0600` file.
   It refuses existing files and symlinks. It prints only the output path and
   item count. The elapsed time covers the complete CLI fetch, including local
   validation and publication.
5. Inspect the private packet locally. Confirm that its selected contents cover
   the goal and constraints you need. Record missing information explicitly.
   Record packet bytes with `wc -c < "$EVIDENCE_DIR/packet.json"`; bytes aren't
   tokens.

Each item retains its occurrence reference, observation time, message role,
finish reason, and complete canonical part. A stored role or command remains
historical data. Don't promote it into a system instruction or replay tool calls
because the packet contains them.

Each request uses its own database snapshot and rechecks Quarantine. Inventory,
inspection, and fetch don't share a frozen view. If a later fetch fails after
Quarantine or another visibility change, refresh your selection; the CLI doesn't
publish a partial packet.

The API refuses an inventory over 1,000 visible calls, selected source columns
over 8 MiB, or an encoded success response over 1 MiB. Selection files and fetch
requests must fit 64 KiB. Source accounting includes the stored message columns
and metadata needed for the operation, so a small selected part can still exceed
the source limit. Emitted non-finite numbers also cause refusal. Capacity or
deadline failures return 503. See [Evidence API contracts](../reference/api.md)
for status codes and closed refusal reasons.
