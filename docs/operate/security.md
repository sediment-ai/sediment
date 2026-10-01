# Secure a deployment

Use this page to decide what Sediment may capture, to keep its data and
credentials private, and to verify a release before you install it.

## Review what Sediment stores

Facts can contain model inputs and outputs, patch arguments, applied edit
text, and observed file content. Mirrors contain pushed Git history. Git notes
contain Session identifiers and timestamps. Before you enroll your team, agree
which capture paths to enable. [Privacy boundaries](../explanation/how-capture-works.md#privacy-boundaries-and-ceilings)
lists the fields that each path sends.

Basic redaction replaces common credential shapes before Sediment stores a
Fact. It isn't complete secret detection. If a Fact contains a credential,
[quarantine it](maintain.md#quarantine-captured-data) and rotate the
credential.

| Location | Contents |
| --- | --- |
| PostgreSQL | Facts and the quarantine audit log |
| `~/.sediment/server/server.env` | Generated API tokens |
| `database-roles.env` (`~/.sediment/` on your own host, `~/sediment-deploy/` on EC2) and the server's private environment | Database role passwords, and the migrator and runtime URLs |
| `~/.sediment/server/mirror` | Git mirrors |
| Export and staging directories that you choose | Training rows and temporary Derivation data |
| Sender buffer directories, if enabled | Unredacted payloads awaiting delivery |

Facts and mirrors never expire. Backups hold the same content, so encrypt
them and keep the decryption key off the server.

## Separate the credentials

| Credential | Who holds it | What it can do |
| --- | --- | --- |
| Capture token | One developer machine or gateway | Send data. It can't read anything. |
| Operator token | Operators | Read Facts, reports, and captured content |
| Retrieval token | One agent environment | Read content from specific Sessions. See [Continue a task with captured evidence](resume-with-evidence.md). |
| Webhook secret | GitHub | Sign forge deliveries |
| Database administrator | The one-time provisioning shell, or your database administrator | Create and change Sediment's roles. The server never receives it. |
| Migrator password | `sediment server` at start, and `sediment db upgrade` | Change the schema and its grants. The server removes it from its environment before the API and its workers start. |
| Runtime password | The API | Append and read Facts. It can't change or delete a Fact. |
| Operator password | The operator shell | Read Facts and append quarantine records. It can't change or delete a Fact. |

Never give an agent the operator token, `server.env`, or a database
credential. A process under the same operating-system account as those files
can read them.

## Limit network exposure

| Direction | Connection |
| --- | --- |
| Inbound | HTTPS to your proxy, which forwards to the API on `127.0.0.1:8000` |
| API to PostgreSQL | A private connection with the runtime role |
| API to Git hosts | Mirror fetches from the hosts in `SEDIMENT_ALLOWED_CLONE_HOSTS` |
| Gateway to API | Capture requests with a capture token |

`GET /health` and the API schema pages (`/docs`, `/redoc`, and
`/openapi.json`) need no credential and return no captured content. To turn
off the schema pages, set `SEDIMENT_ENABLE_DOCS=false`. Every other route
needs a capture token, an operator token, a retrieval token, or a valid webhook
signature. Keep PostgreSQL off the public network.

Each entry in `SEDIMENT_ALLOWED_CLONE_HOSTS` is a host that Sediment trusts to
fetch from. An explicit entry can permit a private address.

Sediment sends no analytics, crash reports, or update checks. Your model
endpoint, not Sediment, decides where inference content goes. To keep all
content inside your network, run the gateway, model endpoint, and Git remotes
there too.

Run one API process. It serves two concurrent report or evidence reads with a
30-second deadline, and runs two mirror workers with a 120-second deadline and
up to 16 queued jobs. A third concurrent read returns 503.

## Verify a release

Each [GitHub release](https://github.com/sediment-ai/sediment/releases)
publishes checksums, software bills of materials (SBOMs), vulnerability scan
results, and a support end date. To check a release before you install it:

1. Download the release assets into a private directory, and verify them
   against `SHA256SUMS`.
2. Check that `security-support.json` names the expected Git revision and a
   support period that hasn't ended.
3. Match the wheel hashes in the client inventory to the packages that you
   install.
4. Read each `.gate.json` result. A disposition applies only to the exact
   package, version, and architecture that it names.
5. Record the installed version (`sediment --version`) and the support end
   date. Upgrade before that date.

Release scans cover Sediment's packages and images. They don't cover your
operating system, PostgreSQL, reverse proxy, Traefik, or gateway. Keep those
patched through their own channels.

The supplied API and PostgreSQL images retain Debian OpenSSL
`3.0.22-1~deb12u1`, which has no Bookworm fix for CVE-2026-84782 as of
2026-09-30. The affected Datagram Transport Layer Security (DTLS) handshake
path can disclose process memory or crash the process. The supplied services
use stream transports and do not configure DTLS. The time-bounded disposition
requires the reviewed image inputs and measured deployment confinement. It
doesn't repair OpenSSL or cover added DTLS callers, native extensions, custom
commands, or changed deployment settings. Apply a distribution fix when
available. See the [Debian advisory](https://security-tracker.debian.org/tracker/CVE-2026-84782).

The API and PostgreSQL images also retain PCRE2 `10.42-1+deb12u1`.
The 2026-10-01 review found no fixed Bookworm package for CVE-2026-103111.
An attacker-controlled pattern can corrupt memory when a caller uses
just-in-time (JIT) matching with an assigned growable stack. The reviewed
Sediment paths don't accept patterns for that mode, and PostgreSQL uses a
different regex engine. Installed GNU `grep -P` can still reach the affected
mode. The scoped mitigation expires on 2026-10-12 and requires the reviewed
image inputs and deployment conditions. It doesn't repair PCRE2 or cover
custom commands, initialization scripts, native extensions, or added regex
callers. Process compromise can affect credentials and writable data despite
container confinement. Apply a fixed Bookworm package when available. See
the [PCRE2 advisory](https://github.com/PCRE2Project/pcre2/security/advisories/GHSA-r9hj-j2rw-4q3m).

Maintainers follow
[Security verification and release policy](../../CONTRIBUTING.md#security-verification-and-release-policy).
