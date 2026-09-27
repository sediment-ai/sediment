# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sediment ingest API."""

# Single source for the API version string — FastAPI's ``app.version``
# (reported by /health) and GET /v1/me both read it, so the two cannot drift.
__version__ = "0.2.0"
