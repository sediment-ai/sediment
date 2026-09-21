// SPDX-License-Identifier: MIT
// The Sediment client capture contract (docs/agents/capture-clients.md) —
// pure builders and env resolution. No I/O here; the wiring lives in
// lib/register.ts. The shapes emitted here are pinned against the golden
// server-side conformance fixture
// (packages/capture/tests/fixtures/otlp/sediment/tool_decision.json).

import { dirname } from "node:path";

export const DECISION_EVENT = "sediment.tool_decision";

// The contract scopes decisions to edit tools; reads stay out.
export const EDIT_TOOLS: ReadonlySet<string> = new Set(["edit", "write"]);

export type AnyValue = { stringValue: string } | { boolValue: boolean };

export interface OtlpAttribute {
  key: string;
  value: AnyValue;
}

export interface OtlpRecord {
  body: { stringValue: string };
  timeUnixNano: string;
  attributes: OtlpAttribute[];
}

export interface LogsPayload {
  resourceLogs: [
    {
      resource: { attributes: OtlpAttribute[] };
      scopeLogs: [{ logRecords: OtlpRecord[] }];
    },
  ];
}

export interface DecisionInput {
  agent: string;
  sessionId: string;
  callId: string;
  toolName: string;
  decision: "accept" | "reject";
  explicit: boolean;
  filePath: string;
  timeUnixNano: string;
}

export function buildDecisionRecord(input: DecisionInput): OtlpRecord {
  const strings: Record<string, string> = {
    agent: input.agent,
    "session.id": input.sessionId,
    tool_use_id: input.callId,
    tool_name: input.toolName,
    decision: input.decision,
    file_path: input.filePath,
  };
  const attributes: OtlpAttribute[] = Object.entries(strings).map(
    ([key, value]) => ({ key, value: { stringValue: value } }),
  );
  // explicit must stay a real bool on the wire — the server requires it.
  attributes.push({ key: "explicit", value: { boolValue: input.explicit } });
  return {
    body: { stringValue: DECISION_EVENT },
    timeUnixNano: input.timeUnixNano,
    attributes,
  };
}

export function buildLogsPayload(
  records: OtlpRecord[],
  resourceAttrs: Record<string, string>,
): LogsPayload {
  return {
    resourceLogs: [
      {
        resource: {
          attributes: Object.entries(resourceAttrs).map(([key, value]) => ({
            key,
            value: { stringValue: value },
          })),
        },
        scopeLogs: [{ logRecords: records }],
      },
    ],
  };
}

export interface Env {
  [key: string]: string | undefined;
}

/**
 * The ingest endpoint, or null when not opted in. Deliberately NO
 * OTEL_EXPORTER_OTLP_ENDPOINT fallback: /v1/logs is the standard OTLP path,
 * so any collector in the machine's generic telemetry config would silently
 * accept decision records bound for Sediment. Explicit opt-in only (same
 * posture as the Python clients).
 */
export function resolveEndpoint(env: Env): string | null {
  // WHATWG URL parsing tolerates surrounding whitespace, so trim before the
  // slash-strip — mirrors the Python sibling (cli/sediment_cli/transcript.py).
  const base = (env.SEDIMENT_OTLP_ENDPOINT ?? "").trim().replace(/\/+$/, "");
  if (!base) return null;
  let endpoint: URL;
  try {
    endpoint = new URL(base);
  } catch {
    return null;
  }
  const host = endpoint.hostname.toLowerCase();
  const ipv4Loopback = /^127(?:\.\d{1,3}){3}$/.test(host);
  const loopback =
    host === "localhost" || host === "::1" || host === "[::1]" || ipv4Loopback;
  if (
    !["http:", "https:"].includes(endpoint.protocol) ||
    endpoint.username ||
    endpoint.password ||
    endpoint.search ||
    endpoint.hash ||
    !["/", "/v1/logs"].includes(endpoint.pathname) ||
    (endpoint.protocol === "http:" && !loopback)
  ) {
    return null;
  }
  return base.endsWith("/v1/logs") ? base : `${base}/v1/logs`;
}

/** Pairs of an OTLP-spec list: `Key=url-encoded-value,Key2=...`. */
function* pairs(list: string | undefined): Generator<[string, string]> {
  for (const part of (list ?? "").split(",")) {
    const eq = part.indexOf("=");
    if (eq >= 0) yield [part.slice(0, eq).trim(), part.slice(eq + 1).trim()];
  }
}

export function resolveToken(env: Env): string | null {
  const token = env.SEDIMENT_INGEST_TOKEN;
  if (token) return token;
  // Safe to read the machine's generic OTLP headers here: the token is only
  // ever sent to the explicit sediment endpoint above.
  for (const [key, value] of pairs(env.OTEL_EXPORTER_OTLP_HEADERS)) {
    if (key.toLowerCase() !== "authorization") continue;
    const decoded = decodeURIComponent(value);
    const space = decoded.indexOf(" ");
    const scheme = space < 0 ? decoded : decoded.slice(0, space);
    const credential = space < 0 ? "" : decoded.slice(space + 1).trim();
    if (scheme.toLowerCase() === "bearer" && credential) return credential;
  }
  return null;
}

export function resourceUserId(env: Env): string | null {
  for (const [key, value] of pairs(env.OTEL_RESOURCE_ATTRIBUTES)) {
    if (key === "user.id" && value) return value;
  }
  return null;
}

/**
 * The decision a tool_execution_end carries, or null when there is none to
 * emit. Stock pi has no human approval gesture, so an applied edit is an
 * implicit accept; an errored execution never touched the file and emits
 * nothing (absent, never guessed at).
 */
export function decisionFromToolEnd(
  toolName: string,
  isError: boolean,
): { decision: "accept"; explicit: false } | null {
  if (!EDIT_TOOLS.has(toolName) || isError) return null;
  return { decision: "accept", explicit: false };
}

/** file_path from tool args; "" when unobservable (the reject convention). */
export function filePathFromArgs(args: unknown): string {
  if (typeof args !== "object" || args === null) return "";
  const path = (args as Record<string, unknown>).path;
  return typeof path === "string" ? path : "";
}

/**
 * The cwd `mark` should run in: the edited file's directory when the path is
 * absolute, else the session cwd. ACP agents edit clones beneath the
 * workspace root — the session cwd is not a git repo, so `mark` there heals
 * nothing. Any directory inside the repo works: git walks up to the repo
 * root.
 */
export function markCwd(filePath: string, sessionCwd: string): string {
  return filePath.startsWith("/") ? dirname(filePath) : sessionCwd;
}
