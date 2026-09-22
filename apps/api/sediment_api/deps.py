# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Shared API plumbing: auth at the two door types and the fact store.

The bearer check and the GitHub "read raw body, verify HMAC, parse JSON"
step live here once instead of being copied into each router. No org
derivation lives here or anywhere: every route stamps facts with the
configured ``settings.org_id``. Query parameters and payload owners cannot
override deployment tenancy.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from typing import Literal
from typing import Any

from fastapi import Depends, Header, HTTPException, Request
from sediment_capture import verify_signature
from sediment_core import EVIDENCE_REQUEST_BYTES_LIMIT, FactStore, NonEmptyId

from .config import settings

# The app-wide request-body ceiling, enforced for every door by
# BodySizeLimitMiddleware. 25 MB because GitHub caps webhook payloads
# there and refuses to deliver anything larger, and no other door has a
# legitimate payload anywhere near it. It is a ceiling on a pre-auth
# allocation, not a tuning parameter — FastAPI reads an envelope route's
# body before its auth dependency runs, so every door's read is pre-auth.
# ponytail: fixed constant; make it a setting only if a non-GitHub forge needs it
MAX_BODY_BYTES = 25 * 1024 * 1024
CONTEXT_REQUEST_BYTES_LIMIT = 16 * 1024


class BodySizeLimitMiddleware:
    """Bound every request-body read at ``MAX_BODY_BYTES`` (413 past it).

    Pure ASGI, wrapping ``receive``: the running byte total is the only
    trustworthy number (Content-Length is absent under chunked encoding and
    attacker-supplied otherwise), and raising during the read stops the
    allocation at the ceiling instead of after it. The HTTPException
    surfaces inside whichever handler frame awaited the body — FastAPI's
    envelope read, ``request.json()``, or ``_read_body_capped``'s stream
    loop — where the exception middleware turns it into the 413 response.

    One guard for every door, including routers added later — the per-door
    alternative is how the envelope doors (gateway, vendor CI) ended up
    uncapped while the webhook door was bounded.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = MAX_BODY_BYTES
        path = scope["path"]
        root_path = scope.get("root_path", "")
        if root_path and path.startswith(root_path + "/"):
            path = path[len(root_path) :]
        if scope["method"] == "POST" and path == "/query/evidence/read":
            # FastAPI parses model envelopes before running dependencies. Keep
            # this operation's smaller bound ahead of that allocation too.
            limit = min(limit, EVIDENCE_REQUEST_BYTES_LIMIT)
        elif scope["method"] == "POST" and path in {
            "/query/context",
            "/query/context/discover",
            "/query/context/selected",
        }:
            limit = min(limit, CONTEXT_REQUEST_BYTES_LIMIT)
        received = 0

        async def capped_receive() -> Any:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                # Module-global lookup on purpose: tests lower the cap by
                # patching MAX_BODY_BYTES after the app is constructed.
                if received > limit:
                    raise HTTPException(
                        status_code=413, detail="request body too large"
                    )
            return message

        async def context_send(message: Any) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() != b"cache-control"
                ] + [(b"cache-control", b"no-store")]
            await send(message)

        await self.app(
            scope,
            capped_receive,
            context_send
            if path in {"/query/context/discover", "/query/context/selected"}
            else send,
        )


@dataclass(frozen=True)
class CredentialIdentity:
    """Configured authority only; never a tenant or captured developer identity."""

    authority: Literal["ingest", "operator", "retrieval"]
    client_id: str


def verify_token(authorization: str | None = Header(None)) -> CredentialIdentity:
    """Authenticate configured authorities without logging credential material."""
    scheme, _, credential = (authorization or "").partition(" ")
    credential = credential.strip()
    if scheme.lower() != "bearer" or not credential:
        raise HTTPException(status_code=401, detail="Invalid token")
    supplied = credential.encode()
    identity = None
    candidates = [
        (
            settings.operator_token.get_secret_value(),
            CredentialIdentity("operator", "operator"),
        ),
        (
            settings.retrieval_token.get_secret_value()
            if settings.retrieval_token is not None
            else "",
            CredentialIdentity("retrieval", "retrieval"),
        ),
        (settings.api_bearer_token, CredentialIdentity("ingest", "legacy")),
        *(
            (token.get_secret_value(), CredentialIdentity("ingest", client_id))
            for client_id, token in settings.ingest_tokens.items()
        ),
    ]
    for token, candidate in candidates:
        if token and secrets.compare_digest(supplied, token.encode()):
            identity = candidate
    if identity is None:
        raise HTTPException(status_code=401, detail="Invalid token")
    return identity


def verify_ingest_token(
    identity: CredentialIdentity = Depends(verify_token),
) -> CredentialIdentity:
    """Capture accepts ingest clients and explicit operator demonstrations."""
    if identity.authority not in {"ingest", "operator"}:
        raise HTTPException(status_code=403, detail="Ingest authority required")
    return identity


def verify_operator_token(
    identity: CredentialIdentity = Depends(verify_token),
) -> CredentialIdentity:
    """Read and inspection routes require operator authority."""
    if identity.authority != "operator":
        raise HTTPException(status_code=403, detail="Operator authority required")
    return identity


def verify_retrieval_token(
    identity: CredentialIdentity = Depends(verify_token),
) -> CredentialIdentity:
    """Read the deployment's fixed source without broadening operator reads."""
    if identity.authority not in {"retrieval", "operator"}:
        raise HTTPException(status_code=403, detail="Retrieval authority required")
    if settings.retrieval_session_id is None:
        raise HTTPException(status_code=404, detail="Context retrieval is disabled")
    return identity


