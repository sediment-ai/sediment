# SPDX-License-Identifier: AGPL-3.0-or-later
"""
POST /ingest/compute — vendor-neutral Compute allocation ingest.

The execution platform that runs a coding agent (a sandbox, a container
scheduler, a CI runner) posts the CPU and memory limits a Session's
environment received: once when the environment starts, and again on every
resize. ``allocation_source`` records who chose the allocation, so the
compute report never presents an agent's own request as an assigned
treatment. The sender is bearer-authenticated; Sediment stores its assertion
and does not verify it against the platform.

Dedup: the store's ``uq_compute_allocations_natural`` UNIQUE index on the
complete allocation collapses redeliveries; ``stored: false`` means success,
not an error.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sediment_core import (
    ComputeAllocation,
    ComputeAllocationSource,
    FactStore,
    NonEmptyId,
    UtcRepresentableDatetime,
)

from ..config import settings
from ..deps import get_store, verify_ingest_token

router = APIRouter(tags=["compute"])


class ComputeAllocationRequest(BaseModel):
    """One Compute allocation as the execution platform observed it.

    ``extra="forbid"`` so a caller naming an org or fabricating fields is
    rejected at the boundary. The limits carry the Fact's own bounds, so
    malformed input 422s at the door rather than inside the handler."""

    model_config = ConfigDict(extra="forbid")

    session_id: NonEmptyId = Field(
        description=(
            "The Session ID the coding agent's captured Facts carry, such as "
            "the gateway Session header"
        )
    )
    allocation_source: ComputeAllocationSource = Field(
        description=(
            "Who chose the allocation: default, agent_request, operator, or "
            "randomized. Only randomized supports a causal comparison"
        )
    )
    cpu_millicores: int | None = Field(
        default=None,
        ge=1,
        le=2**63 - 1,
        strict=True,
        description="CPU limit in millicores; 1000 is one CPU",
    )
    memory_bytes: int | None = Field(
        default=None,
        ge=1,
        le=2**63 - 1,
        strict=True,
        description="Memory limit in bytes",
    )
    occurred_at: UtcRepresentableDatetime = Field(
        description=(
            "When the allocation took effect, by the sender's clock; it must "
            "convert to a UTC instant"
        )
    )

    @model_validator(mode="after")
    def _require_limit(self):
        if self.cpu_millicores is None and self.memory_bytes is None:
            raise ValueError("send cpu_millicores, memory_bytes, or both")
        return self


@router.post("/compute")
def ingest_compute_allocation(
    body: ComputeAllocationRequest,
    _: None = Depends(verify_ingest_token),
    store: FactStore = Depends(get_store),
) -> dict[str, Any]:
    allocation = ComputeAllocation(
        org_id=settings.org_id,
        session_id=body.session_id,
        allocation_source=body.allocation_source,
        cpu_millicores=body.cpu_millicores,
        memory_bytes=body.memory_bytes,
        occurred_at=body.occurred_at,
    )
    receipt = store.store_compute_allocation_receipt(allocation)
    return {"fact_id": receipt.fact_id, "stored": receipt.stored}
