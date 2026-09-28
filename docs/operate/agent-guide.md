# Help operators and developers use Sediment

Use this guide when you help an operator run Sediment or help a developer
capture work from repositories and coding agents. Both roles install the
Sediment CLI. Developers connecting to a shared server use the installer's
`--capture-only` option; they don't need to run a local server.

`sediment guide` prints this bundled guide without reading configuration,
contacting a server, or changing the machine. It converts procedure links to
raw GitHub URLs pinned to the CLI's release tag. Reading those pages requires
network access. `sediment --help` lists public commands; hooks invoke others.

## Read the state before changing it

- Run `sediment --version`. For a shared deployment, compare it with the
  server's `/health` version. Use the guide from the version you operate.
- Establish the task, server URL, deployment method, server account, and
  repository. A developer's machine might only send capture to a remote server.
- Use `/health` to check server readiness and `sediment doctor <repo>` to
  inspect capture. Neither proves that the server received agent evidence.
- Stay within the requested task. Don't start another server, reinstall hooks,
  upgrade, rotate credentials, quarantine Facts, or remove data as a repair
  unless that change is authorized. Follow the linked procedure for changes.

## Help an operator

Run server commands as the account that owns the deployment. For database
commands, first set up the operator shell from the deployment procedure.
An API login doesn't configure database access. Keep operator credentials on
the operator machine; give developers capture credentials only.

