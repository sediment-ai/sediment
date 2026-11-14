# SPDX-License-Identifier: AGPL-3.0-or-later
"""Typed response envelopes for the ingest doors (AGENTS.md §API Conventions).

Routers return these instead of bare dicts so the generated OpenAPI names
each outcome. The wire bodies stay exactly the documented shapes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import ConfigDict, Field

_CLOSED = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class IngestReceipt:
    """The retained Fact for one single-Fact ingest request."""

    __pydantic_config__ = _CLOSED

    fact_id: Annotated[
        str,
        Field(description="ID of the retained Fact, including on redelivery"),
    ]
    stored: Annotated[
        bool,
        Field(
            description=(
                "False when a redelivery collapsed on a UNIQUE index (ADR "
                "0003); success, not an error"
            )
        ),
    ]


@dataclass(frozen=True)
class IngestSkipped:
    """A declined delivery that the sender shouldn't retry."""

    __pydantic_config__ = _CLOSED

    skipped: Literal[True]
    reason: Annotated[
        str, Field(description="Why the route stored nothing for this delivery")
    ]


@dataclass(frozen=True)
class OTLPExportLogsResponse:
    """The empty OTLP/HTTP JSON ExportLogsServiceResponse: full success."""

    __pydantic_config__ = _CLOSED
