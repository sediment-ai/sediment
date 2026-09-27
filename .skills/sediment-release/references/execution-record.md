# Execution record

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
- The owned local PostgreSQL cluster stopped after local checks. The original
  checkout and its uncommitted changes were preserved.
