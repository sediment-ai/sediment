// SPDX-License-Identifier: MIT
// Event wiring for the sediment-pi extension: maps pi tool/session
// events onto the Sediment client capture contract. Side effects ride the
// Deps seam so the mapping is testable without network or processes.
//
// Fail-soft is the contract: a capture failure must never break the agent,
// so capture handlers report closed failure reasons without throwing into pi.
// Attribution remains independent evidence, never proof of another channel.

import { randomUUID } from "node:crypto";

import {
  buildDecisionRecord,
  buildLogsPayload,
  decisionFromToolEnd,
  filePathFromArgs,
  markCwd,
  resolveEndpoint,
  resolveToken,
  resourceUserId,
  EDIT_TOOLS,
  type Env,
} from "./contract.ts";
import {
  DEFAULT_PROVIDER_API,
  DEFAULT_PROVIDER_ID,
  SESSION_HEADER,
} from "./provider.ts";
import { CaptureProcessError } from "./process.ts";

function reportFailure(
  channel: "decision" | "attribution" | "transcript" | "delivery" | "completion",
  error: unknown,
  fallback:
    | "post_failed"
    | "capture_failed"
    | "enqueue_failed"
    | "helper_missing"
    | "invalid_acknowledgment"
    | "session_unavailable" = "capture_failed",
): void {
  const reason = error instanceof CaptureProcessError ? error.reason : fallback;
  console.error(`sediment-pi: capture_failed channel=${channel} reason=${reason}`);
}

export interface CaptureProgram {
  executable: string;
  arguments: string[];
}

export interface Deps {
  post: (url: string, payload: unknown, token: string | null) => Promise<void>;
  runScript: (
    script: string | CaptureProgram,
    args: string[],
    stdin: unknown,
    captureOutput?: boolean,
  ) => Promise<string | void>;
  /** Resolved Python script paths; each channel degrades independently. */
  scripts: {
    attribution: string | CaptureProgram | null;
    transcript: string | CaptureProgram | null;
    delivery?: string | CaptureProgram | null;
  } | null;
  env: Env;
  /** Current time in Unix ms (Date.now). */
  now: () => number;
}

export interface PiLike {
  on(event: string, handler: (event: any, ctx: any) => unknown): void;
}

function deliveryAcknowledgment(output: string | void, deliveryId: string): string | null | undefined {
  try {
    const result = JSON.parse(output ?? "");
    if (result === null || typeof result !== "object" || Array.isArray(result) ||
      result.format_version !== 1 || result.delivery_id !== deliveryId) return undefined;
    const keys = Object.keys(result).sort().join(",");
    if (keys === "delivery_id,format_version,reason,status" &&
      result.status === "queued" &&
      (result.reason === "buffered" || result.reason === "already_buffered")) return null;
    if (keys === "delivery_id,fallback_reason,format_version,reason,status" &&
      result.status === "acknowledged" && result.reason === "otlp_delivered" &&
      ["unsafe_storage", "storage_unavailable", "storage_busy"].includes(result.fallback_reason)) {
      return result.fallback_reason;
    }
  } catch {
    return undefined;
  }
}

