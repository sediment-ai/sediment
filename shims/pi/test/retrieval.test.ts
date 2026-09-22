// SPDX-License-Identifier: MIT
import assert from "node:assert/strict";
import { createServer, type Server, type RequestListener } from "node:http";
import test from "node:test";
import type { ToolDefinition } from "@earendil-works/pi-coding-agent";
import { registerRetrieval } from "../lib/retrieval.ts";

const TOKEN = "test-retrieval-credential";
const skipped = { reasoning_part: 0, non_finite_number: 0, no_match: 0, repeated_content: 0, item_limit: 0, response_budget: 0 };
function responseBody() {
  return {
    schema_version: 1, policy_version: 1, source_session_id: "source-session", quarantine_revision: 0,
    status: "matched", capture_completeness: "unknown",
    coverage: { visible_inference_calls: 1, quarantined_inference_calls: 0, scanned_parts: 1, complete_visible_scan: true },
    skipped: { ...skipped }, items: [{ score: 1, evidence: {
      reference: { inference_call_id: "call-1", side: "input", message_index: 0, part_index: 0 },
      observed_at: "2026-09-21T12:00:00+00:00", role: "tool", finish_reason: null,
      part: { type: "tool_call_response", id: "tool-1", result: { precise: "REPLACE_INTEGER", text: "surrogate\ud800 and NUL\0" } },
    } }],
  };
}
const RAW_RESPONSE = JSON.stringify(responseBody()).replace('"REPLACE_INTEGER"', "9007199254740993");

function tool(env: Record<string, string | undefined>): ToolDefinition | undefined {
  let registered: ToolDefinition | undefined;
  registerRetrieval({ registerTool: (value) => { registered = value as ToolDefinition; } }, env);
  return registered;
}
function enabled(endpoint = "https://retrieval.example.com"): ToolDefinition {
  return tool({ SEDIMENT_RETRIEVAL_ENDPOINT: endpoint, SEDIMENT_RETRIEVAL_TOKEN: TOKEN })!;
}
function invoke(tool: ToolDefinition, args: unknown = { query: "parser failure" }, signal?: AbortSignal) {
  return tool.execute("call", args, signal, () => { throw new Error("unexpected progress"); }, {} as never);
}
async function server(handler: RequestListener): Promise<{ endpoint: string; server: Server; close: () => Promise<void> }> {
  const instance = createServer(handler);
  await new Promise<void>((resolve) => instance.listen(0, "127.0.0.1", resolve));
  const address = instance.address();
  assert.ok(address && typeof address !== "string");
  return { endpoint: `http://127.0.0.1:${address.port}`, server: instance, close: async () => {
    instance.closeAllConnections();
    await new Promise<void>((resolve, reject) => instance.close((error) => error ? reject(error) : resolve()));
  } };
}

test("retrieval opt-in is independent and configuration diagnostics never contain values", () => {
  assert.equal(tool({ SEDIMENT_OTLP_ENDPOINT: "https://ingest.example.com", SEDIMENT_INGEST_TOKEN: TOKEN, OTEL_EXPORTER_OTLP_HEADERS: `Authorization=Bearer ${TOKEN}` }), undefined);
  const diagnostics: string[] = [];
  const original = console.error;
  console.error = (...args) => { diagnostics.push(args.join(" ")); };
  try {
    for (const env of [
      { SEDIMENT_RETRIEVAL_ENDPOINT: "https://retrieval.example.com" },
      { SEDIMENT_RETRIEVAL_TOKEN: TOKEN },
      { SEDIMENT_RETRIEVAL_ENDPOINT: "", SEDIMENT_RETRIEVAL_TOKEN: "" },
      { SEDIMENT_RETRIEVAL_ENDPOINT: "http://remote.example.com", SEDIMENT_RETRIEVAL_TOKEN: TOKEN },
      { SEDIMENT_RETRIEVAL_ENDPOINT: "https://retrieval.example.com", SEDIMENT_RETRIEVAL_TOKEN: "token\nsecret" },
    ]) assert.equal(tool(env), undefined);
  } finally { console.error = original; }
  assert.deepEqual(diagnostics, [
    ...Array(3).fill("sediment-pi: retrieval_disabled reason=incomplete_configuration"),
    "sediment-pi: retrieval_disabled reason=invalid_endpoint",
    "sediment-pi: retrieval_disabled reason=invalid_token",
  ]);
});

