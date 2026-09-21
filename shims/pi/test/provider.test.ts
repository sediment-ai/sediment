// SPDX-License-Identifier: MIT
// Native Session-header contract pins. The header name is a
// cross-repo pin: packages/capture/sediment_capture/session_identity.py
// parses exactly this name server-side — changing one side without the
// other silently zeroes completion capture.

import assert from "node:assert/strict";
import test from "node:test";

import {
  DEFAULT_PROVIDER_API,
  DEFAULT_PROVIDER_ID,
  SESSION_HEADER,
} from "../lib/provider.ts";

test("the session header name is pinned to the server-side parser", () => {
  assert.equal(SESSION_HEADER, "x-sediment-session");
});

test("fleet provider defaults match the models.json renderer", () => {
  assert.equal(DEFAULT_PROVIDER_ID, "sediment");
  assert.equal(DEFAULT_PROVIDER_API, "anthropic-messages");
});
