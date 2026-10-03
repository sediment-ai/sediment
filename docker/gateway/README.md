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

The image uses Hugging Face Hub 2.1.1, which receives upstream security fixes.
Tokenizers 0.23.1 and LiteLLM 1.104.0 retain their original code. A guarded
metadata patch declares their tested compatibility with that exact Hub version
and updates each wheel's file-integrity record. This is Sediment's compatibility
declaration; upstream Tokenizers and LiteLLM still declare Hub versions below 2.
LiteLLM never imports Hub: its bound mirrors Tokenizers', the only Hub caller.
Image tests exercise the real Hub download caller with revisions and
credentials, local Claude token counting, and proxy capture. Hub's HTTPX2
client coexists with LiteLLM's HTTPX client. Remove each declaration when a
reviewed release of that package declares Hub 2 support.

LiteLLM 1.104.0 also bundles optional PostgreSQL clients, Bedrock real-time
packages, and the Vertex speech SDK. The image removes these unused
dependencies, including the native `awscrt` library. It removes the bundled
PgBouncer executable and its unused libevent dependency, and rejects
`LITELLM_PGBOUNCER_*` settings before startup.
Anthropic routes retain the same provider boundary.

The Dockerfile records pinned input images and direct dependency updates.
The PyJWT override uses 2.15.1, which includes the deeply nested payload
error-handling fix and restores trailing Base64URL padding compatibility.
The urllib3 override uses 2.8.0 to fix HTTPS proxy TLS configuration,
unbounded chunk-size lines, and chunked Deflate streaming.
The cryptography override uses 50.0.2, the supported upstream release whose
wheels bundle OpenSSL 4.0.3. The system OpenSSL packages retain their separate
reviewed pins.
The OAuthLib override uses 4.0.0 to fix CVE-2026-49264 and CVE-2026-49265.
Image tests reject the removed JSONP revocation option and exercise the
authorization-code client in FastAPI SSO.
Image labels record removed packages and source patches. The final software
bill of materials records the resolved components. The guarded source patch
removes unused database retry decorators and imports. When Prisma is absent,
its error predicates return `False` and its SQLSTATE lookup returns `None`. The
patch returns 401 for an incorrect master key.
Missing or invalid keys don't require a database and don't cause an import
failure. The patch rejects an unexpected vendor source hash, patch site, or
classifier return type.

The image pins both Python operating system packages to
`3.13.16_git20261002-r0`. Wolfi builds Python 3.13 from the CPython maintenance
branch. This build reports CPython `3.13.16` from commit `15e701addee`: the
[v3.13.16 tag plus two commits](https://github.com/python/cpython/compare/v3.13.16...15e701addee),
the post-release version bump and a removal of unused GitHub files. It links
OpenSSL 3.6. The `-r1` rebuild links OpenSSL 4, so the exact pin excludes it.
Security probes preserve the complete observed version and reject unreleased
runtimes.

CPython 3.13.16 includes the tarfile and archive fixes for CVE-2026-82049,
CVE-2026-19672, CVE-2026-87910, and CVE-2026-15310, so the image carries no
Python source patch. Image tests exercise both tar extraction filters with a
hard link to a symbolic link.

The image pins the OpenSSL 3.6.5-r0 packages, the OpenSSL 4.0.3-r1 libraries
they depend on, and the reviewed legacy provider. The package manager,
certificate bundle, and libuuid also stay at the revisions in
`security/maintenance.json`. Update these pins with their maintenance reviews
and image checks.

The image also pins zlib to the reviewed Wolfi `1.3.2-r7` release. The pin
prevents a release candidate from replacing the reviewed package during
`apk upgrade`. The zlib finding retains its exact caller assessment and native
symbol check; the pin doesn't repair the library. Review an available released
fix before changing the pin. The pypdf override uses `6.19.0`, which bounds
alphabetical PDF page labels; an image test verifies the reader's fallback.

Wolfi ships OpenSSL 3.6.5 through its OpenSSL 4 transition: `libcrypto3` and
`libssl3` 3.6.5 depend on `openssl-4.0-libcrypto` and `openssl-4.0-libssl`,
which own `/etc/ssl/openssl.cnf` and `/etc/ssl/ca.cnf`. The vendor image's
`openssl` 3.6.4 package owns those files too, so the build removes it before
installing the pinned set; the reviewed CLI is reinstalled at 3.6.5. Python's
`ssl` module and the `openssl` command report OpenSSL 3.6.5. The OpenSSL 4
libraries also bring Wolfi's Brotli libraries. Review the next transition step,
such as a Python build linked against OpenSSL 4, and refresh image evidence
before advancing these pins.

If you change these inputs or provider boundaries, run the image tests on both
architectures and retain the resulting inventories and scans. Tests exercise
proxy startup, streamed and non-streamed Anthropic completions, their captured
content, standalone callback delivery, protocol
dependency interoperability, and rejected configurations. The security workflow
runs these gateway tests against each scanned architecture. The deployment
runbook is [Rehearse the single-host Compose deployment](../../docs/operate/rehearse-compose.md).
