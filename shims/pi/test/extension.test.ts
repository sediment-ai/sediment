// SPDX-License-Identifier: MIT
// Wiring tests for the pi extension (shims/pi/lib/register.ts): a fake
// ExtensionAPI captures the registered handlers, which then run against
// injected in-memory deps (no network, no processes) — the event→contract
// mapping is the behavior under test.

import assert from "node:assert/strict";
import test from "node:test";

import { register, type Deps } from "../lib/register.ts";

function fakePi() {
  const handlers = new Map<string, (event: any, ctx: any) => unknown>();
  const providers: Array<[string, unknown]> = [];
  return {
    handlers,
    providers,
    on(event: string, handler: (event: any, ctx: any) => unknown) {
      handlers.set(event, handler);
    },
    registerProvider(id: string, definition: unknown) {
      providers.push([id, definition]);
    },
  };
}

function fakeCtx(over: Record<string, unknown> = {}) {
  return {
    cwd: "/repo",
    sessionManager: {
      getSessionId: () => "sess-1",
      getSessionFile: () => "/home/dev/.pi/agent/sessions/--repo--/s.jsonl",
      ...(over.sessionManager as object | undefined),
    },
    ...over,
  };
}

interface Call {
  kind: "post" | "run";
  args: unknown[];
}

function fakeDeps(env: Record<string, string> = {}) {
  const calls: Call[] = [];
  const deps: Deps = {
    post: async (...args: unknown[]) => {
      calls.push({ kind: "post", args });
    },
    runScript: async (...args: unknown[]) => {
      calls.push({ kind: "run", args });
    },
    scripts: { attribution: "/s/sediment_attribution.py", transcript: "/s/sediment_transcript.py" },
    env: { SEDIMENT_OTLP_ENDPOINT: "https://ingest.example.com", ...env },
    now: () => 1782578510649,
  };
  return { calls, deps };
}

test("native provider headers use the request's Session without an SDK", async () => {
  const pi = fakePi();
  const { deps } = fakeDeps();
  register(pi, deps);
  const handler = pi.handlers.get("before_provider_headers");
  assert.ok(handler, "native header hook must be registered synchronously");
  const headers: Record<string, string | null> = {
    Authorization: "Bearer provider-secret",
    "X-Sediment-Session": "stale-session",
    "x-sediment-session": "another-stale-session",
  };
  for (const sessionId of ["sess-first", "sess-second"]) {
    await handler({ type: "before_provider_headers", headers }, fakeCtx({
      model: { provider: "sediment", api: "anthropic-messages" },
      sessionManager: { getSessionId: () => sessionId },
    }));
    assert.deepEqual(headers, {
      Authorization: "Bearer provider-secret",
      "x-sediment-session": sessionId,
    });
  }
  assert.deepEqual(pi.providers, [], "the shim must not replace provider dispatch");
});

test("native header injection is confined to the configured provider and API", async () => {
  const pi = fakePi();
  const { deps } = fakeDeps({
    SEDIMENT_PROVIDER_ID: "partner-gateway",
    SEDIMENT_PROVIDER_API: "openai-completions",
  });
  register(pi, deps);
  const handler = pi.handlers.get("before_provider_headers");
  assert.ok(handler);
  for (const model of [
    undefined,
    { provider: "sediment", api: "openai-completions" },
    { provider: "partner-gateway", api: "anthropic-messages" },
  ]) {
    const headers = { "x-other": "keep" };
    await handler({ type: "before_provider_headers", headers }, fakeCtx({ model }));
    assert.deepEqual(headers, { "x-other": "keep" });
  }
  const headers = { "x-other": "keep" };
  await handler({ type: "before_provider_headers", headers }, fakeCtx({
    model: { provider: "partner-gateway", api: "openai-completions" },
  }));
  assert.deepEqual(headers, { "x-other": "keep", "x-sediment-session": "sess-1" });
});

