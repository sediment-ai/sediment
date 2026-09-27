# Agent integrations

Use this page to choose an agent integration, and then follow that agent's
guide. Every guide assumes the machine setup in
[Configure local capture](local-capture.md). To enroll a whole team, follow
[Enroll your team](../operate/run-pilot.md).

## Compare integrations

| Agent | Developer decisions | Commit Attribution | Inference calls | Edit observations |
| --- | --- | --- | --- | --- |
| [Claude Code](agents/claude-code.md) | Accept and reject | Yes | Optional, through a gateway | Optional |
| [Codex](agents/codex.md) | Accept and reject | Yes | Optional, through a gateway | Optional, single-file patches |
| [Cursor](agents/cursor.md) | Implicit accepts for successful Agent writes | Yes | No | No |
| [pi](agents/pi.md) | Implicit accepts | Yes | Optional, through a gateway | Optional |
| [Copilot Chat](#github-copilot-chat) | Accept, reject, and retention | No | No integration | No |

A Developer decision can record a human choice or an automatic result, and
Sediment keeps that distinction in `explicit`. Each agent reports decisions in
a different unit, so decision counts aren't comparable across agents.
[Developer decisions by agent harness](../explanation/how-capture-works.md#developer-decisions-by-agent-harness)
defines each agent's events and missing signals.

Edit observations pair the text that an agent applied with the file's content
at Session end. Claude Code transcript capture also records external line
counts, Rejected edits, and Retry linkages. The other agents don't.

Codex decision telemetry can include patch text even when transcript capture is
off. [Privacy boundaries and ceilings](../explanation/how-capture-works.md#privacy-boundaries-and-ceilings)
lists what each capture path sends.

## GitHub Copilot Chat

Sediment reads three Copilot Chat log events: `copilot_chat.edit.feedback`,
`copilot_chat.inline.done`, and `copilot_chat.edit.survival`. It doesn't cover
Copilot CLI.

`sediment install` doesn't configure Copilot Chat. Configure the client, or a
collector, to send those events as authenticated OpenTelemetry Protocol (OTLP)
HTTP/JSON logs to [`POST /v1/logs`](../reference/api.md#post-v1logs). See
[Copilot's OpenTelemetry configuration](https://code.visualstudio.com/docs/agents/guides/monitoring-agents).
Traces alone don't carry the decision events.

After telemetry flushes, check that `developer_decisions` grows in
`sediment facts`.

## Integrate another agent

Sediment has no built-in integration for Gemini CLI, OpenCode, or Factory. A
compatible gateway can capture their inference calls when the client carries a
Session identifier. Developer decisions, commit notes, and Edit observations
need an agent integration.

To build one, start with [What a harness client sends](../agents/capture-clients.md)
and the [pi extension](../../shims/pi/README.md). The agent also needs an entry
in Sediment's `AgentHarness` enum. The server skips unregistered agents.
