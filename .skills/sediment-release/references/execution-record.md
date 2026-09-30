# Execution record

## 0.4.0 publication — 2026-09-30

- [Release issue](https://github.com/sediment-ai/sediment/issues/228) and
  [preparation pull request](https://github.com/sediment-ai/sediment/pull/229).
  The maintainer requested assessment and publication. Work used an isolated
  worktree from `086cd11f3b6a3c9500a6523a7eef516b06cd864a`.
- Decision: 0.4.0 includes PostgreSQL role management, the agent guide,
  context pruning, installer changes, and dependency fixes since 0.3.0.
  The changelog and release notes preserve external-database migration steps
  and removal of the two gateway installer flags.
- Parsed lockfiles differ only in seven first-party version labels.
  Initial security validation then found that cryptography 50.0.1 had lost
  upstream support after 50.0.2 shipped on 2026-09-30. The gateway pin and
  maintenance review moved to 50.0.2, whose wheels bundle OpenSSL 4.0.3.
- Both gateway inventories show that sole package change. Deployment
  predicates, reviewed LiteLLM/SAML caller hashes, and tarfile patch evidence
  remain identical on each architecture. All 65 dispositions preserve dates,
  expiry, targets, predicates, evidence, and decisions. Their final binding is
  `cc4870333c074f7a64d0ce80b31751c0f64d5508047169efc327ec370429e145`.
- Local checks pass: 28 help tests, 142 focused API/release/security/docs tests,
  and 94 gateway policy/assurance tests with 22 external-tool/image skips.
  The complete no-publish rehearsal passes at clean preparation commit
  `452239ba0d33ccd709e3b0b290e9b2fbbe37d949`. Twelve distributions, their hashes,
  and the synthetic acceptance record are retained by the release operator.
- The final separate review against `origin/main` reports no findings.
  [Hosted tests](https://github.com/sediment-ai/sediment/actions/runs/36744353962)
  pass 6,744 tests, Compose acceptance, and release rehearsal.
  [Hosted security](https://github.com/sediment-ai/sediment/actions/runs/36744353474)
  passes all eight inventories, including 13 gateway runtime tests on each
  architecture. Native platforms, shims, and all four consumer profiles pass.
- The preparation merge is `70a97bcb80c31c7e3ae8aa113a4a8fc3b09cbe08`.
  Its tree matches the validated preparation commit. Annotated tag `v0.4.0`
  targets that merge. The ordinary tag push uses the configured maintainer
  creation permission; immutable-tag protections remain unchanged.
- [Publication workflow](https://github.com/sediment-ai/sediment/actions/runs/36746963143)
  succeeds. The permitted maintainer approval targets only its waiting `pypi`
  deployment. No environment or reviewer settings change.
- [v0.4.0](https://github.com/sediment-ai/sediment/releases/tag/v0.4.0) becomes
  the latest immutable release at `2026-09-30T16:57:12Z`. All 87 GitHub assets
  pass checksum coverage, and all eight inventories bind to the tagged commit.
  Twelve distribution hashes match the workflow files, GitHub assets, and six
  PyPI version records. Each project has one wheel and one source distribution;
  none is yanked.
- A fresh Python 3.12.14 installation from PyPI reports all six distributions
  at 0.4.0. Version, command help, server help, release-pinned guide links, and
  bundled pi resources pass. The check doesn't register a harness extension.
- After publication, the README refresh uses `release=0.4.0` on the dynamic
  Shields endpoint. Its SVG reports `pypi: v0.4.0`; the link remains the
  `sediment-cli` PyPI project. The release issue records final display checks.
- The owned PostgreSQL cluster stops after local verification. Historical
  artifact findings remain in #225, and the independent pi development
  dependency finding remains in #227. No support period or release gate changes.

## 0.3.0 preparation — 2026-09-27

- Base: `c745b6bb44412b3cbb6e65e6a1bd3e226a28365f` on `origin/main`.
- Previous release: `v0.2.0`.
- [Tracking issue](https://github.com/sediment-ai/sediment/issues/155).
- Work used an isolated `codex/release-v0.3.0` worktree because the original
  checkout contained unrelated uncommitted changes.
- Parsed lockfile comparison verified that only seven first-party version
  labels changed. All other lockfile fields were identical.
- Sixty-three dispositions preserved dates, predicates, targets, and decisions.
  Their review binding changed from
  `d6601ebbe9258d2ae9634cc32846063dbbedc1f8c95ff4305688b271d45767e6` to
  `81cd505a1387d92dae6d574caf792fbd8553fb058058618abbf60fb9f9be88dd`.
- Local rehearsal on disposable PostgreSQL 17.11 passed: six wheels, six source
  distributions, and the installed synthetic pipeline. Help checks: 28 passed.
- Focused API, release, security, and documentation checks: 132 passed.
- Lint, format, SPDX, docs, generated references, review-binding, and title
  checks passed.
- [Release preparation pull request](https://github.com/sediment-ai/sediment/pull/156)
  at `829d1bff8479d62f17fdc703701436eb18070896`.
- [Hosted validation](https://github.com/sediment-ai/sediment/actions/runs/36347962363)
  and [security scans](https://github.com/sediment-ai/sediment/actions/runs/36347962174).

- All hosted preparation checks passed. The Python suite reported 6,433 passed
  and 29 skipped across its three partitions. Compose acceptance passed.
  The complete installed rehearsal passed, as did all four native platforms,
  four consumer profiles, the shim check, and all artifact security scans.
- The release pull request merged as
  `bb6ab804fa5e67c3d9702f0ab485072899150757`. Its tree matches the validated
  preparation commit byte for byte.
- The release operator retained local acceptance JSON and logs. The linked
  hosted validation and publishing workflows provide the shared evidence.
- Annotated tag `v0.3.0` targets that merge commit.
  [Publishing workflow](https://github.com/sediment-ai/sediment/actions/runs/36348941884).
- The ordinary tag push used the configured maintainer-creation role permission.
  GitHub reported it as a ruleset bypass. The separate immutable-tag rule had
  no bypass actors; no protection setting changed.

- The tagged workflow passed its build, four consumer profiles, source scan,
  client and pi scans, six image scans, runtime checks, and asset preparation.
- The protected `pypi` environment reported `current_user_can_approve: true`.
  The requested release authorized approval of that exact waiting deployment.
  No environment or reviewer configuration changed.
- The workflow completed successfully. [Sediment v0.3.0](https://github.com/sediment-ai/sediment/releases/tag/v0.3.0)
  became the latest immutable release at `2026-09-27T20:48:24Z`.
- All 87 GitHub assets passed checksum coverage checks. The maintained verifier
  validated all eight security inventories and their exact source commit.
- All twelve distribution files matched across the retained workflow artifacts,
  GitHub Release assets, and six PyPI version records. Each project had one wheel
  and one source distribution; no file was yanked.
- A fresh Python 3.12.14 installation from PyPI with source builds disabled
  reported all six distributions at 0.3.0. Command version and help passed.
  The installed pi extension included its entry point, library, manifest,
  license, and README. The check didn't register it in a user's harness.
- The release operator retained the machine-readable publication verification.
- README badge verification on 2026-09-27 confirmed PyPI's latest version is
  `0.3.0`; both the Shields source SVG and GitHub's cached README image report
  `pypi: v0.3.0`. The dynamic image and its PyPI project link were correct, so
  no README edit or cache purge was needed.
- A subsequent stale-badge report required a README update: add
  `release=0.3.0` to the dynamic image URL so readers request a distinct image.
  The refreshed Shields URL reports `pypi: v0.3.0`.
- The owned local PostgreSQL cluster stopped after local checks. The original
  checkout and its uncommitted changes were preserved.
