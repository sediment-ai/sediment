# Security policy

Sediment captures prompts, completions, developer decisions, and repository
contents. Treat a vulnerability that exposes or changes stored Facts as high
severity. Basic redaction doesn't guarantee removal of every secret.

## Report a vulnerability

Report suspected vulnerabilities through
[GitHub's private vulnerability reporting form](https://github.com/sediment-ai/sediment/security/advisories/new).
Include affected versions, reproduction steps, and sanitized evidence. Don't
open a public issue or attach captured data, credentials, or private repository
contents. Maintainers coordinate investigation and disclosure through the
private report.

There is no bounty program or contractual response guarantee.

## Supported releases

Each release's `security-support.json` asset records its support start date,
end date, and maintenance owner. Use those dates to identify supported releases
and upgrade before support ends. A source checkout or Git tag without that
metadata doesn't establish a support period. A successful historical scan
doesn't extend support.

An upstream component that loses support requires replacement or an identified
maintenance provider. Deployment operators install released security updates
and record deployed versions.

## Release checks

Release publication requires checks of the exact resolved client installation,
production pi dependencies, and final API, PostgreSQL, and optional gateway
images on each offered architecture. The checks cover known vulnerabilities,
reviewed maintenance policies, runtime support, local static rules, and existing
secret scanning. Scanner failures and missing evidence fail the checks.

Known vulnerabilities with available fixes block release. An unfixed high or
critical finding requires an exact, evidenced disposition with a named owner
and an expiry within 30 days. A mitigation doesn't mean the affected code is
fixed. Unsupported software has no exception. Complete scan reports remain in
the release evidence.

Release assets include CycloneDX software bills of materials (SBOMs), resolved
inventories, support metadata, and checksums. A daily job rescans retained SBOMs
for supported releases without executing historical code. Dependency update
proposals run daily. GitHub dependency alerts and security update proposals
supplement the repository's open-source checks; paid scanning features aren't
a prerequisite.

See [Check release and deployment security](docs/operate/security.md) for
reproduction, evidence review, and deployment responsibilities.

## Scope

Authentication, capture installers, client credential storage, Fact storage,
mirrors, exports, dependency handling, and release integrity are in scope.
Operator-managed hosts, databases, gateways, harnesses, and surrounding software
require their own inventories and maintenance checks. Repository checks don't
certify a deployment or establish compliance with a security standard.
