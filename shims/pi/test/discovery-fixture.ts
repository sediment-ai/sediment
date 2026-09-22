// SPDX-License-Identifier: MIT
export const anchor = { repository_provider: "github", repository_host: "github.com", repository_id: "123", commit_sha: "a".repeat(40) };
export function preview() {
  return {
    reference: { inference_call_id: "stored-call", side: "input", message_index: 0, part_index: 0 },
    observed_at: "2026-09-22T12:00:00+00:00", role: "tool", finish_reason: null,
    part: { type: "tool_call_response", id: "stored-tool", result: { constraint: "Exclude replay shipments.", precise: "EXACT_INTEGER", text: "\ud800\0" } },
  };
}
export function discoveryBody() {
  return {
    schema_version: 1, policy_version: 1, quarantine_revision: 0, capture_completeness: "unknown",
    commit: null as unknown, status: "matched",
    coverage: { authorized_sessions: 3, found_sessions: 2, visible_inference_calls: 2, quarantined_inference_calls: 0, scanned_parts: 2, matched_parts: 1, complete_visible_scan: true },
    skipped: { reasoning_part: 0, non_finite_number: 0, unmatched_part: 1, unmatched_session: 1, candidate_limit: 0, response_budget: 0 },
    items: [{ session_id: "uncommitted-session", score: 1, matched_parts: 1, preview: preview() as unknown, commit_match: null as unknown }],
  };
}
export function selectedBody(session = "uncommitted-session") {
  return {
    schema_version: 1, policy_version: 1, source_session_id: session, quarantine_revision: 0,
    status: "matched", capture_completeness: "unknown",
    coverage: { visible_inference_calls: 1, quarantined_inference_calls: 0, scanned_parts: 1, complete_visible_scan: true },
    skipped: { reasoning_part: 0, non_finite_number: 0, no_match: 0, repeated_content: 0, item_limit: 0, response_budget: 0 },
    items: [{ score: 1, evidence: preview() }],
  };
}
export function exact(value: unknown): string { return JSON.stringify(value).replaceAll('"EXACT_INTEGER"', "9007199254740993"); }
