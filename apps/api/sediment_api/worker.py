# SPDX-License-Identifier: AGPL-3.0-or-later
"""One fixed API operation in a disposable process; stdout is bounded IPC."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import logging
import sys
from typing import Any, Literal

from fastapi import HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import TypeAdapter
from sediment_core import (
    EVIDENCE_RESPONSE_BYTES_LIMIT,
    EvidenceInventory,
    EvidenceManifest,
    EvidenceRead,
    EvidenceReadError,
    FactStore,
    NonEmptyId,
    OperationalReportLimitExceeded,
    Push,
)
from sediment_core.models import AwareDatetime, CommitSha, RepoSlug, RequiredRepoSlug
from sediment_core.postgres_engine import create_postgres_engine
from sediment_derive import MirrorManager
from sediment_derive.context_retrieval import ContextRetrievalResult, retrieve_context
from sediment_derive.repository_identity import (
    REPOSITORY_IDENTITY_SKIP_REASONS,
    RepositoryIdentity,
)
from sqlalchemy.exc import (
    DisconnectionError,
    SQLAlchemyError,
    OperationalError,
    InterfaceError,
    TimeoutError as PoolTimeout,
)

from .config import settings
from .workers import MAX_REQUEST_BYTES
from .routers import forge, query, reports
from .services.operational_reports import (
    LifecycleReportRequest,
    generate_lifecycle_report,
    model_report_payload,
)


class _Diagnostics(logging.Handler):
    """Send closed counters, never raw errors or exception tracebacks, to stderr."""

    def emit(self, record: logging.LogRecord) -> None:
        if (
            record.msg
            == "attributions_derived repo=%s attributed=%d notes=%d jaccard=%d"
        ):
            repo, attributed, notes, jaccard = record.args
            value = {
                "event": "attributions_derived",
                "repo": repo,
                "attributed": attributed,
                "notes": notes,
                "jaccard": jaccard,
            }
        elif (
            record.msg
            == "session_commit_observations_captured repo=%s stored=%d duplicates=%d"
        ):
            repo, stored, duplicates = record.args
            value = {
                "event": "session_commit_observations_captured",
                "repo": repo,
                "stored": stored,
                "duplicates": duplicates,
            }
        elif (
            record.msg == "mirror_refresh_skipped repo=%s reason=%s count=1"
            and str(record.args[1]) in REPOSITORY_IDENTITY_SKIP_REASONS
        ):
            value = {
                "event": "repository_identity_declined",
                "repo": record.args[0],
                "reason": str(record.args[1]),
                "count": 1,
            }
        elif record.levelno >= logging.WARNING:
            value = {"event": "worker_operation_warning"}
        else:
            return
        sys.stderr.write(json.dumps(value, ensure_ascii=True) + "\n")
        sys.stderr.flush()


@dataclass(frozen=True)
class WorkerRequest:
    """Private subprocess envelope; captured Facts retain their canonical models."""

    kind: Literal[
        "commit",
        "session",
        "model-report",
        "lifecycle-report",
        "push",
        "rename",
        "evidence-inventory",
        "evidence-manifest",
        "evidence-read",
        "context-retrieve",
    ]
    payload: dict[str, Any]


def _dispatch(request: WorkerRequest, store: FactStore) -> Response:
    payload = request.payload
    match request.kind:
        case "context-retrieve":
            selection = query.ContextRetrievalRequest.model_validate(payload)
            session_id = TypeAdapter(NonEmptyId).validate_python(
                settings.retrieval_session_id
            )
            try:
                with store.read_snapshot() as snapshot:
                    source = snapshot.read_context_source(settings.org_id, session_id)
                    value = retrieve_context(
                        source, selection.query, selection.max_bytes
                    )
                    return query._query_response(
                        value, ContextRetrievalResult, max_bytes=selection.max_bytes
                    )
            except EvidenceReadError as exc:
                raise HTTPException(status_code=409, detail=exc.detail) from None
        case "evidence-inventory" | "evidence-manifest" | "evidence-read":
            session_id = TypeAdapter(NonEmptyId).validate_python(payload["session_id"])
            try:
                if request.kind == "evidence-inventory":
                    value = store.read_evidence_inventory(settings.org_id, session_id)
                    contract = EvidenceInventory
                elif request.kind == "evidence-manifest":
                    inference_call_id = TypeAdapter(NonEmptyId).validate_python(
                        payload["inference_call_id"]
                    )
                    value = store.read_evidence_manifest(
                        settings.org_id, session_id, inference_call_id
                    )
                    contract = EvidenceManifest
                else:
                    selection = query.EvidenceReadRequest.model_validate(payload)
                    value = store.read_evidence_parts(
                        settings.org_id, selection.session_id, selection.references
                    )
                    contract = EvidenceRead
            except EvidenceReadError as exc:
                raise HTTPException(status_code=409, detail=exc.detail) from None
            return query._query_response(
                value, contract, max_bytes=EVIDENCE_RESPONSE_BYTES_LIMIT
            )
        case "commit":
            sha = TypeAdapter(CommitSha).validate_python(payload["sha"])
            options = {}
            for name, shape in (
                ("repo", RequiredRepoSlug),
                ("repository_identity", RepositoryIdentity),
                ("as_of", AwareDatetime),
            ):
                if payload.get(name) is not None:
                    options[name] = TypeAdapter(shape).validate_python(payload[name])
            try:
                value = query._run_query(sha, store, **options)
            except OperationalReportLimitExceeded:
                raise HTTPException(
                    status_code=409, detail={"reason": "repository_evidence_limit"}
                ) from None
            return query._query_response(value, dict[str, Any])
        case "session":
            session_id = TypeAdapter(NonEmptyId).validate_python(payload["session_id"])
            try:
                value = query._run_session_query(session_id, store)
            except OperationalReportLimitExceeded:
                raise HTTPException(
                    status_code=409, detail={"reason": "repository_evidence_limit"}
                ) from None
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from None
            return query._query_response(
                value,
                query.SessionDossierFound | query.SessionDossierNotFound,
                exclude_none=True,
            )
        case "model-report" | "lifecycle-report":
            bounds = reports.ReportScopeResponse.model_validate(payload)
            scope = reports._build_scope(
                bounds.cohort_start, bounds.cohort_end, bounds.as_of
            )
            mirrors = MirrorManager(settings.mirror_path or "./mirrors")
            try:
                if request.kind == "model-report":
                    result = model_report_payload(
                        store, mirrors, settings.org_id, scope
                    )
                else:
                    result = asdict(
                        generate_lifecycle_report(
                            store,
                            mirrors,
                            LifecycleReportRequest(settings.org_id, scope),
                        ).report
                    )
            except OperationalReportLimitExceeded:
                raise HTTPException(
                    status_code=409, detail="report evidence exceeds the fixed limit"
                ) from None
            return reports._report_response(
                reports.ReportEnvelope(scope=bounds, report=result)
            )
        case "push":
            push = Push.model_validate(payload["push"])
            if push.org_id != settings.org_id:
                raise ValueError("worker organization mismatch")
            forge._refresh_mirror(
                push,
                store,
                fetch_repo=TypeAdapter(RepoSlug | None).validate_python(
                    payload.get("fetch_repo")
                ),
                fetch_clone_url=TypeAdapter(str | None).validate_python(
                    payload.get("fetch_clone_url")
                ),
            )
            return JSONResponse({"completed": True})
        case "rename":
            old_repo = TypeAdapter(RepoSlug).validate_python(payload["old_repo"])
            new_repo = TypeAdapter(RepoSlug).validate_python(payload["new_repo"])
            if not old_repo or not new_repo or not settings.mirror_path:
                raise ValueError("mirror rename requires configured repository paths")
            moved = MirrorManager(settings.mirror_path).rename(
                settings.org_id, old_repo, new_repo
            )
            return JSONResponse({"renamed": moved})


def execute(request: WorkerRequest) -> Response:
    engine = None
    try:
        engine = create_postgres_engine(
            settings.database_url.get_secret_value(),
            api_work=True,
            single_connection=True,
        )
        return _dispatch(request, FactStore(engine))
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    except (DisconnectionError, OperationalError, InterfaceError, PoolTimeout):
        return JSONResponse(
            status_code=503,
            content={
                "detail": {
                    "code": "database_unavailable",
                    "message": "PostgreSQL fact store unavailable",
                }
            },
        )
    except SQLAlchemyError:
        return JSONResponse(
            status_code=500,
            content={
                "detail": {
                    "code": "database_operation_failed",
                    "message": "PostgreSQL fact store operation failed",
                }
            },
        )
    except Exception:
        return JSONResponse(status_code=500, content={"detail": "work failed"})
    finally:
        if engine is not None:
            engine.dispose()


def main() -> int:
    logging.basicConfig(level=logging.INFO, handlers=[_Diagnostics()], force=True)
    try:
        data = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(data) > MAX_REQUEST_BYTES:
            return 1
        envelope = json.loads(data)
        if not isinstance(envelope, dict) or set(envelope) != {"kind", "payload"}:
            return 1
        request = TypeAdapter(WorkerRequest).validate_python(envelope)
        response = execute(request)
        sys.stdout.buffer.write(str(response.status_code).encode() + b"\n")
        sys.stdout.buffer.write(response.body)
        sys.stdout.buffer.flush()
        return 0
    except Exception:
        # No traceback or request/driver diagnostics cross the process boundary.
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
