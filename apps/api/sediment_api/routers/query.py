# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read-only CI and Session investigations over captured Facts.

Stored observations establish Session-to-commit identity. Optional mirror-based
call/file Attribution remains explicitly inferred and is recomputed per request.
No investigation result is persisted (ADR 0001).
"""

from __future__ import annotations

from sediment_derive.session_commit import bind_session_commit_keys_result

import base64
import binascii
import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator
from sediment_core import (
    AgentHarness,
    CIProvider,
    CIResult,
    FactStore,
    EVIDENCE_REFERENCE_LIMIT,
    EvidenceInventory,
    EvidenceManifest,
    EvidenceRead,
    EvidenceReference,
    EvidenceSchemaVersion,
    validate_evidence_references,
    ForgeProvider,
    ForgeHost,
    ProviderRepositoryId,
    RequiredRepoSlug,
    RepositoryReadAmbiguous,
    OperationalReportLimitExceeded,
    GatewayProvider,
    InteractionMode,
    NonEmptyId,
    WorkflowName,
    normalize_commit_sha,
)
from sediment_core.evidence import EvidenceReadError, encode_evidence_json
from sediment_core.models import AwareDatetime
from sediment_core.store import (
    CIOutcomeProjection,
    PushProjection,
    SessionDossierProjection,
    SessionTimelineProjection,
)
from sediment_derive import (
    AttributionSource,
    MirrorManager,
    InferenceCall,
    derive_attributions,
    inference_fact_id,
    inference_gateway_provider,
    inference_model,
    inference_user_id,
    join_decisions_by_call_id,
)

from sediment_derive.repository_context import read_repository_context
from sediment_derive.repository_identity import (
    RepositoryIdentity,
    RepositoryIdentitySkipReason,
    IdentifiedRepositoryKey,
    CommitKey,
    RepositoryKey,
    repository_identity_of,
    repository_read_key,
    repository_sort_key,
)

from ..config import settings
from ..deps import get_store, verify_operator_token

_SESSION_DOSSIER_LIMIT = 500


router = APIRouter(tags=["query"])


_QUERY_REPRESENTATION_RESPONSES = {
    503: {
        "description": "Database unavailable, or work capacity or execution budget exceeded."
    },
    409: {
        "description": "Repository selection is ambiguous, complete evidence exceeds its bound, or query content contains a non-finite number. The response carries a closed detail.reason."
    },
}


def _query_response(
    value: Any,
    contract: Any,
    *,
    exclude_none: bool = False,
    max_bytes: int | None = None,
) -> Response:
    """Publish the shared lossless encoding with HTTP refusal translation."""
    try:
        content = encode_evidence_json(
            value, contract, exclude_none=exclude_none, max_bytes=max_bytes
        )
    except EvidenceReadError as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from None
    return Response(content=content, media_type="application/json")


class EvidenceReadRequest(BaseModel):
    """An exact versioned selection; organization comes from the deployment."""

    model_config = ConfigDict(extra="forbid")

    schema_version: EvidenceSchemaVersion
    session_id: NonEmptyId
    references: list[EvidenceReference] = Field(
        min_length=1, max_length=EVIDENCE_REFERENCE_LIMIT
    )

    @field_validator("references")
    @classmethod
    def distinct_references(
        cls, value: list[EvidenceReference]
    ) -> list[EvidenceReference]:
        validate_evidence_references(value)
        return value


_EVIDENCE_RESPONSES = {
    409: {
        "description": "Complete read declined: evidence_inventory_limit, evidence_source_limit, evidence_response_limit, evidence_unavailable, evidence_part_absent, or non_finite_number in detail.reason."
    },
    503: {
        "description": "Database unavailable, or shared work capacity or 30-second execution budget exceeded."
    },
}


async def _evidence_worker_response(
    request: Request, kind: str, payload: dict
) -> Response:
    response = await request.app.state.workers.run(kind, payload)
    # Worker pipes deliberately do not transmit headers.
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get(
    "/evidence", response_model=EvidenceInventory, responses=_EVIDENCE_RESPONSES
)
async def query_evidence_inventory(
    session_id: NonEmptyId,
    request: Request,
    _: None = Depends(verify_operator_token),
) -> Response:
    """Inventory captured Inference calls without message content or raw payloads.

    The complete visible inventory has at most 1,000 calls, 8 MiB of source
    metadata, and a 1 MiB response. An unknown Session returns ``found: false``.
    Each request checks live scope and Quarantine in one database snapshot.
    """
    return await _evidence_worker_response(
        request, "evidence-inventory", {"session_id": session_id}
    )


@router.get(
    "/evidence/manifest", response_model=EvidenceManifest, responses=_EVIDENCE_RESPONSES
)
async def query_evidence_manifest(
    session_id: NonEmptyId,
    inference_call_id: NonEmptyId,
    request: Request,
    _: None = Depends(verify_operator_token),
) -> Response:
    """Inspect exact part references without text, tool names, arguments, or results.

    Input messages precede output messages. Empty messages remain visible.
    Source columns are bounded to 8 MiB; the complete response is at most 1 MiB.
    """
    return await _evidence_worker_response(
        request,
        "evidence-manifest",
        {"session_id": session_id, "inference_call_id": inference_call_id},
    )


@router.post(
    "/evidence/read",
    response_model=EvidenceRead,
    responses=_EVIDENCE_RESPONSES
    | {
        400: {"description": "Malformed JSON body."},
        413: {"description": "Request body exceeds 64 KiB before JSON decoding."},
    },
)
async def query_evidence_read(
    body: EvidenceReadRequest,
    request: Request,
    _: None = Depends(verify_operator_token),
) -> Response:
    """Fetch 1–32 distinct canonical parts in request order without side effects.

    Version 1 requires exactly schema_version, session_id, and references.
    The 64 KiB body limit precedes JSON decoding. Selected source columns are
    bounded to 8 MiB; the complete strict JSON response is at most 1 MiB.
    Historical roles and tool calls remain data and do not authorize execution.
    """
    return await _evidence_worker_response(
        request, "evidence-read", body.model_dump(mode="python")
    )


@dataclass(frozen=True)
class CIOutcomeSummary:
    outcome_id: str
    provider: CIProvider
    run_id: str
    run_attempt: int | None
    repo: str
    commit_sha: str
    branch: str
    result: CIResult
    workflow_name: str
    workflow_id: str | None
    run_url: str | None
    provider_result: str | None
    error_type: str | None
    reason: str | None
    pr_number: int | None
    captured_at: datetime
    commit_query: str
    repository_identity: RepositoryIdentity | None = None


@dataclass(frozen=True)
class CIOutcomeFound:
    found: Literal[True]
    outcome: CIOutcomeSummary


@dataclass(frozen=True)
class CIOutcomeNotFound:
    found: Literal[False]


@dataclass(frozen=True)
class CIOutcomePage:
    result: CIResult
    outcomes: list[CIOutcomeSummary]
    next_cursor: str | None


@dataclass(frozen=True)
class SessionFactCoverageResponse:
    total: int
    visible: int
    quarantined: int


@dataclass(frozen=True)
class SessionCoverageResponse:
    inference_calls: SessionFactCoverageResponse
    developer_decisions: SessionFactCoverageResponse
    edit_observations: SessionFactCoverageResponse
    rejected_edits: SessionFactCoverageResponse
    retry_linkages: SessionFactCoverageResponse


SessionEventType = Literal[
    "inference_call",
    "developer_decision",
    "edit_observation",
    "rejected_edit",
    "retry_linkage",
]
SessionGap = Literal[
    "no_inference_calls_observed",
    "no_developer_decisions_observed",
    "no_edit_observations_observed",
    "no_rejected_edits_observed",
    "no_retry_linkages_observed",
    "session_commit_unobserved",
    "no_exact_push_receipts_observed",
    "no_ci_outcomes_observed",
]


@dataclass(frozen=True)
class SessionTimelineEvent:
    event_type: SessionEventType
    fact_id: str
    occurred_at: datetime
    gateway_provider: GatewayProvider | None = None
    model_provider: str | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    duration_ms: int | None = None
    model_call_id: str | None = None
    agent_harness: AgentHarness | None = None
    accepted: bool | None = None
    explicit: bool | None = None
    interaction_mode: InteractionMode | None = None
    has_file_path: bool | None = None
    call_id: str | None = None
    commit_sha: str | None = None
    external_lines_added_present: bool | None = None
    external_lines_removed_present: bool | None = None
    external_lines_added: int | None = None
    external_lines_removed: int | None = None
    tool_name: str | None = None
    rejected_call_id: str | None = None
    accepted_call_id: str | None = None


@dataclass(frozen=True)
class AttributedCommitSummary:
    """Observation-backed Session edge with compatibility Attribution fields.

    ``attribution_sources`` is empty. Similarity extrema and ``attributed_files``
    are unavailable (None), so the Session dossier omits them from JSON.
    """

    repo: str
    commit_sha: str
    attribution_sources: list[AttributionSource]
    minimum_similarity: float | None
    maximum_similarity: float | None
    attributed_files: int | None
    session_commit_observation_ids: tuple[NonEmptyId, ...] = ()
    repository_identity: RepositoryIdentity | None = None


@dataclass(frozen=True)
class PushSummary:
    push_id: str
    provider: ForgeProvider
    repo: str
    ref: str
    before_sha: str
    after_sha: str
    forced: bool
    captured_at: datetime
    match_kind: Literal["after_sha"] = "after_sha"
    repository_identity: RepositoryIdentity | None = None


@dataclass(frozen=True)
class SessionDossierFound:
    found: Literal[True]
    session_id: str
    first_observed_at: datetime
    last_observed_at: datetime
    coverage: SessionCoverageResponse
    omitted_events: int
    gaps: list[SessionGap]
    timeline: list[SessionTimelineEvent]
    attributed_commits: list[AttributedCommitSummary]
    pushes: list[PushSummary]
    ci_outcomes: list[SessionCIOutcomeSummary]
    session_commit_unobserved: int = 0
    repository_skipped: dict[RepositoryIdentitySkipReason, int] = field(
        default_factory=dict
    )


@dataclass(frozen=True)
class SessionDossierNotFound:
    found: Literal[False]


@dataclass(frozen=True)
class SessionCIOutcomeSummary:
    """CI navigation metadata without free-form provider content."""

    outcome_id: str
    provider: CIProvider
    run_id: str
    run_attempt: int | None
    repo: str
    commit_sha: str
    branch: str
    result: CIResult
    workflow_name: str
    workflow_id: str | None
    run_url: str | None
    provider_result: str | None
    error_type: str | None
    pr_number: int | None
    captured_at: datetime
    commit_query: str
    repository_identity: RepositoryIdentity | None = None


def _commit_query_link(outcome: CIOutcomeProjection, as_of: datetime | None) -> str:
    identity = repository_identity_of(outcome)
    params = {}
    if identity is not None:
        params.update(
            repository_provider=identity.provider.value,
            repository_host=identity.host,
            repository_id=identity.repository_id,
        )
    if as_of is not None:
        params["as_of"] = as_of.astimezone(UTC).isoformat()
    path = f"/query/commit/{outcome.commit_sha}"
    return f"{path}?{urlencode(params)}" if params else path


def _selector_identity(provider, host, repository_id) -> RepositoryIdentity | None:
    supplied = (provider is not None, host is not None, repository_id is not None)
    if any(supplied) and not all(supplied):
        raise HTTPException(
            status_code=422, detail="repository identity requires all three components"
        )
    return RepositoryIdentity(provider, host, repository_id) if all(supplied) else None


def _query_repository(context, repo, identity) -> RepositoryKey | None:
    try:
        return context.select_repository(
            settings.org_id, repo=repo, repository_identity=identity
        )
    except RepositoryReadAmbiguous:
        raise HTTPException(
            status_code=409, detail={"reason": "repository_selector_ambiguous"}
        ) from None
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="repository selector conflicts with declared evidence",
        ) from None


def _ci_summary(
    outcome: CIOutcomeProjection, *, as_of: datetime | None = None
) -> CIOutcomeSummary:
    return CIOutcomeSummary(
        outcome_id=outcome.outcome_id,
        provider=outcome.provider,
        run_id=outcome.run_id,
        run_attempt=outcome.run_attempt,
        repo=outcome.repo,
        commit_sha=outcome.commit_sha,
        branch=outcome.branch,
        result=outcome.result,
        workflow_name=outcome.workflow_name,
        workflow_id=outcome.workflow_id,
        run_url=outcome.run_url,
        provider_result=outcome.provider_result,
        error_type=outcome.error_type,
        reason=outcome.reason,
        pr_number=outcome.pr_number,
        captured_at=outcome.captured_at,
        commit_query=_commit_query_link(outcome, as_of),
        repository_identity=repository_identity_of(outcome),
    )


def _session_ci_summary(
    outcome: CIOutcomeProjection, *, as_of: datetime | None = None
) -> SessionCIOutcomeSummary:
    return SessionCIOutcomeSummary(
        outcome_id=outcome.outcome_id,
        provider=outcome.provider,
        run_id=outcome.run_id,
        run_attempt=outcome.run_attempt,
        repo=outcome.repo,
        commit_sha=outcome.commit_sha,
        branch=outcome.branch,
        result=outcome.result,
        workflow_name=outcome.workflow_name,
        workflow_id=outcome.workflow_id,
        run_url=outcome.run_url,
        provider_result=outcome.provider_result,
        error_type=outcome.error_type,
        pr_number=outcome.pr_number,
        captured_at=outcome.captured_at,
        commit_query=_commit_query_link(outcome, as_of),
        repository_identity=repository_identity_of(outcome),
    )


def _encode_ci_cursor(captured_at: datetime, outcome_id: str, filter_key: str) -> str:
    payload = json.dumps(
        [captured_at.isoformat(), outcome_id, filter_key], separators=(",", ":")
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_ci_cursor(cursor: str, filter_key: str) -> tuple[datetime, str]:
    try:
        padding = "=" * (-len(cursor) % 4)
        captured_at, outcome_id, cursor_filter_key = json.loads(
            base64.urlsafe_b64decode(cursor + padding).decode()
        )
        parsed = datetime.fromisoformat(captured_at)
        if parsed.tzinfo is None:
            raise ValueError
        outcome_id = TypeAdapter(NonEmptyId).validate_python(outcome_id)
        if cursor_filter_key != filter_key:
            raise HTTPException(
                status_code=422,
                detail="CI outcome cursor doesn't match filters",
            )
        return parsed, outcome_id
    except HTTPException:
        raise
    except (
        ValueError,
        TypeError,
        binascii.Error,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ):
        raise HTTPException(
            status_code=422, detail="invalid CI outcome cursor"
        ) from None


def _ci_filter_key(
    *,
    repository_key: RepositoryKey,
    org_id: str,
    as_of: datetime,
    quarantine_revision: int,
    captured_after: datetime,
    captured_before: datetime,
    result: CIResult,
    workflow_name: str | None,
    pr_number: int | None,
) -> str:
    return json.dumps(
        [
            org_id,
            repository_sort_key(repository_key),
            as_of.astimezone(UTC).isoformat(),
            quarantine_revision,
            captured_after.astimezone(UTC).isoformat(),
            captured_before.astimezone(UTC).isoformat(),
            result.value,
            workflow_name,
            pr_number,
        ],
        separators=(",", ":"),
    )


def _timeline_event(row: SessionTimelineProjection) -> SessionTimelineEvent:
    return SessionTimelineEvent(**row.__dict__)


def _fact_coverage(
    dossier: SessionDossierProjection, fact_type: str
) -> SessionFactCoverageResponse:
    coverage = dossier.coverage[fact_type]
    return SessionFactCoverageResponse(
        total=coverage.total,
        visible=coverage.visible,
        quarantined=coverage.quarantined,
    )


def _push_summary(push: PushProjection) -> PushSummary:
    return PushSummary(
        push_id=push.push_id,
        provider=ForgeProvider(push.provider),
        repo=push.repo,
        ref=push.ref,
        before_sha=push.before_sha,
        after_sha=push.after_sha,
        forced=push.forced,
        captured_at=push.captured_at,
        repository_identity=repository_identity_of(push),
    )


@router.get(
    "/ci/outcome",
    response_model=CIOutcomeFound | CIOutcomeNotFound,
    responses=_QUERY_REPRESENTATION_RESPONSES,
)
def query_ci_outcome(
    provider: CIProvider,
    run_id: NonEmptyId,
    run_attempt: int | None = Query(default=None, ge=1),
    repo: RequiredRepoSlug | None = None,
    repository_provider: ForgeProvider | None = None,
    repository_host: ForgeHost | None = None,
    repository_id: ProviderRepositoryId | None = None,
    as_of: AwareDatetime | None = None,
    _: None = Depends(verify_operator_token),
    store: FactStore = Depends(get_store),
) -> Response:
    """Return one exact CI run receipt; ambiguous forge namespaces require a selector."""
    identity = _selector_identity(repository_provider, repository_host, repository_id)
    try:
        with store.read_snapshot() as snapshot:
            context = read_repository_context(snapshot, settings.org_id, as_of=as_of)
            key = (
                _query_repository(context, repo, identity)
                if repo is not None or identity is not None
                else None
            )
            if key is None and (repo is not None or identity is not None):
                outcome = None
            else:
                outcome = snapshot.read_ci_outcome_by_run(
                    settings.org_id,
                    provider,
                    run_id,
                    run_attempt=run_attempt,
                    repository_key=repository_read_key(key)
                    if key is not None
                    else None,
                    captured_through=context.as_of,
                )
    except RepositoryReadAmbiguous:
        raise HTTPException(
            status_code=409, detail={"reason": "repository_selector_ambiguous"}
        ) from None
    except OperationalReportLimitExceeded:
        raise HTTPException(
            status_code=409, detail={"reason": "repository_evidence_limit"}
        ) from None
    if outcome is None:
        return _query_response(
            CIOutcomeNotFound(found=False), CIOutcomeFound | CIOutcomeNotFound
        )
    return _query_response(
        CIOutcomeFound(found=True, outcome=_ci_summary(outcome, as_of=context.as_of)),
        CIOutcomeFound | CIOutcomeNotFound,
    )


@router.get(
    "/ci/failures",
    response_model=CIOutcomePage,
    responses=_QUERY_REPRESENTATION_RESPONSES,
)
def query_ci_failures(
    captured_after: AwareDatetime,
    captured_before: AwareDatetime,
    repo: RequiredRepoSlug | None = None,
    repository_provider: ForgeProvider | None = None,
    repository_host: ForgeHost | None = None,
    repository_id: ProviderRepositoryId | None = None,
    as_of: AwareDatetime | None = None,
    result: CIResult = CIResult.FAILED,
    workflow_name: WorkflowName | None = None,
    pr_number: int | None = Query(default=None, ge=1),
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = None,
    _: None = Depends(verify_operator_token),
    store: FactStore = Depends(get_store),
) -> Response:
    """Page exact CI receipts under one repository identity and historical boundary.

    The default identity boundary is ``captured_before``. Capture bounds remain
    half-open; an explicit ``as_of`` adds an inclusive evidence ceiling. Cursors
    bind organization, qualified repository, boundary, filters, and quarantine
    revision. They don't preserve a database transaction between requests.
    """
    if captured_after >= captured_before:
        raise HTTPException(
            status_code=422, detail="captured_after must precede captured_before"
        )
    identity = _selector_identity(repository_provider, repository_host, repository_id)
    if repo is None and identity is None:
        raise HTTPException(
            status_code=422, detail="CI summaries require a repository selector"
        )
    boundary = as_of if as_of is not None else captured_before
    try:
        with store.read_snapshot() as snapshot:
            context = read_repository_context(snapshot, settings.org_id, as_of=boundary)
            key = _query_repository(context, repo, identity)
            if key is None:
                if cursor is not None:
                    raise HTTPException(
                        status_code=422,
                        detail="CI outcome cursor doesn't match filters",
                    )
                return _query_response(
                    CIOutcomePage(result=result, outcomes=[], next_cursor=None),
                    CIOutcomePage,
                )
            filter_key = _ci_filter_key(
                repository_key=key,
                org_id=settings.org_id,
                as_of=context.as_of,
                quarantine_revision=snapshot.quarantine_revision(settings.org_id),
                captured_after=captured_after,
                captured_before=captured_before,
                result=result,
                workflow_name=workflow_name,
                pr_number=pr_number,
            )
            before = (
                _decode_ci_cursor(cursor, filter_key) if cursor is not None else None
            )
            outcomes = snapshot.read_ci_outcome_summaries(
                settings.org_id,
                repository_key=repository_read_key(key),
                result=result,
                captured_between=(captured_after, captured_before),
                captured_through=context.as_of,
                before=before,
                workflow_name=workflow_name,
                pr_number=pr_number,
                limit=limit + 1,
            )
    except OperationalReportLimitExceeded:
        raise HTTPException(
            status_code=409, detail={"reason": "repository_evidence_limit"}
        ) from None
    page = outcomes[:limit]
    next_cursor = None
    if len(outcomes) > limit:
        last = page[-1]
        next_cursor = _encode_ci_cursor(last.captured_at, last.outcome_id, filter_key)
    return _query_response(
        CIOutcomePage(
            result=result,
            outcomes=[_ci_summary(outcome, as_of=context.as_of) for outcome in page],
            next_cursor=next_cursor,
        ),
        CIOutcomePage,
    )


@router.get(
    "/session/{session_id}",
    response_model=SessionDossierFound | SessionDossierNotFound,
    response_model_exclude_none=True,
    responses={
        503: {
            "description": "Database unavailable, or work capacity or execution budget exceeded."
        },
        409: {
            "description": "Session evidence exceeds the bounded read, or query content contains a non-finite number (detail.reason: non_finite_number)."
        },
    },
)
async def query_session_dossier(
    session_id: NonEmptyId,
    request: Request,
    _: None = Depends(verify_operator_token),
) -> Response:
    """Return one bounded, metadata-only Session evidence dossier.

    Observation-backed commit edges keep ``attribution_sources`` empty. Similarity
    extrema and ``attributed_files`` are unavailable and omitted from JSON.
    """
    return await request.app.state.workers.run("session", {"session_id": session_id})


def _run_session_query(
    session_id: str, store: FactStore
) -> SessionDossierFound | SessionDossierNotFound:
    with store.read_snapshot() as snapshot:
        dossier = snapshot.read_session_dossier(
            settings.org_id, session_id, limit=_SESSION_DOSSIER_LIMIT
        )
        if dossier is None:
            return SessionDossierNotFound(found=False)

        # One request boundary and complete context precede the Session filter.
        boundary = datetime.now(UTC)
        context = read_repository_context(snapshot, settings.org_id, as_of=boundary)
        observations = snapshot.read_session_commit_observations(
            settings.org_id,
            session_ids={session_id},
            as_of=boundary,
            limit=_SESSION_DOSSIER_LIMIT,
        )
        binding_result = bind_session_commit_keys_result(
            observations,
            settings.org_id,
            as_of=boundary,
            repository_context=context,
        )
        bindings = binding_result.bindings
        attributed_commits = [
            AttributedCommitSummary(
                repo=context.repo_for(commit.repository),
                commit_sha=commit.commit_sha,
                repository_identity=commit.repository.identity
                if isinstance(commit.repository, IdentifiedRepositoryKey)
                else None,
                attribution_sources=[],
                minimum_similarity=None,
                maximum_similarity=None,
                attributed_files=None,
                session_commit_observation_ids=tuple(
                    sorted({item.observation_id for item in facts})
                ),
            )
            for (commit, _session), facts in bindings.items()
        ]
        repository_commits = {
            (repository_read_key(commit.repository), commit.commit_sha)
            for commit, _session in bindings
        }
        pushes, ci_outcomes = snapshot.read_delivery_summaries(
            settings.org_id,
            repository_commits=repository_commits,
            captured_through=boundary,
            limit=_SESSION_DOSSIER_LIMIT,
        )
    gap_pairs: tuple[tuple[str, SessionGap], ...] = (
        ("inference_calls", "no_inference_calls_observed"),
        ("developer_decisions", "no_developer_decisions_observed"),
        ("edit_observations", "no_edit_observations_observed"),
        ("rejected_edits", "no_rejected_edits_observed"),
        ("retry_linkages", "no_retry_linkages_observed"),
    )
    gaps: list[SessionGap] = [
        gap for fact_type, gap in gap_pairs if dossier.coverage[fact_type].visible == 0
    ]
    if not attributed_commits:
        gaps.append("session_commit_unobserved")
    if attributed_commits and not pushes:
        gaps.append("no_exact_push_receipts_observed")
    if attributed_commits and not ci_outcomes:
        gaps.append("no_ci_outcomes_observed")
    return SessionDossierFound(
        found=True,
        session_id=dossier.session_id,
        first_observed_at=dossier.first_observed_at,
        last_observed_at=dossier.last_observed_at,
        coverage=SessionCoverageResponse(
            inference_calls=_fact_coverage(dossier, "inference_calls"),
            developer_decisions=_fact_coverage(dossier, "developer_decisions"),
            edit_observations=_fact_coverage(dossier, "edit_observations"),
            rejected_edits=_fact_coverage(dossier, "rejected_edits"),
            retry_linkages=_fact_coverage(dossier, "retry_linkages"),
        ),
        omitted_events=sum(
            coverage.quarantined for coverage in dossier.coverage.values()
        ),
        gaps=gaps,
        timeline=[_timeline_event(row) for row in dossier.timeline],
        attributed_commits=attributed_commits,
        session_commit_unobserved=int(not attributed_commits),
        repository_skipped=dict(binding_result.skipped),
        pushes=[_push_summary(push) for push in pushes],
        ci_outcomes=[
            _session_ci_summary(outcome, as_of=boundary) for outcome in ci_outcomes
        ],
    )


def _valid_sha(sha: str) -> str:
    """The shared CommitSha definition as a clean 422: full-length
    40/64 hex, lowercased — so an uppercase or SHA-256 query matches the
    normalized facts instead of missing or 422ing."""
    try:
        return normalize_commit_sha(sha)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get(
    "/commit/{sha}",
    response_model=dict[str, Any],
    responses=_QUERY_REPRESENTATION_RESPONSES,
)
async def query_commit(
    sha: str,
    request: Request,
    repo: RequiredRepoSlug | None = None,
    repository_provider: ForgeProvider | None = None,
    repository_host: ForgeHost | None = None,
    repository_id: ProviderRepositoryId | None = None,
    as_of: AwareDatetime | None = None,
    _: None = Depends(verify_operator_token),
) -> Response:
    """Investigate exact commit evidence in separate repository lifetimes.

    Captured observations establish Session associations. Call-to-file Attribution
    remains inferred. Optional repository selectors and an inclusive ``as_of``
    bound all supporting evidence; otherwise one request instant supplies the bound.
    A bounded child process owns the Derivation and its database connection.
    Capacity and execution deadlines return 503 after process cleanup.
    """
    commit_sha = _valid_sha(sha)
    identity = _selector_identity(repository_provider, repository_host, repository_id)
    return await request.app.state.workers.run(
        "commit",
        {
            "sha": commit_sha,
            "repo": repo,
            "repository_identity": asdict(identity) if identity is not None else None,
            "as_of": as_of.isoformat() if as_of is not None else None,
        },
    )


def _run_query(
    commit_sha: str,
    store: FactStore,
    *,
    repo: RequiredRepoSlug | None = None,
    repository_identity: RepositoryIdentity | None = None,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Read exact CI/Session evidence beside labeled inferred call diagnostics."""
    boundary = as_of if as_of is not None else datetime.now(UTC)
    with store.read_snapshot() as snapshot:
        context = read_repository_context(snapshot, settings.org_id, as_of=boundary)
        selected = repo is not None or repository_identity is not None
        selected_key = (
            _query_repository(context, repo, repository_identity) if selected else None
        )
        if selected and selected_key is None:
            return {"commit_sha": commit_sha, "attributed": False}
        observations = [
            item
            for item in snapshot.read_session_commit_observations(
                settings.org_id, as_of=boundary
            )
            if item.commit_sha == commit_sha
        ]
        binding_result = bind_session_commit_keys_result(
            observations,
            settings.org_id,
            as_of=boundary,
            repository_context=context,
        )
        bindings = {
            key: facts
            for key, facts in binding_result.bindings.items()
            if not selected or key[0].repository == selected_key
        }
        skipped = Counter(binding_result.skipped)
        matching = {}
        if settings.mirror_path:
            for item in derive_attributions(
                snapshot,
                MirrorManager(settings.mirror_path),
                settings.org_id,
                repository_context=context,
                as_of=boundary,
            ):
                if item.commit_sha != commit_sha:
                    continue
                resolution = context.resolve_reference(
                    item.org_id,
                    item.repo,
                    repository_identity=item.repository_identity,
                )
                if resolution.key is None:
                    skipped[resolution.reason] += 1
                    continue
                if not selected or resolution.key == selected_key:
                    matching.setdefault(resolution.key, []).append(item)
        commit_ci = {}
        unresolved_ci = []
        for item in snapshot.read_ci_outcomes(
            settings.org_id, captured_through=boundary
        ):
            if item.commit_sha != commit_sha:
                continue
            resolution = context.resolve_fact(item)
            if resolution.key is None:
                skipped[resolution.reason] += 1
                if not selected:
                    unresolved_ci.append(item.model_dump(mode="python"))
                continue
            if not selected or resolution.key == selected_key:
                commit_ci.setdefault(resolution.key, []).append(item)
        if not bindings and not matching and not commit_ci and not skipped:
            return {"commit_sha": commit_sha, "attributed": False}
        stored = [
            item
            for item in snapshot.read_inference_calls(settings.org_id)
            if item.observed_at.astimezone(UTC) <= boundary.astimezone(UTC)
        ]
        by_inference_call_id = {inference_fact_id(item): item for item in stored}
        joined = join_decisions_by_call_id(
            stored,
            snapshot.read_decisions(settings.org_id, captured_through=boundary),
        )
        repository_keys = (
            set(matching)
            | set(commit_ci)
            | {commit.repository for commit, _session in bindings}
        )
        repos = []
        for repository_key in sorted(repository_keys, key=repository_sort_key):
            best = {}
            for item in matching.get(repository_key, ()):
                score = (item.similarity_score, item.file_path)
                held = best.get(item.inference_call_id)
                if held is None or score > held[0]:
                    best[item.inference_call_id] = (
                        score,
                        item.session_id,
                        item.attribution_source.value,
                    )
            calls = _build_inference_calls(
                by_inference_call_id,
                [(cid, sid, source) for cid, (_, sid, source) in sorted(best.items())],
            )
            for call in calls:
                call["relationship"] = "inferred_call_to_file"
            observed_sessions = [
                {
                    "session_id": session_id,
                    "session_commit_observation_ids": sorted(
                        {item.observation_id for item in facts}
                    ),
                }
                for (commit, session_id), facts in bindings.items()
                if commit.repository == repository_key
            ]
            unobserved = {
                (CommitKey(repository_key, item.commit_sha), item.session_id)
                for item in matching.get(repository_key, ())
                if (CommitKey(repository_key, item.commit_sha), item.session_id)
                not in bindings
            }
            repos.append(
                {
                    "repo": context.repo_for(repository_key),
                    "repository_identity": asdict(repository_key.identity)
                    if isinstance(repository_key, IdentifiedRepositoryKey)
                    else None,
                    "observed_repo_slugs": list(
                        context.observed_repo_slugs(repository_key)
                    ),
                    "inference_calls": calls,
                    "decisions": sum(
                        len(ds) for cid, ds in joined.items() if cid in best
                    ),
                    "observed_sessions": observed_sessions,
                    "session_commit_unobserved": len(unobserved),
                    "ci_outcomes": [
                        item.model_dump(mode="python")
                        for item in commit_ci.get(repository_key, ())
                    ],
                }
            )
        result = {
            "commit_sha": commit_sha,
            "attributed": bool(bindings),
            "repos": repos,
        }
        if skipped:
            result["repository_skipped"] = dict(sorted(skipped.items()))
        if unresolved_ci:
            result["unresolved_ci_outcomes"] = unresolved_ci
        return result


def _build_inference_calls(
    by_inference_call_id: dict[str, InferenceCall],
    entries: Sequence[tuple[str, str, str]],
) -> list[dict[str, Any]]:
    """Resolve inference-call, session, and attribution-source triples into the
    endpoint's inference-call shape, reading model/provider/user_id from stored
    facts. ``session_id`` and ``attribution_source`` come from the attribution, not
    the fact row. The session_id on the fact is
    the session that produced the inference call (ADR 0002), and attribution
    names the attribution source (`git_notes` or `jaccard`)."""
    result: list[dict[str, Any]] = []
    for inference_call_id, session_id, attribution_source in entries:
        call = by_inference_call_id.get(inference_call_id)
        # An inference call may have been quarantined between attribution and
        # read: skip, don't 500.  The session_id from the attribution is
        # still trustable (it was read non-quarantined during derivation).
        if call is None:
            continue
        result.append(
            {
                "inference_call_id": inference_call_id,
                "session_id": session_id,
                "gateway_provider": inference_gateway_provider(call).value,
                "model_provider": call.model_provider,
                "model": inference_model(call),
                "user_id": inference_user_id(call),
                "attribution_source": attribution_source,
            }
        )
    return result
