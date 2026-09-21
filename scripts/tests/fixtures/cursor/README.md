# Cursor native hook regression

`post-tool-use-3.18.25.json` preserves the shape observed in the September 9,
2026 Cursor 3.18.25 desktop rehearsal. The hook summary supplies the event,
Session, call identifier, tool name, and `workspace_roots`; it has no `cwd`.
The same call's local Cursor transcript supplies the `Write` input keys
`path` and `contents`. The fixture combines these two observations; it isn't
an unmodified hook payload. Identifiers and the repository path are replaced.

The regression rewrites `/fixture/repository` into temporary real git
repositories. It also tests the saved hook-summary shape without `tool_input`.
The file text is the synthetic arithmetic example used during the rehearsal.
