# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure keyword selection of exact evidence from one bounded Session source."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import ConfigDict, Field
from sediment_core import (
    ContextCommitAnchor,
    ContextCommitMatch,
    ContextDiscoverySource,
    EvidenceContextSource,
    EvidenceReadItem,
    NonEmptyId,
)
from sediment_core.evidence import (
    CONTEXT_SCAN_PART_LIMIT,
    ContextScanMetadata,
    EvidenceIndex,
    EvidenceReadError,
    EvidenceSchemaVersion,
    encode_evidence_json,
    validate_evidence_numbers,
)

from .similarity import tokenize

CONTEXT_QUERY_BYTES_LIMIT = 2_048
CONTEXT_MIN_RESPONSE_BYTES = 4_096
CONTEXT_MAX_RESPONSE_BYTES = 65_536
CONTEXT_DEFAULT_RESPONSE_BYTES = 16_384
CONTEXT_ENVELOPE_BYTES = 2_048
CONTEXT_ITEM_LIMIT = 8
CONTEXT_STATE_BYTES_LIMIT = 32 * 1024 * 1024
CONTEXT_STATE_ENTRY_BYTES = 512
CONTEXT_SKIP_REASONS = (
    "reasoning_part",
    "non_finite_number",
    "no_match",
    "repeated_content",
    "item_limit",
    "response_budget",
)
CONTEXT_DISCOVERY_SKIP_REASONS = (
    "reasoning_part",
    "non_finite_number",
    "unmatched_part",
    "unmatched_session",
    "candidate_limit",
    "response_budget",
)
_QUERY_STOPWORDS = frozenset(
    "a an and are as at be by did do does for from how i in is it of on or "
    "that the this to was were what when where which who why with".split()
)


@dataclass(frozen=True)
class ContextRetrievalPolicy:
    """Version 1 pins the token, ranking, exclusion, and packing rules."""

    policy_version: EvidenceSchemaVersion = 1

    def __post_init__(self) -> None:
        if type(self.policy_version) is not int or self.policy_version != 1:
            raise ValueError("unsupported context retrieval policy")


@dataclass(frozen=True)
class ContextRetrievalCoverage:
    __pydantic_config__ = ConfigDict(extra="forbid")

    visible_inference_calls: EvidenceIndex
    quarantined_inference_calls: EvidenceIndex
    scanned_parts: EvidenceIndex
    complete_visible_scan: Literal[True]


@dataclass(frozen=True)
class ContextRetrievalSkipped:
    __pydantic_config__ = ConfigDict(extra="forbid")

    reasoning_part: EvidenceIndex
    non_finite_number: EvidenceIndex
    no_match: EvidenceIndex
    repeated_content: EvidenceIndex
    item_limit: EvidenceIndex
    response_budget: EvidenceIndex


@dataclass(frozen=True)
class ContextRetrievalItem:
    __pydantic_config__ = ConfigDict(extra="forbid")

    score: Annotated[int, Field(strict=True, ge=1)]
    evidence: EvidenceReadItem


@dataclass(frozen=True, kw_only=True)
class ContextRetrievalResult:
    __pydantic_config__ = ConfigDict(extra="forbid")

    schema_version: EvidenceSchemaVersion
    policy_version: EvidenceSchemaVersion
    source_session_id: NonEmptyId
    quarantine_revision: EvidenceIndex
    status: Literal["matched", "no_match", "budget_exhausted"]
    capture_completeness: Literal["unknown"]
    coverage: ContextRetrievalCoverage
    skipped: ContextRetrievalSkipped
    items: Annotated[
        tuple[ContextRetrievalItem, ...], Field(max_length=CONTEXT_ITEM_LIMIT)
    ]


@dataclass(frozen=True)
class ContextDiscoveryPolicy:
    """Version 1 ranks direct commit witnesses and distinct keyword overlap."""

    policy_version: EvidenceSchemaVersion = 1

    def __post_init__(self) -> None:
        if type(self.policy_version) is not int or self.policy_version != 1:
            raise ValueError("unsupported context discovery policy")


