# Review rules

Comment on behavioral bugs. Skip style, naming, and formatting.

## Git hooks the installer writes

- Installing Sediment must not change whether a repository's own hook blocks.
  A block that runs last must leave the hook's exit status as it found it.
- A block can sit before code the developer adds later. It must not `exit`
  early or skip the rest of the hook.
- Generated hook text must run under `sh`, `set -e`, and `set -u`. Git calls
  `prepare-commit-msg` with one, two, or three arguments.
- Sediment's own commands must still never fail a commit or push.
- The per-repository installer and the fleet bundle share one writer. A fix in
  only one of them is incomplete; the fleet fixtures must match the writer.
- `docs/operate/agent-guide.md` and `cli/sediment_cli/agent_guide.txt` must stay
  identical.

## Tests

- Prefer a failing test over a comment when the diff claims a behavior the
  tests do not cover. Hook tests must run a real `git` against a real
  repository, not a mock.
