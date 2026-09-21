# SPDX-License-Identifier: AGPL-3.0-or-later
"""Homebrew discovery doesn't require changes to the caller's shell."""

import os
import subprocess
from types import SimpleNamespace

import pytest

from sediment_core import postgres_engine


@pytest.mark.parametrize(
    "platform,configured", [("linux", None), ("darwin", "/custom/pg_config")]
)
def test_configured_driver_keeps_path(monkeypatch, platform, configured):
    monkeypatch.setattr(postgres_engine.sys, "platform", platform)
    monkeypatch.setattr(postgres_engine.shutil, "which", lambda _: configured)
    monkeypatch.setenv("PATH", "/configured/bin")
    postgres_engine.configure_libpq()
    assert os.environ["PATH"] == "/configured/bin"


def test_homebrew_discovery_preserves_existing_path(monkeypatch, tmp_path):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    (binaries / "pg_config").touch()
    monkeypatch.setattr(postgres_engine.sys, "platform", "darwin")
    monkeypatch.setattr(postgres_engine.shutil, "which", lambda _: None)
    monkeypatch.setenv("PATH", "/configured/bin")
    monkeypatch.setattr(
        postgres_engine.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(stdout=f"{tmp_path}\n"),
    )
    postgres_engine.configure_libpq()
    assert os.environ["PATH"] == f"/configured/bin{os.pathsep}{binaries}"


@pytest.mark.parametrize("prefix", ["", ".", "relative", "/missing/homebrew/libpq"])
def test_unusable_homebrew_prefix_does_not_enter_path(monkeypatch, prefix):
    monkeypatch.setattr(postgres_engine.sys, "platform", "darwin")
    monkeypatch.setattr(postgres_engine.shutil, "which", lambda _: None)
    monkeypatch.setenv("PATH", "/configured/bin")
    monkeypatch.setattr(
        postgres_engine.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(stdout=prefix),
    )
    postgres_engine.configure_libpq()
    assert os.environ["PATH"] == "/configured/bin"


@pytest.mark.parametrize(
    "error", [FileNotFoundError(), subprocess.TimeoutExpired("brew", 10)]
)
def test_homebrew_failure_leaves_normal_driver_diagnostic(monkeypatch, error):
    monkeypatch.setattr(postgres_engine.sys, "platform", "darwin")
    monkeypatch.setattr(postgres_engine.shutil, "which", lambda _: None)
    monkeypatch.setenv("PATH", "/configured/bin")

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(postgres_engine.subprocess, "run", fail)
    postgres_engine.configure_libpq()
    assert os.environ["PATH"] == "/configured/bin"
