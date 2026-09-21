# Transcript fixtures

`claude_permission_deny_retry.jsonl` preserves the Claude Code 2.1.200
permission-deny shape recorded in a real transcript and the successful Edit
shape from the same wire family. Identifiers, paths, and authored text are
sanitized. The fixture includes the separate user correction entry that
distinguishes a correction retry from a pure regenerate sequence.

The client test treats the file as frozen. Don't add transcript fields to the
upload contract: the extractor emits identifiers and event metadata only.

## Codex native file changes

`codex_0_153_4_completed_file_change.jsonl` records the successful Add event
from a Codex 0.153.4 rehearsal on September 9, 2026. The fixture retains the
`item_completed` / `FileChange` shape, status, native tool-call identifier,
event timestamp, and authored arithmetic function. Session and turn identifiers
and paths are sanitized. The Session header retains only identity and working
directory; account metadata, prompts, and unrelated transcript entries are
omitted. `__ROOT__` is the test directory. This recorded fixture is frozen.

`test_codex_completed_transcript.py` feeds the recorded Add through the client
and capture translator. Separate conformance cases substitute Update hunks,
move paths, failed or absent statuses, mismatched Sessions, absent tool-call
identifiers, malformed changes, and multi-file patches. Update conformance
cases aren't recorded native Update events. The native `item.id` remains the
Edit observation's call identifier; neither the turn identifier nor stdout
supplies identity or success evidence.

`codex_native_file_changes.jsonl` is a synthetic conformance fixture, pinned to
Codex `rust-v0.142.5`, not a recorded user transcript. The upstream
[FileChange enum](https://github.com/openai/codex/blob/rust-v0.142.5/codex-rs/protocol/src/protocol.rs#L3664)
defines Add with `content`, Update with `unified_diff` and `move_path`, and Delete
with removed file content. Removed content isn't an authored proposal, so the
adapter declines Delete. The fixture uses native `patch_apply_end` events.
`__ROOT__` is the test directory; each changed path resolves against that directory.

The Add proposal retains its terminal newline. The Update hunk replaces one line
with `++counter; update_total(counter, delta, result);`; the patch prefix is one
`+`, so the two authored increment operators survive. The test feeds both pairs
through the capture translator and checks the Update's edit retention score.

Shell cases in `test_transcript.py` separately use apply_patch heredoc grammar.
The first Update hunk can omit `@@`, including after `*** Move to:` and before
`*** End of File`; tests preserve exact additions through Edit observation
capture and retention scoring. The grammar source is the
[Codex patch parser](https://github.com/openai/codex/blob/rust-v0.142.5/codex-rs/apply-patch/src/parser.rs).
They pin the [Codex execution-result header](https://github.com/openai/codex/blob/rust-v0.142.5/codex-rs/core/src/tools/context.rs#L409):
optional Chunk ID, Wall time, Process exited with code, optional token count,
then Output. The supported legacy wrapper starts with Exit code, followed by
Wall time and Output. Status text after Output never proves success.
Quoted heredocs preserve authored text; unquoted expansion syntax is declined.
These fixtures establish this version's supported cases, not support for every
Codex release. Existing recorded fixtures remain frozen.
