# ADR 0017 — Prepared capture payloads have a bounded transport buffer

Status: accepted

Payload buffering requires explicit deployment enrollment. This contract does
not enable capture on a running host.

## Context

The gateway callback and pi capture channels can complete agent work while
losing its evidence during an API outage. A later transcript extraction can
observe different file contents. Network retries without a retained source
cannot recover a sender restart. Existing Fact deduplication protects the
receiver after delivery; it doesn't retain unsent payloads.

## Decision

Decision: permit an opt-in, bounded, sender-local transport buffer of prepared
HTTP payloads. This is a narrow exception to ADR 0001's persisted-state wording,
not another FactStore or a persisted Derivation. PostgreSQL remains the sole
FactStore under ADR 0012. Derivations, reports, and exports never read transport
records, and no transport disposition becomes a training label.

One Python implementation owns publication, limits, acknowledgment classification,
and replay. The gateway imports it, the transcript client reuses it, and pi calls
its bounded enqueue command with explicit direct fallback. Each enrolled host supervises its own worker.
Database UNIQUE indexes remain the Fact deduplication authority. Payloads remain
exact after enqueue; process recovery resends them without rebuilding observations.

The transport permits one best-effort direct attempt when
private storage is unsafe, unavailable, or busy. The sender preserves the
prepared body and labels the fallback with its storage reason. Capacity, size,
and identity declines remain terminal. Strict enqueue still means durable
publication and never attempts HTTP; callers explicitly select fallback.

Buffering requires explicit consent through `SEDIMENT_DELIVERY_DIR`. It doesn't
enable another capture channel. Transport credentials remain outside stored
records, but gateway payload content may itself contain credentials and code
before server-side Basic redaction. Private filesystem permissions and an
operator-controlled volume are required; no application encryption is claimed.
The default ceilings are 256 MiB, 2,048 active entries, 8 MiB per entry, and a
24-hour replay window. Maintenance removes expired content when it runs;
an inactive host cannot promise deadline-based deletion.

Gateway requests may carry a capture UUID and aware observation instant prepared
once by the callback. The receiver uses the deployment organization and capture
UUID for Fact identity and preserves that observation instant. This makes arrival
order independent of the captured call chronology. Source `model_call_id` retains
its provider meaning. Requests without this metadata retain receiver-time capture.
The duplicate receipt names the Fact that the database retained.

OTLP source event times remain unchanged. Its Fact receipt timestamps remain
receiver capture times. HTTP success, declared skip, and persisted Fact counts
remain separate claims. A missing event before durable enqueue cannot be
recovered or counted from the buffer alone.

### Transport records and acknowledgment

`cli/sediment_cli/delivery.py` owns the shared transport contract. A published
record retains the prepared body, format version, delivery ID, capture instant,
channel, destination, byte count, and checksum. Transport authorization headers
and copied environment values stay outside the record. Replay reads the active
credential, verifies the original destination, and preserves the prepared bytes.

Enqueue holds a short lock for capacity accounting and publication. It flushes
and syncs a private temporary file before atomic publication, then syncs the
directory. An interrupted unpublished file isn't delivered. Retry state and
content-free terminal receipts remain separate from immutable payload bytes.
A separate process lock gives one worker ownership of the directory; network
requests don't hold the enqueue lock. After a lost acknowledgment, redelivery
relies on PostgreSQL uniqueness rather than sender-side Fact deduplication.

A gateway acknowledgment requires the retained `fact_id` and Boolean `stored`,
or the recognized `skipped: true` / `reason: no_session` envelope. An OTLP `{}`
acknowledges delivery only. A malformed acknowledgment remains pending.
Authorization failures and other non-retryable client responses block automatic
retry; the operator must deliberately retry after correcting the cause. Blocked
entries retain the replay deadline. Neither a declared skip nor a successful
helper process proves that the receiver stored a Fact.

[Sender replay operations](../capture/local-capture.md#preserve-prepared-payloads-through-outages)
defines consent, permissions, limits, retention, retry timing, credential changes,
and worker enrollment. [Gateway deployment](../operate/deploy.md#enable-bundled-litellm)
requires server upgrade before callback rollout and describes the gateway-owned
worker. Pi's Attribution channel remains independent of delivery. Transcript
replay uses the prepared observation instead of extracting it again.

### Acceptance boundaries

`cli/tests/test_delivery_transport.py` checks exact bytes, private storage,
concurrent publication, interruption, capacity, expiration, worker ownership,
acknowledgments, and retry classification. Gateway identity and chronology checks
live in `apps/api/tests/test_gateway_capture_receipt.py`; callback restart and
credential checks live in `litellm/tests/test_sediment_callback.py`.
`shims/pi/test/delivery.test.ts` and `shims/pi/test/process.test.ts` cover the
installed helper's process contract and independent capture channels.

The [installed release rehearsal](../operate/rehearse-release.md#run-the-no-publish-rehearsal)
checks outage, restart, lost response, stored source identities, reports, bundle
roundtrip, and eligible training output over repeated delivery of the same Facts.
Its synthetic corpus doesn't establish live harness compatibility. Live pi,
Cursor, Codex, gateway, and forge capture need separate source-version and
recovery evidence before release acceptance.

## Consequences

Sender restart and bounded API outages become recoverable after durable enqueue.
Local capture hosts acquire explicit storage and worker-enrollment requirements.
The enrollment guide must state unredacted-content handling and stopped-worker
retention limits. A disk or host loss can still destroy pending payloads.

Fact and training schemas don't change. The gateway HTTP envelope gains capture
metadata; server upgrade precedes callback rollout. Existing Facts remain
immutable, and the transport buffer cannot reconstruct evidence already lost.
Native vendor telemetry, separate Cursor delivery, and forge webhooks need their
own recovery evidence during deployment verification.
