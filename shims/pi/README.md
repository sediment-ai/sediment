# sediment-pi

The pi-harness shim for Sediment capture. Implements the three
client-side obligations of the Sediment client capture contract
(`docs/agents/capture-clients.md`):

| seam | behavior |
|---|---|
| Inference calls | `before_provider_headers` stamps the live Session identifier on requests for the configured provider and API. |
| decisions | edit/write `tool_execution_end` (no error) → `sediment.tool_decision` record to `POST $SEDIMENT_OTLP_ENDPOINT/v1/logs`. Always `accept` / `explicit=false` — stock pi has no human approval gesture, and a gesture we cannot observe is absent, never guessed at. |
| attribution | edit/write `tool_execution_end` (error or not) → `sediment mark --tool pi` with `{session_id, cwd}` on stdin |
| transcripts | When `SEDIMENT_PI_TRANSCRIPTS=1`, `session_shutdown` (except `reason: "reload"`, which tears down the extension runtime, not the Session) → `sediment transcript --agent pi` with `{session_id, transcript_path}` on stdin |

Capture failures never throw into the harness. Each channel reports failures
independently; Attribution doesn't prove a missing Decision or Edit observation.
With `SEDIMENT_DELIVERY_DIR`, the shared Python helper retains prepared Decision
and opted-in transcript payloads before delivery. Without it, delivery is best-effort.

## Install

Use pi 0.84.1 or later with Node 24. Node 22.18 or later in the Node 22
release line is also supported. The shim rejects other Node release lines.
The shim has no runtime package dependencies and doesn't require `npm install`.
Your pi installation is a separate prerequisite: inventory its packages and
support status before installing it in your deployment.

Run it from a checkout: shims are not shipped in the `sediment-cli` wheel,
so an installed CLI resolves no shim directory and skips the pi
registration with a message.

```
sediment install <repo>
```

registers this directory in `~/.pi/agent/settings.json` (`extensions` list)
alongside the git hooks and the claude-code/codex hook fragments.
`uninstall --agents` removes the entry; `doctor` checks it.

## Opt in to Edit observations

Transcript capture sends applied edit text and observed file text. Decision
capture and Attribution don't authorize that content capture.

To opt in, run `sediment install --transcripts <repo>` from the checkout.
Load the generated environment before you restart pi.

If you use `--no-env`, set the endpoint, token, and explicit content opt-in in
the environment that starts pi:

```bash
export SEDIMENT_OTLP_ENDPOINT=https://sediment-api.example.com
export SEDIMENT_INGEST_TOKEN='<deployment token>'
export SEDIMENT_PI_TRANSCRIPTS=1
```

An existing pi installation that supplies only an endpoint and token continues
to send Developer decisions and mark Attribution. To continue Edit observations,
you must opt in to transcript content with `SEDIMENT_PI_TRANSCRIPTS=1`.
An unset variable or any other value disables transcript extraction.

## Environment

- `SEDIMENT_PROVIDER_ID` / `SEDIMENT_PROVIDER_API` — provider and API that receive
  the Session header (defaults `sediment` / `anthropic-messages`). The native
  header hook reads the Session identifier for each request. It replaces stale
  Session headers and preserves all other headers. If the identifier is absent
  or invalid for an HTTP header, the shim omits it and logs a content-free
  diagnostic. Decision capture and Attribution continue independently.
- `SEDIMENT_OTLP_ENDPOINT` — ingest base URL. Required for decision capture;
  unset means not opted in (there is deliberately no
  `OTEL_EXPORTER_OTLP_ENDPOINT` fallback — see the contract doc). Remote hosts
  require HTTPS. Plain HTTP accepts only literal `localhost`, IPv4 loopback,
  or `[::1]`; authenticated POSTs reject redirects.
- `SEDIMENT_INGEST_TOKEN` — bearer token (falls back to an `Authorization`
  header in `OTEL_EXPORTER_OTLP_HEADERS`, only ever sent to the explicit
  endpoint above).
- `SEDIMENT_PI_TRANSCRIPTS` — explicit content opt-in. Only the exact value
  `1` enables transcript extraction. It doesn't change decision capture,
  Attribution, or gateway Session identity.
- `SEDIMENT_EXTRACT_ON_SETTLE` — a nonempty value also extracts on
  `agent_settled`, provided `SEDIMENT_PI_TRANSCRIPTS=1`. Set it only for hosts
  that use one Session per task. An interactive Session can settle before its
  final edit; first-write-wins storage would retain that earlier observation.
  A runtime reload never triggers transcript extraction.
- `OTEL_RESOURCE_ATTRIBUTES` — `user.id` is forwarded as resource identity.
- `SEDIMENT_SCRIPT_DIR` — fallback directory containing
  `sediment_attribution.py`, `sediment_transcript.py`, and `sediment_delivery.py`.
  Copy the delivery implementation from `cli/sediment_cli/delivery.py`, not the
  checkout shim. The shim prefers an
  installed `sediment` executable on `PATH`. Without one, each source-checkout
  script resolves independently, so a missing transcript client doesn't
  disable attribution.
- `SEDIMENT_PYTHON` — interpreter for the Python clients (default
  `python3`; stdlib-only, any 3.12+ works).
- `SEDIMENT_DELIVERY_DIR` — opt-in private prepared-payload storage. Run
  `sediment delivery replay --watch` under your existing process supervisor.
  Startup requests a bounded drain; it doesn't install a background service.
  See [Sender replay operations](../../docs/capture/local-capture.md#preserve-prepared-payloads-through-outages)
  for content consent, limits, retention, active credentials, and recovery.

Child processes have a bounded deadline. On macOS and Linux, the runner stops
its owned process group, including descendants. Spawn, stdin, timeout, and
nonzero-exit failures produce content-free channel diagnostics. The runner drains
stderr without relaying its contents. A successful enqueue requires a matching
structured acknowledgment; exit zero alone doesn't prove durable acceptance.
Pi explicitly requests the shared helper's storage-fault fallback. If private
storage is unsafe, unavailable, or busy, one direct attempt preserves the prepared
Decision. A validated direct acknowledgment reports `best_effort` and its storage
reason. Capacity and identity declines remain failures. Repair storage even when
the direct send succeeds; that send has no durable recovery guarantee.

## Develop

```
npm ci
npm test           # node --test (type stripping; Node 24 or Node 22.18+)
npm run typecheck  # tsc --noEmit
```

The `shims` workflow builds and installs Sediment wheels, then runs the pi
delivery test against that installed command. Changes to the shared Python
delivery owner also trigger this workflow. To exercise the same test locally,
set `SEDIMENT_PI_TEST_PYTHON` to the installed environment's Python executable
and `SEDIMENT_PI_TEST_INSTALLED_BIN` to its `bin` directory before `npm test`.
