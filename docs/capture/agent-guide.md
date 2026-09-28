# Work as a coding agent where Sediment is installed

This guide is for a coding agent that works on a machine or in a repository
where the developer installed Sediment. It says what to leave alone, how to
check capture, and what leaves the machine.

`sediment guide` prints this guide. `sediment --help` lists the commands that
people run. It leaves out `sediment repair-notes` and the commands that hooks
run, such as `sediment mark` and `sediment cursor-hook`.

Sediment records evidence about coding-agent work and sends it to a Sediment
server that the developer or their team runs. You don't need to run anything
for capture. Commit in the repository where Sediment is installed, and let its
git hooks run.

## Leave Sediment's changes in place

- Don't edit or remove the marked block between `# >>> sediment-attribution >>>`
  and `# <<< sediment-attribution <<<` in the repository's `post-commit`,
  `prepare-commit-msg`, and `pre-push` hooks.
- Don't skip or reroute those hooks. `git push --no-verify`, a change to
  `core.hooksPath`, or a hook manager that rewrites the hook files turns capture
  off without an error.
- If the repository keeps its hooks in the working tree, for example with husky
  or a tracked `core.hooksPath`, Sediment's blocks show up as uncommitted
  changes. Don't commit, stash, or revert them. Leave them for the developer.
- Don't edit or remove the marked block between `# >>> sediment env >>>` and
  `# <<< sediment env <<<` in `~/.zprofile`, `~/.bashrc`, or `~/.profile`.
  Without it, later agent Sessions start without capture configuration.
- Don't remove the `notes.rewriteRef` setting or the `refs/notes/sediment` ref.
- Don't move or delete the `sediment` executable. The hooks call it by its
  absolute path. For a source install, that includes the checkout and its
  virtual environment.
- Don't edit Sediment's entries in these files:
  - `~/.claude/settings.json`, or Claude Code's `managed-settings.json` on a
    managed machine
  - `hooks.json` in the Codex home (`$CODEX_HOME`, or `~/.codex`)
  - `~/.cursor/hooks.json`
  - `~/.pi/agent/settings.json`

Each Sediment hook command ends in `|| true`, so a Sediment failure doesn't fail
a commit or a push. Leave any failure that Sediment logs for the developer.

Sediment appends its block as the last command of an existing hook. If the
repository's own `pre-push` or `prepare-commit-msg` check relies on the exit
status of its last command, that check no longer stops the push or commit. Read
the hook output. Don't treat a zero exit status as a pass.

## Keep credentials out of your output

Don't read, print, or paste these. They hold credentials:

- `~/.sediment/config.json`
- `~/.sediment/env.sh` and `~/.config/fish/conf.d/sediment.fish`
- The Codex `*.config.toml` profiles that `sediment install --codex-profile`
  writes
- `server.env` in `~/.sediment/server/`, or in the directory passed to
  `sediment server --root`, if the developer runs a local Sediment server
- Lines in the developer's shell profiles or service environment that set
  `SEDIMENT_INGEST_TOKEN` outside Sediment's marked block

Your own environment can hold the same credentials, because the developer's
shell loads `~/.sediment/env.sh`. If you print environment variables, leave out
`OTEL_EXPORTER_OTLP_HEADERS` and `SEDIMENT_INGEST_TOKEN`. If the operator
routes model calls through a Sediment gateway, your environment also holds a
gateway credential, such as `ANTHROPIC_AUTH_TOKEN`. Leave that out too.

## Check capture

From the repository, run:

```bash
sediment doctor .
```

It checks the agent hooks, the repository's git hooks and notes ref, and the
capture endpoint. It contacts the Sediment server and the `origin` remote. It
changes nothing unless you pass `--fetch`, which updates one notes tracking ref.

Read the result this way:

- The command exits 1 only when a line says `FAIL`. `info` lines don't change
  the exit status.
- It checks configuration, not delivery. A clean run doesn't prove that evidence
  reached the server.
- It checks the environment and network that it runs in. If your sandbox blocks
  the network, the server check fails as unreachable. If your shell didn't load
  `~/.sediment/env.sh`, the capture endpoint shows as unset. Neither result
  means that capture is broken for the developer.
- Report `FAIL` lines to the developer. Unless the developer asks you to, don't
  run the fix that a line names, such as `sediment install`,
  `sediment repair-notes`, `sediment stamp`, or `sediment login`. Those commands
  rewrite agent and shell configuration, store credentials, or push to a remote.

If the developer logged in with an operator credential, you can run
`sediment commit <full commit SHA>` to see what the server has for one commit:
the Sessions that its git note names, any Inference calls attributed to it, and
its CI outcomes. The server learns about a commit only after the push, including
`refs/notes/sediment`, reaches it through the forge. Until then, the command
prints `no attributions`. That result alone isn't a capture failure.

## Undo capture only when the developer asks

