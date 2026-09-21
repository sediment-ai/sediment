# SPDX-License-Identifier: AGPL-3.0-or-later
"""Disposable local turn-level worktree-observation prototype.

The Claude Code ``UserPromptSubmit`` and ``Stop`` hooks delimit one turn. The
prototype reads only ``session_id``, optional ``turn_id``, ``cwd``, and
``hook_event_name`` from their JSON payloads. It never opens the transcript or
reads prompt, response, tool-result, shell-output, or environment fields.

The start record keeps content hashes and references to blobs that Git already
stores. It doesn't copy worktree contents. The end record contains hashes and
changed-line counts for changed paths plus one bounded unified patch when every
before state can be reconstructed from an existing Git blob.

This script is a spike. It doesn't emit a Fact or call a Sediment endpoint.
Every command exits zero so an observation failure can't block the developer
workflow.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import stat
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
DEFAULT_MAX_PATCH_BYTES = 64 * 1024
_START_EVENT = "UserPromptSubmit"
_END_EVENT = "Stop"
_STATE_ENV = "SEDIMENT_TURN_OBSERVATION_STATE"
_UNSUPPORTED_CLAIMS = [
    "actor_identity_unestablished",
    "correctness_unestablished_without_verifier_result",
    "human_authorship_unestablished",
    "turn_causality_unestablished",
]


def canonical_json(value: object) -> str:
    """Serialize a spike record in its byte-deterministic JSON form."""
    return json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"


def _trail(message: str) -> None:
    print(f"turn-worktree-observation-spike: {message}", file=sys.stderr)


def _git(repo: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        input=input_bytes,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout


def _repo_root(cwd: object) -> Path:
    if not isinstance(cwd, str) or not cwd:
        raise ValueError("missing cwd")
    root = _git(Path(cwd), "rev-parse", "--show-toplevel")
    return Path(os.fsdecode(root.rstrip(b"\n"))).resolve()


def _index_blobs(repo: Path) -> dict[str, str]:
    blobs: dict[str, str] = {}
    for raw in _git(repo, "ls-files", "--stage", "-z").split(b"\0"):
        if not raw:
            continue
        metadata, separator, path_bytes = raw.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3 or fields[2] != b"0":
            continue
        blobs[os.fsdecode(path_bytes)] = fields[1].decode("ascii")
    return blobs


def _listed_paths(repo: Path) -> list[str]:
    raw = _git(
        repo,
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "-z",
    )
    return sorted({os.fsdecode(path) for path in raw.split(b"\0") if path})


def _read_path(repo: Path, relative: str) -> tuple[bytes, str]:
    path = repo / relative
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode):
        raise ValueError("symlink is outside the spike contract")
    if stat.S_ISREG(mode):
        return path.read_bytes(), "regular"
    raise ValueError("unsupported file type")


def _worktree_snapshot(
    repo: Path, *, capture_blob_references: bool
) -> tuple[dict[str, dict[str, Any]], dict[str, bytes], list[str], list[str]]:
    index_blobs = _index_blobs(repo) if capture_blob_references else {}
    manifest: dict[str, dict[str, Any]] = {}
    contents: dict[str, bytes] = {}
    skip_reasons: set[str] = set()
    unknown_paths: set[str] = set()
    for relative in _listed_paths(repo):
        try:
            content, file_type = _read_path(repo, relative)
        except FileNotFoundError:
            continue
        except ValueError:
            skip_reasons.add("unsupported_file_type")
            unknown_paths.add(relative)
            continue
        except OSError:
            skip_reasons.add("unreadable_path")
            unknown_paths.add(relative)
            continue
        digest = hashlib.sha256(content).hexdigest()
        record: dict[str, Any] = {
            "bytes": len(content),
            "file_type": file_type,
            "sha256": digest,
        }
        blob_oid = index_blobs.get(relative)
        if blob_oid:
            try:
                candidate_oid = os.fsdecode(
                    _git(repo, "hash-object", "--stdin", input_bytes=content).strip()
                )
            except subprocess.CalledProcessError:
                candidate_oid = ""
            if candidate_oid == blob_oid:
                record["git_blob_oid"] = blob_oid
        manifest[relative] = record
        contents[relative] = content
    return manifest, contents, sorted(skip_reasons), sorted(unknown_paths)


def _state_key(repo: Path, session_id: str) -> str:
    value = f"{repo}\0{session_id}".encode("utf-8", "surrogateescape")
    return hashlib.sha256(value).hexdigest()


def _state_path(state_dir: Path, repo: Path, session_id: str) -> Path:
    return state_dir / f"active-{_state_key(repo, session_id)}.json"


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(canonical_json(payload), encoding="utf-8")
    temporary.replace(path)


def _load_state(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _active_states(state_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    states = []
    try:
        paths = sorted(state_dir.glob("active-*.json"))
    except OSError:
        return []
    for path in paths:
        state_value = _load_state(path)
        if state_value is not None:
            states.append((path, state_value))
    return states


def _next_turn_id(state_dir: Path, repo: Path, session_id: str) -> str:
    key = _state_key(repo, session_id)
    counter_path = state_dir / f"counter-{key}.json"
    previous = _load_state(counter_path) or {}
    sequence = previous.get("sequence", 0)
    if isinstance(sequence, bool) or not isinstance(sequence, int):
        sequence = 0
    sequence += 1
    _write_json_atomic(counter_path, {"sequence": sequence})
    return f"turn-{sequence:06d}"


def _start_turn(hook: dict[str, Any], state_dir: Path) -> dict[str, Any]:
    session_id = hook.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("missing session_id")
    repo = _repo_root(hook.get("cwd"))
    turn_id = hook.get("turn_id")
    if not isinstance(turn_id, str) or not turn_id:
        turn_id = _next_turn_id(state_dir, repo, session_id)
    active_path = _state_path(state_dir, repo, session_id)
    active = _load_state(active_path)
    superseded_turn = None
    if active is not None and active.get("turn_id") != turn_id:
        superseded_turn = _missing_turn_end_record(active)
    manifest, _contents, skip_reasons, unknown_paths = _worktree_snapshot(
        repo, capture_blob_references=True
    )
    overlaps: set[str] = set()
    for path, active in _active_states(state_dir):
        other_session = active.get("session_id")
        if active.get("repo") != str(repo) or other_session == session_id:
            continue
        if isinstance(other_session, str):
            overlaps.add(other_session)
            other_overlaps = active.get("overlap_sessions")
            if not isinstance(other_overlaps, list):
                other_overlaps = []
            active["overlap_sessions"] = sorted({*other_overlaps, session_id})
            _write_json_atomic(path, active)
    state_value = {
        "harness": "claude-code",
        "manifest": manifest,
        "overlap_sessions": sorted(overlaps),
        "repo": str(repo),
        "schema_version": SCHEMA_VERSION,
        "session_id": session_id,
        "skip_reasons": skip_reasons,
        "turn_id": turn_id,
        "unknown_paths": unknown_paths,
    }
    _write_json_atomic(active_path, state_value)
    result = {
        "harness": "claude-code",
        "kind": "turn_start_recorded",
        "schema_version": SCHEMA_VERSION,
        "session_id": session_id,
        "skip_reasons": skip_reasons,
        "turn_id": turn_id,
    }
    if superseded_turn is not None:
        result["superseded_turn"] = superseded_turn
    return result


def _blob_content(repo: Path, record: dict[str, Any]) -> bytes | None:
    blob_oid = record.get("git_blob_oid")
    if not isinstance(blob_oid, str) or not blob_oid:
        return None
    try:
        content = _git(repo, "cat-file", "blob", blob_oid)
    except subprocess.CalledProcessError:
        return None
    if hashlib.sha256(content).hexdigest() != record.get("sha256"):
        return None
    return content


def _line_delta(before: bytes, after: bytes) -> tuple[int, int] | None:
    try:
        before_text = before.decode("utf-8")
        after_text = after.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if "\0" in before_text or "\0" in after_text:
        return None
    matcher = difflib.SequenceMatcher(
        None, before_text.splitlines(), after_text.splitlines(), autojunk=False
    )
    added = removed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return added, removed


def _change_sort_key(change: dict[str, Any]) -> tuple[str, str]:
    return (
        change.get("old_path") or change.get("path") or "",
        change.get("new_path") or "",
    )


def _detect_renames(
    before_only: set[str],
    after_only: set[str],
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
) -> tuple[list[tuple[str, str]], bool]:
    deleted_by_hash: dict[str, list[str]] = {}
    written_by_hash: dict[str, list[str]] = {}
    for path in before_only:
        deleted_by_hash.setdefault(before[path]["sha256"], []).append(path)
    for path in after_only:
        written_by_hash.setdefault(after[path]["sha256"], []).append(path)
    renames = []
    ambiguous = False
    for digest in sorted(set(deleted_by_hash) & set(written_by_hash)):
        old_paths = sorted(deleted_by_hash[digest])
        new_paths = sorted(written_by_hash[digest])
        if len(old_paths) == len(new_paths) == 1:
            renames.append((old_paths[0], new_paths[0]))
        else:
            ambiguous = True
    return renames, ambiguous


def _unified_diff(
    before: bytes, after: bytes, old_path: str | None, new_path: str | None
) -> str | None:
    try:
        before_text = before.decode("utf-8")
        after_text = after.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if "\0" in before_text or "\0" in after_text:
        return None
    old_label = "/dev/null" if old_path is None else f"a/{old_path}"
    new_label = "/dev/null" if new_path is None else f"b/{new_path}"
    lines = difflib.unified_diff(
        before_text.splitlines(keepends=True),
        after_text.splitlines(keepends=True),
        fromfile=old_label,
        tofile=new_label,
        lineterm="\n",
    )
    body = "".join(
        line if line.endswith("\n") else f"{line}\n\\ No newline at end of file\n"
        for line in lines
    )
    shown_old = old_path if old_path is not None else new_path
    shown_new = new_path if new_path is not None else old_path
    return f"diff --git a/{shown_old} b/{shown_new}\n{body}"


def _build_observation(
    state_value: dict[str, Any], repo: Path, max_patch_bytes: int
) -> dict[str, Any]:
    before = state_value["manifest"]
    after, after_contents, end_skips, end_unknown_paths = _worktree_snapshot(
        repo, capture_blob_references=False
    )
    start_unknown_paths = set(state_value.get("unknown_paths", []))
    before_paths = set(before)
    after_paths = set(after)
    common_changed = {
        path
        for path in before_paths & after_paths
        if before[path]["sha256"] != after[path]["sha256"]
        or before[path]["file_type"] != after[path]["file_type"]
    }
    before_only = before_paths - after_paths - set(end_unknown_paths)
    after_only = after_paths - before_paths - start_unknown_paths
    renames, ambiguous_rename = _detect_renames(before_only, after_only, before, after)
    for old_path, new_path in renames:
        before_only.remove(old_path)
        after_only.remove(new_path)

    changes: list[dict[str, Any]] = []
    content_pairs: dict[tuple[str | None, str | None], tuple[bytes, bytes] | None] = {}
    skip_reasons = set(state_value.get("skip_reasons", [])) | set(end_skips)
    if ambiguous_rename:
        skip_reasons.add("rename_ambiguous")
    if state_value.get("overlap_sessions"):
        skip_reasons.add("concurrent_session_overlap")

    for old_path, new_path in renames:
        before_content = _blob_content(repo, before[old_path])
        after_content = after_contents[new_path]
        if before_content is None:
            skip_reasons.add("before_content_unavailable")
        pair = None if before_content is None else (before_content, after_content)
        content_pairs[(old_path, new_path)] = pair
        changes.append(
            {
                "after_sha256": after[new_path]["sha256"],
                "before_sha256": before[old_path]["sha256"],
                "kind": "rename",
                "lines_added": 0,
                "lines_removed": 0,
                "new_path": new_path,
                "old_path": old_path,
            }
        )

    for path, kind in [
        *((path, "edit") for path in common_changed),
        *((path, "delete") for path in before_only),
        *((path, "write") for path in after_only),
    ]:
        before_record = before.get(path)
        after_record = after.get(path)
        before_content = (
            b"" if before_record is None else _blob_content(repo, before_record)
        )
        after_content = b"" if after_record is None else after_contents[path]
        pair = None if before_content is None else (before_content, after_content)
        content_pairs[
            (path if before_record else None, path if after_record else None)
        ] = pair
        line_delta = None if pair is None else _line_delta(*pair)
        if before_content is None:
            skip_reasons.add("before_content_unavailable")
        elif line_delta is None:
            skip_reasons.add("binary_content")
        changes.append(
            {
                "after_sha256": None
                if after_record is None
                else after_record["sha256"],
                "before_sha256": (
                    None if before_record is None else before_record["sha256"]
                ),
                "kind": kind,
                "lines_added": None if line_delta is None else line_delta[0],
                "lines_removed": None if line_delta is None else line_delta[1],
                "path": path,
            }
        )

    changes.sort(key=_change_sort_key)
    patch_parts: list[str] = []
    patch_complete = True
    for change in changes:
        if change["kind"] == "rename":
            old_path = change["old_path"]
            new_path = change["new_path"]
            pair = content_pairs[(old_path, new_path)]
            if pair is None:
                patch_complete = False
                continue
            patch_parts.append(
                f"diff --git a/{old_path} b/{new_path}\n"
                "similarity index 100%\n"
                f"rename from {old_path}\nrename to {new_path}\n"
            )
            continue
        path = change["path"]
        old_path = None if change["kind"] == "write" else path
        new_path = None if change["kind"] == "delete" else path
        pair = content_pairs[(old_path, new_path)]
        diff = None if pair is None else _unified_diff(*pair, old_path, new_path)
        if diff is None:
            patch_complete = False
        else:
            patch_parts.append(diff)
    if {"unreadable_path", "unsupported_file_type"} & skip_reasons:
        patch_complete = False
    patch = "".join(patch_parts) if patch_complete else None
    if patch is not None and len(patch.encode("utf-8")) > max_patch_bytes:
        patch = None
        skip_reasons.add("patch_over_limit")
    return {
        "changes": changes,
        "harness": "claude-code",
        "kind": "turn_worktree_observation",
        "patch": patch,
        "patch_bytes": 0 if patch is None else len(patch.encode("utf-8")),
        "schema_version": SCHEMA_VERSION,
        "session_id": state_value["session_id"],
        "skip_reasons": sorted(skip_reasons),
        "turn_id": state_value["turn_id"],
        "unsupported_claims": _UNSUPPORTED_CLAIMS,
    }


def _end_turn(
    hook: dict[str, Any], state_dir: Path, max_patch_bytes: int
) -> dict[str, Any]:
    session_id = hook.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("missing session_id")
    repo = _repo_root(hook.get("cwd"))
    path = _state_path(state_dir, repo, session_id)
    state_value = _load_state(path)
    if state_value is None:
        return {
            "harness": "claude-code",
            "kind": "turn_worktree_observation_skipped",
            "schema_version": SCHEMA_VERSION,
            "session_id": session_id,
            "skip_reasons": ["missing_turn_start_hook"],
            "turn_id": hook.get("turn_id"),
        }
    hook_turn_id = hook.get("turn_id")
    if isinstance(hook_turn_id, str) and hook_turn_id != state_value.get("turn_id"):
        return {
            "harness": "claude-code",
            "kind": "turn_worktree_observation_skipped",
            "schema_version": SCHEMA_VERSION,
            "session_id": session_id,
            "skip_reasons": ["turn_boundary_mismatch"],
            "turn_id": hook_turn_id,
        }
    observation = _build_observation(state_value, repo, max_patch_bytes)
    try:
        path.unlink()
    except OSError:
        pass
    return observation


def handle_hook(
    hook: dict[str, Any],
    *,
    state_dir: Path,
    max_patch_bytes: int = DEFAULT_MAX_PATCH_BYTES,
) -> dict[str, Any]:
    """Handle one Claude Code turn-boundary payload."""
    if max_patch_bytes <= 0:
        raise ValueError("max_patch_bytes must be positive")
    event = hook.get("hook_event_name")
    if event == _START_EVENT:
        return _start_turn(hook, state_dir)
    if event == _END_EVENT:
        return _end_turn(hook, state_dir, max_patch_bytes)
    raise ValueError(f"unsupported hook_event_name: {event!r}")


def expire_incomplete(state_dir: Path) -> list[dict[str, Any]]:
    """Convert starts without a matching end hook into visible skip records."""
    skipped = []
    for path, state_value in _active_states(state_dir):
        skipped.append(_missing_turn_end_record(state_value))
        try:
            path.unlink()
        except OSError:
            pass
    return sorted(skipped, key=lambda item: (item["session_id"], item["turn_id"]))


def _missing_turn_end_record(state_value: dict[str, Any]) -> dict[str, Any]:
    return {
        "harness": state_value.get("harness", "claude-code"),
        "kind": "turn_worktree_observation_skipped",
        "schema_version": SCHEMA_VERSION,
        "session_id": state_value.get("session_id"),
        "skip_reasons": ["missing_turn_end_hook"],
        "turn_id": state_value.get("turn_id"),
    }


def compare_observations(observations: list[dict[str, Any]]) -> dict[str, Any]:
    """Return an order-independent comparison summary for spike observations."""
    ordered = sorted(observations, key=canonical_json)
    accounted_records = []
    for observation in ordered:
        accounted_records.append(observation)
        superseded_turn = observation.get("superseded_turn")
        if isinstance(superseded_turn, dict):
            accounted_records.append(superseded_turn)
    skip_counts: Counter[str] = Counter()
    for observation in accounted_records:
        skip_counts.update(observation.get("skip_reasons", []))
    encoded = [canonical_json(observation) for observation in ordered]
    return {
        "observation_bytes": sum(len(item.encode("utf-8")) for item in encoded),
        "observation_count": len(ordered),
        "observable_turns": sum(
            item.get("kind") == "turn_worktree_observation" for item in ordered
        ),
        "schema_version": SCHEMA_VERSION,
        "skip_counts": dict(sorted(skip_counts.items())),
        "skipped_turns": sum(
            item.get("kind") == "turn_worktree_observation_skipped"
            for item in accounted_records
        ),
    }


def _default_state_dir() -> Path:
    configured = os.environ.get(_STATE_ENV)
    if configured:
        return Path(configured)
    cache_base = os.environ.get("XDG_CACHE_HOME")
    if cache_base:
        return Path(cache_base) / "sediment" / "turn-worktree-observation-spike"
    return Path.home() / ".cache" / "sediment" / "turn-worktree-observation-spike"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the local Claude Code turn worktree-observation spike."
    )
    parser.add_argument(
        "command",
        choices=("hook", "expire", "compare"),
        help="process one hook, expire starts without ends, or compare JSONL",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=_default_state_dir(),
        help=f"local state directory (default: ${_STATE_ENV} or user cache)",
    )
    parser.add_argument(
        "--max-patch-bytes",
        type=int,
        default=DEFAULT_MAX_PATCH_BYTES,
        help=f"whole-turn patch cap (default: {DEFAULT_MAX_PATCH_BYTES})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run a spike command without propagating failures to the agent harness."""
    try:
        args = build_parser().parse_args(argv)
        if args.command == "hook":
            hook = json.loads(sys.stdin.read() or "{}")
            if not isinstance(hook, dict):
                raise ValueError("hook payload must be an object")
            print(
                canonical_json(
                    handle_hook(
                        hook,
                        state_dir=args.state_dir,
                        max_patch_bytes=args.max_patch_bytes,
                    )
                ),
                end="",
            )
        elif args.command == "expire":
            for record in expire_incomplete(args.state_dir):
                print(canonical_json(record), end="")
        else:
            observations = [json.loads(line) for line in sys.stdin if line.strip()]
            print(canonical_json(compare_observations(observations)), end="")
    except SystemExit as exc:
        if exc.code:
            _trail(f"command skipped (argument parser exited {exc.code})")
    except Exception as exc:
        _trail(f"hook skipped ({exc})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
