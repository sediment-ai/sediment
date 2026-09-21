// SPDX-License-Identifier: MIT
// Public extension -> installed Python helper -> real HTTP receiver.

import assert from "node:assert/strict";
import { execFile, execFileSync } from "node:child_process";
import { chmodSync, copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { delimiter, join } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import test, { type TestContext } from "node:test";

const execute = promisify(execFile);
const extension = new URL("../index.ts", import.meta.url).href;
const implementation = fileURLToPath(new URL("../../../cli/sediment_cli/delivery.py", import.meta.url));
const python = process.env.SEDIMENT_PI_TEST_PYTHON ?? execFileSync("python3", ["-c", "import sys; print(sys.executable)"], { encoding: "utf8" }).trim();
const observedMs = 1789171200123;
const privateText = "synthetic-private-source-and-token";

function fixture(t: TestContext) {
  const root = mkdtempSync(join(tmpdir(), "sediment-pi-delivery-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const scripts = join(root, "scripts");
  mkdirSync(scripts);
  copyFileSync(implementation, join(scripts, "sediment_delivery.py"));
  writeFileSync(join(scripts, "sediment_attribution.py"), `import sys\nfrom pathlib import Path\nsys.stdin.read()\nPath("marked").write_text("yes")\n`);
  writeFileSync(join(scripts, "sediment_transcript.py"), `raise RuntimeError("content consent disabled")\n`);
  return { root, scripts, directory: join(root, "queue") };
}

async function runExtension(
  root: string,
  env: Record<string, string>,
  { startup = false, tool = true } = {},
) {
  return execute(process.execPath, ["--input-type=module", "-e", `
    import sedimentPi from ${JSON.stringify(extension)};
    Date.now = () => ${observedMs};
    const handlers = new Map();
    sedimentPi({ on: (name, handler) => handlers.set(name, handler) });
    const ctx = {
      cwd: ${JSON.stringify(root)},
      sessionManager: {
        getSessionId: () => "session-original",
        getSessionFile: () => { throw new Error("disabled content read"); },
      },
    };
    if (${startup}) await handlers.get("session_start")?.({reason: "startup"}, ctx);
    if (${tool}) {
      const event = { toolName: "write", toolCallId: "call-original", isError: false,
        args: { path: "source.py", content: ${JSON.stringify(privateText)} } };
      await handlers.get("tool_execution_start")(event, ctx);
      await handlers.get("tool_execution_end")(event, ctx);
    }
    await handlers.get("session_shutdown")({reason: "quit"}, ctx);
    console.log("completed");
  `], {
    cwd: root,
    env: {
      PATH: "", HOME: root,
      SEDIMENT_SCRIPT_DIR: join(root, "scripts"), SEDIMENT_PYTHON: python,
      SEDIMENT_OTLP_ENDPOINT: "", SEDIMENT_INGEST_TOKEN: privateText,
      SEDIMENT_PI_TRANSCRIPTS: "0", ...env,
    },
    timeout: 15_000,
  });
}

async function receiver(t: TestContext, response = "{}", status = 200, hold = false) {
  const bodies: Buffer[] = [];
  const server = createServer(async (request, res) => {
    const chunks: Buffer[] = [];
    for await (const chunk of request) chunks.push(Buffer.from(chunk));
    bodies.push(Buffer.concat(chunks));
    assert.equal(request.url, "/v1/logs");
    if (hold) return;
    res.writeHead(status, { "Content-Type": "application/json" });
    res.end(response);
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise<void>((resolve) => {
    server.closeAllConnections();
    server.close(() => resolve());
  }));
  const address = server.address();
  assert.ok(address && typeof address !== "string");
  return { bodies, endpoint: `http://127.0.0.1:${address.port}` };
}

for (const installed of [false, true]) {
  test(`storage fault uses visible direct fallback through ${installed ? "installed wheel" : "standalone helper"}`, {
    skip: installed && !process.env.SEDIMENT_PI_TEST_INSTALLED_BIN ? "set SEDIMENT_PI_TEST_INSTALLED_BIN for installed wheel acceptance" : false,
  }, async (t) => {
    const { root, directory } = fixture(t);
    mkdirSync(directory);
    chmodSync(directory, 0o755);
    if (installed) execFileSync("git", ["init", "-q", root], {
      env: { PATH: process.env.PATH, HOME: root, GIT_CONFIG_NOSYSTEM: "1" },
    });
    const { endpoint, bodies } = await receiver(t);
    const result = await runExtension(root, {
      SEDIMENT_OTLP_ENDPOINT: endpoint, SEDIMENT_DELIVERY_DIR: directory,
      ...(installed ? { PATH: process.env.SEDIMENT_PI_TEST_INSTALLED_BIN! + delimiter + process.env.PATH } : {}),
    });
    assert.equal(bodies.length, 1, "storage failure must still attempt bounded delivery");
    assert.match(result.stderr, /delivery_mode channel=decision mode=best_effort reason=unsafe_storage/);
    assert.doesNotMatch(result.stderr, /capture_failed|synthetic-private-source-and-token/);
    assert.deepEqual(readdirSync(directory), []);
    const [record] = JSON.parse(bodies[0].toString()).resourceLogs[0].scopeLogs[0].logRecords;
    assert.equal(record.timeUnixNano, "1789171200123000000");
    assert.deepEqual(record.attributes.find((attr: any) => attr.key === "tool_use_id").value, { stringValue: "call-original" });
    assert.deepEqual(record.attributes.find((attr: any) => attr.key === "session.id").value, { stringValue: "session-original" });
    if (installed) {
      const marker = JSON.parse(readFileSync(join(root, ".git", "sediment-sessions"), "utf8"));
      assert.equal(marker.session_id, "session-original");
      assert.equal(marker.tool, "pi");
    } else {
      assert.equal(readFileSync(join(root, "marked"), "utf8"), "yes");
    }
  });

  test(`buffered Decision survives restart through ${installed ? "installed wheel command" : "standalone helper copy"}`, {
    skip: installed && !process.env.SEDIMENT_PI_TEST_INSTALLED_BIN ? "set SEDIMENT_PI_TEST_INSTALLED_BIN for installed wheel acceptance" : false,
  }, async (t) => {
    const { root, directory } = fixture(t);
    const { endpoint, bodies } = await receiver(t);
    const env = {
      SEDIMENT_OTLP_ENDPOINT: endpoint, SEDIMENT_DELIVERY_DIR: directory,
      ...(installed ? { PATH: process.env.SEDIMENT_PI_TEST_INSTALLED_BIN! } : {}),
    };
    const first = await runExtension(root, env);
    assert.equal(first.stdout.trim(), "completed");
    assert.doesNotMatch(first.stderr, /channel=decision/);
    assert.equal(bodies.length, 0, "enqueue must not wait for network delivery");
    const entries = readdirSync(directory).filter((name) => name.endsWith(".entry"));
    assert.equal(entries.length, 1);
    const entry = readFileSync(join(directory, entries[0]));
    const separator = entry.indexOf(10);
    const header = JSON.parse(entry.subarray(0, separator).toString());
    const body = entry.subarray(separator + 1);
    assert.match(header.delivery_id, /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/);
    assert.equal(Date.parse(header.captured_at), observedMs);
    const [record] = JSON.parse(body.toString()).resourceLogs[0].scopeLogs[0].logRecords;
    assert.equal(record.timeUnixNano, "1789171200123000000");
    assert.deepEqual(record.attributes.find((attr: any) => attr.key === "tool_use_id").value, { stringValue: "call-original" });
    assert.doesNotMatch(body.toString(), new RegExp(privateText));
    const restarted = await runExtension(root, env, { startup: true, tool: false });
    assert.equal(restarted.stdout.trim(), "completed");
    assert.equal(bodies.length, 1);
    assert.deepEqual(bodies[0], body, "replay must send the exact prepared body");
    assert.equal(existsSync(join(directory, entries[0])), false);
    const receipt = JSON.parse(readFileSync(join(directory, `${header.delivery_id}.receipt`), "utf8"));
    assert.equal(receipt.status, "acknowledged");
    assert.equal(receipt.reason, "otlp_delivered");
  });
}

test("storage fault with failed direct acknowledgment stays visibly failed", async (t) => {
  const { root, directory } = fixture(t);
  mkdirSync(directory);
  chmodSync(directory, 0o755);
  const { endpoint, bodies } = await receiver(t, '{"partialSuccess":{}}');
  const result = await runExtension(root, { SEDIMENT_OTLP_ENDPOINT: endpoint, SEDIMENT_DELIVERY_DIR: directory });
  assert.equal(bodies.length, 1);
  assert.match(result.stderr, /capture_failed channel=decision reason=nonzero_exit/);
  assert.deepEqual(readdirSync(directory), []);
  assert.equal(readFileSync(join(root, "marked"), "utf8"), "yes");
});

for (const mutation of ["empty", "wrong-id", "wrong-version", "wrong-status", "wrong-reason", "extra-field", "unknown-fallback", "false-durable", "oversized", "nonzero"]) {
  test(`enqueue ${mutation} is visible and does not suppress Attribution`, async (t) => {
    const { root, scripts, directory } = fixture(t);
    const { endpoint } = await receiver(t);
    writeFileSync(join(scripts, "sediment_delivery.py"), `
import json, sys
request = json.load(sys.stdin)
result = {"format_version": 1, "delivery_id": request["delivery_id"], "status": "queued", "reason": "buffered"}
mutation = ${JSON.stringify(mutation)}
if mutation == "wrong-id": result["delivery_id"] = "00000000-0000-4000-8000-000000000000"
if mutation == "wrong-version": result["format_version"] = 2
if mutation == "wrong-status": result["status"] = "acknowledged"
if mutation == "wrong-reason": result["reason"] = "unknown"
if mutation == "extra-field": result["private"] = ${JSON.stringify(privateText)}
if mutation == "unknown-fallback": result.update(status="acknowledged", reason="otlp_delivered", fallback_reason=${JSON.stringify(privateText)})
if mutation == "false-durable": result["fallback_reason"] = "unsafe_storage"
if mutation == "nonzero": sys.exit(1)
if mutation == "oversized": print(${JSON.stringify(privateText)} * 10000)
elif mutation != "empty": print(json.dumps(result))
`);
    const result = await runExtension(root, { SEDIMENT_OTLP_ENDPOINT: endpoint, SEDIMENT_DELIVERY_DIR: directory });
    assert.equal(result.stdout.trim(), "completed");
    assert.match(result.stderr, /channel=decision reason=(invalid_acknowledgment|stdout_too_large|nonzero_exit)/);
    assert.doesNotMatch(result.stderr, new RegExp(privateText));
    assert.equal(readFileSync(join(root, "marked"), "utf8"), "yes");
  });
}

test("matching already-buffered acknowledgment allows Attribution to continue", async (t) => {
  const { root, scripts, directory } = fixture(t);
  const { endpoint } = await receiver(t);
  writeFileSync(join(scripts, "sediment_delivery.py"), `
import json, sys
request = json.load(sys.stdin)
print(json.dumps({"format_version": 1, "delivery_id": request["delivery_id"], "status": "queued", "reason": "already_buffered"}))
`);
  const result = await runExtension(root, { SEDIMENT_OTLP_ENDPOINT: endpoint, SEDIMENT_DELIVERY_DIR: directory });
  assert.doesNotMatch(result.stderr, /channel=decision/);
  assert.equal(readFileSync(join(root, "marked"), "utf8"), "yes");
});

test("startup drain timeout leaves every prepared body intact and pi continues", async (t) => {
  const { root, directory } = fixture(t);
  const { endpoint, bodies } = await receiver(t, "{}", 200, true);
  const env = { SEDIMENT_OTLP_ENDPOINT: endpoint, SEDIMENT_DELIVERY_DIR: directory };
  for (let index = 0; index < 3; index++) await runExtension(root, env);
  const entries = readdirSync(directory).filter((name) => name.endsWith(".entry"));
  assert.equal(entries.length, 3);
  const prepared = entries.map((name) => readFileSync(join(directory, name)));
  rmSync(join(root, "marked"));
  const started = performance.now();
  const result = await runExtension(root, env, { startup: true });
  assert.match(result.stderr, /channel=delivery reason=timeout/);
  assert.equal(result.stdout.trim(), "completed");
  assert.ok(performance.now() - started < 13_000);
  assert.ok(bodies.length >= 1);
  assert.deepEqual(entries.map((name) => readFileSync(join(directory, name))), prepared);
  assert.equal(readFileSync(join(root, "marked"), "utf8"), "yes");
});

test("missing delivery helper is visible and does not fall back to network or suppress Attribution", async (t) => {
  const { root, scripts, directory } = fixture(t);
  rmSync(join(scripts, "sediment_delivery.py"));
  const { endpoint, bodies } = await receiver(t);
  const result = await runExtension(root, { SEDIMENT_OTLP_ENDPOINT: endpoint, SEDIMENT_DELIVERY_DIR: directory });
  assert.match(result.stderr, /channel=decision reason=helper_missing/);
  assert.equal(bodies.length, 0);
  assert.equal(readFileSync(join(root, "marked"), "utf8"), "yes");
});

for (const endpoint of ["", "http://remote.invalid"]) {
  test(`absent or rejected endpoint ${JSON.stringify(endpoint)} never invokes delivery or content capture`, async (t) => {
    const { root, scripts, directory } = fixture(t);
    writeFileSync(join(scripts, "sediment_delivery.py"), `from pathlib import Path\nPath("delivery-read").write_text("bad")\n`);
    const result = await runExtension(root, { SEDIMENT_OTLP_ENDPOINT: endpoint, SEDIMENT_DELIVERY_DIR: directory }, { startup: true });
    assert.equal(result.stdout.trim(), "completed");
    assert.equal(existsSync(directory), false);
    assert.equal(existsSync(join(root, "delivery-read")), false);
    assert.doesNotMatch(result.stderr, /content|transcript/);
  });
}

for (const [status, body] of [[200, "{}"], [200, "[]"], [200, ""], [200, '{"partialSuccess":{}}'], [201, "{}"], [200, " ".repeat(20_000) + "{}"]] as const) {
  test(`direct OTLP acknowledgment status=${status} body=${body.slice(0, 20)}`, async (t) => {
    const { root, directory } = fixture(t);
    const { endpoint } = await receiver(t, body, status);
    const result = await runExtension(root, { SEDIMENT_OTLP_ENDPOINT: endpoint, SEDIMENT_DELIVERY_DIR: "   " }, { startup: true });
    if (status === 200 && body === "{}") assert.doesNotMatch(result.stderr, /channel=decision/);
    else assert.match(result.stderr, /channel=decision reason=post_failed/);
    assert.equal(existsSync(directory), false);
    assert.equal(readFileSync(join(root, "marked"), "utf8"), "yes");
  });
}