@dataclass(frozen=True)
class ContextDiscoveryCoverage:
    __pydantic_config__ = ConfigDict(extra="forbid")

    authorized_sessions: EvidenceIndex
    found_sessions: EvidenceIndex
    visible_inference_calls: EvidenceIndex
    quarantined_inference_calls: EvidenceIndex
    scanned_parts: EvidenceIndex
    matched_parts: EvidenceIndex
    complete_visible_scan: Literal[True]


@dataclass(frozen=True)
class ContextDiscoverySkipped:
    __pydantic_config__ = ConfigDict(extra="forbid")

    reasoning_part: EvidenceIndex
    non_finite_number: EvidenceIndex
    unmatched_part: EvidenceIndex
    unmatched_session: EvidenceIndex
    candidate_limit: EvidenceIndex
    response_budget: EvidenceIndex


@dataclass(frozen=True)
class ContextDiscoveryItem:
    __pydantic_config__ = ConfigDict(extra="forbid")

    session_id: NonEmptyId
    score: EvidenceIndex
    matched_parts: EvidenceIndex
    preview: EvidenceReadItem | None
    commit_match: ContextCommitMatch | None


@dataclass(frozen=True, kw_only=True)
class ContextDiscoveryResult:
    __pydantic_config__ = ConfigDict(extra="forbid")

    schema_version: EvidenceSchemaVersion
    policy_version: EvidenceSchemaVersion
    quarantine_revision: EvidenceIndex
    capture_completeness: Literal["unknown"]
    commit: ContextCommitAnchor | None
    status: Literal["matched", "no_match", "budget_exhausted"]
    coverage: ContextDiscoveryCoverage
    skipped: ContextDiscoverySkipped
    items: Annotated[
        tuple[ContextDiscoveryItem, ...], Field(max_length=CONTEXT_ITEM_LIMIT)
    ]


