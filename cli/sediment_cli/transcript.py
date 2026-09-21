# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sediment transcript extractor — the session-end edit observation client.

Runs as the harness's session-end hook (Claude Code and Codex SessionEnd; the
pi shim invokes it with ``--agent pi`` on session_shutdown): reads the hook
payload on stdin, parses the session transcript (JSONL at
``transcript_path``), and ships bounded OTLP/JSON logs batches of
``sediment.edit_observation`` records to the Sediment ingest endpoint
(``POST <endpoint>/v1/logs``) — one record per *applied* edit tool call,
carrying wire attributes that the server maps to an ``EditObservation``:

  - ``applied_text`` is the ``new_string`` or ``content`` that the tool
    applied.
  - ``observed_file_text`` is the file content at session end (``""`` when
    the file is gone).

No scoring happens here. Which edit retention metric to use is a server-side
derivation decision (``sediment_derive.survival``); the client ships
the pair, not a number, so the metric stays re-derivable over all history.

A second entry point, ``sediment transcript snapshot``, runs as a
PreToolUse hook and records line hashes around each edit call so the
session-end pass can report how many lines something *other* than the agent
changed (``external_lines_added``/``external_lines_removed``). See
the external-delta section below for why the transcript cannot supply that
on its own. Whether an external change was a human, a formatter, or a
rebase is a server-side judgment; the client ships counts, not intent.

Privacy contract (normative — tested): the payload contains ONLY model output
and the state of files the agent itself edited. Concretely: the AI-authored
text of applied edits (also captured as the completion at the gateway), the
session-end state of those files (the near-commit state the mirror captures), line
counts for changes the agent did not make, and the AI-authored text of edits
the developer refused. Retry linkage adds call identifiers, the file path, and
the edit-tool name. No prompts, correction text, conversation, Read/tool
results, environment, duplicate attempt text, or raw transcript leaves the
machine.

One of those is not a duplicate of another seam: a refused proposal on a
harness that does not route through the gateway is captured here and nowhere
else. It is still model output, never the developer's own work.

Best-effort: always exits 0. A missed session only means the signal degrades
to the attribution backstop (notes/jaccard) — it must never break the agent.

Harness seam: per-agent parsers (``_PARSERS``) extract edits from
each harness's session-file shape; everything downstream — pair-building,
the size cap, the privacy contract, the POST — is shared. Each record
carries an ``agent`` attribute (an ``AgentHarness`` enum value) so the
server attributes the pair without per-agent event names.