test("unavailable Session identity removes stale headers and leaves capture independent", async () => {
  const pi = fakePi();
  const { deps, calls } = fakeDeps();
  register(pi, deps);
  const handler = pi.handlers.get("before_provider_headers");
  assert.ok(handler);
  const errors: unknown[][] = [];
  const originalError = console.error;
  console.error = (...args) => { errors.push(args); };
  try {
    for (const sessionId of [undefined, "", " ", "id\r\ninjected: secret", "id\u0000", "id\ud800"]) {
      const headers = { "x-other": "keep", "X-Sediment-Session": "stale" };
      await handler({ type: "before_provider_headers", headers }, fakeCtx({
        model: { provider: "sediment", api: "anthropic-messages" },
        sessionManager: { getSessionId: () => sessionId },
      }));
      assert.deepEqual(headers, { "x-other": "keep" });
    }
    assert.equal(errors.length, 6);
    assert.ok(errors.every((entry) => !JSON.stringify(entry).includes("injected")));
    await runTool(pi, fakeCtx(), { toolName: "write", args: { path: "/repo/app.py" } });
    assert.deepEqual(calls.map((call) => call.kind), ["post", "run"]);
  } finally {
    console.error = originalError;
  }
});

/** One tool call, start through end — the same event both handlers see. */
async function runTool(
  pi: ReturnType<typeof fakePi>,
  ctx: unknown,
  event: { toolName: string; args?: unknown; isError?: boolean },
) {
  const full = { toolCallId: "call-1", isError: false, ...event };
  await pi.handlers.get("tool_execution_start")!(full, ctx);
  await pi.handlers.get("tool_execution_end")!({ ...full, result: {} }, ctx);
}

test("applied edit tool posts a decision and fires the stamper", async () => {
  const pi = fakePi();
  const { calls, deps } = fakeDeps();
  register(pi as never, deps);
  const ctx = fakeCtx();

  await runTool(pi, ctx, {
    toolName: "write",
    args: { path: "/repo/a.py", content: "x" },
  });

  const [post, run] = calls;
  assert.equal(post.kind, "post");
  const [url, payload, token] = post.args as [string, any, string | null];
  assert.equal(url, "https://ingest.example.com/v1/logs");
  assert.equal(token, null);
  const [record] = payload.resourceLogs[0].scopeLogs[0].logRecords;
  assert.deepEqual(record.body, { stringValue: "sediment.tool_decision" });
  assert.equal(record.timeUnixNano, "1782578510649000000");
  const attrs = new Map(record.attributes.map((a: any) => [a.key, a.value]));
  assert.deepEqual(attrs.get("agent"), { stringValue: "pi" });
  assert.deepEqual(attrs.get("session.id"), { stringValue: "sess-1" });
  assert.deepEqual(attrs.get("tool_use_id"), { stringValue: "call-1" });
  assert.deepEqual(attrs.get("decision"), { stringValue: "accept" });
  assert.deepEqual(attrs.get("explicit"), { boolValue: false });
  assert.deepEqual(attrs.get("file_path"), { stringValue: "/repo/a.py" });

  // mark fires with the session marker on stdin (PostToolUse parity).
  assert.equal(run.kind, "run");
  const [script, args, stdin] = run.args as [string, string[], any];
  assert.equal(script, "/s/sediment_attribution.py");
  assert.deepEqual(args, ["mark", "--tool", "pi"]);
  assert.equal(stdin.session_id, "sess-1");
  assert.equal(stdin.cwd, "/repo");
});

test("mark cwd derives from the edited file, not the session cwd", async () => {
  // ACP agents: ctx.cwd is the workspace root (/home/agent), the edit
  // lands in a clone beneath it. mark needs the repo, not the session.
  const pi = fakePi();
  const { calls, deps } = fakeDeps();
  register(pi as never, deps);
  const ctx = fakeCtx({ cwd: "/home/agent" });

  await runTool(pi, ctx, {
    toolName: "write",
    args: { path: "/home/agent/REPOS/sediment/docs/x.md", content: "x" },
  });

  const run = calls.find((c) => c.kind === "run")!;
  const stdin = run.args[2] as any;
  assert.equal(stdin.cwd, "/home/agent/REPOS/sediment/docs");
});

