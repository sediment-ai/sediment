# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shim — the implementation moved to ``sediment_api.mirror_gc``
so it ships in the API image as ``sediment mirror-gc``. This path keeps the
supported ``uv run python scripts/mirror_gc.py ...`` invocation working.
"""

from sediment_api.mirror_gc import main

if __name__ == "__main__":
    raise SystemExit(main())
