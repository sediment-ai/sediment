# Check release and deployment security

Review release evidence before installation. Retain the assets, release version,
and installed package identities in your deployment record.

## Review the supplied evidence

1. Download the complete release asset set into a private directory. Verify
   `SHA256SUMS` before using the artifacts. Confirm that `security-support.json`
   identifies the expected Git revision and an unexpired support period.
2. Check the eight inventories: the resolved client installation, the production
   pi dependencies, and API, PostgreSQL, and gateway images for AMD64 and ARM64.
   Match the wheel hashes to the packages you install. Image evidence describes
   those images only; it doesn't attest to a package installation.
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

## Verify the installed packages

Record `sediment --version` and retain the installer version and supplied
software bill of materials (SBOM). If the installed dependencies differ from
the release inventory, assess the installed environment separately.

Keep the host libraries and PostgreSQL installation patched through their
respective package managers. Package evidence doesn't cover the operating
system, your reverse proxy, or an independently operated gateway.

Release maintainers follow the [security verification procedure](../../CONTRIBUTING.md#security-verification-and-release-policy).

## Verify the installed environment

Follow [Deploy Sediment](deploy.md) for the network, credential, process,
storage, and backup configuration. If you change the deployment configuration,
verify equivalent controls.
In particular, keep the database private, distribute capture credentials instead
of operator credentials, and keep bootstrap credentials out of the API.

Set a dedicated storage quota. Retain encrypted backups with restricted access
and test restoration. Monitor database restarts and failed security scans. A
process limit doesn't protect the PostgreSQL data volume after compromise of
the database process.

Inventory operator-managed operating systems, container runtimes, harnesses,
gateways, extensions, and other installed software. Track each publisher's
support policy and your deployment's update deadline. The supplied SBOMs cover
Sediment artifacts; they don't establish the maintenance status of the whole
assessment scope.
