# Context-minimal agents at the gateway

Implementation tracker: [Issue #134](https://github.com/sediment-ai/sediment/issues/134).
Evidence: the Phase 2 and Phase 3 designs and results on issue #134.

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

## Build (stage A): deterministic supersession, per request

The contract is the wire format (OpenAI chat and Anthropic Messages), not the
gateway. Stage A has two parts:

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
| pi (evaluation harness) | OpenAI chat: `tool_calls`, role `tool` | `read` | `edit`, `write` | `bash` |
| Claude Code (launch demo) | Anthropic Messages: `tool_use`, `tool_result` | `Read` | `Edit`, `MultiEdit`, `Write` | `Bash` |

An unknown tool is never pruned.

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
- Tool-call pairing is preserved in both formats.
- User and assistant text are never altered.
- Unknown tools pass through untouched.

## Build (stage A.1): standalone pruning proxy

Stage A reaches only LiteLLM users. Many teams run another gateway (Portkey,
Kong, Cloudflare AI Gateway, OpenRouter, an in-house proxy) or none at all, with
the agent calling the provider directly. Stage A.1 reaches them with a small
stdlib HTTP pass-through around the same core:

- It accepts OpenAI chat (`POST /v1/chat/completions`) and Anthropic Messages
  (`POST /v1/messages`) requests, applies `prune`, and forwards them to one
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

The targets are fixed before any run. Every saving is reported in priced tokens:
uncached input, plus cached input at the provider's discount, plus output.

1. **E1, reuse the existing harness (about 2 days).** Run pi through the
   experiment gateway on long multi-step fixture tasks: several files to edit,
   checks rerun after each edit. Arms: passthrough and stage A. Two families × 3
   profiles × 2 repetitions × 2 arms = 24 runs. Targets: priced input cost at
   least 30% lower, and both-check passes no more than one below passthrough.
2. **E2, the public result (about 1 week, plus model spend).** A fixed-seed
   random 50-instance subset of SWE-bench Verified, run with one agent through
   the stage A.1 proxy: pass-through against pruning, with the same model and
   settings. Report resolve rate, priced tokens, cache hits, calls, and latency
   per instance. Targets: priced input at least 30% lower, and resolved
   instances within 2 of passthrough. Publish the harness, subset, and raw
   counts.

If E1 misses its token target, stop before E2 and report it. Stage B starts only
if E1 or E2 shows that non-superseded tool output is still a large share of
input.

## Effort, debt, and risk

| Item | Estimate |
| --- | --- |
| ADR 0027: Sediment may transform requests in the request path (the LiteLLM hook and the proxy), opt-in, with captured input equal to model input | 1 day |
| Stage A core, both format adapters, the LiteLLM hook, and tests | 2–3 days |
| Stage A.1 proxy and pass-through tests | 1–2 days |
| E1 | about 2 days |
| E2 | about 1 week, plus model spend |

Risks:

- **Cache breaks** can cancel the saving. The mitigation is the 4 KB threshold
  and priced-token reporting.
- **Harness message formats change.** Adapters are small, and unknown shapes
  pass through untouched.
- **An agent may re-read a stubbed file.** That's acceptable: the stub states how
  to fetch the current content, and E1 counts the extra calls.
- **The gateway stops being passive capture.** The capture docs promise "Sediment
  stays out of the LLM request path". Both delivery modes must stay opt-in, the
  ADR must amend that statement, and captured input must remain exactly what the
  model received.
- **Provider-native formats aren't covered** (Bedrock and Vertex wrappers) until
  there's demand.
- **Open core.** The transform serves a single team's pipeline, so it's open
  source under ADR 0006.