Environment:

  SEDIMENT_OTLP_ENDPOINT   ingest base URL — required; deliberately NO
                           OTEL_EXPORTER_OTLP_ENDPOINT fallback. Any standard
                           collector accepts POST /v1/logs, so inheriting the
                           machine's generic telemetry config would silently
                           ship edit text pairs to whatever third-party
                           collector it happens to export to.
  SEDIMENT_INGEST_TOKEN    bearer token (falls back to an Authorization header
                           in OTEL_EXPORTER_OTLP_HEADERS — safe, because it is
                           only ever sent to the explicit endpoint above)
  OTEL_RESOURCE_ATTRIBUTES ``user.id`` is forwarded when present (parity with
                           the agents' own telemetry identity)

Unset endpoint = not opted in: the hook exits 0 without reading anything.
"""

from __future__ import annotations

import difflib
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

EVENT_NAME = "sediment.edit_observation"
# The refused-edit record. Claude Code only: the pi session format
# reports isError without distinguishing a refusal from a tool failure, so
# the pi parser emits none rather than guessing.
REJECTED_EVENT_NAME = "sediment.rejected_edit"
RETRY_LINKAGE_EVENT_NAME = "sediment.retry_linkage"
# Edit tools whose input carries the full AI-authored text. NotebookEdit and
# the legacy MultiEdit are skipped (ponytail: cell-JSON and multi-part edits
# need their own pairing rules; those sessions fall back to the attribution
# backstop until those pairing rules exist).
_EDIT_TOOLS = {"Edit": "new_string", "Write": "content"}
# ponytail: fixed per-side cap keeps a pathological file from turning the
# hook into a multi-MB POST; raise if real source files exceed it.
MAX_TEXT_BYTES = 256 * 1024
_DELIVERY_MODULE = None


def _trail(msg: str) -> None:
    print(f"sediment-transcript: {msg}", file=sys.stderr)


def _iter_jsonl(path: Path):
    """Yield parsed JSONL entries, tolerating junk lines."""
    try:
        # JSONL is LF-framed. Unicode separators inside JSON strings are content.
        lines = path.read_text(encoding="utf-8").split("\n")
    except (OSError, UnicodeError):
        _trail("transcript_unreadable; snapshots retained")
        # The hook's fail-soft handler must skip cache cleanup on extraction
        # failure. An unreadable source is not a readable session with no edits.
        raise RuntimeError("transcript_unreadable") from None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            yield entry


def _nanos(timestamp: object) -> int | None:
    if not isinstance(timestamp, str):
        return None
    if timestamp.endswith("Z"):  # fromisoformat only accepts Z on 3.11+
        timestamp = timestamp[:-1] + "+00:00"
    try:
        return int(datetime.fromisoformat(timestamp).timestamp() * 1_000_000_000)
    except ValueError:
        return None


def _ms_nanos(timestamp: object) -> int | None:
    """pi message timestamps are Unix milliseconds (numbers), not ISO."""
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
        return None
    if timestamp <= 0:
        return None
    return int(timestamp * 1_000_000)


# How a Claude Code transcript marks a call the developer refused, as
# opposed to one the tool failed on. Measured over ~150 real transcripts:
# the entry-level ``toolUseResult`` is exactly this string on every reject,
# and the model-visible ``content`` opens with the phrase below on the same
# 8/8 — the two never disagreed.
#
# Both are matched **exactly**, never as a substring. "rejected" appears in
# ordinary tool output (`git push` prints "! [rejected]", source files
# contain the word), and a substring test turned 8 real rejects into 23
# during the survey. The Edit/Write filter already excludes those, but the
# exactness is the part that must not rot.
_REJECT_RESULT = "User rejected tool use"
_REJECT_PHRASE = "The user doesn't want to proceed with this tool use."


def _result_text(block: dict) -> str:
    """The model-visible text of a tool_result block, however it is shaped."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return ""


def _is_user_reject(entry: dict, block: dict) -> bool:
    """True when the developer refused this call, not when the tool failed.

    Two independent markers, either sufficient: ``toolUseResult`` is a Claude
    Code internal with no compatibility promise, while the result content is
    the model-facing shape. A genuine tool error ("String to replace not
    found", "File has not been read yet") matches neither.
    """
    if entry.get("toolUseResult") == _REJECT_RESULT:
        return True
    return _result_text(block).startswith(_REJECT_PHRASE)


def _claude_calls(entries, session_id: str | None = None) -> tuple[list[dict], dict]:
    """Every Edit/Write call in the transcript, plus how each one ended.

    Returns ``(calls, outcome_by_id)`` where the outcome is ``"ok"``,
    ``"reject"`` (the developer refused), or ``"error"`` (the tool failed).
    A call with no result at all — an interrupted session — appears in
    ``calls`` and is absent from the mapping, which is neither an
    application nor a refusal.

    Lines from other sessions are skipped: a resumed session's transcript
    embeds the prior session's history, each line stamped with its own
    ``sessionId``. Those calls were already observed at their own session's
    end — re-emitting them here would duplicate them under the wrong session
    and drag that session's bounds backwards (ADR 0002).
    """
    calls: list[dict] = []
    outcome: dict[str, str] = {}
    for entry in entries:
        line_sid = entry.get("sessionId")
        if (
            session_id is not None
            and isinstance(line_sid, str)
            and line_sid != session_id
        ):
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if entry.get("type") == "assistant" and block.get("type") == "tool_use":
                name = block.get("name")
                field = _EDIT_TOOLS.get(name)
                if field is None:
                    continue
                tool_input = block.get("input")
                if not isinstance(tool_input, dict):
                    continue
                file_path = tool_input.get("file_path")
                text = tool_input.get(field)
                tool_use_id = block.get("id")
                nanos = _nanos(entry.get("timestamp"))
                if not (
                    isinstance(file_path, str)
                    and file_path
                    and isinstance(text, str)
                    and isinstance(tool_use_id, str)
                    and tool_use_id
                    and nanos
                ):
                    continue
                calls.append(
                    {
                        "tool_use_id": tool_use_id,
                        "tool_name": name,
                        "file_path": file_path,
                        "applied_text": text,
                        "time_unix_nano": nanos,
                    }
                )
            elif entry.get("type") == "user" and block.get("type") == "tool_result":
                tool_use_id = block.get("tool_use_id")
                if not isinstance(tool_use_id, str):
                    continue
                if not block.get("is_error"):
                    outcome[tool_use_id] = "ok"
                elif _is_user_reject(entry, block):
                    outcome[tool_use_id] = "reject"
                else:
                    outcome[tool_use_id] = "error"
    return calls, outcome


def extract_edits(entries, session_id: str | None = None) -> list[dict]:
    """Applied Edit/Write tool calls from transcript entries.

    An edit counts only when its ``tool_result`` arrived without ``is_error``
    — a rejected or failed call never touched the file, and pairing its text
    against the session-end state would fabricate a zero edit-retention signal
    on top of the reject decision the OTLP wire already carries.
    """
    calls, outcome = _claude_calls(entries, session_id)
    return [c for c in calls if outcome.get(c["tool_use_id"]) == "ok"]


def extract_rejected_edits(entries, session_id: str | None = None) -> list[dict]:
    """Edit/Write calls the developer refused, with the text they refused.

    The rejected side of a DPO pair needs the model's proposed text, and for
    a non-gateway harness it exists nowhere else: the decision fact carries
    ``file_path=""`` and no content, and the gateway stores an empty
    completion for tool-call turns. The transcript still holds the call, so
    the proposal is recoverable (ADR 0007 — model output only, no
    prompts, no conversation, no file state).

    Only refusals. A call the tool failed on emits nothing: the model's text
    was never judged by anyone, so it is not a preference signal.
    """
    calls, outcome = _claude_calls(entries, session_id)
    rejected = []
    for call in calls:
        if outcome.get(call["tool_use_id"]) != "reject":
            continue
        proposed = call["applied_text"]
        if len(proposed.encode("utf-8", "replace")) > MAX_TEXT_BYTES:
            _trail(
                f"rejected edit over {MAX_TEXT_BYTES}B for {call['file_path']}; dropped"
            )
            continue
        rejected.append({**call, "proposed": proposed})
    return rejected


def _has_user_text(entry: dict) -> bool:
    """Whether a user transcript entry carries a developer text turn."""
    if entry.get("type") != "user":
        return False
    message = entry.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if not isinstance(content, list):
        return False
    return any(
        isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
        and bool(block["text"].strip())
        for block in content
    )


def _retry_entry_order(entry: dict) -> tuple[int, int, str]:
    """Return deterministic transcript order for retry extraction."""
    timestamp = _nanos(entry.get("timestamp")) or 2**63 - 1
    type_order = 0 if entry.get("type") == "assistant" else 1
    return timestamp, type_order, json.dumps(entry, sort_keys=True, default=str)


def extract_retry_linkages(entries, session_id: str | None = None) -> list[dict]:
    """Link a human-rejected edit call to its accepted correction retry.

    The rule requires the same session, Edit/Write tool, and file; a real
    refusal for A; at least one developer text entry after A's rejection and
    before B; and a successful result for B. Tool failures, agent-only
    rewrites, and pure regenerate sequences emit nothing.
    """
    ordered = sorted(list(entries), key=_retry_entry_order)
    if session_id is not None and (
        not isinstance(session_id, str) or not session_id.strip()
    ):
        return []
    ordered = [
        entry
        for entry in ordered
        if isinstance(entry.get("sessionId"), str)
        and bool(entry["sessionId"].strip())
        and (session_id is None or entry["sessionId"] == session_id)
    ]
    calls, outcomes = _claude_calls(ordered, session_id)
    calls_by_id = {call["tool_use_id"]: call for call in calls}
    call_positions: dict[str, int] = {}
    call_sessions: dict[str, str | None] = {}
    outcome_positions: dict[str, int] = {}
    outcome_sessions: dict[str, str | None] = {}
    user_text_positions: dict[int, str | None] = {}

    for index, entry in enumerate(ordered):
        line_sid = entry.get("sessionId")
        effective_session = line_sid
        if _has_user_text(entry):
            user_text_positions[index] = effective_session
        message = entry.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if entry.get("type") == "assistant" and block.get("type") == "tool_use":
                call_id = block.get("id")
                if call_id in calls_by_id:
                    call_positions[call_id] = index
                    call_sessions[call_id] = effective_session
            elif entry.get("type") == "user" and block.get("type") == "tool_result":
                call_id = block.get("tool_use_id")
                if call_id in calls_by_id:
                    outcome_positions[call_id] = index
                    outcome_sessions[call_id] = effective_session

    def position(call: dict) -> int:
        return call_positions[call["tool_use_id"]]

    rejected = sorted(
        (
            call
            for call in calls
            if outcomes.get(call["tool_use_id"]) == "reject"
            and call["tool_use_id"] in outcome_positions
            and call_sessions.get(call["tool_use_id"])
            == outcome_sessions.get(call["tool_use_id"])
        ),
        key=position,
    )
    accepted = sorted(
        (
            call
            for call in calls
            if outcomes.get(call["tool_use_id"]) == "ok"
            and call["tool_use_id"] in outcome_positions
            and call_sessions.get(call["tool_use_id"])
            == outcome_sessions.get(call["tool_use_id"])
        ),
        key=position,
    )
    linkages = []
    for first in rejected:
        rejected_id = first["tool_use_id"]
        rejection_position = outcome_positions[rejected_id]
        retry_session = call_sessions[rejected_id]
        for retry in accepted:
            accepted_id = retry["tool_use_id"]
            retry_position = call_positions[accepted_id]
            if retry_position <= rejection_position:
                continue
            if (
                retry["tool_name"] != first["tool_name"]
                or retry["file_path"] != first["file_path"]
                or call_sessions[accepted_id] != retry_session
            ):
                continue
            if not any(
                rejection_position < user_position < retry_position
                and user_session == retry_session
                for user_position, user_session in user_text_positions.items()
            ):
                continue
            linkages.append(
                {
                    "rejected_call_id": rejected_id,
                    "accepted_call_id": accepted_id,
                    "tool_name": retry["tool_name"],
                    "file_path": retry["file_path"],
                    "time_unix_nano": retry["time_unix_nano"],
                }
            )
            break
    return linkages


# pi session files are JSONL too, but entry-shaped: a {"type": "session"}
# header then {"type": "message"} entries whose message holds the content
# (the upstream pi session format). Edit tools are "edit" ({path, edits:
# [{oldText, newText}]}) and "write" ({path, content}); results are
# role="toolResult" messages keyed by toolCallId.
_PI_EDIT_TOOLS = {"edit", "write"}
_PI_PARENT_MAX_BYTES = 64 * 1024 * 1024


def _pi_entry_index(entries: list[dict]) -> dict[str, str]:
    """Validate a complete native source before deciding fork ownership."""
    if not entries or entries[0].get("type") != "session":
        raise ValueError
    header = entries[0]
    if (
        header.get("version") != 3
        or not isinstance(header.get("id"), str)
        or not header["id"].strip()
    ):
        raise ValueError
    index = {}
    for entry in entries[1:]:
        identity = entry.get("id")
        if (
            entry.get("type") == "session"
            or not isinstance(entry.get("type"), str)
            or not isinstance(identity, str)
            or not identity.strip()
            or identity in index
            or (
                entry.get("type") == "message"
                and not isinstance(entry.get("message"), dict)
            )
        ):
            raise ValueError
        # pi re-chains parentId while copying the otherwise immutable entry.
        index[identity] = json.dumps(
            {k: v for k, v in entry.items() if k != "parentId"},
            sort_keys=True,
            allow_nan=False,
            separators=(",", ":"),
        )
    return index


def _pi_parent_entries(reference: object, source_path: Path | None) -> list[dict]:
    """Read one bounded regular parent leaf inside the child source directory."""
    if not isinstance(reference, str) or not reference or source_path is None:
        raise ValueError
    if "\0" in reference or any(p in {".", ".."} for p in reference.split("/")):
        raise ValueError
    source = source_path.resolve(strict=True)
    parent = Path(reference)
    if not parent.is_absolute():
        if parent.name != reference:
            raise ValueError
        parent = source.parent / parent
    if (
        parent.suffix != ".jsonl"
        or parent.parent.resolve(strict=True) != source.parent
        or parent.name == source.name
    ):
        raise ValueError
    directory_fd = os.open(source.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        # NONBLOCK ensures an untrusted FIFO cannot block before the fstat gate.
        fd = os.open(
            parent.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory_fd,
        )
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_size > _PI_PARENT_MAX_BYTES
            ):
                raise ValueError
            content = stream.read(_PI_PARENT_MAX_BYTES + 1)
            after = os.fstat(stream.fileno())
            if (
                len(content) != before.st_size
                or after.st_size != before.st_size
                or after.st_mtime_ns != before.st_mtime_ns
            ):
                raise ValueError
    finally:
        os.close(directory_fd)
    # Unlike ordinary malformed siblings, a damaged parent makes ownership
    # unprovable. Never classify omitted parent entries as child-owned work.
    entries = [
        json.loads(line) for line in content.decode("utf-8").split("\n") if line.strip()
    ]
    if not all(isinstance(entry, dict) for entry in entries):
        raise ValueError
    return entries


def _pi_owned_entries(entries: list[dict], source_path: Path | None) -> list[dict]:
    headers = [entry for entry in entries if entry.get("type") == "session"]
    if not any("parentSession" in header for header in headers):
        return entries
    try:
        child = _pi_entry_index(entries)
        header = entries[0]
        parent_entries = _pi_parent_entries(header["parentSession"], source_path)
        parent = _pi_entry_index(parent_entries)
        if parent_entries[0]["id"] == header["id"]:
            raise ValueError
        if any(child[key] != parent[key] for key in child.keys() & parent.keys()):
            raise ValueError
    except (OSError, ValueError, RuntimeError):
        count = sum(entry.get("type") == "message" for entry in entries)
        _trail(
            f"pi_transcript_declined reason=parent_source_unverified skipped_entries={count}"
        )
        # Preserve snapshots on a source failure, just like an unreadable child.
        raise RuntimeError("parent_source_unverified") from None
    owned = [
        entry
        for entry in entries
        if entry.get("type") != "message" or entry.get("id") not in parent
    ]
    count = len(entries) - len(owned)
    if count:
        _trail(f"pi_transcript_skipped reason=inherited_entry skipped_entries={count}")
    return owned


def _pi_edit_path(file_path: str, cwd: object) -> str | None:
    # These native expansion forms need their own source-path contract. The
    # ordinary filesystem-path contract never consults the extractor's HOME.
    if (
        "\0" in file_path
        or file_path.startswith(("~", "@", "file://"))
        or re.search(r"[\u00a0\u2000-\u200a\u202f\u205f\u3000]", file_path)
    ):
        _trail("pi_transcript_declined reason=unsupported_path skipped_edits=1")
        return None
    if Path(file_path).is_absolute():
        return os.path.normpath(file_path)
    if not isinstance(cwd, str) or "\0" in cwd or not Path(cwd).is_absolute():
        _trail(
            "pi_transcript_declined reason=execution_directory_invalid skipped_edits=1"
        )
        return None
    return os.path.normpath(os.path.join(cwd, file_path))


def _pi_applied_text(name: str, arguments: dict) -> str | None:
    """The AI-authored text of a pi edit/write call, or None if unobservable."""
    if name == "write":
        content = arguments.get("content")
        return content if isinstance(content, str) else None
    # edit: the authored text is the replacement spans. ponytail: multi-span
    # calls join newTexts with newlines — span-vs-file pairing is the same
    # posture as Claude's new_string; the upgrade path, if precision floors
    # show it matters, is reconstructing the post-edit file from the
    # toolResult's details.patch (a standard unified diff).
    edits = arguments.get("edits")
    if not isinstance(edits, list) or not edits:
        return None
    texts = [e.get("newText") for e in edits if isinstance(e, dict)]
    if len(texts) != len(edits) or not all(isinstance(t, str) for t in texts):
        return None
    return "\n".join(texts)


def extract_edits_pi(
    entries, session_id: str | None = None, *, source_path: Path | None = None
) -> list[dict]:
    """Applied edit/write tool calls from pi session-file entries.

    The header supplies execution cwd; fork ownership comes from an authorized
    immediate parent source. A contradicted Session never supplies observations.
    """
    entries = list(entries)
    for entry in entries:
        if entry.get("type") == "session" and (
            session_id is not None and entry.get("id") != session_id
        ):
            count = sum(item.get("type") == "message" for item in entries)
            _trail(
                f"pi_transcript_declined reason=session_mismatch skipped_entries={count}"
            )
            return []
    entries = _pi_owned_entries(entries, source_path)
    headers = [entry for entry in entries if entry.get("type") == "session"]
    cwd = headers[0].get("cwd") if len(headers) == 1 else None
    edits: list[dict] = []
    ok: set[str] = set()
    for entry in entries:
        entry_type = entry.get("type")
        if entry_type == "session":
            continue
        if entry_type != "message":
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "assistant":
            content = message.get("content")
            if not isinstance(content, list):
                continue
            nanos = _ms_nanos(message.get("timestamp"))
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "toolCall":
                    continue
                name = block.get("name")
                if name not in _PI_EDIT_TOOLS:
                    continue
                arguments = block.get("arguments")
                if not isinstance(arguments, dict):
                    continue
                file_path = arguments.get("path")
                text = _pi_applied_text(name, arguments)
                tool_use_id = block.get("id")
                if not (
                    isinstance(file_path, str)
                    and file_path
                    and isinstance(text, str)
                    and isinstance(tool_use_id, str)
                    and tool_use_id
                    and nanos
                ):
                    continue
                file_path = _pi_edit_path(file_path, cwd)
                if file_path is None:
                    continue
                edits.append(
                    {
                        "tool_use_id": tool_use_id,
                        "tool_name": name,
                        "file_path": file_path,
                        "applied_text": text,
                        "time_unix_nano": nanos,
                    }
                )
        elif role == "toolResult":
            if not message.get("isError"):
                tool_use_id = message.get("toolCallId")
                if isinstance(tool_use_id, str):
                    ok.add(tool_use_id)
    return [e for e in edits if e["tool_use_id"] in ok]


def _codex_added_text(unified_diff: str) -> str | None:
    """Read additions inside complete native unified-diff hunks."""
    additions = []
    remaining_old = remaining_new = 0
    saw_hunk = False
    lines = unified_diff.split("\n")
    if lines[-1:] == [""]:
        lines.pop()
    for line in lines:
        header = re.fullmatch(r"@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@(?: .*)?", line)
        if header:
            if remaining_old or remaining_new:
                return None
            # A hunk cannot consume more lines than the supplied diff. Bound
            # digit parsing too, so malformed counts cannot sink sibling edits.
            counts = (header[1] or "1", header[2] or "1")
            if any(len(count.lstrip("0")) > len(str(len(lines))) for count in counts):
                return None
            remaining_old, remaining_new = (
                int(count.lstrip("0") or "0") for count in counts
            )
            if max(remaining_old, remaining_new) > len(lines):
                return None
            saw_hunk = True
            continue
        if not saw_hunk and line.startswith(("diff --git ", "index ", "--- ", "+++ ")):
            continue
        if line == "\\ No newline at end of file" and saw_hunk:
            continue
        if not saw_hunk or not line:
            return None
        if line[0] == "+" and remaining_new:
            remaining_new -= 1
            additions.append(line[1:])
        elif line[0] == "-" and remaining_old:
            remaining_old -= 1
        elif line[0] == " " and remaining_old and remaining_new:
            remaining_old -= 1
            remaining_new -= 1
        else:
            return None
    return (
        "\n".join(additions)
        if saw_hunk and not (remaining_old or remaining_new)
        else None
    )


def _codex_shell_patch(command: str) -> tuple[str, str] | None:
    """Read one apply_patch heredoc; never execute or infer shell context."""
    lines = command.lstrip().split("\n")
    while lines[-1:] == [""]:
        lines.pop()
    if not lines:
        return None
    header = re.fullmatch(
        r"apply_patch <<\s*(?:'([^']+)'|\"([^\"]+)\"|(\w+))", lines[0]
    )
    if header is None:
        if "apply_patch" in command:
            _trail("execution_directory_unknown: unsupported shell command")
        return None
    delimiter = next(value for value in header.groups() if value is not None)
    if lines[-1] != delimiter:
        _trail("execution_directory_unknown: command extends beyond patch heredoc")
        return None
    patch = lines[1:-1]
    if header[3] is not None and any(char in "\n".join(patch) for char in "$`\\"):
        _trail("malformed_patch: unquoted heredoc can change authored text")
        return None
    if len(patch) < 3 or patch[0] != "*** Begin Patch" or patch[-1] != "*** End Patch":
        _trail("malformed_patch: incomplete patch envelope")
        return None
    file_headers = [
        line for line in patch if re.match(r"\*\*\* (Add|Update|Delete) File:", line)
    ]
    file_header = re.fullmatch(r"\*\*\* (Add|Update|Delete) File: (.+)", patch[1])
    if len(file_headers) != 1 or file_header is None:
        _trail("unsupported_patch_kind: expected one file")
        return None
    kind, path = file_header.groups()
    if kind == "Delete":
        _trail("unsupported_patch_kind: deletion has no authored proposal")
        return None
    body = patch[2:-1]
    if kind == "Update" and body and body[0].startswith("*** Move to: "):
        path = body.pop(0).removeprefix("*** Move to: ")
    additions = []
    # The first Update hunk may start with change lines without an @@ header.
    in_hunk = kind == "Add" or bool(body)
    for index, line in enumerate(body):
        if kind == "Update" and (line == "@@" or line.startswith("@@ ")):
            in_hunk = True
        elif in_hunk and line.startswith("+"):
            additions.append(line[1:])
        elif (
            kind == "Update" and in_hunk and (line.startswith((" ", "-")) or line == "")
        ):
            continue
        elif (
            kind == "Update"
            and in_hunk
            and index > 0
            and line == "*** End of File"
            and index == len(body) - 1
        ):
            continue
        else:
            _trail("malformed_patch: unsupported patch body")
            return None
    if not in_hunk or not path:
        _trail("malformed_patch: missing patch body or path")
        return None
    return path, "\n".join(additions)


def _codex_status_reason(outputs: list[object]) -> str | None:
    """Accept status metadata before the output delimiter, never stdout text."""
    statuses = []
    unknown = not outputs
    for output in outputs:
        if not isinstance(output, str):
            unknown = True
            continue
        header, delimiter, _stdout = output.partition("\nOutput:")
        wall_times = 0
        codes = []
        for line in header.splitlines():
            status = re.fullmatch(
                r"(?:Exit code: |Process exited with code )(-?\d{1,10})", line
            )
            if status:
                codes.append(int(status[1]))
            elif re.fullmatch(r"Wall time: \d+(?:\.\d+)? seconds", line):
                wall_times += 1
            elif not re.fullmatch(r"(?:Chunk ID: \S+|Original token count: \d+)", line):
                unknown = True
        statuses.extend(codes)
        if not delimiter or wall_times != 1 or len(codes) != 1:
            unknown = True
    if len(set(statuses)) > 1:
        return "execution_status_conflict"
    if unknown or not statuses:
        return "execution_status_unknown"
    return "execution_failed" if statuses[0] != 0 else None


def _codex_edit_path(
    file_path: str, cwd: object, arguments: dict | None = None
) -> str | None:
    """Resolve an edit against its declared execution directory."""
    explicit = arguments is not None and "workdir" in arguments
    directory = arguments["workdir"] if explicit else cwd
    try:
        path = Path(file_path)
        if directory is None and not explicit:
            if path.is_absolute():
                return str(path)
            _trail("execution_directory_unknown: relative path has no directory")
            return None
        if not isinstance(directory, str) or not directory:
            raise ValueError("directory must be a nonempty path")
        base = Path(directory)
        if not base.is_absolute():
            if not explicit or not isinstance(cwd, str) or not Path(cwd).is_absolute():
                raise ValueError("directory cannot be resolved")
            base = Path(cwd) / base
        base = base.resolve(strict=True)
        if not base.is_dir():
            raise ValueError("directory is not a directory")
        if "\x00" in file_path:
            raise ValueError("invalid file path")
        return str(path if path.is_absolute() else base / path)
    except (OSError, ValueError, UnicodeError):
        _trail("execution_directory_invalid: unusable execution directory or path")
        return None


def extract_edits_codex(entries, session_id: str | None = None) -> list[dict]:
    """Extract successful single-file native changes and supported shell patches.

    # ponytail: multi-file calls need a Fact identity that can join each file;
    # skip them until that contract exists, without inventing child call ids.
    """
    material = list(entries)
    cwd: object = None
    transcript_session: object = None
    found_session = False
    for entry in material:
        if entry.get("type") != "session_meta":
            continue
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            continue
        transcript_session = payload.get("id") or payload.get("session_id")
        if (
            session_id is not None
            and isinstance(transcript_session, str)
            and transcript_session != session_id
        ):
            _trail(
                f"session file header id {transcript_session} != {session_id}; skipping session"
            )
            return []
        found_session = isinstance(transcript_session, str) and bool(transcript_session)
        cwd = payload.get("cwd")
        break
    if session_id is not None and not found_session:
        return []

    outputs: dict[str, list[object]] = {}
    for entry in material:
        payload = entry.get("payload")
        if (
            entry.get("type") == "response_item"
            and isinstance(payload, dict)
            and payload.get("type") == "function_call_output"
            and isinstance(payload.get("call_id"), str)
        ):
            outputs.setdefault(payload["call_id"], []).append(payload.get("output"))
    edits = []
    for entry in material:
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            continue
        call_id = payload.get("call_id")
        nanos = _nanos(entry.get("timestamp"))
        item = payload.get("item")
        completed_change = (
            entry.get("type") == "event_msg"
            and payload.get("type") == "item_completed"
            and isinstance(item, dict)
            and item.get("type") == "FileChange"
        )
        if completed_change:
            thread_id = payload.get("thread_id")
            if (
                not isinstance(thread_id, str)
                or not thread_id.strip()
                or (transcript_session is not None and thread_id != transcript_session)
            ):
                _trail(
                    "malformed_patch: FileChange Session identity is absent or mismatched"
                )
                continue
            # Codex 0.153.4 puts the real tool-call id and result inside item;
            # the enclosing turn or event id cannot join a Developer decision.
            payload = item
            call_id = payload.get("id")
            if not isinstance(call_id, str) or not call_id.strip() or not nanos:
                _trail(
                    "malformed_patch: FileChange call identity or timestamp is absent"
                )
                continue
            status = payload.get("status")
            if status != "completed":
                reason = (
                    "execution_failed"
                    if status in ("failed", "declined")
                    else "execution_status_unknown"
                )
                _trail(f"{reason}: patch {call_id}")
                continue
        if not isinstance(call_id, str) or not call_id or not nanos:
            continue
        arguments = None
        if (
            entry.get("type") == "response_item"
            and payload.get("type") == "function_call"
            and payload.get("name") == "exec_command"
        ):
            try:
                arguments = (
                    json.loads(payload["arguments"])
                    if isinstance(payload.get("arguments"), str)
                    else None
                )
            except ValueError:
                arguments = None
            command = arguments.get("cmd") if isinstance(arguments, dict) else None
            if not isinstance(command, str):
                continue
            patch = _codex_shell_patch(command)
            if patch is None:
                continue
            reason = _codex_status_reason(outputs.get(call_id, []))
            if reason:
                _trail(f"{reason}: patch {call_id}")
                continue
            file_path, applied_text = patch
            tool_name = "exec_command"
        elif entry.get("type") == "event_msg" and (
            completed_change
            or (
                payload.get("type") == "patch_apply_end"
                and payload.get("success") is True
            )
        ):
            changes = payload.get("changes")
            if not isinstance(changes, dict) or not changes:
                _trail(f"malformed_patch: patch {call_id} changes are absent")
                continue
            if len(changes) != 1:
                _trail(
                    f"unsupported_patch_kind: multi-file patch {call_id} cannot join one EditObservation"
                )
                continue
            file_path, change = next(iter(changes.items()))
            if (
                not isinstance(file_path, str)
                or not file_path
                or not isinstance(change, dict)
            ):
                _trail(f"malformed_patch: patch {call_id}")
                continue
            kind = change.get("type")
            if kind == "add":
                applied_text = change.get("content")
            elif kind == "update":
                move_path = change.get("move_path")
                if move_path is not None:
                    if not isinstance(move_path, str) or not move_path:
                        _trail(f"malformed_patch: patch {call_id} move path")
                        continue
                    file_path = move_path
                diff = change.get("unified_diff")
                applied_text = (
                    _codex_added_text(diff) if isinstance(diff, str) else None
                )
            else:
                _trail(f"unsupported_patch_kind: patch {call_id}")
                continue
            if not isinstance(applied_text, str):
                _trail(f"malformed_patch: patch {call_id}")
                continue
            tool_name = "apply_patch"
        else:
            continue
        path = _codex_edit_path(file_path, cwd, arguments)
        if path is not None:
            edits.append(
                {
                    "tool_use_id": call_id,
                    "tool_name": tool_name,
                    "file_path": path,
                    "applied_text": applied_text,
                    "time_unix_nano": nanos,
                }
            )
    return edits


# The harness seam: per-agent parsers, one shipper. A new harness
# plugs in as one parser + tests; pair-building, caps, the privacy contract,
# and the POST never change.
_PARSERS = {
    "claude-code": extract_edits,
    "codex": extract_edits_codex,
    "pi": extract_edits_pi,
}


# A low edit retention score is ambiguous: the agent revising its own edit and a
# human correcting it look identical. Telling them apart needs the file's
# state *between* two agent edits, and the transcript cannot supply it —
# Claude Code records ``toolUseResult.originalFile`` only for files under
# ~10 KiB (measured over 1,056 samples: none above 9,969 chars, null for
# every larger file). A transcript-only baseline would therefore exist for
# small files and vanish for large ones, and a gap that tracks file size
# biases every label derived from it.
#
# So the pre-edit state is read from disk at the one moment it is still
# there: a PreToolUse hook (``sediment transcript snapshot``). Per edit
# call it stores two line-hash lists — the file before the edit, and the
# file as the edit will leave it, computed by performing the tool's own
# substitution. The window between edit i and edit i+1 on one file is then
# ``post_i`` vs ``pre_{i+1}``: whatever changed in there, this agent did
# not do. The last edit's window closes against the session-end content
# the extractor already reads.
#
# Hashes, never lines: the cache is local-only and never shipped, and the
# wire carries counts alone. A window whose baseline is unobservable is
# omitted, never reported as zero (AGENTS.md, absent-never-guessed).


# Filename-safe, and never an all-dots name. The charset alone still admits
# "." and "..", which are not children of anything — and this id becomes a
# path that `_clear_cache` deletes, so a payload carrying ".." would walk out
# of the cache root and take a sibling session's snapshots with it.
_SAFE_ID = re.compile(r"^(?!\.+$)[A-Za-z0-9._-]{1,128}$")
# ponytail: a crashed session leaves its dir behind; SessionEnd sweeps
# siblings older than this. Raise it if sessions legitimately outlive it.
_CACHE_TTL_S = 7 * 24 * 3600


def _cache_root() -> Path | None:
    """The directory holding every session's snapshots, or None when no
    cache base is resolvable.

    The ``sediment/deltas`` segments are appended under *any* base, the
    override included, so this path is always one this hook created. That is
    what makes the stale-session sweep in :func:`_clear_cache` safe: it
    deletes children of this directory, and pointing the override at a home
    directory must never turn that into deleting the home directory's
    contents.

    A missing home (``HOME`` unset and no passwd entry — a minimal
    container) must not gate the emit: the cache no-ops and the session
    ships without deltas.
    """
    base = os.environ.get("SEDIMENT_DELTA_CACHE") or os.environ.get("XDG_CACHE_HOME")
    if not base:
        try:
            base = str(Path.home() / ".cache")
        except RuntimeError:
            return None
    return Path(base) / "sediment" / "deltas"


def _cache_dir(session_id: str) -> Path | None:
    """This session's snapshot dir, or None when there is nothing to cache.

    None when the id is not filename-safe, or when no cache base is
    resolvable — the cache is an optimization and never gates the emit.
    """
    if not _SAFE_ID.match(session_id):
        return None
    root = _cache_root()
    if root is None:
        return None
    return root / session_id


def _line_hashes(text: str) -> list[str]:
    """Per-line digests — enough to diff, never enough to reconstruct."""
    return [
        hashlib.blake2b(line.encode("utf-8", "replace"), digest_size=8).hexdigest()
        for line in text.splitlines()
    ]


def _post_edit_text(pre: str, tool_name: str, tool_input: dict) -> str | None:
    """The file as this edit will leave it, or None if that is unpredictable.

    Performs the tool's own substitution against the on-disk state. An
    ``old_string`` that does not occur means the call will not apply as
    described (a stale read, a racing writer) — unpredictable, so the window
    it would have opened is omitted rather than guessed.
    """
    if tool_name == "Write":
        content = tool_input.get("content")
        return content if isinstance(content, str) else None
    old = tool_input.get("old_string")
    new = tool_input.get("new_string")
    if not isinstance(old, str) or not isinstance(new, str) or old not in pre:
        return None
    if tool_input.get("replace_all"):
        return pre.replace(old, new)
    return pre.replace(old, new, 1)


def cmd_snapshot(hook: dict) -> None:
    """The PreToolUse hook: record one edit call's before/after line hashes.

    One file per call rather than appends to a shared log — parallel edit
    calls would interleave a shared file, and a per-call name needs no lock.
    """
    session_id = hook.get("session_id")
    call_id = hook.get("tool_use_id")
    tool_name = hook.get("tool_name")
    tool_input = hook.get("tool_input")
    if not (
        isinstance(session_id, str)
        and isinstance(call_id, str)
        and _SAFE_ID.match(call_id)
        and tool_name in _EDIT_TOOLS
        and isinstance(tool_input, dict)
    ):
        return
    file_path = tool_input.get("file_path")
    directory = _cache_dir(session_id)
    if directory is None or not (isinstance(file_path, str) and file_path):
        return
    try:
        pre = Path(file_path).read_text(encoding="utf-8")
    except FileNotFoundError:
        pre = ""  # a Write creating the file: no prior lines, not a gap
    except (OSError, UnicodeDecodeError) as exc:
        _trail(f"pre-edit state unreadable for {file_path} ({exc}); window skipped")
        return
    post = _post_edit_text(pre, tool_name, tool_input)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{call_id}.json").write_text(
        json.dumps(
            {
                "call_id": call_id,
                "file_path": file_path,
                # PreToolUse fires in edit order, so this orders the windows.
                "ts": time.time_ns(),
                "pre": _line_hashes(pre),
                "post": None if post is None else _line_hashes(post),
            }
        ),
        encoding="utf-8",
    )


