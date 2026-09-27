# Publication and recovery

## Publish the recorded commit

Inspect live rulesets, public visibility, and the protected `pypi` environment.
Existing releases normally establish the six trusted publishers. For missing
publishers, follow `CONTRIBUTING.md`'s bootstrap procedure; don't bypass the gate.

Check that the tag is absent locally and remotely before creating it. If it
exists, inspect its commit and workflow instead. PyPI versions can't be reused.

The maintainer-creation tag ruleset can implement permitted role access through
GitHub's configured bypass actors. A normal authorized tag push can therefore
print a bypass notice. Verify the live rule and actor permissions; don't alter
them or force a rejected push. The separate immutable-tag rule forbids updates
and deletions without bypass actors.

```sh
git fetch origin main --tags
git merge-base --is-ancestor "$RELEASE_COMMIT" origin/main
git tag -a "v$RELEASE_VERSION" "$RELEASE_COMMIT" -m "v$RELEASE_VERSION"
git push origin "refs/tags/v$RELEASE_VERSION"
```

Use `gh run list --workflow release.yaml` to identify the run. Inspect its head
commit, jobs, logs, and pending deployments with bounded polling.

The workflow owns the twelve distributions, security evidence, installer,
support metadata, and `SHA256SUMS`. GitHub publication follows PyPI; don't
create a separate release ahead of it. The workflow rechecks tag identity and
`main` ancestry before each publisher.

After `build` passes, you can download its `python-distributions` artifact with
`gh run download --name python-distributions`. Run the maintained
`release_rehearsal.validate_wheels` and `validate_sdists` functions on that set
and retain its twelve hashes for the final comparison. Keep this directory
separate from the later GitHub Release download.

If a protected deployment waits, inspect:

```sh
gh api "repos/sediment-ai/sediment/actions/runs/$RELEASE_RUN/pending_deployments"
```

Approval targets the returned environment ID for that exact run and follows
the user's authorization and environment rules. Never remove a reviewer or
override a rejected environment approval.

## Verify completion

1. Confirm the workflow succeeded and the GitHub Release has the intended tag
   commit, published state, and prerelease/latest status.
2. Download assets to a fresh directory. Verify `SHA256SUMS`, the twelve Python
   distribution filenames, installer, support metadata, and security evidence.
   Read `scripts/rescan_releases.py` for the live bundle contract instead of
   hard-coding the total asset count.
3. Read `https://pypi.org/pypi/PROJECT/VERSION/json` for `sediment-api`,
   `sediment-capture`, `sediment-cli`, `sediment-core`, `sediment-derive`, and
   `sediment-export`. Verify one wheel and one source distribution each, no
   yanked files, and SHA-256 equality with the GitHub assets and the retained
   workflow artifacts when downloaded.
4. Install `sediment-cli==VERSION` from PyPI with uv in an isolated Python 3.12
   environment outside the checkout. Refresh the index cache when needed.
   Verify all six installed versions, `sediment --version`, and command help.
   If promised by the release, verify bundled pi resources without registering
   the extension in the user's harness.
5. Report the release URL and actual checks. Stop only task-owned temporary
   services. Retain logs and the worktree when useful for investigation.

## Recover interruption

| State | Next action |
| --- | --- |
| Build, compatibility, or security failure before uploads | Diagnose logs and fix demonstrated blockers through review. Don't weaken the gate. If tagged source must change, use a distinct version/tag. |
| Transient failure, unchanged source | Rerun the failed job once after inspecting its cause. Repeated identical failure requires diagnosis. |
| Protected `pypi` deployment waiting | Complete permitted approval or provide the exact run and required human action. |
| Partial PyPI upload | Preserve files and tag. Rerun the publisher with the retained validated artifacts. `uv publish --check-url` skips accepted filenames. |
| PyPI complete, GitHub draft incomplete | Rerun `publish-github`; it replaces and verifies the entire draft asset set. |
| GitHub Release published | Verify it. Don't delete assets or retarget its tag. |
| Required workflow artifacts expired | Identify missing evidence. Don't silently substitute rebuilt artifacts for the validated files. |

Issue closure after the preparation merge doesn't establish publication. Record
remaining work accurately until all release acceptance criteria pass.
