# Install capture

Install wires a git repository and this machine for capture: repository git
hooks, detected agent hooks, and an environment file. Doctor checks that
configuration.

## Sub-features

- `install-repo` adds Sediment git hooks to a repository.
- `install-env` writes capture environment configuration for the logged-in server.
- `doctor-machine` checks attribution and server health on this machine.
- `doctor-repo` checks per-repository hooks when given a repo path.
- `uninstall-repo` removes per-repo hooks.

## How to get to it (user POV)

- Run `sediment install .` inside a git repository after `sediment login`.
- Run `sediment doctor` for machine checks, or `sediment doctor <repo>` for repo checks.
- Run `sediment uninstall <repo>` to remove per-repo hooks.

## Driving it with verify-sediment

Preconditions:

- Launch, Doctor, and Login have passed for this run.
- A disposable directory is available under `$SEDIMENT_VERIFY_HOME`.

- **Scratch repo.** Create an empty git repository. Run `mkdir -p "$SEDIMENT_VERIFY_HOME/scratch" && git -C "$SEDIMENT_VERIFY_HOME/scratch" init -q`.
- **Install.** Wire the repo. Run `.skills/verify-sediment/bin/cli -- install "$SEDIMENT_VERIFY_HOME/scratch"`. Exit code `0`.
- **Doctor.** Check machine and repo. Run `.skills/verify-sediment/bin/cli -- doctor "$SEDIMENT_VERIFY_HOME/scratch"`. Exit code `0`. Resolve any `FAIL` line before claiming success.
- **Optional hook smoke.** Mark a synthetic session and create an empty commit, then show the notes ref (quickstart §5). Require `session_id` in the note JSON.
- **Proof.** Save install and doctor transcripts under `$SEDIMENT_VERIFY_EVIDENCE/install-capture/`. Uninstall afterward with `.skills/verify-sediment/bin/cli -- uninstall "$SEDIMENT_VERIFY_HOME/scratch"`; keep the transcripts.

## Gotchas

- Doctor rows depend on which agents are installed on the machine. Absent agents are skips, not failures; a `FAIL` must be fixed.
- Install needs a prior login so the env file points at the verify server URL.
- Do not install into the Sediment checkout under test; use the scratch path under the verify `HOME`.
- Pi has extra prerequisites (`docs/capture/agents/pi.md`); skip pi-specific proof when those are unmet and record the skip.