test("tool accepts only the fixed API base URL and native argument schema", () => {
  for (const endpoint of ["https://retrieval.example.com", "http://localhost:8080", "http://127.0.0.2:8080", "http://[::1]:8080"]) {
    const registered = enabled(endpoint);
    assert.equal(registered.name, "sediment_retrieve_context");
    assert.deepEqual(Object.keys((registered.parameters as Record<string, unknown>).properties ?? {}), ["query", "max_bytes"]);
    assert.equal((registered.parameters as Record<string, unknown>).additionalProperties, false);
  }
  const original = console.error;
  console.error = () => {};
  const credentialsInUrl = new URL("https://host");
  credentialsInUrl.username = "fixture-user";
  credentialsInUrl.password = "fixture-password";
  try {
    for (const endpoint of [credentialsInUrl.href, "https://host/?token=secret", "https://host/#secret", "https://host/v1/logs", "https://host/query/context", "file:///tmp/secret", "http://localhost.example.com", "https://host/base"]) {
      assert.equal(enabled(endpoint), undefined);
    }
  } finally { console.error = original; }
});

test("request uses the fixed route and original JSON reaches the tool result without numeric rounding", async () => {
  let requests = 0;
  const running = await server(async (req, res) => {
    requests++;
    assert.equal(req.url, "/query/context");
    assert.equal(req.method, "POST");
    assert.equal(req.headers.authorization, `Bearer ${TOKEN}`);
    assert.equal(req.headers["accept-encoding"], "identity");
    let body = "";
    for await (const chunk of req) body += chunk;
    assert.deepEqual(JSON.parse(body), { schema_version: 1, query: "parser failure", max_bytes: 16_384 });
    res.writeHead(200, { "Content-Type": "application/json" }); res.end(RAW_RESPONSE);
  });
  try {
    assert.deepEqual(await invoke(enabled(running.endpoint)), { content: [{ type: "text", text: RAW_RESPONSE }], details: {} });
    assert.equal(requests, 1);
  } finally { await running.close(); }
});

test("invalid arguments cannot choose Session, authority, endpoint, or exceed query limits", async () => {
  const original = globalThis.fetch;
  globalThis.fetch = () => { throw new Error("must not reach fetch"); };
  try {
    for (const args of [
      {}, null, { query: "" }, { query: "the and WHAT" }, { query: "\ud800 error" }, { query: "İ" },
      { query: "error", max_bytes: true }, { query: "error", max_bytes: 4095 }, { query: "error", max_bytes: 65537 },
      { query: "error", max_bytes: 5000.5 }, { query: "é".repeat(1024) + "x" },
      ...["session_id", "org_id", "endpoint", "token", "reference", "schema_version"].map((key) => ({ query: "error", [key]: "secret" })),
    ]) await assert.rejects(invoke(enabled(), args), /reason=invalid_arguments$/);
  } finally { globalThis.fetch = original; }
});

test("redirects are refused without sending credentials to their target or retrying", async () => {
  let count = 0;
  let redirected = 0;
  const target = await server((_req, res) => { redirected++; res.end(); });
  const source = await server((_req, res) => { count++; res.writeHead(307, { Location: target.endpoint }); res.end(); });
  try {
    await assert.rejects(invoke(enabled(source.endpoint)), /reason=request_failed$/);
    assert.equal(count, 1); assert.equal(redirected, 0);
  } finally { await source.close(); await target.close(); }
});

test("streaming response is bounded independently of Content-Length and cancels its body", async () => {
  const original = globalThis.fetch;
  let cancelled = false;
  let reads = 0;
  globalThis.fetch = async () => new Response(new ReadableStream({
    pull(controller) { reads++; controller.enqueue(new Uint8Array(1024).fill(32)); },
    cancel() { cancelled = true; },
  }), { headers: { "Content-Type": "application/json", "Content-Length": "1" } });
  try {
    await assert.rejects(invoke(enabled(), { query: "error", max_bytes: 4096 }), /reason=response_limit$/);
    assert.equal(cancelled, true); assert.ok(reads <= 7);
  } finally { globalThis.fetch = original; }
});

test("cancellation interrupts an in-flight body without emitting partial content", async () => {
  const controller = new AbortController();
  const running = await server((_req, res) => {
    res.writeHead(200, { "Content-Type": "application/json" }); res.write('{"private":');
    controller.abort(new Error("private cancellation message"));
  });
  try { await assert.rejects(invoke(enabled(running.endpoint), undefined, controller.signal), /reason=cancelled$/); }
  finally { await running.close(); }
});

