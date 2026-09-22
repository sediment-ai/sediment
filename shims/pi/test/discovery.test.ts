// SPDX-License-Identifier: MIT
import assert from "node:assert/strict";
import test from "node:test";
import type { ToolDefinition } from "@earendil-works/pi-coding-agent";
import { registerRetrieval } from "../lib/retrieval.ts";
import { anchor, discoveryBody, exact, selectedBody } from "./discovery-fixture.ts";

function tools(mode: string | undefined = "true", extra: Record<string, string | undefined> = {}) {
  const found: ToolDefinition[] = [];
  registerRetrieval({ registerTool: (tool) => { found.push(tool as ToolDefinition); } }, {
    SEDIMENT_RETRIEVAL_ENDPOINT: "https://retrieval.example.com", SEDIMENT_RETRIEVAL_TOKEN: "discovery-test-token",
    SEDIMENT_RETRIEVAL_DISCOVERY: mode, ...extra,
  });
  return found;
}
function invoke(name: string, args: unknown, signal?: AbortSignal) {
  const tool = tools().find((tool) => tool.name === name);
  assert.ok(tool, `missing native tool ${name}`);
  return tool.execute("call", args, signal, () => {}, {} as never);
}
const discover = (args: unknown = { query: "shipment replay constraint" }, signal?: AbortSignal) => invoke("sediment_discover_context", args, signal);
const selected = (args: unknown = { query: "shipment replay constraint", session_id: "uncommitted-session" }) => invoke("sediment_retrieve_context", args);

test("discovery opt-in changes native schemas while false and absent keep singleton schema", () => {
  for (const mode of [undefined, "false"]) {
    const found = tools(mode, { SEDIMENT_RETRIEVAL_DISCOVERY: mode });
    assert.deepEqual(found.map((tool) => tool.name), ["sediment_retrieve_context"]);
    assert.deepEqual(Object.keys((found[0]!.parameters as any).properties), ["query", "max_bytes"]);
  }
  const found = tools();
  assert.deepEqual(found.map((tool) => tool.name), ["sediment_discover_context", "sediment_retrieve_context"]);
  assert.deepEqual((found[1]!.parameters as any).required, ["query", "session_id"]);
  assert.equal((found[0]!.parameters as any).additionalProperties, false);
  const diagnostics: string[] = [];
  const previous = console.error; console.error = (...args) => { diagnostics.push(args.join(" ")); };
  try {
    for (const mode of ["TRUE", "1", "", "private-invalid-value"]) assert.deepEqual(tools(mode), []);
    assert.deepEqual(tools("true", { SEDIMENT_RETRIEVAL_ENDPOINT: undefined, SEDIMENT_RETRIEVAL_TOKEN: undefined }), []);
  } finally { console.error = previous; }
  assert.deepEqual(diagnostics, [...Array(4).fill("sediment-pi: retrieval_disabled reason=invalid_discovery_mode"), "sediment-pi: retrieval_disabled reason=incomplete_configuration"]);
});

test("discovery and selected calls use bounded independent transport and preserve original JSON", async () => {
  const previous = globalThis.fetch;
  const requests: unknown[] = [];
  globalThis.fetch = async (url, init) => {
    assert.equal(init?.method, "POST"); assert.equal(init.redirect, "error");
    assert.equal((init.headers as Record<string, string>).Authorization, "Bearer discovery-test-token");
    const request = JSON.parse(init.body as string); requests.push([url, request]);
    return new Response(exact(String(url).endsWith("/discover") ? discoveryBody() : selectedBody()), { headers: { "Content-Type": "application/json" } });
  };
  try {
    assert.deepEqual((await discover()).content, [{ type: "text", text: exact(discoveryBody()) }]);
    assert.deepEqual((await selected()).content, [{ type: "text", text: exact(selectedBody()) }]);
    assert.deepEqual(requests, [
      ["https://retrieval.example.com/query/context/discover", { schema_version: 1, query: "shipment replay constraint", max_bytes: 16384 }],
      ["https://retrieval.example.com/query/context/selected", { schema_version: 1, query: "shipment replay constraint", max_bytes: 16384, session_id: "uncommitted-session" }],
    ]);
  } finally { globalThis.fetch = previous; }
});

test("discovery validates full normalized commit identity and commit-only evidence", async () => {
  const previous = globalThis.fetch;
  globalThis.fetch = async (_url, init) => {
    assert.deepEqual(JSON.parse(init!.body as string).commit, anchor);
    const value = discoveryBody(); value.commit = anchor;
    value.items[0] = { session_id: "observed-session", score: 0, matched_parts: 0, preview: null, commit_match: { observation_id: "observation", source_push_id: "push", captured_at: "2026-09-22T12:00:00Z" } };
    value.coverage.matched_parts = 0; value.skipped.unmatched_part = 2;
    return new Response(exact(value), { headers: { "Content-Type": "application/json" } });
  };
  try {
    const result = await discover({ query: "shipment", commit: { ...anchor, repository_host: "GitHub.COM", commit_sha: anchor.commit_sha.toUpperCase() } });
    assert.ok(result.content[0]?.type === "text");
  } finally { globalThis.fetch = previous; }
});

