// SPDX-License-Identifier: MIT
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import test from "node:test";
import { exerciseEvidence } from "./evidence-harness.ts";
import { discoveryBody } from "./discovery-fixture.ts";
import { exact, grantBody, inventoryBody, manifestBody, readBody, references, source } from "./evidence-fixture.ts";

test("native pi independently selects a granted Session, then reads all exact occurrences", async () => {
  const previous = globalThis.fetch;
  const chosen = randomUUID();
  let requests = 0;
  globalThis.fetch = async (url, init) => {
    requests++;
    assert.equal(init?.redirect, "error");
    const target = new URL(String(url));
    let value: unknown;
    if (target.pathname === "/v1/me") value = { ...grantBody(), source_session_ids: ["ranked-session", "other-ranked-session", chosen] };
    else if (target.pathname.endsWith("/discover")) {
      const discovery = discoveryBody(); discovery.items[0]!.session_id = "ranked-session";
      discovery.items.push({ ...discovery.items[0]!, session_id: "other-ranked-session" });
      discovery.coverage.found_sessions = 3; discovery.coverage.visible_inference_calls = 3; discovery.coverage.scanned_parts = 3; discovery.coverage.matched_parts = 2;
      value = discovery;
    } else if (target.pathname.endsWith("/manifest")) {
      assert.equal(target.searchParams.get("session_id"), chosen);
      value = { ...manifestBody(), session_id: chosen };
    } else if (target.pathname.endsWith("/evidence")) {
      assert.equal(target.searchParams.get("session_id"), chosen);
      value = { ...inventoryBody(), session_id: chosen };
    } else {
      assert.ok(target.pathname.endsWith("/read"));
      const request = JSON.parse(init?.body as string);
      if (request.session_id === "outside-grant") return new Response('{"detail":"forbidden"}', { status: 403, headers: { "Content-Type": "application/json" } });
      assert.equal(request.session_id, chosen);
      assert.deepEqual(request.references, references);
      value = { ...readBody(), session_id: chosen };
    }
    return new Response(exact(value), { headers: { "Content-Type": "application/json" } });
  };
  try { await exerciseEvidence("https://retrieval.example.com", "native-evidence-token"); assert.equal(requests, 6); }
  finally { globalThis.fetch = previous; }
});

test("a known native reference is refused after Quarantine and readable after release", async () => {
  const previous = globalThis.fetch;
  try {
    for (const unavailable of [true, false]) {
      globalThis.fetch = async () => new Response(unavailable ? '{"detail":{"reason":"evidence_unavailable"}}' : exact({ ...readBody([references[0]!]), quarantine_revision: 2 }), {
        status: unavailable ? 409 : 200, headers: { "Content-Type": "application/json" },
      });
      await exerciseEvidence("https://retrieval.example.com", "native-evidence-token", { session_id: source, inference_call_id: references[0]!.inference_call_id, unavailable, revision: unavailable ? 1 : 2 });
    }
  } finally { globalThis.fetch = previous; }
});
