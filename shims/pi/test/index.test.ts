// SPDX-License-Identifier: MIT

import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import {
  chmodSync,
  cpSync,
  mkdtempSync,
  mkdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import sedimentPi from "../index.ts";

test("the source-installed extension captures Session headers without node_modules", () => {
  const root = mkdtempSync(join(tmpdir(), "sediment-pi-source-"));
  try {
    cpSync(new URL("../index.ts", import.meta.url), join(root, "index.ts"));
    cpSync(new URL("../lib", import.meta.url), join(root, "lib"), { recursive: true });
    writeFileSync(join(root, "package.json"), '{"type":"module"}');
    const output = execFileSync(process.execPath, ["--input-type=module", "-e", `
      import sedimentPi from "./index.ts";
      const handlers = new Map();
      sedimentPi({ on: (name, handler) => handlers.set(name, handler) });
      const event = { headers: { Authorization: "retained" } };
      handlers.get("before_provider_headers")(event, {
        model: { provider: "sediment", api: "anthropic-messages" },
        sessionManager: { getSessionId: () => "session-without-sdk" },
      });
      console.log(JSON.stringify(event.headers));
    `], {
      cwd: root,
      env: { ...process.env, SEDIMENT_DELIVERY_DIR: "", SEDIMENT_PROVIDER_ID: "sediment",
        SEDIMENT_PROVIDER_API: "anthropic-messages" },
      encoding: "utf8",
    });
    assert.deepEqual(JSON.parse(output), {
      Authorization: "retained",
      "x-sediment-session": "session-without-sdk",
    });
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("the extension declines unsupported Node runtimes before registering capture", () => {
  const version = Object.getOwnPropertyDescriptor(process.versions, "node")!;
  const originalError = console.error;
  const errors: string[] = [];
  console.error = (message: string) => { errors.push(message); };
  try {
    for (const node of ["20.19.0", "22.17.0", "23.9.0", "25.9.0", "26.4.0", "24.0.0-nightly"]) {
      Object.defineProperty(process.versions, "node", { ...version, value: node });
      const events: string[] = [];
      sedimentPi({ on: (event: string) => { events.push(event); } });
      assert.deepEqual(events, [], `Node ${node} must not enroll capture`);
    }
    assert.equal(errors.length, 6);
    assert.ok(errors.every((message) => message.includes("unsupported Node")));
  } finally {
    Object.defineProperty(process.versions, "node", version);
    console.error = originalError;
  }
});

test("authenticated decision capture rejects redirects", async () => {
  const previousEndpoint = process.env.SEDIMENT_OTLP_ENDPOINT;
  const previousToken = process.env.SEDIMENT_INGEST_TOKEN;
  const originalFetch = globalThis.fetch;
  const requests: RequestInit[] = [];
  process.env.SEDIMENT_OTLP_ENDPOINT = "https://ingest.example.com";
  process.env.SEDIMENT_INGEST_TOKEN = "secret-token";
  globalThis.fetch = (async (_url: string | URL | Request, init?: RequestInit) => {
    requests.push(init ?? {});
    return new Response("", { status: 200 });
  }) as typeof fetch;
  try {
    const handlers = new Map<string, (event: any, ctx: any) => unknown>();
    sedimentPi({
      on(event: string, handler: (event: any, ctx: any) => unknown) {
        handlers.set(event, handler);
      },
    } as never);
    const event = {
      toolCallId: "call-redirect",
      toolName: "write",
      args: { path: "/repo/app.py" },
      isError: false,
    };
    const ctx = {
      cwd: "/repo",
      sessionManager: {
        getSessionId: () => "sess-redirect",
        getSessionFile: () => "/repo/session.jsonl",
      },
    };
    await handlers.get("tool_execution_start")!(event, ctx);
    await handlers.get("tool_execution_end")!(event, ctx);
    assert.equal(requests.length, 1);
    assert.equal(requests[0]?.redirect, "error");
  } finally {
    globalThis.fetch = originalFetch;
    if (previousEndpoint === undefined) delete process.env.SEDIMENT_OTLP_ENDPOINT;
    else process.env.SEDIMENT_OTLP_ENDPOINT = previousEndpoint;
    if (previousToken === undefined) delete process.env.SEDIMENT_INGEST_TOKEN;
    else process.env.SEDIMENT_INGEST_TOKEN = previousToken;
  }
});

test("installed sediment command drives attribution and transcripts", async () => {
  const root = mkdtempSync(join(tmpdir(), "sediment-pi-installed-"));
  const bin = join(root, "bin");
  const emptyScripts = join(root, "scripts");
  const calls = join(root, "calls.txt");
  mkdirSync(bin);
  mkdirSync(emptyScripts);
  const sediment = join(bin, "sediment");
  writeFileSync(
    sediment,
    '#!/bin/sh\nprintf "%s\\n" "$@" >> "$SEDIMENT_TEST_CALLS"\n/bin/cat >/dev/null\n',
  );
  chmodSync(sediment, 0o755);

  const previous = {
    PATH: process.env.PATH,
    SEDIMENT_SCRIPT_DIR: process.env.SEDIMENT_SCRIPT_DIR,
    SEDIMENT_TEST_CALLS: process.env.SEDIMENT_TEST_CALLS,
    SEDIMENT_OTLP_ENDPOINT: process.env.SEDIMENT_OTLP_ENDPOINT,
    SEDIMENT_PI_TRANSCRIPTS: process.env.SEDIMENT_PI_TRANSCRIPTS,
  };
  process.env.PATH = bin;
  process.env.SEDIMENT_SCRIPT_DIR = emptyScripts;
  process.env.SEDIMENT_TEST_CALLS = calls;
  process.env.SEDIMENT_OTLP_ENDPOINT = "";
  process.env.SEDIMENT_PI_TRANSCRIPTS = "1";
  try {
    const handlers = new Map<string, (event: any, ctx: any) => unknown>();
    sedimentPi({
      on(event: string, handler: (event: any, ctx: any) => unknown) {
        handlers.set(event, handler);
      },
    } as never);
    const ctx = {
      cwd: root,
      sessionManager: {
        getSessionId: () => "sess-installed",
        getSessionFile: () => join(root, "session.jsonl"),
      },
    };
    const event = {
      toolCallId: "call-installed",
      toolName: "write",
      args: { path: join(root, "app.py") },
      isError: false,
    };
    await handlers.get("tool_execution_start")!(event, ctx);
    await handlers.get("tool_execution_end")!(event, ctx);
    await handlers.get("session_shutdown")!({ reason: "quit" }, ctx);

    assert.deepEqual(readFileSync(calls, "utf-8").split(/\n/).filter(Boolean), [
      "mark",
      "--tool",
      "pi",
      "transcript",
      "--agent",
      "pi",
    ]);
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
    rmSync(root, { recursive: true, force: true });
  }
});

test("source-installed retrieval registers without node_modules and preserves capture handlers", () => {
  const root = mkdtempSync(join(tmpdir(), "sediment-pi-retrieval-source-"));
  try {
    cpSync(new URL("../index.ts", import.meta.url), join(root, "index.ts"));
    cpSync(new URL("../lib", import.meta.url), join(root, "lib"), { recursive: true });
    writeFileSync(join(root, "package.json"), '{"type":"module"}');
    for (const token of ["restricted-token", ""]) {
      const output = execFileSync(process.execPath, ["--input-type=module", "-e", `
        import sedimentPi from "./index.ts";
        const handlers = new Map();
        const tools = [];
        sedimentPi({ on: (name, handler) => handlers.set(name, handler), registerTool: (tool) => tools.push(tool.name) });
        console.log(JSON.stringify({ tools, captures: handlers.has("tool_execution_end") && handlers.has("session_shutdown") && handlers.has("before_provider_headers") }));
      `], {
        cwd: root, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"],
        env: { ...process.env, SEDIMENT_RETRIEVAL_ENDPOINT: "https://retrieval.example.com", SEDIMENT_RETRIEVAL_TOKEN: token },
      });
      assert.deepEqual(JSON.parse(output), { tools: token ? ["sediment_list_context_sessions", "sediment_evidence_inventory", "sediment_evidence_manifest", "sediment_read_evidence", "sediment_retrieve_context"] : [], captures: true });
    }
  } finally { rmSync(root, { recursive: true, force: true }); }
});
