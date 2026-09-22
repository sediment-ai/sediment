// SPDX-License-Identifier: MIT
import assert from "node:assert/strict";
import { createServer } from "node:http";
import test from "node:test";
import type { ToolDefinition } from "@earendil-works/pi-coding-agent";
import { registerRetrieval } from "../lib/retrieval.ts";
import { callId, exact, grantBody, inventoryBody, manifestBody, readBody, references, source } from "./evidence-fixture.ts";

const NAMES = ["sediment_list_context_sessions", "sediment_evidence_inventory", "sediment_evidence_manifest", "sediment_read_evidence"] as const;
function registered(endpoint = "https://retrieval.example.com", discovery?: string) {
  const tools: ToolDefinition[] = [];
  registerRetrieval({ registerTool: (tool) => tools.push(tool as ToolDefinition) }, {
    SEDIMENT_RETRIEVAL_ENDPOINT: endpoint, SEDIMENT_RETRIEVAL_TOKEN: "factual-test-token", SEDIMENT_RETRIEVAL_DISCOVERY: discovery,
  });
  return tools;
}
function invoke(name: typeof NAMES[number], args: unknown = {}, signal?: AbortSignal, endpoint?: string) {
  const tool = registered(endpoint).find((tool) => tool.name === name); assert.ok(tool);
  return tool.execute("call", args, signal, () => { throw new Error("unexpected partial evidence"); }, {} as never);
}
const calls = [
  [NAMES[0], {}, grantBody], [NAMES[1], { session_id: source }, inventoryBody],
  [NAMES[2], { session_id: source, inference_call_id: callId }, manifestBody],
  [NAMES[3], { session_id: source, references }, readBody],
] as const;

test("factual tools register in singleton and discovery modes with closed argument schemas", () => {
  for (const mode of [undefined, "false", "true"]) {
    const tools = registered(undefined, mode);
    assert.deepEqual(tools.slice(0, 4).map((tool) => tool.name), NAMES);
    for (const tool of tools) assert.equal((tool.parameters as any).additionalProperties, false);
    assert.equal(tools.at(-1)!.name, "sediment_retrieve_context");
  }
});

test("grant, inventory, manifest, and exact fetch use independent bounded HTTP and preserve original text", async () => {
  const previous = globalThis.fetch;
  const requests: [string, RequestInit | undefined][] = [];
  try {
    for (const [name, args, response] of calls) {
      const text = exact(response());
      globalThis.fetch = async (url, init) => { requests.push([String(url), init]); return new Response(text, { headers: { "Content-Type": "application/json" } }); };
      assert.deepEqual((await invoke(name, args)).content, [{ type: "text", text }]);
    }
    assert.deepEqual(requests.map(([url, init]) => [url, init?.method]), [
      ["https://retrieval.example.com/v1/me", "GET"],
      [`https://retrieval.example.com/query/context/evidence?session_id=${source}`, "GET"],
      [`https://retrieval.example.com/query/context/evidence/manifest?session_id=${source}&inference_call_id=${callId}`, "GET"],
      ["https://retrieval.example.com/query/context/evidence/read", "POST"],
    ]);
    for (const [, init] of requests) {
      assert.equal(init?.redirect, "error"); assert.ok(init.signal);
      assert.equal((init.headers as Record<string, string>).Authorization, "Bearer factual-test-token");
    }
    assert.deepEqual(JSON.parse(requests[3]![1]!.body as string), { schema_version: 1, session_id: source, references });
    assert.ok(requests.slice(0, 3).every(([, init]) => init?.body === undefined));
  } finally { globalThis.fetch = previous; }
});

test("grant enumeration preserves UTF-8 and validates singleton/plural retrieval identity without exposing unrelated fields", async () => {
  const previous = globalThis.fetch;
  try {
    for (const value of [grantBody(), { org_id: "acme", version: "0.1.0", authority: "retrieval", client_id: "retrieval", source_session_id: "café/追跡" }]) {
      const text = JSON.stringify(value);
      globalThis.fetch = async () => new Response(text, { headers: { "Content-Type": "application/json" } });
      assert.deepEqual((await invoke(NAMES[0])).content, [{ type: "text", text }]);
    }
    for (const value of [
      { ...grantBody(), authority: "operator" }, { ...grantBody(), client_id: "ingest" },
      { ...grantBody(), source_session_ids: [] }, { ...grantBody(), source_session_ids: Array(33).fill("s") },
      { ...grantBody(), source_session_ids: [source, source] }, { ...grantBody(), source_session_ids: ["\ud800"] },
      { ...grantBody(), token: "private" }, { ...grantBody(), source_session_id: source },
    ]) {
      globalThis.fetch = async () => new Response(JSON.stringify(value), { headers: { "Content-Type": "application/json" } });
      await assert.rejects(invoke(NAMES[0]), /reason=invalid_response$/);
    }
  } finally { globalThis.fetch = previous; }
});