def _load_snapshots(session_id: str) -> list[dict]:
    directory = _cache_dir(session_id)
    if directory is None:
        return []
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return []
    out: list[dict] = []
    for entry in entries:
        try:
            record = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (
            isinstance(record, dict)
            and isinstance(record.get("call_id"), str)
            and isinstance(record.get("file_path"), str)
            and isinstance(record.get("ts"), int)
            and isinstance(record.get("pre"), list)
            and isinstance(record.get("post"), (list, type(None)))
        ):
            out.append(record)
    return out


def _clear_cache(session_id: str) -> None:
    """Drop this session's snapshots, and any left by a session that died.

    Only ever deletes children of :func:`_cache_root`, and only ones whose
    name is a session id this hook could have written — a stray file or
    directory sharing the cache root is left alone.
    """
    directory = _cache_dir(session_id)
    if directory is None:
        return
    shutil.rmtree(directory, ignore_errors=True)
    root = _cache_root()
    if root is None:
        return
    cutoff = time.time() - _CACHE_TTL_S
    try:
        siblings = list(root.iterdir())
    except OSError:
        return
    for sibling in siblings:
        if not (sibling.is_dir() and _SAFE_ID.match(sibling.name)):
            continue
        try:
            if sibling.stat().st_mtime < cutoff:
                shutil.rmtree(sibling, ignore_errors=True)
        except OSError:
            continue


