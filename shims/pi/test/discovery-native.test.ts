// SPDX-License-Identifier: MIT
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import test from "node:test";
import { exerciseDiscovery } from "./discovery-harness.ts";
import { discoveryBody, exact, selectedBody } from "./discovery-fixture.ts";

test("native pi discovers an unspecified Session, selects exact context, and reports denied selection", async () => {
  const previous = globalThis.fetch;
  const chosen = randomUUID();
  let requests = 0;
  globalThis.fetch = async (url, init) => {
    requests++;
    assert.equal(init?.redirect, "error");
    const request = JSON.parse(init?.body as string);
    assert.equal(request.query, "shipment replay constraint");
    if (String(url).endsWith("/discover")) {
      const value = discoveryBody(); value.items[0]!.session_id = chosen;
      value.items.unshift({ ...value.items[0]!, session_id: randomUUID(), preview: { ...(value.items[0]!.preview as object), part: { type: "text", content: "Shipment schema constraint." } } });
      value.coverage.matched_parts = 2; value.skipped.unmatched_part = 0; value.skipped.unmatched_session = 0;
      return new Response(exact(value), { headers: { "Content-Type": "application/json" } });
    }
    assert.ok(String(url).endsWith("/selected"));
    if (request.session_id === "outside-grant") return new Response('{"detail":"forbidden"}', { status: 403, headers: { "Content-Type": "application/json" } });
    assert.equal(request.session_id, chosen);
    return new Response(exact(selectedBody(chosen)), { headers: { "Content-Type": "application/json" } });
  };
  try {
    await exerciseDiscovery("https://retrieval.example.com", "native-discovery-token");
    assert.equal(requests, 3);
  } finally { globalThis.fetch = previous; }
});
