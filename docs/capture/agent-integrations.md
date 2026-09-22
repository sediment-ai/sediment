# Agent integrations

Choose an integration by the evidence that you need to capture. The dedicated
Claude Code, Codex, and Cursor guides take you from installation through
verification.

[Run a Cursor, pi, and Codex pilot](../operate/run-pilot.md) covers a pinned
source checkout, model routing, and verification across those three harnesses.

## Compare integrations

| Agent | Developer decisions | Commit Attribution | Edit observations |
| --- | --- | --- | --- |
| [Claude Code](#claude-code) | Accept / reject | Yes | Opt-in |
| [Codex](#codex) | Accept / reject | Yes | Opt-in: single-file patches |
| [Cursor](#cursor) | Implicit accepts for successful Agent writes | Yes | No extractor |
| [pi](#pi) | Implicit accepts | Yes | Opt-in |
| [Copilot Chat](#github-copilot-chat) | Accept / reject / retention | No installer hook | No extractor |

Cursor Tab edits write commit Attribution without a Developer decision because
Cursor doesn't supply a per-call identifier for that event.

Edit observations pair the text that an agent applied with the file's content
at Session end. They support an Edit retention score; they aren't full Session
transcripts. Claude Code also supports external line counts, Rejected edits,
and Retry linkages through its opt-in transcript hooks. Codex and pi don't
capture those three signals.

Copilot's retention events carry a vendor-provided grade on a Developer
decision. They aren't transcript-derived Edit observations.

A Developer decision can reflect a human choice or an automatic result.
Sediment preserves that distinction in `explicit`. Decision units differ
across agents, so counts aren't comparable counts of human approvals.
[Developer decisions by agent harness](../explanation/how-capture-works.md#developer-decisions-by-agent-harness)
defines the event mapping and missing-signal limits.

## Claude Code

Claude Code supports native accept and reject telemetry, commit Attribution,
and gateway inference calls from the command-line interface (CLI). Optional
transcript capture adds Edit observations, Rejected edits, Retry linkages, and
external line counts.

[Capture Claude Code work](agents/claude-code.md) covers installation,
Developer decisions, inference calls, Edit observations, verification, and
limits.

## Codex

Codex supports native patch decisions, commit Attribution, Responses API
gateway capture, and opt-in Edit observations for successful single-file
patches.

Native Codex decisions can retain patch/tool arguments in `raw` even when
`log_user_prompt=false`. Transcript opt-in adds separate Edit observations;
it doesn't make native decision telemetry free of code text.

[Capture Codex work](agents/codex.md) covers hook trust, the required `[otel]`
table, gateway configuration, transcript extraction, verification, and limits.

## Cursor

Cursor supports commit Attribution for local desktop Agent and Tab work.
Successful Agent `Write` calls can also emit implicit-accept Developer
decisions. Cursor doesn't support inference-call capture or Edit observations.

[Capture Cursor work](agents/cursor.md) covers native user hooks, the complete
event-to-evidence flow, verification, privacy, and platform limits.

## pi

The installer registers `shims/pi/` in `~/.pi/agent/settings.json` when you run
it from a Sediment source checkout. The installed CLI package doesn't include
the extension.

Connect the CLI and install from the checkout:

```bash
sediment login https://sediment-api.example.com --capture
uv run sediment install --user-id alice /path/to/repo
. "$HOME/.sediment/env.sh"
```

Replace `alice` with your developer identifier. The generated environment sets
`SEDIMENT_OTLP_ENDPOINT` and `SEDIMENT_INGEST_TOKEN` for decision delivery.
Restart pi from that environment. Attribution remains independent of telemetry.

If you approve sending applied text and observed file text, opt in:

```bash
uv run sediment install --user-id alice --transcripts /path/to/repo
. "$HOME/.sediment/env.sh"
```

This writes `SEDIMENT_PI_TRANSCRIPTS=1` and enables extraction at
`session_shutdown`. An unset variable or any other value disables pi content
capture. An ordinary reinstall preserves a generated opt-in. With `--no-env`,
set the endpoint, token, and `SEDIMENT_PI_TRANSCRIPTS=1` manually in the pi
environment. An existing endpoint-only installation must explicitly opt in to
continue Edit observations.

A successful `edit` or `write` produces an implicit accept. Stock pi exposes
no explicit human approval gesture, so Sediment doesn't label these events as
human approvals. The extractor omits failed edits and doesn't emit Rejected
edits or Retry linkages.

Pi resolves relative edit paths from the absolute working directory in the
transcript header. Absolute paths retain their meaning. If that directory is
absent or invalid, relative edits decline with an `execution_directory_invalid`
count. Home expansion (`~`), `@` prefixes, file URLs, and Unicode space
normalization require source context this contract doesn't supply; those paths
decline with an `unsupported_path` count.

Keep a fork's transcript and its immediate parent in the original Session
directory. Sediment reads only a different regular `.jsonl` file in the same
physical directory, without symlinks or traversal, and caps that read at 64 MiB.
The parent must be a complete version 3 source. Sediment compares immutable
entry identities and content, ignoring pi's rewritten `parentId`, and excludes
matching inherited messages with an `inherited_entry` count. Child observations
retain their original tool-call identifiers and source timestamps. Sediment
doesn't read further ancestors or infer ownership from timestamps.

If the parent is missing, unreadable, oversized, or inconsistent, Sediment
declines the fork with `parent_source_unverified` and retains pending snapshots.
Ownership diagnostics count message entries and contain no source paths or
content. Path diagnostics count affected edits.

If content capture is enabled and a one-task host keeps pi alive, set
`SEDIMENT_EXTRACT_ON_SETTLE=1` so extraction runs at `agent_settled`. Leave the
variable unset for an interactive Session. Repeated settling would preserve an
early file state under first-write-wins deduplication.

After pi edits a file and the Session ends, commit the change. Copy the actual
Session identifier from its note and verify that Session:

```bash
git notes --ref=refs/notes/sediment show HEAD
sediment doctor /path/to/repo --agent pi --session-id '<Session identifier>'
```

Add `--transcripts` when you opted in to content capture. Add `--inference-calls`
only for a Session routed through a gateway. The
[Pilot gateway procedure](../operate/run-pilot.md#add-approved-gateway-capture)
provides a concrete `models.json` entry. [Configure local capture](local-capture.md)
defines the shared endpoint, git hook, and transcript privacy behavior.

To enable agent-requested evidence from one previous Session, configure the
independent `SEDIMENT_RETRIEVAL_ENDPOINT` and `SEDIMENT_RETRIEVAL_TOKEN` pair
in an isolated agent environment. This registers `sediment_retrieve_context`
without changing capture settings. The [continuation guide](../operate/resume-with-evidence.md#enable-agent-requested-retrieval)
defines source authorization and response limits. Retrieval never falls back to
your ingest token or operator login.

## GitHub Copilot Chat

Sediment translates Copilot Chat's `copilot_chat.edit.feedback`,
`copilot_chat.inline.done`, and `copilot_chat.edit.survival` log events. This
integration covers Copilot Chat telemetry; it doesn't establish Copilot CLI
support.

Configure the client or a collector to deliver those events as authenticated
OpenTelemetry Protocol (OTLP) HTTP/JSON logs to Sediment's
[`POST /v1/logs`](../reference/api.md#post-v1logs) endpoint. Consult
[Copilot's OpenTelemetry configuration](https://code.visualstudio.com/docs/agents/guides/monitoring-agents)
for client settings. Generic traces alone don't supply the decision events
that Sediment consumes.

After telemetry flushes, run `sediment facts` and confirm that
`developer_decisions` grows. `sediment install` doesn't configure Copilot Chat
or install a Copilot Attribution hook. Sediment has no Copilot transcript
extractor.

## Capture model inputs and outcomes

An agent integration alone doesn't create Inference-call Facts.
[Configure inference-call capture](managed-capture.md#configure-inference-call-capture)
records calls through a supported gateway. The gateway must carry the real
Session identifier so Sediment can join the call to the agent's Facts.

For pushed commits and continuous integration (CI) outcomes, follow
[Configure push and CI capture](managed-capture.md#configure-push-and-ci-capture).

Transcript extraction sends selected edit text and Session-end file content.
Gateway capture includes model inputs and outputs.
[Privacy boundaries and ceilings](../explanation/how-capture-works.md#privacy-boundaries-and-ceilings)
lists the payload details.

## Integrate another agent

Sediment doesn't have a built-in integration for Gemini CLI, OpenCode, or
Factory.

A compatible gateway can capture inference calls when its client carries
Session identity. Developer decisions, commit notes, and Edit observations
need an agent integration.

To build one, start with [What a harness client sends](../agents/capture-clients.md)
and the [pi extension](../../shims/pi/README.md). A supported agent needs
registration in Sediment's `AgentHarness` enum and the appropriate hooks or shim.
The server skips unregistered agent values.
