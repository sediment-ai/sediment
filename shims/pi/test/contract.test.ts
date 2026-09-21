// SPDX-License-Identifier: MIT
// Tests for the pure contract builders (shims/pi/lib/contract.ts). The
// expected shapes mirror the golden server-side conformance fixture
// (packages/capture/tests/fixtures/otlp/sediment/tool_decision.json) — the
// two ends of the contract must never drift apart.

import assert from "node:assert/strict";
import test from "node:test";

import {
  buildDecisionRecord,
  buildLogsPayload,
  decisionFromToolEnd,
  filePathFromArgs,
  resolveEndpoint,
  resolveToken,
  resourceUserId,
} from "../lib/contract.ts";

test("buildDecisionRecord emits the contract shape with typed values", () => {
  const record = buildDecisionRecord({
    agent: "pi",
    sessionId: "sess-1",
    callId: "call-1",
    toolName: "write",
    decision: "accept",
    explicit: false,
    filePath: "/repo/app/main.py",
    timeUnixNano: "1782578510649000000",
  });
  assert.deepEqual(record.body, { stringValue: "sediment.tool_decision" });
  assert.equal(record.timeUnixNano, "1782578510649000000");
  const attrs = new Map(record.attributes.map((a) => [a.key, a.value]));
  assert.deepEqual(attrs.get("agent"), { stringValue: "pi" });
  assert.deepEqual(attrs.get("session.id"), { stringValue: "sess-1" });
  assert.deepEqual(attrs.get("tool_use_id"), { stringValue: "call-1" });
  assert.deepEqual(attrs.get("tool_name"), { stringValue: "write" });
  assert.deepEqual(attrs.get("decision"), { stringValue: "accept" });
  // explicit must stay a boolValue — the server requires a real bool.
  assert.deepEqual(attrs.get("explicit"), { boolValue: false });
  assert.deepEqual(attrs.get("file_path"), { stringValue: "/repo/app/main.py" });
});

test("buildLogsPayload wraps records with resource identity when present", () => {
  const records = [
    buildDecisionRecord({
      agent: "pi",
      sessionId: "sess-1",
      callId: "call-1",
      toolName: "edit",
      decision: "reject",
      explicit: true,
      filePath: "",
      timeUnixNano: "1782578510649000000",
    }),
  ];
  const payload = buildLogsPayload(records, { "user.id": "developer-1" });
  const [resourceLogs] = payload.resourceLogs;
  assert.deepEqual(resourceLogs.resource, {
    attributes: [{ key: "user.id", value: { stringValue: "developer-1" } }],
  });
  assert.equal(resourceLogs.scopeLogs[0].logRecords, records);
  // No identity → empty resource, not a placeholder.
  const anonymous = buildLogsPayload(records, {});
  assert.deepEqual(anonymous.resourceLogs[0].resource, { attributes: [] });
});

test("resolveEndpoint requires the explicit sediment endpoint", () => {
  assert.equal(resolveEndpoint({}), null);
  // Never falls back to the machine's generic telemetry config (normative
  // privacy contract — same posture as the Python clients).
  assert.equal(
    resolveEndpoint({ OTEL_EXPORTER_OTLP_ENDPOINT: "http://collector:4318" }),
    null,
  );
  assert.equal(
    resolveEndpoint({ SEDIMENT_OTLP_ENDPOINT: "https://ingest.example:9000" }),
    "https://ingest.example:9000/v1/logs",
  );
  assert.equal(
    resolveEndpoint({ SEDIMENT_OTLP_ENDPOINT: "http://localhost:9000/" }),
    "http://localhost:9000/v1/logs",
  );
  assert.equal(
    resolveEndpoint({ SEDIMENT_OTLP_ENDPOINT: "http://127.42.0.1:9000" }),
    "http://127.42.0.1:9000/v1/logs",
  );
  assert.equal(
    resolveEndpoint({ SEDIMENT_OTLP_ENDPOINT: "http://[::1]:9000/v1/logs" }),
    "http://[::1]:9000/v1/logs",
  );
  assert.equal(
    resolveEndpoint({ SEDIMENT_OTLP_ENDPOINT: "http://ingest:9000" }),
    null,
  );
  assert.equal(
    resolveEndpoint({ SEDIMENT_OTLP_ENDPOINT: "http://192.168.1.10:9000" }),
    null,
  );
});

