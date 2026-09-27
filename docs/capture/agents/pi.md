# Capture pi work

Use this guide to capture pi Developer decisions, commit Attribution,
inference calls, and optional Edit observations on one developer machine.

## Evidence captured

| Evidence | Support | Source or limit |
| --- | --- | --- |
| Commit Attribution | Yes | The bundled extension marks the Session after edits. |
| Developer decisions | Implicit | A successful `edit` or `write` emits one implicit accept. Stock pi has no explicit approval gesture. |
| Inference calls | Optional | pi can route an Anthropic-compatible provider through a gateway. |
| Edit observations | Optional | The extension extracts successful edits at `session_shutdown`. |
| Rejected edits and Retry linkages | No | The extractor omits failed edits. |
| External line counts | No | pi has no pre-edit snapshot hook. |

## Before you begin

You need the following:

- Sediment 0.3.0 or later. Earlier releases don't bundle the pi extension.
- Node 24 and pi 0.84.1:

  ```bash
  npm install --global @earendil-works/pi-coding-agent@0.84.1
  ```

- A capture login, a git repository, and the other steps in
  [Configure local capture](../local-capture.md).

Start pi once to create `~/.pi/agent`, sign in to your model, and then close
pi.

## Install capture

1. Connect the CLI to the Sediment endpoint:

   ```bash
   sediment login https://sediment-api.example.com --capture
   ```

2. Install capture in the repository. The installer registers the extension in
   `~/.pi/agent/settings.json`:

   ```bash
   sediment install --user-id alice /path/to/repo
   ```

   Replace `alice` with your developer identifier.

3. Load the environment, and start pi in the repository:

   ```bash
   . "$HOME/.sediment/env.sh"
   cd /path/to/repo
   pi
   ```

If the extension is missing or unregistered, `sediment doctor` reports `FAIL`.
After you move the CLI, rerun `sediment install` and remove the stale extension
path from `~/.pi/agent/settings.json`.

## Configure Edit observations

Edit observations send applied text and observed file text. If participants
approve that, rerun the installer with `--transcripts` and the same other
flags:

```bash
sediment install --user-id alice --transcripts /path/to/repo
. "$HOME/.sediment/env.sh"
```

This sets `SEDIMENT_PI_TRANSCRIPTS=1`, and the extension extracts edits at
`session_shutdown`. Any other value disables extraction. A later reinstall keeps
the opt-in. If you manage the environment yourself with `--no-env`, set the
endpoint, token, and `SEDIMENT_PI_TRANSCRIPTS=1` there.

If a one-task host keeps pi running, set `SEDIMENT_EXTRACT_ON_SETTLE=1` so that
extraction runs at `agent_settled`. Leave it unset for interactive Sessions.
Repeated settling keeps the earliest file state, because the first write wins.

Keep transcripts at their original paths. pi resolves relative edits from the
transcript's working directory. The extractor skips an edit with an invalid
directory (`execution_directory_invalid`) or path form (`unsupported_path`).
For a fork, keep the complete parent transcript in the same directory. The
extractor reads one regular parent file of up to 64 MiB, excludes inherited
messages, and counts an unusable parent as `parent_source_unverified`.

## Configure inference-call capture

Set up the gateway to serve your existing model first, with
[Configure inference-call capture](../managed-capture.md#configure-inference-call-capture).
Then configure pi:

1. Merge this provider into `~/.pi/agent/models.json`. Keep your existing
   providers, and replace the URL and model ID:

   ```json
   {
     "providers": {
       "sediment": {
         "baseUrl": "https://sediment-llm.example.com",
         "api": "anthropic-messages",
         "apiKey": "$SEDIMENT_GATEWAY_KEY",
         "models": [{"id": "<existing Anthropic model ID>"}]
       }
     }
   }
   ```

   Keep `$SEDIMENT_GATEWAY_KEY` literally in the file; pi resolves it from the
   environment. Copy the model's capabilities, context, and output limits from
   its existing definition.

2. Load `SEDIMENT_GATEWAY_KEY` into the pi environment from your team's
   credential store.
3. Start pi with the provider:

   ```bash
   pi --provider sediment --model '<existing Anthropic model ID>'
   ```

   In a running Session, open `/model` to reload the file and select the model.

If you register a different provider name or API, set `SEDIMENT_PROVIDER_ID` and
`SEDIMENT_PROVIDER_API` to match it.

## Verify capture

Remote checks need a separate [operator login](../local-capture.md#verify-capture).

1. Ask pi to create a small file with its `write` tool, and end the Session.
2. Commit the file, and read the commit's Session note:

   ```bash
   git notes --ref=refs/notes/sediment show HEAD
   ```

3. Check that Session on the server:

   ```bash
   sediment doctor /path/to/repo --agent pi --session-id '<session_id>'
   ```

   If you opted in to Edit observations, add `--transcripts`. For a
   gateway-routed Session, add `--inference-calls`. A model response alone
   doesn't prove capture.

## Limits

- pi accepts are implicit, so reports don't count them as human approvals.
- The extractor omits failed edits and doesn't emit Rejected edits or Retry
  linkages.
- pi can request evidence from a previous Session through a separate retrieval
  token. See
  [Enable agent-requested retrieval](../../operate/resume-with-evidence.md#enable-agent-requested-retrieval).

[Privacy boundaries and ceilings](../../explanation/how-capture-works.md#privacy-boundaries-and-ceilings)
lists the fields that each capture path sends.
