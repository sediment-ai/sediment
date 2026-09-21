# Supplied gateway image

This image runs the checked-in Anthropic model routes and Sediment capture
callback. The `claude-*` wildcard preserves requests for the different models
that Claude Code uses within a Session.

The entrypoint requires one self-contained local YAML configuration. Every
model route must name the `anthropic/` provider. Credentials come from the
environment. The entrypoint rejects other providers, database settings,
configuration overlays, and external secret-manager settings before the proxy
starts.

The image excludes the unused Google GenAI and Vertex AI SDKs. Their dependency
constraints prevent a supported WebSockets release. It also excludes the
discontinued Prisma Python client, embedded Prisma engines and Node runtime,
archived backoff package, and unused legacy Langfuse SDK. Database-backed
LiteLLM features, Google provider routes, and the legacy Langfuse integration
aren't supported by this image. The standalone Sediment callback remains
available for a separately maintained gateway.

LiteLLM 1.102.0 also bundles optional PostgreSQL clients and Bedrock real-time
packages. The image removes these unused dependencies, including the native
`awscrt` library. It removes the bundled PgBouncer executable and its unused
libevent dependency, and rejects `LITELLM_PGBOUNCER_*` settings before startup.
Anthropic routes retain the same provider boundary.

The Dockerfile records pinned input images and direct dependency updates.
Image labels record removed packages and source patches. The final software
bill of materials records the resolved components. The guarded source patch
removes only the unused database retry decorators and imports; it rejects an
unexpected vendor source hash or patch site.

The image pins both Python operating system packages to `3.13.15-r8`. This
[Wolfi build recipe](https://github.com/wolfi-dev/os/blob/d52bf0e18defc56a9d18c3fe4c214b545d82a93c/python-3.13.yaml)
uses CPython release `3.13.15`. The pins prevent `apk` from selecting a
development snapshot that sorts after the released version. Security probes
preserve the complete observed version and reject unreleased runtimes.

The image also pins zlib to the reviewed Wolfi `1.3.2-r7` release. The pin
prevents a release candidate from replacing the reviewed package during
`apk upgrade`. The zlib finding retains its exact caller assessment and native
symbol check; the pin doesn't repair the library. Review an available released
fix before changing the pin. The pypdf override uses `6.19.0`, which bounds
alphabetical PDF page labels; an image test verifies the reader's fallback.

If you change these inputs or provider boundaries, run the image tests on both
architectures and retain the resulting inventories and scans. Tests exercise
proxy startup, streamed and non-streamed Anthropic completions, their captured
content, standalone callback delivery, protocol
dependency interoperability, and rejected configurations. The security workflow
runs these gateway tests against each scanned architecture. The deployment
runbook is [Deploy Sediment](../../docs/operate/deploy.md).
