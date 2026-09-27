---
name: sediment-release
description: Prepare, validate, and publish a Sediment release through its annotated-tag GitHub Actions workflow. Use when asked to cut a release, prepare release metadata, or resume an interrupted Sediment publication.
---

# Release Sediment

Deliver six PyPI distributions at one version, a verified GitHub Release, and
an accurate record of any remaining approval or failure. A pushed tag alone
isn't completion.

## Establish scope

1. Confirm the checkout's remote is `sediment-ai/sediment`. Read `AGENTS.md`,
   `docs/onboarding.md`, the release and security sections of `CONTRIBUTING.md`,
   `.github/workflows/release.yaml`, and the writing-style, issue-tracker,
   doc-sync, and review guides under `docs/agents/`.
2. Inspect the working tree, fetch `origin/main` and tags, and inspect releases,
   pending release runs, open release issues, and the Unreleased changelog.
   Resume an existing attempt before creating another release.
3. If the checkout contains unrelated work, create an isolated release worktree
   from the selected base using the repository's branch naming convention.
   Preserve the existing checkout.
4. Choose the version from the user's request and merged changes. State the
   decision; don't invent a fixed patch/minor policy. Supported syntax is
   `X.Y.Z` or `X.Y.ZrcN`; the tag is `v` plus that version. Check GitHub and
   PyPI for collisions before publication.
5. Claim the release issue according to the repository contract. Limit changes
   to metadata, generated output, release documentation, and demonstrated
   release blockers.

## Prepare and validate

Follow [Preparation and verification](references/preparation.md). Read live
files before following examples.

- Synchronize the seven project declarations, six packages' exact pins, API
  runtime version, lockfile, help golden, OpenAPI document, examples, changelog,
  and first-party maintenance catalog.
- Classify old-version matches. Preserve historical releases, third-party
  protocol versions, and fixtures that test arbitrary version input.
- Review changed inputs before updating a security review digest. Never renew
  expiry or weaken conditions to make a release pass.
- Run the complete no-publish rehearsal against disposable PostgreSQL. Retain
  its acceptance record, twelve distributions, and source revision.
- Complete documentation sync, focused checks, required hosted checks, and the
  repository review contract. Open a pull request with `Closes #<issue>`.
  If your harness supports pull request attachments, attach it to the task.

## Merge, tag, and publish

1. Resolve required checks and review blockers. Follow the maintainer-approval
   requirement. An explicit instruction to cut the release authorizes necessary
   release actions within that scope; don't ask again for authorized actions.
   If a platform requires a human decision, finish preparation and present the
   concrete pull request or waiting deployment. Don't disable protections or
   override a blocked operation.
2. Squash-merge the validated release pull request. Record its merge commit,
   fetch it, and verify its version and ancestry in `origin/main`. Tag that
   exact commit, even if `main` advances.
3. Confirm the version is unused. Create an annotated tag at the recorded commit
   and push only that tag. Don't tag an unmerged branch, retarget a tag, or
   publish local artifacts separately from the workflow.
4. Follow the workflow using bounded waits: build and compatibility, security,
   prepare-release, protected `pypi` publication, then GitHub publication.
   Inspect pending deployments to identify the actual gate. When authorized
   and permitted, approve that exact deployment; otherwise provide its run
   link and required action.
5. Follow [Publication and recovery](references/publication.md) to verify all
   six PyPI projects, GitHub asset hashes, an isolated public installation, and
   the README's displayed PyPI badge. After stable publication, update the
   badge's `release=VERSION` query parameter and verify GitHub's rendered
   version before reporting completion. Report the release link and actual
   checks. A waiting or failed attempt remains incomplete.

## Record observations

Update this skill for demonstrated workflow details. Keep a specific release's
version, commits, and run URLs in [Execution record](references/execution-record.md).
Validate skill changes with skill-creator's `quick_validate.py` when available.
