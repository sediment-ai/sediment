# SPDX-License-Identifier: AGPL-3.0-or-later
"""Checkout shim for the packaged transcript capture client."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parents[1] / "cli" / "sediment_cli" / "transcript.py"

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
