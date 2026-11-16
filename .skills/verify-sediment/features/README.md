# Sediment verification map

This directory is the maintained source for verifying user-facing Sediment
behavior. Read the index before driving the app, then use the matching feature
file as the recipe.

## Baseline preconditions

- Launch with `.skills/verify-sediment/bin/launch` and `eval` its exports.
- Require an isolated `SEDIMENT_VERIFY_HOME` and a free loopback port — never
  drive `~/.sediment/server` or port 8000 unless this run started them.
- Run `.skills/verify-sediment/bin/doctor` and require `status=ok`, a non-empty
  version, and mode-`600` `server.env` under the verify home.
- Put proof under `$SEDIMENT_VERIFY_EVIDENCE`. Cleanup must leave it in place.
- Drive the CLI through `.skills/verify-sediment/bin/cli -- …`.

## Driving conventions

- Start every recipe from Launch + Doctor unless its preconditions say otherwise.
- Prefer documented CLI verbs and HTTP doors over library calls.
- Treat every command as literal. Keep quoted names and flags unchanged.
- After a mutation, confirm with a second user-facing read (`facts`, reopen).
- Restore or discard scratch repos after install journeys. Keep proof artifacts.

## Proof and skip reporting

- Capture the user action and the resulting state, not only the final line.
- CLI proof includes the command, stdout, stderr, and exit code.
- HTTP proof includes path, status, and body when the body is the contract.
- Mutation proof includes a read-only second view (`sediment facts`, session
  tables, or a file the user would inspect).
- Record the feature ID and entry point with every artifact.
- Report an unreachable path with the attempted command and unmet precondition.
- Do not report a skipped entry point as verified through a different path.

## Feature entry contract

Each feature file starts with an H1 title and one paragraph describing the
user-visible behavior. It then uses exactly four H2 sections in this order.

1. `Sub-features` lists short IDs with one line for each behavior.
2. `How to get to it (user POV)` lists every user entry point.
3. `Driving it with verify-sediment` starts with `Preconditions:` and uses
   labeled bullets that pair each user action with an exact command and
   observable result.
4. `Gotchas` lists traps that can waste or invalidate a verification run.

Keep implementation details out of the map. Name only user paths, stable
handles, required state, commands, and observable proof.

## Features

- [Local server](./local-server.md) — start managed PostgreSQL + API, health.
- [Login](./login.md) — store operator and ingest credentials for a server.
- [Demo ingest](./demo-ingest.md) — plant synthetic Facts and confirm counts.
- [Install capture](./install-capture.md) — wire a scratch repo and run doctor.
- [Facts](./facts.md) — read Fact counts for the logged-in org.