def verify_context_grant_token(
    identity: CredentialIdentity = Depends(verify_token),
) -> CredentialIdentity:
    """Discovery and selection share the configured Session grant."""
    if identity.authority not in {"retrieval", "operator"}:
        raise HTTPException(status_code=403, detail="Retrieval authority required")
    if not settings.context_session_ids:
        raise HTTPException(status_code=404, detail="Context retrieval is disabled")
    return identity


def require_context_session(session_id: NonEmptyId) -> None:
    """Refuse before any storage lookup, independent of Session existence."""
    if session_id not in settings.context_session_ids:
        raise HTTPException(
            status_code=403, detail="Session is outside the context grant"
        )


def get_store(request: Request) -> FactStore:
    """Borrow the lifespan-owned PostgreSQL fact store for one request."""
    return request.app.state.fact_store


async def _read_body_capped(request: Request, limit: int) -> bytearray:
    """Buffer the request body, refusing anything over ``limit`` bytes (413).

    ``request.body()`` reads the stream to completion with no cap, and the
    HMAC check below can only run *after* the body is in hand — so an
    unauthenticated caller would otherwise choose how much memory this
    allocates. Streaming stops at the ceiling instead of after it.

    Content-Length is deliberately not consulted: it is absent under chunked
    encoding and attacker-supplied otherwise. The running total is the only
    number that can be trusted.

    Accumulating into one ``bytearray`` rather than a chunk list plus
    ``b"".join`` keeps peak memory at the ceiling instead of twice it: the
    join holds both the chunks and the joined copy alive at once (measured
    2.00x vs 1.01x of the limit). Halving the cap would not substitute —
    the doubling is what makes the ceiling unenforceable. The bytearray is
    returned as-is for the same reason; ``bytes()`` here would reintroduce
    the copy. Both consumers (``verify_signature``, ``json.loads``) take
    bytes-like and produce identical results either way.
    """
    buf = bytearray()
    async for chunk in request.stream():
        if len(buf) + len(chunk) > limit:
            raise HTTPException(status_code=413, detail="request body too large")
        buf.extend(chunk)
    return buf


async def read_verified_webhook(
    request: Request, signature: str | None
) -> dict[str, Any]:
    """Read the raw body, verify its GitHub HMAC signature, return parsed JSON.

    Raises 401 if the signature is missing or invalid (an unsigned request
    is unauthenticated, not a schema error). This is the one place webhook
    auth is enforced for every GitHub webhook endpoint — and, because the
    body must be read before it can be authenticated, the one place the
    pre-auth read is bounded.
    """
    body_bytes = await _read_body_capped(request, MAX_BODY_BYTES)
    if not verify_signature(
        body_bytes, signature or "", settings.github_webhook_secret
    ):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    try:
        payload = json.loads(body_bytes)
    except (ValueError, RecursionError):
        # Fail-soft on any malformed body from an authenticated caller:
        # JSONDecodeError and UnicodeDecodeError (non-UTF-8 bytes) are both
        # ValueError; deeply nested JSON raises RecursionError. None of them
        # may 500 — they're a bad request (400 below), not a server fault.
        payload = None
    if not isinstance(payload, dict):
        # Signature-valid but not a JSON object: an authenticated caller sent
        # a malformed body. 400, not 500 — and not 401, the HMAC passed.
        raise HTTPException(status_code=400, detail="invalid JSON body")
    return payload