def _line_delta(before: list[str], after: list[str]) -> tuple[int, int]:
    """(lines_added, lines_removed) between two line-hash sequences.

    ``autojunk=False``: the default heuristic treats any line recurring in
    more than 1% of a long file as junk, which would quietly distort counts
    on exactly the large files this path exists to cover.
    """
    matcher = difflib.SequenceMatcher(None, before, after, autojunk=False)
    added = removed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return added, removed


def external_deltas(
    snapshots: list[dict],
    observed_file_states: dict[str, str | None],
    applied: set[str],
) -> dict[str, tuple[int, int]]:
    """Per applied edit call, the lines something other than this agent changed.

    Windows are delimited by **applied** edits only. A call the transcript
    shows as rejected or failed never touched the file, so it is transparent
    here: letting one close the preceding window would end that window early
    and strand everything after it on a call that ships no pair. The result
    would be a confident ``(0, 0)`` for the very pattern this measurement
    exists to catch — the agent edits, the developer rejects its next
    proposal, then fixes the file by hand.

    Only ``post`` opens a window, and only ``pre`` closes one, so an edit
    whose post state was unpredictable still closes its predecessor's window.
    """
    by_file: dict[str, list[dict]] = {}
    for snap in sorted(snapshots, key=lambda s: (s["ts"], s["call_id"])):
        if snap["call_id"] in applied:
            by_file.setdefault(snap["file_path"], []).append(snap)
    out: dict[str, tuple[int, int]] = {}
    for file_path, snaps in by_file.items():
        for index, snap in enumerate(snaps):
            opened = snap["post"]
            if opened is None:  # unpredictable edit — no window to open
                continue
            if index + 1 < len(snaps):
                closed = snaps[index + 1]["pre"]
            else:
                observed_file_text = observed_file_states.get(file_path)
                if observed_file_text is None:  # unreadable — absent, not zero
                    continue
                closed = _line_hashes(observed_file_text)
            out[snap["call_id"]] = _line_delta(opened, closed)
    return out


