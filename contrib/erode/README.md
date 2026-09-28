# erode

erode wears stale tool output out of an AI coding agent's model requests.

An agent resends its whole conversation on every model call. In a long
session, much of that input is tool output that later tool calls have made
out of date: a file read before an edit of the same file, or a test run
before the same test run again. erode replaces each such result with one stub
line before the request reaches the model. The rule is deterministic, needs no
model, and uses only the Python standard library.

erode is MIT-licensed. It lives in the
[Sediment](https://github.com/sediment-ai/sediment) repository until it moves
to its own repository, `sediment-ai/erode`.

## What erode changes

A tool result is superseded when a later tool call in the same request makes it
out of date:

| Earlier result | Superseded by |
| --- | --- |
| Read of a file | A later read of the whole file or of the same range, or a later edit or write of the file |
| Run of a command | A later run of the identical command |

A superseded result keeps its message and its tool-call pairing. Its content
becomes one stub line, for example:

```text
[erode: superseded by step 14 (edit of src/app.py); read it again if you need the current content]
```

erode never changes the following parts of a request:

- System, user, and assistant text.
- Tool calls and their arguments.
- Results in the last two turns.
- Results under 512 bytes, and error results.
- Output of any tool other than pi's `read`, `edit`, `write`, and `bash` (OpenAI
  chat format), Claude Code's `Read`, `Edit`, `MultiEdit`, `Write`, and `Bash`
  (Anthropic Messages format), or the Codex CLI `exec` calls described in
  [Use erode with Codex CLI](#use-erode-with-codex-cli) (OpenAI Responses
  format).
- Any field outside tool results, including Anthropic `cache_control`
  breakpoints.
- A request that carries Anthropic `context_management`, which hands context
  editing or compaction to the provider.
- A Responses request that sets `previous_response_id`, which keeps its history
  on the server. The report counts it as skipped.

erode applies new stubs only when they remove at least 4 KB, and the output is
a pure function of the request. Each request reproduces the stubs of the
request before it, so the provider's cached prefix breaks rarely. An agent's
own compaction request is an ordinary Messages request that nothing in its
wire shape identifies, so erode prunes it like any other request.

Pruning changes the model's input, so it can change the model's output. Compare
billed cost and task outcomes with erode on and off before you rely on it.

## Run the proxy

The proxy prunes `POST /v1/chat/completions`, `POST /v1/messages`, and
`POST /v1/responses` requests and forwards every request to one upstream URL. It forwards the agent's
headers, including its credentials, unchanged. It stores nothing, adds no
retries, and streams each response as it arrives.

1. Install erode with Python 3.12 or later:

   ```bash
   pip install ./contrib/erode
   ```

2. Start the proxy in front of your provider or gateway:

   ```bash
   erode proxy --upstream https://api.anthropic.com
   ```

   The proxy listens on `127.0.0.1:8787`. To change the address, pass `--host`
   and `--port`.
3. Point the agent at the proxy:

   ```bash
   ANTHROPIC_BASE_URL=http://127.0.0.1:8787 claude
   ```

The proxy logs a count-only report for each chat request:
`erode_prune policy_version=3 stubbed_results=3 bytes_removed=48210`.

| Setting | Flag | Environment variable | Default |
| --- | --- | --- | --- |
| Upstream URL | `--upstream` | `ERODE_UPSTREAM` | None; required |
| Bind address | `--host` | None | `127.0.0.1` |
| Bind port | `--port` | None | `8787` |
| Mode | `--mode` | `ERODE_MODE` | `supersede` |

With `--mode off`, the proxy forwards every request unchanged, which gives you
a pass-through baseline to compare against.

The proxy has no authentication of its own and relays whatever credentials an
agent sends. If you bind it to an address other than loopback, restrict who can
reach that address.

## Use erode with Codex CLI

Codex CLI 0.158.0 sends every tool call as a Responses `custom_tool_call` named
`exec`, whose `input` is JavaScript that calls `tools.exec_command` and
`tools.apply_patch`. erode parses that JavaScript with a strict line grammar
and never runs it:

- Each line must be exactly `text(await tools.exec_command({...}));` or
  `text(await tools.apply_patch("..."));`, with JSON string, number, or boolean
  values. Any other line makes the whole call opaque, and erode never stubs its
  output.
- `cat P` and `nl -ba P` are full reads of P. `head P`, `tail P`, and
  `sed -n '<range>p' P` are partial reads, which only an edit of P or the
  identical command supersedes. A form with a pipe, a redirect, `&&`, two paths,
  a glob, or a variable is an ordinary command run.
- Each path an `apply_patch` names in an `*** Add File:`, `*** Update File:`,
  `*** Delete File:`, or `*** Move to:` line is an edit. A patch counts only when
  its result is `{}`.
- Relative paths join the command's `workdir` when it has one, and otherwise the
  working directory from Codex's environment context. erode compares paths as
  text and never touches the filesystem.
- erode stubs individual results inside a bundled output and leaves the rest of
  the output unchanged.

Codex ignores `OPENAI_BASE_URL`, so point it at the proxy with an explicit model
provider:

```bash
erode proxy --upstream https://api.openai.com
codex -c 'model_provider="erode"' \
  -c 'model_providers.erode={name="erode",base_url="http://127.0.0.1:8787/v1",wire_api="responses",env_key="OPENAI_API_KEY"}'
```

This routing needs an OpenAI API key in `OPENAI_API_KEY`. Routing a Codex that
is signed in with a ChatGPT account through erode isn't verified. A Codex
version that changes the `exec` shape fails closed: erode forwards its requests
without stubs.

## Use the LiteLLM hook

If you run a LiteLLM proxy, erode can prune inside it instead:

1. Install erode into the LiteLLM proxy's Python environment.
2. Register the hook in the proxy's `config.yaml`:

   ```yaml
   litellm_settings:
     callbacks: erode.litellm_hook.handler
   ```

3. Restart the proxy.

The hook also updates the messages that LiteLLM logs, so logging callbacks
record exactly what the model received. To turn the hook off without editing the
configuration, set `ERODE_MODE=off` in the proxy's environment.

## Use erode from Python

```python
from erode import PrunePolicy, prune_request

pruned, report = prune_request(request_body, PrunePolicy())
```

`report` is `None` when the request isn't prunable. Otherwise it holds
`policy_version`, `stubbed_results`, and `bytes_removed`. `prune_request` never
changes its input.

## Develop

Run the tests from this directory:

```bash
python -m pytest tests
```