test("resolveEndpoint trims surrounding whitespace before slash-strip", () => {
  // Leading + trailing space on a bare host: must trim to the clean value,
  // not surface a malformed URL to fetch() (mirrors the Python sibling's
  // configured.strip().rstrip("/")).
  assert.equal(
    resolveEndpoint({ SEDIMENT_OTLP_ENDPOINT: " https://localhost:9000 " }),
    "https://localhost:9000/v1/logs",
  );
  assert.equal(
    resolveEndpoint({ SEDIMENT_OTLP_ENDPOINT: "\thttps://localhost:9000\t" }),
    "https://localhost:9000/v1/logs",
  );
  assert.equal(
    resolveEndpoint({ SEDIMENT_OTLP_ENDPOINT: "\nhttps://localhost:9000\n" }),
    "https://localhost:9000/v1/logs",
  );
  assert.equal(
    resolveEndpoint({ SEDIMENT_OTLP_ENDPOINT: " https://ingest.example:9000" }),
    "https://ingest.example:9000/v1/logs",
  );
  assert.equal(
    resolveEndpoint({ SEDIMENT_OTLP_ENDPOINT: "https://ingest.example:9000 " }),
    "https://ingest.example:9000/v1/logs",
  );
  // A trailing slash with surrounding space: trim first, then strip slashes,
  // so the trailing space never survives into endsWith()/fetch().
  assert.equal(
    resolveEndpoint({ SEDIMENT_OTLP_ENDPOINT: " http://localhost:9000/ " }),
    "http://localhost:9000/v1/logs",
  );
  assert.equal(
    resolveEndpoint({ SEDIMENT_OTLP_ENDPOINT: " http://127.42.0.1:9000/\t" }),
    "http://127.42.0.1:9000/v1/logs",
  );
});

test("resolveEndpoint trims whitespace around an already-suffixed endpoint", () => {
  // The doubled-path bug: an untrimmed "https://h/v1/logs " made
  // endsWith("/v1/logs") false and appended /v1/logs again. After trim the
  // suffix is recognized and the value is returned unchanged.
  assert.equal(
    resolveEndpoint({ SEDIMENT_OTLP_ENDPOINT: "https://localhost:9000/v1/logs " }),
    "https://localhost:9000/v1/logs",
  );
  assert.equal(
    resolveEndpoint({ SEDIMENT_OTLP_ENDPOINT: " http://[::1]:9000/v1/logs " }),
    "http://[::1]:9000/v1/logs",
  );
});

test("resolveEndpoint treats a whitespace-only endpoint as not opted in", () => {
  // A pure-whitespace value trims to "" → null, so the rejection diagnostic
  // gate (SEDIMENT_OTLP_ENDPOINT?.trim() && !url) in register.ts fires.
  assert.equal(resolveEndpoint({ SEDIMENT_OTLP_ENDPOINT: " " }), null);
  assert.equal(resolveEndpoint({ SEDIMENT_OTLP_ENDPOINT: "\t" }), null);
  assert.equal(resolveEndpoint({ SEDIMENT_OTLP_ENDPOINT: "\n  \t" }), null);
  // Whitespace plus otherwise-rejected content still trims to a clean value
  // before validation, so it is the inner content (not the surrounding
  // whitespace) that decides acceptance.
  assert.equal(
    resolveEndpoint({ SEDIMENT_OTLP_ENDPOINT: " http://ingest:9000 " }),
    null,
  );
  assert.equal(
    resolveEndpoint({ SEDIMENT_OTLP_ENDPOINT: " nota url " }),
    null,
  );
});

test("resolveToken prefers the sediment token, then OTLP headers bearer", () => {
  assert.equal(resolveToken({}), null);
  assert.equal(resolveToken({ SEDIMENT_INGEST_TOKEN: "tok-1" }), "tok-1");
  assert.equal(
    resolveToken({ OTEL_EXPORTER_OTLP_HEADERS: "Authorization=Bearer%20tok-2" }),
    "tok-2",
  );
  assert.equal(
    resolveToken({
      SEDIMENT_INGEST_TOKEN: "tok-1",
      OTEL_EXPORTER_OTLP_HEADERS: "Authorization=Bearer%20tok-2",
    }),
    "tok-1",
  );
  assert.equal(
    resolveToken({ OTEL_EXPORTER_OTLP_HEADERS: "api-key=abc" }),
    null,
  );
});

test("resourceUserId reads user.id from OTEL_RESOURCE_ATTRIBUTES", () => {
  assert.equal(resourceUserId({}), null);
  assert.equal(
    resourceUserId({ OTEL_RESOURCE_ATTRIBUTES: "team=x,user.id=developer-1" }),
    "developer-1",
  );
  assert.equal(
    resourceUserId({ OTEL_RESOURCE_ATTRIBUTES: "team=x" }),
    null,
  );
});

test("decisionFromToolEnd: applied edit tools accept implicitly", () => {
  assert.deepEqual(decisionFromToolEnd("write", false), {
    decision: "accept",
    explicit: false,
  });
  assert.deepEqual(decisionFromToolEnd("edit", false), {
    decision: "accept",
    explicit: false,
  });
  // Errored executions never touched the file; non-edit tools are out of
  // contract scope. Both are absent, never guessed at.
  assert.equal(decisionFromToolEnd("edit", true), null);
  assert.equal(decisionFromToolEnd("read", false), null);
  assert.equal(decisionFromToolEnd("bash", false), null);
});

test("filePathFromArgs extracts path, degrades to empty string", () => {
  assert.equal(filePathFromArgs({ path: "/repo/a.py" }), "/repo/a.py");
  assert.equal(filePathFromArgs({}), "");
  assert.equal(filePathFromArgs({ path: 42 }), "");
  assert.equal(filePathFromArgs(undefined), "");
});
