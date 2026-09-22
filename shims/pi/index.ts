// SPDX-License-Identifier: MIT
// sediment-pi — the pi-harness shim for Sediment capture.
//
// Closes pi's capture gaps client-side, per docs/agents/capture-clients.md:
//   decisions    — edit-tool executions emit sediment.tool_decision records
//                  to $SEDIMENT_OTLP_ENDPOINT/v1/logs, through the Python
//                  delivery helper when buffering is enrolled (accepted,
//                  explicit=false: stock pi has no human approval gesture)
//   attribution  — edit-tool executions invoke sediment_attribution.py
//                  `mark --tool pi` (the stamper is tool-agnostic)
//   transcripts  — session_shutdown invokes sediment_transcript.py
//                  `--agent pi` (the pair is the fact; ADR 0007)
//   completions  — the native provider-header event attaches the current
//                  Session to requests for the configured gateway
//
// All capture is best-effort: a failure degrades the signal to the
// notes/jaccard backstop and must never break the agent.

import { accessSync, constants, existsSync } from "node:fs";
import { delimiter, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  register,
  type CaptureProgram,
  type Deps,
  type PiLike,
} from "./lib/register.ts";
import { runCaptureProcess } from "./lib/process.ts";
import { registerRetrieval } from "./lib/retrieval.ts";

const POST_TIMEOUT_MS = 10_000;
const MAX_ACKNOWLEDGMENT_BYTES = 16_384;

async function post(
  url: string,
  payload: unknown,
  token: string | null,
): Promise<void> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(url, {
    method: "POST",
    headers,
    body: JSON.stringify(payload),
    redirect: "error",
    signal: AbortSignal.timeout(POST_TIMEOUT_MS),
  });
  if (response.status !== 200 || !response.body) {
    await response.body?.cancel();
    throw new Error("invalid_acknowledgment");
  }
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let bytes = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      bytes += value.length;
      if (bytes > MAX_ACKNOWLEDGMENT_BYTES) {
        throw new Error("invalid_acknowledgment");
      }
      chunks.push(value);
    }
    const acknowledgment = JSON.parse(
      new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks)),
    );
    // OTLP acknowledgment describes delivery, never the number of Facts.
    if (
      !acknowledgment || Array.isArray(acknowledgment) ||
      typeof acknowledgment !== "object" || Object.keys(acknowledgment).length !== 0
    ) {
      throw new Error("invalid_acknowledgment");
    }
  } finally {
    await reader.cancel();
  }
}

function runScript(
  script: string | CaptureProgram,
  args: string[],
  stdin: unknown,
  captureOutput = false,
): Promise<string> {
  // ponytail: PATH python3 (overridable via SEDIMENT_PYTHON) — the scripts
  // are stdlib-only, so any 3.12+ interpreter runs them; the stamper's hook
  // fragments pin sys.executable instead because hooks fire in bare envs.
  const python = process.env.SEDIMENT_PYTHON ?? "python3";
  const executable = typeof script === "string" ? python : script.executable;
  const commandArgs =
    typeof script === "string"
      ? [script, ...args]
      : [...script.arguments, ...args];
  return runCaptureProcess(executable, commandArgs, stdin, captureOutput);
}

function findSediment(): string | null {
  for (const dir of (process.env.PATH ?? "").split(delimiter)) {
    if (!dir) continue;
    const candidate = join(dir, "sediment");
    try {
      accessSync(candidate, constants.X_OK);
      return candidate;
    } catch {
      // Try the next PATH entry.
    }
  }
  return null;
}

function resolveScripts(): Deps["scripts"] {
  const sediment = findSediment();
  if (sediment) {
    return {
      attribution: { executable: sediment, arguments: [] },
      transcript: { executable: sediment, arguments: ["transcript"] },
      delivery: { executable: sediment, arguments: ["delivery"] },
    };
  }
  // Default: installed in place, <repo>/shims/pi/index.ts → <repo>/scripts.
  const dir =
    process.env.SEDIMENT_SCRIPT_DIR ||
    join(dirname(fileURLToPath(import.meta.url)), "..", "..", "scripts");
  const attribution = join(dir, "sediment_attribution.py");
  const transcript = join(dir, "sediment_transcript.py");
  const delivery = join(dir, "sediment_delivery.py");
  const resolved = {
    attribution: existsSync(attribution) ? attribution : null,
    transcript: existsSync(transcript) ? transcript : null,
    delivery: existsSync(delivery) ? delivery : null,
  };
  if (!resolved.attribution)
    console.error("sediment-pi: attribution capture degraded (script not found)");
  if (!resolved.transcript)
    console.error("sediment-pi: transcript capture degraded (script not found)");
  return resolved.attribution || resolved.transcript || resolved.delivery
    ? resolved : null;
}

export default function (pi: PiLike & Parameters<typeof registerRetrieval>[0]) {
  const version = /^(22|24)\.(\d+)\.\d+$/.exec(process.versions.node);
  if (!version || (version[1] === "22" && Number(version[2]) < 18)) {
    console.error("sediment-pi: unsupported Node runtime; use Node 24 or Node 22.18+");
    return;
  }
  registerRetrieval(pi, process.env);
  register(pi, {
    post,
    runScript,
    scripts: resolveScripts(),
    env: process.env,
    now: Date.now,
  });
}
