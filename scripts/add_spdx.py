# SPDX-License-Identifier: AGPL-3.0-or-later
"""Insert (or check) the SPDX header on first-party Python files. Idempotent.

Usage:
    uv run python scripts/add_spdx.py          # insert where missing
    uv run python scripts/add_spdx.py --check  # exit 1 if any file lacks it
"""

from __future__ import annotations

import sys
from pathlib import Path

HEADER = "# SPDX-License-Identifier: AGPL-3.0-or-later\n"
ROOTS = ("packages", "apps", "scripts", "sim", "litellm")


def first_party_files() -> list[Path]:
    repo = Path(__file__).resolve().parent.parent
    files: list[Path] = []
    for root in ROOTS:
        base = repo / root
        if base.exists():
            files.extend(p for p in base.rglob("*.py") if ".venv" not in p.parts)
    return sorted(files)


def main() -> int:
    check = "--check" in sys.argv
    missing: list[Path] = []
    for path in first_party_files():
        text = path.read_text()
        if text.startswith(HEADER):
            continue
        missing.append(path)
        if not check:
            path.write_text(HEADER + text)
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
