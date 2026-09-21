# SPDX-License-Identifier: AGPL-3.0-or-later
"""Database prerequisites fail with an actionable, credential-free diagnostic."""

import os
import subprocess
import sys

import pytest

# A fresh interpreter is required: importing psycopg once caches a working libpq.
# Simulate the driver's ImportError without depending on the host's library path.
MISSING_LIBPQ = """
import importlib.abc, sys
class MissingLibpq(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'psycopg':
            raise ImportError('no pq wrapper available; secret-driver-diagnostic')
sys.meta_path.insert(0, MissingLibpq())
from sediment_cli.cli import main
raise SystemExit(main(sys.argv[1:]))
"""


@pytest.fixture
def no_libpq(tmp_path):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("SEDIMENT_", "OTEL_"))
    }
    env.update(
        HOME=str(tmp_path),
        SEDIMENT_ORG_ID="missing-library-test",
        SEDIMENT_DATABASE_URL=(
            "postgresql+psycopg://postgres:secret-database-password@127.0.0.1:9/test"
        ),
        SEDIMENT_MIGRATOR_PASSWORD="synthetic-migrator-password",
        SEDIMENT_RUNTIME_PASSWORD="synthetic-runtime-password",
        SEDIMENT_OPERATOR_PASSWORD="synthetic-operator-password",
    )
    env["SEDIMENT_BOOTSTRAP_DATABASE_URL"] = env["SEDIMENT_DATABASE_URL"]

    def run(*args):
        return subprocess.run(
            [sys.executable, "-c", MISSING_LIBPQ, *args],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )

    return run


@pytest.mark.parametrize(
    "args",
    [
        ("db", "status"),
        ("db", "upgrade"),
        ("db", "provision"),
        ("server",),
        ("quarantine-log",),
        ("facts",),
    ],
)
def test_database_commands_explain_missing_libpq_without_secrets(no_libpq, args):
    result = no_libpq(*args)
    assert result.returncode == 1
    assert "install a maintained system libpq library" in result.stderr
    assert "Traceback" not in result.stderr
    assert "secret-" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "args",
    [("--help",), ("--version",)],
)
def test_capture_commands_do_not_import_postgresql_driver(no_libpq, args):
    result = no_libpq(*args)
    assert result.returncode == 0, result.stderr
    assert "secret-" not in result.stdout + result.stderr


def test_capture_hook_installation_does_not_import_postgresql_driver(
    no_libpq, tmp_path
):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    result = no_libpq("install", str(tmp_path), "--no-agents", "--no-env")
    assert result.returncode == 0, result.stderr
    assert (tmp_path / ".git/hooks/pre-push").is_file()


def test_capture_delivery_status_does_not_import_postgresql_driver(no_libpq, tmp_path):
    directory = tmp_path / "delivery"
    directory.mkdir(mode=0o700)
    result = no_libpq("delivery", "status", "--directory", str(directory))
    assert result.returncode == 0, result.stderr
    assert '"pending": 0' in result.stdout
