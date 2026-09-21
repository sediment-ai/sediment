# SPDX-License-Identifier: AGPL-3.0-or-later
"""Native Codex 0.153.4 FileChange capture and its absence boundaries."""

import json
from pathlib import Path

import pytest

from sediment_capture import parse_otlp_edit_observations
from sediment_cli.transcript import build_pairs, build_payload


def _entries(tmp_path: Path) -> list[dict]:
    source = (
        Path(__file__).parent
        / "fixtures/transcripts/codex_0_153_4_completed_file_change.jsonl"
    ).read_text()
    return [
        json.loads(line)
        for line in source.replace("__ROOT__", str(tmp_path)).splitlines()
    ]


def test_codex_completed_recorded_add_preserves_call_identity_and_text(tmp_path):
    entries = _entries(tmp_path)
    target = tmp_path / "codex_fixed.py"
    target.write_text("def subtract(a, b):\n    return a - b  # reviewed\n")

    [pair] = build_pairs(entries, "sess-codex", agent="codex")
    [fact] = parse_otlp_edit_observations(
        build_payload("sess-codex", [pair], agent="codex"), org_id="acme"
    )

    assert fact.call_id == "exec-41bf137a-c5d5-4b57-b892-b392267d654b"
    assert fact.session_id == "sess-codex"
    assert pair["tool_name"] == "apply_patch"
    assert fact.file_path == str(target)
    assert fact.applied_text == "def subtract(a, b):\n    return a - b\n"
    assert fact.observed_file_text == target.read_text()
    assert pair["time_unix_nano"] == 1788988293960000000
    assert "stdout" not in pair and "stderr" not in pair


@pytest.mark.parametrize("move_path", [None, "moved.js"])
def test_codex_completed_update_preserves_hunk_additions(tmp_path, move_path):
    entries = _entries(tmp_path)
    item = entries[1]["payload"]["item"]
    item["changes"] = {
        "counter.js": {
            "type": "update",
            "unified_diff": "@@ -1 +1 @@\n-old();\n+++counter; record(counter);\n",
            "move_path": move_path,
        }
    }
    target = tmp_path / (move_path or "counter.js")
    target.write_text("++counter; record(counter);\n")

    [pair] = build_pairs(entries, "sess-codex", agent="codex")

    assert pair["tool_use_id"] == item["id"]
    assert pair["file_path"] == str(target)
    assert pair["applied_text"] == "++counter; record(counter);"
    assert pair["observed_file_text"] == target.read_text()


@pytest.mark.parametrize(
    "status,reason",
    [
        ("failed", "execution_failed"),
        ("declined", "execution_failed"),
        ("in_progress", "execution_status_unknown"),
        (None, "execution_status_unknown"),
        (True, "execution_status_unknown"),
    ],
)
def test_codex_completed_requires_success_metadata(tmp_path, status, reason, capsys):
    entries = _entries(tmp_path)
    entries[1]["payload"]["item"]["status"] = status

    assert build_pairs(entries, "sess-codex", agent="codex") == []
    assert capsys.readouterr().err.count(reason) == 1


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({}, "malformed_patch"),
        (None, "malformed_patch"),
        ({"": {"type": "add", "content": "text"}}, "malformed_patch"),
        ({"app.py": None}, "malformed_patch"),
        ({"app.py": {"type": "add"}}, "malformed_patch"),
        (
            {"app.py": {"type": "update", "unified_diff": "+unframed"}},
            "malformed_patch",
        ),
        ({"app.py": {"type": "delete", "content": "text"}}, "unsupported_patch_kind"),
        (
            {
                "app.py": {"type": "add", "content": "one"},
                "other.py": {"type": "add", "content": "two"},
            },
            "unsupported_patch_kind",
        ),
    ],
)
def test_codex_completed_declines_unproved_changes(tmp_path, changes, reason, capsys):
    entries = _entries(tmp_path)
    entries[1]["payload"]["item"]["changes"] = changes

    assert build_pairs(entries, "sess-codex", agent="codex") == []
    assert capsys.readouterr().err.count(reason) == 1


@pytest.mark.parametrize("call_id", [None, "", " ", 123])
def test_codex_completed_requires_native_call_identity(tmp_path, call_id, capsys):
    entries = _entries(tmp_path)
    entries[1]["payload"]["item"]["id"] = call_id
    # The enclosing event's identifier must never replace the tool call's id.
    entries[1]["payload"]["call_id"] = "outer-call"

    assert build_pairs(entries, "sess-codex", agent="codex") == []
    assert capsys.readouterr().err.count("malformed_patch") == 1


@pytest.mark.parametrize("thread_id", [None, "", "another-session", 123])
def test_codex_completed_requires_same_session(tmp_path, thread_id, capsys):
    entries = _entries(tmp_path)
    entries[1]["payload"]["thread_id"] = thread_id

    assert build_pairs(entries, "sess-codex", agent="codex") == []
    assert capsys.readouterr().err.count("malformed_patch") == 1


def test_codex_completed_missing_timestamp_does_not_use_receipt_time(tmp_path, capsys):
    entries = _entries(tmp_path)
    del entries[1]["timestamp"]

    assert build_pairs(entries, "sess-codex", agent="codex") == []
    assert capsys.readouterr().err.count("malformed_patch") == 1


def test_codex_completed_preserves_valid_sibling_after_failed_patch(tmp_path, capsys):
    entries = _entries(tmp_path)
    failed = json.loads(json.dumps(entries[1]))
    failed["payload"]["item"]["status"] = "failed"
    failed["payload"]["item"]["id"] = "failed-call"
    entries.insert(1, failed)

    [pair] = build_pairs(entries, "sess-codex", agent="codex")

    assert pair["tool_use_id"] == "exec-41bf137a-c5d5-4b57-b892-b392267d654b"
    assert capsys.readouterr().err.count("execution_failed") == 1
