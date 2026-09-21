# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Shared Git section, path, and hunk interpretation for Derivations and diff-SFT.
Invalid sections contribute neither paths nor partial additions.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from io import StringIO
from typing import Literal

# Source-code extensions attribution scores; everything else is skipped.
CODE_EXTENSIONS = {
    ".py",
    ".ts",
    ".js",
    ".tsx",
    ".jsx",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".scala",
    ".rb",
    ".php",
    ".cs",
    ".cpp",
    ".c",
    ".h",
    ".tf",
    ".hcl",
    ".sql",
    ".sh",
    ".bash",
}
# Generated / vendored files to skip even when the extension matches.
SKIP_PATTERNS = {
    "package-lock.json",
    "yarn.lock",
    "poetry.lock",
    "uv.lock",
    ".min.js",
    ".min.css",
}


DiffSkipReason = Literal["unsupported_diff_section", "malformed_diff_section"]
DIFF_SKIP_REASONS: tuple[DiffSkipReason, ...] = (
    "unsupported_diff_section",
    "malformed_diff_section",
)
logger = logging.getLogger("sediment.derive.diff")
_HUNK = re.compile(r"@@ -([0-9]+)(?:,([0-9]+))? \+([0-9]+)(?:,([0-9]+))? @@(?: .*)?")
_INDEX = re.compile(r"index [0-9a-f]+\.\.[0-9a-f]+(?: [0-7]{6})?")
_MODE = re.compile(r"(?:new file|deleted file|old|new) mode ([0-7]{6})")
_SIMILARITY = re.compile(r"(?:dis)?similarity index (?:100|[0-9]{1,2})%")
_ESCAPES = {
    "a": 7,
    "b": 8,
    "t": 9,
    "n": 10,
    "v": 11,
    "f": 12,
    "r": 13,
    '"': 34,
    "\\": 92,
}


@dataclass(frozen=True)
class FileDiff:
    """Compatibility view of a file's concatenated additions."""

    file_path: str
    added_lines: str


@dataclass(frozen=True)
class DiffSection:
    """One validated Git section, retaining its exact original patch text."""

    file_path: str
    added_lines: str
    patch: str


@dataclass
class DiffParseResult:
    """Valid sections in wire order and one diagnostic per declined section."""

    sections: list[DiffSection] = field(default_factory=list)
    skipped: Counter[DiffSkipReason] = field(default_factory=Counter)


class _InvalidSection(ValueError):
    def __init__(self, reason: DiffSkipReason = "malformed_diff_section") -> None:
        self.reason = reason
        super().__init__(reason)


def _git_path(token: str) -> str:
    """Decode Git's C quoting, including octal UTF-8 bytes, without shell rules."""
    if not token:
        raise _InvalidSection()
    if not token.startswith('"'):
        if any(ord(char) < 32 or ord(char) == 127 or char in '\\"' for char in token):
            raise _InvalidSection()
        return token
    if not token.endswith('"'):
        raise _InvalidSection()
    encoded = bytearray()
    index = 1
    while index < len(token) - 1:
        char = token[index]
        if char == '"':
            raise _InvalidSection()
        if char != "\\":
            encoded.extend(char.encode("utf-8"))
            index += 1
            continue
        index += 1
        if index >= len(token) - 1:
            raise _InvalidSection()
        escape = token[index]
        if escape in _ESCAPES:
            encoded.append(_ESCAPES[escape])
            index += 1
        elif escape in "0123" and re.fullmatch(r"[0-7]{3}", token[index : index + 3]):
            encoded.append(int(token[index : index + 3], 8))
            index += 3
        else:
            raise _InvalidSection()
    try:
        path = encoded.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _InvalidSection("unsupported_diff_section") from exc
    if not path or "\0" in path:
        raise _InvalidSection()
    return path


def _file_header(token: str, prefix: str) -> str | None:
    # Git appends a separator tab to an unquoted path containing spaces.
    path = _git_path(token.removesuffix("\t"))
    if path == "/dev/null":
        return None
    if not path.startswith(prefix) or len(path) == len(prefix):
        raise _InvalidSection()
    return path[len(prefix) :]


def _section_path(header: str, old: str | None, new: str | None) -> str:
    """Validate both diff header paths against unambiguous per-file headers."""
    candidates = []
    for index, char in enumerate(header):
        if char != " ":
            continue
        try:
            source = _file_header(header[:index], "a/")
            target = _file_header(header[index + 1 :], "b/")
        except _InvalidSection:
            continue
        if source is None or target is None:
            continue
        if old is None and new is None:
            if source != target:
                continue
        elif source != (old if old is not None else new) or target != (
            new if new is not None else old
        ):
            continue
        candidates.append(target if new is not None else source)
    if len(candidates) != 1:
        raise _InvalidSection()
    return candidates[0]


