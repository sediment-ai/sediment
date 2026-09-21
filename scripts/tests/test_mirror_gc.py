# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for sediment_api/mirror_gc.py.

Git fixture helpers are kept inline — same convention as
test_model_report.py — rather than sharing derive's ``tests/gitfixtures.py``.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sediment_api import mirror_gc
from sediment_core import ForgeProvider, Push
from sediment_derive import MirrorManager

ORG = "acme-corp"


main = mirror_gc.main


def _gc(database_url: str, mirror_path: str, *extra: str) -> int:
    return main(
        [
            "--org",
            ORG,
            "--database-url",
            database_url,
            "--mirror-path",
            mirror_path,
            *extra,
        ]
    )


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def _seed_mirrored_repo(
    postgres_store_factory,
    tmp_path: Path,
    name: str,
    *,
    pushed_days_ago: int | None,
) -> tuple[str, str]:
    """Builds one real mirrored repo and, unless ``pushed_days_ago`` is None,
    a matching Push fact that many days before ``now``. Returns
    ``(database_url, mirror_path)``; the repo is named ``acme-corp/{name}``."""
    root = tmp_path / name
    root.mkdir()
    work = root / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "dev@example.com")
    _git(work, "config", "user.name", "Dev")
    (work / "a.py").write_text("x = 1\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    head = _git(work, "rev-parse", "HEAD").strip()

    remote = root / "remote.git"
    _git(root, "init", "-q", "--bare", str(remote))
    _git(work, "push", "-q", str(remote), "refs/heads/*:refs/heads/*")

    database_url, store = postgres_store_factory()
    mirror_path = str(tmp_path / "mirrors")
    repo = f"{ORG}/{name}"
    push = Push(
        org_id=ORG,
        provider=ForgeProvider.GITHUB,
        repo=repo,
        clone_url=str(remote),
        ref="refs/heads/main",
        before_sha="0" * 40,
        after_sha=head,
    )
    MirrorManager(mirror_path).ensure(push)
    if pushed_days_ago is not None:
        push = push.model_copy(
            update={"captured_at": datetime.now(UTC) - timedelta(days=pushed_days_ago)}
        )
        store.store_push(push)
    return database_url, mirror_path


def test_dry_run_by_default_leaves_stale_mirror_in_place(
    tmp_path, postgres_store_factory, capsys
) -> None:
    database_url, mirror_path = _seed_mirrored_repo(
        postgres_store_factory, tmp_path, "stale", pushed_days_ago=200
    )

    assert _gc(database_url, mirror_path) == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert MirrorManager(mirror_path).open(ORG, f"{ORG}/stale") is not None


def test_apply_removes_stale_mirror(tmp_path, postgres_store_factory, capsys) -> None:
    database_url, mirror_path = _seed_mirrored_repo(
        postgres_store_factory, tmp_path, "stale", pushed_days_ago=200
    )

    assert _gc(database_url, mirror_path, "--apply") == 0
    assert MirrorManager(mirror_path).open(ORG, f"{ORG}/stale") is None


def test_recent_push_is_kept(tmp_path, postgres_store_factory) -> None:
    database_url, mirror_path = _seed_mirrored_repo(
        postgres_store_factory, tmp_path, "fresh", pushed_days_ago=1
    )

    assert _gc(database_url, mirror_path, "--apply") == 0
    assert MirrorManager(mirror_path).open(ORG, f"{ORG}/fresh") is not None


def test_custom_retention_days(tmp_path, postgres_store_factory) -> None:
    database_url, mirror_path = _seed_mirrored_repo(
        postgres_store_factory, tmp_path, "mid", pushed_days_ago=10
    )

    assert _gc(database_url, mirror_path, "--retention-days", "5", "--apply") == 0
    assert MirrorManager(mirror_path).open(ORG, f"{ORG}/mid") is None


def test_json_output_reports_quarantine_ambiguous_for_orphan_mirror(
    tmp_path, postgres_store_factory, capsys
) -> None:
    database_url, mirror_path = _seed_mirrored_repo(
        postgres_store_factory, tmp_path, "orphan", pushed_days_ago=None
    )

    assert _gc(database_url, mirror_path, "--json") == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1
    assert rows[0]["action"] == "kept"
    assert rows[0]["skip_reason"] == "quarantine_ambiguous"


def test_json_output_marks_dry_run_so_removed_is_not_misread_as_deleted(
    tmp_path, postgres_store_factory, capsys
) -> None:
    # A dry run and an applied run both report action="removed" for
    # an eligible mirror -- a --json consumer needs dry_run to tell them
    # apart, plus the policy that produced the verdict.
    database_url, mirror_path = _seed_mirrored_repo(
        postgres_store_factory, tmp_path, "stale", pushed_days_ago=200
    )

    assert _gc(database_url, mirror_path, "--json") == 0  # dry-run by default
    [row] = json.loads(capsys.readouterr().out)
    assert row["action"] == "removed"
    assert row["dry_run"] is True
    assert row["retention_days"] == 90
    assert row["policy_version"] == "1"
    assert MirrorManager(mirror_path).open(ORG, f"{ORG}/stale") is not None

    assert _gc(database_url, mirror_path, "--json", "--apply") == 0
    [applied_row] = json.loads(capsys.readouterr().out)
    assert applied_row["action"] == "removed"
    assert applied_row["dry_run"] is False


def test_no_mirrored_repos_is_a_clean_empty_report(
    tmp_path, postgres_store_factory, capsys
) -> None:
    database_url, _ = postgres_store_factory()

    assert _gc(database_url, str(tmp_path / "mirrors")) == 0
    assert "no mirrored repos found" in capsys.readouterr().out


def test_zero_or_negative_retention_is_rejected_before_touching_anything(
    tmp_path, capsys
) -> None:
    # A sign slip with --apply must not become a mass delete: validation
    # runs before FactStore or MirrorManager are constructed, so neither
    # path needs to exist for the rejection.
    for bad in ("0", "-90"):
        database_url = "not-a-database-url"
        mirrors = str(tmp_path / "mirrors")
        assert _gc(database_url, mirrors, "--retention-days", bad, "--apply") == 2
        assert "--retention-days must be >= 1" in capsys.readouterr().err