def context_query_tokens(query: str) -> set[str]:
    """Validate a question without echoing content; return meaningful tokens."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("invalid context query")
    try:
        encoded = query.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("invalid context query") from None
    if len(encoded) > CONTEXT_QUERY_BYTES_LIMIT:
        raise ValueError("invalid context query")
    tokens = tokenize(query) - _QUERY_STOPWORDS
    if not tokens:
        raise ValueError("invalid context query")
    return tokens


def _strict_json(value: object) -> str:
    validate_evidence_numbers(value)
    return json.dumps(
        value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def _search_text(item: EvidenceReadItem) -> str:
    part = item.part
    if part.type == "text":
        return part.content
    if part.type == "tool_call":
        return part.name + " " + _strict_json(part.arguments)
    if part.type == "tool_call_response":
        return _strict_json(part.result)
    raise ValueError("reasoning has no retrieval text")


def _rank_key(item: ContextRetrievalItem) -> tuple:
    reference = item.evidence.reference
    # A timedelta preserves microsecond ties across the entire datetime range;
    # floating-point Unix timestamps can collapse distinct observed instants.
    newest_first = datetime.max.replace(
        tzinfo=UTC
    ) - item.evidence.observed_at.astimezone(UTC)
    return (
        -item.score,
        newest_first,
        reference.inference_call_id,
        0 if reference.side == "input" else 1,
        reference.message_index,
        reference.part_index,
    )


def _content_key(item: EvidenceReadItem) -> str:
    return _strict_json(
        {
            "role": item.role,
            "finish_reason": item.finish_reason,
            "part": item.part.model_dump(mode="python"),
        }
    )


def _discovery_rank_key(item: ContextDiscoveryItem) -> tuple:
    return item.commit_match is None, -item.score, item.session_id


def _candidate(
    item: EvidenceReadItem,
    query_tokens: set[str],
    skipped: Counter[str],
    unmatched_reason: Literal["no_match", "unmatched_part"],
) -> ContextRetrievalItem | None:
    if item.part.type == "reasoning":
        skipped["reasoning_part"] += 1
        return None
    try:
        score = len(query_tokens & tokenize(_search_text(item)))
    except EvidenceReadError:
        skipped["non_finite_number"] += 1
        return None
    if not score:
        skipped[unmatched_reason] += 1
        return None
    return ContextRetrievalItem(score, item)


def _pack_candidates[Item](
    candidates: Iterable[Item], contract: type[Item], max_bytes: int
) -> tuple[tuple[Item, ...], int, int]:
    selected = []
    limited = oversized = 0
    remaining = max_bytes - CONTEXT_ENVELOPE_BYTES
    for candidate in candidates:
        if len(selected) == CONTEXT_ITEM_LIMIT:
            limited += 1
            continue
        comma_bytes = int(bool(selected))
        try:
            encoded = encode_evidence_json(
                candidate, contract, max_bytes=remaining - comma_bytes
            )
        except EvidenceReadError as exc:
            if exc.detail["reason"] != "evidence_response_limit":
                raise
            oversized += 1
            continue
        remaining -= len(encoded) + comma_bytes
        selected.append(candidate)
    return tuple(selected), limited, oversized


def _check_state_bytes(size: int) -> None:
    if size > CONTEXT_STATE_BYTES_LIMIT:
        raise EvidenceReadError(
            "retrieval_state_limit", limit_bytes=CONTEXT_STATE_BYTES_LIMIT
        )


def _state_encoding_size(value: object, contract: type) -> int:
    try:
        return len(
            encode_evidence_json(value, contract, max_bytes=CONTEXT_STATE_BYTES_LIMIT)
        )
    except EvidenceReadError as exc:
        if exc.detail["reason"] != "evidence_response_limit":
            raise
        raise EvidenceReadError(
            "retrieval_state_limit", limit_bytes=CONTEXT_STATE_BYTES_LIMIT
        ) from None


def _validate_response_budget(max_bytes: int) -> None:
    if (
        type(max_bytes) is not int
        or not CONTEXT_MIN_RESPONSE_BYTES <= max_bytes <= CONTEXT_MAX_RESPONSE_BYTES
    ):
        raise ValueError("invalid context response budget")


def retrieve_context_stream(
    metadata: ContextScanMetadata,
    items: Iterable[tuple[NonEmptyId, EvidenceReadItem]],
    query: str,
    max_bytes: int = CONTEXT_DEFAULT_RESPONSE_BYTES,
    policy: ContextRetrievalPolicy = ContextRetrievalPolicy(),
) -> ContextRetrievalResult:
    """Reduce a complete Session stream with exact keys and bounded state.

    Each group reserves its ASCII key, largest observed encoded candidate, and
    fixed overhead. Reservations only grow; only the best original occurrence
    remains retained. Encoded reservations don't measure Python resident memory.
    """
    query_tokens = context_query_tokens(query)
    _validate_response_budget(max_bytes)
    if len(metadata.sessions) != 1:
        raise ValueError("retrieval scan requires one found Session")
    session_id = metadata.sessions[0].session_id
    skipped = Counter({reason: 0 for reason in CONTEXT_SKIP_REASONS})
    groups: dict[str, tuple[ContextRetrievalItem, int]] = {}
    scanned_parts = state_bytes = 0
    for identifier, item in items:
        if identifier != session_id:
            raise ValueError("retrieval scan Session mismatch")
        scanned_parts += 1
        candidate = _candidate(item, query_tokens, skipped, "no_match")
        if candidate is None:
            continue
        key = _content_key(item)
        size = _state_encoding_size(candidate, ContextRetrievalItem)
        previous = groups.get(key)
        if previous is None:
            state_bytes += len(key) + size + CONTEXT_STATE_ENTRY_BYTES
            if len(groups) == CONTEXT_SCAN_PART_LIMIT:
                raise EvidenceReadError(
                    "retrieval_state_limit", limit_bytes=CONTEXT_STATE_BYTES_LIMIT
                )
            _check_state_bytes(state_bytes)
            groups[key] = candidate, size
        else:
            skipped["repeated_content"] += 1
            best, reserved = previous
            state_bytes += max(0, size - reserved)
            _check_state_bytes(state_bytes)
            if _rank_key(candidate) < _rank_key(best):
                best = candidate
            groups[key] = best, max(size, reserved)

    selected, skipped["item_limit"], skipped["response_budget"] = _pack_candidates(
        sorted((best for best, _ in groups.values()), key=_rank_key),
        ContextRetrievalItem,
        max_bytes,
    )
    result = ContextRetrievalResult(
        schema_version=1,
        policy_version=policy.policy_version,
        source_session_id=session_id,
        quarantine_revision=metadata.quarantine_revision,
        status="matched" if selected else "budget_exhausted" if groups else "no_match",
        capture_completeness="unknown",
        coverage=ContextRetrievalCoverage(
            visible_inference_calls=metadata.visible_inference_calls,
            quarantined_inference_calls=metadata.quarantined_inference_calls,
            scanned_parts=scanned_parts,
            complete_visible_scan=True,
        ),
        skipped=ContextRetrievalSkipped(**skipped),
        items=selected,
    )
    encode_evidence_json(
        replace(result, items=()),
        ContextRetrievalResult,
        max_bytes=CONTEXT_ENVELOPE_BYTES,
    )
    encode_evidence_json(result, ContextRetrievalResult, max_bytes=max_bytes)
    return result


def discover_context_stream(
    metadata: ContextScanMetadata,
    items: Iterable[tuple[NonEmptyId, EvidenceReadItem]],
    query: str,
    max_bytes: int = CONTEXT_DEFAULT_RESPONSE_BYTES,
    policy: ContextDiscoveryPolicy = ContextDiscoveryPolicy(),
) -> ContextDiscoveryResult:
    """Keep exact match counts and one best preview per found Session.

    Each found Session reserves fixed overhead plus its largest observed matched
    EvidenceReadItem encoding, even when a smaller occurrence becomes its best.
    The store independently bounds the content-free scan header and witnesses.
    """
    query_tokens = context_query_tokens(query)
    _validate_response_budget(max_bytes)
    skipped = Counter({reason: 0 for reason in CONTEXT_DISCOVERY_SKIP_REASONS})
    sessions: dict[NonEmptyId, tuple[int, ContextRetrievalItem | None, int]] = {
        session.session_id: (0, None, 0) for session in metadata.sessions
    }
    state_bytes = len(sessions) * CONTEXT_STATE_ENTRY_BYTES
    _check_state_bytes(state_bytes)
    scanned_parts = matched_parts = 0
    for identifier, item in items:
        if identifier not in sessions:
            raise ValueError("discovery scan Session mismatch")
        scanned_parts += 1
        candidate = _candidate(item, query_tokens, skipped, "unmatched_part")
        if candidate is None:
            continue
        count, best, reserved = sessions[identifier]
        matched_parts += 1
        size = _state_encoding_size(item, EvidenceReadItem)
        state_bytes += max(0, size - reserved)
        _check_state_bytes(state_bytes)
        if best is None or _rank_key(candidate) < _rank_key(best):
            best = candidate
        sessions[identifier] = count + 1, best, max(size, reserved)

    candidates = []
    for session in metadata.sessions:
        count, best, _ = sessions[session.session_id]
        if best is None and session.commit_match is None:
            skipped["unmatched_session"] += 1
            continue
        candidates.append(
            ContextDiscoveryItem(
                session.session_id,
                best.score if best else 0,
                count,
                best.evidence if best else None,
                session.commit_match,
            )
        )
    selected, skipped["candidate_limit"], skipped["response_budget"] = _pack_candidates(
        sorted(candidates, key=_discovery_rank_key),
        ContextDiscoveryItem,
        max_bytes,
    )
    result = ContextDiscoveryResult(
        schema_version=1,
        policy_version=policy.policy_version,
        quarantine_revision=metadata.quarantine_revision,
        capture_completeness="unknown",
        commit=metadata.commit,
        status="matched"
        if selected
        else "budget_exhausted"
        if candidates
        else "no_match",
        coverage=ContextDiscoveryCoverage(
            metadata.authorized_sessions,
            len(metadata.sessions),
            metadata.visible_inference_calls,
            metadata.quarantined_inference_calls,
            scanned_parts,
            matched_parts,
            True,
        ),
        skipped=ContextDiscoverySkipped(**skipped),
        items=selected,
    )
    encode_evidence_json(
        replace(result, items=()),
        ContextDiscoveryResult,
        max_bytes=CONTEXT_ENVELOPE_BYTES,
    )
    encode_evidence_json(result, ContextDiscoveryResult, max_bytes=max_bytes)
    return result


def retrieve_context(
    source: EvidenceContextSource,
    query: str,
    max_bytes: int = CONTEXT_DEFAULT_RESPONSE_BYTES,
    policy: ContextRetrievalPolicy = ContextRetrievalPolicy(),
) -> ContextRetrievalResult:
    """Select complete original parts; never persist or synthesize evidence."""
    query_tokens = context_query_tokens(query)
    _validate_response_budget(max_bytes)
    skipped = Counter({reason: 0 for reason in CONTEXT_SKIP_REASONS})
    candidates = []
    for item in source.items:
        candidate = _candidate(item, query_tokens, skipped, "no_match")
        if candidate is not None:
            candidates.append(candidate)

    def distinct_candidates():
        seen = set()
        for candidate in sorted(candidates, key=_rank_key):
            key = _content_key(candidate.evidence)
            if key in seen:
                skipped["repeated_content"] += 1
                continue
            seen.add(key)
            yield candidate

    selected, skipped["item_limit"], skipped["response_budget"] = _pack_candidates(
        distinct_candidates(), ContextRetrievalItem, max_bytes
    )

    result = ContextRetrievalResult(
        schema_version=1,
        policy_version=policy.policy_version,
        source_session_id=source.session_id,
        quarantine_revision=source.quarantine_revision,
        status="matched"
        if selected
        else "budget_exhausted"
        if candidates
        else "no_match",
        capture_completeness="unknown",
        coverage=ContextRetrievalCoverage(
            visible_inference_calls=source.visible_inference_calls,
            quarantined_inference_calls=source.quarantined_inference_calls,
            scanned_parts=len(source.items),
            complete_visible_scan=True,
        ),
        skipped=ContextRetrievalSkipped(**skipped),
        items=selected,
    )
    encode_evidence_json(
        replace(result, items=()),
        ContextRetrievalResult,
        max_bytes=CONTEXT_ENVELOPE_BYTES,
    )
    encode_evidence_json(result, ContextRetrievalResult, max_bytes=max_bytes)
    return result


def discover_context(
    source: ContextDiscoverySource,
    query: str,
    max_bytes: int = CONTEXT_DEFAULT_RESPONSE_BYTES,
    policy: ContextDiscoveryPolicy = ContextDiscoveryPolicy(),
) -> ContextDiscoveryResult:
    """Rank authorized Sessions without inventing previews or repository scope."""
    query_tokens = context_query_tokens(query)
    _validate_response_budget(max_bytes)
    skipped = Counter({reason: 0 for reason in CONTEXT_DISCOVERY_SKIP_REASONS})
    candidates = []
    scanned_parts = matched_parts = 0
    for session in source.sessions:
        matches = []
        for item in session.items:
            scanned_parts += 1
            candidate = _candidate(item, query_tokens, skipped, "unmatched_part")
            if candidate is not None:
                matches.append(candidate)
        matched_parts += len(matches)
        if not matches and session.commit_match is None:
            skipped["unmatched_session"] += 1
            continue
        best = min(matches, key=_rank_key) if matches else None
        candidates.append(
            ContextDiscoveryItem(
                session.session_id,
                best.score if best else 0,
                len(matches),
                best.evidence if best else None,
                session.commit_match,
            )
        )

    selected, skipped["candidate_limit"], skipped["response_budget"] = _pack_candidates(
        sorted(candidates, key=_discovery_rank_key), ContextDiscoveryItem, max_bytes
    )

    result = ContextDiscoveryResult(
        schema_version=1,
        policy_version=policy.policy_version,
        quarantine_revision=source.quarantine_revision,
        capture_completeness="unknown",
        commit=source.commit,
        status="matched"
        if selected
        else "budget_exhausted"
        if candidates
        else "no_match",
        coverage=ContextDiscoveryCoverage(
            source.authorized_sessions,
            len(source.sessions),
            source.visible_inference_calls,
            source.quarantined_inference_calls,
            scanned_parts,
            matched_parts,
            True,
        ),
        skipped=ContextDiscoverySkipped(**skipped),
        items=selected,
    )
    encode_evidence_json(
        replace(result, items=()),
        ContextDiscoveryResult,
        max_bytes=CONTEXT_ENVELOPE_BYTES,
    )
    encode_evidence_json(result, ContextDiscoveryResult, max_bytes=max_bytes)
    return result
