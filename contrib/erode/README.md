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
- Output of any tool other than Claude Code's `Read`, `Edit`, `MultiEdit`,
  `Write`, and `Bash`, or pi's `read`, `edit`, `write`, and `bash`, in the
  Anthropic Messages format.
- A request in the OpenAI chat format. Its tool results carry no error flag, so
  a failed edit looks like a successful one and would supersede a read the model
  still needs. The report counts the request as skipped.
- Any field outside tool results, including Anthropic `cache_control`
  breakpoints.
- A request that carries Anthropic `context_management`, which hands context
  editing or compaction to the provider.

erode applies new stubs only when they remove at least 4 KB, and the output is
a pure function of the request. Each request reproduces the stubs of the
request before it, so the provider's cached prefix breaks rarely. An agent's
own compaction request is an ordinary Messages request that nothing in its
wire shape identifies, so erode prunes it like any other request.

Pruning changes the model's input, so it can change the model's output. Compare
billed cost and task outcomes with erode on and off before you rely on it.

## Run the proxy

The proxy prunes `POST /v1/messages` requests, logs `POST /v1/chat/completions`
requests as skipped, and forwards every request to one upstream URL. It forwards the agent's
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
`erode_prune policy_version=3 stubbed_results=3 bytes_removed=48210 skipped=none`.

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
