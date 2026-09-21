# Check release and deployment security

Use this procedure to review a Sediment release before installation and
to retain evidence for your deployment. You need access to the release assets
and the source revision identified by that release.

## Review the supplied evidence

1. Download the complete release asset set into a private directory. Verify
   `SHA256SUMS` before using the artifacts. Confirm that `security-support.json`
   identifies the expected Git revision and an unexpired support period.
2. Check the eight inventories: the resolved client installation, the production
   pi dependencies, and API, PostgreSQL, and gateway images for AMD64 and ARM64.
   Match the image digests and wheel hashes to the artifacts you install.
3. Read each `.gate.json` result and its complete scanner reports. Review every
   disposition in the recorded policy. Confirm that its exact package, version,
   architecture, deployment conditions, owner, and expiry apply to your
   installation. A mitigated vulnerability remains in the raw report.
4. Record your deployed artifact identities and the release support end date.
   Schedule installation of updates before that date. The operator owns this
   deployment record and update rollout.

If you omit the optional gateway, exclude it from your deployed inventory. Keep
its release evidence with the other supplied artifacts. If you use your own
gateway or database, inventory and assess that software separately.

## Reproduce the checks

Run the commands from the release source checkout. Use Python 3.12.14 and uv
0.12.17. Install the exact Trivy version and verified checksum specified in
[the security workflow](../../.github/workflows/security.yml). The scanner
helpers install their pinned Python tools into isolated uv tool environments.
They don't add those tools to Sediment's runtime dependencies.

```sh
uv run --python 3.12.14 --no-project python scripts/security_static.py --out security-evidence
uv build --all-packages --wheel --out-dir dist
uv run --python 3.12.14 --no-project python scripts/security_scan.py client \
  --wheels dist --out security-evidence
```

The client command installs those six wheels into a fresh environment and audits
the resolved dependencies. Its software bill of materials (SBOM) represents that installation. An installation
that resolves different versions needs another inventory.

The client collector also downloads the managed PostgreSQL build selected by the
installed CLI. `client-native-postgres.json` records its archive URL, pinned hash,
reported server version, installed file hashes, and host-library links. The
PostgreSQL runtime passes the same support and latest-patch checks as the image.
The [local-server workflow](../../.github/workflows/local-server.yml) exercises
startup, data reuse, and shutdown on all four supported native targets. Host
libraries remain operator-managed prerequisites; this evidence doesn't attest
to the host operating system. Keep them updated with the host package manager.

With Node 24.21.0 on `PATH`, run the pi check inside the shim directory:

```sh
(
  cd shims/pi
  npm exec --yes --package=npm@12.0.2 -- \
    uv run --python 3.12.14 --no-project python ../../scripts/security_scan.py pi \
    --out ../../security-evidence
)
```

The pi collector generates npm's production dependency projection, removes
build dependencies, and compares it with the installed production tree. It adds
the measured Node runtime through the maintained CycloneDX library. The harness
and the rest of the developer machine remain separate installation prerequisites.

Use the image build and scan commands in the security workflow for each offered
architecture. Supply the source revision and source digest to the image build.
The collector probes the resulting immutable image, records its identity, and
checks that it belongs to the source under review.

To rescan one retained inventory, keep its referenced evidence files beside it:

```sh
uv run --python 3.12.14 --no-project python scripts/security_scan.py rescan \
  --inventory retained/api-amd64.inventory.json --out rescanned
```

The command verifies retained hashes and scans the existing SBOM with fresh
advisory data. It also checks the reviewed maintenance catalog and public
upstream metadata. It doesn't rebuild or run the historical artifact.
A source scanner checks only the local reviewed rules; it doesn't replace a
manual review of authentication, data access, or deployment configuration.

## Maintain the release policy

When a dependency or image changes, review its upstream support policy and exact
resolved versions. Update [the maintenance catalog](../../security/maintenance.json)
with primary evidence and a review expiry within 30 days. Repository activity
alone doesn't override an explicit version support policy. Debian-maintained
backports have a distinct provider from their upstream release line.

When a scanner reports a vulnerability, apply an available fix. If the finding
has no fix, document its exact scope, prerequisites, residual risk, evidence,
owner, and expiry in [the disposition register](../../security/dispositions.json).
Verify the required image and deployment conditions. Don't remove findings from
raw reports or use a blanket exclusion. A changed source or deployment condition
requires another review.

An independent maintainer must approve changes to dispositions or release
gates. Contributors cannot approve their own suppressions. Source fingerprints
and required predicates verify integrity; they do not authorize a suppression.

PostgreSQL retains native XML support that an ordinary SQL role can invoke.
A `mitigated` XML finding relies on the measured database access and containment
conditions; it doesn't mean that the library is patched or that Fact grants
sandbox native parsing. A compromised database process can still affect the
Fact volume and database availability. Review each advisory's affected function
against the exact distribution source before assigning a disposition.

For the gateway zlib disposition, pair the native symbol check with a review of
the exact image's Python callers. The `gzip_write_api_unreachable` predicate
rejects dynamic imports and unreviewed static embeds. It doesn't inspect Python
filename arguments to the permitted SAML extensions. Retain the caller source
identities and review together with the image evidence.

Run the security workflow and the normal test suite after updating the policy.
A stale review, unsupported version, incomplete inventory, unavailable metadata
source, or scanner error blocks the gate. Keep failed evidence for investigation.

Automatic security runs check lint, formatting, and retained review dates and
source fingerprints before building artifacts. If a review is stale, use a
manual security run to collect complete evidence, then review or remove the
obsolete disposition. Manual and reusable release runs keep the final scanner's
policy checks and don't stop at the early review check. The preflight never
renews a review or approves an image's deployment conditions.

Draft pull requests wait until you mark them ready for review. The
[contributor workflow](../onboarding.md#your-first-pull-request) describes the
prose path, which doesn't produce artifact scan evidence. The final `security`
job fails if a selected upstream job fails, is canceled, or doesn't complete.

## Verify the installed environment

Follow [Deploy Sediment](deploy.md) for the tested network, credential, process,
storage, and backup configuration. If you change the deployment configuration,
verify equivalent controls.
In particular, keep the database private, distribute capture credentials instead
of operator credentials, and keep bootstrap credentials out of the API.

Set a dedicated storage quota. Retain encrypted backups with restricted access
and test restoration. Monitor database restarts and failed security scans. A
container limit doesn't protect the PostgreSQL data volume after compromise of
the database process.

Inventory operator-managed operating systems, container runtimes, harnesses,
gateways, extensions, and other installed software. Track each publisher's
support policy and your deployment's update deadline. The supplied SBOMs cover
Sediment artifacts; they don't establish the maintenance status of the whole
assessment scope.
