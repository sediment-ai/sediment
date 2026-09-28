# ADR 0027 — Opt-in request transforms in the model request path

Status: accepted

Amends the capture documentation's statement that Sediment stays out of the
model request path. Applies [ADR 0006](0006-open-core-boundary.md).

## Context

The capture documentation promises that Sediment stays out of the model request
path. Architecture describes a gateway that owns the request path and sends
Sediment a copy of each completed call. [Roll out managed
capture](../capture/managed-capture.md) states that Sediment doesn't serve
model requests.

Experiments on issue #134 measured where an agent's input cost comes from.
Trimming history once, at resumption, moved cost into extra model round trips.
On long Sessions, the conversation that the agent resends on every call
dominated input. Much of that resent input is tool output that later tool calls
have made stale: a file read before an edit of the same file, or a test run
before the same test run again.

Only a layer in the request path can remove that output per request. The
[context-minimal gateway spec](../superpowers/specs/2026-09-28-gateway-context-pruning-design.md)
defines a deterministic rule for it.

## Decision

Decision: Sediment may transform a model request before the model receives it,
through two delivery modes:

- The LiteLLM hook: `async_pre_call_hook` on the capture callback in
  `litellm/sediment_callback.py`. It serves the bundled gateway and any
  existing LiteLLM proxy.
- The standalone pruning proxy: `litellm/sediment_prune_proxy.py`, a stdlib
  HTTP pass-through that a deployment chains in front of any gateway, or that
  an agent calls directly.

Both modes wrap one pure, stdlib-only core, `litellm/sediment_context.py`. The
core applies only the supersession rule that the spec defines. It changes
nothing but the content of superseded tool results, which become one stub line
each.

The following constraints bind every request transform:

- **Opt-in, off by default.** A transform runs only when the operator sets
  `SEDIMENT_CONTEXT_PRUNE=supersede`. Any other value, or no value, leaves the
  request path a pure pass-through.
- **Captured input equals model input.** The hook updates the messages that
  LiteLLM logs, so the Inference call Fact records the pruned request. Behind
  the proxy, the downstream gateway and its capture record the pruned request.
  The agent's own transcript keeps the unpruned conversation.
- **A pure function of the request.** The transform reads no stored state, no
  clock, and no other request. The same request always produces the same model
  input, which keeps the provider's cached prefix stable across turns.
- **Fail-open.** A transform failure logs a structured reason and forwards the
  request unchanged. A request that the transform doesn't recognize passes
  through unchanged.
- **No stored credentials.** Both modes forward the agent's own
  authentication and provider headers. The proxy stores nothing, adds no
  retries, and binds to loopback unless the operator chooses another address.
- **No Fact schema change.** The hook adds a count-only report
  (`policy_version`, stubbed results, and bytes removed) under the
  `sediment_context` key of the payload that the Inference call Fact keeps on
  `raw`. The report is observation of the request, not Derivation output.

The transform is open core under ADR 0006. It serves a single team's
pipeline: removing it makes that team's agents cost more, and a team must be
able to audit exactly what its model received.

## Consequences

- The capture documentation no longer promises that Sediment stays out of the
  request path. It states that Sediment stays out of the request path unless
  the operator enables a request transform.
- `PrunePolicy.policy_version` identifies the rule set. Tuning a threshold or
  changing a rule bumps it, so reports from different rules stay separable.
- A deployment that enables the transform changes the model's input, and so
  can change its output. Evaluations report savings in priced tokens and
  compare task outcomes against pass-through.
- A decision model in the request path, which the spec calls stage B, needs a
  separate ADR.