| Task | First command | Procedure |
| --- | --- | --- |
| Try a local server | `sediment server` | [Quickstart](../quickstart.md) |
| Deploy on your own host | `sediment --version` | [Deploy Sediment on your own host](deploy.md) |
| Deploy on Amazon Elastic Compute Cloud (EC2) | `sediment --version` | [Deploy Sediment on EC2](deploy-ec2.md) |
| Enroll a team and connect repository webhooks | `sediment facts` | [Enroll your team](run-pilot.md) |
| Check schema and Fact counts | `sediment db status` | [Monitor the deployment](maintain.md#monitor-the-deployment) |
| Upgrade | `sediment --version` | [Upgrade Sediment](maintain.md#upgrade-sediment) |
| Back up or restore | `sediment db status` | [Back up and restore](maintain.md#back-up-and-restore) |
| Rotate credentials | `sediment --version` | [Rotate credentials](maintain.md#rotate-credentials) |
| Build a Derivation bundle | `sediment derive --help` | [Run derivations](run-derivations.md) |
| Export training data | `sediment export --help` | [Choose a training export](../exports/training-exports.md) |
| Quarantine captured data | `sediment quarantine-log` | [Quarantine captured data](maintain.md#quarantine-captured-data) |
| Tear down a deployment | `sediment --version` | [Tear down the deployment](maintain.md#tear-down-the-deployment) |

`sediment server` runs in the foreground. Without an external PostgreSQL
connection, it downloads PostgreSQL on first use and owns the local database.
It stores data, credentials, and mirrors under `~/.sediment/server` or `--root`.
Starting it provisions database roles and applies migrations. Preserve that
directory across restarts and upgrades. Plan backups before migrations or
teardown. Use the deployment's supervisor to manage a shared server.

## Help a developer

Use the server URL and capture credential that the operator supplies.
Login saves credentials; installation changes Git hooks, agent configuration,
and shell configuration. Run these steps when the developer requests capture.

| Task | First command | Procedure |
| --- | --- | --- |
| Enroll with a shared server | `sediment login <url> --capture` | [Configure local capture](../capture/local-capture.md) |
| Install repository and agent capture | `sediment install <repo>` | [Install capture](../capture/local-capture.md#install-capture) |
| Select agent setup and verify delivery | `sediment doctor <repo>` | [Choose an agent integration](../capture/agent-integrations.md) |
| Check buffered delivery | `sediment delivery status` | [Preserve prepared payloads through outages](../capture/local-capture.md#preserve-prepared-payloads-through-outages) |
| Inspect evidence for a pushed commit | `sediment commit <full commit SHA>` | [Measure agent work](measure-agent-work.md) |
| Remove capture | `sediment uninstall <repo>` | [Uninstall capture](../capture/local-capture.md#uninstall-capture) |

Default `doctor` checks configuration and contacts the server and `origin`.
It changes nothing unless you pass `--fetch`, which updates a notes tracking
ref. Only `FAIL` lines make it exit 1. A sandbox network restriction or an
unloaded capture environment can explain a failure. Use the agent procedure's
Session checks to verify delivery. `commit` requires an operator credential;
`no attributions` before the push and notes reach the server isn't a failure.

## Preserve capture while doing repository work

- Leave the marked `sediment-attribution` Git hook blocks, `notes.rewriteRef`,
  and `refs/notes/sediment` in place. Don't bypass hooks or change
  `core.hooksPath`. If hook files are tracked, leave Sediment's uncommitted
  blocks for the developer; don't commit, stash, or revert them incidentally.
- Keep the `sediment` executable at its installed path. Hooks use that absolute
  path; source installs also depend on their checkout and virtual environment.
- Preserve the `sediment env` blocks in shell profiles and Sediment's entries
  in Claude Code settings, Codex hooks and profiles, Cursor hooks, and pi
  settings. User-level agent hooks apply across repositories.
- Read hook output. Sediment commands end in `|| true`; an appended block can
  also mask a repository check that relies on its last command's exit status.
  A zero status alone doesn't prove that the repository's checks passed.

[What the installer changes](../capture/local-capture.md#what-the-installer-changes)
describes the hooks. [Roll out managed capture](../capture/managed-capture.md)
covers system settings, gateways, and fleet installation.

## Remove capture within its installation scope

- `uninstall <repo>` removes that repository's hook blocks and Sediment's
  `notes.rewriteRef` value. Require `removed git hooks from` in its output;
  a silent exit can mean that the path isn't a Git repository.
- `uninstall <repo> --agents` also removes user-level agent hooks, including
  pi, environment files, shell blocks, and verified Codex telemetry blocks.
  These user-level removals affect capture across repositories. The command
  doesn't remove Claude Code managed settings, fleet Git templates, or
  externally distributed environment settings. Follow
  [Remove managed capture](../capture/managed-capture.md#remove-managed-capture)
  for those installations. Report skipped files; don't claim complete removal.
- `logout` removes stored CLI credentials, but environment files retain their
  ingest token and hooks keep running. It doesn't stop capture.
- Neither uninstall form removes existing notes, remote notes, or
  `~/.sediment/`. An `auto_install_remotes` allowlist can reinstall hooks at
  the next Session. Remove that entry as part of an authorized teardown.
  Restart running agents so they stop using the earlier environment.

## Keep credentials and captured content private

Don't print credentials, paste them into chat, or commit them. Credential
locations include `~/.sediment/config.json`, `~/.sediment/env.sh`, the Fish
`sediment.fish` file, Codex telemetry profiles, and server environment files.
Custom server roots, shell profiles, and service settings can hold them too.
Don't dump the process environment: it can contain `SEDIMENT_INGEST_TOKEN`,
`OTEL_EXPORTER_OTLP_HEADERS`, database passwords, and gateway credentials.
Follow [Secure a deployment](security.md) when handling secrets.

Capture can leave the developer machine in these forms:

- The pre-push hook sends the whole notes ref to each push remote, including
  forks, even during `git push --dry-run`. Notes contain commit SHAs, agent
  names, Session identifiers, and timestamps, including other branches' notes.
  They don't contain prompts, diffs, file paths, model names, or hostnames.
- Agent telemetry goes to the Sediment server. Claude Code tool details can
  include paths, shell commands, edit text, and the Claude account email.
  Codex decisions can include patch code and tool arguments; Sediment removes
  the Codex account email and account identifier before storage. Cursor sends
  accept decisions for applied writes and a configured user identifier.
- Opt-in transcript capture sends edit text and edited-file contents. Claude
  Code transcripts also include refused edit text and non-agent change counts.
- If the operator routes model calls through a gateway, it captures full
  prompts and responses.

[Privacy boundaries and ceilings](../explanation/how-capture-works.md#privacy-boundaries-and-ceilings)
describes these payloads and their limits.