test("exact reads normalize canonical identity whitespace and encode URL delimiters without path selection", async () => {
  const previous = globalThis.fetch;
  const session = "café/with?query#fragment";
  try {
    globalThis.fetch = async (url) => {
      const parsed = new URL(String(url)); assert.equal(parsed.pathname, "/query/context/evidence"); assert.equal(parsed.searchParams.get("session_id"), session);
      return new Response(exact({ ...inventoryBody(), session_id: session }), { headers: { "Content-Type": "application/json" } });
    };
    await invoke(NAMES[1], { session_id: "\u0085" + session + "\u001c" });
  } finally { globalThis.fetch = previous; }
});

test("grant transport rejects malformed UTF-8 and BOMs without rewriting identity", async () => {
  const previous = globalThis.fetch;
  try {
    for (const bytes of [
      Buffer.concat([Buffer.from('{"org_id":"'), Buffer.from([255]), Buffer.from('"}')]),
      Buffer.from("\ufeff" + JSON.stringify(grantBody())),
    ]) {
      globalThis.fetch = async () => new Response(bytes, { headers: { "Content-Type": "application/json" } });
      await assert.rejects(invoke(NAMES[0]), /reason=invalid_response$/);
    }
  } finally { globalThis.fetch = previous; }
});

test("invalid or duplicate occurrence arguments never send HTTP", async () => {
  const previous = globalThis.fetch; let sent = 0;
  globalThis.fetch = async () => { sent++; throw new Error("unexpected request"); };
  try {
    for (const [name, args] of [
      [NAMES[0], { session_id: source }], [NAMES[1], {}], [NAMES[1], { session_id: "\0" }],
      [NAMES[1], { session_id: source, query: "requirements" }], [NAMES[2], { session_id: source }],
      [NAMES[3], { session_id: source, references: [] }], [NAMES[3], { session_id: source, references: Array(33).fill(references[0]) }],
      [NAMES[3], { session_id: source, references: [references[0], references[0]] }],
      ...[{ side: "raw" }, { message_index: -1 }, { part_index: Number.MAX_SAFE_INTEGER + 1 }, { part_index: true }, { inference_call_id: "\ud800" }, { extra: "private" }].map((change) => [NAMES[3], { session_id: source, references: [{ ...references[0], ...change }] }]),
      [NAMES[3], { session_id: source, references, schema_version: 1 }],
    ] as [typeof NAMES[number], unknown][]) await assert.rejects(invoke(name, args), /reason=invalid_arguments$/);
    await assert.rejects(invoke(NAMES[3], { session_id: source, references: [references[0], { ...references[0], inference_call_id: ` ${callId} ` }] }), /reason=invalid_arguments$/);
    await assert.rejects(invoke(NAMES[3], { session_id: "s".repeat(65536), references }), /reason=request_limit$/);
    await assert.rejects(invoke(NAMES[1], { session_id: "s".repeat(65536) }), /reason=request_limit$/);
    assert.equal(sent, 0);
  } finally { globalThis.fetch = previous; }
});

test("full inventories and manifests reject truncation, foreign identity, private fields, and invalid occurrence order", async () => {
  const previous = globalThis.fetch;
  try {
    for (const [name, args, build, mutations] of [
      [NAMES[1], { session_id: source }, inventoryBody, [
        (v: any) => { v.visible_inference_calls = 2; }, (v: any) => { v.found = false; },
        (v: any) => { v.calls.push(v.calls[0]); v.visible_inference_calls = 2; },
        (v: any) => { v.calls[0].user_id = "private"; }, (v: any) => { v.calls[0].raw = {}; },
        (v: any) => { v.calls[0].observed_at = "2026-02-30T00:00:00Z"; },
      ]],
      [NAMES[2], { session_id: source, inference_call_id: callId }, manifestBody, [
        (v: any) => { v.call.inference_call_id = "foreign"; }, (v: any) => { v.messages.reverse(); },
        (v: any) => { v.messages[0].message_index = 1; }, (v: any) => { v.messages[0].parts[0].content = "private"; },
        (v: any) => { v.messages[0].parts[0].reference.inference_call_id = "foreign"; },
        (v: any) => { v.messages[1].parts[1].reference.part_index = 2; },
      ]],
    ] as const) {
      for (const mutate of [...mutations, (v: any) => { v.session_id = "foreign"; }, (v: any) => { v.schema_version = 2; }]) {
        const value = build(); mutate(value);
        globalThis.fetch = async () => new Response(exact(value), { headers: { "Content-Type": "application/json" } });
        await assert.rejects(invoke(name, args), /reason=invalid_response$/);
      }
    }
  } finally { globalThis.fetch = previous; }
});

