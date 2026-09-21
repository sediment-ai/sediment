# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shim — the implementation moved to ``sediment_api.reports.recovery_yield_report``
so it ships in the API image as ``sediment report recovery-yield``. This path keeps the
supported ``uv run python scripts/recovery_yield_report.py ...`` invocation working.
"""

from sediment_api.reports.recovery_yield_report import main

if __name__ == "__main__":
    raise SystemExit(main())
