# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sediment ingest API — FastAPI entry point."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sediment_core import FactStore, RepositoryIdentityConflict
from sediment_core.postgres_engine import (
    DatabaseOperationError,
    DatabaseURLValidationError,
    create_postgres_engine,
    database_operation_error,
    sanitized_database_target,
    verify_minimum_server_version,
    wait_for_database,
)
from sediment_core.postgres_migrations import (
    HEAD_REVISION,
    RevisionState,
    inspect_engine_revision,
)
from sediment_core.postgres_roles import validate_runtime_privileges
from sqlalchemy.exc import (
    DisconnectionError,
    InterfaceError,
    OperationalError,
    SQLAlchemyError,
    TimeoutError as SQLAlchemyTimeoutError,
)

from . import __version__
from .config import settings
from .deps import BodySizeLimitMiddleware
from .routers import ci_vendor, forge, gateway, otlp, query, reports, v1
from .workers import WorkerSupervisor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("sediment.api")

# Fail closed: refuse to construct the app with default/empty secrets.
# SEDIMENT_DEV_MODE=true opts out (local dev only). The message names each
# offending setting but never its value. A missing or invalid
# SEDIMENT_ORG_ID already failed the boot at Settings() construction.
_security_problems = settings.validate_production_security()
if _security_problems:
    _enumerated = "\n".join(f"  - {p}" for p in _security_problems)
    raise RuntimeError(
        "Refusing to start: insecure configuration detected.\n"
        f"{_enumerated}\n"
        "Set the named SEDIMENT_* environment variables to real values, or set "
        "SEDIMENT_DEV_MODE=true for local development only."
    )

# Advisory posture warnings (non-fatal): open mirror allowlist, etc. Logged,
# never raised — the operator may have chosen the posture deliberately.
for _warning in settings.config_warnings():
    logger.warning("config_warning: %s", _warning)


@asynccontextmanager
async def lifespan(application: FastAPI):
    """Own one PostgreSQL engine and verify the exact schema before traffic."""
    database_url = settings.database_url.get_secret_value()
    engine = None
    workers = None
    try:
        engine = create_postgres_engine(database_url, api_work=True)
        wait_for_database(engine)
        with engine.connect() as connection:
            verify_minimum_server_version(connection)
        inspection = inspect_engine_revision(engine)
        if inspection.state is not RevisionState.AT_HEAD:
            raise DatabaseOperationError(
                "verify database schema failed for "
                f"{sanitized_database_target(database_url)}: expected "
                f"{HEAD_REVISION}, found {inspection.state.value}"
            )
        if not settings.dev_mode:
            validate_runtime_privileges(engine)
        application.state.database_engine = engine
        application.state.fact_store = FactStore(engine)
        application.state.database_target = sanitized_database_target(database_url)
        workers = WorkerSupervisor()
        application.state.workers = workers
        yield
    except (DatabaseOperationError, DatabaseURLValidationError):
        raise
    except Exception:
        raise database_operation_error(
            "verify database startup", database_url
        ) from None
    finally:
        if workers is not None:
            await workers.close()
        if engine is not None:
            engine.dispose()


# enable_docs=False must close the whole schema surface: docs_url alone
# leaves /redoc and /openapi.json publicly served.
app = FastAPI(
    title="Sediment API",
    version=__version__,
    description="Self-hosted ingest: facts in through three doors (ADR 0001)",
    docs_url="/docs" if settings.enable_docs else None,
    redoc_url="/redoc" if settings.enable_docs else None,
    openapi_url="/openapi.json" if settings.enable_docs else None,
    lifespan=lifespan,
)

# Every door's body read is bounded — see the class docstring.
app.add_middleware(BodySizeLimitMiddleware)


@app.exception_handler(SQLAlchemyError)
async def database_error_response(
    request: Request, exc: SQLAlchemyError
) -> JSONResponse:
    """Return a stable response without exposing driver diagnostics."""
    target = getattr(request.app.state, "database_target", "configured database")
    if isinstance(
        exc,
        (
            DisconnectionError,
            InterfaceError,
            OperationalError,
            SQLAlchemyTimeoutError,
        ),
    ):
        logger.error("database_unavailable target=%s", target)
        return JSONResponse(
            status_code=503,
            content={
                "detail": {
                    "code": "database_unavailable",
                    "message": "PostgreSQL fact store unavailable",
                }
            },
        )
    logger.error("database_operation_failed target=%s", target)
    return JSONResponse(
        status_code=500,
        content={
            "detail": {
                "code": "database_operation_failed",
                "message": "PostgreSQL fact store operation failed",
            }
        },
    )


@app.exception_handler(RepositoryIdentityConflict)
async def repository_identity_conflict_response(
    request: Request, exc: RepositoryIdentityConflict
) -> JSONResponse:
    """Conflicting receipts cannot disclose retained evidence or acknowledge it."""
    logger.warning("repository_identity_conflict")
    return JSONResponse(
        status_code=409,
        content={
            "detail": {
                "code": "repository_identity_conflict",
                "message": "Repository identity conflicts with retained evidence",
            }
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_error_without_input(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """422 that names the failing field but never echoes the input.

    FastAPI's default handler serializes each error's ``input`` through
    ``jsonable_encoder``, which recurses until the stack gives out on a
    deeply nested body — a 6 KB request became a 500, and AGENTS.md §API
    Conventions says malformed bodies from authenticated callers are
    400/422, never 500. type/loc/msg are what a caller needs to fix the
    request; the offending input is theirs already.
    """
    if getattr(request.scope.get("route"), "endpoint", None) in {
        query.query_context_discover,
        query.query_context_selected,
    } and any(error.get("type") == "json_invalid" for error in exc.errors()):
        return JSONResponse(status_code=400, content={"detail": "Malformed JSON body"})
    detail = [
        {"type": e.get("type", ""), "loc": e.get("loc", ()), "msg": e.get("msg", "")}
        for e in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": detail})


app.include_router(gateway.router, prefix="/ingest")
# The GitHub webhook door is grouped by source: auth (HMAC vs bearer) and
# payload shape are per-source, and the plain names belong to the
# vendor-neutral doors — /ingest/ci is ci_vendor's, and /ingest/push stays
# free for a vendor-neutral push door later.
app.include_router(forge.router, prefix="/ingest/github")
app.include_router(ci_vendor.router, prefix="/ingest")
# OTLP receiver: path fixed by the exporter (<endpoint>/v1/logs), no prefix.
app.include_router(otlp.router)
app.include_router(query.router, prefix="/query")
# Read-only v1 surfaces: remote CLI probes and bounded operational reports.
app.include_router(v1.router, prefix="/v1")
app.include_router(reports.router, prefix="/v1/reports")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": app.version}
