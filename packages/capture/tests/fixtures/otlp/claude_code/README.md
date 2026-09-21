# Claude Code OTLP fixtures

These are real Claude Code (2.1.200) OTLP `/v1/logs` payloads. Paths,
Session/prompt UUIDs, and the installation hash are sanitized; each payload
contains only the records under test. Claude Code exports **OTLP/JSON**, so
fixtures are `.json`. Preserve these frozen wire captures; do not regenerate
or trim them.

The decision-bearing event is the `claude_code.tool_decision` log event (namespaced name in the
record **body**; the `event.name` attribute is the bare `tool_decision`), with
`file_path` joined from the `claude_code.tool_result` sharing the same
`tool_use_id`. The capture method uses headless `claude -p` against a scripted
Messages-API mock, with permission outcomes driven through `--allowedTools`,
`--permission-mode`, and `--permission-prompt-tool`.

## Committed fixtures

| Fixture | `decision` / `source` | Decision |
|---|---|---|
| `tool_decision_accept_config.json` | `accept` / `config` (`--allowedTools`) | auto-applied accept (`explicit=false`), path joined; includes a `user_prompt` record to prove filtering |
| `tool_decision_accept_user.json` | `accept` / `user_temporary` (prompt allow) | explicit accept (`explicit=true`), path joined |
| `tool_decision_reject_user.json` | `reject` / `user_reject` (prompt deny) | explicit reject, **no `tool_result` on the wire** → `file_path=""` |
| `tool_decision_acceptedits_edit.json` | `accept` / `config` (`acceptEdits`, Edit tool) | auto-applied accept; includes an orphan **Read** `tool_result` to prove the edit-tool filter |
| `tool_result_truncated_input.json` | `accept` / `config` (1500-char Write) | `tool_input` values truncated in place (`…[N chars]`) — JSON stays valid, `file_path` survives |
