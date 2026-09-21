// SPDX-License-Identifier: MIT
// Bound capture subprocesses without exposing their input or diagnostic output.

import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";

export type CaptureProcessReason =
  | "spawn_failed"
  | "stdin_failed"
  | "nonzero_exit"
  | "stdout_too_large"
  | "timeout"
  | "cleanup_failed";

export class CaptureProcessError extends Error {
  readonly reason: CaptureProcessReason;

  constructor(reason: CaptureProcessReason) {
    super(reason);
    this.name = "CaptureProcessError";
    this.reason = reason;
  }
}

const PROCESS_TIMEOUT_MS = 10_000;
const KILL_GRACE_MS = 250;
const MAX_STDOUT_BYTES = 16_384;

/** Successful exit means process completion, not delivered or persisted Facts. */
export function runCaptureProcess(
  executable: string,
  args: string[],
  input: unknown,
  captureOutput = false,
): Promise<string> {
  let payload: string;
  try {
    payload = JSON.stringify(input);
    if (payload === undefined) throw new Error();
  } catch {
    return Promise.reject(new CaptureProcessError("stdin_failed"));
  }

  return new Promise((resolve, reject) => {
    let child: ChildProcessWithoutNullStreams;
    try {
      child = spawn(executable, args, {
        // macOS/Linux: descendants share this owned process group. Windows
        // has only direct-child termination; no descendant guarantee there.
        detached: process.platform !== "win32",
        stdio: ["pipe", "pipe", "pipe"],
      });
    } catch {
      reject(new CaptureProcessError("spawn_failed"));
      return;
    }
    let finishing = false;
    let cleanupFailed = false;
    const output: Buffer[] = [];
    let outputBytes = 0;
    const deadline = setTimeout(() => finish("timeout"), PROCESS_TIMEOUT_MS);

    function signalOwned(signal: NodeJS.Signals): boolean {
      if (!child.pid) return false;
      try {
        if (process.platform === "win32") return child.kill(signal);
        process.kill(-child.pid, signal);
        return true;
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "ESRCH") cleanupFailed = true;
        return false;
      }
    }

    function finish(reason?: CaptureProcessReason) {
      if (finishing) return;
      finishing = true;
      clearTimeout(deadline);
      const settle = () => {
        // EMFILE/ENFILE spawns return before Node creates any stdio stream.
        child.stdin?.destroy();
        child.stdout?.destroy();
        child.stderr?.destroy();
        if (cleanupFailed) reject(new CaptureProcessError("cleanup_failed"));
        else if (reason) reject(new CaptureProcessError(reason));
        else resolve(Buffer.concat(output).toString("utf8"));
      };
      if (!signalOwned("SIGTERM")) {
        settle();
        return;
      }
      // Keep this timer even if the direct child closes after TERM. An owned
      // descendant can ignore TERM after its parent has already exited.
      setTimeout(() => {
        signalOwned("SIGKILL");
        settle();
      }, KILL_GRACE_MS);
    }

    // Listen first: a spawn that fails with EMFILE/ENFILE emits "error" on the
    // next tick and exposes no pid and no stdio streams.
    child.on("error", () => finish("spawn_failed"));
    child.on("close", (code) => finish(code === 0 ? undefined : "nonzero_exit"));
    if (!child.stdin || !child.stdout || !child.stderr) return;
    if (captureOutput) {
      child.stdout.on("data", (chunk: Buffer) => {
        if (finishing) return;
        outputBytes += chunk.length;
        if (outputBytes > MAX_STDOUT_BYTES) finish("stdout_too_large");
        else output.push(chunk);
      });
    } else child.stdout.resume();
    child.stderr.resume(); // Drain without retaining or echoing private content.
    child.stdin.on("error", () => finish("stdin_failed"));
    child.stdin.end(payload, (error?: Error | null) => {
      if (error) finish("stdin_failed");
    });
  });
}
