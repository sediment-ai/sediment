# SPDX-License-Identifier: AGPL-3.0-or-later
"""
org_id validation and normalization.

org_id is the tenant boundary: every fact, derivation, and export is keyed
by it, and the mirror uses it in filesystem paths. Every boundary
that accepts an org_id — config validation now, token→org mapping later —
must route through normalize_org_id.
"""

from __future__ import annotations

import re

_ORG_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")


def normalize_org_id(raw: str) -> str:
    """Lowercase and validate an org_id; raise ValueError if invalid.

    Rejects non-ASCII input before lowercasing so unicode lookalikes
    (e.g. U+212A KELVIN SIGN, which str.lower() maps to "k") cannot
    alias an ASCII tenant.
    """
    if not raw.isascii():
        raise ValueError(f"org_id must be ASCII: {raw!r}")
    normalized = raw.lower()
    if not _ORG_ID_RE.fullmatch(normalized):
        raise ValueError(f"invalid org_id: {raw!r}")
    return normalized