test("invalid discovery and selected inputs never reach HTTP", async () => {
  const previous = globalThis.fetch; let requests = 0;
  globalThis.fetch = async () => { requests++; throw new Error("unexpected request"); };
  try {
    for (const commit of ["sha", {}, { commit_sha: anchor.commit_sha }, { ...anchor, repository_provider: "gitlab" }, { ...anchor, repository_host: "github.com:443" }, { ...anchor, repository_id: 123 }, { ...anchor, repository_id: "01" }, { ...anchor, repository_id: "1".repeat(21) }, { ...anchor, commit_sha: "a".repeat(39) }, { ...anchor, extra: "secret" }]) {
      await assert.rejects(discover({ query: "shipment", commit }), /reason=invalid_arguments$/);
    }
    for (const args of [{ query: "shipment" }, { query: "shipment", session_id: "\0" }, { query: "shipment", session_id: "\ud800" }, { query: "shipment", session_id: "source", commit: anchor }]) await assert.rejects(selected(args), /reason=invalid_arguments$/);
    for (const args of [{ query: "shipment", session_id: "source" }, { query: "shipment", token: "secret" }, { query: "the and" }, { query: "error", max_bytes: true }]) await assert.rejects(discover(args), /reason=invalid_arguments$/);
    assert.equal(requests, 0);
  } finally { globalThis.fetch = previous; }
});

test("selected request bytes are capped before transport even for a valid long identity", async () => {
  const previous = globalThis.fetch; let requests = 0;
  globalThis.fetch = async () => { requests++; throw new Error("unexpected request"); };
  try {
    await assert.rejects(selected({ query: "shipment", session_id: "source".repeat(4000) }), /reason=request_limit$/);
    assert.equal(requests, 0);
  } finally { globalThis.fetch = previous; }
});

test("selected identity normalization matches canonical Python whitespace without erasing a BOM", async () => {
  const previous = globalThis.fetch;
  try {
    for (const [input, canonical] of [["\u0085source\u001c", "source"], ["\ufeffsource", "\ufeffsource"]]) {
      globalThis.fetch = async (_url, init) => {
        assert.equal(JSON.parse(init!.body as string).session_id, canonical);
        return new Response(exact(selectedBody(canonical)).replaceAll("\ufeff", "\\ufeff"), { headers: { "Content-Type": "application/json" } });
      };
      const result = await selected({ query: "shipment", session_id: input });
      assert.equal(result.content[0]?.type, "text");
    }
  } finally { globalThis.fetch = previous; }
});

test("whole discovery envelope, counts, unique Sessions, anchors, and preview invariants are validated", async () => {
  const previous = globalThis.fetch;
  try {
    const mutations: ((v: any) => void)[] = [
      (v) => { v.extra = "secret"; }, (v) => { v.policy_version = 2; },
      (v) => { v.coverage.complete_visible_scan = false; }, (v) => { v.coverage.authorized_sessions = 33; },
      (v) => { v.coverage.found_sessions = 4; }, (v) => { v.coverage.visible_inference_calls = 1001; },
      (v) => { v.coverage.scanned_parts = 2049; }, (v) => { v.coverage.matched_parts = 2; },
      (v) => { v.skipped.unmatched_session = 0; }, (v) => { v.skipped.extra = 0; },
      (v) => { v.items.push(v.items[0]); v.skipped.unmatched_session = 0; },
      (v) => { v.items[0].score = 0; }, (v) => { v.items[0].matched_parts = 0; },
      (v) => { v.items[0].matched_parts = 2; }, (v) => { v.items[0].preview = null; },
      (v) => { v.items[0].preview.part = { type: "reasoning", content: "secret" }; },
      (v) => { v.items[0].preview.reference.part_index = -1; },
      (v) => { v.items[0].preview.observed_at = "2026-02-30T12:00:00Z"; },
      (v) => { v.skipped.unmatched_session = 0; v.skipped.candidate_limit = 1; },
      (v) => { v.items[0].commit_match = { observation_id: "o", source_push_id: "p", captured_at: "2026-09-22T12:00:00Z" }; },
      (v) => { v.commit = anchor; }, (v) => { v.status = "no_match"; },
    ];
    for (const mutate of mutations) {
      const value = discoveryBody(); mutate(value);
      globalThis.fetch = async () => new Response(exact(value), { headers: { "Content-Type": "application/json" } });
      await assert.rejects(discover(), /reason=invalid_response$/);
    }
    for (const change of [{ repository_host: "another.example" }, { repository_id: "456" }, { commit_sha: "b".repeat(40) }]) {
      const value = discoveryBody(); value.commit = { ...anchor, ...change };
      globalThis.fetch = async () => new Response(exact(value), { headers: { "Content-Type": "application/json" } });
      await assert.rejects(discover({ query: "shipment", commit: anchor }), /reason=invalid_response$/);
    }
    globalThis.fetch = async () => new Response(exact(selectedBody("other-session")), { headers: { "Content-Type": "application/json" } });
    await assert.rejects(selected(), /reason=invalid_response$/);
  } finally { globalThis.fetch = previous; }
});

test("empty discovery outcomes preserve conservation and a full response bound", async () => {
  const previous = globalThis.fetch;
  try {
    for (const status of ["no_match", "budget_exhausted"]) {
      const value = discoveryBody(); value.items = []; value.status = status;
      if (status === "no_match") { value.coverage.matched_parts = 0; value.skipped.unmatched_part = 2; value.skipped.unmatched_session = 2; }
      else value.skipped.response_budget = 1;
      const text = exact(value);
      globalThis.fetch = async () => new Response(text, { headers: { "Content-Type": "application/json" } });
      assert.deepEqual((await discover()).content, [{ type: "text", text }]);
    }
    globalThis.fetch = async () => new Response(" ".repeat(4097), { headers: { "Content-Type": "application/json" } });
    await assert.rejects(discover({ query: "shipment", max_bytes: 4096 }), /reason=response_limit$/);
    globalThis.fetch = async () => { throw new Error("must not reach transport"); };
    await assert.rejects(discover(undefined, AbortSignal.abort("secret")), /reason=cancelled$/);
  } finally { globalThis.fetch = previous; }
});