def _hunk_additions(lines: list[str]) -> str:
    old_remaining = new_remaining = 0
    old_end = new_end = 1
    previous_body = False
    added = []
    for line in lines:
        text = line.removesuffix("\n")
        if text == "\\ No newline at end of file":
            if not previous_body:
                raise _InvalidSection()
            previous_body = False
            continue
        if text.startswith("@@"):
            if old_remaining or new_remaining:
                raise _InvalidSection()
            match = _HUNK.fullmatch(text)
            if match is None:
                raise _InvalidSection()
            old_start, old_count, new_start, new_count = match.groups()
            old_remaining = int(old_count) if old_count is not None else 1
            new_remaining = int(new_count) if new_count is not None else 1
            if (old_remaining and int(old_start) == 0) or (
                new_remaining and int(new_start) == 0
            ):
                raise _InvalidSection()
            old_begin = int(old_start) + (old_remaining == 0)
            new_begin = int(new_start) + (new_remaining == 0)
            if old_begin < old_end or old_begin - old_end != new_begin - new_end:
                raise _InvalidSection()
            old_end = old_begin + old_remaining
            new_end = new_begin + new_remaining
            previous_body = False
            continue
        if not text or not (old_remaining or new_remaining):
            raise _InvalidSection()
        if text[0] == "+":
            new_remaining -= 1
            added.append(text[1:])
        elif text[0] == "-":
            old_remaining -= 1
        elif text[0] == " ":
            old_remaining -= 1
            new_remaining -= 1
        else:
            raise _InvalidSection()
        if old_remaining < 0 or new_remaining < 0:
            raise _InvalidSection()
        previous_body = True
    if old_remaining or new_remaining:
        raise _InvalidSection()
    return "\n".join(added)


def _parse_section(patch: str) -> DiffSection:
    lines = list(StringIO(patch))
    if not lines[0].startswith("diff --git "):
        raise _InvalidSection("unsupported_diff_section")
    headers: dict[str, str | None] = {}
    rename: dict[str, str] = {}
    has_change_metadata = False
    body_at = len(lines)
    for index, line in enumerate(lines[1:], 1):
        text = line.removesuffix("\n")
        if text.startswith(
            ("Binary files ", "GIT binary patch", "@@@", "copy from ", "copy to ")
        ):
            raise _InvalidSection("unsupported_diff_section")
        if text.startswith("@@"):
            body_at = index
            break
        if text.startswith("--- ") and not headers:
            headers["old"] = _file_header(text[4:], "a/")
        elif text.startswith("+++ ") and set(headers) == {"old"}:
            headers["new"] = _file_header(text[4:], "b/")
        elif headers:
            raise _InvalidSection()
        elif text.startswith(("rename from ", "rename to ")):
            key = "old" if text.startswith("rename from ") else "new"
            if key in rename:
                raise _InvalidSection()
            rename[key] = _git_path(text.split(" ", 2)[2])
            has_change_metadata = True
        elif match := _MODE.fullmatch(text):
            if match[1] == "160000":
                raise _InvalidSection("unsupported_diff_section")
            has_change_metadata = True
        elif _INDEX.fullmatch(text) or _SIMILARITY.fullmatch(text):
            if text.endswith(" 160000"):
                raise _InvalidSection("unsupported_diff_section")
        else:
            raise _InvalidSection()
    if headers and (set(headers) != {"old", "new"} or body_at == len(lines)):
        raise _InvalidSection()
    if body_at < len(lines) and not headers:
        raise _InvalidSection()
    if not headers and not has_change_metadata:
        raise _InvalidSection()
    if rename and (set(rename) != {"old", "new"} or headers and rename != headers):
        raise _InvalidSection()
    identity = headers or rename
    path = _section_path(
        lines[0][11:].removesuffix("\n"), identity.get("old"), identity.get("new")
    )
    if headers and headers["old"] is None and headers["new"] is None:
        raise _InvalidSection()
    return DiffSection(path, _hunk_additions(lines[body_at:]), patch)


def parse_diff_sections(raw_diff: str) -> DiffParseResult:
    """Parse the mirror's Git dialect, discarding invalid sections as a whole."""
    result = DiffParseResult()
    sections: list[str] = []
    current: list[str] = []
    # StringIO splits only on LF; splitlines also splits authored control text.
    for line in StringIO(raw_diff):
        if line.startswith("diff ") and current:
            sections.append("".join(current))
            current = []
        current.append(line)
    if current:
        sections.append("".join(current))
    for index, patch in enumerate(sections):
        if not patch.strip():
            continue
        try:
            result.sections.append(_parse_section(patch))
        except (ValueError, UnicodeError) as exc:
            reason = (
                exc.reason
                if isinstance(exc, _InvalidSection)
                else "malformed_diff_section"
            )
            result.skipped[reason] += 1
            logger.warning(
                "git_diff_section_declined reason=%s section_index=%s", reason, index
            )
    return result


def parse_unified_diff(raw_diff: str) -> list[FileDiff]:
    """Return the existing additions-only public view of valid Git sections."""
    return [
        FileDiff(section.file_path, section.added_lines)
        for section in parse_diff_sections(raw_diff).sections
    ]


def is_code_file(file_path: str) -> bool:
    """Filter to source code files only — skip docs, config, lockfiles."""
    if any(p in file_path for p in SKIP_PATTERNS):
        return False
    return any(file_path.endswith(ext) for ext in CODE_EXTENSIONS)
