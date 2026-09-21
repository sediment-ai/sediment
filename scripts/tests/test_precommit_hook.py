# SPDX-License-Identifier: AGPL-3.0-or-later
"""The opt-in reference-regeneration pre-commit hook.

The failure this pins: someone adds a parser module or a route, a generator
picks it up, and the hook's path filter does not — so the hook stays silent
and the staleness only surfaces in CI, which is the thing the hook exists to
spare them. The filter and the generators' sources must name the same
surfaces.
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
HOOK = REPO_ROOT / "scripts" / "hooks" / "pre-commit"

# Paths a change to which must regenerate a committed artifact. From
# gen_cli_docs.render(): the cli tree, the attribution tree, mirror_gc, and
# every report module in _REPORTS. From dump_openapi/gen_api_docs: the
# routers and the spec they dump to. From gen_schema_docs.GROUPS: the fact
# models and every module defining a derived artifact or a training row.
GENERATOR_SOURCES = [
    "cli/sediment_cli/cli.py",
    "cli/sediment_cli/attribution.py",
    "apps/api/sediment_api/mirror_gc.py",
    "apps/api/sediment_api/reports/model_report.py",
    "apps/api/sediment_api/reports/precision_report.py",
    "apps/api/sediment_api/routers/gateway.py",
    "apps/api/sediment_api/routers/v1.py",
    "openapi.yaml",
    "scripts/gen_cli_docs.py",
    "scripts/gen_api_docs.py",
    "scripts/dump_openapi.py",
    "packages/core/sediment_core/models.py",
    "packages/derive/sediment_derive/attribution.py",
    "packages/derive/sediment_derive/rollout.py",
    "packages/derive/sediment_derive/recovery.py",
    "packages/export/sediment_export/attributed_completions.py",
    "packages/export/sediment_export/dpo.py",
    "packages/export/sediment_export/sft.py",
    "packages/export/sediment_export/diff_sft.py",
    "packages/export/sediment_export/recovery.py",
    "scripts/gen_schema_docs.py",
]

UNRELATED = [
    "packages/core/sediment_core/store.py",
    "packages/derive/sediment_derive/mirror.py",
    "docs/quickstart.md",
    "README.md",
    "apps/api/sediment_api/config.py",
]


def _patterns() -> list[str]:
    """The shell `case` patterns the hook filters staged paths with."""
    text = HOOK.read_text(encoding="utf-8")
    _, _, rest = text.partition('case "$staged" in')
    body, _, _ = rest.partition(") ;;")
    # The pattern list is one `|`-separated alternation, wrapped with a
    # trailing backslash for line length.
    return [p.strip() for p in body.replace("\\\n", "").split("|") if p.strip()]


def test_hook_is_executable() -> None:
    assert os.access(HOOK, os.X_OK), "git silently ignores a non-executable hook"


def test_filter_covers_every_generator_source() -> None:
    patterns = _patterns()
    for path in GENERATOR_SOURCES:
        assert any(fnmatch.fnmatch(path, p) for p in patterns), (
            f"{path} feeds docs/reference/cli.md but no hook pattern matches it"
        )


def test_filter_ignores_unrelated_paths() -> None:
    # A filter that matches everything would run the generator on every
    # commit — slow, and it would mask a real drift in the list above.
    patterns = _patterns()
    for path in UNRELATED:
        assert not any(fnmatch.fnmatch(path, p) for p in patterns), path


def test_hook_is_valid_posix_sh() -> None:
    assert (
        subprocess.run(["sh", "-n", str(HOOK)], capture_output=True).returncode == 0
    ), "hook does not parse under /bin/sh"