def _observed_file_states(edits: list[dict]) -> dict[str, str | None]:
    """Read each edited file's session-end content from disk, once per path.

    A missing file reads as ``""`` (the edit fully discarded by deletion — a
    legitimate zero-survival observation). Any other failure reads as ``None``
    and drops that file's pairs: an unobservable session-end state is a capture
    gap, not a zero.
    """
    observed_file_states: dict[str, str | None] = {}
    for path in {e["file_path"] for e in edits}:
        try:
            observed_file_states[path] = Path(path).read_text(encoding="utf-8")
        except FileNotFoundError:
            target = Path(path)
            if target.is_symlink() or not target.parent.is_dir():
                _trail(
                    f"observation_unreadable: unresolved file path {path}; pairs dropped"
                )
                observed_file_states[path] = None
            else:
                observed_file_states[path] = ""
        except (OSError, UnicodeError, ValueError) as exc:
            _trail(
                f"observation_unreadable: observed file state unreadable for {path} ({exc}); pairs dropped"
            )
            observed_file_states[path] = None
    return observed_file_states


def build_pairs(
    entries,
    session_id: str | None = None,
    *,
    agent: str,
    source_path: Path | None = None,
) -> list[dict]:
    """Extract applied edits, attach observed file states, enforce the size cap.

    Each pair also carries its external-delta counts when the
    PreToolUse snapshots cover that call; a call with no usable window
    simply omits them.

    Counts ship per file under one rule: **every** applied edit on that file
    must both have a window and ship a pair. The server sums a file's
    windows forward from each edit, so the shipped windows have to tile the
    file's whole timeline. Any hole and the remaining counts understate by
    an unknown amount, or — worse — swallow the agent's own next edit and
    report it as somebody else's work.

    A hole is anything that costs one applied edit its window or its pair: a
    missing or unreadable PreToolUse snapshot, an edit whose post state was
    unpredictable, an unreadable observed file state, an oversized side. Each of
    those is invisible to the server, so the honest move is to drop the
    file's counts here. Other files in the session keep theirs.
    """
    edits = (
        extract_edits_pi(entries, session_id, source_path=source_path)
        if agent == "pi"
        else _PARSERS[agent](entries, session_id)
    )
    observed_file_states = _observed_file_states(edits)
    deltas = (
        external_deltas(
            _load_snapshots(session_id),
            observed_file_states,
            {e["tool_use_id"] for e in edits},
        )
        if session_id
        else {}
    )
    shippable, holed = [], set()
    for edit in edits:
        observed_file_text = observed_file_states[edit["file_path"]]
        if observed_file_text is None:
            holed.add(edit["file_path"])
            continue
        if (
            len(edit["applied_text"].encode("utf-8", "replace")) > MAX_TEXT_BYTES
            or len(observed_file_text.encode("utf-8", "replace")) > MAX_TEXT_BYTES
        ):
            _trail(f"pair over {MAX_TEXT_BYTES}B for {edit['file_path']}; dropped")
            holed.add(edit["file_path"])
            continue
        shippable.append((edit, observed_file_text))
    # The whole-timeline check: an applied edit missing from `deltas` had no
    # usable window, which leaves a gap its neighbours would silently absorb.
    for edit in edits:
        if edit["tool_use_id"] not in deltas:
            holed.add(edit["file_path"])
    pairs = []
    for edit, observed_file_text in shippable:
        pair = {**edit, "observed_file_text": observed_file_text}
        if edit["file_path"] not in holed:
            pair["external_lines_added"], pair["external_lines_removed"] = deltas[
                edit["tool_use_id"]
            ]
        pairs.append(pair)
    return pairs


