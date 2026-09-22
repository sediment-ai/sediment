// SPDX-License-Identifier: MIT
export const source = "unranked-session";
export const callId = "requirements-call";
export const when = "2026-09-22T12:00:00+00:00";
export const requirement = "Requirements: Invoice identifiers must remain exact integers.";
export const failure = "Failed attempt: converting identifiers to floats changed the receipt.";
export const largeInteger = "1" + "0".repeat(399) + "7";
export const ref = (part_index: number, side: "input" | "output" = "output") => ({ inference_call_id: callId, side, message_index: 0, part_index });
export const references = [ref(2), ref(0, "input"), ref(0), ref(1), ref(3)];
export function grantBody() { return { org_id: "acme", version: "0.1.0", authority: "retrieval", client_id: "retrieval", source_session_ids: ["ranked-session", source] }; }
export function callMetadata() { return { inference_call_id: callId, observed_at: when, model_provider: null, model: "synthetic" }; }
export function inventoryBody() { return { schema_version: 1, session_id: source, quarantine_revision: 0, found: true, visible_inference_calls: 1, quarantined_inference_calls: 0, calls: [callMetadata()], capture_completeness: "unknown" }; }
export function manifestBody() { return { schema_version: 1, session_id: source, quarantine_revision: 0, call: callMetadata(), messages: [
  { side: "input", message_index: 0, role: "user", finish_reason: null, parts: [{ type: "text", reference: ref(0, "input") }] },
  { side: "output", message_index: 0, role: "assistant", finish_reason: "stop", parts: ["text", "reasoning", "tool_call_response", "text"].map((type, index) => ({ type, reference: ref(index) })) },
] }; }
export function readBody(requested = references) {
  const parts = [{ type: "text", content: failure }, { type: "reasoning", content: "The prior attempt rounded an identifier." }, { type: "tool_call_response", id: "failed-check", result: { integer: "PRECISE_INTEGER", large_integer: "LARGE_INTEGER", text: "surrogate\ud800 and NUL\0" } }, { type: "text", content: requirement }];
  return { schema_version: 1, session_id: source, quarantine_revision: 0, items: requested.map((reference) => ({ reference: { ...reference }, observed_at: when, role: reference.side === "input" ? "user" : "assistant", finish_reason: reference.side === "input" ? null : "stop", part: reference.side === "input" ? { type: "text", content: requirement } : parts[reference.part_index] })) };
}
export function exact(value: unknown): string {
  return JSON.stringify(value).replaceAll('"PRECISE_INTEGER"', "9007199254740993").replaceAll('"LARGE_INTEGER"', largeInteger)
    .replace(/[\u007f-\uffff]/g, (character) => "\\u" + character.charCodeAt(0).toString(16).padStart(4, "0"));
}
