# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shim — the implementation moved to ``sediment_api.reports.dataset_diagnostics``
so it ships in the API image as ``sediment report dataset-diagnostics``. This path keeps the
supported ``uv run python scripts/dataset_diagnostics.py ...`` invocation working.
"""

from sediment_api.reports.dataset_diagnostics import main

if __name__ == "__main__":
    raise SystemExit(main())
