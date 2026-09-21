# Copilot OTLP fixtures

These are real, decoded, sanitized GitHub Copilot OTLP request payloads.
They are frozen wire captures; do not regenerate or trim them.

Copilot's exporter sends **OTLP/JSON** (not protobuf), so fixtures are `.json`.

## Committed fixtures (present in this directory)

| Fixture | `event.name` | Surface | Decision |
|---|---|---|---|
| `edit_feedback_reject_agent.json` | `copilot_chat.edit.feedback` | `edit_surface=agent` | explicit **reject** (`outcome=rejected`) |
| `inline_done_accept.json` | `copilot_chat.inline.done` | inline ⌘I | explicit **accept** (`accepted=true`) |
| `edit_survival.json` | `copilot_chat.edit.survival` | agent `apply_patch` | **implicit accept** (`no_revert`) |

All are real, sanitized `/v1/logs` payloads; the translator tests load these
by name. No fixture records an explicit `chat_editing` **accept**.
