# Capture fixture provenance

`litellm_responses_standard_logging_object.json` records a toy slugger debugging
session through LiteLLM. It retains instruction and tool-description excerpts
from [OpenAI Codex 0.142.5](https://github.com/openai/codex/tree/26de83050b20f7e0ee211b9739e52ae00ce8032a).
Codex distributes those sources under Apache License 2.0. The upstream
[LICENSE](attribution/openai-codex/LICENSE) and
[NOTICE](attribution/openai-codex/NOTICE) accompany the excerpts.

The captured text comes from these files at that revision:

- `codex-rs/models-manager/prompt.md`
- `codex-rs/core/src/tools/handlers/shell_spec.rs`
- `codex-rs/core/src/context/apps_instructions.rs`
- `codex-rs/core-skills/src/render.rs`
- `codex-rs/core/src/context/available_plugins_instructions.rs`
- `codex-rs/prompts/templates/permissions/sandbox_mode/workspace_write.md`

The fixture curator truncated instruction and prompt text and sanitized the
installation identifier, gateway hostname, local paths, and traceback. The
fixture retains experiment identifiers and the request and response structure
used by the translator tests. Preserve the frozen fixture bytes.
