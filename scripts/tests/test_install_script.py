# SPDX-License-Identifier: AGPL-3.0-or-later
"""install.sh dry-run checks — every method branch, no network,
no tool installations: ``--method``/``SEDIMENT_INSTALL_METHOD`` force the
branch and ``--dry-run`` prints the command instead of running it."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

INSTALL_SH = Path(__file__).parents[2] / "install.sh"


@pytest.fixture
def bin_dir(tmp_path: Path) -> Path:
    """An empty directory to shadow PATH with, per detection test."""
    path = tmp_path / "bin"
    path.mkdir()
    return path


def _run(*args: str, env_method: str | None = None) -> subprocess.CompletedProcess:
    env = {"PATH": "/usr/bin:/bin"}
    if env_method is not None:
        env["SEDIMENT_INSTALL_METHOD"] = env_method
    return subprocess.run(
        ["sh", str(INSTALL_SH), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def test_dry_run_uv() -> None:
    r = _run("--dry-run", "--method", "uv")
    assert r.returncode == 0
    assert r.stdout.strip() == "would run: uv tool install sediment-cli"


def test_dry_run_pipx_with_version() -> None:
    r = _run("--dry-run", "--method", "pipx", "--version", "0.2.0")
    assert r.returncode == 0
    assert r.stdout.strip() == "would run: pipx install sediment-cli==0.2.0"


def test_dry_run_pip() -> None:
    r = _run("--dry-run", "--method", "pip")
    assert r.returncode == 0
    assert r.stdout.strip() == "would run: python3 -m pip install --user sediment-cli"


def test_env_method_form() -> None:
    r = _run("--dry-run", env_method="pipx")
    assert r.returncode == 0
    assert r.stdout.strip() == "would run: pipx install sediment-cli"


BOOTSTRAPS_UV = [
    "would run: curl -LsSf https://astral.sh/uv/install.sh | sh",
    "would run: uv tool install sediment-cli",
]


def _shim(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)


def _detect(bin_dir: Path, *, python3: str = "exit 1") -> subprocess.CompletedProcess:
    """Run detection against ``bin_dir``, shadowing the ambient python3.

    Every detect test states the interpreter it means to test with, because
    the runner's own is not a constant: macOS ships 3.9, ubuntu-24.04 ships
    3.12, and a test that let the real one through would assert a different
    branch on each (it did, and CI went red). Default: too old for the
    wheel, so only an explicitly-provided tool can win.
    """
    _shim(bin_dir, "python3", python3)
    return subprocess.run(
        ["sh", str(INSTALL_SH), "--dry-run"],
        capture_output=True,
        text=True,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        timeout=30,
    )


def test_detect_bootstraps_uv_when_nothing_usable_is_present(bin_dir: Path) -> None:
    # No uv, no pipx, and a python3 older than the 3.12 the wheel requires:
    # the script must install uv itself rather than print a command for
    # someone to run.
    r = _detect(bin_dir)
    assert r.returncode == 0
    assert r.stdout.splitlines() == BOOTSTRAPS_UV, r.stdout


def test_detect_skips_pipx_on_an_old_interpreter(bin_dir: Path) -> None:
    # `apt install pipx` on Debian 12 is python3.11; pipx builds its venv
    # with its own interpreter, so `pipx install` would fail the wheel's
    # requires-python (">=3.12") with a resolver error that reads like a
    # missing package. That machine must get uv, not a broken install.
    _shim(bin_dir, "python3.11", "exit 1")  # the version probe says "too old"
    _shim(bin_dir, "pipx", f"echo {bin_dir / 'python3.11'}")  # pipx environment
    r = _detect(bin_dir)
    assert r.returncode == 0
    assert r.stdout.splitlines() == BOOTSTRAPS_UV, r.stdout


def test_detect_uses_pipx_on_a_new_enough_interpreter(bin_dir: Path) -> None:
    _shim(bin_dir, "python3.12", "exit 0")
    _shim(bin_dir, "pipx", f"echo {bin_dir / 'python3.12'}")  # pipx environment
    r = _detect(bin_dir)
    assert r.returncode == 0
    assert r.stdout.strip() == "would run: pipx install sediment-cli", r.stdout


def test_detect_falls_back_to_the_pipx_shebang(bin_dir: Path) -> None:
    # pipx older than 1.2 has no `environment` subcommand; its shebang names
    # the same interpreter. Here that interpreter is new enough.
    _shim(bin_dir, "python3.12", "exit 0")
    pipx = bin_dir / "pipx"
    pipx.write_text(f"#!{bin_dir / 'python3.12'}\n")  # no `environment` support
    pipx.chmod(0o755)
    r = _detect(bin_dir)
    assert r.returncode == 0
    assert r.stdout.strip() == "would run: pipx install sediment-cli", r.stdout


def test_forced_pipx_skips_the_interpreter_gate() -> None:
    # --method is the operator overriding detection; it must not silently
    # become a different method (the same posture --method pip already has).
    r = _run("--dry-run", "--method", "pipx")
    assert r.stdout.strip() == "would run: pipx install sediment-cli"


def test_detect_uses_pip_when_python_is_new_enough(bin_dir: Path) -> None:
    # A python3 that reports 3.12: the pip branch is chosen, no uv bootstrap.
    r = _detect(bin_dir, python3="exit 0")
    assert r.returncode == 0
    assert r.stdout.strip() == "would run: python3 -m pip install --user sediment-cli"


def test_unknown_method_fails() -> None:
    r = _run("--dry-run", "--method", "conda")
    assert r.returncode == 1
    assert "unknown method" in r.stderr


def test_unknown_flag_fails() -> None:
    r = _run("--frobnicate")
    assert r.returncode == 1
    assert "unknown argument" in r.stderr


def test_install_guidance_separates_capture_and_local_database_prerequisites(bin_dir):
    _shim(bin_dir, "uv", "exit 0")
    _shim(bin_dir, "sediment", "exit 0")
    result = subprocess.run(
        ["sh", str(INSTALL_SH), "--method", "uv"],
        capture_output=True,
        text=True,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "sediment login <url>" in result.stdout
    assert "sediment login <url> --capture --with-token" in result.stdout
    assert "operator" in result.stdout
    assert "ingest" in result.stdout
    assert "capture-only" in result.stdout.lower()
    assert "libpq" in result.stdout
    assert (
        "https://github.com/sediment-ai/sediment/blob/main/docs/quickstart.md"
        in result.stdout
    )