def _resource_user_id() -> str | None:
    for part in os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "").split(","):
        key, sep, value = part.partition("=")
        if sep and key.strip() == "user.id" and value.strip():
            return value.strip()
    return None


def build_payload(
    session_id: str,
    pairs: list[dict],
    *,
    agent: str,
    rejected: list[dict] | None = None,
    linkages: list[dict] | None = None,
) -> dict:
    """The OTLP/JSON resource and ordered records for the session.

    Applied edits ship as ``sediment.edit_observation``; refused ones ship as
    ``sediment.rejected_edit`` records. Publication packs these independent
    records into bounded requests; the server self-filters each record by body.
    """

    def attrs(mapping: dict[str, str | int]) -> list[dict]:
        return [
            {
                "key": k,
                # OTLP/JSON encodes int64 as a string; the counts are the only
                # non-string attribute this client emits.
                "value": {"intValue": str(v)}
                if isinstance(v, int)
                else {"stringValue": v},
            }
            for k, v in mapping.items()
        ]

    records = [
        {
            "body": {"stringValue": EVENT_NAME},
            "timeUnixNano": str(p["time_unix_nano"]),
            "attributes": attrs(
                {
                    "session.id": session_id,
                    "tool_use_id": p["tool_use_id"],
                    "tool_name": p["tool_name"],
                    "file_path": p["file_path"],
                    "applied_text": p["applied_text"],
                    "observed_file_text": p["observed_file_text"],
                    "agent": agent,
                    # Omitted, never zeroed, when no window covered the call.
                    **{
                        k: p[k]
                        for k in ("external_lines_added", "external_lines_removed")
                        if k in p
                    },
                }
            ),
        }
        for p in pairs
    ]
    records += [
        {
            "body": {"stringValue": REJECTED_EVENT_NAME},
            "timeUnixNano": str(r["time_unix_nano"]),
            "attributes": attrs(
                {
                    "session.id": session_id,
                    "tool_use_id": r["tool_use_id"],
                    "tool_name": r["tool_name"],
                    "file_path": r["file_path"],
                    # A refused edit never reached the file, so there is no
                    # observed file state to ship.
                    "proposed": r["proposed"],
                    "agent": agent,
                }
            ),
        }
        for r in rejected or []
    ]
    records += [
        {
            "body": {"stringValue": RETRY_LINKAGE_EVENT_NAME},
            "timeUnixNano": str(linkage["time_unix_nano"]),
            "attributes": attrs(
                {
                    "session.id": session_id,
                    "rejected_call_id": linkage["rejected_call_id"],
                    "accepted_call_id": linkage["accepted_call_id"],
                    "tool_name": linkage["tool_name"],
                    "file_path": linkage["file_path"],
                    "agent": agent,
                }
            ),
        }
        for linkage in linkages or []
    ]
    user_id = _resource_user_id()
    resource = {"attributes": attrs({"user.id": user_id})} if user_id else {}
    return {
        "resourceLogs": [{"resource": resource, "scopeLogs": [{"logRecords": records}]}]
    }


