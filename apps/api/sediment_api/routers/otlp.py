# SPDX-License-Identifier: AGPL-3.0-or-later
"""
POST /v1/logs — OTLP/HTTP JSON receiver for coding-agent telemetry.

The path is fixed by the OTLP spec (``<endpoint>/v1/logs``), so no
``/ingest`` prefix. Translation lives in ``packages/capture`` (one
translator per agent, self-filtering by event name); this route walks each
translated fact into the store.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sediment_capture import parse_otlp_logs
from sediment_core import FactStore
from starlette.concurrency import run_in_threadpool

from ..config import settings
from ..deps import get_store, verify_ingest_token

logger = logging.getLogger("sediment.api.otlp")
router = APIRouter(tags=["otlp"])


@router.post("/v1/logs")
async def ingest_otlp_logs(
    request: Request,
    _: None = Depends(verify_ingest_token),
    store: FactStore = Depends(get_store),
) -> dict[str, Any]:
    try:
        payload = await request.json()
    except HTTPException:
        # The body-size middleware's 413 surfaces inside the read — it must
        # stay a 413, not collapse into the 400 below.
        raise
    except Exception as exc:
        # Malformed body: 400 so the exporter drops it rather than retrying.
        raise HTTPException(status_code=400, detail="invalid OTLP/JSON body") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="OTLP body must be a JSON object")

    capture = parse_otlp_logs(payload, org_id=settings.org_id)
    stored_counts = await run_in_threadpool(
        _store_translated_facts,
        store,
        capture.decisions,
        capture.edit_observations,
        capture.rejected_edits,
        capture.retry_linkages,
    )
    record_counts = {
        "received": capture.records_received,
        "translated": capture.records_translated,
        "untranslated": capture.records_untranslated,
        "malformed": capture.records_malformed,
    }
    fact_counts = {
        name: {
            "candidates": len(facts),
            "stored": stored,
            "duplicates": len(facts) - stored,
        }
        for (name, facts), stored in zip(
            (
                ("developer_decisions", capture.decisions),
                ("edit_observations", capture.edit_observations),
                ("rejected_edits", capture.rejected_edits),
                ("retry_linkages", capture.retry_linkages),
            ),
            stored_counts,
            strict=True,
        )
    }
    retry_linkage_skips = {
        reason.value: count for reason, count in capture.retry_linkage_skips.items()
    }
    # A later storage failure can leave a committed prefix. Only completed
    # requests get a receipt; database dedup accounts for that prefix on replay.
    logger.info(
        "otlp_logs_received org_id=%s record_counts=%s malformed_containers=%d"
        " fact_counts=%s retry_linkage_skips=%s",
        settings.org_id,
        json.dumps(record_counts, sort_keys=True),
        capture.malformed_containers,
        json.dumps(fact_counts, sort_keys=True),
        json.dumps(retry_linkage_skips, sort_keys=True),
        extra={
            "org_id": settings.org_id,
            "record_counts": record_counts,
            "malformed_containers": capture.malformed_containers,
            "fact_counts": fact_counts,
            "retry_linkage_skips": retry_linkage_skips,
        },
    )
    # Empty ExportLogsServiceResponse = full success (OTLP/HTTP JSON).
    return {}


def _store_translated_facts(
    store: FactStore,
    decisions: list,
    observations: list,
    rejected: list,
    retry_linkages: list,
) -> tuple[int, int, int, int]:
    """Store one translated OTLP request outside the event loop."""
    stored = sum(store.store_decisions(decisions))
    observations_stored = sum(
        store.store_edit_observation(observation) for observation in observations
    )
    rejected_stored = sum(store.store_rejected_edit(edit) for edit in rejected)
    retry_linkages_stored = sum(
        store.store_retry_linkage(linkage) for linkage in retry_linkages
    )
    return stored, observations_stored, rejected_stored, retry_linkages_stored
