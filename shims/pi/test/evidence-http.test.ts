// SPDX-License-Identifier: MIT
// The acceptance parent owns real storage, Quarantine changes, and API startup.
import assert from "node:assert/strict";
import test from "node:test";
import { exerciseEvidence, type KnownEvidence } from "./evidence-harness.ts";

const endpoint = process.env.SEDIMENT_PI_EVIDENCE_API_URL;
const token = process.env.SEDIMENT_PI_EVIDENCE_TOKEN;
const probe = process.env.SEDIMENT_PI_EVIDENCE_KNOWN;
test("native pi reads factual evidence through the real API", { skip: endpoint === undefined && token === undefined }, async () => {
  assert.ok(endpoint && token, "both public-path acceptance settings are required");
  await exerciseEvidence(endpoint, token, probe ? JSON.parse(probe) as KnownEvidence : undefined);
});