def _validated_endpoint(configured: str | None) -> str | None:
    """Validate an explicit capture endpoint without including its value in errors."""
    if not configured:
        return None
    return _delivery_client().configured_destination(
        "otlp", {"SEDIMENT_OTLP_ENDPOINT": configured}
    )


def _endpoint() -> str | None:
    # Deliberately NOT OTEL_EXPORTER_OTLP_ENDPOINT: a generic collector must
    # never receive edit text because another tool enabled shared telemetry.
    try:
        return _validated_endpoint(os.environ.get("SEDIMENT_OTLP_ENDPOINT"))
    except ValueError:
        _trail(
            "configured ingest endpoint rejected; remote endpoints require HTTPS "
            "and HTTP is limited to literal loopback hosts"
        )
        return None


def _delivery_client():
    """Load the same stdlib owner from a package or adjacent standalone copy."""
    global _DELIVERY_MODULE
    if _DELIVERY_MODULE is None:
        if __package__:
            from . import delivery

            _DELIVERY_MODULE = delivery
        else:
            directory = Path(__file__).resolve().parent
            path = directory / "delivery.py"
            if not path.is_file():
                path = directory / "sediment_delivery.py"
            spec = importlib.util.spec_from_file_location("_sediment_delivery", path)
            if spec is None or spec.loader is None:
                raise RuntimeError("helper_unavailable")
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            _DELIVERY_MODULE = module
    return _DELIVERY_MODULE


