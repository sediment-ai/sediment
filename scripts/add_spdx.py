# SPDX-License-Identifier: AGPL-3.0-or-later
"""Insert (or check) the SPDX header on first-party Python files. Idempotent.

``contrib/erode`` is an MIT carve-out, like ``shims/`` (ADR 0027): its files
carry the MIT identifier instead.

Usage:
    uv run python scripts/add_spdx.py          # insert where missing
    uv run python scripts/add_spdx.py --check  # exit 1 if any file lacks it
"""

from __future__ import annotations

import sys
from pathlib import Path

HEADER = "# SPDX-License-Identifier: AGPL-3.0-or-later\n"
MIT_HEADER = "# SPDX-License-Identifier: MIT\n"
ROOTS = ("packages", "apps", "scripts", "sim", "litellm")
MIT_ROOTS = ("contrib/erode",)


def first_party_files(roots: tuple[str, ...] = ROOTS) -> list[Path]:
    repo = Path(__file__).resolve().parent.parent
    files: list[Path] = []
    for root in roots:
        base = repo / root
        if base.exists():
            files.extend(p for p in base.rglob("*.py") if ".venv" not in p.parts)
    return sorted(files)


def main() -> int:
    check = "--check" in sys.argv
    missing: list[Path] = []
    for header, roots in ((HEADER, ROOTS), (MIT_HEADER, MIT_ROOTS)):
        for path in first_party_files(roots):
            text = path.read_text()
            if text.startswith(header):
                continue
            missing.append(path)
            if not check:
                path.write_text(header + text)
    if check and missing:
        for p in missing:
            print(f"missing SPDX header: {p}")
        return 1
    if not check and missing:
        print(f"added SPDX header to {len(missing)} file(s)")
    else:
        print("All first-party Python files carry the SPDX header.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
