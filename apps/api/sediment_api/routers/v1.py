# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Read-only v1 endpoints for the remote CLI (ADR 0001).

GET /v1/me     — auth probe: which tenant does this bearer token serve, and
                 what API version (the same string /health reports).  The
                 CLI uses it to validate ``sediment login`` and to warn on
                 version skew between client and server.

GET /v1/facts  — the counts ``sediment facts`` prints (``cmd_facts``), as
                 JSON.  Presentation-only: COUNT(*) through ``FactStore``'s
                 quarantine-excluding defaults; no derivation output is
                 computed or persisted.

GET /v1/facts/session/{session_id} — counts for one session, used to verify
                 a client's own writes without trusting unrelated org totals.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sediment_core import FactStore, FactTable, NonEmptyId

from .. import __version__
from ..config import settings
from ..deps import CredentialIdentity, get_store, verify_operator_token, verify_token

router = APIRouter(tags=["v1"])

_SESSION_FACT_TABLES = (
    FactTable.INFERENCE_CALLS,
    FactTable.DEVELOPER_DECISIONS,
    FactTable.EDIT_OBSERVATIONS,
    FactTable.REJECTED_EDITS,
    FactTable.RETRY_LINKAGES,
)


@router.get("/me")
def me(identity: CredentialIdentity = Depends(verify_token)) -> dict[str, str]:
    """Auth probe for ``sediment login``: the deployment's tenant and the API
    version, reported verbatim so the client can detect skew."""
    return {
        "org_id": settings.org_id,
        "version": __version__,
        "authority": identity.authority,
        "client_id": identity.client_id,
    }


@router.get("/facts")
def facts(
    _: None = Depends(verify_operator_token),
    store: FactStore = Depends(get_store),
) -> dict[str, Any]:
    """One entry per ``FactTable``: total rows and the derivation-facing
    ("visible" = not quarantined) count.  Sessions are upserted metadata,
    not quarantinable facts, so they carry a bare count with no visible
    column — the same shape ``sediment facts`` prints."""
    tables: dict[str, dict[str, int]] = {}
    for table in FactTable:
        tables[table.value] = {
            "total": store.count_facts(
                settings.org_id, table, include_quarantined=True
            ),
            "visible": store.count_facts(settings.org_id, table),
        }
    return {
        "sessions": store.count_sessions(settings.org_id),
        "tables": tables,
        # quarantine_revision is the org's provenance high-water mark (MAX
        # quarantine-log rowid, 0 when none) — already an int from the store.
        "quarantine_revision": store.quarantine_revision(settings.org_id),
    }


@router.get("/facts/session/{session_id}")
def session_facts(
    session_id: NonEmptyId,
    _: None = Depends(verify_operator_token),
    store: FactStore = Depends(get_store),
) -> dict[str, Any]:
    """Fact counts for one session, so a client can verify its own writes
    without treating unrelated organization-wide totals as evidence."""
    tables = {
        table.value: {
            "total": store.count_session_facts(
                settings.org_id, table, session_id, include_quarantined=True
            ),
            "visible": store.count_session_facts(settings.org_id, table, session_id),
        }
        for table in _SESSION_FACT_TABLES
    }
    return {"session_id": session_id, "tables": tables}


@router.get("/facts/session/{session_id}/inference-calls")
def session_inference_calls(
    session_id: NonEmptyId,
    _: None = Depends(verify_operator_token),
    store: FactStore = Depends(get_store),
) -> dict[str, Any]:
    """Return the narrow fields needed to reconcile one Session's usage.

    Prompts, responses, raw provider payloads, and user identity stay absent.
    """
    try:
        calls = store.read_inference_call_reconciliation(settings.org_id, session_id)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {
        "session_id": session_id,
        "inference_calls": [
            {
                "inference_call_id": call.inference_call_id,
                "model_call_id": call.model_call_id,
                "gateway_provider": call.gateway_provider,
                "model_provider": call.model_provider,
                "model": call.model,
                "input_tokens": call.input_tokens,
                "output_tokens": call.output_tokens,
                "duration_ms": call.duration_ms,
            }
            for call in calls
        ],
    }


@router.get("/facts/session/{session_id}/compatibility-evidence")
def session_compatibility_evidence(
    session_id: NonEmptyId,
    _: None = Depends(verify_operator_token),
    store: FactStore = Depends(get_store),
) -> dict[str, Any]:
    """Return bounded decision, Edit observation, and inference join fields."""
    try:
        calls = store.read_compatibility_inference_evidence(settings.org_id, session_id)
        decisions, observations = store.read_compatibility_evidence(
            settings.org_id, session_id
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {
        "session_id": session_id,
        "inference_calls": [
            {
                "tool_call_ids": call.tool_call_ids,
            }
            for call in calls
        ],
        "developer_decisions": [
            {
                "agent_harness": decision.agent_harness,
                "accepted": decision.accepted,
                "explicit": decision.explicit,
                "interaction_mode": decision.interaction_mode,
                "call_id": decision.call_id,
            }
            for decision in decisions
        ],
        "edit_observations": [
            {
                "agent_harness": observation.agent_harness,
                "call_id": observation.call_id,
            }
            for observation in observations
        ],
    }
