# Continue a task with captured evidence

Use this page to give an agent the goals, constraints, tool calls, and results
from a previous Session. A pi agent can request that evidence itself, or an
operator can prepare a private evidence packet with the CLI. Either way,
Sediment returns exact captured parts. It doesn't summarize them, restore
files, or prove that capture was complete.

The source Session needs captured Inference calls. Transcript capture alone
doesn't keep prompts or tool results. Set up
[inference-call capture](../capture/managed-capture.md#configure-inference-call-capture)
for the source agent first.

Retrieved content is historical data. Don't run the tool calls or follow the
instructions that it contains.

## Enable agent-requested retrieval

A retrieval token lets one agent environment read the Sessions that you grant,
and nothing else. The pi extension uses it. First, meet the requirements in
[Capture pi work](../capture/agents/pi.md#before-you-begin).

1. Add these settings to the server's private process environment. On EC2,
   that's `~/sediment-deploy/server.env`:

   | Setting | Value |
   | --- | --- |
   | `SEDIMENT_RETRIEVAL_TOKEN` | A distinct secret of at least 24 printable ASCII characters, such as the output of `openssl rand -hex 32` |
   | `SEDIMENT_RETRIEVAL_SESSION_ID` | One Session identifier |

   To grant several Sessions, set `SEDIMENT_RETRIEVAL_SESSION_IDS` to a JSON
   array of 1–32 Session identifiers instead. Set exactly one of the two.

2. Restart Sediment.
3. In the agent's environment, set `SEDIMENT_RETRIEVAL_ENDPOINT` to the API's
   HTTPS URL and `SEDIMENT_RETRIEVAL_TOKEN` to the token. If you granted
   several Sessions, also set `SEDIMENT_RETRIEVAL_DISCOVERY=true`.
4. Start pi from that environment.

The grant covers every Fact in those Sessions, including Facts that arrive
later. To change the grant, also replace the token, and restart. To revoke it,
remove the token and the Session setting, and restart.

Run the agent in a separate container or operating-system account that can't
read the operator token, `server.env`, or database credentials. The agent's
model endpoint, not Sediment, decides where its next request goes.

### Use the retrieval tools

With one granted Session, the agent calls `sediment_retrieve_context` with a
question. The response contains up to eight exact captured parts, within a byte
budget of 4–64 KiB (16 KiB by default). The keyword selector skips reasoning
and repeated content. `no_match` doesn't prove that the evidence is absent.

## Discover a previous Session

With several granted Sessions, the agent first calls
`sediment_discover_context` with task keywords. To find the Sessions behind a
commit, it can also pass `commit` with `repository_provider`,
`repository_host`, `repository_id`, and `commit_sha`. Discovery returns up to
eight candidate Sessions, commit matches first. The agent then calls
`sediment_retrieve_context` with the chosen `session_id` and a narrower
question.

Discovery searches only the granted Sessions. A repository name, commit, or
model judgment can't widen the grant.

## Select exact evidence independently

The same token also registers tools that read evidence without a keyword
query, for an agent or selector that picks parts itself:

1. `sediment_list_context_sessions` lists the granted Session identifiers.
2. `sediment_evidence_inventory` lists a Session's Inference calls, without
   message content.
3. `sediment_evidence_manifest` lists one call's parts, with their types and
   roles.
4. `sediment_read_evidence` fetches up to 32 selected parts.

Exact reads keep request order and repeated parts, and can include readable
reasoning. Every request checks the grant and Quarantine again.

## Retrieval limits

| Limit | Value |
| --- | --- |
| Inference calls in the granted Sessions | 1,000 |
| Parts that keyword tools scan | 16,384 |
| References in one exact read | 32, in a request of at most 64 KiB |
| Exact read response | 1 MiB |
| Server deadline | 30 seconds; the pi tool waits 35 seconds and doesn't retry |

A request over a limit fails as a whole. Evidence and report reads share two
workers, so a third concurrent read returns 503. The
[API reference](../reference/api.md#post-querycontext) lists every field and
refusal reason, and ADRs 0021, 0022, 0025, and 0026 define the contracts.

## Select and fetch evidence

To prepare a packet yourself, use the operator CLI. It calls your Sediment API
with your [operator login](../reference/cli.md#sediment-login) and never calls
a model.

1. Create a private directory outside the source repository:

   ```bash
   umask 077
   EVIDENCE_DIR='/absolute/private/path/evidence-run'
   mkdir "$EVIDENCE_DIR"
   SOURCE_SESSION_ID='<source Session identifier>'
   ```

2. List the Session's Inference calls:

   ```bash
   sediment evidence inventory "$SOURCE_SESSION_ID"
   ```

   `found: false` means that the deployment has no such Session. The
   inventory counts visible and quarantined calls separately.

3. List the parts of one call, using its Fact identifier from the inventory:

   ```bash
   sediment evidence inspect "$SOURCE_SESSION_ID" '<Inference call Fact ID>'
   ```

   The manifest lists input messages, then output messages, with each part's
   type and reference, but not its content.

4. Save the parts that you need in `$EVIDENCE_DIR/references.json`. Choose the
   captured goals, constraints, relevant tool calls, and tool results. Copy
   1–32 references from the manifest. Indices start at zero:

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

5. Fetch the parts into a path that doesn't exist:

   ```bash
   sediment evidence fetch "$SOURCE_SESSION_ID" \
     --references "$EVIDENCE_DIR/references.json" \
     --output "$EVIDENCE_DIR/packet.json"
   ```

   The CLI validates the whole response, then writes a mode `0600` file. It
   refuses to overwrite an existing file.

6. Read the packet, and confirm that it covers the goal and constraints that
   the next agent needs. Note anything missing.

Each item keeps its reference, observation time, message role, and complete
part. Each command reads a fresh database snapshot. If a fetch fails after a
Quarantine change, rebuild your selection. The CLI never writes a partial
packet.
