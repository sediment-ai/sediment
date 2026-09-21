# SPDX-License-Identifier: AGPL-3.0-or-later
"""Checkout entry point; standalone deployment copies delivery.py itself."""

from __future__ import annotations

import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "cli/sediment_cli/delivery.py"),
        run_name="__main__",
    )
