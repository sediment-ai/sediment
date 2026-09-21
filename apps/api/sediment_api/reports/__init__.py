# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read-only operator reports — the ``sediment report`` subcommands.

Each module keeps the shared script convention (AGENTS.md's scripts section):
``--org`` (defaulting from ``SEDIMENT_ORG_ID``), ``--database-url``/
``SEDIMENT_DATABASE_URL``, ``--json``, and empty result = "no data" + exit 0.
None of them import ``sediment_api.config`` — a report never needs API auth
config to read a store.
"""