test("mark cwd falls back to ctx.cwd when the edit path is relative or absent", async () => {
  const pi = fakePi();
  const { calls, deps } = fakeDeps();
  register(pi as never, deps);
  const ctx = fakeCtx({ cwd: "/home/agent" });

  await runTool(pi, ctx, {
    toolName: "edit",
    args: { path: "docs/x.md", edits: [] },
    isError: true,
  });

  const stdin = calls[0].args[2] as any;
  assert.equal(stdin.cwd, "/home/agent");
});

test("errored edit executions fire the stamper but post no decision", async () => {
  const pi = fakePi();
  const { calls, deps } = fakeDeps();
  register(pi as never, deps);
  const ctx = fakeCtx();

  await runTool(pi, ctx, {
    toolName: "edit",
    args: { path: "/repo/a.py", edits: [] },
    isError: true,
  });

  assert.equal(calls.length, 1);
  assert.equal(calls[0].kind, "run");
});

test("non-edit tools are ignored entirely", async () => {
  const pi = fakePi();
  const { calls, deps } = fakeDeps();
  register(pi as never, deps);

  await runTool(pi, fakeCtx(), { toolName: "bash", args: { command: "ls" } });
  assert.deepEqual(calls, []);
});

test("no endpoint means not opted in: stamper still fires, nothing posts", async () => {
  const pi = fakePi();
  const { calls, deps } = fakeDeps({ SEDIMENT_OTLP_ENDPOINT: "" });
  register(pi as never, deps);

  await runTool(pi, fakeCtx(), { toolName: "write", args: { path: "/repo/a.py" } });
  assert.deepEqual(calls.map((c) => c.kind), ["run"]);
});

test("rejected configured endpoint reports one credential-safe diagnostic", async () => {
  const pi = fakePi();
  const endpoint = "http://ingest.example.com";
  const token = "secret-token-must-not-leak";
  const { calls, deps } = fakeDeps({
    SEDIMENT_OTLP_ENDPOINT: endpoint,
    SEDIMENT_INGEST_TOKEN: token,
  });
  const diagnostics: string[] = [];
  const originalError = console.error;
  console.error = (...args: unknown[]) => diagnostics.push(args.join(" "));
  try {
    register(pi as never, deps);
    await runTool(pi, fakeCtx(), {
      toolName: "write",
      args: { path: "/repo/a.py" },
    });
    await runTool(pi, fakeCtx(), {
      toolName: "write",
      args: { path: "/repo/b.py" },
    });

    assert.equal(diagnostics.length, 1);
    assert.match(diagnostics[0], /configured ingest endpoint rejected/);
    assert.doesNotMatch(diagnostics[0], new RegExp(endpoint));
    assert.doesNotMatch(diagnostics[0], new RegExp(token));

    deps.env.SEDIMENT_OTLP_ENDPOINT = "https://ingest.example.com";
    await runTool(pi, fakeCtx(), {
      toolName: "write",
      args: { path: "/repo/c.py" },
    });
    assert.equal(calls.filter((call) => call.kind === "post").length, 1);
  } finally {
    console.error = originalError;
  }
});

