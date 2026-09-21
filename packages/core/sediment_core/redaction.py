# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic basic credential redaction for facts before storage."""

from __future__ import annotations

import re
from collections import Counter
from enum import StrEnum
from typing import Any, Callable, TypeVar, cast

from .models import (
    CIOutcome,
    CI_REASON_MAX_LENGTH,
    DeveloperDecision,
    EditObservation,
    InferenceCall,
    InferenceMessage,
    RejectedEdit,
    RetryLinkage,
    ReasoningPart,
    TextPart,
    ToolCallPart,
    ToolCallResponsePart,
)

REDACTION_MARKER = "[REDACTED_CREDENTIAL]"


class RedactionReason(StrEnum):
    """Closed vocabulary for counted credential replacements."""

    STRUCTURED_CREDENTIAL = "structured_credential"
    AUTHORIZATION_HEADER = "authorization_header"
    API_KEY_ASSIGNMENT = "api_key_assignment"
    BEARER_TOKEN = "bearer_token"
    PROVIDER_KEY = "provider_key"


RedactableFact = (
    InferenceCall
    | DeveloperDecision
    | EditObservation
    | RejectedEdit
    | RetryLinkage
    | CIOutcome
)
FactT = TypeVar("FactT", bound=RedactableFact)

_BEARER = re.compile(
    r"(?i)(?P<prefix>\bBearer[ \t]+)"
    r"(?P<credential>[A-Za-z0-9\-._~+/]{16,}=*)"
)
_AUTHORIZATION_HEADER = re.compile(
    r"(?i)(?P<prefix>\bAuthorization\s*[:=]\s*Bearer[ \t]+)"
    r"(?P<credential>[A-Za-z0-9\-._~+/]{16,}=*)"
)
_QUOTED_STRUCTURED_ASSIGNMENT = re.compile(
    r"(?P<prefix>(?P<label_quote>['\"])(?P<key>[^'\"]+)"
    r"(?P=label_quote)\s*:\s*)"
    r"(?P<quote>['\"])(?P<credential>(?:\\.|(?!(?P=quote)).)+)"
    r"(?:(?P<closing_quote>(?P=quote))|\Z)"
)
_API_KEY_ASSIGNMENT = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(?P<prefix>(?P<label_quote>['\"]?)"
    r"(?:[A-Za-z0-9]+[_-])*api[_ -]?key(?P=label_quote)\s*[:=]\s*)"
    r"(?:"
    r"(?P<quote>['\"])(?P<quoted>[^'\"\s]{8,})"
    r"(?:(?P<closing_quote>(?P=quote))|\Z)"
    r"|(?P<bare>[A-Za-z0-9\-._~+/]{16,}=*)"
    r")"
)
_PROVIDER_KEY = re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{16,}(?![A-Za-z0-9_-])")


def _credential_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    normalized = key.casefold().replace("-", "_")
    return normalized in {
        "authorization",
        "proxy_authorization",
        "x_api_key",
        "api_key",
        "apikey",
    } or normalized.endswith("_api_key")


def _replace(
    pattern: re.Pattern[str],
    value: str,
    reason: RedactionReason,
    counts: Counter[RedactionReason],
    replacement: Callable[[re.Match[str]], str],
) -> str:
    def apply(match: re.Match[str]) -> str:
        counts[reason] += 1
        return replacement(match)

    return pattern.sub(apply, value)


def _redact_text(value: str, counts: Counter[RedactionReason]) -> str:
    value = _replace(
        _AUTHORIZATION_HEADER,
        value,
        RedactionReason.AUTHORIZATION_HEADER,
        counts,
        lambda match: match.group("prefix") + REDACTION_MARKER,
    )

    def redact_quoted_assignment(match: re.Match[str]) -> str:
        key = match.group("key")
        credential = match.group("credential")
        if not _credential_key(key) or credential == REDACTION_MARKER:
            return match.group(0)
        counts[RedactionReason.STRUCTURED_CREDENTIAL] += 1
        return (
            match.group("prefix")
            + match.group("quote")
            + REDACTION_MARKER
            + (match.group("closing_quote") or "")
        )

    value = _QUOTED_STRUCTURED_ASSIGNMENT.sub(redact_quoted_assignment, value)

    def redact_assignment(match: re.Match[str]) -> str:
        credential = match.group("quoted") or match.group("bare")
        if credential == REDACTION_MARKER:
            return match.group(0)
        counts[RedactionReason.API_KEY_ASSIGNMENT] += 1
        quote = match.group("quote") or ""
        closing_quote = match.group("closing_quote") or ""
        return match.group("prefix") + quote + REDACTION_MARKER + closing_quote

    value = _API_KEY_ASSIGNMENT.sub(redact_assignment, value)
    value = _replace(
        _BEARER,
        value,
        RedactionReason.BEARER_TOKEN,
        counts,
        lambda match: match.group("prefix") + REDACTION_MARKER,
    )
    return _replace(
        _PROVIDER_KEY,
        value,
        RedactionReason.PROVIDER_KEY,
        counts,
        lambda _match: REDACTION_MARKER,
    )


def _redact_structured(value: str, counts: Counter[RedactionReason]) -> str:
    if value in {REDACTION_MARKER, f"Bearer {REDACTION_MARKER}"}:
        return value
    bearer = _BEARER.fullmatch(value)
    counts[RedactionReason.STRUCTURED_CREDENTIAL] += 1
    if bearer is not None:
        return bearer.group("prefix") + REDACTION_MARKER
    return REDACTION_MARKER


def _redact_json(value: Any, counts: Counter[RedactionReason]) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                _redact_structured(item, counts)
                if _credential_key(key) and isinstance(item, str)
                else _redact_json(item, counts)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_json(item, counts) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_json(item, counts) for item in value)
    if isinstance(value, str):
        return _redact_text(value, counts)
    return value


def _structured_credentials(value: Any) -> set[str]:
    credentials: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if _credential_key(key) and isinstance(item, str):
                if item in {REDACTION_MARKER, f"Bearer {REDACTION_MARKER}"}:
                    continue
                bearer = _BEARER.fullmatch(item)
                credential = bearer.group("credential") if bearer is not None else item
                if credential:
                    credentials.add(credential)
            else:
                credentials.update(_structured_credentials(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            credentials.update(_structured_credentials(item))
    return credentials


def _redact_known_credentials(
    value: str,
    credentials: set[str],
    counts: Counter[RedactionReason],
) -> str:
    ordered = sorted(credentials, key=lambda item: (-len(item), item))
    if not ordered:
        return value
    alternatives = "|".join(re.escape(credential) for credential in ordered)
    pattern = re.compile(rf"^(?:{alternatives})$", re.MULTILINE)
    return _replace(
        pattern,
        value,
        RedactionReason.STRUCTURED_CREDENTIAL,
        counts,
        lambda _match: REDACTION_MARKER,
    )


def _redact_known_credentials_json(
    value: Any,
    credentials: set[str],
    counts: Counter[RedactionReason],
) -> Any:
    """Propagate structured credentials through every JSON string leaf."""
    if isinstance(value, dict):
        return {
            key: _redact_known_credentials_json(item, credentials, counts)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _redact_known_credentials_json(item, credentials, counts) for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            _redact_known_credentials_json(item, credentials, counts) for item in value
        )
    if isinstance(value, str):
        return _redact_known_credentials(value, credentials, counts)
    return value


def _redact_inference_message(
    message: InferenceMessage,
    credentials: set[str],
    counts: Counter[RedactionReason],
) -> InferenceMessage:
    parts = []
    for part in message.parts:
        if isinstance(part, (TextPart, ReasoningPart)):
            text = _redact_text(part.content, counts)
            parts.append(
                part.model_copy(
                    update={
                        "content": _redact_known_credentials(text, credentials, counts)
                    }
                )
            )
        elif isinstance(part, ToolCallPart):
            arguments = _redact_json(part.arguments, counts)
            parts.append(
                part.model_copy(
                    update={
                        "arguments": _redact_known_credentials_json(
                            arguments, credentials, counts
                        )
                    }
                )
            )
        elif isinstance(part, ToolCallResponsePart):
            result = _redact_json(part.result, counts)
            parts.append(
                part.model_copy(
                    update={
                        "result": _redact_known_credentials_json(
                            result, credentials, counts
                        )
                    }
                )
            )
    return message.model_copy(update={"parts": parts})


def redact_fact(fact: FactT) -> tuple[FactT, Counter[RedactionReason]]:
    """Return a redacted model copy and per-reason replacement counts."""
    counts: Counter[RedactionReason] = Counter()
    updates: dict[str, Any] = {"raw": _redact_json(fact.raw, counts)}
    if isinstance(fact, InferenceCall):
        structured_credentials = {
            credential
            for message in (*fact.input_messages, *fact.output_messages)
            for part in message.parts
            if isinstance(part, (ToolCallPart, ToolCallResponsePart))
            for credential in _structured_credentials(
                part.arguments if isinstance(part, ToolCallPart) else part.result
            )
        }
        updates.update(
            raw=_redact_known_credentials_json(
                updates["raw"], structured_credentials, counts
            ),
            input_messages=[
                _redact_inference_message(message, structured_credentials, counts)
                for message in fact.input_messages
            ],
            output_messages=[
                _redact_inference_message(message, structured_credentials, counts)
                for message in fact.output_messages
            ],
        )
    elif isinstance(fact, EditObservation):
        updates.update(
            applied_text=_redact_text(fact.applied_text, counts),
            observed_file_text=_redact_text(fact.observed_file_text, counts),
        )
    elif isinstance(fact, RejectedEdit):
        updates["proposed"] = _redact_text(fact.proposed, counts)
    elif isinstance(fact, CIOutcome) and fact.reason is not None:
        credentials = _structured_credentials(fact.raw)
        reason = _redact_text(fact.reason, counts)
        reason = _redact_known_credentials(reason, credentials, counts)
        if len(reason) > CI_REASON_MAX_LENGTH:
            reason = (
                reason[: CI_REASON_MAX_LENGTH - len(REDACTION_MARKER)]
                + REDACTION_MARKER
            )
        updates["reason"] = reason
    redacted = fact.model_copy(update=updates)
    return cast(FactT, redacted), counts
