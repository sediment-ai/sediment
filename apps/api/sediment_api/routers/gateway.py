# SPDX-License-Identifier: AGPL-3.0-or-later
"""POST /ingest/gateway — gateway payloads into inference-call facts."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, ValidationError
from sediment_capture import ADAPTERS, resolve_identity
from sediment_core import (
    FactStore,
    GatewayProvider,
    InferenceCallIdentityConflict,
    NonEmptyId,
)
from sediment_core.models import AwareDatetime

from ..config import settings
from ..deps import get_store, verify_ingest_token

logger = logging.getLogger("sediment.api.gateway")
router = APIRouter(tags=["gateway"])


class GatewayCapture(BaseModel):
    """Identity and observation instant prepared once by the gateway callback."""

    model_config = ConfigDict(extra="forbid")

    id: UUID
    observed_at: AwareDatetime


class GatewayIngestRequest(BaseModel):
    # extra="forbid": the envelope carries no org — tenancy binds to the
    # credential — so a client naming one is rejected, not silently
    # ignored. Ids are optional because the thinned callback forwards
    # payloads without extracting identity and the server resolves it; when
    # present they win, and empty/whitespace-only values are still rejected
    # (NonEmptyId, the same validator InferenceCall enforces): placeholder
    # sessions are banned (ADR 0002), and a 422 here beats a 500 at fact
    # construction.
    model_config = ConfigDict(extra="forbid")

    provider: GatewayProvider
    session_id: NonEmptyId | None = None
    user_id: NonEmptyId | None = None
    payload: dict[str, Any]
    capture: GatewayCapture | None = None


@router.post("/gateway")
def ingest_gateway(
    body: GatewayIngestRequest,
    _: None = Depends(verify_ingest_token),
    store: FactStore = Depends(get_store),
) -> dict[str, Any]:
    adapter = ADAPTERS.get(body.provider)
    if adapter is None:
        raise HTTPException(
            status_code=400, detail=f"Unsupported provider: {body.provider}"
        )

    session_id, user_id = body.session_id, body.user_id
    if session_id is None or user_id is None:
        # Server-side resolution is LiteLLM-SLO-specific: another
        # provider's payload must never have those heuristics run over it —
        # its missing ids go straight to the no_session skip.
        identity = (
            resolve_identity(body.payload)
            if body.provider == GatewayProvider.LITELLM
            else None
        )
        if identity is not None:
            session_id = session_id or identity.session_id
            user_id = user_id or identity.user_id
        if session_id is None:
            # Absent, never guessed (ADR 0002): no placeholder session id
            # may reach the store. 200 so the forwarder does not retry.
            logger.warning(
                "gateway_ingest_skipped_no_session provider=%s call_id=%s",
                body.provider,
                body.payload.get("litellm_call_id"),
            )
            return {"skipped": True, "reason": "no_session"}

    try:
        inference_call = adapter.normalize(
            body.payload,
            session_id=session_id,
            user_id=user_id,
            org_id=settings.org_id,
            capture_id=body.capture.id if body.capture is not None else None,
            observed_at=body.capture.observed_at if body.capture is not None else None,
        )
    except ValidationError as exc:
        # The adapter degrades hostile payload fields by design; identity is
        # the one construction-raise path left, and payload-derived identity
        # widened it. A malformed body from an authenticated caller is a
        # 422, never a 500 (AGENTS.md §API Conventions).
        logger.warning(
            "gateway_ingest_invalid_payload provider=%s call_id=%s",
            body.provider,
            body.payload.get("litellm_call_id"),
            extra={
                "reason": "invalid_fact",
                "source": body.provider.value,
                "record_position": 0,
            },
        )
        raise HTTPException(
            status_code=422,
            detail="payload does not normalize to a valid inference call",
        ) from exc
    try:
        receipt = store.store_inference_call_receipt(inference_call)
    except InferenceCallIdentityConflict as exc:
        logger.warning(
            "gateway_ingest_identity_conflict",
            extra={
                "org_id": settings.org_id,
                "reason": "inference_call_identity_conflict",
            },
        )
        raise HTTPException(
            status_code=409,
            detail={"code": "inference_call_identity_conflict"},
        ) from exc
    logger.info(
        "inference_call_stored fact_id=%s stored=%s",
        receipt.fact_id,
        receipt.stored,
    )
    return {"fact_id": receipt.fact_id, "stored": receipt.stored}