for (const optIn of [undefined, "0", "false", "1"]) {
  for (const trigger of ["session_shutdown", "agent_settled"]) {
    test(`${trigger} requires transcript opt-in ${String(optIn)} independently of decisions`, async () => {
      const pi = fakePi();
      const { calls, deps } = fakeDeps({ SEDIMENT_EXTRACT_ON_SETTLE: "1" });
      if (optIn !== undefined) deps.env.SEDIMENT_PI_TRANSCRIPTS = optIn;
      register(pi as never, deps);
      let transcriptReads = 0;
      const ctx = fakeCtx({
        sessionManager: {
          getSessionId: () => "sess-1",
          getSessionFile: () => {
            transcriptReads += 1;
            return "/home/dev/.pi/agent/sessions/--repo--/s.jsonl";
          },
        },
      });

      await runTool(pi, ctx, {
        toolName: "write",
        args: { path: "/repo/a.py", content: "private edit text" },
      });
      await pi.handlers.get("session_shutdown")!({ reason: "reload" }, ctx);
      assert.equal(transcriptReads, 0);
      await pi.handlers.get(trigger)!({ reason: "quit" }, ctx);

      const [post, marker, ...transcripts] = calls;
      assert.equal(post.kind, "post");
      assert.equal(marker.args[0], "/s/sediment_attribution.py");
      const payload = post.args[1] as any;
      const [record] = payload.resourceLogs[0].scopeLogs[0].logRecords;
      const attrs = new Map(record.attributes.map((a: any) => [a.key, a.value]));
      assert.deepEqual(attrs.get("decision"), { stringValue: "accept" });
      assert.deepEqual(attrs.get("explicit"), { boolValue: false });
      assert.doesNotMatch(JSON.stringify(payload), /private edit text/);
      assert.equal(transcriptReads, optIn === "1" ? 1 : 0);
      assert.deepEqual(
        transcripts.map((call) => call.args[0]),
        optIn === "1" ? ["/s/sediment_transcript.py"] : [],
      );
    });
  }
}

test("session_shutdown ships opted-in transcript pairs, except on reload", async () => {
  const pi = fakePi();
  const { calls, deps } = fakeDeps({ SEDIMENT_PI_TRANSCRIPTS: "1" });
  register(pi as never, deps);
  const ctx = fakeCtx();

  await pi.handlers.get("session_shutdown")!({ reason: "reload" }, ctx);
  assert.equal(calls.length, 0); // reload keeps the session alive — no pairs

  await pi.handlers.get("session_shutdown")!({ reason: "quit" }, ctx);
  assert.equal(calls.length, 1);
  const [script, args, stdin] = calls[0].args as [string, string[], any];
  assert.equal(script, "/s/sediment_transcript.py");
  assert.deepEqual(args, ["--agent", "pi"]);
  assert.deepEqual(stdin, {
    session_id: "sess-1",
    transcript_path: "/home/dev/.pi/agent/sessions/--repo--/s.jsonl",
  });
});

test("ephemeral sessions (no session file) ship nothing on shutdown", async () => {
  const pi = fakePi();
  const { calls, deps } = fakeDeps({ SEDIMENT_PI_TRANSCRIPTS: "1" });
  register(pi as never, deps);
  const ctx = fakeCtx({
    sessionManager: {
      getSessionId: () => "sess-1",
      getSessionFile: () => undefined,
    },
  });
  await pi.handlers.get("session_shutdown")!({ reason: "quit" }, ctx);
  assert.deepEqual(calls, []);
});

test("missing python scripts degrade quietly: decisions still post", async () => {
  const pi = fakePi();
  const { calls, deps } = fakeDeps({ SEDIMENT_PI_TRANSCRIPTS: "1" });
  deps.scripts = null;
  register(pi as never, deps);
  const ctx = fakeCtx();

  await runTool(pi, ctx, { toolName: "write", args: { path: "/repo/a.py" } });
  await pi.handlers.get("session_shutdown")!({ reason: "quit" }, ctx);
  assert.deepEqual(calls.map((c) => c.kind), ["post"]);
});

