# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pin the minimal-comments checker (CONTRIBUTING.md ground rules)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from check_comments import (  # noqa: E402
    COMMENTS_BUDGET,
    is_excuse,
    main,
)

REPO_ROOT = Path(__file__).parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_comments.py"


def test_excuse_patterns():
    assert is_excuse("# TODO: wire this up")
    assert is_excuse("# FIXME later")
    assert is_excuse("# workaround for flaky CI")
    assert is_excuse("# for now just skip")
    assert is_excuse("# ponytail: skip until we invent pairing")
    assert not is_excuse("# SPDX-License-Identifier: AGPL-3.0-or-later")
    assert not is_excuse(
        "# ponytail: fixed rename limit; promote to policy if repos need it"
    )
    assert not is_excuse("# noqa: E402")


def test_repository_passes_at_budget():
    assert main([]) == 0


def test_budget_constant_matches_checker_default():
    assert COMMENTS_BUDGET > 0
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--budget", "0"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "comment density" in result.stderr
