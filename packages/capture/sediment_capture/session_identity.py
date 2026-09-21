# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Session / user identity extraction for forwarded LiteLLM gateway payloads.

These heuristics run server-side: ``litellm/sediment_callback.py`` forwards
LiteLLM's
``StandardLoggingPayload`` verbatim, and every identity source below reads
fields inside that dict. LiteLLM-SLO-specific — never run these heuristics
over another provider's payload.

The session sources, in resolution order (the shapes below are the
wire-verified forms):

1. Explicit request metadata ``session_id``. Clients pass
   ``extra_body={"metadata": {"session_id": ..., "user_id": ...}}``; on the
   SLO it survives as ``metadata.requester_metadata`` (``_meta_value`` digs
   into that nesting).
2. Claude Code routed via ``ANTHROPIC_BASE_URL`` speaks the raw Anthropic
   ``/v1/messages`` API and cannot attach LiteLLM metadata, but its
   Anthropic-native ``metadata.user_id`` carries either an API-key JSON object
   with ``session_id`` or the older
   ``user_<hash>_account_<uuid>_session_<session-uuid>`` string. LiteLLM lifts
   that value into the SLO's ``end_user`` / ``requester_metadata.user_id``.
3. Codex CLI speaks ``/v1/responses`` and cannot attach metadata either, but
   every request carries an ``x-codex-turn-metadata`` header whose JSON names
   the session id, and LiteLLM preserves client ``x-*`` headers on
   ``metadata.requester_custom_headers`` (LiteLLM 1.91.0,
   codex-cli 0.142.5). The id must look like a UUID so a junk header can't
   smuggle in a fake session (ADR 0002).
4. The sediment-pi shim stamps an ``x-sediment-session`` header (the pi
   session uuid, same UUID gate) alongside the fleet models.json's static
   ``x-sediment-agent`` header (``docs`` → user_id ``agent:docs``); the
   agent header only fills a user_id every richer source left blank.

No source yields a session id → ``None`` — absent, never guessed (ADR 0002:
placeholder session ids are banned).

Under API-key auth Claude Code's identity string is instead a JSON blob —
``{"device_id": ..., "account_uuid": "", "session_id": ...}``. Forwarding it
verbatim puts raw JSON in every stored inference call, so ``_identity_label``
condenses it to a short readable label (truncated ``device_id``, the one
field that is both populated and stable per developer). Session extraction
is untouched — it runs first, over the identity string as it arrived. On
source 2's string form the per-session suffix is stripped instead, so
user_id stays stable across sessions.

Deliberate delta from the callback: the ``kwargs["user"]`` fallback is
dropped. LiteLLM lifts the request ``user`` into the SLO's ``end_user``,
which the order above already covers.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("sediment.capture.session_identity")


def _identity_text(value: Any) -> str:
    """Identity carriers contain strings; malformed shapes supply no evidence."""
    if isinstance(value, str):
        return value.strip()
    if value is not None:
        logger.warning(
            "gateway_identity_invalid_shape",
            extra={"value_type": type(value).__name__},
        )
    return ""


