# ADR 0019 — Repository identity survives name changes

Status: accepted

The [Contract checks](#contract-checks) define the acceptance boundaries for
repository identity across capture, storage, Derivations, and exports.

Amends [ADR 0010](0010-canonical-ci-outcome-facts.md),
[ADR 0013](0013-git-note-observation-facts.md),
[ADR 0014](0014-factual-outcomes-and-training-evidence.md), and
[ADR 0016](0016-bundle-derivation-consistency.md).

## Context

A repository name identifies a location that can change or be reused.
Moving a mirror directory after a GitHub rename leaves immutable Pushes and
Session-to-commit observations with their source names. Matching only the
directory's name loses those relationships. Matching clone URLs can instead
attach a different repository's observation after the original name is reused.
Equal commit SHAs and shared Git history cannot distinguish forks.

Changing observation attachment alone also breaks the canonical contract.
A Rollout can carry an old-name observation beside a new-name commit while
bundle validation and RLVR still require literal repository equality.
Repository qualification needs one owner across capture, Derivations,
reports, and exports.

## Decision

Decision: capture provider repository identity on repository-bearing Facts,
store immutable Repository rename Facts, and resolve repository qualification
through one pure owner. Preserve every captured repository name. Do not infer
identity from clone URLs, mirror directories, commit SHAs, or Session IDs.

### Captured identity and rename receipts

Repository identity is `(org_id, repository_provider, repository_host,
repository_id)`. The provider ID identifies one repository lifetime within a
forge instance. The organization remains the deployment tenancy boundary.
The configured forge host supplies the instance namespace; a clone URL or
request Host header cannot supply it.

Push, CIOutcome, SessionCommitObservation, PullRequestMerge, and
PullRequestRevision carry the flat nullable fields `repository_provider`,
`repository_host`, and `repository_id`. Each triple is either wholly present
or wholly absent. PR Facts carry an independent `head_repository_*` triple.
A fork's head identity never inherits the target identity. CI provider and
repository provider remain distinct concepts.

The Fact schema owns validated provider, host, repository ID, and required
repository-name types. GitHub capture uses the repository ID from the signed
payload. Vendor-neutral CI identity is an assertion by its authenticated
sender, like its existing provider and run identity. Malformed or absent
identity leaves an otherwise valid Fact available with counted absence.

A RepositoryRename Fact records its own ID, organization, required identity
triple, old and new normalized repository names, optional source event ID,
optional occurrence time, and capture time. It contains no raw webhook body.
The GitHub rename payload supplies no reliable occurrence timestamp for this
contract, so that value remains absent. Repository update time and webhook
arrival order do not substitute for occurrence time.

The rename route stores the Fact even when mirrors are disabled. Its receipt
names the retained Fact. Storage failure returns a retryable response before
any success acknowledgment. Mirror availability does not determine whether
the rename happened.

### Storage and historical evidence

PostgreSQL remains the deduplication authority. Identified Push, observation,
and PR keys use provider repository identity instead of the repository name.
Legacy NULL-identity rows retain their separate existing natural-key branch.
CI run identity retains provider, run, and attempt semantics and gains the
forge-instance namespace. A conflicting repository claim for the same run is
an identity conflict, not another successful run receipt.

Repository rename delivery IDs deduplicate within organization and forge
instance. A missing provider delivery ID is keyless. A conflicting retained
primary or natural key cannot return a foreign identity or silently accept
a different rename edge. Repeated receipts preserve the first captured Fact
and its time. Identity-bearing Session observations require their exact
retained source Push to agree on organization and repository identity. Legacy
observations retain their existing source-absence semantics.

A forward-only migration adds the identity columns and marks the preserved
legacy payload contract. It does not populate identities from names, URLs,
raw payloads, or provider queries. The Fact models read versions 1 and 2; version 1 has absent identity. Capture
writes version 2. Published
schema versions advance independently under ADR 0015.

Repository identity projections and rename Facts obey the same organization,
quarantine, repeatable-read snapshot, and inclusive capture boundary as their
consumers. Capture time controls evidence availability; it does not order
renames or CI attempts.

[ADR 0024](0024-targeted-commit-investigations.md) permits compact source witnesses
for commit investigations while preserving complete names, claims, and queried
source identity. Complete bundle and report populations retain this contract.

### One pure repository resolver

The shared resolver consumes captured identity projections, rename Facts,
organization, and an aware `as_of`. It uses no network, mutable Git
configuration, or wall-clock time. All repository/commit and repository/PR
joins use its qualified keys, including CI resolution, Attribution,
observation binding, abandonment, merge retention, Recovery, canonical
assembly, and operational consumers.

CI consumers qualify the complete eligible run population before selecting
commits or cohort artifacts. The shared CI owner retains original outcomes
from identity-consistent runs, including non-verdicts and suspected flakes.
Conflicting runs contribute counted absence instead of a clean partial lineage.
Preloaded consumers declare their complete CI population separately from the
selected commits. The bundle validator rejects conflicts visible across its
carried CI Facts; it cannot discover an omitted external run sibling.

Equal captured provider identities establish repository equality even if a
rename receipt is missing. Different identities remain distinct when names
or Git objects coincide. A rename receipt establishes the named transition;
it does not assign identity to historical Facts that lack it.

Legacy references can retain literal-name matching only when no eligible
identified evidence claims that name. Mixed legacy/identified references
decline visibly. An exact recorded source-Push relationship can supply
identity when that Push carries it; a name-based inference cannot. Missing
identity cannot prove abandonment, CI failure, or failure to merge. Direct
Decision, usage, and Edit retention measurements remain available.

The derived repository label is the smallest normalized name in the complete
eligible evidence for that identity. It is a representative label, not a
claim about the provider's latest name. The resolver can expose the observed
names alongside it. Captured Facts retain their own names unchanged.

### Mirrors and source limits

Identified mirrors and locks use stable provider identity in a separate
versioned namespace. A rename does not move that directory. Legacy mirrors
remain separate and never acquire an ID from shared objects or origin URLs.
Derivations read mirrors without fetching and record refs under the resolved
repository key.

A stable path does not authenticate what a remote URL serves. Git transport
does not return a GitHub repository ID. Capture declines a known competing
lifetime or unresolved source before refreshing refs or creating observations.
Valid Push and rename Facts remain stored, and the refusal is counted.

An unobserved deletion, transfer, or name reuse between the signed source
receipt and Git fetch remains a substrate limit. If live acceptance requires
detecting that case, a bounded capture-side provider metadata check and
staged ref publication are a separate dependency. This decision does not add
a provider registry, online verification service, or metadata credential.
It does not claim that path names prove remote authenticity.

### Offline bundle proof

Bundle v4 retains the four v3 files and adds
`repository_identities.jsonl` and `repository_renames.jsonl`.
The first contains frozen source-Fact identity projections, including absent
identities and both PR repository roles. The second contains full rename
Facts. Both populations cover the complete visible organization through
`as_of`, independent of the artifact cohort. Each has a 50,000-row cap;
an over-limit read refuses the bundle instead of truncating evidence.

The manifest declares both populations, exact counts, byte sizes, hashes,
and the repository resolver version. Existing lossless record encoding,
Inference call identity evidence, staged publication, and partial-artifact
rules remain. Versions 1–3 require recomputation; they cannot be upgraded by
inventing identities.

The public validator constructs the shared resolver and verifies carried
Facts, source-Push anchors, organization, Session, qualified commit, capture
boundary, and exact identity copies. It runs before publication, after read,
and before in-memory bundle training. A different captured name is valid
only when the identity evidence proves the relationship. The validator never
rewrites a Fact to make the relationship fit.

The bundle proves internal consistency against a producer-declared population.
It cannot authenticate an external producer, discover an intentionally omitted
Fact, or prove external Git authenticity or historical quarantine state.

## Consequences

Repository qualification stays consistent through canonical artifacts, reports,
and every training target. Training rows retain the selected repository
identity and observation metadata. Existing recipe eligibility, categorical
labels, CI reliability, and numeric Reward rules remain unchanged.

Historical Facts without captured provider identity remain readable. Some
cross-era relationships become explicit unknowns because their source cannot
prove a repository lifetime. Recomputing cannot restore uncaptured identity.
The implementation must demonstrate both valid rename continuity and counted
refusal across forks, reused names, historical boundaries, and offline exports.

`LegacyRepositoryKey` — the join key for every Fact without captured
identity, across pushes, CI outcomes, session-commit observations, and pull
request merges and revisions — carries `(org_id, repo)` only, not provider.
Two forges sharing one organization and one legacy repository slug without
either side capturing identity would join as one repository. `ForgeProvider`
has one member, so no stored Fact can carry a second provider value.
This path stays unexercisable until a second forge exists. Adding one needs
`LegacyRepositoryKey` to carry provider before that join is safe.

## Contract checks

These checks follow repository evidence through public boundaries. Positive
cases preserve original source IDs. Negative cases refuse publication or expose
counted absence; they do not substitute a name, URL, or shared Git object.

| Boundary | Executable acceptance |
| --- | --- |
| Capture and storage | `packages/core/tests/test_repository_identity_store.py` and `apps/api/tests/test_repository_identity_ingest.py`: signed identity and rename receipts, retained duplicates, source contradictions, migration, quarantine, and source boundaries. |
| Resolution and mirrors | `packages/derive/tests/test_repository_identity_resolution.py`, `test_repository_identity_mirrors.py`, and `test_repository_identity_joins.py`: shuffled evidence, forks and reused names, stable mirror keys, unsafe refresh refusal, and exact source observations. |
| Factual Derivations | `packages/derive/tests/test_repository_identity_factual_derivations.py`: Recovery, abandonment, and merge retention preserve identity, historical boundaries, and independent pull-request head evidence. |
| Canonical artifacts and training | `packages/export/tests/test_identity_export_review_regressions.py`, `test_assembly_complete_ci_lineage.py`, and `test_derived_bundle_io.py`: renamed evidence, complete CI attempts, original observation IDs, counted source loss, every training target, and rehashed bundle contradictions. |
| Reports and investigation | `packages/export/tests/test_repository_identity_model_reports.py`, `test_repository_identity_lifecycle_reports.py`, and `apps/api/tests/test_repository_identity_queries.py`: separate lifetimes, qualified CI populations, cohorts, cursors, quarantine, and direct Decision counts. |
| Diagnostics and simulation | `packages/export/tests/test_repository_identity_diagnostics.py` and `test_repository_identity_assembly.py`: CI, Confidence, and recipe counts reuse complete identity evidence through later Developer decisions. `sim/tests/test_scenarios.py` retains its pinned scenario totals while using identified mirrors and consumer context. |
| Publication and installation | `scripts/tests/test_canonical_json_schemas.py` preserves prior contracts. `scripts/tests/test_release_rehearsal.py` builds and installs all distributions, then exercises signed rename capture, Git observations, authenticated reports and commit inspection, bundle v4, training identity, and rehashed tamper refusal. |

The installed rehearsal uses an owned PostgreSQL database, local Git, and declared
synthetic source inputs. It does not certify live pi, Cursor, Codex, gateway, or
forge interoperability. Enrolled source capture requires deployment verification.