test("exact fetch preserves reasoning, repeated content and requested order, refusing partial or reordered packets", async () => {
  const previous = globalThis.fetch;
  try {
    for (const mutate of [
      (v: any) => { v.items.pop(); }, (v: any) => { v.items.reverse(); },
      (v: any) => { v.items[0].reference.inference_call_id = "foreign"; },
      (v: any) => { v.items[0].part.raw = "private"; },
      (v: any) => { v.items[0].part = { type: "unknown" }; },
    ]) {
      const value = readBody(); mutate(value);
      globalThis.fetch = async () => new Response(exact(value), { headers: { "Content-Type": "application/json" } });
      await assert.rejects(invoke(NAMES[3], { session_id: source, references }), /reason=invalid_response$/);
    }
    const text = exact(readBody());
    globalThis.fetch = async () => new Response(text, { headers: { "Content-Type": "application/json" } });
    assert.deepEqual((await invoke(NAMES[3], { session_id: source, references })).content, [{ type: "text", text }]);
    assert.equal((text.match(/Requirements:/g) ?? []).length, 2);
    assert.ok(text.includes('"type":"reasoning"'));
  } finally { globalThis.fetch = previous; }
});

test("exact transport enforces the whole 1-MiB response bound and cancels overflow", async () => {
  const previous = globalThis.fetch; let cancelled = false;
  globalThis.fetch = async () => new Response(new ReadableStream({
    pull(controller) { controller.enqueue(new Uint8Array(65536).fill(32)); }, cancel() { cancelled = true; },
  }), { headers: { "Content-Type": "application/json", "Content-Length": "1" } });
  try { await assert.rejects(invoke(NAMES[1], { session_id: source }), /reason=response_limit$/); assert.ok(cancelled); }
  finally { globalThis.fetch = previous; }
});

test("factual transport preserves safe refusal reasons and never forwards content-bearing errors", async () => {
  const previous = globalThis.fetch;
  try {
    for (const [status, reason, expected] of [[409, "evidence_unavailable", "evidence_unavailable"], [409, "evidence_part_absent", "evidence_part_absent"], [409, "non_finite_number", "non_finite_number"], [409, "private", "request_failed"], [403, "private", "forbidden"], [404, "private", "disabled"], [503, "private", "unavailable"]] as const) {
      globalThis.fetch = async () => new Response(JSON.stringify({ detail: { reason, private: "secret-body" } }), { status, headers: { "Content-Type": "application/json" } });
      await assert.rejects(invoke(NAMES[3], { session_id: source, references }), new RegExp(`reason=${expected}$`));
    }
    globalThis.fetch = async () => { throw new Error("must not send"); };
    for (const [name, args] of calls) await assert.rejects(invoke(name, args, AbortSignal.abort("private")), /reason=cancelled$/);
  } finally { globalThis.fetch = previous; }
});

test("factual GET and POST refuse redirects without sending credentials onward", async () => {
  let targets = 0;
  const target = createServer((_req, res) => { targets++; res.end(); });
  const redirect = createServer((_req, res) => { const address = target.address(); assert.ok(address && typeof address !== "string"); res.writeHead(307, { Location: `http://127.0.0.1:${address.port}` }); res.end(); });
  await new Promise<void>((resolve) => target.listen(0, "127.0.0.1", resolve));
  await new Promise<void>((resolve) => redirect.listen(0, "127.0.0.1", resolve));
  const address = redirect.address(); assert.ok(address && typeof address !== "string");
  try {
    for (const [name, args] of calls) await assert.rejects(invoke(name, args, undefined, `http://127.0.0.1:${address.port}`), /reason=request_failed$/);
    assert.equal(targets, 0);
  } finally { redirect.closeAllConnections(); target.closeAllConnections(); await Promise.all([new Promise<void>((resolve) => redirect.close(() => resolve())), new Promise<void>((resolve) => target.close(() => resolve()))]); }
});
