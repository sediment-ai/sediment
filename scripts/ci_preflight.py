# SPDX-License-Identifier: AGPL-3.0-or-later
"""Route prose, shim, and artifact-scan checks conservatively; check reviews early."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Markdown that a generator produces, or that a packaging/golden test pins
# against shipped bytes. Any other added or modified ``*.md`` can take the
# prose path. Non-markdown paths (code, workflows, locks, Dockerfiles,
# ``security/``, OpenAPI, help goldens, SVGs) already select the full gate.
FULL_VALIDATION_MARKDOWN = {
    # Exact twin of ``cli/sediment_cli/agent_guide.txt``; the guide test
    # requires a match (``cli/tests/test_agent_guide.py``).
    "docs/operate/agent-guide.md",
}
FULL_VALIDATION_MARKDOWN_PREFIXES = (
    # Outputs of gen_api_docs / gen_cli_docs / gen_schema_docs /
    # gen_compatibility_docs. The prose path still runs ``--check``; a hand
    # edit still selects the full suite so packaging and schema contract
    # tests see it.
    "docs/reference/",
)
SHIM_FILES = {
    "cli/hatch_build.py",
    "cli/sediment_cli/attribution.py",
    "cli/sediment_cli/delivery.py",
    "cli/sediment_cli/cli.py",
    "cli/pyproject.toml",
    "pyproject.toml",
    "uv.lock",
    ".github/workflows/shims.yml",
    "scripts/ci_preflight.py",
    "scripts/tests/test_ci_preflight.py",
}
# Inputs that can change a scanned artifact or the scanner's verdict. A pull
# request or merge group touching none of them skips the artifact scans; main
# pushes, the daily schedule, releases, and manual runs always scan, so
# upstream drift surfaces there instead of on unrelated pull requests.
SCAN_FILES = {
    "Dockerfile",
    "uv.lock",
    ".env.example",
    "docker-compose.yml",
    ".github/workflows/security.yml",
    "scripts/ci_preflight.py",
}
SCAN_PREFIXES = ("docker/", "security/", "scripts/security_", "shims/pi/package")


def _changed_paths(root: Path, event: str, base: str) -> list[str] | None:
    """Paths a readable PR or merge-group diff changes; None when unknown."""
    if event not in {"pull_request", "merge_group"} or not re.fullmatch(
        r"[0-9a-f]{40}", base
    ):
        return None
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "-z", "--no-renames", base, "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
        )
        names = result.stdout.decode("utf-8").split("\0")
        if names.pop() != "" or not names:
            return None
        return names
    except (OSError, subprocess.CalledProcessError, UnicodeError):
        return None


def requires_shim_validation(root: Path, event: str, base: str) -> bool:
    """Skip shim work only for a readable PR diff outside its dependencies."""
    names = _changed_paths(root, event, base)
    return names is None or any(
        name.startswith("shims/") or name in SHIM_FILES for name in names
    )


def requires_artifact_scans(root: Path, event: str, base: str) -> bool:
    """Skip artifact scans only for a readable PR diff outside the scan inputs."""
    names = _changed_paths(root, event, base)
    # Markdown never enters an image or the scanner's verdict; review_digest
    # skips it for the same reason.
    return names is None or any(
        not name.endswith(".md")
        and (
            name in SCAN_FILES
            or name.startswith(SCAN_PREFIXES)
            or name.rsplit("/", 1)[-1] == "pyproject.toml"
        )
        for name in names
    )


def is_prose_path(name: str) -> bool:
    """True when ``name`` is ordinary markdown eligible for the prose path."""
    if not name.endswith(".md"):
        return False
    if name in FULL_VALIDATION_MARKDOWN:
        return False
    return not name.startswith(FULL_VALIDATION_MARKDOWN_PREFIXES)


def requires_full_validation(root: Path, event: str, base: str) -> bool:
    """Only added or modified regular prose files qualify for reduced checks."""
    if event not in {"push", "pull_request", "merge_group"} or not re.fullmatch(
        r"[0-9a-f]{40}", base
    ):
        return True
    try:
        result = subprocess.run(
            ["git", "diff", "--name-status", "-z", "--no-renames", base, "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
        )
        fields = result.stdout.decode("utf-8").split("\0")
        if fields.pop() != "" or not fields or len(fields) % 2:
            return True
        for status, name in zip(fields[::2], fields[1::2], strict=True):
            path = root / name
            if status not in {"A", "M"} or not is_prose_path(name) or path.is_symlink():
                return True
            if not path.is_file() or path.stat().st_mode & 0o111:
                return True
        return False
    except (OSError, subprocess.CalledProcessError, UnicodeError):
        # An unavailable base or unreadable diff must retain the full gate.
        return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("scope", "shims", "scans", "reviews"))
    args = parser.parse_args()
    if args.command == "shims":
        required = requires_shim_validation(
            ROOT, os.environ.get("CI_EVENT", ""), os.environ.get("CI_BASE_SHA", "")
        )
        print(f"shims={str(required).lower()}")
        return 0
    if args.command == "scans":
        required = requires_artifact_scans(
            ROOT, os.environ.get("CI_EVENT", ""), os.environ.get("CI_BASE_SHA", "")
        )
        print(f"scans={str(required).lower()}")
        return 0
    if args.command == "scope":
        full = requires_full_validation(
            ROOT, os.environ.get("CI_EVENT", ""), os.environ.get("CI_BASE_SHA", "")
        )
        print(f"full={str(full).lower()}")
        return 0

    from security_image_assurance import review_digest
    from security_policy import check_source_reviews

    try:
        dispositions = json.loads((ROOT / "security/dispositions.json").read_text())[
            "dispositions"
        ]
        errors = check_source_reviews(
            dispositions, review_digest(ROOT), datetime.now(UTC).date()
        )
    except (OSError, KeyError, TypeError, ValueError) as exc:
        errors = [f"Cannot read source reviews: {exc}"]
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        print(
            "Run security manually to collect full scan evidence before reviewing "
            "or removing stale dispositions. This check never renews a review.",
            file=sys.stderr,
        )
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