export function register(pi: PiLike, deps: Deps): void {
  // toolCallId → the edited path observed at tool_execution_start (the end
  // event carries no args). Entries are deleted at execution end; a call that
  // never ends leaks one map entry — bounded by session size, acceptable.
  const pending = new Map<string, string>();

  let rejectedEndpointReported = false;
  let drainStarted = false;

  pi.on("session_start", async () => {
    if (
      drainStarted || !resolveEndpoint(deps.env) ||
      !deps.env.SEDIMENT_DELIVERY_DIR?.trim()
    ) return;
    drainStarted = true;
    if (!deps.scripts?.delivery) {
      reportFailure("delivery", undefined, "helper_missing");
      return;
    }
    try {
      // The separately enrolled worker owns continued replay. This startup
      // attempt is bounded by the same process deadline as other channels.
      await deps.runScript(deps.scripts.delivery, ["replay"], {});
    } catch (error) {
      reportFailure("delivery", error);
    }
  });

  const providerId = deps.env.SEDIMENT_PROVIDER_ID ?? DEFAULT_PROVIDER_ID;
  const providerApi = deps.env.SEDIMENT_PROVIDER_API ?? DEFAULT_PROVIDER_API;
  pi.on("before_provider_headers", (event, ctx) => {
    try {
      if (ctx.model?.provider !== providerId || ctx.model.api !== providerApi) return;
      // Remove every spelling of a previous Session before reading fresh
      // evidence. Unavailable identity must never leave a stale header behind.
      for (const name of Object.keys(event.headers)) {
        if (name.toLowerCase() === SESSION_HEADER) delete event.headers[name];
      }
      const sessionId: unknown = ctx.sessionManager.getSessionId();
      if (
        typeof sessionId !== "string" || !sessionId ||
        sessionId.trim() !== sessionId || !/^[\x20-\x7e]+$/.test(sessionId)
      ) {
        reportFailure("completion", undefined, "session_unavailable");
        return;
      }
      event.headers[SESSION_HEADER] = sessionId;
    } catch {
      reportFailure("completion", undefined, "session_unavailable");
    }
  });

  pi.on("tool_execution_start", (event) => {
    try {
      if (!EDIT_TOOLS.has(event.toolName)) return;
      pending.set(event.toolCallId, filePathFromArgs(event.args));
    } catch {
      // fail-soft
    }
  });

  pi.on("tool_execution_end", async (event, ctx) => {
    try {
      // "" is a real value here (path unobservable), so test for presence.
      const filePath = pending.get(event.toolCallId);
      if (filePath === undefined) return;
      pending.delete(event.toolCallId);

      // Prepare and enqueue or send the Decision before the spawned stamper
      // process gets a chance to delay or lose it.
      const outcome = decisionFromToolEnd(event.toolName, event.isError === true);
      const url = resolveEndpoint(deps.env);
      if (
        deps.env.SEDIMENT_OTLP_ENDPOINT?.trim() &&
        !url &&
        !rejectedEndpointReported
      ) {
        console.error(
          "sediment-pi: configured ingest endpoint rejected; remote " +
            "endpoints require HTTPS and HTTP is limited to literal " +
            "loopback hosts",
        );
        rejectedEndpointReported = true;
      }
      if (outcome && url) {
        const buffered = Boolean(deps.env.SEDIMENT_DELIVERY_DIR?.trim());
        // Own try: channels degrade independently. A failed enqueue/post must not
        // skip the attribution mark below — the mark is local and feeds the
        // notes/jaccard backstop, the very path that covers an ingest outage.
        try {
          const observedMs = deps.now();
          const record = buildDecisionRecord({
            agent: "pi",
            sessionId: ctx.sessionManager.getSessionId(),
            callId: event.toolCallId,
            toolName: event.toolName,
            decision: outcome.decision,
            explicit: outcome.explicit,
            filePath,
            // BigInt: Date.now()*1e6 overflows double integer precision.
            timeUnixNano: (BigInt(observedMs) * 1_000_000n).toString(),
          });
          const userId = resourceUserId(deps.env);
          const payload = buildLogsPayload(
            [record],
            userId ? { "user.id": userId } : {},
          );
          if (buffered) {
            if (!deps.scripts?.delivery) {
              reportFailure("decision", undefined, "helper_missing");
            } else {
              const deliveryId = randomUUID();
              const output = await deps.runScript(
                deps.scripts.delivery, ["enqueue", "--fallback-direct"], {
                  channel: "otlp",
                  destination: url,
                  body_base64: Buffer.from(JSON.stringify(payload)).toString("base64"),
                  captured_at: new Date(observedMs).toISOString(),
                  delivery_id: deliveryId,
                }, true,
              );
              const fallbackReason = deliveryAcknowledgment(output, deliveryId);
              if (fallbackReason === undefined) {
                reportFailure("decision", undefined, "invalid_acknowledgment");
              } else if (fallbackReason !== null) {
                console.error(
                  `sediment-pi: delivery_mode channel=decision mode=best_effort reason=${fallbackReason}`,
                );
              }
            }
          } else {
            await deps.post(url, payload, resolveToken(deps.env));
          }
        } catch (error) {
          reportFailure("decision", error, buffered ? "enqueue_failed" : "post_failed");
        }
      }

      // Attribution (PostToolUse parity): the session marker fires on every
      // edit-tool execution end, errored or not — the session worked in this
      // repo either way.
      if (deps.scripts?.attribution) {
        await deps.runScript(deps.scripts.attribution, ["mark", "--tool", "pi"], {
          session_id: ctx.sessionManager.getSessionId(),
          cwd: markCwd(filePath, ctx.cwd),
        });
      }
    } catch (error) {
      reportFailure("attribution", error);
    }
  });

  // Ship the session's edit observations. The transcript wire carries applied
  // text and observed file text. It reads each edited file at extraction time,
  // so the caller decides which observation closes the session. The facts
  // dedup first-write-wins on
  // (org, agent harness, session, call_id), meaning an
  // early extraction wins over the real one rather than being corrected by it.
  async function extractTranscript(ctx: any): Promise<void> {
    // Decision enrollment doesn't authorize sending applied or observed text.
    if (deps.env.SEDIMENT_PI_TRANSCRIPTS !== "1") return;
    if (!deps.scripts?.transcript) return;
    const sessionFile = ctx.sessionManager.getSessionFile();
    if (!sessionFile) return; // ephemeral session — nothing to parse
    await deps.runScript(deps.scripts.transcript, ["--agent", "pi"], {
      session_id: ctx.sessionManager.getSessionId(),
      transcript_path: sessionFile,
    });
  }

  // A long-lived host (the fleet's pi-acp pod) finishes the task and holds
  // the session open, so session_shutdown never arrives and the pairs never
  // ship — decisions and inference calls land, edit retention does not.
  // agent_settled
  // is the task-complete signal such a host does have.
  //
  // Opt-in, because "settled" only equals "final" when the session is
  // one-shot. An interactive session settles once per agent run and keeps
  // going; extracting there would freeze an early state that first-write-wins
  // then makes permanent. Hosts that run a session per task set this.
  if (deps.env.SEDIMENT_EXTRACT_ON_SETTLE) {
    pi.on("agent_settled", async (_event, ctx) => {
      try {
        await extractTranscript(ctx);
      } catch (error) {
        reportFailure("transcript", error);
      }
    });
  }

  pi.on("session_shutdown", async (event, ctx) => {
    try {
      // "reload" tears down the extension runtime, not the session — the
      // file keeps growing, so pairing now would freeze a non-final state
      // (and first-write-wins would block the real one). Every other reason
      // ends this runtime's claim on the session.
      if (event.reason === "reload") return;
      await extractTranscript(ctx);
    } catch (error) {
      reportFailure("transcript", error);
    }
  });
}