test("missing transcript capture preserves attribution", async () => {
  const pi = fakePi();
  const { calls, deps } = fakeDeps({ SEDIMENT_PI_TRANSCRIPTS: "1" });
  deps.scripts = {
    attribution: "/s/sediment_attribution.py",
    transcript: null,
  } as never;
  register(pi as never, deps);

  await runTool(pi, fakeCtx(), {
    toolName: "write",
    args: { path: "/repo/a.py" },
  });
  await pi.handlers.get("session_shutdown")!({ reason: "quit" }, fakeCtx());

  const runs = calls.filter((call) => call.kind === "run");
  assert.deepEqual(runs.map((call) => call.args[0]), ["/s/sediment_attribution.py"]);
});

test("missing attribution preserves transcript capture", async () => {
  const pi = fakePi();
  const { calls, deps } = fakeDeps({ SEDIMENT_PI_TRANSCRIPTS: "1" });
  deps.scripts = {
    attribution: null,
    transcript: "/s/sediment_transcript.py",
  } as never;
  register(pi as never, deps);

  await runTool(pi, fakeCtx(), {
    toolName: "write",
    args: { path: "/repo/a.py" },
  });
  await pi.handlers.get("session_shutdown")!({ reason: "quit" }, fakeCtx());

  const runs = calls.filter((call) => call.kind === "run");
  assert.deepEqual(runs.map((call) => call.args[0]), ["/s/sediment_transcript.py"]);
});

test("a failing post never throws into the harness", async () => {
  const pi = fakePi();
  const { calls, deps } = fakeDeps();
  deps.post = async () => {
    throw new Error("connection refused");
  };
  register(pi as never, deps);

  const diagnostics: string[] = [];
  const originalError = console.error;
  console.error = (...args: unknown[]) => diagnostics.push(args.join(" "));
  try {
    // Must resolve, not reject.
    await runTool(pi, fakeCtx(), { toolName: "write", args: { path: "/repo/a.py" } });
  } finally {
    console.error = originalError;
  }
  assert.deepEqual(diagnostics, [
    "sediment-pi: capture_failed channel=decision reason=post_failed",
  ]);

  // Channels degrade independently: the lost post must not take the
  // attribution mark (the notes/jaccard backstop's input) down with it.
  assert.deepEqual(
    calls.map((c) => c.kind),
    ["run"],
  );
});

test("agent_settled ships pairs only when the host and content capture opt in", async () => {
  // Default (interactive pi): settling is not session end, so extracting
  // there would freeze an early state that first-write-wins makes permanent.
  const plain = fakePi();
  register(plain as never, fakeDeps({ SEDIMENT_PI_TRANSCRIPTS: "1" }).deps);
  assert.equal(plain.handlers.has("agent_settled"), false);

  const pi = fakePi();
  const { calls, deps } = fakeDeps({
    SEDIMENT_EXTRACT_ON_SETTLE: "1",
    SEDIMENT_PI_TRANSCRIPTS: "1",
  });
  register(pi as never, deps);

  await pi.handlers.get("agent_settled")!({}, fakeCtx());
  assert.equal(calls.length, 1);
  const [script, args, stdin] = calls[0].args as [string, string[], any];
  assert.equal(script, "/s/sediment_transcript.py");
  assert.deepEqual(args, ["--agent", "pi"]);
  assert.deepEqual(stdin, {
    session_id: "sess-1",
    transcript_path: "/home/dev/.pi/agent/sessions/--repo--/s.jsonl",
  });
});

test("a settle then a shutdown re-ship identically, so dedup collapses them", async () => {
  const pi = fakePi();
  const { calls, deps } = fakeDeps({
    SEDIMENT_EXTRACT_ON_SETTLE: "1",
    SEDIMENT_PI_TRANSCRIPTS: "1",
  });
  register(pi as never, deps);
  const ctx = fakeCtx();

  await pi.handlers.get("agent_settled")!({}, ctx);
  await pi.handlers.get("session_shutdown")!({ reason: "quit" }, ctx);

  assert.equal(calls.length, 2);
  // Same session and same transcript => the same (session, call_id) keys
  // server-side, which is what first-write-wins collapses.
  assert.deepEqual(calls[0].args, calls[1].args);
});