test("already cancelled tool never sends a request", async () => {
  const original = globalThis.fetch;
  globalThis.fetch = () => { throw new Error("must not reach fetch"); };
  try { await assert.rejects(invoke(enabled(), undefined, AbortSignal.abort("secret")), /reason=cancelled$/); }
  finally { globalThis.fetch = original; }
});

test("35-second deadline covers the request and reports a content-free error", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const original = globalThis.fetch;
  globalThis.fetch = async (_url, init) => new Promise((_resolve, reject) => {
    init!.signal!.addEventListener("abort", () => reject(new Error("private transport error")), { once: true });
  });
  try {
    const pending = invoke(enabled());
    const rejected = assert.rejects(pending, /reason=deadline$/);
    t.mock.timers.tick(35_000);
    await rejected;
  } finally { globalThis.fetch = original; t.mock.timers.reset(); }
});

test("safe refusals preserve only recognized server reasons", async () => {
  const original = globalThis.fetch;
  try {
    for (const [status, body, reason] of [
      [409, { detail: { reason: "evidence_source_limit", content: "secret" } }, "evidence_source_limit"],
      [409, { detail: { reason: "secret" } }, "request_failed"],
      [401, { detail: "secret" }, "unauthorized"], [403, { detail: "secret" }, "forbidden"],
      [503, { detail: "secret" }, "unavailable"], [422, { detail: "secret" }, "invalid_request"],
    ] as const) {
      globalThis.fetch = async () => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
      await assert.rejects(invoke(enabled()), new RegExp(`reason=${reason}$`));
    }
  } finally { globalThis.fetch = original; }
});

test("client validates the entire response before publishing any evidence", async () => {
  const original = globalThis.fetch;
  try {
    const malformed = [
      "broken private json", RAW_RESPONSE.replace("9007199254740993", "NaN"),
      RAW_RESPONSE.replace("surrogate", "é"),
      ...[
        (v: any) => { v.schema_version = 2; }, (v: any) => { v.policy_version = 2; },
        (v: any) => { v.coverage.complete_visible_scan = false; },
        (v: any) => { v.coverage.scanned_parts = 10; },
        (v: any) => { v.skipped.unknown = 0; }, (v: any) => { delete v.skipped.no_match; },
        (v: any) => { v.items[0].evidence.reference.side = "raw"; },
        (v: any) => { v.items[0].evidence.reference.part_index = -1; },
        (v: any) => { v.items[0].evidence.part = { type: "reasoning", content: "secret" }; },
        (v: any) => { v.items[0].evidence.part.raw = "secret"; },
        (v: any) => { v.items[0].evidence.observed_at = "yesterday"; },
        (v: any) => { v.items[0].score = 0; }, (v: any) => { v.status = "no_match"; },
        (v: any) => { v.source_session_id = "\0"; }, (v: any) => { v.extra = "secret"; },
      ].map((mutate) => { const value = responseBody(); mutate(value); return JSON.stringify(value); }),
    ];
    for (const body of malformed) {
      globalThis.fetch = async () => new Response(body, { headers: { "Content-Type": "application/json" } });
      await assert.rejects(invoke(enabled()), /reason=invalid_response$/);
    }
    for (const headers of [{ "Content-Type": "text/plain" }, { "Content-Type": "application/json", "Content-Encoding": "gzip" }]) {
      globalThis.fetch = async () => new Response(RAW_RESPONSE, { headers: headers as Record<string, string> });
      await assert.rejects(invoke(enabled()), /reason=invalid_response$/);
    }
  } finally { globalThis.fetch = original; }
});

test("empty success preserves unknown capture completeness and counted exclusions", async () => {
  const original = globalThis.fetch;
  try {
    for (const status of ["no_match", "budget_exhausted"]) {
      const value: any = responseBody(); value.status = status; value.items = [];
      value.skipped[status === "no_match" ? "no_match" : "response_budget"] = 1;
      const text = JSON.stringify(value);
      globalThis.fetch = async () => new Response(text, { headers: { "Content-Type": "application/json" } });
      assert.deepEqual((await invoke(enabled())).content, [{ type: "text", text }]);
    }
  } finally { globalThis.fetch = original; }
});
