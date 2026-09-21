# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run reviewed local static rules without uploading source or scanner metrics."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "1.177.0"
CONFIG = ROOT / "security/semgrep.yml"
SCANNER = ["uv", "tool", "run", "--from", f"semgrep=={VERSION}", "semgrep"]


class StaticFailure(Exception):
    """Static scanning did not produce a complete clean result."""


def run(command: list[str]) -> None:
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=1200,
            env={
                **os.environ,
                "SEMGREP_SEND_METRICS": "off",
                "SEMGREP_ENABLE_VERSION_CHECK": "0",
            },
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise StaticFailure(f"static scanner failed ({type(exc).__name__})") from None
    if result.returncode:
        sys.stderr.write(result.stderr[-8192:])
        sys.stderr.write(result.stdout[-8192:])
        raise StaticFailure(f"static scanner exit {result.returncode}")


def validate_report(path: Path) -> None:
    try:
        report = json.loads(path.read_text())
        if (
            report["results"] != []
            or report["errors"] != []
            or not report["paths"]["scanned"]
        ):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        raise StaticFailure("static scan is incomplete or has findings") from None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    report = args.out.resolve() / "source.semgrep.json"
    try:
        # No registry rules, login, or `semgrep ci`: --metrics off keeps this local.
        run(
            [
                *SCANNER,
                "--test",
                "--config",
                str(CONFIG),
                "security/tests/unsafe.py",
                "--metrics",
                "off",
                "--disable-version-check",
            ]
        )
        run(
            [
                *SCANNER,
                "scan",
                "--config",
                str(CONFIG),
                "--strict",
                "--error",
                "--metrics",
                "off",
                "--disable-version-check",
                "--disable-nosem",
                "--json",
                "--output",
                str(report),
                "--exclude",
                "**/tests/**",
                "packages",
                "apps",
                "cli",
                "litellm",
                "scripts",
            ]
        )
        validate_report(report)
        (args.out / "source.static-metadata.json").write_text(
            json.dumps(
                {
                    "scanner": "semgrep",
                    "version": VERSION,
                    "checked_at": datetime.now(UTC).isoformat(),
                    "rules_sha256": hashlib.sha256(CONFIG.read_bytes()).hexdigest(),
                    "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
                },
                indent=2,
            )
            + "\n"
        )
        return 0
    except StaticFailure as exc:
        print(f"static security gate failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
