# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bounded operational-report HTTP routes."""

from __future__ import annotations

from dataclasses import asdict
from datetime import timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import AwareDatetime, BaseModel
from sediment_export import OperationalReportScope

from ..deps import verify_operator_token

router = APIRouter(tags=["reports"])

_MAX_COHORT_DAYS = 31
_MAX_INFERENCE_CALLS = 50_000
_MAX_REPORT_RESPONSE_BYTES = 64 * 1024 * 1024


class ReportScopeResponse(BaseModel):
    """The explicit bounds applied to one operational report."""

    cohort_start: AwareDatetime
    cohort_end: AwareDatetime
    as_of: AwareDatetime
    max_inference_calls: Literal[50_000]


class ReportEnvelope(BaseModel):
    """Version 1 operational-report response."""

    schema_version: Literal[1] = 1
    scope: ReportScopeResponse
    report: dict[str, Any]


def _report_response(envelope: ReportEnvelope) -> Response:
    content = envelope.model_dump_json().encode()
    if len(content) > _MAX_REPORT_RESPONSE_BYTES:
        raise HTTPException(
            status_code=409,
            detail="report response exceeds the fixed limit",
        )
    return Response(content=content, media_type="application/json")


@router.get(
    "/model-outcomes",
    response_model=ReportEnvelope,
    responses={
        503: {
            "description": "Database unavailable, or work capacity or execution budget exceeded."
        }
    },
)
async def model_outcomes_report(
    cohort_start: AwareDatetime,
    cohort_end: AwareDatetime,
    as_of: AwareDatetime,
    request: Request,
    _: None = Depends(verify_operator_token),
) -> Response:
    """Return a bounded model-outcome report."""

    scope = _build_scope(cohort_start, cohort_end, as_of)
    return await request.app.state.workers.run(
        "model-report", ReportScopeResponse(**asdict(scope)).model_dump(mode="json")
    )


def _build_scope(
    cohort_start: AwareDatetime,
    cohort_end: AwareDatetime,
    as_of: AwareDatetime,
) -> OperationalReportScope:
    if cohort_end - cohort_start > timedelta(days=_MAX_COHORT_DAYS):
        raise HTTPException(
            status_code=422,
            detail=f"report cohort must not exceed {_MAX_COHORT_DAYS} days",
        )
    try:
        return OperationalReportScope(
            cohort_start=cohort_start,
            cohort_end=cohort_end,
            as_of=as_of,
            max_inference_calls=_MAX_INFERENCE_CALLS,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get(
    "/accepted-work-lifecycle",
    response_model=ReportEnvelope,
    responses={
        503: {
            "description": "Database unavailable, or work capacity or execution budget exceeded."
        }
    },
)
async def accepted_work_lifecycle_report(
    cohort_start: AwareDatetime,
    cohort_end: AwareDatetime,
    as_of: AwareDatetime,
    request: Request,
    _: None = Depends(verify_operator_token),
) -> Response:
    """Return a bounded accepted-work lifecycle report."""

    scope = _build_scope(cohort_start, cohort_end, as_of)
    return await request.app.state.workers.run(
        "lifecycle-report", ReportScopeResponse(**asdict(scope)).model_dump(mode="json")
    )
