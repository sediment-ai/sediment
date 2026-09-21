# SPDX-License-Identifier: AGPL-3.0-or-later
"""Checkout shim: the attribution stamper moved into the package at
``cli/sediment_cli/attribution.py`` so installed CLIs carry it
(``sediment install`` / ``mark`` / ``doctor`` …). This shim keeps every
documented ``python3 scripts/sediment_attribution.py …`` invocation working
on a bare checkout — stdlib-only, no venv required — by executing the moved
file by path. Fleet/MDM bundles are unaffected: ``install --fleet`` copies
the implementation file's own bytes, not this shim."""

import runpy
import sys
from pathlib import Path

_TARGET = (
    Path(__file__).resolve().parent.parent / "cli" / "sediment_cli" / "attribution.py"
)

if __name__ == "__main__":
    sys.argv[0] = str(_TARGET)
    runpy.run_path(str(_TARGET), run_name="__main__")