def _post(url: str, payload: dict, *, buffered: bool = False) -> None:
    """Accept one prepared payload; Cursor's existing caller remains direct."""
    delivery = _delivery_client()
    request = delivery.prepare_request(
        "otlp", url, json.dumps(payload, allow_nan=False).encode("utf-8")
    )
    if buffered:
        result = delivery.deliver(request)
    else:
        result = delivery.send_once(request)
    if result.status not in {"queued", "acknowledged"}:
        raise RuntimeError(result.reason)


def _prepare_requests(url: str, payload: dict):
    """Pack this client's independent records, encoding each record once.

    This envelope belongs to build_payload, not arbitrary native OTLP batches:
    native translators may join decision and result records within a request.
    Preserve the same JSON encoding as _post, including envelope and separators.
    """
    delivery = _delivery_client()
    resource = payload["resourceLogs"][0]
    prefix = (
        b'{"resourceLogs": [{"resource": '
        + json.dumps(resource["resource"], allow_nan=False).encode("utf-8")
        + b', "scopeLogs": [{"logRecords": ['
    )
    suffix = b"]}]}]}"
    envelope_bytes = len(prefix) + len(suffix)
    prepared = []
    chunk = []
    chunk_bytes = envelope_bytes
    oversized = 0

    def finish():
        prepared.append(
            (
                delivery.prepare_request(
                    "otlp", url, prefix + b", ".join(chunk) + suffix
                ),
                len(chunk),
            )
        )

    for record in resource["scopeLogs"][0]["logRecords"]:
        encoded = json.dumps(record, allow_nan=False).encode("utf-8")
        if envelope_bytes + len(encoded) > delivery.MAX_ENTRY_BYTES:
            oversized += 1
            continue
        if chunk and chunk_bytes + 2 + len(encoded) > delivery.MAX_ENTRY_BYTES:
            finish()
            chunk = []
            chunk_bytes = envelope_bytes
        chunk_bytes += len(encoded) + (2 if chunk else 0)
        chunk.append(encoded)
    if chunk:
        finish()
    return prepared, oversized


def _publish(url: str, payload: dict) -> bool:
    """Attempt each prepared request once and report publication by unit."""
    delivery = _delivery_client()
    candidates = Counter(
        record["body"]["stringValue"]
        for record in payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
    )
    prepared, oversized = _prepare_requests(url, payload)
    summary = {
        "candidate_records": {
            "edit_observations": candidates[EVENT_NAME],
            "rejected_edits": candidates[REJECTED_EVENT_NAME],
            "retry_linkages": candidates[RETRY_LINKAGE_EVENT_NAME],
        },
        "prepared_requests": len(prepared),
        "prepared_records": sum(count for _, count in prepared),
        "queued_requests": 0,
        "queued_records": 0,
        "acknowledged_requests": 0,
        "acknowledged_records": 0,
        "unsuccessful_requests": {},
        "unsuccessful_records": {},
        "record_too_large": oversized,
        "unsubmitted_requests": 0,
        "unsubmitted_records": 0,
        "delivery_mode": "buffered"
        if os.environ.get("SEDIMENT_DELIVERY_DIR")
        else "best_effort",
    }
    for index, (request, records) in enumerate(prepared):
        interrupted = False
        try:
            result = delivery.deliver(request)
            status = result.status
            if status not in {
                "queued",
                "acknowledged",
                "pending",
                "blocked",
                "declined",
            }:
                status = "invalid_disposition"
        except Exception as exc:
            # Exception text can contain payloads or credentials. These reasons
            # describe the boundary only; the attempted request isn't a suffix.
            status = "io_error" if isinstance(exc, OSError) else "publication_error"
            interrupted = True
        if status in {"queued", "acknowledged"}:
            summary[f"{status}_requests"] += 1
            summary[f"{status}_records"] += records
        else:
            for unit, count in (("requests", 1), ("records", records)):
                failures = summary[f"unsuccessful_{unit}"]
                failures[status] = failures.get(status, 0) + count
        if interrupted:
            suffix = prepared[index + 1 :]
            summary["unsubmitted_requests"] = len(suffix)
            summary["unsubmitted_records"] = sum(count for _, count in suffix)
            break
    complete = not (oversized or summary["unsuccessful_requests"])
    summary["outcome"] = "complete" if complete else "partial"
    _trail("emission_summary " + json.dumps(summary, sort_keys=True))
    if not complete:
        _trail(
            "partial_publication; snapshots retained; queued requests remain replayable; "
            "observations not durably enqueued can be lost and snapshots cannot reconstruct them"
        )
    return complete


def main(argv: list[str] | None = None) -> int:
    """The hook entry point — SessionEnd, or ``snapshot`` for PreToolUse.

    Every path exits 0.
    """
    try:
        url = _endpoint()
        if url is None:
            return 0  # not opted in — and nothing is cached either
        args = sys.argv[1:] if argv is None else argv
        if "--agent" not in args:
            _trail("missing --agent; skipping")
            return 0
        index = args.index("--agent")
        if index + 1 >= len(args):
            _trail("missing --agent value; skipping")
            return 0
        agent = args[index + 1]
        if agent not in _PARSERS:
            _trail(f"unknown --agent {agent!r} (known: {sorted(_PARSERS)}); skipping")
            return 0
        hook = json.loads(sys.stdin.read() or "{}")
        if not isinstance(hook, dict):
            return 0
        if args and args[0] == "snapshot":  # positional: the PreToolUse hook
            cmd_snapshot(hook)
            return 0
        session_id = hook.get("session_id")
        transcript_path = hook.get("transcript_path")
        if not (
            isinstance(session_id, str)
            and session_id
            and isinstance(transcript_path, str)
            and transcript_path
        ):
            return 0
        entries = list(_iter_jsonl(Path(transcript_path)))
        pairs = build_pairs(
            entries, session_id, agent=agent, source_path=Path(transcript_path)
        )
        # Refusals are Claude Code-only for now; the pi parser cannot tell a
        # refusal from a tool failure, so it contributes nothing here.
        rejected = (
            extract_rejected_edits(entries, session_id)
            if agent == "claude-code"
            else []
        )
        linkages = (
            extract_retry_linkages(entries, session_id)
            if agent == "claude-code"
            else []
        )
        if pairs or rejected or linkages:
            complete = _publish(
                url,
                build_payload(
                    session_id,
                    pairs,
                    agent=agent,
                    rejected=rejected,
                    linkages=linkages,
                ),
            )
            if not complete:
                return 0
        # Durable enqueue or acknowledged direct delivery owns the prepared
        # bytes. A decline preserves snapshots; replay never re-extracts them.
        _clear_cache(session_id)
    except Exception:  # fail-soft: never fail the session end
        _trail("emit failed; Edit observation coverage is incomplete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