Unless the developer asks you to, don't run `sediment uninstall`,
`sediment logout`, `sediment quarantine`, `sediment release`, or
`sediment quarantine-inference-calls`.

- `sediment uninstall <repo>` removes the repository's hook blocks and
  Sediment's `notes.rewriteRef` value. Confirm that its output includes
  `removed git hooks from`. A silent exit means that the path isn't a git
  repository.
- `sediment uninstall <repo> --agents` also turns capture off for every
  repository on this machine. It removes the user-level agent hook entries,
  including the pi extension, the environment files, the shell profile blocks,
  and managed Codex telemetry. If it exits with a nonzero status, report the
  file that it skipped to the developer instead of editing that file.
- `sediment logout` removes the stored CLI credentials. It doesn't stop
  capture. The environment files keep their copy of the ingest token, and the
  hooks keep running.
- Neither `uninstall` form removes `refs/notes/sediment`, notes already pushed
  to a remote, or `~/.sediment/`.

After an uninstall:

- If `auto_install_remotes` in Sediment's `config.json` covers the repository's
  `origin`, the next agent Session there reinstalls the git hooks. The
  developer has to remove that entry first.
- Running agents, including you, keep the environment that they started with.
  Tell the developer to restart them.

[Uninstall capture](https://docs.sediment.so/capture/local-capture#uninstall-capture)
has the details.

## What the installer changed

`sediment install <repo>` makes these changes.
[What the installer changes](https://docs.sediment.so/capture/local-capture#what-the-installer-changes)
lists the agent and git hooks.
[Install capture](https://docs.sediment.so/capture/local-capture#install-capture)
covers the environment files.

- **The repository's git hooks.** The `post-commit` block writes a git note
  under `refs/notes/sediment` on the next commit in the working tree after an
  agent Session used a hooked tool there. The note names every such Session,
  whether or not the commit contains that Session's changes. The `pre-push`
  block fetches and pushes `refs/notes/sediment` to the remote that you push
  to, before your push goes out, even for `git push --dry-run`.
  `notes.rewriteRef` carries each note through amend and rebase.
- **Agent hooks, for each agent that it finds, unless the developer passes
  `--no-agents`.** These hooks apply in every repository that you work in.
  Claude Code and Codex entries run `sediment mark`, which records the Session
  in the git directory of your working directory. For Claude Code, a hooked
  tool is any Edit, MultiEdit, Write, NotebookEdit, or Bash call. For Codex,
  it's any tool call. Cursor entries run `sediment cursor-hook`. pi loads Sediment's
  bundled extension, registered in `~/.pi/agent/settings.json`. Codex runs its
  hooks only after the developer trusts them with `/hooks`.
- **Capture configuration.** `~/.sediment/env.sh` and
  `~/.config/fish/conf.d/sediment.fish` set the telemetry endpoint and its
  credential. A marked block in the shell profiles loads `env.sh`. An agent
  picks these up when it starts from a shell that loaded `env.sh`. Cursor's
  hooks read `env.sh` directly. The developer skips these files with `--no-env`
  or `--no-agents`.
- **Opt-in hooks.** `--transcripts` adds Session-end transcript hooks and a
  `PreToolUse` snapshot hook. `--codex-profile` writes a Codex telemetry
  profile.

## What leaves the machine

[Privacy boundaries and ceilings](https://docs.sediment.so/capture/how-capture-works#privacy-boundaries-and-ceilings)
has more detail. In summary:

- **The git notes ref.** The `pre-push` block pushes the whole ref to every
  remote that you push to, including forks and third-party remotes. The ref
  holds a note for every commit stamped in this clone, so it can reveal commit
  SHAs and Session ids from branches that you didn't push to that remote. For
  each Session, a note contains only the agent name, the Session id, and a
  timestamp. Sediment's test suite enforces that contract. A note contains no
  prompt text, diff content, file paths, model names, or hostnames.
- **Agent telemetry.** Each agent exports its OpenTelemetry log events to the
  Sediment server. The server stores the tool-decision events as Developer
  decisions and keeps each whole record.
  - For Claude Code, the installer turns on tool details. The records carry
    each tool call's input, including file paths, Bash commands, and Edit and
    Write text, which can be truncated. They also carry your Claude account
    email when Claude Code is logged in to a Claude account.
  - Codex decisions can carry patch code and other tool arguments. Sediment
    removes the Codex account email and account id before it stores them.
  - Cursor sends one accept decision per applied Write, with the Session id,
    the tool call id, the tool name, and the user id when the developer set
    one.
- **Transcript capture**, if the developer enabled it, sends the edit text that
  an agent applied and the Session-end content of each edited file. For Claude
  Code, it also sends the text of edits that the developer refused and counts
  of lines that something other than the agent changed.
- **A Sediment gateway**, if the operator routes model calls through one,
  captures full prompts and responses.
