# Context-minimal agents at the gateway

Implementation tracker: [Issue #134](https://github.com/sediment-ai/sediment/issues/134).
Evidence: the Phase 2 and Phase 3 designs and results on issue #134.
Status: stage A, A.1, and A.2 are built. E0 failed its gate on the issue #134
recordings; see [E0 result](#e0-result-fails-its-gate). E1 failed its gate on
recorded real Claude Code Sessions, and the stubs it applied would have raised
billed cost; see [E1 result](#e1-result-fails-its-gate). The evaluation stops
before E2.

## Thesis

An agent's cost comes from resending its growing conversation on every model
call, not from the history it starts with. A layer in the request path can
remove tool output that later context has made stale, so per-call input stops
growing. The removal rule is deterministic and needs no model. A decision model
is added only where the deterministic rule leaves a measured gap.

## Evidence so far

- Trimming history once, at resumption, moved the cost rather than removing it.
  With 0.9 KB instead of 15 KB of context, the agent doubled its model round
  trips (Phase 2: 17% saving against full history).
- On longer histories, the resent history dominated. Minimal context used about
  35% of full history's tokens (Phase 3 development run).
- A list of paths and commands didn't stop re-reading (J2P used 1.02 times J2's
  tokens).
- The decision model's measured value is keeping requirements: user and
  assistant statements that change the task. J2 delivered every required rule
  in Phase 2.
- 80% to 90% of coding input was provider cache reads. Any change to an earlier
  message breaks the cached prefix from that point, so a saving must be measured
  in priced tokens, not raw tokens.
- The resent history in these experiments was mostly context the harness
  delivered in user messages, not stale tool output. In E0's long Sessions, user
  messages were 45.8% of input bytes and tool results 17.3%, with a median tool
  result of 165 bytes. So issue #134 shows that resending dominates, but not
  that superseded tool output does; E1 tests that on real Sessions.

## Build (stage A): deterministic supersession, per request

The contract is the wire format (Anthropic Messages), not the gateway. Stage A has two parts:

1. **A pure core** in one stdlib-only module (planned: sediment_context.py in the
   litellm directory). It imports nothing from LiteLLM:

   ```python
   def prune(messages: list[dict], policy: PrunePolicy) -> tuple[list[dict], dict]:
       """Return the messages the model sees and a count-only report."""
   ```

2. **A LiteLLM hook** of about 20 lines around the core: a
   `CustomLogger.async_pre_call_hook` that rewrites `data["messages"]`. It serves
   the bundled gateway and any customer's own LiteLLM, which registers it the way
   it registers the capture callback today: copy one file and add it to
   `litellm_settings.callbacks`.

Both are off by default and enabled per deployment with
`SEDIMENT_CONTEXT_PRUNE=supersede`.

### Rules

A tool result is **superseded** when later messages in the same request make it
out of date:

| Earlier result | Superseded by |
| --- | --- |
| Read of path P | A later read of P, or a later edit or write of P |
| Run of command C | A later run of the identical command C |

A superseded result keeps its message and its tool-call pairing. Its content
becomes one stub line, for example: `[sediment: superseded by step 14 (edit of
src/app.py); read it again if you need the current content]`.

Never changed:

- System, user, and assistant text. J2 showed that requirements live there.
- Tool calls and their arguments.
- The last two turns.
- Results under 512 bytes, which aren't worth breaking the cache for.
- Error results.

### Cache stability

`prune` is a pure function of the request, and supersession only grows as a
conversation grows. So each request reproduces the previous request's stubs, and
the cached prefix breaks only where a new stub appears. To limit breaks, new
stubs are applied only when the newly superseded bytes reach at least 4 KB. That
threshold is also a function of the request alone.

### Harness adapters

The core works on a normalized sequence of (tool kind, target, result position).
Two small adapters map each wire format onto it:

| Harness | Wire format | Read | Edit or write | Run |
| --- | --- | --- | --- | --- |
| pi (evaluation harness) | Anthropic Messages: `tool_use`, `tool_result` | `read` | `edit`, `write` | `bash` |
| Claude Code (launch demo) | Anthropic Messages: `tool_use`, `tool_result` | `Read` | `Edit`, `MultiEdit`, `Write` | `Bash` |

An unknown tool is never pruned. An OpenAI chat request (`tool_calls`, role
`tool`) passes through unpruned, and the report counts it as skipped: its tool
results carry no error flag, so a failed edit is indistinguishable from a
successful one and would supersede a read the model still needs. pi 0.87.1
sets `is_error` on Anthropic Messages tool results, so pi keeps pruning through
its `anthropic-messages` provider.

### Capture

The capture callback already records the messages LiteLLM sends. After the hook
runs, that is the pruned request, which is what the model saw. The hook adds a
count-only report (`policy_version`, stubbed results, bytes removed) to the call's
`raw` envelope, so no Fact schema changes. The agent's own transcript keeps the
unpruned conversation.

### Tests

- Determinism: the same request gives the same output.
- Idempotence: `prune(prune(x)) == prune(x)`.
- Monotonic prefix: appending a turn never changes an earlier stub unless the
  4 KB threshold newly fires.
- Tool-call pairing is preserved.
- OpenAI chat requests pass through unpruned and are counted as skipped.
- User and assistant text are never altered.
- Unknown tools pass through untouched.

## Build (stage A.1): standalone pruning proxy

Stage A reaches only LiteLLM users. Many teams run another gateway (Portkey,
Kong, Cloudflare AI Gateway, OpenRouter, an in-house proxy) or none at all, with
the agent calling the provider directly. Stage A.1 reaches them with a small
stdlib HTTP pass-through around the same core:

- It accepts OpenAI chat (`POST /v1/chat/completions`) and Anthropic Messages
  (`POST /v1/messages`) requests, applies `prune` (which counts OpenAI chat
  requests as skipped), and forwards them to one
  configured upstream URL: the customer's gateway, another gateway, or the
  provider.
- It streams responses byte for byte without buffering, and forwards the agent's
  auth and provider headers unchanged. It stores no credentials and adds no
  retries.
- It binds to loopback by default. A deployment chains it in front of its
  existing gateway, or a developer points an agent at it directly, for example
  `ANTHROPIC_BASE_URL=http://127.0.0.1:8787 claude`.
- Provider fields it doesn't understand, including Anthropic `cache_control`
  breakpoints, pass through untouched. A malformed or unknown request is
  forwarded unchanged, never rejected.
- Capture keeps working: the downstream gateway, and Sediment capture behind it,
  record exactly what the model received.

Tests reuse the core's tests and add pass-through checks: streaming order,
header forwarding, unknown routes forwarded, and an upstream error returned
unchanged.

E2 and the launch demo use the proxy: "point your agent at one URL" works with
Claude Code without LiteLLM or a Sediment deployment.

## Build (stage A.2): OpenAI Responses API and Codex CLI

Codex CLI talks to the OpenAI Responses API (`POST /v1/responses`), not Chat
Completions, so stages A and A.1 don't reach it. Stage A.2 adds a third
wire-format adapter to the same core, and the matching proxy route. It lands
after the stage A and A.1 PR.

### Recorded Codex traffic

Three multi-step Codex CLI 0.158.0 Sessions, recorded through the proxy in
pass-through mode, fix the adapter's shape. The redacted request bodies and
their findings are in `contrib/erode/tests/fixtures/codex/`.

- Every request resends the full history in `input` with `store: false`. No
  request sets `previous_response_id`.
- Top-level `input` items are `additional_tools`, `message` (developer, user,
  and assistant), `reasoning` with `encrypted_content`, `custom_tool_call`, and
  `custom_tool_call_output`. No request contains `function_call`,
  `function_call_output`, `local_shell_call`, or `local_shell_call_output`.
- Every tool call is a `custom_tool_call` named `exec` whose `input` is
  JavaScript. Shell commands appear as
  `text(await tools.exec_command({cmd:"<command>", ...}));` with `cmd` as one
  string. Patches appear as `text(await tools.apply_patch("<patch>"));`. One
  call can bundle several commands and patches.
- A `custom_tool_call_output` carries the same `call_id` and an `output` array
  of `input_text` parts. The first part is a script header (`Script
  completed ...`). Each later part is the result of one `text(...)` statement,
  in order. A command result is a JSON object with `exit_code` and `output`. A
  successful patch result is `{}`.
- Commands carry no `workdir`. The working directory is the `<cwd>` element of
  the user message that holds `<environment_context>`. Reads use relative paths,
  and patches use absolute paths.
- Codex ignored `OPENAI_BASE_URL` in every checked configuration. An explicit
  model provider with `base_url` and `env_key = "OPENAI_API_KEY"` worked. A
  Codex signed in with a ChatGPT account and only `OPENAI_BASE_URL` sent no
  request to the proxy. A ChatGPT sign-in with an explicit provider wasn't
  checked.

### Request shape

The adapter maps `input` items onto the core's normalized sequence:

| Item | Treatment |
| --- | --- |
| `message` (any role), `additional_tools` | Never changed |
| `reasoning`, including `encrypted_content` | Never changed; opaque to the proxy |
| `custom_tool_call` named `exec` | Never changed; its statements identify what each result part was |
| `custom_tool_call_output` | Only its recognized result parts may be stubbed; `call_id`, the header part, and every other part stay unchanged |
| Any other item type, including `function_call`, `function_call_output`, `local_shell_call`, and `local_shell_call_output` | Never changed |

Decision: stage A.2 doesn't recognize the `function_call` or
`local_shell_call` families. No recorded Codex traffic uses them, and a rule
for a shape nobody has recorded would be guessed. A later stage adds them from
a recording of the client that sends them.

A request that sets `previous_response_id` references server-side history
instead of resending it. There is nothing to prune, so it passes through
untouched and the report counts it.

### Recognizing an exec call

The adapter parses an `exec` call's `input` with a strict grammar and never
evaluates it. The whole `input`, ignoring blank lines and leading or trailing
whitespace on each line, must be a sequence of lines, each exactly one of:

- `text(await tools.exec_command({<arguments>}));`
- `text(await tools.apply_patch(<string>));`

`<arguments>` is a comma-separated list of `key:value` pairs. Each key is an
unquoted identifier, and each value is a JSON string, number, `true`, or
`false`. `exec_command` needs a `cmd` string. `<string>` is a JSON string
literal; a JavaScript-only escape such as `\'` or `\x41` fails to parse. Any
other line fails the whole call: variables, `Promise.allSettled`, loops,
template literals, single-quoted strings, or another tool.

A call that fails the grammar is opaque, and none of its result parts are ever
stubbed. An output whose part count isn't one more than the call's statement
count is also opaque. Two of the 18 recorded calls use `Promise.allSettled` and
stay opaque.

### Codex tool semantics

Each statement in a recognized call is one normalized tool call, in statement
order, with the matching output part as its result:

| Statement | Normalized as |
| --- | --- |
| `apply_patch` | Edit or write of each path named by its `*** Add File:`, `*** Update File:`, `*** Delete File:`, or `*** Move to:` lines |
| `exec_command` whose `cmd` is `cat P` or `nl -ba P` | Read of path P |
| `exec_command` whose `cmd` is `head P`, `tail P`, or `sed -n '<range>p' P` | Partial read of P: superseded only by a later edit of P or the identical command, never by another read |
| Any other `exec_command` | Run of command C, compared by its exact `cmd` string and other arguments |

A read form must split, with POSIX shell quoting rules, into exactly the tokens
shown and one path. It must contain none of `|`, `&`, `;`, `<`, `>`, `` ` ``,
`$`, `(`, `)`, `*`, `?`, `[`, or a newline outside the `sed` range. Any other
form, including `cat` with two paths, is an opaque run, never a read.

A read is superseded by a later edit of the same path, by a later identical
statement (same `cmd` and same other arguments, such as `max_output_tokens`),
or, for full reads only, by a later full read of P with the same other
arguments. A run is superseded only by a later identical statement.

Paths are compared lexically. A relative path joins the command's `workdir`
argument when it has one, itself joined to the `<cwd>` of the latest environment
context before the call when relative. Without a `workdir`, a relative path
joins that `<cwd>`. Both paths are then normalized
without touching a filesystem or resolving symlinks. If no `<cwd>` precedes a
call, its relative paths are compared as written, so they never match an
absolute path.

The core's rules for results apply to each part:

- A command result is an error when its part isn't a JSON object with an
  integer `exit_code`, or when a read's `exit_code` isn't 0. A run with a
  nonzero `exit_code`, such as a failing test suite, is an ordinary result.
- A patch counts as an edit, and supersedes anything, only when its result
  part is exactly `{}`.
- A stubbed part keeps its position and type. Only its `text` becomes the stub
  line. The 512-byte floor and the 4 KB threshold count part bytes.

### Proxy

The proxy adds `POST /v1/responses` with the same pass-through guarantees:
streamed server-sent events forwarded byte for byte, headers forwarded, no
stored credentials, and unknown fields untouched. Point Codex at it with an
explicit model provider, because Codex ignores `OPENAI_BASE_URL`:

```sh
codex -c 'model_provider="erode"' \
  -c 'model_providers.erode={name="erode",base_url="http://127.0.0.1:8787/v1",wire_api="responses",env_key="OPENAI_API_KEY"}'
```

Document that this routing needs an API key, and that ChatGPT sign-in routing
is unverified. Don't work around it.

### Tests

The stage A tests apply unchanged: determinism, idempotence, monotonic prefix,
pairing, text untouched, unknown items untouched, and the 4 KB threshold. Stage
A.2 adds:

- recorded Sessions passing through unchanged with pruning off, and pruned
  output that differs only in stubbed part text;
- `previous_response_id` pass-through;
- encrypted reasoning kept byte-identical;
- each recognized read form, and near-miss forms that must stay runs: a pipe, a
  redirect, `&&`, two paths, and a glob;
- grammar failures that make a call opaque: `Promise.allSettled` (recorded), a
  template literal, a single-quoted string, a JavaScript-only escape, and a
  part-count mismatch;
- `apply_patch` path extraction for add, update, delete, and move, and a failed
  patch that supersedes nothing;
- relative reads matched to absolute patch paths through `<cwd>`, and no match
  without one;
- a read with a nonzero `exit_code` never stubbed and never superseding;
- partial reads not superseding one another, and differing `max_output_tokens`
  blocking supersession between reads.

Fixtures are the recorded Codex requests in `contrib/erode/tests/fixtures/codex/`.
Each recorded result is under the 4 KB threshold, so the tests that must stub
use `min_new_bytes=0` or hand-written fixtures. Hand-written fixtures cover only
the near-miss and edge cases in this list that the recordings don't contain,
and their names mark them as hand-written.

## Out of scope for stage A

- **The decision model.** It's stage B, and runs only if stage A leaves a
  measured gap.
- **Cross-Session file identity from Edit observations.** That's the resumption
  case; stage A covers the within-Session case, where the cost grows.
- **Workspace hashing, and side effects of shell commands.** A pruned result is
  always one the conversation itself shows to be superseded, so pruning never
  hides newer information.

## Stage B (only if stage A leaves a gap): decision model

For tool results older than eight turns that aren't superseded, the gateway asks
a self-hosted decision model one batched request per qualifying turn: "Is this
output still needed for the current step?" A confident "no" gets a stub.
Batching applies only when those candidates total at least 8 KB, so the cache
breaks rarely. The model runs in the control plane on operator hardware; with
pinned weights and deterministic scoring, its policy version includes the model
digest. User and assistant text stay out of scope.

## Evaluation

The targets are fixed before any run. E0 and E1 replay recorded requests, so
they report input bytes. E2 runs the agent, so it reports billed cost from the
provider's actual prices.

0. **E0, replay recorded requests (about half a day, no model calls).** Every
   coding request in the issue #134 experiments was recorded in full by the
   evaluation gate: about 1,500 requests across Phases 1–3, kept in the private
   run archive, not in Git. Once `prune`
   exists, run it over those recorded request bodies, in order within each
   Session, and report per Session:
   - input bytes before and after, and bytes removed per request as the
     conversation grows;
   - the positions where new stubs appear, which are where the cached prefix
     would break;
   - how often a stubbed file was read again later in the same Session;
   - that the output is deterministic and preserves tool-call pairing on real
     traffic.

   `prune` counts OpenAI chat requests as skipped. If the recorded requests
   are OpenAI chat, E0 reports the skip count and measures nothing else; record
   pi's `anthropic-messages` traffic instead.

   Gate: if `prune` removes less than 20% of input bytes at the median across
   long Sessions (Phase 3 sources and multi-call continuations), report that
   and stop before E1. The replay estimates the opportunity only; it can't show
   how an agent behaves with pruned input, so it never replaces E1.
1. **E1, replay recorded real Sessions (about 1 day, plus model spend for 3 to
   5 Sessions).** This replaces the original E1, which reran the issue #134
   harness and fixtures: E0 showed that their tool output is too small to prune.
   - **Record.** Run Claude Code on a frontier model, with its default settings,
     on open issues in this repository that need changes across several files,
     one issue per run. This workload is a mid-sized Python repository with
     real history, so Sessions are long and read large files. E2 keeps its
     SWE-bench Verified subset, so E2 stays held out, and E1 says nothing about
     SWE-bench traffic. Route each run through `scripts/erode_record.py`, which
     runs `erode.proxy.make_server(upstream, prune=False)` and saves each
     `POST /v1/messages` body before forwarding it. The recorder changes only
     transport: it drops `Accept-Encoding` so it can read the response. It also
     writes each response's `usage` token counts to `usage.jsonl`, so a later
     replay step can estimate billed cost from cache reads, cache writes, and
     where new stubs would break the cached prefix. Bodies of requests the
     upstream refused are kept apart, because the provider doesn't bill them
     and the agent retries them. Keep the recordings private: they hold prompts
     and repository content, and reports carry counts only.
   - **Group requests into Sessions.** One run through the proxy interleaves
     the main agent's conversation with subagent (`Task`) conversations, small
     side requests, and compaction requests. A Session is one main-agent
     conversation in which each request's `messages` extends the previous
     request's. A compaction starts a new segment of the same Session.
     Subagent conversations and side requests are reported separately and don't
     count toward the gate. A long Session has at least 20 requests. Record
     until at least 3 Sessions are long. None of E0's Sessions reached 20; the
     longest had 16.
   - **Replay.** Run each Session's requests, in order, through `prune` with the
     default `PrunePolicy`. The replay tool reads request bodies from a
     directory given on the command line and writes counts only. It reports
     E0's measures, plus:
     - each Session's input bytes by part: user messages, tool results, tool
       schema, system prompt, and assistant turns;
     - tool-result bytes by tool name. Only `Read`, `Bash`, `Edit`,
       `MultiEdit`, and `Write` are prunable, so results from `Grep`, `Glob`,
       `Task`, and other tools are the share stage B would target;
     - where each tool result drops out of the rules: unrecognized tool, under
       512 bytes, never superseded, superseded but inside the protected turns,
       superseded but held back by the 4 KB threshold, not prunable in shape
       (such as a part that carries `cache_control`), or stubbed;
     - the same replay with no batch threshold, and with no thresholds and no
       protected turns, which tells a strict policy apart from nothing to
       prune;
     - errored superseders: stubs whose superseder has `is_error`, which must
       be 0, and superseders skipped because they errored;
     - prefix continuity: raw prefix breaks, compactions, and breaks of the
       pruned prefix without a new stub.

   Gate: for each long Session, divide the bytes removed, summed over all its
   requests, by the input bytes sent, summed over all its requests. This is the
   cost-relevant measure, because every request resends the prefix; it is also
   the measure E0 used. E1 passes when the median of this ratio across the long
   Sessions is at least 20%. Report every long Session's ratio as well as the
   median, because a median of 3 to 5 values is fragile. Like E0, the replay
   estimates the opportunity only; it can't show how the agent behaves with
   pruned input, so it never replaces E2.
2. **E2, the public result (about 1 week, plus model spend).** A fixed-seed
   random 50-instance subset of SWE-bench Verified, run with Claude Code on a
   frontier model through the stage A.1 proxy. The comparison is against what
   the agent already does by default, and against the provider's own tool-result
   clearing, which is what most users would try first:

   | Arm | Setup |
   | --- | --- |
   | Default | The agent as shipped: provider prompt caching and its own automatic compaction on; the proxy forwards unchanged |
   | Pruned | The same agent and settings, with the proxy applying stage A |
   | Provider clearing | The same agent and settings, with pruning off and the proxy adding Anthropic context editing to each `POST /v1/messages` request: `context_management` with one `clear_tool_uses_20250919` edit at its default settings, and the `context-management-2025-06-27` beta header. The API clears the oldest tool results past its threshold, by age rather than by supersession |

   The same model, settings, subset, and seed apply to all three arms. The
   provider-clearing arm changes only the request the proxy forwards; erode
   leaves a request with `context_management` unpruned, so the arms never
   combine. If the agent already sends `context_management`, report that and
   leave its request as sent. Report per instance:
   - resolved or not;
   - **billed cost**, taken from the provider's usage fields (uncached input,
     cache writes, cache reads, and output), each at the provider's published
     price on the run date, with the price list recorded;
   - calls, compactions, and latency.

   Targets: billed cost at least 30% lower than Default, and resolved instances
   within 2 of Default. Report Pruned against Provider clearing on the same
   measures, with no target: it says whether stage A earns its place next to a
   feature the API already offers. Publish the harness, subset, prices, and raw
   counts. A
   plain pass-through run without compaction is a secondary diagnostic, not the
   comparison.

If E1 misses its gate, stop before E2 and report it. Stage B starts only if
E1 or E2 shows that non-superseded tool output is still a large share of
input.

### E0 result: fails its gate

E0 ran on 2026-09-29 over the 1,466 recorded coding requests; no model calls
were made. Issue #134 records the counts.

- **As specified.** pi sent every recorded request as OpenAI chat
  (`openai-completions`). `prune` on `main` (e6303f6) skipped 1,200 as OpenAI
  chat. The other 266 were each Session's first request, with no tool calls yet.
  It applied no stubs and measured nothing else.
- **Upper bound.** A what-if replay used the core from `6ad9007^`, the last
  revision that pruned OpenAI chat, with the shipped policy. Without an error
  flag, a failed call counts as a superseder, so every figure is an upper bound.
  Across the 127 long Sessions (10 Phase 3 source Sessions and 117
  continuations with at least 2 calls), it removed 0.00% of input bytes at the
  median and at the maximum. It applied no stubs, so there are no cache-break
  positions and no re-read rate. Output was deterministic, and tool-call pairing
  was preserved, in all 1,466 requests.
- **Why.** The median tool result was 165 bytes. Only 23 results of at least 512
  bytes were ever superseded, about 18.8 KB across all long Sessions. With no
  thresholds and no protected turns, the median was 0.36% (maximum 6.58%). Tool
  results were 17.3% of input bytes, so stubbing all of them would still miss
  the 20% gate. User messages, where the harness delivers context, were 45.8%.

Following the gate, the evaluation stopped before the original E1, which
reran the same harness and fixtures. Recording the same
fixtures in the `anthropic-messages` format wouldn't change the result, since the
traffic would look the same. These are small synthetic workspaces of about
18 KB, so the result says nothing about real long Sessions with large file
reads. Measuring the opportunity again needs recordings of such Sessions, which
the revised E1 makes.

### E1 result: fails its gate

E1 recorded five Claude Code Sessions on 2026-10-03, one per open issue in this
repository (#46, #204, #33, #50, and #172), with a frontier model and the
agent's default settings in headless mode. The model spend was $7.82. Every
Session was long (25 to 86 requests), had one segment and no compactions, and
used no subagents. The recordings stay private; the replay reports hold
counts only.

Two defects hid the result on the first replay; both are fixed:

- Claude Code sends `context_management` with one `clear_thinking_20251015`
  edit (`keep: all`) on every request. erode passed any request with
  `context_management` through, so it pruned nothing. erode now passes a
  request through only when an edit clears tool results or compacts, or the
  field has a shape it doesn't recognize (`PrunePolicy.policy_version` `"5"`).
- Claude Code ends every request with a `system`-role message that the next
  request doesn't carry, so no request extended the previous one. The replay
  now drops trailing `system` messages before grouping and reports them: 223
  messages, 82 KB in total.

After the fixes, the replay was deterministic, preserved tool-call pairing, and
had no stub regressions or unexplained prefix breaks in all 223 requests.

| Session | Requests | Removed / sent | No thresholds, no protected turns | Cache breaks |
| --- | --- | --- | --- | --- |
| #204 | 39 | 23.5% | 23.6% | 2 (requests 10, 15) |
| #33 | 38 | 8.7% | 8.7% | 1 (request 12) |
| #46 | 86 | 0.16% | 0.47% | 1 (request 78) |
| #172 | 25 | 0% | 0.30% | 0 |
| #50 | 35 | 0% | 0% | 0 |

The median is 0.16% against the 20% gate. Turning the thresholds off barely
moves it, so the policy isn't too strict: the Sessions rarely supersede their
own tool output.

- **Why so little.** Tool results were 31% to 43% of input bytes, and tool
  schemas another 21% to 36%. In #46, #50, and #172, `Bash` produced 95% to 98%
  of tool-result bytes: test and command runs whose commands differ from run
  to run, so no later call supersedes them. In each final request, most
  tool-result bytes were never superseded (159 KB of 183 KB in #46). #204 is the
  exception: `Read` produced most of its bytes, and the agent re-read files.
  No stubbed target was read again in any Session.
- **Billed cost.** In every Session, the responses' `usage` counts at the
  provider's published prices reproduce the agent's reported cost to the cent.
  Almost all input was cache reads; uncached input was 2 tokens per request.
  Cache reads were 37% to 56% of each Session's cost, cache writes 22% to 32%,
  and output 22% to 36%. A stub
  rewrites every cached token after it at the cache-write price, 25 times the
  read price, so a stub saves money only when the tokens it removes, times the
  requests left, exceed about 24 times the tokens it rewrites. An estimate at 4
  bytes per token puts every Session with stubs at a net loss: #204 saves about
  $0.10 of cache reads and adds about $0.23 of rewrites, #33 adds about $0.14
  net, and #46 about $0.21 net.
- **The ceiling.** Stubbing every tool result with no rewrite cost would save at
  most about a quarter of billed cost, because tool results are at most 43% of
  input bytes and cache reads at most 56% of cost.

Following the gate, the evaluation stops before E2. These Sessions are short
for agent work, so the result doesn't cover Sessions of several hundred
requests, where one batched rewrite can buy many cheaper reads. That is the
case for age-based clearing in rare large batches, such as `clear_tool_uses`
with its trigger settings, not for supersession. Claude Code already sends
`context_management`, so an E2 provider-clearing arm would add its edit to the
agent's list rather than set the field.

## Effort, debt, and risk

| Item | Estimate |
| --- | --- |
| ADR 0028: Sediment may transform requests in the request path (the LiteLLM hook and the proxy), opt-in, with captured input equal to model input | 1 day |
| Stage A core, both format adapters, the LiteLLM hook, and tests | 2–3 days |
| Stage A.1 proxy and pass-through tests | 1–2 days |
| Stage A.2 Responses API adapter, proxy route, Codex traffic recording, and tests | 2–3 days |
| E1 | about 1 day, plus model spend for 3 to 5 recorded Sessions |
| E2 | about 1 week, plus model spend |

Risks:

- **Cache breaks** can cancel the saving. The mitigation is the 4 KB threshold
  and priced-token reporting.
- **Harness message formats change.** Adapters are small, and unknown shapes
  pass through untouched.
- **An agent may re-read a stubbed file.** That's acceptable: the stub states how
  to fetch the current content, and E2 counts the extra calls.
- **The gateway stops being passive capture.** The capture docs promise "Sediment
  stays out of the LLM request path". Both delivery modes must stay opt-in, the
  ADR must amend that statement, and captured input must remain exactly what the
  model received.
- **Provider-native formats aren't covered** (Bedrock and Vertex wrappers) until
  there's demand.
- **Codex reads files through shell commands inside JavaScript.** Recognizing
  reads from command text is heuristic. The adapter parses one strict statement
  grammar and a short list of single-file read forms. Any other call is opaque
  and any other command is a run, so a missed read costs savings, never
  correctness.
- **Codex's wire format is version-specific.** Codex CLI 0.158.0 wraps every
  tool call in a JavaScript `exec` call. A later version can change that shape,
  and the grammar then fails closed: the adapter stubs nothing. Record each
  supported Codex version before claiming savings for it.
- **Open core.** The transform serves a single team's pipeline, so it's open
  source under ADR 0006.
