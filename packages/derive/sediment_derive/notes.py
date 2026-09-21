# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Attribution notes reader.

Reads the ``refs/notes/sediment`` note a client stamper wrote onto a commit,
out of the local bare mirror — notes are not served over GitHub's REST API, so this path is
git-protocol only, exactly like the mirror diff source. The mirror's refresh
fetch already carries ``refs/notes/sediment*`` unconditionally
(``mirror.FETCH_REFSPECS``), so the note is present after every push; no
transport work happens here.

The note models live here, not in ``sediment_core.models``: a note is the
git-side wire contract this derivation reads, never a persisted fact shape.

Fail-soft is the whole posture: a missing, malformed, unknown-version, or
privacy-contract-violating note yields ``None`` and the derivation falls
through to the jaccard fallback (similarity). Reading a note must never
crash a derivation.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from .mirror import MirrorError, _git

logger = logging.getLogger("sediment.derive.notes")

# The notes ref is a fixed constant on both client and server: a knob here is
# a stamper/reader mismatch waiting to happen. ``--ref=sediment`` expands to
# refs/notes/sediment — the same ref the mirror fetches and the stamper writes.
_NOTES_REF = "sediment"

# A git object name: hex, 7–64 chars (abbreviated through SHA-256). commit_sha
# originates in webhook payloads, so validate it before it ever reaches git —
# a value like "--output=/etc/x" then can't be read as a git option (defence
# in depth on top of --end-of-options in the git call below).
_HEX_OBJECT = re.compile(r"[0-9a-fA-F]{7,64}")

# A well-formed note is {v, sessions:[{tool, session_id, stamped_at}, …]} — a
# few hundred bytes even with many sessions, or a handful of such payloads
# concatenated by note rewriting (see _parse_note_body). Anything past this
# is hostile or corrupt; reject before parsing so a giant/deeply-nested body
# can't recurse or allocate its way into a crash.
_MAX_NOTE_BYTES = 64 * 1024


class AttributionSession(BaseModel):
    """One agent session that contributed to a commit, as recorded in the note.

    ``extra="forbid"`` is the privacy contract in code: a stamp carrying prompt
    text, file paths, diff content, model names, or any developer-identifying
    field beyond these three fails validation — the reader then drops the
    whole note (→ jaccard fallback) rather than ingest content the note
    must never carry.
    """

    model_config = ConfigDict(extra="forbid")

    # Informational provenance only — the notes join uses session_id, never
    # this. Kept a plain str (not the AgentHarness enum) so a newer
    # stamper's tool value can't fail-soft an otherwise-valid note.
    tool: str
    session_id: str  # opaque agent-session id; joins InferenceCall.session_id
    stamped_at: str  # ISO-8601 UTC — when the stamper wrote the note


class AttributionNote(BaseModel):
    """The ``refs/notes/sediment`` payload contract. One note per commit.

    Same ``extra="forbid"`` privacy guard at the top level. ``v`` is the note
    schema version, pinned to ``1`` (``Literal[1]``): a v2 note fails
    validation and the reader fail-softs to the jaccard fallback, so the
    version gate lives in the schema rather than a hand-written check in the
    reader.
    """

    model_config = ConfigDict(extra="forbid")

    v: Literal[1]
    sessions: list[AttributionSession]

    @field_validator("v", mode="before")
    @classmethod
    def _require_schema_version_one(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("v must be the integer 1")
        return value


def _parse_note_body(raw: str) -> AttributionNote:
    """Parse a note body holding ONE OR MORE concatenated v1 payloads.

    The stamper installer sets ``notes.rewriteRef``; ``notes.rewriteMode``
    defaults to ``concatenate``, so on amend/rebase/squash git appends the
    rewritten commit's copied note after the hook-written one, blank-line
    separated. Every payload must validate — one bad payload drops the whole
    note (the single-payload privacy posture, unchanged). Sessions are
    unioned, first occurrence per (tool, session_id) wins.
    """
    decoder = json.JSONDecoder()
    notes: list[AttributionNote] = []
    idx = 0
    while idx < len(raw):
        if raw[idx].isspace():
            idx += 1
            continue
        payload, idx = decoder.raw_decode(raw, idx)
        notes.append(AttributionNote.model_validate(payload))
    if not notes:
        raise ValueError("empty note body")
    seen: set[tuple[str, str]] = set()
    merged: list[AttributionSession] = []
    for note in notes:
        for session in note.sessions:
            if (session.tool, session.session_id) not in seen:
                seen.add((session.tool, session.session_id))
                merged.append(session)
    return AttributionNote(v=1, sessions=merged)


def read_commit_note(mirror_path: Path, commit_sha: str) -> AttributionNote | None:
    """The commit's attribution note out of the bare mirror, or None.

    None covers every no-note case — no note, bad SHA, oversized body,
    malformed JSON, schema/privacy violation — each logged, none raised.
    """
    if not _HEX_OBJECT.fullmatch(commit_sha):
        logger.warning(
            "attribution_note_bad_sha", extra={"commit_sha": commit_sha[:100]}
        )
        return None

    try:
        raw = _git(
            mirror_path,
            "notes",
            f"--ref={_NOTES_REF}",
            "show",
            "--end-of-options",
            commit_sha,
        )
    except MirrorError:
        # Overwhelmingly the ordinary "no note for this commit" case (git
        # exits non-zero) until stamper adoption is universal; also absorbs a
        # genuine git failure. Either way notes attribution has nothing →
        # jaccard fallback.
        # Debug, not warning: an unstamped commit is not a problem.
        logger.debug("attribution_note_absent", extra={"commit_sha": commit_sha})
        return None

    # Cap the body before parsing — a note is a handful of small session
    # records, so anything large is hostile or corrupt, and an unbounded
    # deeply-nested JSON body would make json.loads recurse/allocate. Measure
    # bytes, not code points: a note of astral-plane characters is ~4x its
    # len() on the wire and must not slip under a character-counted cap.
    size = len(raw.encode("utf-8", errors="replace"))
    if size > _MAX_NOTE_BYTES:
        logger.warning(
            "attribution_note_too_large",
            extra={"commit_sha": commit_sha, "bytes": size},
        )
        return None

    try:
        return _parse_note_body(raw)
    except Exception as exc:
        # Broad on purpose: beyond malformed JSON (JSONDecodeError) and a
        # privacy-contract violation (ValidationError from extra="forbid"
        # rejecting prompts/paths/content/model-names/identity), a hostile
        # note can raise RecursionError (deeply nested JSON) — none of which
        # subclass the others. Any of them degrades to the jaccard
        # fallback; none may propagate out of the derivation.
        logger.warning(
            "attribution_note_invalid",
            extra={"commit_sha": commit_sha, "error": str(exc)},
        )
        return None
