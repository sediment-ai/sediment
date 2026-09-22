// SPDX-License-Identifier: MIT
// Opt-in API acceptance. The caller seeds isolated real storage; fetch is real.
import assert from "node:assert/strict";
import test from "node:test";
import { exerciseDiscovery } from "./discovery-harness.ts";

const endpoint = process.env.SEDIMENT_PI_DISCOVERY_API_URL;
const token = process.env.SEDIMENT_PI_DISCOVERY_TOKEN;
test("native pi discovery and selected retrieval through the real API", { skip: endpoint === undefined && token === undefined }, async () => {
  assert.ok(endpoint && token, "both public-path acceptance settings are required");
  await exerciseDiscovery(endpoint, token);
});
