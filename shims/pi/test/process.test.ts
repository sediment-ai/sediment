// SPDX-License-Identifier: MIT
// Real child processes exercise the default extension's failure boundaries.

import assert from "node:assert/strict";
import { execFile, spawn } from "node:child_process";
import { chmodSync, mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";
import test, { type TestContext } from "node:test";

const execute = promisify(execFile);
const extension = new URL("../index.ts", import.meta.url).href;
const privateText = "synthetic-payload-and-token-must-not-be-logged";

function fixture(t: TestContext, body: string) {
  const root = mkdtempSync(join(tmpdir(), "sediment-pi-process-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const bin = join(root, "bin");
  mkdirSync(bin);
  const program = join(bin, "sediment");
  writeFileSync(program, `#!${process.execPath}\n${body}\n`);
  chmodSync(program, 0o755);
  return { root, bin };
}

async function runExtension(
  root: string,
  env: Record<string, string>,
  setup = "",
  lifecycle = "session_shutdown",
) {
  // --no-warnings: the stderr assertions prove no private content is echoed;
  // a Node type-stripping ExperimentalWarning must not fail them.
  return execute(process.execPath, ["--no-warnings", "--input-type=module", "-e", `
    import sedimentPi from ${JSON.stringify(extension)};
    const handlers = new Map();
    sedimentPi({ on: (name, handler) => handlers.set(name, handler) });
    const ctx = {
      cwd: ${JSON.stringify(root)},
      sessionManager: {
        getSessionId: () => "session-synthetic",
        getSessionFile: () => ${JSON.stringify(join(root, "private-transcript.jsonl"))},
      },
    };
    ${setup}
    const event = {
      toolName: "write", toolCallId: "call-synthetic", isError: false,
      args: { path: ${JSON.stringify(join(root, "private-source.py"))} },
    };
    await handlers.get("tool_execution_start")(event, ctx);
    await handlers.get("tool_execution_end")(event, ctx);
    await handlers.get(${JSON.stringify(lifecycle)})({reason: "quit"}, ctx);
    console.log("completed");
  `], {
    cwd: root,
    env: {
      PATH: join(root, "bin"),
      SEDIMENT_OTLP_ENDPOINT: "",
      SEDIMENT_PI_TRANSCRIPTS: "1",
      ...env,
    },
    timeout: 15_000,
  });
}

test("spawn failure is visible for both capture channels without breaking pi", async (t) => {
  const { root } = fixture(t, "process.stdin.resume();");
  const scripts = join(root, "scripts");
  mkdirSync(scripts);
  writeFileSync(join(scripts, "sediment_attribution.py"), "");
  writeFileSync(join(scripts, "sediment_transcript.py"), "");
  const result = await runExtension(root, {
    PATH: "",
    SEDIMENT_SCRIPT_DIR: scripts,
    SEDIMENT_PYTHON: join(root, privateText),
  });
  assert.equal(result.stdout.trim(), "completed");
  assert.match(result.stderr, /channel=attribution reason=spawn_failed/);
  assert.match(result.stderr, /channel=transcript reason=spawn_failed/);
  assert.doesNotMatch(result.stderr, new RegExp(privateText));
  assert.ok(!result.stderr.includes(root));
});

test("nonzero capture exits report closed diagnostics and discard stderr content", async (t) => {
  const { root } = fixture(t, `
    process.stdin.resume();
    process.stdin.on("end", () => {
      process.stderr.write(${JSON.stringify(privateText)}, () => process.exit(23));
    });
  `);
  const result = await runExtension(root, {});
  assert.equal(result.stdout.trim(), "completed");
  assert.match(result.stderr, /channel=attribution reason=nonzero_exit/);
  assert.match(result.stderr, /channel=transcript reason=nonzero_exit/);
  assert.doesNotMatch(result.stderr, new RegExp(privateText));
  assert.ok(!result.stderr.includes(root));
});

test("failed real Decision POST remains visible while Attribution and shutdown run", async (t) => {
  const { root } = fixture(t, `
    const fs = require("node:fs");
    process.stdin.resume();
    process.stdin.on("end", () => {
      fs.appendFileSync(${JSON.stringify("calls.txt")}, process.argv[2] + "\\n");
    });
  `);
  const server = createServer((request, response) => {
    request.resume();
    response.writeHead(503);
    response.end(privateText);
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(() => new Promise<void>((resolve) => server.close(() => resolve())));
  const address = server.address();
  assert.ok(address && typeof address !== "string");
  const result = await runExtension(root, {
    SEDIMENT_OTLP_ENDPOINT: `http://127.0.0.1:${address.port}`,
    SEDIMENT_INGEST_TOKEN: privateText,
  });
  assert.equal(result.stdout.trim(), "completed");
  assert.equal(readFileSync(join(root, "calls.txt"), "utf8"), "mark\ntranscript\n");
  assert.match(result.stderr, /channel=decision reason=post_failed/);
  assert.doesNotMatch(result.stderr, new RegExp(privateText));
  assert.ok(!result.stderr.includes(root));
});

for (const lifecycle of ["session_shutdown", "agent_settled"]) {
  test(`${lifecycle} extraction failures are content-free and fail-soft`, async (t) => {
    const { root } = fixture(t, "process.stdin.resume();");
    const result = await runExtension(root, { SEDIMENT_EXTRACT_ON_SETTLE: "1" }, `
      ctx.sessionManager.getSessionFile = () => { throw new Error(${JSON.stringify(privateText)}); };
    `, lifecycle);
    assert.equal(result.stdout.trim(), "completed");
    assert.match(result.stderr, /channel=transcript reason=capture_failed/);
    assert.doesNotMatch(result.stderr, new RegExp(privateText));
  });
}

function stop(pid: number) {
  assert.ok(Number.isSafeInteger(pid) && pid > 1);
  try {
    process.kill(pid, "SIGKILL");
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ESRCH") throw error;
  }
}

function running(pid: number): boolean {
  assert.ok(Number.isSafeInteger(pid) && pid > 1);
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ESRCH") throw error;
    return false;
  }
}

async function assertStopped(pid: number) {
  const end = Date.now() + 2_000;
  while (running(pid) && Date.now() < end) {
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  assert.equal(running(pid), false, `owned process ${pid} survived capture failure`);
}

test("broken child stdin is classified without an uncaught stream error", async (t) => {
  const { root } = fixture(t, `
    const fs = require("node:fs");
    fs.writeFileSync("pid.txt", String(process.pid));
    fs.closeSync(0);
    setInterval(() => {}, 1000);
  `);
  const result = await runExtension(root, { SEDIMENT_PI_TRANSCRIPTS: "0" }, `
    ctx.sessionManager.getSessionId = () => ${JSON.stringify(privateText)}.repeat(500_000);
  `).catch((error) => ({ stdout: error.stdout as string, stderr: error.stderr as string }));
  const pid = Number(readFileSync(join(root, "pid.txt"), "utf8"));
  t.after(() => stop(pid));
  assert.equal(result.stdout.trim(), "completed");
  assert.match(result.stderr, /channel=attribution reason=stdin_failed/);
  assert.doesNotMatch(result.stderr, new RegExp(privateText));
  assert.doesNotMatch(result.stderr, /Unhandled|EPIPE|Error:/);
  await assertStopped(pid);
});

test("capture deadline kills owned descendants after their parent exits on TERM", async (t) => {
  const descendantCode = `
    const fs = require("node:fs");
    process.on("SIGTERM", () => {});
    fs.writeFileSync("descendant.txt", String(process.pid));
    setInterval(() => {}, 1000);
  `;
  const { root } = fixture(t, `
    const { spawn } = require("node:child_process");
    const fs = require("node:fs");
    fs.writeFileSync("parent.txt", String(process.pid));
    process.stdin.resume();
    process.on("SIGTERM", () => process.exit(0));
    spawn(process.execPath, ["-e", ${JSON.stringify(descendantCode)}], { stdio: "ignore" });
    setInterval(() => {}, 1000);
  `);
  const unrelated = spawn(process.execPath, ["-e", "setInterval(() => {}, 1000)"], {
    stdio: "ignore",
  });
  assert.ok(unrelated.pid);
  t.after(() => stop(unrelated.pid!));
  const started = Date.now();
  const result = await runExtension(root, { SEDIMENT_PI_TRANSCRIPTS: "0" })
    .catch((error) => ({ stdout: error.stdout as string, stderr: error.stderr as string }));
  const parent = Number(readFileSync(join(root, "parent.txt"), "utf8"));
  const descendant = Number(readFileSync(join(root, "descendant.txt"), "utf8"));
  t.after(() => { stop(parent); stop(descendant); });
  assert.equal(result.stdout.trim(), "completed");
  assert.match(result.stderr, /channel=attribution reason=timeout/);
  assert.ok(Date.now() - started < 13_000, "process exceeded the capture deadline");
  await assertStopped(parent);
  await assertStopped(descendant);
  assert.equal(running(unrelated.pid), true);
});

test("successful capture drains large stderr without echoing it or claiming delivery", async (t) => {
  const { root } = fixture(t, `
    process.stdin.resume();
    process.stdin.on("end", () => {
      process.stderr.write(${JSON.stringify(privateText)}.repeat(50_000));
    });
  `);
  const result = await runExtension(root, {});
  assert.equal(result.stdout.trim(), "completed");
  assert.equal(result.stderr, "");
});

test("spawn under descriptor exhaustion is classified without an uncaught exception", async () => {
  // Node returns from an EMFILE/ENFILE spawn before creating stdio streams and
  // emits "error" on the next tick; the runner must still settle fail-soft.
  const probe = `
    import fs from "node:fs";
    import { runCaptureProcess } from ${JSON.stringify(new URL("../lib/process.ts", import.meta.url).href)};
    const say = (line) => fs.writeSync(2, line + "\\n");
    process.on("uncaughtException", (error) => say("UNCAUGHT " + error.name));
    const held = [];
    try { for (;;) held.push(fs.openSync("/dev/null", "r")); } catch { say("EXHAUSTED"); }
    fs.closeSync(held.pop());
    try {
      await runCaptureProcess(process.execPath, ["-e", "0"], { synthetic: true });
      say("RESOLVED");
    } catch (error) { say("CAUGHT " + error.name + " " + error.message); }
    await new Promise((resolve) => setTimeout(resolve, 600));
    say("DONE");
  `;
  const { stderr } = await execute("sh", [
    "-c",
    'ulimit -n 64; exec "$0" --no-warnings --input-type=module -e "$1"',
    process.execPath,
    probe,
  ]);
  const lines = stderr.split("\n").filter((line) => line.length > 0);
  assert.ok(lines.includes("EXHAUSTED"), stderr);
  assert.ok(lines.includes("CAUGHT CaptureProcessError spawn_failed"), stderr);
  assert.ok(!lines.some((line) => line.startsWith("UNCAUGHT")), stderr);
  assert.equal(lines.at(-1), "DONE");
});