# Claude Code's Anthropic metadata.user_id ends in the agent session uuid:
# user_<hash>_account_<uuid>_session_<uuid>.
_SESSION_SUFFIX = re.compile(
    r"(?:^|_)session_([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)

# A bare UUID — the shape of a Codex session id.
_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


@dataclass(frozen=True)
class SessionIdentity:
    """Resolved identity for one gateway inference call. ``source`` names the
    winning session source for log lines: ``metadata`` (explicit request
    metadata), ``identity_json``, ``identity_suffix``, ``codex_header``, or
    ``sediment_header``."""

    session_id: str
    user_id: str | None
    source: str


def _session_from_identity(*candidates: Any) -> str:
    """The trailing ``session_<uuid>`` from the first candidate carrying one,
    else ''. Anchored to the end of the string so an id that merely mentions
    'session' somewhere can't smuggle in a fake session."""
    for candidate in candidates:
        match = _SESSION_SUFFIX.search(_identity_text(candidate))
        if match:
            return match.group(1)
    return ""


def _session_from_identity_json(*candidates: Any) -> str:
    """The UUID ``session_id`` from the first JSON-object identity carrying one.

    API-key-authenticated Claude Code uses this form. Malformed JSON, a
    non-object value, or a non-UUID id contributes nothing.
    """
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        try:
            identity = json.loads(candidate)
        except ValueError:
            continue
        if not isinstance(identity, dict):
            continue
        session_id = _identity_text(identity.get("session_id"))
        if _UUID.match(session_id):
            return session_id
    return ""


def _custom_headers(metas: tuple[Any, ...]) -> Iterator[dict[str, Any]]:
    """Each metadata dict's ``requester_custom_headers``, skipping anything
    the wrong shape — the header sources below take untrusted payloads."""
    for meta in metas:
        if isinstance(meta, dict):
            headers = meta.get("requester_custom_headers")
            if isinstance(headers, dict):
                yield headers


def _session_from_codex_headers(*metas: Any) -> str:
    """The ``session_id`` from an ``x-codex-turn-metadata`` header on the first
    metadata dict carrying one, else '' (see module doc). Fail-soft: a
    missing/malformed header or non-UUID id contributes nothing."""
    for headers in _custom_headers(metas):
        try:
            turn_meta = json.loads(headers.get("x-codex-turn-metadata") or "")
        except (TypeError, ValueError):
            continue
        if not isinstance(turn_meta, dict):
            continue
        session = _identity_text(turn_meta.get("session_id"))
        if _UUID.match(session):
            return session
    return ""


def _sediment_header(metas: tuple[Any, ...], name: str) -> str:
    """The first non-empty ``name`` value across metadata dicts'
    ``requester_custom_headers``, else ''. Fail-soft: malformed shapes
    contribute nothing."""
    for headers in _custom_headers(metas):
        value = _identity_text(headers.get(name))
        if value:
            return value
    return ""


def _session_from_sediment_headers(*metas: Any) -> str:
    """The pi session uuid from the shim-stamped ``x-sediment-session``
    header (see module doc). Same UUID gate as the Codex path — a junk header
    must not smuggle in a fake session (ADR 0002)."""
    session = _sediment_header(metas, "x-sediment-session")
    return session if _UUID.match(session) else ""


def _agent_from_sediment_headers(*metas: Any) -> str:
    """``agent:<name>`` from the fleet's static ``x-sediment-agent`` header,
    else ''. Only fills a user_id every richer source left blank."""
    agent = _sediment_header(metas, "x-sediment-agent")
    return f"agent:{agent}" if agent else ""


# Under API-key auth Claude Code sends metadata.user_id as a JSON blob —
# {"device_id": ..., "account_uuid": "", "session_id": ...} — rather than the
# user_<hash>_account_<uuid>_session_<uuid> string. Stored raw it is neither
# readable nor a stable key, so we condense it to a short label. The fields
# are tried in this order; session_id is deliberately absent, it changes
# every session and would never be a stable user key.
_IDENTITY_LABEL_FIELDS = (("device_id", "device"), ("account_uuid", "account"))
# Enough of the field to stay unique across a tenant's devices, short enough
# to read in a log line or a fact row.
_IDENTITY_LABEL_LEN = 12


def _identity_label(identity: str) -> str:
    """A short readable label for an identity string that is really a JSON
    blob (see module doc), else the string unchanged. Fail-soft: a blob with
    no usable field yields '' and falls back to the caller's default;
    anything that is not a JSON object is left alone, so the identity-string
    form still reaches the session-suffix strip untouched."""
    try:
        parsed = json.loads(identity)
    except (TypeError, ValueError):
        return identity
    if not isinstance(parsed, dict):
        return identity
    for field, prefix in _IDENTITY_LABEL_FIELDS:
        value = _identity_text(parsed.get(field))
        if value:
            return f"{prefix}_{value[:_IDENTITY_LABEL_LEN]}"
    return ""


def _meta_value(meta: Any, key: str, default: str) -> str:
    """Pull an identity field from request metadata, tolerant of nesting.

    Non-string and whitespace-only values are absent: padding is transport noise
    (NonEmptyId's rule), and a padded id surfacing from here would raise at
    fact construction — a 500, where the callback-era path 422'd at the
    envelope. Deliberate delta from the callback, which returned the raw
    string."""
    if not isinstance(meta, dict):
        return default
    value = _identity_text(meta.get(key))
    if value:
        return value
    requester = meta.get("requester_metadata")
    if isinstance(requester, dict):
        value = _identity_text(requester.get(key))
        if value:
            return value
    return default


def resolve_identity(payload: dict) -> SessionIdentity | None:
    """Session/user identity from a forwarded LiteLLM payload, or ``None``
    when no source yields a session id (module doc names the order)."""
    if not isinstance(payload, dict):
        return None
    meta = payload.get("metadata")
    # Stripped for the same reason as _meta_value: a whitespace-only
    # end_user must fall through the user_id chain, never ship as an id.
    end_user = _identity_text(payload.get("end_user"))

    session_id = _meta_value(meta, "session_id", "")
    source = "metadata"
    if not session_id:
        session_id = _session_from_identity_json(
            end_user, _meta_value(meta, "user_id", "")
        )
        source = "identity_json"
    if not session_id:
        session_id = _session_from_identity(end_user, _meta_value(meta, "user_id", ""))
        source = "identity_suffix"
    if not session_id:
        session_id = _session_from_codex_headers(meta)
        source = "codex_header"
    if not session_id:
        session_id = _session_from_sediment_headers(meta)
        source = "sediment_header"
    if not session_id:
        return None

    user_id = (
        _meta_value(meta, "user_id", "")
        or end_user
        or (_agent_from_sediment_headers(meta))
    )
    # Under API-key auth the identity is a JSON blob rather than the suffixed
    # string; condense it to a readable label. Otherwise the identity string
    # embeds the per-session uuid — strip it so user_id stays stable for the
    # same developer across sessions.
    # .strip(): the suffix strip can leave trailing separators/padding, and
    # user_id feeds NonEmptyId at fact construction.
    user_id = _SESSION_SUFFIX.sub("", _identity_label(user_id)).strip() or None

    return SessionIdentity(session_id=session_id, user_id=user_id, source=source)
