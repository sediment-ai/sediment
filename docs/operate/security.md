# Check release and deployment security

Review release evidence before installation. Retain the assets, source revision,
and deployed artifact identities in your deployment record.

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

## Confine agent retrieval access

If you enable context retrieval, bind its credential to one source Session or
an explicit set of at most 32 Sessions with the [deployment settings](deploy.md).
The entire selected Session is authorized, including future captured Facts;
an observed commit link does not establish exclusive repository ownership.
The grant also permits factual inventory, manifest, and exact part reads,
including readable reasoning. Keyword omission is not a permission boundary.
Rotate the token when changing the set. Give the agent only
its retrieval endpoint/token and any separate ingest credential required by
capture. Keep operator login, database credentials, deployment configuration,
and mounts containing them outside the agent environment. A separate process
under the same unrestricted account doesn't establish isolation.

Use a separate container or operating-system account for a continuation
experiment. If all inference must remain within your perimeter, use internal
model and gateway endpoints too. The retrieval endpoint doesn't control where
the agent sends its next model request. Historical evidence can contain
instructions; ordinary harness tool controls still govern subsequent actions.

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

The client collector also inventories the CLI's managed PostgreSQL build.
Its evidence records the archive identity, server version, installed hashes,
and host-library links. Host libraries remain operator-managed; update them
through the host package manager. The [local-server workflow](../../.github/workflows/local-server.yml)
checks startup, data reuse, and shutdown on the supported targets.

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

For the gateway zlib disposition, review the exact image's Python callers as
well as native symbols. The `gzip_write_api_unreachable` check doesn't inspect
filename arguments to permitted SAML (Security Assertion Markup Language)
extensions. Compare `gateway_caller_files` with the reviewed LiteLLM and
`onelogin/saml2` sources. Changed files require another source review; matching
hashes establish integrity, not approval. Retain that review with the image evidence.

Run the security workflow and the normal test suite after updating the policy.
A stale review, unsupported version, incomplete inventory, unavailable metadata
source, or scanner error blocks the gate. Keep failed evidence for investigation.

Automatic security runs check lint, formatting, review dates, and source
fingerprints before building. If a review expires, run the workflow manually to
collect evidence, then review or remove the disposition. Manual and release
runs still enforce the final scanner gate.

Draft pull requests wait until review readiness. The
[prose validation path](../onboarding.md#your-first-pull-request) doesn't produce
artifact scan evidence. Selected jobs must complete successfully for the final
security gate to pass.

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
