# SPDX-License-Identifier: AGPL-3.0-or-later
"""The optional reference hook follows source changes across all packages."""

import os
import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).parents[1] / "hooks/pre-commit"


@pytest.mark.parametrize(
    "source",
    [
        "packages/export/sediment_export/compatibility.py",
        "packages/export/sediment_export/schema_contracts.py",
        "scripts/gen_compatibility_docs.py",
    ],
)
def test_hook_refreshes_consumer_reference_from_staged_sources(tmp_path, source):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True)

    git("init", "-q")
    git("config", "user.name", "Reference test")
    git("config", "user.email", "test@example.com")
    reference = tmp_path / "docs/reference/compatibility.md"
    reference.parent.mkdir(parents=True)
    reference.write_text("old\n")
    git("add", ".")
    git("commit", "-qm", "fixture")
    changed = tmp_path / source
    changed.parent.mkdir(parents=True, exist_ok=True)
    changed.write_text("source change\n")
    git("add", source)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(
        '#!/bin/sh\ncase "$4" in\n'
        "scripts/gen_compatibility_docs.py) echo fresh > docs/reference/compatibility.md ;;\n"
        "scripts/dump_openapi.py) echo fresh > openapi.yaml ;;\n"
        "scripts/gen_cli_docs.py) echo fresh > docs/reference/cli.md ;;\n"
        "scripts/gen_api_docs.py) echo fresh > docs/reference/api.md ;;\n"
        "scripts/gen_schema_docs.py) echo fresh > docs/reference/schema.md ;;\n"
        "esac\n"
    )
    uv.chmod(0o755)
    subprocess.run(
        ["sh", str(HOOK)],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
        check=True,
        capture_output=True,
        text=True,
    )
    assert git("show", ":docs/reference/compatibility.md") == "fresh\n"
