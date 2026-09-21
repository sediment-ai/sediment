# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shim — the implementation moved to ``sediment_api.reports.attribution_share_report``
so it ships in the API image as ``sediment report attribution-share``. This path keeps the
supported ``uv run python scripts/attribution_share_report.py ...`` invocation working.
"""

from sediment_api.reports.attribution_share_report import main

if __name__ == "__main__":
    raise SystemExit(main())
