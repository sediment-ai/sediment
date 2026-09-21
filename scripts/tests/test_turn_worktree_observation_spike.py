# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contract tests for the disposable turn worktree-observation spike."""

from __future__ import annotations

import importlib.util
import json
import os
import random
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts" / "turn_worktree_observation_spike.py"
FIXTURES = Path(__file__).parent / "fixtures" / "turn_worktree_observation"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "turn_worktree_observation_spike", SCRIPT
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mod = _load_module()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()


def _repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "spike@example.test")
    _git(repo, "config", "user.name", "Spike Fixture")
    return repo


def _commit(repo: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fixture")


def _hook(name: str, repo: Path, **updates: object) -> dict:
    payload = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    payload["cwd"] = str(repo)
    payload.update(updates)
    return payload


def _observe(
    repo: Path,
    state_dir: Path,
    change,
    *,
    session_id: str = "session-alpha",
    turn_id: str = "turn-001",
    max_patch_bytes: int = 64 * 1024,
) -> dict:
    start = _hook("claude-start.json", repo, session_id=session_id, turn_id=turn_id)
    stop = _hook("claude-stop.json", repo, session_id=session_id, turn_id=turn_id)
    result = mod.handle_hook(
        start, state_dir=state_dir, max_patch_bytes=max_patch_bytes
    )
    assert result["kind"] == "turn_start_recorded"
    change()
    return mod.handle_hook(stop, state_dir=state_dir, max_patch_bytes=max_patch_bytes)


def test_hook_fixtures_capture_edit_write_delete_and_exact_rename(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _commit(
        repo,
        {
            "edit.txt": "before\n",
            "delete.txt": "remove me\n",
            "rename.txt": "same bytes\n",
        },
    )

    def change() -> None:
        (repo / "edit.txt").write_text("after\n", encoding="utf-8")
        (repo / "write.txt").write_text("created\n", encoding="utf-8")
        (repo / "delete.txt").unlink()
        (repo / "rename.txt").rename(repo / "renamed.txt")

    observation = _observe(repo, tmp_path / "state", change)

    assert observation["kind"] == "turn_worktree_observation"
    assert observation["harness"] == "claude-code"
    assert observation["session_id"] == "session-alpha"
    assert observation["turn_id"] == "turn-001"
    assert observation["skip_reasons"] == []
    assert [(item["kind"], item.get("path")) for item in observation["changes"]] == [
        ("delete", "delete.txt"),
        ("edit", "edit.txt"),
        ("rename", None),
        ("write", "write.txt"),
    ]
    rename = observation["changes"][2]
    assert (rename["old_path"], rename["new_path"]) == (
        "rename.txt",
        "renamed.txt",
    )
    assert rename["before_sha256"] == rename["after_sha256"]
    assert observation["patch_bytes"] == len(observation["patch"].encode())
    assert "--- a/edit.txt" in observation["patch"]
    assert "+++ /dev/null" in observation["patch"]
    assert "rename from rename.txt" in observation["patch"]
    assert "+++ b/write.txt" in observation["patch"]


def test_patch_preserves_missing_final_newlines_for_edit_write_and_delete(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _commit(
        repo,
        {
            "both-missing.txt": "before",
            "delete-missing.txt": "deleted",
            "new-missing.txt": "before\n",
            "old-missing.txt": "before",
        },
    )

    def change() -> None:
        (repo / "both-missing.txt").write_bytes(b"after")
        (repo / "delete-missing.txt").unlink()
        (repo / "new-missing.txt").write_bytes(b"after")
        (repo / "old-missing.txt").write_bytes(b"after\n")
        (repo / "write-missing.txt").write_bytes(b"created")

    observation = _observe(repo, tmp_path / "state", change)
    patch = observation["patch"]
    assert patch is not None
    assert patch.count("\\ No newline at end of file\n") == 6
    assert "-before\n\\ No newline at end of file\n" in patch
    assert "+after\n\\ No newline at end of file\n" in patch
    assert "-deleted\n\\ No newline at end of file\n" in patch
    assert "+created\n\\ No newline at end of file\n" in patch

    patch_path = tmp_path / "turn.patch"
    patch_path.write_text(patch, encoding="utf-8")
    subprocess.run(["git", "apply", "--reverse", str(patch_path)], cwd=repo, check=True)
    assert (repo / "both-missing.txt").read_bytes() == b"before"
    assert (repo / "delete-missing.txt").read_bytes() == b"deleted"
    assert (repo / "new-missing.txt").read_bytes() == b"before\n"
    assert (repo / "old-missing.txt").read_bytes() == b"before"
    assert not (repo / "write-missing.txt").exists()
    subprocess.run(["git", "apply", str(patch_path)], cwd=repo, check=True)
    assert (repo / "both-missing.txt").read_bytes() == b"after"
    assert not (repo / "delete-missing.txt").exists()
    assert (repo / "new-missing.txt").read_bytes() == b"after"
    assert (repo / "old-missing.txt").read_bytes() == b"after\n"
    assert (repo / "write-missing.txt").read_bytes() == b"created"


def test_shell_or_formatter_change_is_observed_without_an_actor_claim(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _commit(repo, {"formatted.py": "value=1\n"})

    observation = _observe(
        repo,
        tmp_path / "state",
        lambda: (repo / "formatted.py").write_text("value = 1\n", encoding="utf-8"),
    )

    assert observation["changes"][0]["kind"] == "edit"
    assert observation["changes"][0]["lines_added"] == 1
    assert observation["changes"][0]["lines_removed"] == 1
    assert "actor_identity_unestablished" in observation["unsupported_claims"]
    assert "turn_causality_unestablished" in observation["unsupported_claims"]


def test_oversized_patch_keeps_hashes_and_counts_but_omits_patch(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _commit(repo, {"large.txt": "before\n"})

    observation = _observe(
        repo,
        tmp_path / "state",
        lambda: (repo / "large.txt").write_text("x" * 4096 + "\n", encoding="utf-8"),
        max_patch_bytes=128,
    )

    [change] = observation["changes"]
    assert change["before_sha256"] != change["after_sha256"]
    assert (change["lines_added"], change["lines_removed"]) == (1, 1)
    assert observation["patch"] is None
    assert observation["patch_bytes"] == 0
    assert observation["skip_reasons"] == ["patch_over_limit"]


def test_dirty_non_git_baseline_keeps_hash_but_omits_incomplete_patch(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _commit(repo, {"dirty.txt": "committed\n"})
    (repo / "dirty.txt").write_text("dirty before\n", encoding="utf-8")

    observation = _observe(
        repo,
        tmp_path / "state",
        lambda: (repo / "dirty.txt").write_text("dirty after\n", encoding="utf-8"),
    )

    assert observation["changes"][0]["before_sha256"]
    assert observation["changes"][0]["after_sha256"]
    assert observation["changes"][0]["lines_added"] is None
    assert observation["patch"] is None
    assert observation["skip_reasons"] == ["before_content_unavailable"]


def test_unreadable_path_is_absent_instead_of_misreported_as_deleted(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _commit(repo, {"unreadable.txt": "private\n"})
    state_dir = tmp_path / "state"
    mod.handle_hook(_hook("claude-start.json", repo), state_dir=state_dir)
    target = repo / "unreadable.txt"
    target.chmod(0)
    try:
        observation = mod.handle_hook(
            _hook("claude-stop.json", repo), state_dir=state_dir
        )
    finally:
        target.chmod(0o644)

    assert observation["changes"] == []
    assert observation["patch"] is None
    assert observation["skip_reasons"] == ["unreadable_path"]


def test_symlink_stays_absent_without_reading_its_target(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside content must stay unread\n", encoding="utf-8")
    (repo / "link.txt").symlink_to(outside)
    _git(repo, "add", "link.txt")
    _git(repo, "commit", "-qm", "fixture")
    state_dir = tmp_path / "state"

    start = mod.handle_hook(_hook("claude-start.json", repo), state_dir=state_dir)
    observation = mod.handle_hook(_hook("claude-stop.json", repo), state_dir=state_dir)

    assert start["skip_reasons"] == ["unsupported_file_type"]
    assert observation["changes"] == []
    assert observation["patch"] is None
    assert observation["skip_reasons"] == ["unsupported_file_type"]
    assert "outside content" not in mod.canonical_json(observation)


def test_ambiguous_rename_stays_delete_and_write_with_skip_reason(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _commit(repo, {"old-a.txt": "same\n", "old-b.txt": "same\n"})

    def change() -> None:
        (repo / "old-a.txt").unlink()
        (repo / "old-b.txt").unlink()
        (repo / "new-a.txt").write_text("same\n", encoding="utf-8")
        (repo / "new-b.txt").write_text("same\n", encoding="utf-8")

    observation = _observe(repo, tmp_path / "state", change)

    assert [item["kind"] for item in observation["changes"]] == [
        "write",
        "write",
        "delete",
        "delete",
    ]
    assert observation["skip_reasons"] == ["rename_ambiguous"]


def test_missing_turn_end_hook_expires_to_visible_skip(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _commit(repo, {"a.txt": "a\n"})
    state_dir = tmp_path / "state"
    start = _hook("claude-start.json", repo)

    assert mod.handle_hook(start, state_dir=state_dir)["kind"] == (
        "turn_start_recorded"
    )
    [skipped] = mod.expire_incomplete(state_dir)

    assert skipped["kind"] == "turn_worktree_observation_skipped"
    assert skipped["skip_reasons"] == ["missing_turn_end_hook"]
    assert mod.expire_incomplete(state_dir) == []


def test_consecutive_start_records_superseded_turn_before_new_turn(
    tmp_path: Path,
) -> None:
    outputs = []
    for suffix in ("one", "two"):
        repo = _repo(tmp_path, suffix)
        _commit(repo, {"a.txt": "a\n"})
        state_dir = tmp_path / f"state-{suffix}"
        first = _hook("claude-start.json", repo, turn_id="turn-001")
        second = _hook("claude-start.json", repo, turn_id="turn-002")

        mod.handle_hook(first, state_dir=state_dir)
        result = mod.handle_hook(second, state_dir=state_dir)
        outputs.append(mod.canonical_json(result))

        assert result["superseded_turn"] == {
            "harness": "claude-code",
            "kind": "turn_worktree_observation_skipped",
            "schema_version": 1,
            "session_id": "session-alpha",
            "skip_reasons": ["missing_turn_end_hook"],
            "turn_id": "turn-001",
        }
        summary = mod.compare_observations([result])
        assert summary["observation_count"] == 1
        assert summary["observation_bytes"] == len(
            mod.canonical_json(result).encode("utf-8")
        )
        assert summary["observable_turns"] == 0
        assert summary["skipped_turns"] == 1
        assert summary["skip_counts"] == {"missing_turn_end_hook": 1}
        [active] = mod.expire_incomplete(state_dir)
        assert active["turn_id"] == "turn-002"

    assert outputs[0] == outputs[1]


def test_two_concurrent_sessions_remain_separate_and_flag_causality(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    _commit(repo, {"a.txt": "a\n", "b.txt": "b\n"})
    state_dir = tmp_path / "state"
    start_a = _hook("claude-start.json", repo)
    start_b = _hook(
        "claude-start.json",
        repo,
        session_id="session-beta",
        turn_id="turn-002",
    )
    stop_a = _hook("claude-stop.json", repo)
    stop_b = _hook(
        "claude-stop.json",
        repo,
        session_id="session-beta",
        turn_id="turn-002",
    )

    mod.handle_hook(start_a, state_dir=state_dir)
    mod.handle_hook(start_b, state_dir=state_dir)
    (repo / "a.txt").write_text("session a\n", encoding="utf-8")
    observation_a = mod.handle_hook(stop_a, state_dir=state_dir)
    (repo / "b.txt").write_text("session b\n", encoding="utf-8")
    observation_b = mod.handle_hook(stop_b, state_dir=state_dir)

    assert observation_a["session_id"] == "session-alpha"
    assert observation_b["session_id"] == "session-beta"
    assert "concurrent_session_overlap" in observation_a["skip_reasons"]
    assert "concurrent_session_overlap" in observation_b["skip_reasons"]
    assert observation_a["changes"] != observation_b["changes"]


def test_same_inputs_are_byte_identical_and_ignore_transcript_fields(
    tmp_path: Path,
) -> None:
    outputs = []
    state_payloads = []
    for suffix in ("one", "two"):
        repo = _repo(tmp_path, suffix)
        _commit(repo, {"app.py": "x = 1\n"})
        state_dir = tmp_path / f"state-{suffix}"
        start = _hook("claude-start.json", repo)
        mod.handle_hook(start, state_dir=state_dir)
        state_payloads.append(
            "\n".join(
                path.read_text(encoding="utf-8")
                for path in sorted(state_dir.glob("*.json"))
            )
        )
        (repo / "app.py").write_text("x = 2\n", encoding="utf-8")
        outputs.append(
            mod.canonical_json(
                mod.handle_hook(_hook("claude-stop.json", repo), state_dir=state_dir)
            )
        )

    assert outputs[0] == outputs[1]
    assert "fixture prompt" not in "".join(state_payloads + outputs)
    assert "fixture response" not in "".join(state_payloads + outputs)
    assert "transcript.jsonl" not in "".join(state_payloads + outputs)


def test_shuffled_observation_delivery_produces_same_comparison(tmp_path: Path) -> None:
    repo_a = _repo(tmp_path, "a")
    repo_b = _repo(tmp_path, "b")
    _commit(repo_a, {"a.txt": "a\n"})
    _commit(repo_b, {"b.txt": "b\n"})
    observations = [
        _observe(
            repo_a,
            tmp_path / "state-a",
            lambda: (repo_a / "a.txt").write_text("aa\n", encoding="utf-8"),
        ),
        _observe(
            repo_b,
            tmp_path / "state-b",
            lambda: (repo_b / "b.txt").write_text("bb\n", encoding="utf-8"),
            session_id="session-beta",
            turn_id="turn-002",
        ),
    ]
    shuffled = observations.copy()
    random.Random(583).shuffle(shuffled)

    assert mod.canonical_json(mod.compare_observations(observations)) == (
        mod.canonical_json(mod.compare_observations(shuffled))
    )


def test_hook_cli_never_blocks_workflow_on_malformed_input(tmp_path: Path) -> None:
    env = {**os.environ, "SEDIMENT_TURN_OBSERVATION_STATE": str(tmp_path / "state")}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "hook"],
        input="not json",
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )

    assert result.returncode == 0
    assert "hook skipped" in result.stderr


def test_help_exits_cleanly_without_a_false_skip_message() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode == 0
    assert "Run the local Claude Code" in result.stdout
    assert result.stderr == ""
