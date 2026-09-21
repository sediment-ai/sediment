# SPDX-License-Identifier: AGPL-3.0-or-later
"""The sim repo generator's contract: deterministic history, runnable pairs.

Byte-identical regeneration (same seed → same HEAD SHA) is what lets the
scenario harness pin exact SHAs in its ground-truth manifest; the red→green
check is the executable-subset contract the RLVR round-trip (Group 6)
depends on. ``PYTHONDONTWRITEBYTECODE=1`` in the subprocess env keeps stale
``__pycache__`` from masking a checkout — the generator additionally
guarantees every gold patch changes file size, but the harness should not
rely on that alone.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "gen_repo.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("gen_repo", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen_repo = _load_module()


def _pytest_in(repo: Path) -> int:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=repo,
        capture_output=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
    ).returncode


def _checkout(repo: Path, sha: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-q", sha],
        check=True,
        capture_output=True,
    )


def test_generation_is_deterministic_and_pairs_flip_red_green(tmp_path):
    first = gen_repo.generate(tmp_path / "a")
    second = gen_repo.generate(tmp_path / "b")

    # Byte-identical history: same seed, same SHAs, both runs.
    assert first["head"] == second["head"]
    assert first["commits"] == second["commits"]
    assert 150 <= first["commits"] <= 200  # synthetic history size bound
    assert len(first["executable_pairs"]) >= 5

    # Manifest matches the on-disk repo it describes.
    repo = tmp_path / "a" / gen_repo.REPO_NAME
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert head == first["head"]
    on_disk = json.loads((tmp_path / "a" / "sim_repo_manifest.json").read_text())
    assert on_disk == first

    # The no-background-maintenance posture is part of the contract: a
    # detached auto-maintenance repack can race the harness's file:// mirror
    # fetch mid-upload-pack (observed CI flake) — the generator pins it off.
    for key, want in (
        ("gc.auto", "0"),
        ("gc.autoDetach", "false"),
        ("maintenance.auto", "false"),
    ):
        got = subprocess.run(
            ["git", "-C", str(repo), "config", "--get", key],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert got == want, (key, got)

    # Executable contract on the first pair: red at base, green at gold.
    pair = first["executable_pairs"][0]
    _checkout(repo, pair["base_sha"])
    assert _pytest_in(repo) != 0, "base commit must be red"
    _checkout(repo, pair["gold_sha"])
    assert _pytest_in(repo) == 0, "gold commit must be green"

    # The designed-in content the scenarios rely on exists at HEAD.
    _checkout(repo, "main")
    for rel in (
        *first["jaccard_bait"],
        first["unicode_identifiers"],
        first["binary_asset"],
        "accounting/journal.py",  # the renamed subtree's new home
        *first["workflows"],
    ):
        assert (repo / rel).exists(), rel
    assert not (repo / "ledger").exists()  # old subtree name is gone


def test_refuses_to_overwrite(tmp_path):
    gen_repo.generate(tmp_path)
    try:
        gen_repo.generate(tmp_path)
    except SystemExit as exc:
        assert "refusing" in str(exc)
    else:
        raise AssertionError("expected SystemExit on existing repo path")
